"""Single-source performance metrics for live and replay Prediction records."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any, Iterable


CONFIDENCE_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.80),
    (0.80, 0.90),
    (0.90, 1.00),
)
INVALID_PARSE_STATUSES = {
    "model_unavailable",
    "model_not_configured",
    "parse_error",
    "repair_failed",
    "output_too_long",
}


def _prediction(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("prediction", record)


def _outcome(record: dict[str, Any]) -> dict[str, Any] | None:
    outcome = record.get("outcome")
    return outcome if isinstance(outcome, dict) else None


def _context(prediction: dict[str, Any]) -> dict[str, Any]:
    raw = prediction.get("context_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _instrument(prediction: dict[str, Any]) -> dict[str, Any]:
    value = prediction.get("instrument")
    return value if isinstance(value, dict) else {}


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def is_valid_prediction(record: dict[str, Any]) -> bool:
    prediction = _prediction(record)
    action = str(prediction.get("action", "")).upper()
    parse_status = str(prediction.get("parse_status", "")).lower()
    if action not in {"LONG", "SHORT", "WAIT"}:
        return False
    if parse_status in INVALID_PARSE_STATUSES or any(token in parse_status for token in ("failed", "unavailable")):
        return False
    return True


def is_actionable(record: dict[str, Any]) -> bool:
    return str(_prediction(record).get("action", "")).upper() in {"LONG", "SHORT"}


def is_resolved_actionable(record: dict[str, Any]) -> bool:
    outcome = _outcome(record)
    return is_valid_prediction(record) and is_actionable(record) and _float(outcome.get("realized_r")) is not None if outcome else False


def confidence_bucket(confidence: float | int | None) -> str:
    value = _float(confidence)
    if value is None:
        return "unknown"
    if value < CONFIDENCE_BUCKETS[0][0]:
        return "<0.50"
    for low, high in CONFIDENCE_BUCKETS:
        if value >= low and (value < high or (high == 1.00 and value <= high)):
            return f"{low:.2f}-{high:.2f}"
    return ">1.00"


def _news_state(context: dict[str, Any]) -> str:
    market = context.get("market_context") if isinstance(context.get("market_context"), dict) else {}
    events = context.get("news") if isinstance(context.get("news"), list) else []
    if any(isinstance(event, dict) and int(event.get("importance", 0) or 0) >= 70 for event in events):
        return "high-impact"
    if events:
        return "low"
    if market.get("news_available") is False:
        return "unavailable"
    return "none"


def _news_sentiment(context: dict[str, Any]) -> str:
    events = context.get("news") if isinstance(context.get("news"), list) else []
    values = [_float(event.get("sentiment")) for event in events if isinstance(event, dict)]
    values = [value for value in values if value is not None]
    if not values:
        return "neutral"
    average = mean(values)
    if average > 0.15:
        return "positive"
    if average < -0.15:
        return "negative"
    return "neutral"


def _holding_bucket(prediction: dict[str, Any]) -> str:
    minutes = int(prediction.get("expected_hold_minutes") or 0)
    if minutes <= 1440:
        return "intraday"
    if minutes <= 4320:
        return "1-3d"
    return "3-10d"


def record_dimensions(record: dict[str, Any]) -> dict[str, Any]:
    prediction = _prediction(record)
    instrument = _instrument(prediction)
    context = _context(prediction)
    quant = context.get("quant") if isinstance(context.get("quant"), dict) else {}
    raw_confidence = _float(prediction.get("raw_confidence"))
    return {
        "asset_type": instrument.get("asset_type"),
        "symbol": instrument.get("symbol") or prediction.get("symbol"),
        "timeframe": prediction.get("analysis_timeframe"),
        "action": str(prediction.get("action", "")).upper(),
        "regime": quant.get("market_regime", "unknown"),
        "news_state": _news_state(context),
        "news_sentiment": _news_sentiment(context),
        "confidence_bucket": confidence_bucket(raw_confidence),
        "holding_bucket": _holding_bucket(prediction),
        "model_id": prediction.get("model_id"),
        "prompt_version": prediction.get("prompt_version"),
        "source_type": prediction.get("source_type", "live"),
        "raw_confidence": raw_confidence,
    }


def filter_records(records: Iterable[dict[str, Any]], scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    selected = [record for record in records if is_valid_prediction(record)]
    if not scope:
        return selected
    result: list[dict[str, Any]] = []
    for record in selected:
        dimensions = record_dimensions(record)
        if all(str(dimensions.get(key)) == str(value) for key, value in scope.items() if value is not None):
            result.append(record)
    return result


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _reliability(records: list[dict[str, Any]], confidence_key: str) -> tuple[float | None, list[dict[str, Any]]]:
    resolved = [record for record in records if is_resolved_actionable(record)]
    grouped: dict[str, list[tuple[float, int, float]]] = {}
    for record in resolved:
        prediction = _prediction(record)
        confidence = _float(prediction.get(confidence_key))
        if confidence is None:
            confidence = _float(prediction.get("raw_confidence")) or 0.0
        outcome = _outcome(record) or {}
        realized = _float(outcome.get("realized_r")) or 0.0
        bucket = confidence_bucket(confidence)
        grouped.setdefault(bucket, []).append((confidence, 1 if realized > 0 else 0, confidence))
    total = len(resolved)
    ece = 0.0
    rows: list[dict[str, Any]] = []
    for bucket in sorted(grouped):
        entries = grouped[bucket]
        count = len(entries)
        average_confidence = mean(item[0] for item in entries)
        empirical_rate = mean(item[1] for item in entries)
        ece += (count / total) * abs(average_confidence - empirical_rate) if total else 0.0
        rows.append(
            {
                "bucket": bucket,
                "count": count,
                "raw_avg_confidence": average_confidence,
                "empirical_win_rate": empirical_rate,
            }
        )
    return (ece if resolved else None), rows


def aggregate_performance(
    records: Iterable[dict[str, Any]],
    *,
    scope: dict[str, Any] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, Any]:
    records = list(records)
    selected = filter_records(records, scope)
    if window_start or window_end:
        filtered: list[dict[str, Any]] = []
        for record in selected:
            timestamp = _timestamp(_prediction(record).get("generated_at"))
            if timestamp is None:
                continue
            if window_start and timestamp < window_start.astimezone(timezone.utc):
                continue
            if window_end and timestamp > window_end.astimezone(timezone.utc):
                continue
            filtered.append(record)
        selected = filtered

    actionable = [record for record in selected if is_actionable(record)]
    resolved = [record for record in actionable if is_resolved_actionable(record)]
    returns = [_float(_outcome(record).get("realized_r")) for record in resolved]  # type: ignore[union-attr]
    returns = [value for value in returns if value is not None]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    flats = [value for value in returns if value == 0]
    positive_sum = sum(wins)
    negative_sum = abs(sum(losses))
    profit_factor = positive_sum / negative_sum if negative_sum > 0 else None
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    ordered_records = sorted(resolved, key=lambda record: _timestamp(_prediction(record).get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc))
    for record in ordered_records:
        cumulative += _float((_outcome(record) or {}).get("realized_r")) or 0.0
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    mfe = [_float((_outcome(record) or {}).get("mfe_r")) for record in resolved]
    mae = [_float((_outcome(record) or {}).get("mae_r")) for record in resolved]
    mfe = [value for value in mfe if value is not None]
    mae = [value for value in mae if value is not None]
    timeout_count = sum(1 for record in resolved if str((_outcome(record) or {}).get("status")) == "TIMEOUT")
    brier_values: list[float] = []
    calibrated_brier_values: list[float] = []
    for record in resolved:
        prediction = _prediction(record)
        outcome = _outcome(record) or {}
        target = 1.0 if (_float(outcome.get("realized_r")) or 0.0) > 0 else 0.0
        raw = _float(prediction.get("raw_confidence"))
        calibrated = _float(prediction.get("calibrated_confidence"))
        if raw is not None:
            brier_values.append((raw - target) ** 2)
        if calibrated is None:
            calibrated = raw
        if calibrated is not None:
            calibrated_brier_values.append((calibrated - target) ** 2)
    ece_raw, raw_buckets = _reliability(selected, "raw_confidence")
    ece_calibrated, calibrated_buckets = _reliability(selected, "calibrated_confidence")
    calibrated_by_bucket = {row["bucket"]: row for row in calibrated_buckets}
    for row in raw_buckets:
        calibrated_row = calibrated_by_bucket.get(row["bucket"])
        row["calibrated_confidence"] = calibrated_row["raw_avg_confidence"] if calibrated_row else row["raw_avg_confidence"]
    return {
        "status": "PRELIMINARY",
        "sample_count": len(selected),
        "actionable_count": len(actionable),
        "resolved_actionable": len(resolved),
        "pending_actionable": len(actionable) - len(resolved),
        "wait_count": sum(1 for record in selected if not is_actionable(record)),
        "invalid_count": sum(1 for record in records if not is_valid_prediction(record)),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": (len(wins) / len(resolved)) if resolved else None,
        "avg_r": mean(returns) if returns else None,
        "expectancy_r": mean(returns) if returns else None,
        "profit_factor": profit_factor,
        "max_drawdown_r": max_drawdown,
        "mfe_r_avg": mean(mfe) if mfe else None,
        "mfe_r_median": median(mfe) if mfe else None,
        "mfe_r_p90": _percentile(mfe, 0.90),
        "mae_r_avg": mean(mae) if mae else None,
        "mae_r_median": median(mae) if mae else None,
        "mae_r_p90": _percentile(mae, 0.90),
        "timeout_count": timeout_count,
        "timeout_rate": timeout_count / len(resolved) if resolved else None,
        "coverage": len(actionable) / len(selected) if selected else None,
        "action_rate": len(actionable) / len(selected) if selected else None,
        "wait_rate": sum(1 for record in selected if not is_actionable(record)) / len(selected) if selected else None,
        "brier_raw": mean(brier_values) if brier_values else None,
        "brier_calibrated": mean(calibrated_brier_values) if calibrated_brier_values else None,
        "ece_raw": ece_raw,
        "ece_calibrated": ece_calibrated,
        "confidence_buckets": raw_buckets,
        "scope": dict(scope or {}),
        "window_start": window_start.astimezone(timezone.utc).isoformat() if window_start else None,
        "window_end": window_end.astimezone(timezone.utc).isoformat() if window_end else None,
    }


def build_performance_snapshot(
    records: Iterable[dict[str, Any]],
    *,
    scope: dict[str, Any] | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, Any]:
    metrics = aggregate_performance(records, scope=scope, window_start=window_start, window_end=window_end)
    return {
        "scope": dict(scope or {}),
        "window_start": metrics["window_start"],
        "window_end": metrics["window_end"],
        "sample_count": metrics["sample_count"],
        "actionable_count": metrics["actionable_count"],
        "model_id": (scope or {}).get("model_id"),
        "prompt_version": (scope or {}).get("prompt_version"),
        "source_type": (scope or {}).get("source_type", "live"),
        "status": metrics["status"],
        "metrics": metrics,
    }
