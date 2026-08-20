"""Deterministic local alert reconciliation for the Phase 5 runtime.

Alerts are durable observability evidence only.  This module never invokes a
model, sends a notification, creates a PaperTrade, or changes financial
records.  Event identity is explicit so repeated scheduler runs coalesce into
one row while materially new evidence remains visible.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .radar import build_radar
from .signals import SignalProposal, signal_from_payload
from .storage import SQLiteStore


ALERT_POLICY_VERSION = "alert_policy_v1"
ALERT_RETENTION_LIMIT = 500
ALERT_OPERATIONAL_COOLDOWN_SECONDS = 15 * 60
ALERT_RECORD_BATCH_LIMIT = 500
ALERT_PAGE_LIMIT = 100
ALERT_SOURCES = frozenset({"prediction", "outcome", "radar", "news_event", "operational"})
ALERT_SEVERITIES = frozenset({"INFO", "WARNING", "CRITICAL"})
ALERT_STATUSES = frozenset({"OPEN", "ACKNOWLEDGED"})


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _normalized(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _digest(value: object, *, length: int = 32) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _number(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _context(prediction: dict[str, Any]) -> dict[str, Any]:
    value = prediction.get("context_json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def alert_policy() -> dict[str, object]:
    return {
        "version": ALERT_POLICY_VERSION,
        "mode": "local_observability_only",
        "sources": sorted(ALERT_SOURCES),
        "dedupe": "deterministic event identity; operational failures coalesce in bounded UTC cooldown windows",
        "operational_cooldown_seconds": ALERT_OPERATIONAL_COOLDOWN_SECONDS,
        "retention_limit": ALERT_RETENTION_LIMIT,
        "retention": "open alerts are retained before acknowledged history; oldest acknowledged rows are pruned first, then oldest open rows only when necessary",
        "acknowledgement": "single-alert idempotent acknowledgement",
        "outbound_notifiers": False,
        "real_orders": False,
        "paper_trades_mutated": False,
        "calibration_mutated": False,
    }


def alert_capabilities() -> dict[str, object]:
    return {
        "reconciliation": "after_settlement_and_scan",
        "prediction": True,
        "outcome": True,
        "radar_transition": True,
        "news_event_evidence": True,
        "news_event_provenance": "stored_prediction_context_only_no_external_fetch",
        "provider_failure": True,
        "resource_failure": True,
        "operational_cooldown_seconds": ALERT_OPERATIONAL_COOLDOWN_SECONDS,
        "retention_limit": ALERT_RETENTION_LIMIT,
        "model_invoked": False,
        "outbound_notifiers": False,
        "broker_or_real_order": False,
    }


class AlertReconciler:
    """Reconcile stored deterministic evidence into durable deduped alerts."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        clock: Callable[[], datetime],
        retention_limit: int = ALERT_RETENTION_LIMIT,
        operational_cooldown_seconds: int = ALERT_OPERATIONAL_COOLDOWN_SECONDS,
    ) -> None:
        self.store = store
        self.clock = clock
        self.retention_limit = max(1, min(int(retention_limit), ALERT_RETENTION_LIMIT))
        self.operational_cooldown_seconds = max(60, min(int(operational_cooldown_seconds), 86_400))

    def _now(self, value: datetime | None = None) -> datetime:
        return _as_utc(value or self.clock())

    def _emit(
        self,
        *,
        source: str,
        severity: str,
        title: str,
        message: str,
        symbol: str | None,
        prediction_id: str | None,
        event_identity: str,
        dedupe_key: str,
        evidence: dict[str, Any],
        point: datetime,
    ) -> tuple[bool, dict[str, object]]:
        result = self.store.upsert_alert(
            alert_id=f"alert-{_digest({'policy': ALERT_POLICY_VERSION, 'source': source, 'identity': event_identity}, length=24)}",
            policy_version=ALERT_POLICY_VERSION,
            source=source,
            severity=severity,
            title=title,
            message=message,
            symbol=symbol,
            prediction_id=prediction_id,
            event_identity=event_identity,
            fingerprint=_digest(evidence),
            evidence=evidence,
            dedupe_key=dedupe_key,
            first_seen_at=_iso(point),
        )
        return bool(result["created"]), result["alert"]  # type: ignore[return-value]

    @staticmethod
    def _prediction_symbol(record: dict[str, Any], prediction: dict[str, Any]) -> str:
        nested = prediction.get("instrument")
        return str(
            prediction.get("symbol")
            or (nested.get("symbol") if isinstance(nested, dict) else None)
            or record.get("symbol")
            or "UNKNOWN"
        ).upper()

    @staticmethod
    def _validated_actionable(
        prediction: dict[str, Any],
        *,
        counts: dict[str, int],
    ) -> SignalProposal | None:
        action = str(prediction.get("action") or "").upper()
        if action not in {"LONG", "SHORT"}:
            return None
        try:
            signal = signal_from_payload(prediction)
        except (TypeError, ValueError):
            counts["errors"] += 1
            counts["quarantined"] += 1
            return None
        if signal.action.value not in {"LONG", "SHORT"}:
            return None
        return signal

    def _reconcile_prediction(
        self,
        record: dict[str, Any],
        *,
        point: datetime,
        counts: dict[str, int],
    ) -> None:
        prediction = record.get("prediction")
        if not isinstance(prediction, dict):
            counts["errors"] += 1
            return
        signal = self._validated_actionable(prediction, counts=counts)
        if signal is None:
            return
        symbol = signal.instrument.symbol
        prediction_id = signal.prediction_id
        if not prediction_id:
            counts["errors"] += 1
            return
        action = signal.action.value
        generated_at = prediction.get("generated_at") or record.get("generated_at")
        evidence = {
            "prediction_id": prediction_id,
            "symbol": symbol,
            "action": action,
            "timeframe": prediction.get("analysis_timeframe"),
            "generated_at": generated_at,
            "data_as_of": prediction.get("data_as_of"),
            "raw_confidence": _number(prediction.get("raw_confidence")),
            "calibrated_confidence": _number(prediction.get("calibrated_confidence")),
            "model_id": prediction.get("model_id"),
            "source_type": prediction.get("source_type", "live"),
        }
        created, _ = self._emit(
            source="prediction",
            severity="INFO",
            title=f"New {action} prediction · {symbol}",
            message=f"A new actionable {action} prediction is available for {symbol}.",
            symbol=symbol,
            prediction_id=prediction_id,
            event_identity=f"prediction:{prediction_id}",
            dedupe_key=f"prediction:{prediction_id}",
            evidence=evidence,
            point=point,
        )
        counts["prediction_created" if created else "deduped"] += 1

        outcome = record.get("outcome")
        if not isinstance(outcome, dict):
            return
        outcome_status = str(outcome.get("status") or record.get("outcome_status") or "").upper()
        if not outcome_status or outcome_status in {"PENDING", "NOT_ACTIONABLE"}:
            return
        outcome_evidence = {
            "prediction_id": prediction_id,
            "symbol": symbol,
            "action": action,
            "status": outcome_status,
            "settled_at": outcome.get("settled_at"),
            "exit_price": _number(outcome.get("exit_price")),
            "realized_r": _number(outcome.get("realized_r")),
            "bars_held": outcome.get("bars_held"),
        }
        created, _ = self._emit(
            source="outcome",
            severity="INFO" if outcome_status in {"TP1", "TP2"} else "WARNING",
            title=f"Outcome settled · {symbol} {outcome_status}",
            message=f"The {action} prediction for {symbol} settled as {outcome_status}.",
            symbol=symbol,
            prediction_id=prediction_id,
            event_identity=f"outcome:{prediction_id}:{outcome_status}",
            dedupe_key=f"outcome:{prediction_id}:{outcome_status}",
            evidence=outcome_evidence,
            point=point,
        )
        counts["outcome_created" if created else "deduped"] += 1

    @staticmethod
    def _event_key(event: dict[str, Any]) -> str:
        stable = event.get("dedupe_hash") or event.get("event_id") or event.get("id")
        if not stable:
            stable = {
                "source": _normalized(event.get("source")),
                "title": _normalized(event.get("title")),
                "url": _normalized(event.get("url")),
            }
        return _digest({"stable": stable, "published_at": _normalized(event.get("published_at"))})

    def _reconcile_context_events(
        self,
        record: dict[str, Any],
        *,
        point: datetime,
        counts: dict[str, int],
    ) -> None:
        prediction = record.get("prediction")
        if not isinstance(prediction, dict):
            return
        action = str(prediction.get("action") or "").upper()
        if action not in {"LONG", "SHORT"}:
            return
        signal = self._validated_actionable(prediction, counts=counts)
        if signal is None:
            return
        symbol = signal.instrument.symbol
        prediction_id = signal.prediction_id
        context = _context(prediction)
        time_policy = context.get("time_policy") if isinstance(context.get("time_policy"), dict) else {}
        explicit_event_risk = time_policy.get("event_risk")
        grouped: dict[str, dict[str, Any]] = {}
        for field in ("news", "risk_events"):
            raw_events = context.get(field)
            if not isinstance(raw_events, list):
                continue
            for raw_event in raw_events:
                if not isinstance(raw_event, dict):
                    continue
                key = self._event_key(raw_event)
                current = grouped.setdefault(
                    key,
                    {
                        "source": raw_event.get("source"),
                        "event_id": raw_event.get("event_id") or raw_event.get("id"),
                        "dedupe_hash": raw_event.get("dedupe_hash"),
                        "title": raw_event.get("title"),
                        "published_at": raw_event.get("published_at"),
                        "url": raw_event.get("url"),
                        "category": raw_event.get("category"),
                        "importance": raw_event.get("importance"),
                        "provenance": [],
                    },
                )
                current["provenance"].append(f"context.{field}")
                if field == "risk_events":
                    current_importance = _number(current.get("importance")) or 0
                    event_importance = _number(raw_event.get("importance")) or 0
                    current["importance"] = max(int(current_importance), int(event_importance))
        if explicit_event_risk is True and not grouped:
            grouped["time-policy-event-risk"] = {
                "source": "stored_context",
                "event_id": None,
                "dedupe_hash": None,
                "title": "Explicit stored event risk",
                "published_at": None,
                "url": None,
                "category": "event_risk",
                "importance": 100,
                "provenance": ["context.time_policy.event_risk"],
            }
        for key, event in grouped.items():
            importance = int(_number(event.get("importance")) or 0)
            high_impact = explicit_event_risk is True or importance >= 70
            event_evidence = {
                "symbol": symbol,
                "prediction_id": prediction_id,
                "source": event.get("source"),
                "event_id": event.get("event_id"),
                "dedupe_hash": event.get("dedupe_hash"),
                "title": event.get("title"),
                "published_at": event.get("published_at"),
                "url": event.get("url"),
                "category": event.get("category"),
                "importance": importance,
                "high_impact": high_impact,
                "provenance": sorted(
                    set(event.get("provenance") or [])
                    | ({"context.time_policy.event_risk"} if explicit_event_risk is True else set())
                ),
            }
            event_identity = f"{symbol}:{key}"
            created, _ = self._emit(
                source="news_event",
                severity="WARNING" if high_impact else "INFO",
                title=f"News/event evidence · {symbol}",
                message=str(event.get("title") or "Stored news or event evidence requires review."),
                symbol=symbol,
                prediction_id=prediction_id,
                event_identity=event_identity,
                dedupe_key=f"news_event:{event_identity}",
                evidence=event_evidence,
                point=point,
            )
            counts["news_event_created" if created else "deduped"] += 1

    def _reconcile_radar(self, *, point: datetime, counts: dict[str, int]) -> None:
        entries = self.store.list_watchlist_entries()
        symbols = [str(entry["symbol"]) for entry in entries]
        instruments = {}
        for symbol in symbols:
            try:
                instruments[symbol] = self.store.resolve_instrument(symbol)
            except (TypeError, ValueError):
                counts["errors"] += 1
        if not instruments:
            self.store.set_scheduler_state("alerts.radar_state", {}, updated_at=_iso(point))
            return
        predictions = self.store.list_latest_prediction_records(tuple(instruments), source_type="live")
        calibrations = self.store.list_calibration_results(limit=1)
        radar = build_radar(
            entries,
            instruments=instruments,
            predictions=predictions,
            calibration=calibrations[0] if calibrations else None,
            now=point,
        )
        previous_raw = self.store.get_scheduler_state("alerts.radar_state")
        previous = previous_raw if isinstance(previous_raw, dict) else {}
        current: dict[str, dict[str, object]] = {}
        for entry in radar.get("entries", []):
            if not isinstance(entry, dict):
                continue
            symbol = str(entry.get("symbol") or "").upper()
            category = str(entry.get("category") or "NOT_RANKED")
            if not symbol:
                continue
            prior = previous.get(symbol)
            prior_state = prior if isinstance(prior, dict) else {"category": prior} if isinstance(prior, str) else {}
            prior_category = str(prior_state.get("category") or "")
            sequence = int(prior_state.get("transition_sequence") or 0)
            if prior_category and prior_category != category:
                sequence += 1
                evidence = {
                    "symbol": symbol,
                    "from_category": prior_category,
                    "to_category": category,
                    "prediction_id": entry.get("prediction_id"),
                    "score": entry.get("score"),
                    "as_of": radar.get("as_of"),
                    "transition_sequence": sequence,
                }
                created, _ = self._emit(
                    source="radar",
                    severity="WARNING" if category in {"AVOID", "NOT_RANKED"} else "INFO",
                    title=f"Radar transition · {symbol}",
                    message=f"{symbol} moved from {prior_category} to {category}.",
                    symbol=symbol,
                    prediction_id=str(entry.get("prediction_id")) if entry.get("prediction_id") else None,
                    event_identity=f"radar:{symbol}:{prior_category}->{category}:{sequence}",
                    dedupe_key=f"radar:{symbol}:{prior_category}->{category}:{sequence}",
                    evidence=evidence,
                    point=point,
                )
                counts["radar_created" if created else "deduped"] += 1
            current[symbol] = {
                "category": category,
                "prediction_id": entry.get("prediction_id"),
                "score": entry.get("score"),
                "transition_sequence": sequence,
                "as_of": radar.get("as_of"),
            }
        self.store.set_scheduler_state("alerts.radar_state", current, updated_at=_iso(point))

    def _reconcile_operational(self, *, run_id: str | None, point: datetime, counts: dict[str, int]) -> None:
        if not run_id:
            return
        items = self.store.list_scheduler_items(run_id)
        for item in items:
            status = str(item.get("status") or "")
            error_code = str(item.get("error_code") or "").strip()
            reason = str(item.get("resource_reason") or "").strip()
            skip_reason = str(item.get("skip_reason") or "").strip()
            if not error_code and not reason:
                continue
            if skip_reason in {"OUTCOME_PENDING", "PAYLOAD_QUARANTINED"} and not reason and not error_code.startswith(("PROVIDER_", "SETTLEMENT_")):
                continue
            if status not in {"ERROR", "PENDING", "SKIPPED", "INTERRUPTED"} and not reason:
                continue
            failure = reason or error_code
            if failure in {"SCHEDULER_STOP_REQUESTED", "SETTLEMENT_STOP_REQUESTED"}:
                continue
            observed = item.get("as_of") or item.get("finished_at") or point.isoformat()
            try:
                observed_at = _as_utc(datetime.fromisoformat(str(observed).replace("Z", "+00:00")))
            except ValueError:
                observed_at = point
            window_start_epoch = int(observed_at.timestamp()) // self.operational_cooldown_seconds * self.operational_cooldown_seconds
            window_start = datetime.fromtimestamp(window_start_epoch, tz=timezone.utc)
            symbol = str(item.get("symbol") or "UNKNOWN").upper()
            stage = str(item.get("stage") or "scan")
            event_identity = f"{stage}:{symbol}:{failure}:{_iso(window_start)}"
            evidence = {
                "run_id": run_id,
                "item_id": item.get("item_id"),
                "stage": stage,
                "symbol": symbol,
                "status": status,
                "failure": failure,
                "error_code": item.get("error_code"),
                "resource_reason": item.get("resource_reason"),
                "provider": item.get("provider"),
                "retry_after_at": item.get("retry_after_at"),
                "capability": item.get("capability"),
                "cooldown_window_start": _iso(window_start),
            }
            created, _ = self._emit(
                source="operational",
                severity="WARNING",
                title=f"{stage.title()} failure · {symbol}",
                message=f"{stage.title()} evidence reports {failure} for {symbol}.",
                symbol=symbol,
                prediction_id=str(item.get("prediction_id")) if item.get("prediction_id") else None,
                event_identity=event_identity,
                dedupe_key=f"operational:{event_identity}",
                evidence=evidence,
                point=point,
            )
            counts["operational_created" if created else "deduped"] += 1

    def status(self) -> dict[str, object]:
        return {
            "policy": alert_policy(),
            "capabilities": alert_capabilities(),
            "counts": self.store.alert_counts(),
            "last_reconciliation": self.store.get_scheduler_state("last_alert_reconciliation"),
        }

    def run_once(
        self,
        *,
        run_id: str | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        point = self._now(as_of)
        records = self.store.list_live_prediction_records_for_alerts(limit=ALERT_RECORD_BATCH_LIMIT)
        counts: dict[str, int] = {
            "records": len(records),
            "prediction_created": 0,
            "outcome_created": 0,
            "radar_created": 0,
            "news_event_created": 0,
            "operational_created": 0,
            "deduped": 0,
            "errors": 0,
            "quarantined": 0,
            "pruned": 0,
        }
        error_details: list[dict[str, str]] = []
        for record in records:
            try:
                self._reconcile_prediction(record, point=point, counts=counts)
                self._reconcile_context_events(record, point=point, counts=counts)
            except Exception as exc:  # isolate one malformed record from other evidence
                counts["errors"] += 1
                counts["quarantined"] += 1
                error_details.append(
                    {
                        "stage": "prediction",
                        "prediction_id": str(record.get("prediction_id") or "unknown"),
                        "error": str(exc)[:500],
                    }
                )
        try:
            self._reconcile_radar(point=point, counts=counts)
        except Exception as exc:  # a stale Radar state cannot suppress source alerts
            counts["errors"] += 1
            error_details.append({"stage": "radar", "error": str(exc)[:500]})
        try:
            self._reconcile_operational(run_id=run_id, point=point, counts=counts)
        except Exception as exc:  # operational evidence is best-effort and isolated
            counts["errors"] += 1
            error_details.append({"stage": "operational", "error": str(exc)[:500]})
        counts["created"] = sum(
            counts[key]
            for key in (
                "prediction_created",
                "outcome_created",
                "radar_created",
                "news_event_created",
                "operational_created",
            )
        )
        counts["pruned"] = self.store.prune_alerts(keep=self.retention_limit)
        result = {
            "version": ALERT_POLICY_VERSION,
            "status": "COMPLETED_WITH_ERRORS" if counts["errors"] else "COMPLETED",
            "as_of": _iso(point),
            "run_id": run_id,
            "counts": counts,
            "policy": alert_policy(),
            "capabilities": alert_capabilities(),
            "retention": {"limit": self.retention_limit, "pruned": counts["pruned"]},
            "errors": error_details,
        }
        self.store.set_scheduler_state("last_alert_reconciliation", result, updated_at=_iso(point))
        return result


__all__ = [
    "ALERT_OPERATIONAL_COOLDOWN_SECONDS",
    "ALERT_PAGE_LIMIT",
    "ALERT_POLICY_VERSION",
    "ALERT_RECORD_BATCH_LIMIT",
    "ALERT_RETENTION_LIMIT",
    "ALERT_SEVERITIES",
    "ALERT_SOURCES",
    "ALERT_STATUSES",
    "AlertReconciler",
    "alert_capabilities",
    "alert_policy",
]
