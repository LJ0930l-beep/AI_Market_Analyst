"""Bounded, point-in-time settlement for live predictions.

This service is deliberately independent from model scanning.  It only reads
stored SignalProposal payloads, public market bars, and the deterministic
outcome/performance layers; it never probes model resources or creates paper
trades.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any, Callable

from .instruments import Instrument
from .outcomes import OutcomeStatus, evaluate_outcome_as_of
from .performance.metrics import build_performance_snapshot
from .providers import MarketProvider, ProviderError, fetch_market_data
from .signals import Action, SignalProposal, signal_from_payload
from .storage import SQLiteStore


SETTLEMENT_VERSION = "settlement_v1"
LIVE_PERFORMANCE_VERSION = "live_performance_v1"
SETTLEMENT_BATCH_LIMIT = 100
SETTLEMENT_BARS_LIMIT = 500
SETTLEMENT_RETRY_SECONDS = 300
PERFORMANCE_SNAPSHOT_RETENTION = 100


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


class SettlementService:
    """Settle a bounded batch of live predictions with injected public data."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        provider_factory: Callable[[Instrument], MarketProvider],
        clock: Callable[[], datetime],
        batch_size: int = SETTLEMENT_BATCH_LIMIT,
        bars_limit: int = SETTLEMENT_BARS_LIMIT,
        retry_after_seconds: int = SETTLEMENT_RETRY_SECONDS,
        snapshot_retention: int = PERFORMANCE_SNAPSHOT_RETENTION,
    ) -> None:
        self.store = store
        self.provider_factory = provider_factory
        self.clock = clock
        self.batch_size = max(1, min(int(batch_size), 500))
        self.bars_limit = max(1, min(int(bars_limit), 2000))
        self.retry_after_seconds = max(1, min(int(retry_after_seconds), 86_400))
        self.snapshot_retention = max(1, min(int(snapshot_retention), PERFORMANCE_SNAPSHOT_RETENTION))

    def _now(self) -> datetime:
        return _as_utc(self.clock())

    @staticmethod
    def _capability(*, provider_available: bool, bars_returned: int = 0, error: str | None = None) -> dict[str, object]:
        capability: dict[str, object] = {
            "version": SETTLEMENT_VERSION,
            "settlement": "python_point_in_time",
            "provider_available": provider_available,
            "model_scan_resource_guard": "not_used",
            "future_bar_filter": "generated_at_lt_timestamp_lte_as_of",
            "bars_returned": bars_returned,
        }
        if error:
            capability["error"] = error
        return capability

    def _item_id(self, run_id: str, prediction_id: str) -> str:
        return f"{run_id}:settlement:{prediction_id}"

    def _finish_item(
        self,
        item_id: str,
        *,
        status: str,
        point: datetime,
        provider: str | None = None,
        provider_as_of: str | None = None,
        capability: dict[str, object] | None = None,
        outcome_status: str | None = None,
        skip_reason: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        retry_after_at: str | None = None,
    ) -> None:
        self.store.update_scheduler_item(
            item_id,
            status=status,
            finished_at=_iso(point),
            provider=provider,
            provider_as_of=provider_as_of,
            as_of=_iso(point),
            capability=capability,
            outcome_status=outcome_status,
            skip_reason=skip_reason,
            error_code=error_code,
            error_detail=error_detail,
            retry_after_at=retry_after_at,
        )

    def _mark_interrupted(self, item_id: str, point: datetime) -> None:
        self._finish_item(
            item_id,
            status="INTERRUPTED",
            point=point,
            skip_reason="STOP_REQUESTED",
            error_code="SETTLEMENT_STOP_REQUESTED",
            error_detail="settlement stop requested before item completion",
            capability=self._capability(provider_available=False, error="stop_requested"),
        )

    @staticmethod
    def _performance_fingerprint(snapshot: dict[str, Any]) -> str:
        metrics = snapshot.get("metrics")
        normalized = dict(metrics) if isinstance(metrics, dict) else {}
        normalized.pop("audit_metadata", None)
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def _performance_refresh(self, point: datetime) -> dict[str, object]:
        records = self.store.list_prediction_records(source_type="live")
        snapshot = build_performance_snapshot(
            records,
            scope={"source_type": "live"},
        )
        snapshot["created_at"] = _iso(point)
        audit_metadata = {
            "snapshot_version": LIVE_PERFORMANCE_VERSION,
            "refreshed_by": "settlement",
            "settlement_as_of": _iso(point),
        }
        snapshot["audit_metadata"] = audit_metadata
        metrics = snapshot.get("metrics")
        if isinstance(metrics, dict):
            metrics["audit_metadata"] = audit_metadata
        fingerprint = self._performance_fingerprint(snapshot)
        latest = self.store.list_performance_snapshots(source_type="live", limit=1)
        if latest and self._performance_fingerprint(latest[0]) == fingerprint:
            pruned = self.store.prune_performance_snapshots(source_type="live", keep=self.snapshot_retention)
            evidence = {
                "version": LIVE_PERFORMANCE_VERSION,
                "snapshot_id": latest[0]["snapshot_id"],
                "status": latest[0]["status"],
                "as_of": _iso(point),
                "sample_count": latest[0]["sample_count"],
                "actionable_count": latest[0]["actionable_count"],
                "capability": "live_records_only_no_calibration_mutation",
                "reused": True,
                "metrics_fingerprint": fingerprint,
                "retention": self.snapshot_retention,
                "pruned": pruned,
            }
            self.store.set_scheduler_state("last_performance_refresh", evidence, updated_at=_iso(point))
            return evidence
        snapshot_id = self.store.save_performance_snapshot(snapshot)
        pruned = self.store.prune_performance_snapshots(source_type="live", keep=self.snapshot_retention)
        evidence = {
            "version": LIVE_PERFORMANCE_VERSION,
            "snapshot_id": snapshot_id,
            "status": snapshot.get("status", "PRELIMINARY"),
            "as_of": _iso(point),
            "sample_count": snapshot.get("sample_count", 0),
            "actionable_count": snapshot.get("actionable_count", 0),
            "capability": "live_records_only_no_calibration_mutation",
            "reused": False,
            "metrics_fingerprint": fingerprint,
            "retention": self.snapshot_retention,
            "pruned": pruned,
        }
        self.store.set_scheduler_state("last_performance_refresh", evidence, updated_at=_iso(point))
        return evidence

    def run_once(
        self,
        *,
        run_id: str,
        as_of: datetime | None = None,
        stop_event: Event | None = None,
    ) -> dict[str, object]:
        point = _as_utc(as_of or self._now())
        records = self.store.list_unsettled_live_prediction_records(limit=self.batch_size, as_of=point)
        counts: dict[str, int] = {
            "planned": len(records),
            "settled": 0,
            "pending": 0,
            "wait": 0,
            "errors": 0,
            "provider_errors": 0,
            "interrupted": 0,
            "idempotent": 0,
            "provider_fetches": 0,
            "provider_reused": 0,
            "performance_refreshes": 0,
            "performance_reused": 0,
        }
        items: list[dict[str, Any]] = []
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        interrupted = False

        for record in records:
            prediction_id = str(record["prediction_id"])
            payload = record.get("prediction")
            symbol = str(record.get("symbol") or "unknown")
            timeframe = "unknown"
            if isinstance(payload, dict):
                timeframe = str(payload.get("analysis_timeframe") or "unknown")
                nested = payload.get("instrument")
                if isinstance(nested, dict) and nested.get("symbol"):
                    symbol = str(nested["symbol"])
            item_id = self._item_id(run_id, prediction_id)
            self.store.create_scheduler_item(
                item_id=item_id,
                run_id=run_id,
                symbol=symbol,
                timeframe=timeframe,
                stage="settlement",
                prediction_id=prediction_id,
            )
            item = {"item_id": item_id, "prediction_id": prediction_id, "record": record, "symbol": symbol, "timeframe": timeframe}
            items.append(item)
            previous = self.store.get_latest_settlement_item(prediction_id)
            retry_after = previous.get("retry_after_at") if previous else None
            if isinstance(retry_after, str):
                try:
                    retry_point = _as_utc(datetime.fromisoformat(retry_after))
                except ValueError:
                    retry_point = None
                if retry_point is not None and retry_point > point:
                    self._finish_item(
                        item_id,
                        status="PENDING",
                        point=point,
                        skip_reason="PROVIDER_BACKOFF_ACTIVE",
                        error_code="PROVIDER_BACKOFF_ACTIVE",
                        error_detail="previous provider failure has a bounded retry window",
                        retry_after_at=_iso(retry_point),
                        capability=self._capability(provider_available=False, error="bounded_provider_backoff"),
                    )
                    counts["pending"] += 1
                    item["skip"] = True
                    continue
            if stop_event is not None and stop_event.is_set():
                interrupted = True
                self._mark_interrupted(item_id, point)
                counts["interrupted"] += 1
                item["skip"] = True
                continue
            if record.get("prediction_error") or not isinstance(payload, dict):
                counts["errors"] += 1
                self._finish_item(
                    item_id,
                    status="ERROR",
                    point=point,
                    error_code="SETTLEMENT_BAD_PAYLOAD",
                    error_detail=str(record.get("prediction_error") or "stored prediction payload is invalid"),
                    capability=self._capability(provider_available=False, error="bad_payload"),
                    skip_reason="PAYLOAD_QUARANTINED",
                    retry_after_at=_iso(point + timedelta(seconds=self.retry_after_seconds)),
                )
                item["skip"] = True
                continue
            try:
                instrument = self.store.resolve_instrument(symbol)
                signal = signal_from_payload(payload, instrument=instrument)
                item["instrument"] = instrument
                item["signal"] = signal
            except Exception as exc:
                counts["errors"] += 1
                self._finish_item(
                    item_id,
                    status="ERROR",
                    point=point,
                    error_code="SETTLEMENT_BAD_PAYLOAD",
                    error_detail=str(exc),
                    capability=self._capability(provider_available=False, error="rehydration_failed"),
                    skip_reason="PAYLOAD_QUARANTINED",
                    retry_after_at=_iso(point + timedelta(seconds=self.retry_after_seconds)),
                )
                item["skip"] = True
                continue
            signal = item["signal"]
            if not isinstance(signal, SignalProposal):
                counts["errors"] += 1
                self._finish_item(
                    item_id,
                    status="ERROR",
                    point=point,
                    error_code="SETTLEMENT_BAD_PAYLOAD",
                    error_detail="rehydration did not produce SignalProposal",
                    skip_reason="PAYLOAD_QUARANTINED",
                    retry_after_at=_iso(point + timedelta(seconds=self.retry_after_seconds)),
                )
                item["skip"] = True
                continue
            if signal.action is Action.WAIT:
                self.store.update_scheduler_item(item_id, status="RUNNING", started_at=_iso(point))
                outcome = evaluate_outcome_as_of(signal, [], point)
                assert outcome is not None and outcome.status is OutcomeStatus.NOT_ACTIONABLE
                saved = self.store.save_outcome(outcome)
                if saved:
                    counts["settled"] += 1
                else:
                    counts["idempotent"] += 1
                counts["wait"] += 1
                self._finish_item(
                    item_id,
                    status="COMPLETED",
                    point=point,
                    outcome_status=outcome.status.value,
                    capability=self._capability(provider_available=False, error="wait_not_actionable"),
                )
                item["skip"] = True
                continue
            grouped.setdefault((instrument.symbol, signal.analysis_timeframe), []).append(item)

        for group_key, group in grouped.items():
            if any(item.get("skip") for item in group):
                continue
            if stop_event is not None and stop_event.is_set():
                interrupted = True
                for item in group:
                    self._mark_interrupted(item["item_id"], point)
                    counts["interrupted"] += 1
                continue
            instrument = group[0]["instrument"]
            provider_name = "unknown"
            try:
                provider = self.provider_factory(instrument)
                provider_name = str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))
                bundle = fetch_market_data(provider, instrument, group_key[1], self.bars_limit)
                counts["provider_fetches"] += 1
                if len(group) > 1:
                    counts["provider_reused"] += len(group) - 1
            except ProviderError as exc:
                counts["provider_errors"] += 1
                retry_at = point + timedelta(seconds=self.retry_after_seconds)
                provider_name = exc.provider or provider_name
                for item in group:
                    if stop_event is not None and stop_event.is_set():
                        interrupted = True
                        self._mark_interrupted(item["item_id"], point)
                        counts["interrupted"] += 1
                        continue
                    counts["pending"] += 1
                    self._finish_item(
                        item["item_id"],
                        status="PENDING",
                        point=point,
                        provider=provider_name,
                        capability=self._capability(provider_available=False, error=exc.code),
                        error_code=f"PROVIDER_{exc.code.upper()}",
                        error_detail=str(exc),
                        skip_reason="PROVIDER_UNAVAILABLE",
                        retry_after_at=_iso(retry_at),
                    )
                continue
            except Exception as exc:
                counts["provider_errors"] += 1
                retry_at = point + timedelta(seconds=self.retry_after_seconds)
                for item in group:
                    counts["pending"] += 1
                    self._finish_item(
                        item["item_id"],
                        status="PENDING",
                        point=point,
                        provider=provider_name,
                        capability=self._capability(provider_available=False, error="provider_error"),
                        error_code="PROVIDER_ERROR",
                        error_detail=str(exc),
                        skip_reason="PROVIDER_UNAVAILABLE",
                        retry_after_at=_iso(retry_at),
                    )
                continue

            provider_as_of = _iso(bundle.data_as_of)
            capability = self._capability(provider_available=True, bars_returned=len(bundle.bars))
            for item in group:
                if stop_event is not None and stop_event.is_set():
                    interrupted = True
                    self._mark_interrupted(item["item_id"], point)
                    counts["interrupted"] += 1
                    continue
                self.store.update_scheduler_item(item["item_id"], status="RUNNING", started_at=_iso(point))
                try:
                    outcome = evaluate_outcome_as_of(item["signal"], list(bundle.bars), point)
                    if outcome is None:
                        counts["pending"] += 1
                        self._finish_item(
                            item["item_id"],
                            status="PENDING",
                            point=point,
                            provider=provider_name,
                            provider_as_of=provider_as_of,
                            capability=capability,
                            skip_reason="OUTCOME_PENDING",
                        )
                        continue
                    saved = self.store.save_outcome(outcome)
                    if saved:
                        counts["settled"] += 1
                    else:
                        counts["idempotent"] += 1
                    self._finish_item(
                        item["item_id"],
                        status="COMPLETED",
                        point=point,
                        provider=provider_name,
                        provider_as_of=provider_as_of,
                        capability=capability,
                        outcome_status=outcome.status.value,
                    )
                except Exception as exc:
                    counts["errors"] += 1
                    self._finish_item(
                        item["item_id"],
                        status="ERROR",
                        point=point,
                        provider=provider_name,
                        provider_as_of=provider_as_of,
                        capability=self._capability(provider_available=True, bars_returned=len(bundle.bars), error="evaluation_error"),
                        error_code="SETTLEMENT_ITEM_ERROR",
                        error_detail=str(exc),
                        skip_reason="EVALUATION_QUARANTINED",
                        retry_after_at=_iso(point + timedelta(seconds=self.retry_after_seconds)),
                    )

        if stop_event is not None and stop_event.is_set():
            interrupted = True
        if interrupted:
            for item in items:
                if item.get("skip"):
                    continue
                latest = self.store.get_latest_settlement_item(item["prediction_id"])
                if latest and latest.get("status") in {"PENDING", "RUNNING"} and latest.get("run_id") == run_id:
                    self._mark_interrupted(item["item_id"], point)
                    counts["interrupted"] += 1
        performance: dict[str, object] | None = None
        if not interrupted:
            performance = self._performance_refresh(point)
            if performance.get("reused"):
                counts["performance_reused"] = 1
            else:
                counts["performance_refreshes"] = 1
        status = "INTERRUPTED" if interrupted else "COMPLETED_WITH_ERRORS" if counts["errors"] or counts["provider_errors"] else "COMPLETED"
        evidence = {
            "version": SETTLEMENT_VERSION,
            "run_id": run_id,
            "as_of": _iso(point),
            "status": status,
            "counts": counts,
            "capability": {
                "model_scan_resource_guard": "not_used",
                "provider_fetch": "batched_by_symbol_timeframe",
                "future_leakage_guard": "generated_at_lt_timestamp_lte_as_of",
            },
        }
        self.store.set_scheduler_state("last_settlement", evidence, updated_at=_iso(point))
        return {"status": status, "counts": counts, "performance": performance}


__all__ = ["LIVE_PERFORMANCE_VERSION", "SETTLEMENT_VERSION", "SettlementService"]
