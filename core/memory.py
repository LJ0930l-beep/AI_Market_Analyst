"""Leakage-safe deterministic Market Memory over durable local records."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


MEMORY_VERSION = "market_memory_v1"
MEMORY_FEATURE_VERSION = "feature_representation_v1"
MEMORY_RETENTION_LIMIT = 1_000
MEMORY_MIN_RESOLVED_SAMPLES = 3


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _context(prediction: dict[str, Any]) -> dict[str, Any]:
    raw = prediction.get("context_json")
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _instrument_payload(prediction: dict[str, Any]) -> dict[str, Any]:
    value = prediction.get("instrument")
    return value if isinstance(value, dict) else {}


def feature_from_prediction(prediction: dict[str, Any]) -> dict[str, object]:
    """Extract a fixed, auditable feature representation from stored context."""

    context = _context(prediction)
    quant = context.get("quant") if isinstance(context.get("quant"), dict) else {}
    benchmark = context.get("market_context") if isinstance(context.get("market_context"), dict) else {}
    benchmark_context = benchmark.get("benchmark_context") if isinstance(benchmark.get("benchmark_context"), dict) else {}
    event_context = benchmark.get("event_intelligence") if isinstance(benchmark.get("event_intelligence"), dict) else {}
    clusters = event_context.get("clusters") if isinstance(event_context.get("clusters"), list) else []
    high_impact = any(isinstance(cluster, dict) and int(cluster.get("importance", 0) or 0) >= 70 for cluster in clusters)
    risk_events = context.get("risk_events") if isinstance(context.get("risk_events"), list) else []
    high_impact = high_impact or any(isinstance(event, dict) and int(event.get("importance", 0) or 0) >= 70 for event in risk_events)
    return {
        "representation_version": MEMORY_FEATURE_VERSION,
        "symbol": str(prediction.get("symbol") or _instrument_payload(prediction).get("symbol", "")).upper(),
        "asset_type": _instrument_payload(prediction).get("asset_type"),
        "timeframe": prediction.get("analysis_timeframe"),
        "action": str(prediction.get("action", "")).upper(),
        "regime": quant.get("market_regime", "unknown"),
        "trend_score": _number(quant.get("trend_score")),
        "momentum_score": _number(quant.get("momentum_score")),
        "volume_ratio": _number(quant.get("volume_ratio")),
        "relative_strength": _number(benchmark_context.get("relative_strength")),
        "event_risk": high_impact,
        "event_cluster_count": len(clusters),
    }


def _feature_is_eligible(feature: dict[str, object]) -> bool:
    """Reject records whose stored context cannot support an auditable match."""

    if not feature.get("symbol") or not feature.get("timeframe") or not feature.get("action"):
        return False
    return feature.get("regime") != "unknown" or any(
        _number(feature.get(key)) is not None
        for key in ("trend_score", "momentum_score", "volume_ratio", "relative_strength")
    )


def _context_as_ofs(prediction: dict[str, Any]) -> list[datetime]:
    """Return explicit Phase 6 evidence boundaries embedded in a prediction."""

    result: list[datetime] = []
    data_as_of = _timestamp(prediction.get("data_as_of"))
    if data_as_of is not None:
        result.append(data_as_of)
    context = _context(prediction)
    market_context = context.get("market_context") if isinstance(context.get("market_context"), dict) else {}
    for key in ("benchmark_context", "event_intelligence", "market_memory"):
        value = market_context.get(key)
        if isinstance(value, dict):
            boundary = _timestamp(value.get("as_of"))
            if boundary is not None:
                result.append(boundary)
    return result


def _distance(left: dict[str, object], right: dict[str, object]) -> float:
    distance = 0.0
    for key, scale in (("trend_score", 2.0), ("momentum_score", 2.0), ("volume_ratio", 4.0), ("relative_strength", 2.0)):
        left_value = _number(left.get(key))
        right_value = _number(right.get(key))
        if left_value is None or right_value is None:
            distance += 0.35
        else:
            distance += min(1.0, abs(left_value - right_value) / scale)
    if left.get("regime") != right.get("regime"):
        distance += 0.25
    if left.get("action") != right.get("action"):
        distance += 0.20
    if bool(left.get("event_risk")) != bool(right.get("event_risk")):
        distance += 0.15
    return round(distance, 8)


@dataclass(frozen=True, slots=True)
class MemoryMatch:
    prediction_id: str
    symbol: str
    generated_at: str
    distance: float
    outcome_status: str | None
    realized_r: float | None
    outcome_known_as_of: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "symbol": self.symbol,
            "generated_at": self.generated_at,
            "distance": self.distance,
            "outcome_status": self.outcome_status,
            "realized_r": self.realized_r,
            "outcome_known_as_of": self.outcome_known_as_of,
        }


@dataclass(frozen=True, slots=True)
class MarketMemoryContext:
    version: str
    feature_version: str
    status: str
    query_symbol: str
    query_timeframe: str
    as_of: str
    eligible_sample_count: int
    resolved_sample_count: int
    similar_count: int
    win_rate: float | None
    avg_r: float | None
    typical_outcomes: tuple[str, ...]
    matches: tuple[MemoryMatch, ...]
    capability: dict[str, object]
    provenance: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "feature_version": self.feature_version,
            "status": self.status,
            "query_symbol": self.query_symbol,
            "query_timeframe": self.query_timeframe,
            "as_of": self.as_of,
            "eligible_sample_count": self.eligible_sample_count,
            "resolved_sample_count": self.resolved_sample_count,
            "similar_count": self.similar_count,
            "win_rate": self.win_rate,
            "avg_r": self.avg_r,
            "typical_outcomes": list(self.typical_outcomes),
            "matches": [match.to_dict() for match in self.matches],
            "capability": dict(self.capability),
            "provenance": dict(self.provenance),
        }


class MarketMemoryService:
    def __init__(self, store: object | None, *, min_resolved_samples: int = MEMORY_MIN_RESOLVED_SAMPLES) -> None:
        self.store = store
        self.min_resolved_samples = max(1, int(min_resolved_samples))

    def query(
        self,
        *,
        symbol: str,
        timeframe: str,
        as_of: datetime,
        query_prediction: dict[str, Any] | None = None,
        exclude_prediction_id: str | None = None,
        top_k: int = 5,
    ) -> MarketMemoryContext:
        cutoff = as_of.astimezone(timezone.utc) if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        query_feature = feature_from_prediction(query_prediction or {
            "symbol": symbol,
            "instrument": {"symbol": symbol},
            "analysis_timeframe": timeframe,
            "action": "WAIT",
        })
        records = []
        if self.store is not None and hasattr(self.store, "list_memory_source_records"):
            records = self.store.list_memory_source_records(  # type: ignore[attr-defined]
                as_of=cutoff.isoformat(),
                timeframe=timeframe,
                limit=10_000,
            )
        eligible: list[tuple[float, dict[str, Any], dict[str, object], dict[str, Any] | None]] = []
        for record in records:
            prediction = record.get("prediction")
            if not isinstance(prediction, dict):
                continue
            prediction_id = str(prediction.get("prediction_id", ""))
            if exclude_prediction_id and prediction_id == exclude_prediction_id:
                continue
            generated_at = _timestamp(prediction.get("generated_at"))
            if generated_at is None or generated_at >= cutoff:
                continue
            if any(boundary > cutoff for boundary in _context_as_ofs(prediction)):
                continue
            feature = feature_from_prediction(prediction)
            if not _feature_is_eligible(feature):
                continue
            outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else None
            # The store deliberately strips outcomes not known at the cutoff;
            # keep the check here too so callers cannot bypass the boundary.
            if outcome is not None:
                settled_at = _timestamp(outcome.get("settled_at"))
                if settled_at is None or settled_at > cutoff:
                    outcome = None
            eligible.append((_distance(query_feature, feature), record, feature, outcome))
        eligible.sort(key=lambda item: (item[0], str(item[1].get("prediction", {}).get("prediction_id", ""))))
        matches: list[MemoryMatch] = []
        for distance, record, _feature, outcome in eligible[: max(1, min(int(top_k), 20))]:
            prediction = record["prediction"]
            assert isinstance(prediction, dict)
            matches.append(
                MemoryMatch(
                    prediction_id=str(prediction.get("prediction_id", "")),
                    symbol=str(prediction.get("symbol") or _instrument_payload(prediction).get("symbol", "")).upper(),
                    generated_at=str(prediction.get("generated_at")),
                    distance=distance,
                    outcome_status=str(outcome.get("status")) if outcome else None,
                    realized_r=_number(outcome.get("realized_r")) if outcome else None,
                    outcome_known_as_of=str(outcome.get("settled_at")) if outcome else None,
                )
            )
        # Eligibility and sample sufficiency are calculated over every
        # point-in-time eligible record, while the returned matches remain
        # bounded to ``top_k`` for the UI/context payload.
        resolved_records = [item for item in eligible if isinstance(item[3], dict)]
        values: list[float] = []
        typical_outcomes: set[str] = set()
        for _distance_value, _record, _feature, outcome in resolved_records:
            assert isinstance(outcome, dict)
            realized_r = _number(outcome.get("realized_r"))
            if realized_r is not None:
                values.append(realized_r)
            if isinstance(outcome.get("status"), str):
                typical_outcomes.add(str(outcome["status"]))
        wins = sum(1 for value in values if value > 0)
        status = "ready" if len(values) >= self.min_resolved_samples else "preliminary"
        reason = None if status == "ready" else "insufficient_point_in_time_resolved_samples"
        capability: dict[str, object] = {
            "eligible": True,
            "min_resolved_samples": self.min_resolved_samples,
            "future_evidence_excluded": True,
            "self_match_excluded": True,
            "outcomes_after_as_of_excluded": True,
            "incomplete_features_excluded": True,
            "prediction_data_after_as_of_excluded": True,
        }
        if reason:
            capability["reason"] = reason
        return MarketMemoryContext(
            version=MEMORY_VERSION,
            feature_version=MEMORY_FEATURE_VERSION,
            status=status,
            query_symbol=symbol.upper(),
            query_timeframe=timeframe,
            as_of=cutoff.isoformat(),
            eligible_sample_count=len(eligible),
            resolved_sample_count=len(values),
            similar_count=len(matches),
            win_rate=(wins / len(values)) if values else None,
            avg_r=(sum(values) / len(values)) if values else None,
            typical_outcomes=tuple(sorted(typical_outcomes)),
            matches=tuple(matches),
            capability=capability,
            provenance={
                "computed_by": "python_deterministic",
                "ranking": "fixed_feature_distance_v1",
                "as_of_boundary": cutoff.isoformat(),
                "read_only": True,
            },
        )

    def materialize(self, *, as_of: datetime, source_type: str = "live", limit: int = 500) -> dict[str, object]:
        """Explicitly persist point-in-time feature evidence; GET never calls this."""

        if self.store is None or not hasattr(self.store, "list_memory_source_records"):
            return {"version": MEMORY_VERSION, "status": "unavailable", "count": 0, "reason": "store_not_configured"}
        cutoff = as_of.astimezone(timezone.utc) if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        records = self.store.list_memory_source_records(as_of=cutoff.isoformat(), source_type=source_type, limit=max(1, min(int(limit), 1000)))  # type: ignore[attr-defined]
        saved = 0
        for record in records:
            prediction = record.get("prediction")
            if not isinstance(prediction, dict):
                continue
            generated_at = _timestamp(prediction.get("generated_at"))
            if generated_at is None or generated_at >= cutoff:
                continue
            if any(boundary > cutoff for boundary in _context_as_ofs(prediction)):
                continue
            features = feature_from_prediction(prediction)
            if not _feature_is_eligible(features):
                continue
            feature = {
                "feature_id": f"{MEMORY_VERSION}:{prediction.get('prediction_id')}:{cutoff.isoformat()}",
                "prediction_id": prediction.get("prediction_id"),
                "source_type": source_type,
                "feature_as_of": cutoff.isoformat(),
                "representation_version": MEMORY_FEATURE_VERSION,
                "features": features,
                "outcome": record.get("outcome") if isinstance(record.get("outcome"), dict) else None,
            }
            self.store.save_memory_feature(feature)  # type: ignore[attr-defined]
            saved += 1
        pruned = self.store.prune_memory_features(keep=MEMORY_RETENTION_LIMIT)  # type: ignore[attr-defined]
        return {
            "version": MEMORY_VERSION,
            "status": "materialized",
            "count": saved,
            "pruned": pruned,
            "as_of": cutoff.isoformat(),
            "source_type": source_type,
            "future_evidence_excluded": True,
        }


def memory_capabilities() -> dict[str, object]:
    return {
        "version": MEMORY_VERSION,
        "feature_version": MEMORY_FEATURE_VERSION,
        "ranking": "deterministic_fixed_feature_distance_v1",
        "minimum_resolved_samples": MEMORY_MIN_RESOLVED_SAMPLES,
        "retention_limit": MEMORY_RETENTION_LIMIT,
        "vector_database_required": False,
        "llm_calculates": False,
        "mutates_prediction_confidence": False,
        "mutates_outcomes": False,
        "mutates_paper_trades": False,
        "get_read_only": True,
    }
