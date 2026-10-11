"""Causal, bounded OHLCV facts for the isolated V36 research prompt."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
PROMPT_BAR_LIMITS = {"15m": 8, "5m": 6, "1h": 4, "4h": 4}
REQUIRED_TIMEFRAMES = ("15m", "5m", "1h", "4h")
VALID_QUALITY = {"VALID", "FRESH", "READY", "RECONSTRUCTED_LATE", "FROZEN_RESEARCH"}


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CausalBar:
    timeframe: str
    start: datetime
    end: datetime
    available_at: datetime
    source: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    ref: str

    def record(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "bar_start": _stamp(self.start),
            "bar_end": _stamp(self.end),
            "available_at": _stamp(self.available_at),
            "source": self.source,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "evidence_ref": self.ref,
        }


@dataclass(frozen=True)
class CausalContext:
    decision_time: datetime
    frames: dict[str, dict[str, Any]]
    bars: dict[str, tuple[CausalBar, ...]]
    input_sha256: str
    status: str

    @property
    def evidence_refs(self) -> set[str]:
        return {
            str(ref)
            for frame in self.frames.values()
            for ref in frame.get("evidence_refs", [])
        }

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "pa-decision-quality-v36/context-1",
            "decision_time": _stamp(self.decision_time),
            "data_as_of_by_timeframe": {
                timeframe: frame.get("data_as_of") for timeframe, frame in self.frames.items()
            },
            "input_sha256": self.input_sha256,
            "input_status": self.status,
            "frames": self.frames,
            "interpretation_contract": {
                "python_facts_are_not_model_conclusions": True,
                "model_market_regime_required": True,
                "model_trade_location_required": True,
                "model_signal_is_interpretation_unless_fact_is_explicit": True,
                "python_facts_do_not_authorize_orders": True,
                "future_hypotheses_are_not_facts": True,
            },
        }

    def record(self) -> dict[str, Any]:
        return {
            "schema_version": "pa-decision-quality-v36/context-1",
            "decision_time": _stamp(self.decision_time),
            "data_as_of_by_timeframe": {
                timeframe: frame.get("data_as_of") for timeframe, frame in self.frames.items()
            },
            "input_sha256": self.input_sha256,
            "status": self.status,
            "frames": self.frames,
        }


class FrameError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _frame_bars(rows: Any, timeframe: str, decision_time: datetime,
                *, minimum_bars: int, maximum_bars: int) -> tuple[CausalBar, ...]:
    if timeframe not in TIMEFRAME_MINUTES:
        raise FrameError("TIMEFRAME_UNSUPPORTED")
    if not isinstance(rows, (list, tuple)):
        raise FrameError("BARS_MISSING")
    duration = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    causal_ends: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        end = _utc(row.get("bar_end"))
        available = _utc(row.get("available_at"))
        if end is None or available is None or end >= decision_time or available >= decision_time:
            continue
        if causal_ends and end < causal_ends[-1]:
            raise FrameError("BAR_ORDER_INVALID")
        causal_ends.append(end)
    output: list[CausalBar] = []
    seen: set[datetime] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise FrameError("BAR_NOT_OBJECT")
        start = _utc(row.get("bar_start", row.get("timestamp")))
        end = _utc(row.get("bar_end"))
        available = _utc(row.get("available_at"))
        if start is None or end is None or available is None:
            raise FrameError("BAR_TIME_UNKNOWN")
        # Strictly-before removes equal-timestamp ordering ambiguity. Future
        # values are not inspected or hashed, so appending them cannot alter a decision.
        if end >= decision_time or available >= decision_time:
            continue
        if row.get("is_closed") is not True:
            raise FrameError("BAR_NOT_CONFIRMED_CLOSED")
        if row.get("timeframe") not in (None, timeframe):
            raise FrameError("BAR_TIMEFRAME_MISMATCH")
        if end - start != duration:
            raise FrameError("BAR_DURATION_INVALID")
        if str(row.get("quality_status") or "").strip().upper() not in VALID_QUALITY:
            raise FrameError("BAR_QUALITY_UNKNOWN")
        source = str(row.get("source") or "").strip()
        if not source:
            raise FrameError("BAR_SOURCE_UNKNOWN")
        values = {key: _num(row.get(key)) for key in ("open", "high", "low", "close", "volume")}
        if any(value is None for value in values.values()):
            raise FrameError("BAR_OHLCV_INVALID")
        opening, high, low, close, volume = (values[key] for key in ("open", "high", "low", "close", "volume"))
        if (min(opening, high, low, close) <= 0 or volume < 0
                or high < max(opening, close, low) or low > min(opening, close)):
            raise FrameError("BAR_OHLCV_GEOMETRY_INVALID")
        if end in seen:
            raise FrameError("DUPLICATE_BAR_END")
        ref_seed = f"{timeframe}|{_stamp(start)}|{_stamp(end)}|{source}"
        ref = "bar:" + timeframe + ":" + hashlib.sha256(ref_seed.encode()).hexdigest()[:16]
        output.append(CausalBar(timeframe, start, end, available, source,
                                opening, high, low, close, volume, ref))
        seen.add(end)
    previous_end: datetime | None = None
    for item in output:
        if previous_end is not None:
            if item.end < previous_end:
                raise FrameError("BAR_ORDER_INVALID")
            if item.end - previous_end != duration:
                raise FrameError("BAR_GAP_OR_DUPLICATE")
        previous_end = item.end
    output = output[-maximum_bars:]
    if len(output) < minimum_bars:
        raise FrameError("INSUFFICIENT_CAUSAL_BARS")
    if decision_time - output[-1].end > duration + timedelta(minutes=2):
        raise FrameError("STALE_CAUSAL_BARS")
    return tuple(output)


def _round(value: float | None, digits: int = 8) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _frame_facts(timeframe: str, bars: tuple[CausalBar, ...]) -> dict[str, Any]:
    recent = list(bars[-20:])
    last = bars[-1]
    prior = list(bars[-21:-1])
    changes = [right.close - left.close for left, right in pairwise(recent)]
    true_ranges: list[float] = []
    for left, right in pairwise(bars[-15:]):
        true_ranges.append(max(right.high - right.low,
                               abs(right.high - left.close),
                               abs(right.low - left.close)))
    atr = sum(true_ranges) / len(true_ranges) if true_ranges else None
    overlap_values: list[float] = []
    for left, right in pairwise(recent):
        denominator = min(left.high - left.low, right.high - right.low)
        overlap_values.append(
            max(0.0, min(left.high, right.high) - max(left.low, right.low)) / denominator
            if denominator > 0 else 0.0
        )

    swings: list[dict[str, Any]] = []
    for index in range(2, len(bars) - 2):
        window = bars[index - 2:index + 3]
        pivot = bars[index]
        known_at = max(window[-1].end, *(item.available_at for item in window))
        confirmation_refs = [item.ref for item in window]
        if all(pivot.high > item.high for item in window if item is not pivot):
            swings.append({"side": "HIGH", "price": _round(pivot.high),
                           "pivot_at": _stamp(pivot.end), "confirmed_at": _stamp(window[-1].end),
                           "available_at": _stamp(max(item.available_at for item in window)),
                           "known_at": _stamp(known_at), "evidence_ref": pivot.ref,
                           "confirmation_evidence_refs": confirmation_refs})
        if all(pivot.low < item.low for item in window if item is not pivot):
            swings.append({"side": "LOW", "price": _round(pivot.low),
                           "pivot_at": _stamp(pivot.end), "confirmed_at": _stamp(window[-1].end),
                           "available_at": _stamp(max(item.available_at for item in window)),
                           "known_at": _stamp(known_at), "evidence_ref": pivot.ref,
                           "confirmation_evidence_refs": confirmation_refs})
    swings.sort(key=lambda item: (item["pivot_at"], item["side"]))
    highs = [item for item in swings if item["side"] == "HIGH"][-2:]
    lows = [item for item in swings if item["side"] == "LOW"][-2:]
    swing_progression = {
        "highs": "HIGHER_HIGH" if len(highs) == 2 and highs[-1]["price"] > highs[-2]["price"] else (
            "LOWER_HIGH" if len(highs) == 2 and highs[-1]["price"] < highs[-2]["price"] else "UNKNOWN"
        ),
        "lows": "HIGHER_LOW" if len(lows) == 2 and lows[-1]["price"] > lows[-2]["price"] else (
            "LOWER_LOW" if len(lows) == 2 and lows[-1]["price"] < lows[-2]["price"] else "UNKNOWN"
        ),
    }
    previous_high = max(item.high for item in prior) if prior else None
    previous_low = min(item.low for item in prior) if prior else None
    range_high_refs = sorted({item.ref for item in prior if item.high == previous_high})
    range_low_refs = sorted({item.ref for item in prior if item.low == previous_low})
    range_position = None
    location_zone = "UNKNOWN"
    if previous_high is not None and previous_low is not None and previous_high > previous_low:
        range_position = (last.close - previous_low) / (previous_high - previous_low)
        if last.close > previous_high or last.close < previous_low:
            location_zone = "OUTSIDE_PRIOR_RANGE"
        elif range_position <= 0.2:
            location_zone = "LOWER_RANGE_AREA"
        elif range_position >= 0.8:
            location_zone = "UPPER_RANGE_AREA"
        else:
            location_zone = "RANGE_MIDDLE"
    breakout = "NONE"
    breakout_refs: list[str] = []
    if previous_high is not None and last.close > previous_high:
        breakout = "CLOSE_ABOVE_PRIOR_RANGE"
        breakout_refs = [last.ref, *range_high_refs]
    elif previous_low is not None and last.close < previous_low:
        breakout = "CLOSE_BELOW_PRIOR_RANGE"
        breakout_refs = [last.ref, *range_low_refs]

    failed_breakout = None
    start_index = max(21, len(bars) - 5)
    for index in range(start_index, len(bars)):
        event_bar = bars[index - 1]
        return_bar = bars[index]
        if index < 21:
            continue
        frozen = bars[index - 21:index - 1]
        if len(frozen) != 20:
            continue
        frozen_high = max(item.high for item in frozen)
        frozen_low = min(item.low for item in frozen)
        if event_bar.close > frozen_high and return_bar.close <= frozen_high:
            failed_breakout = {"direction": "UPPER_FAILURE", "level": _round(frozen_high),
                               "event_bar_at": _stamp(event_bar.end), "confirmed_at": _stamp(return_bar.end),
                               "evidence_refs": [event_bar.ref, return_bar.ref]}
        elif event_bar.close < frozen_low and return_bar.close >= frozen_low:
            failed_breakout = {"direction": "LOWER_FAILURE", "level": _round(frozen_low),
                               "event_bar_at": _stamp(event_bar.end), "confirmed_at": _stamp(return_bar.end),
                               "evidence_refs": [event_bar.ref, return_bar.ref]}

    extension = None
    if atr and atr > 0:
        mean_close = sum(item.close for item in recent) / len(recent)
        extension = (last.close - mean_close) / atr
    direction_fraction = sum(change > 0 for change in changes) / len(changes) if changes else None
    refs = [last.ref]
    recent_limit = PROMPT_BAR_LIMITS.get(timeframe, 4)
    recent_bars = [item.record() for item in bars[-recent_limit:]]
    refs.extend(item.ref for item in bars[-recent_limit:])
    refs.extend(item["evidence_ref"] for item in (*highs, *lows))
    refs.extend(ref for item in (*highs, *lows)
                for ref in item.get("confirmation_evidence_refs", []))
    refs.extend(range_high_refs)
    refs.extend(range_low_refs)
    refs.extend(breakout_refs)
    if failed_breakout:
        refs.extend(failed_breakout["evidence_refs"])
    return {
        "status": "READY",
        "data_as_of": _stamp(last.end),
        "bar_count": len(bars),
        "recent_candles": recent_bars,
        "source": sorted({item.source for item in bars}),
        "evidence_refs": sorted(set(refs)),
        "objective_facts": {
            "last_close": _round(last.close),
            "close_change_20_fraction": _round((last.close / recent[0].close - 1) if recent[0].close else None),
            "up_close_fraction_19": _round(direction_fraction),
            "mean_adjacent_range_overlap": _round(sum(overlap_values) / len(overlap_values) if overlap_values else None),
            "atr14_simple": _round(atr),
            "extension_from_mean_in_atr": _round(extension),
            "confirmed_swings": [*highs, *lows],
            "swing_progression": swing_progression,
            "prior_20_range": {
                "high": _round(previous_high), "high_evidence_refs": range_high_refs,
                "low": _round(previous_low), "low_evidence_refs": range_low_refs,
            },
            "position_in_prior_range": _round(range_position),
            "location_zone_fact": location_zone,
            "close_breakout_fact": breakout,
            "close_breakout_evidence_refs": sorted(set(breakout_refs)),
            "failed_breakout_fact": failed_breakout,
        },
        "model_interpretation_required": ["market_regime", "higher_timeframe_bias", "trade_location", "setup_quality"],
    }


def build_context(bars_by_timeframe: dict[str, Any], decision_time: Any,
                  *, required_timeframes: Iterable[str] = REQUIRED_TIMEFRAMES,
                  minimum_bars: int = 32, maximum_bars: int = 240) -> CausalContext:
    """Build immutable facts from bars strictly closed and available before decision_time."""
    point = _utc(decision_time)
    if point is None:
        raise ValueError("DECISION_TIME_INVALID")
    if not isinstance(bars_by_timeframe, dict):
        raise TypeError("TIMEFRAME_INPUT_INVALID")
    required = tuple(dict.fromkeys(str(item).strip().lower() for item in required_timeframes))
    frames: dict[str, dict[str, Any]] = {}
    bars: dict[str, tuple[CausalBar, ...]] = {}
    frame_hashes: dict[str, Any] = {}
    for timeframe in required:
        try:
            causal_bars = _frame_bars(bars_by_timeframe.get(timeframe), timeframe, point,
                                      minimum_bars=minimum_bars, maximum_bars=maximum_bars)
            bars[timeframe] = causal_bars
            facts = _frame_facts(timeframe, causal_bars)
            frames[timeframe] = facts
            frame_hashes[timeframe] = [item.record() for item in causal_bars]
        except FrameError as exc:
            frames[timeframe] = {"status": "UNAVAILABLE", "reason": exc.code,
                                 "data_as_of": None, "evidence_refs": [], "objective_facts": {}}
            frame_hashes[timeframe] = {"status": "UNAVAILABLE", "reason": exc.code}
    all_ready = all(frames.get(tf, {}).get("status") == "READY" for tf in required)
    input_hash = _canonical_sha({"decision_time": _stamp(point), "timeframes": frame_hashes})
    return CausalContext(point, frames, bars, input_hash, "READY" if all_ready else "INCOMPLETE")
