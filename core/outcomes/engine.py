"""Deterministic TP/SL/timeout settlement with MFE/MAE and R multiple."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from ..providers.base import Bar
from ..signals.schema import Action, SignalProposal


class OutcomeStatus(StrEnum):
    TP1 = "TP1"
    TP2 = "TP2"
    STOP = "STOP"
    TIMEOUT = "TIMEOUT"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"


@dataclass(frozen=True, slots=True)
class Outcome:
    prediction_id: str
    status: OutcomeStatus
    settled_at: datetime
    exit_price: float | None
    realized_r: float | None
    mfe_r: float | None
    mae_r: float | None
    bars_held: int
    timeout: bool

    def __post_init__(self) -> None:
        if self.settled_at.tzinfo is None:
            raise ValueError("settled_at must be timezone-aware")
        if self.bars_held < 0:
            raise ValueError("bars_held must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "status": self.status.value,
            "settled_at": self.settled_at.astimezone(timezone.utc).isoformat(),
            "exit_price": self.exit_price,
            "realized_r": self.realized_r,
            "mfe_r": self.mfe_r,
            "mae_r": self.mae_r,
            "bars_held": self.bars_held,
            "timeout": self.timeout,
        }


def settle_prediction(signal: SignalProposal, bars: list[Bar]) -> Outcome:
    """Settle a signal against future bars.

    If a single OHLC bar touches both a stop and a target, the stop wins. This
    conservative tie-break removes an otherwise unobservable intrabar ordering.
    """

    if signal.action is Action.WAIT:
        return Outcome(signal.prediction_id, OutcomeStatus.NOT_ACTIONABLE, signal.generated_at, None, None, None, None, 0, False)
    assert signal.entry_low is not None and signal.entry_high is not None
    assert signal.stop is not None and signal.tp1 is not None and signal.tp2 is not None
    entry = (signal.entry_low + signal.entry_high) / 2.0
    risk = abs(entry - signal.stop)
    if risk <= 0:
        raise ValueError("signal risk must be positive")
    ordered = sorted((bar for bar in bars if bar.timestamp > signal.generated_at), key=lambda bar: bar.timestamp)
    if not ordered:
        raise ValueError("outcome settlement requires future bars")
    mfe = 0.0
    mae = 0.0
    for index, bar in enumerate(ordered, start=1):
        if signal.action is Action.LONG:
            mfe = max(mfe, (bar.high - entry) / risk)
            mae = min(mae, (bar.low - entry) / risk)
            # Conservative order when both thresholds are touched.
            if bar.low <= signal.stop:
                return Outcome(signal.prediction_id, OutcomeStatus.STOP, bar.timestamp, signal.stop, -1.0, mfe, mae, index, False)
            if bar.high >= signal.tp2:
                return Outcome(signal.prediction_id, OutcomeStatus.TP2, bar.timestamp, signal.tp2, (signal.tp2 - entry) / risk, mfe, mae, index, False)
            if bar.high >= signal.tp1:
                return Outcome(signal.prediction_id, OutcomeStatus.TP1, bar.timestamp, signal.tp1, (signal.tp1 - entry) / risk, mfe, mae, index, False)
        else:
            mfe = max(mfe, (entry - bar.low) / risk)
            mae = min(mae, (entry - bar.high) / risk)
            if bar.high >= signal.stop:
                return Outcome(signal.prediction_id, OutcomeStatus.STOP, bar.timestamp, signal.stop, -1.0, mfe, mae, index, False)
            if bar.low <= signal.tp2:
                return Outcome(signal.prediction_id, OutcomeStatus.TP2, bar.timestamp, signal.tp2, (entry - signal.tp2) / risk, mfe, mae, index, False)
            if bar.low <= signal.tp1:
                return Outcome(signal.prediction_id, OutcomeStatus.TP1, bar.timestamp, signal.tp1, (entry - signal.tp1) / risk, mfe, mae, index, False)
        if bar.timestamp >= signal.max_hold_until:
            return Outcome(signal.prediction_id, OutcomeStatus.TIMEOUT, bar.timestamp, bar.close, _realized_r(signal.action, entry, bar.close, risk), mfe, mae, index, True)
    last = ordered[-1]
    return Outcome(signal.prediction_id, OutcomeStatus.TIMEOUT, last.timestamp, last.close, _realized_r(signal.action, entry, last.close, risk), mfe, mae, len(ordered), True)


def _realized_r(action: Action, entry: float, exit_price: float, risk: float) -> float:
    if action is Action.LONG:
        return (exit_price - entry) / risk
    return (entry - exit_price) / risk

