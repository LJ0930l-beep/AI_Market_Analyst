"""Versioned empirical confidence calibration with honest sample gates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Iterable

from ..signals.schema import Action, SignalProposal
from ..model_routing import DEFAULT_SMART_MODEL
from .metrics import CONFIDENCE_BUCKETS, _float, _outcome, _prediction, _timestamp, confidence_bucket, filter_records, is_resolved_actionable


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    lower: float
    upper: float
    n: int
    wins: int
    empirical_rate: float | None
    shrunk_rate: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "n": self.n,
            "wins": self.wins,
            "empirical_rate": self.empirical_rate,
            "shrunk_rate": self.shrunk_rate,
        }


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    calibration_id: str
    version: str
    scope: dict[str, Any]
    method: str
    params: dict[str, Any]
    sample_count: int
    trained_until: str | None
    brier_raw: float | None
    brier_calibrated: float | None
    ece_raw: float | None
    ece_calibrated: float | None
    status: str
    fallback: str | None
    buckets: tuple[CalibrationBucket, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibration_id": self.calibration_id,
            "version": self.version,
            "scope": self.scope,
            "method": self.method,
            "params": self.params,
            "sample_count": self.sample_count,
            "trained_until": self.trained_until,
            "brier_raw": self.brier_raw,
            "brier_calibrated": self.brier_calibrated,
            "ece_raw": self.ece_raw,
            "ece_calibrated": self.ece_calibrated,
            "status": self.status,
            "fallback": self.fallback,
            "buckets": [bucket.to_dict() for bucket in self.buckets],
        }


def _eligible_records(
    records: Iterable[dict[str, Any]],
    *,
    scope: dict[str, Any] | None,
    trained_until: datetime | None,
) -> list[dict[str, Any]]:
    selected = [record for record in filter_records(records, scope) if is_resolved_actionable(record)]
    if trained_until is None:
        return selected
    cutoff = trained_until.astimezone(timezone.utc)
    return [
        record
        for record in selected
        if (_timestamp((_outcome(record) or {}).get("settled_at")) or datetime.min.replace(tzinfo=timezone.utc)) < cutoff
    ]


def _bucket_entries(records: list[dict[str, Any]]) -> dict[str, list[tuple[float, int]]]:
    grouped: dict[str, list[tuple[float, int]]] = {}
    for record in records:
        prediction = _prediction(record)
        raw = _float(prediction.get("raw_confidence"))
        if raw is None:
            continue
        realized = _float((_outcome(record) or {}).get("realized_r")) or 0.0
        grouped.setdefault(confidence_bucket(raw), []).append((raw, 1 if realized > 0 else 0))
    return grouped


def _reliability(records: list[dict[str, Any]], value_for: Any) -> float | None:
    entries: list[tuple[float, int]] = []
    for record in records:
        outcome = _outcome(record) or {}
        target = 1 if (_float(outcome.get("realized_r")) or 0.0) > 0 else 0
        value = value_for(record)
        if value is not None:
            entries.append((value, target))
    if not entries:
        return None
    grouped: dict[str, list[tuple[float, int]]] = {}
    for value, target in entries:
        grouped.setdefault(confidence_bucket(value), []).append((value, target))
    return sum(
        (len(values) / len(entries)) * abs(mean(item[0] for item in values) - mean(item[1] for item in values))
        for values in grouped.values()
    )


def fit_calibration(
    records: Iterable[dict[str, Any]],
    *,
    scope: dict[str, Any] | None = None,
    global_records: Iterable[dict[str, Any]] | None = None,
    trained_until: datetime | None = None,
    min_sample: int = 100,
    alpha: float = 5.0,
    beta: float = 5.0,
    version: str | None = None,
) -> CalibrationResult:
    records = list(records)
    scope = dict(scope or {})
    candidate = _eligible_records(records, scope=scope, trained_until=trained_until)
    effective_scope = dict(scope)
    fallback: str | None = None
    if len(candidate) < min_sample and scope and global_records is not None:
        global_candidate = _eligible_records(list(global_records), scope={}, trained_until=trained_until)
        if len(global_candidate) >= min_sample:
            candidate = global_candidate
            effective_scope = {}
            fallback = "global"
    model_id = str(effective_scope.get("model_id") or DEFAULT_SMART_MODEL).replace(":", "-")
    version = version or f"cal-v1-{model_id}"
    trained_text = trained_until.astimezone(timezone.utc).isoformat() if trained_until else None
    grouped = _bucket_entries(candidate)
    buckets: list[CalibrationBucket] = []
    shrunk_by_bucket: dict[str, float] = {}
    for lower, upper in CONFIDENCE_BUCKETS:
        key = f"{lower:.2f}-{upper:.2f}"
        entries = grouped.get(key, [])
        n = len(entries)
        wins = sum(item[1] for item in entries)
        empirical = wins / n if n else None
        shrunk = (wins + alpha) / (n + alpha + beta) if n else alpha / (alpha + beta)
        if n:
            shrunk_by_bucket[key] = shrunk
        buckets.append(CalibrationBucket(lower, upper, n, wins, empirical, shrunk))
    active = len(candidate) >= min_sample
    status = "ACTIVE" if active else "INSUFFICIENT_SAMPLE"
    if not active and fallback is None:
        fallback = "raw_insufficient_sample"

    def calibrated_value(record: dict[str, Any]) -> float | None:
        raw = _float(_prediction(record).get("raw_confidence"))
        if raw is None:
            return None
        if not active:
            return raw
        return shrunk_by_bucket.get(confidence_bucket(raw), raw)

    raw_values: list[float] = []
    calibrated_values: list[float] = []
    targets: list[int] = []
    for record in candidate:
        raw = _float(_prediction(record).get("raw_confidence"))
        calibrated = calibrated_value(record)
        if raw is None or calibrated is None:
            continue
        target = 1 if (_float((_outcome(record) or {}).get("realized_r")) or 0.0) > 0 else 0
        raw_values.append(raw)
        calibrated_values.append(calibrated)
        targets.append(target)
    brier_raw = mean((value - target) ** 2 for value, target in zip(raw_values, targets)) if raw_values else None
    brier_calibrated = mean((value - target) ** 2 for value, target in zip(calibrated_values, targets)) if calibrated_values else None
    ece_raw = _reliability(candidate, lambda record: _float(_prediction(record).get("raw_confidence")))
    ece_calibrated = _reliability(candidate, calibrated_value)
    calibration_id = hashlib.sha256(
        json.dumps({"version": version, "scope": effective_scope, "trained_until": trained_text, "n": len(candidate)}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return CalibrationResult(
        calibration_id=calibration_id,
        version=version,
        scope=effective_scope,
        method="empirical_beta_shrinkage",
        params={"alpha": alpha, "beta": beta, "min_sample": min_sample},
        sample_count=len(candidate),
        trained_until=trained_text,
        brier_raw=brier_raw,
        brier_calibrated=brier_calibrated,
        ece_raw=ece_raw,
        ece_calibrated=ece_calibrated,
        status=status,
        fallback=fallback,
        buckets=tuple(buckets),
    )


def apply_calibration(signal: SignalProposal, result: CalibrationResult) -> SignalProposal:
    raw = signal.raw_confidence
    if signal.action is Action.WAIT:
        calibrated = None
    else:
        calibrated = calibrated_confidence(raw, result)
    scope_value = json.dumps(result.scope, sort_keys=True, separators=(",", ":")) if result.scope else "global"
    fallback = result.fallback
    if signal.action is not Action.WAIT and result.status == "ACTIVE" and calibrated == raw and result.fallback is None:
        fallback = "bucket_fallback"
    return replace(
        signal,
        calibrated_confidence=calibrated,
        calibration_version=result.version,
        calibration_scope=scope_value,
        calibration_sample_size=result.sample_count,
        calibration_fallback=fallback,
    )


def calibrated_confidence(raw_confidence: float, result: CalibrationResult) -> float:
    """Map one raw actionable confidence without mutating the original value."""

    if result.status != "ACTIVE":
        return raw_confidence
    key = confidence_bucket(raw_confidence)
    for bucket in result.buckets:
        if f"{bucket.lower:.2f}-{bucket.upper:.2f}" == key and bucket.n:
            return bucket.shrunk_rate if bucket.shrunk_rate is not None else raw_confidence
    return raw_confidence
