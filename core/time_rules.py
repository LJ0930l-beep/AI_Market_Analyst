"""Initial, explicit time-validity rules for signal lifecycle tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .providers.news import NewsEvent


@dataclass(frozen=True, slots=True)
class TimeRule:
    signal_validity_min: int
    signal_validity_max: int
    max_hold_min: int


@dataclass(frozen=True, slots=True)
class TimePolicy:
    timeframe: str
    signal_validity_min: int
    signal_validity_max: int
    holding_horizon_min: int
    holding_horizon_max: int
    reevaluate_min: int
    reevaluate_max: int
    volatility_ratio: float
    market_regime: str
    event_risk: bool
    reason_codes: tuple[str, ...]
    signal_validity_allowed: tuple[int, ...] = ()
    holding_horizon_allowed: tuple[int, ...] = ()
    reevaluate_allowed: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "signal_validity_minutes": [self.signal_validity_min, self.signal_validity_max],
            "holding_horizon_minutes": [self.holding_horizon_min, self.holding_horizon_max],
            "reevaluate_minutes": [self.reevaluate_min, self.reevaluate_max],
            "allowed_values": {
                "signal_validity_minutes": list(self.signal_validity_allowed),
                "holding_horizon_minutes": list(self.holding_horizon_allowed),
                "reevaluate_minutes": list(self.reevaluate_allowed),
            },
            "volatility_ratio": round(self.volatility_ratio, 4),
            "market_regime": self.market_regime,
            "event_risk": self.event_risk,
            "reason_codes": list(self.reason_codes),
        }


BASELINE_RULES: dict[str, TimeRule] = {
    "5m": TimeRule(30, 90, 24 * 60),
    "15m": TimeRule(60, 180, 24 * 60),
    "1h": TimeRule(120, 480, 3 * 24 * 60),
    "4h": TimeRule(480, 1440, 10 * 24 * 60),
    "1d": TimeRule(1440, 4320, 28 * 24 * 60),
}

# Phase 2 policy ranges from the specification. The older TimeRule map remains
# available for Phase 1 callers that only need a single max-hold value.
PHASE2_RANGES: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "5m": ((15, 60), (60, 24 * 60)),
    "15m": ((30, 120), (60, 24 * 60)),
    "1h": ((120, 360), (24 * 60, 3 * 24 * 60)),
    "4h": ((240, 720), (3 * 24 * 60, 10 * 24 * 60)),
    "1d": ((1440, 1440), (7 * 24 * 60, 28 * 24 * 60)),
}

_VALIDITY_VALUES = {
    "5m": (15, 30, 45, 60),
    "15m": (30, 60, 90, 120),
    "1h": (120, 180, 240, 360),
    "4h": (240, 360, 480, 720),
    "1d": (1440,),
}
_HOLDING_VALUES = {
    "5m": (60, 120, 240, 480, 720, 1440),
    "15m": (60, 120, 240, 480, 720, 1440),
    "1h": (1440, 2160, 2880, 4320),
    "4h": (4320, 5760, 7200, 10080, 14400),
    "1d": (10080, 20160, 30240, 40320),
}


def _allowed_values(candidates: tuple[int, ...], low: int, high: int) -> tuple[int, ...]:
    values = {low, high}
    values.update(candidate for candidate in candidates if low <= candidate <= high)
    return tuple(sorted(values))


def time_rule_for(timeframe: str, volatility_ratio: float = 1.0) -> TimeRule:
    """Return a bounded rule; elevated volatility shortens entry validity."""

    key = timeframe.lower()
    if key not in BASELINE_RULES:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    base = BASELINE_RULES[key]
    if volatility_ratio <= 1.25:
        return base
    factor = 0.5 if volatility_ratio >= 2.0 else 0.75
    return TimeRule(
        signal_validity_min=max(15, round(base.signal_validity_min * factor)),
        signal_validity_max=max(30, round(base.signal_validity_max * factor)),
        max_hold_min=base.max_hold_min,
    )


def build_time_policy(
    timeframe: str,
    *,
    price: float,
    atr14: float,
    market_regime: str,
    events: Iterable[NewsEvent] = (),
    event_evidence: Iterable[object] = (),
    now: datetime | None = None,
) -> TimePolicy:
    """Build bounded time choices using only program-calculated inputs."""

    key = timeframe.lower()
    if key not in PHASE2_RANGES:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    if price <= 0 or atr14 < 0:
        raise ValueError("price must be positive and ATR must be non-negative")
    events = tuple(events)
    event_evidence = tuple(event_evidence)
    validity, holding = PHASE2_RANGES[key]
    volatility_ratio = atr14 / max(price * 0.01, 1e-9)
    reasons: list[str] = ["timeframe_baseline"]
    validity_min, validity_max = validity
    holding_min, holding_max = holding
    if volatility_ratio >= 2.0:
        validity_min = max(15, round(validity_min * 0.5))
        validity_max = max(validity_min, round(validity_max * 0.5))
        reasons.append("high_atr_shorten_validity")
    elif volatility_ratio >= 1.25:
        validity_min = max(15, round(validity_min * 0.75))
        validity_max = max(validity_min, round(validity_max * 0.75))
        reasons.append("elevated_atr_shorten_validity")
    if market_regime == "range":
        validity_min = max(15, round(validity_min * 0.75))
        validity_max = max(validity_min, round(validity_max * 0.75))
        reasons.append("range_regime_shorten_validity")
    elif market_regime in {"bull_trend", "bear_trend"} and volatility_ratio < 1.25:
        validity_max = min(validity_max + max(15, validity_max // 10), validity_max * 2)
        reasons.append("trend_regime_allowed_extension")
    event_risk = any(event.importance >= 70 for event in events) or any(
        int(getattr(event, "importance", 0) or 0) >= 70 for event in event_evidence
    )
    if event_risk:
        holding_max = max(holding_min, min(holding_max, 3 * 24 * 60))
        validity_max = min(validity_max, max(validity_min, 4 * 60))
        reasons.append("high_impact_event_cap")
        if any(int(getattr(event, "importance", 0) or 0) >= 70 for event in event_evidence):
            reasons.append("phase6_event_evidence")
    reevaluate_min = max(15, validity_min // 2)
    reevaluate_max = max(reevaluate_min, validity_max)
    validity_allowed = _allowed_values(_VALIDITY_VALUES[key], validity_min, validity_max)
    holding_allowed = _allowed_values(_HOLDING_VALUES[key], holding_min, holding_max)
    reevaluate_allowed = _allowed_values(tuple(max(15, value // 2) for value in validity_allowed), reevaluate_min, reevaluate_max)
    return TimePolicy(
        timeframe=key,
        signal_validity_min=validity_min,
        signal_validity_max=validity_max,
        holding_horizon_min=holding_min,
        holding_horizon_max=holding_max,
        reevaluate_min=reevaluate_min,
        reevaluate_max=reevaluate_max,
        volatility_ratio=volatility_ratio,
        market_regime=market_regime,
        event_risk=event_risk,
        reason_codes=tuple(reasons),
        signal_validity_allowed=validity_allowed,
        holding_horizon_allowed=holding_allowed,
        reevaluate_allowed=reevaluate_allowed,
    )
