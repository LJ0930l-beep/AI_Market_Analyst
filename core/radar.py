"""Deterministic, read-only Opportunity Score and Market Radar primitives."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isclose, isfinite
from typing import Any, Iterable, Mapping

from .instruments import Instrument


OPPORTUNITY_SCORING_VERSION = "opportunity_v1"
CALIBRATION_MIN_SAMPLE = 100
FRESHNESS_MAX_AGE_HOURS = 24
RADAR_CATEGORIES = frozenset({
    "STRONG_OPPORTUNITY",
    "WATCH",
    "AVOID",
    "WAIT",
    "NOT_RANKED",
})


@dataclass(frozen=True, slots=True)
class OpportunityScoreConfig:
    """Versioned, explicit scoring configuration owned by Python."""

    version: str = OPPORTUNITY_SCORING_VERSION
    weights: tuple[tuple[str, float], ...] = (
        ("calibrated_confidence", 0.30),
        ("risk_reward", 0.20),
        ("freshness", 0.15),
        ("regime_alignment", 0.15),
        ("news_event_risk", 0.10),
        ("data_quality", 0.10),
    )
    strong_threshold: float = 0.70
    watch_threshold: float = 0.45
    freshness_max_age_hours: int = FRESHNESS_MAX_AGE_HOURS
    calibration_min_sample: int = CALIBRATION_MIN_SAMPLE

    def __post_init__(self) -> None:
        expected_names = {
            "calibrated_confidence",
            "risk_reward",
            "freshness",
            "regime_alignment",
            "news_event_risk",
            "data_quality",
        }
        names = tuple(name for name, _ in self.weights)
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("opportunity score version is required")
        if set(names) != expected_names or len(names) != len(set(names)):
            raise ValueError("opportunity score weights must name each required component exactly once")
        try:
            numeric_weights = tuple(float(weight) for _, weight in self.weights)
        except (TypeError, ValueError) as exc:
            raise ValueError("opportunity score weights must be finite numbers") from exc
        if any(not isfinite(weight) or weight < 0 for weight in numeric_weights):
            raise ValueError("opportunity score weights must be finite and non-negative")
        if not isclose(sum(numeric_weights), 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("opportunity score weights must sum to 1")
        if not 0.0 <= self.watch_threshold < self.strong_threshold <= 1.0:
            raise ValueError("opportunity score thresholds must satisfy 0 <= watch < strong <= 1")
        if self.freshness_max_age_hours <= 0 or self.calibration_min_sample <= 0:
            raise ValueError("opportunity score freshness and calibration limits must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "weights": dict(self.weights),
            "thresholds": {
                "strong_opportunity": self.strong_threshold,
                "watch": self.watch_threshold,
            },
            "normalization": {
                "calibrated_confidence": "value in [0,1]",
                "risk_reward": "clamp((rr - 1.5) / 1.5, 0, 1)",
                "freshness": "clamp(1 - age_hours / freshness_max_age_hours, 0, 1)",
                "regime_alignment": "LONG bull_trend/SHORT bear_trend=1; range=0.5",
                "news_event_risk": "no evidence=unavailable (no rank); explicit no-event/clear news=1; low-impact=0.5; high-impact=0",
                "data_quality": "verified provider context=1; technical-only=0.5 (degraded); unavailable=unavailable (no rank)",
            },
            "freshness_max_age_hours": self.freshness_max_age_hours,
            "calibration_min_sample": self.calibration_min_sample,
        }


OPPORTUNITY_CONFIG = OpportunityScoreConfig()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prediction(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("prediction", record)
    return value if isinstance(value, dict) else {}


def _outcome(record: Mapping[str, Any]) -> dict[str, Any] | None:
    value = record.get("outcome")
    return value if isinstance(value, dict) else None


def _context(prediction: Mapping[str, Any]) -> dict[str, Any]:
    value = prediction.get("context_json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _component(
    name: str,
    *,
    input_value: Any,
    score: float | None,
    weight: float,
    status: str,
    reason: str | None = None,
    provenance: str,
) -> dict[str, object]:
    normalized = _rounded(score)
    return {
        "name": name,
        "input": input_value,
        "score": normalized,
        "weight": weight,
        "contribution": _rounded(normalized * weight) if normalized is not None else None,
        "status": status,
        "available": status == "available",
        "reason": reason,
        "provenance": provenance,
    }


def _calibrated_confidence(
    prediction: Mapping[str, Any],
    calibration: Mapping[str, Any] | None,
    config: OpportunityScoreConfig,
) -> tuple[float | None, str, str | None, str]:
    raw = _number(prediction.get("raw_confidence"))
    if calibration is None:
        return None, "unavailable", "calibration_not_eligible", "no calibration result"
    scope = calibration.get("scope") if isinstance(calibration.get("scope"), dict) else {}
    prediction_source = str(prediction.get("source_type") or "live").lower()
    scoped_source = str(scope.get("source_type") or "").lower()
    if scoped_source and scoped_source != prediction_source:
        return None, "unavailable", "calibration_not_eligible", "calibration scope does not match prediction source"
    scoped_model = scope.get("model_id")
    prediction_model = prediction.get("model_id")
    if scoped_model and prediction_model != scoped_model:
        return None, "unavailable", "calibration_not_eligible", "calibration scope does not match prediction model"
    status = str(calibration.get("status", "")).upper()
    sample_count = _number(calibration.get("sample_count"))
    params = calibration.get("params") if isinstance(calibration.get("params"), dict) else {}
    minimum = _number(params.get("min_sample")) or float(config.calibration_min_sample)
    if status != "ACTIVE" or sample_count is None or sample_count < minimum:
        return None, "unavailable", "calibration_not_eligible", "calibration is not ACTIVE at the required sample threshold"
    if raw is None:
        return None, "unavailable", "raw_confidence_missing", "prediction has no finite raw confidence"

    version = calibration.get("version")
    stored = _number(prediction.get("calibrated_confidence"))
    if stored is not None and prediction.get("calibration_version") == version:
        return stored, "available", None, "stored prediction calibration"

    bucket_label = _confidence_bucket(raw)
    buckets = calibration.get("buckets") if isinstance(calibration.get("buckets"), list) else []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        lower = _number(bucket.get("lower"))
        upper = _number(bucket.get("upper"))
        count = _number(bucket.get("n"))
        shrunk = _number(bucket.get("shrunk_rate"))
        if lower is None or upper is None or bucket_label != f"{lower:.2f}-{upper:.2f}":
            continue
        if count is not None and count > 0 and shrunk is not None:
            return shrunk, "available", None, "latest calibration bucket"
        return None, "unavailable", "calibration_bucket_unavailable", "active calibration has no evidence for this confidence bucket"
    return None, "unavailable", "calibration_bucket_unavailable", "active calibration bucket was not returned"


def _confidence_bucket(value: float) -> str:
    if value < 0.50:
        return "<0.50"
    for lower, upper in ((0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.00)):
        if value >= lower and (value < upper or (upper == 1.0 and value <= upper)):
            return f"{lower:.2f}-{upper:.2f}"
    return ">1.00"


def _risk_reward(prediction: Mapping[str, Any], action: str) -> tuple[float | None, str, str | None, str]:
    entry_low = _number(prediction.get("entry_low"))
    entry_high = _number(prediction.get("entry_high"))
    stop = _number(prediction.get("stop"))
    target = _number(prediction.get("tp1"))
    if any(value is None for value in (entry_low, entry_high, stop, target)):
        return None, "unavailable", "risk_reward_missing", "validated entry/stop/tp1 fields are required"
    assert entry_low is not None and entry_high is not None and stop is not None and target is not None
    entry = (entry_low + entry_high) / 2.0
    if action == "LONG":
        risk = entry - stop
        reward = target - entry
    else:
        risk = stop - entry
        reward = entry - target
    if risk <= 0 or reward < 0:
        return None, "degraded", "risk_reward_invalid", "stored risk levels do not form a positive risk/reward relationship"
    ratio = reward / risk
    return ratio, "available", None, "stored SignalProposal levels"


def _freshness(
    prediction: Mapping[str, Any],
    context: Mapping[str, Any],
    now: datetime,
    config: OpportunityScoreConfig,
) -> tuple[float | None, str, str | None, str, datetime | None, float | None]:
    data_as_of = _timestamp(prediction.get("data_as_of"))
    if data_as_of is None:
        provider = context.get("provider_snapshot") if isinstance(context.get("provider_snapshot"), dict) else {}
        data_as_of = _timestamp(provider.get("data_as_of"))
    if data_as_of is None:
        return None, "unavailable", "freshness_missing", "prediction/provider data_as_of is missing", None, None
    age_seconds = (now - data_as_of).total_seconds()
    if age_seconds < 0:
        return None, "degraded", "freshness_future_timestamp", "data_as_of is in the future", data_as_of, age_seconds
    age_hours = age_seconds / 3600.0
    if age_hours > config.freshness_max_age_hours:
        return 0.0, "degraded", "stale_data", "data_as_of exceeds the freshness policy", data_as_of, age_seconds
    return _clamp(1.0 - age_hours / config.freshness_max_age_hours), "available", None, "prediction/provider data_as_of", data_as_of, age_seconds


def _regime_alignment(context: Mapping[str, Any], action: str) -> tuple[float | None, str, str | None, str, str | None]:
    quant = context.get("quant") if isinstance(context.get("quant"), dict) else {}
    regime = quant.get("market_regime")
    if not isinstance(regime, str) or not regime:
        return None, "unavailable", "regime_unavailable", "quant market_regime is not present", None
    normalized = regime.lower()
    if normalized == "range":
        return 0.5, "available", None, "stored quant market_regime", normalized
    if (action == "LONG" and normalized == "bull_trend") or (action == "SHORT" and normalized == "bear_trend"):
        return 1.0, "available", None, "stored quant market_regime", normalized
    return 0.0, "available", None, "stored quant market_regime does not align with action", normalized


def _news_event_risk(context: Mapping[str, Any]) -> tuple[float | None, str, str | None, str, str | None]:
    time_policy = context.get("time_policy") if isinstance(context.get("time_policy"), dict) else {}
    explicit_event_risk = time_policy.get("event_risk")
    risk_events = context.get("risk_events")
    risk_importance = [
        _number(event.get("importance"))
        for event in risk_events
        if isinstance(event, dict) and _number(event.get("importance")) is not None
    ] if isinstance(risk_events, list) else []
    if explicit_event_risk is True:
        return 0.0, "available", "stored time_policy.event_risk is explicitly true", "context.time_policy.event_risk", "high-impact"
    if any(value >= 70 for value in risk_importance):
        return 0.0, "available", "stored risk_events include high-impact evidence", "context.risk_events", "high-impact"

    market_context = context.get("market_context") if isinstance(context.get("market_context"), dict) else {}
    news = context.get("news")
    provenance_prefix = "context.time_policy.event_risk=false; " if explicit_event_risk is False else ""
    if market_context.get("news_available") is False:
        return 0.0, "degraded", "news_unavailable", f"{provenance_prefix}context.market_context.news_available=false", "unavailable"
    if not isinstance(news, list):
        return None, "unavailable", "news_risk_unknown", f"{provenance_prefix}context.news missing", None
    importance = [
        _number(event.get("importance"))
        for event in news
        if isinstance(event, dict) and _number(event.get("importance")) is not None
    ]
    if any(value is not None and value >= 70 for value in importance):
        return 0.0, "available", "stored news events include high-impact evidence", f"{provenance_prefix}context.news", "high-impact"
    if news:
        return 0.5, "available", "stored news events include no high-impact evidence", f"{provenance_prefix}context.news", "low-impact"
    if market_context.get("news_available") is True:
        return 1.0, "available", "stored context reports no current news events", f"{provenance_prefix}context.market_context.news_available=true", "none"
    return None, "unavailable", "news_risk_unknown", f"{provenance_prefix}news availability is not explicit", None


def _data_quality(context: Mapping[str, Any], prediction: Mapping[str, Any]) -> tuple[float | None, str, str | None, str, str | None]:
    provider = context.get("provider_snapshot") if isinstance(context.get("provider_snapshot"), dict) else {}
    capabilities = (
        context.get("market_context", {}).get("context_capabilities", {})
        if isinstance(context.get("market_context"), dict)
        else {}
    )
    parse_status = str(prediction.get("parse_status", "")).lower()
    if any(token in parse_status for token in ("failed", "error", "unavailable")):
        return 0.0, "degraded", "prediction_invalid", "prediction parse status is not valid", "invalid"
    if not provider:
        return None, "unavailable", "data_quality_unknown", "provider snapshot is not present in saved context", None
    if provider.get("error_code"):
        return 0.0, "degraded", "provider_error", "provider snapshot contains an error", "provider_error"
    if provider.get("stale") is True:
        return 0.0, "degraded", "provider_stale", "provider snapshot is marked stale", "stale"
    if isinstance(capabilities, dict) and capabilities.get("technical_only") is True:
        return 0.5, "degraded", "technical_only", "context is explicitly technical-only", "technical_only"
    if provider.get("provider") and provider.get("data_as_of"):
        return 1.0, "available", None, "stored provider snapshot", "verified"
    return None, "unavailable", "data_quality_unknown", "provider snapshot lacks usable provenance", None


def _instrument_payload(instrument: Instrument) -> dict[str, object]:
    return {
        "symbol": instrument.symbol,
        "asset_type": instrument.asset_type.value,
        "exchange": instrument.exchange,
        "currency": instrument.currency,
        "quote_currency": instrument.quote_currency,
        "timezone": instrument.timezone,
        "trading_hours": instrument.trading_hours.value,
        "sector": instrument.sector,
    }


def _empty_component_map(config: OpportunityScoreConfig) -> dict[str, dict[str, object]]:
    return {
        name: _component(
            name,
            input_value=None,
            score=None,
            weight=weight,
            status="unavailable",
            reason="no prediction evidence",
            provenance="no_prediction",
        )
        for name, weight in config.weights
    }


def _entry(
    watchlist_entry: Mapping[str, Any],
    instrument: Instrument,
    prediction_record: Mapping[str, Any] | None,
    calibration: Mapping[str, Any] | None,
    now: datetime,
    config: OpportunityScoreConfig,
) -> dict[str, object]:
    symbol = instrument.symbol
    base: dict[str, object] = {
        "symbol": symbol,
        "instrument": _instrument_payload(instrument),
        "watchlist": {
            "added_at": watchlist_entry.get("added_at"),
            "updated_at": watchlist_entry.get("updated_at"),
        },
        "prediction_id": None,
        "action": None,
        "category": "NOT_RANKED",
        "status": "awaiting_analysis",
        "ranking_eligible": False,
        "score": None,
        "rank": None,
        "generated_at": None,
        "data_as_of": None,
        "signal_valid_until": None,
        "freshness": {
            "status": "unavailable",
            "data_as_of": None,
            "age_seconds": None,
            "max_age_hours": config.freshness_max_age_hours,
            "reason": "no prediction evidence",
        },
        "inputs": {},
        "components": _empty_component_map(config),
        "missing_reasons": ["awaiting_analysis"],
        "degraded_reasons": [],
        "outcome": None,
        "provenance": {
            "sources": ["durable_watchlist"],
            "read_only": True,
            "model_invoked": False,
            "writes": False,
        },
    }
    if prediction_record is None:
        return base

    prediction = _prediction(prediction_record)
    context = _context(prediction)
    action = str(prediction.get("action", "")).upper()
    parse_status = str(prediction.get("parse_status", "")).lower()
    prediction_id = prediction.get("prediction_id")
    raw = _number(prediction.get("raw_confidence"))
    calibrated, calibration_status, calibration_reason, calibration_provenance = _calibrated_confidence(prediction, calibration, config)
    rr, rr_status, rr_reason, rr_provenance = _risk_reward(prediction, action) if action in {"LONG", "SHORT"} else (None, "unavailable", "wait_not_rankable", "WAIT has no risk/reward",)
    freshness_score, freshness_status, freshness_reason, freshness_provenance, data_as_of, age_seconds = _freshness(prediction, context, now, config)
    regime_score, regime_status, regime_reason, regime_provenance, regime = _regime_alignment(context, action) if action in {"LONG", "SHORT"} else (None, "unavailable", "wait_not_rankable", "WAIT does not receive an opportunity score", None)
    news_score, news_status, news_reason, news_provenance, news_state = _news_event_risk(context) if action in {"LONG", "SHORT"} else (None, "unavailable", "wait_not_rankable", "WAIT does not receive an opportunity score", None)
    quality_score, quality_status, quality_reason, quality_provenance, quality_state = _data_quality(context, prediction) if action in {"LONG", "SHORT"} else (None, "unavailable", "wait_not_rankable", "WAIT does not receive an opportunity score", None)

    components = {
        "calibrated_confidence": _component(
            "calibrated_confidence", input_value=calibrated, score=calibrated, weight=dict(config.weights)["calibrated_confidence"],
            status=calibration_status, reason=calibration_reason, provenance=calibration_provenance,
        ),
        "risk_reward": _component(
            "risk_reward", input_value=rr, score=_clamp((rr - 1.5) / 1.5) if rr is not None else None,
            weight=dict(config.weights)["risk_reward"], status=rr_status, reason=rr_reason, provenance=rr_provenance,
        ),
        "freshness": _component(
            "freshness", input_value=age_seconds, score=freshness_score, weight=dict(config.weights)["freshness"],
            status=freshness_status, reason=freshness_reason, provenance=freshness_provenance,
        ),
        "regime_alignment": _component(
            "regime_alignment", input_value=regime, score=regime_score, weight=dict(config.weights)["regime_alignment"],
            status=regime_status, reason=regime_reason, provenance=regime_provenance,
        ),
        "news_event_risk": _component(
            "news_event_risk", input_value=news_state, score=news_score, weight=dict(config.weights)["news_event_risk"],
            status=news_status, reason=news_reason, provenance=news_provenance,
        ),
        "data_quality": _component(
            "data_quality", input_value=quality_state, score=quality_score, weight=dict(config.weights)["data_quality"],
            status=quality_status, reason=quality_reason, provenance=quality_provenance,
        ),
    }
    missing_reasons = [str(component["reason"]) for component in components.values() if component["status"] == "unavailable" and component["reason"]]
    degraded_reasons = [str(component["reason"]) for component in components.values() if component["status"] == "degraded" and component["reason"]]
    validity = _timestamp(prediction.get("signal_valid_until"))
    if validity is None:
        generated = _timestamp(prediction.get("generated_at"))
        validity_minutes = _number(prediction.get("signal_validity_minutes"))
        if generated is not None and validity_minutes is not None:
            validity = generated + timedelta(minutes=validity_minutes)
    invalid = action not in {"LONG", "SHORT", "WAIT"} or parse_status in {"model_unavailable", "model_not_configured", "parse_error", "repair_failed", "output_too_long"} or any(token in parse_status for token in ("failed", "unavailable", "error"))
    expired = validity is not None and now >= validity
    eligible = not invalid and action in {"LONG", "SHORT"} and validity is not None and not expired and all(component["status"] == "available" for component in components.values())
    score = None
    category = "NOT_RANKED"
    status = "degraded"
    if invalid:
        category = "AVOID"
        status = "invalid_prediction"
        degraded_reasons.append("prediction_invalid")
    elif action == "WAIT":
        category = "WAIT"
        status = "wait"
        missing_reasons.append("wait_not_rankable")
    elif validity is None:
        category = "AVOID"
        status = "not_ranked"
        missing_reasons.append("signal_validity_missing")
    elif expired:
        category = "AVOID"
        status = "expired"
        degraded_reasons.append("signal_expired")
    elif eligible:
        score = _rounded(sum(float(component["contribution"]) for component in components.values() if component["contribution"] is not None))
        if score is not None and score >= config.strong_threshold:
            category = "STRONG_OPPORTUNITY"
        elif score is not None and score >= config.watch_threshold:
            category = "WATCH"
        else:
            category = "AVOID"
        status = "ranked"
    else:
        if calibration_reason == "calibration_not_eligible":
            status = "calibration_not_eligible"
            missing_reasons.append("calibration_not_eligible")
        if not missing_reasons and not degraded_reasons:
            missing_reasons.append("not_ranked")

    outcome = _outcome(prediction_record)
    safe_outcome = None
    if outcome is not None:
        safe_outcome = {
            "status": outcome.get("status") or prediction_record.get("outcome_status"),
            "settled_at": outcome.get("settled_at"),
            "realized_r": _number(outcome.get("realized_r")),
        }
    base.update(
        {
            "prediction_id": prediction_id,
            "action": action or None,
            "category": category,
            "status": status,
            "ranking_eligible": eligible,
            "score": score,
            "generated_at": prediction.get("generated_at"),
            "data_as_of": data_as_of.isoformat() if data_as_of else None,
            "signal_valid_until": validity.isoformat() if validity else None,
            "freshness": {
                "status": freshness_status,
                "data_as_of": data_as_of.isoformat() if data_as_of else None,
                "age_seconds": _rounded(age_seconds),
                "max_age_hours": config.freshness_max_age_hours,
                "reason": freshness_reason,
            },
            "inputs": {
                "raw_confidence": raw,
                "calibrated_confidence": calibrated,
                "risk_reward": rr,
                "freshness": {
                    "age_seconds": _rounded(age_seconds),
                    "score": _rounded(freshness_score),
                },
                "regime": regime,
                "news_event_risk": news_state,
                "data_quality": quality_state,
            },
            "components": components,
            "missing_reasons": sorted(set(missing_reasons)),
            "degraded_reasons": sorted(set(degraded_reasons)),
            "outcome": safe_outcome,
            "provenance": {
                "sources": ["durable_watchlist", "latest_live_prediction", "linked_outcome", "latest_calibration", "prediction_context_json"],
                "prediction_source_type": prediction.get("source_type", "live"),
                "calibration_version": calibration.get("version") if calibration else None,
                "calibration_status": calibration.get("status") if calibration else None,
                "calibration_scope": calibration.get("scope") if calibration else None,
                "read_only": True,
                "model_invoked": False,
                "writes": False,
            },
        }
    )
    return base


def build_radar(
    watchlist_entries: Iterable[Mapping[str, Any]],
    *,
    instruments: Mapping[str, Instrument],
    predictions: Mapping[str, Mapping[str, Any]],
    calibration: Mapping[str, Any] | None,
    asset_type: str | None = None,
    category: str | None = None,
    now: datetime | None = None,
    config: OpportunityScoreConfig = OPPORTUNITY_CONFIG,
) -> dict[str, object]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entries: list[dict[str, object]] = []
    for watchlist_entry in watchlist_entries:
        symbol = str(watchlist_entry.get("symbol", "")).upper()
        instrument = instruments.get(symbol)
        if instrument is None:
            continue
        if asset_type and instrument.asset_type.value != asset_type:
            continue
        record = predictions.get(symbol)
        built = _entry(watchlist_entry, instrument, record, calibration, current, config)
        if category and built["category"] != category:
            continue
        entries.append(built)

    category_order = {"STRONG_OPPORTUNITY": 0, "WATCH": 1, "AVOID": 2, "WAIT": 3, "NOT_RANKED": 4}
    entries.sort(
        key=lambda item: (
            0 if item["ranking_eligible"] else 1,
            -(float(item["score"]) if item["ranking_eligible"] and item["score"] is not None else 0.0),
            category_order.get(str(item["category"]), 9),
            str(item["symbol"]),
        )
    )
    rank = 0
    for item in entries:
        if item["ranking_eligible"]:
            rank += 1
            item["rank"] = rank

    eligible_count = sum(1 for item in entries if item["ranking_eligible"])
    if not entries:
        status = "empty"
    elif eligible_count == 0:
        status = "degraded"
    else:
        status = "ready"
    reasons = sorted({reason for item in entries for reason in [*item["missing_reasons"], *item["degraded_reasons"]]})
    return {
        "scoring_version": config.version,
        "config": config.to_dict(),
        "status": status,
        "as_of": current.isoformat(),
        "filters": {"asset_type": asset_type, "category": category},
        "counts": {
            "total": len(entries),
            "ranking_eligible": eligible_count,
            "awaiting_analysis": sum(1 for item in entries if item["status"] == "awaiting_analysis"),
            "wait": sum(1 for item in entries if item["category"] == "WAIT"),
            "strong_opportunity": sum(1 for item in entries if item["category"] == "STRONG_OPPORTUNITY"),
            "watch": sum(1 for item in entries if item["category"] == "WATCH"),
            "avoid": sum(1 for item in entries if item["category"] == "AVOID"),
        },
        "provenance": {
            "sources": ["durable_watchlist", "latest_live_prediction", "linked_outcome", "latest_calibration", "prediction_context_json"],
            "calibration_version": calibration.get("version") if calibration else None,
            "calibration_status": calibration.get("status") if calibration else None,
            "calibration_scope": calibration.get("scope") if calibration else None,
            "read_only": True,
            "model_invoked": False,
            "writes": False,
            "reasons": reasons,
        },
        "entries": entries,
    }


__all__ = [
    "CALIBRATION_MIN_SAMPLE",
    "FRESHNESS_MAX_AGE_HOURS",
    "OPPORTUNITY_CONFIG",
    "OPPORTUNITY_SCORING_VERSION",
    "OpportunityScoreConfig",
    "RADAR_CATEGORIES",
    "build_radar",
]
