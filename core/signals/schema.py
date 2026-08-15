"""Validated signal contract shared by future AI, API, paper, and outcome layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from ..instruments import Instrument


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
