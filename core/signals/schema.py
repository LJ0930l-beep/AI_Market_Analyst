"""Validated signal contract shared by future AI, API, paper, and outcome layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import math
from typing import Any, Mapping

from ..instruments import Instrument, instrument_from_payload


class Action(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


@dataclass(frozen=True, slots=True)
class SignalProposal:
    prediction_id: str
    instrument: Instrument
    analysis_timeframe: str
    generated_at: datetime
    action: Action
    entry_low: float | None
    entry_high: float | None
    stop: float | None
    tp1: float | None
    tp2: float | None
    signal_validity_minutes: int
    expected_hold_minutes: int
    max_hold_minutes: int
    reevaluate_at: datetime
    invalidation: tuple[str, ...]
    raw_confidence: float
    reason_codes: tuple[str, ...]
    summary: str
    model_id: str = "phase1-quant-baseline"
    model_version: str | None = None
    prompt_version: str | None = None
    input_hash: str | None = None
    data_as_of: datetime | None = None
    context_json: str | None = None
    raw_model_response: str | None = None
    parse_status: str = "not_applicable"
    latency_ms: float | None = None
    source_type: str = "live"
    replay_run_id: str | None = None
    calibrated_confidence: float | None = None
    calibration_version: str | None = None
    calibration_scope: str | None = None
    calibration_sample_size: int | None = None
    calibration_fallback: str | None = None

    def __post_init__(self) -> None:
        if not self.prediction_id:
            raise ValueError("prediction_id is required")
        if self.generated_at.tzinfo is None or self.reevaluate_at.tzinfo is None:
            raise ValueError("signal timestamps must be timezone-aware")
        if self.data_as_of is not None and self.data_as_of.tzinfo is None:
            raise ValueError("data_as_of must be timezone-aware")
        if self.source_type not in {"live", "replay"}:
            raise ValueError("source_type must be live or replay")
        if self.source_type == "replay" and not self.replay_run_id:
            raise ValueError("replay predictions require replay_run_id")
        if self.calibrated_confidence is not None and not 0.0 <= self.calibrated_confidence <= 1.0:
            raise ValueError("calibrated_confidence must be in [0, 1]")
        if self.calibration_sample_size is not None and self.calibration_sample_size < 0:
            raise ValueError("calibration_sample_size must be non-negative")
        if self.signal_validity_minutes <= 0 or self.expected_hold_minutes <= 0:
            raise ValueError("signal validity and expected hold must be positive")
        if self.max_hold_minutes < self.expected_hold_minutes:
            raise ValueError("max hold must not be shorter than expected hold")
        if self.reevaluate_at <= self.generated_at:
            raise ValueError("reevaluate_at must be after generated_at")
        if not 0.0 <= self.raw_confidence <= 1.0:
            raise ValueError("raw_confidence must be in [0, 1]")
        if not self.summary:
            raise ValueError("summary is required")
        if self.action is Action.WAIT:
            if any(value is not None for value in (self.entry_low, self.entry_high, self.stop, self.tp1, self.tp2)):
                raise ValueError("WAIT must not carry actionable price levels")
            return
        levels = (self.entry_low, self.entry_high, self.stop, self.tp1, self.tp2)
        if any(value is None or value <= 0 for value in levels):
            raise ValueError("actionable signals require positive entry/stop/target levels")
        assert self.entry_low is not None
        assert self.entry_high is not None
        assert self.stop is not None
        assert self.tp1 is not None
        assert self.tp2 is not None
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low must not exceed entry_high")
        risk = abs((self.entry_low + self.entry_high) / 2.0 - self.stop)
        if risk <= 0:
            raise ValueError("stop must be different from entry")
        entry = (self.entry_low + self.entry_high) / 2.0
        if self.action is Action.LONG:
            if not self.stop < entry < self.tp1 <= self.tp2:
                raise ValueError("LONG levels must be stop < entry < tp1 <= tp2")
        else:
            if not self.stop > entry > self.tp1 >= self.tp2:
                raise ValueError("SHORT levels must be stop > entry > tp1 >= tp2")
        if abs(self.tp1 - entry) / risk < 1.5 - 1e-9:
            raise ValueError("Phase 1 actionable signal requires TP1 R:R >= 1.5")
        if not self.invalidation:
            raise ValueError("actionable signals require invalidation conditions")

    @property
    def signal_valid_until(self) -> datetime:
        return self.generated_at.astimezone(timezone.utc) + timedelta(minutes=self.signal_validity_minutes)

    @property
    def expected_hold_until(self) -> datetime:
        return self.generated_at.astimezone(timezone.utc) + timedelta(minutes=self.expected_hold_minutes)

    @property
    def max_hold_until(self) -> datetime:
        return self.generated_at.astimezone(timezone.utc) + timedelta(minutes=self.max_hold_minutes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id,
            "instrument": {
                "symbol": self.instrument.symbol,
                "asset_type": self.instrument.asset_type.value,
                "exchange": self.instrument.exchange,
                "currency": self.instrument.currency,
                "timezone": self.instrument.timezone,
                "trading_hours": self.instrument.trading_hours.value,
                "sector": self.instrument.sector,
            },
            "analysis_timeframe": self.analysis_timeframe,
            "generated_at": self.generated_at.astimezone(timezone.utc).isoformat(),
            "action": self.action.value,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "stop": self.stop,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "signal_validity_minutes": self.signal_validity_minutes,
            "signal_valid_until": self.signal_valid_until.isoformat(),
            "expected_hold_minutes": self.expected_hold_minutes,
            "expected_hold_until": self.expected_hold_until.isoformat(),
            "max_hold_minutes": self.max_hold_minutes,
            "max_hold_until": self.max_hold_until.isoformat(),
            "reevaluate_at": self.reevaluate_at.astimezone(timezone.utc).isoformat(),
            "invalidation": list(self.invalidation),
            "raw_confidence": self.raw_confidence,
            "reason_codes": list(self.reason_codes),
            "summary": self.summary,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "input_hash": self.input_hash,
            "data_as_of": self.data_as_of.astimezone(timezone.utc).isoformat() if self.data_as_of else None,
            "context_json": self.context_json,
            "raw_model_response": self.raw_model_response,
            "parse_status": self.parse_status,
            "latency_ms": self.latency_ms,
            "source_type": self.source_type,
            "replay_run_id": self.replay_run_id,
            "calibrated_confidence": self.calibrated_confidence,
            "calibration_version": self.calibration_version,
            "calibration_scope": self.calibration_scope,
            "calibration_sample_size": self.calibration_sample_size,
            "calibration_fallback": self.calibration_fallback,
        }


def _payload_datetime(payload: Mapping[str, Any], key: str, *, required: bool) -> datetime | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"signal payload field {key!r} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"signal payload field {key!r} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"signal payload field {key!r} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _payload_number(payload: Mapping[str, Any], key: str, *, required: bool) -> float | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"signal payload field {key!r} must be a finite number")
    return float(value)


def _payload_integer(payload: Mapping[str, Any], key: str, *, required: bool) -> int | None:
    value = payload.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"signal payload field {key!r} must be an integer")
    return int(value)


def _payload_strings(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"signal payload field {key!r} must be a list of non-empty strings")
    return tuple(value)


def _payload_optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"signal payload field {key!r} must be a string or null")
    return value


def signal_from_payload(payload: Mapping[str, Any], *, instrument: Instrument | None = None) -> SignalProposal:
    """Rehydrate a stored SignalProposal through the same financial validators.

    Settlement never constructs a signal by trusting a subset of JSON fields.
    The nested instrument is validated and, when a durable store resolution is
    supplied, must match it exactly before ``SignalProposal`` validates all
    action levels, horizons and confidence bounds.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("stored signal payload must be an object")
    nested_instrument = payload.get("instrument")
    if not isinstance(nested_instrument, dict):
        raise ValueError("stored signal payload instrument is invalid")
    stored_instrument = instrument_from_payload(nested_instrument)
    if instrument is None:
        instrument = stored_instrument
    elif instrument != stored_instrument:
        raise ValueError("stored signal instrument does not match the durable instrument registry")
    prediction_id = payload.get("prediction_id")
    timeframe = payload.get("analysis_timeframe")
    summary = payload.get("summary")
    if not isinstance(prediction_id, str) or not prediction_id:
        raise ValueError("stored signal prediction_id is invalid")
    if not isinstance(timeframe, str) or not timeframe:
        raise ValueError("stored signal analysis_timeframe is invalid")
    if not isinstance(summary, str) or not summary:
        raise ValueError("stored signal summary is invalid")
    try:
        action = Action(str(payload["action"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stored signal action is invalid") from exc
    generated_at = _payload_datetime(payload, "generated_at", required=True)
    reevaluate_at = _payload_datetime(payload, "reevaluate_at", required=True)
    signal_validity_minutes = _payload_integer(payload, "signal_validity_minutes", required=True)
    expected_hold_minutes = _payload_integer(payload, "expected_hold_minutes", required=True)
    max_hold_minutes = _payload_integer(payload, "max_hold_minutes", required=True)
    raw_confidence = _payload_number(payload, "raw_confidence", required=True)
    calibrated_confidence = _payload_number(payload, "calibrated_confidence", required=False)
    calibration_sample_size = _payload_integer(payload, "calibration_sample_size", required=False)
    latency_ms = _payload_number(payload, "latency_ms", required=False)
    source_type = payload.get("source_type", "live")
    if not isinstance(source_type, str):
        raise ValueError("stored signal source_type is invalid")
    replay_run_id = _payload_optional_string(payload, "replay_run_id")
    return SignalProposal(
        prediction_id=prediction_id,
        instrument=instrument,
        analysis_timeframe=timeframe,
        generated_at=generated_at,
        action=action,
        entry_low=_payload_number(payload, "entry_low", required=False),
        entry_high=_payload_number(payload, "entry_high", required=False),
        stop=_payload_number(payload, "stop", required=False),
        tp1=_payload_number(payload, "tp1", required=False),
        tp2=_payload_number(payload, "tp2", required=False),
        signal_validity_minutes=signal_validity_minutes,  # type: ignore[arg-type]
        expected_hold_minutes=expected_hold_minutes,  # type: ignore[arg-type]
        max_hold_minutes=max_hold_minutes,  # type: ignore[arg-type]
        reevaluate_at=reevaluate_at,  # type: ignore[arg-type]
        invalidation=_payload_strings(payload, "invalidation"),
        raw_confidence=raw_confidence,  # type: ignore[arg-type]
        reason_codes=_payload_strings(payload, "reason_codes"),
        summary=summary,
        model_id=str(payload.get("model_id", "phase1-quant-baseline")),
        model_version=_payload_optional_string(payload, "model_version"),
        prompt_version=_payload_optional_string(payload, "prompt_version"),
        input_hash=_payload_optional_string(payload, "input_hash"),
        data_as_of=_payload_datetime(payload, "data_as_of", required=False),
        context_json=_payload_optional_string(payload, "context_json"),
        raw_model_response=_payload_optional_string(payload, "raw_model_response"),
        parse_status=str(payload.get("parse_status", "not_applicable")),
        latency_ms=latency_ms,
        source_type=source_type,
        replay_run_id=replay_run_id,
        calibrated_confidence=calibrated_confidence,
        calibration_version=_payload_optional_string(payload, "calibration_version"),
        calibration_scope=_payload_optional_string(payload, "calibration_scope"),
        calibration_sample_size=calibration_sample_size,
        calibration_fallback=_payload_optional_string(payload, "calibration_fallback"),
    )
