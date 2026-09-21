"""Pure, bounded technical indicator snapshots for imported NOFX settings.

This module deliberately has no provider, database, model, or execution
dependencies.  It calculates the latest value of the indicators requested by
NOFX's ``IndicatorConfig`` shape from an already validated, oldest-to-newest
OHLCV sequence.  Derivatives values are copied only when the caller supplies
explicitly verified, source-backed records.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import math
from typing import Any


SNAPSHOT_VERSION = "nofx_indicator_snapshot_v1"
MAX_BARS = 1024
MAX_PERIOD = 500
MAX_PERIODS = 8
_MAX_MAGNITUDE = 1e100
_DEFAULT_PERIODS = {
    "ema_periods": (20, 50),
    "rsi_periods": (7, 14),
    "atr_periods": (14,),
    "boll_periods": (20,),
}
_ENABLE_FLAGS = (
    "enable_ema",
    "enable_macd",
    "enable_rsi",
    "enable_atr",
    "enable_boll",
    "enable_volume",
    "enable_oi",
    "enable_funding_rate",
)


def _strict_bool(value: Any, *, name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"INDICATOR_CONFIG_INVALID:{name}")
    return value


def _periods(value: Any, *, name: str, default: tuple[int, ...]) -> list[int]:
    if value is None:
        result = list(default)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_PERIODS:
            raise ValueError(f"INDICATOR_PERIOD_COUNT_EXCEEDED:{name}")
        result = []
        for period in value:
            if isinstance(period, bool) or not isinstance(period, int):
                raise ValueError(f"INDICATOR_PERIOD_INVALID:{name}")
            if period < 2 or period > MAX_PERIOD:
                raise ValueError(f"INDICATOR_PERIOD_OUT_OF_RANGE:{name}")
            if period not in result:
                result.append(period)
    else:
        raise ValueError(f"INDICATOR_PERIODS_INVALID:{name}")
    if not result:
        raise ValueError(f"INDICATOR_PERIODS_EMPTY:{name}")
    return result


def normalize_nofx_indicator_config(configuration: Any) -> dict[str, Any]:
    """Validate and normalize the indicator portion of a NOFX strategy.

    Both a direct ``IndicatorConfig`` JSON object and a strategy/config wrapper
    containing an ``indicators`` object are accepted.  NOFX defaults are kept
    for periods while switches default to off, matching NOFX's default config.
    """
    if not isinstance(configuration, Mapping):
        raise ValueError("INDICATOR_CONFIG_INVALID")
    raw: Mapping[str, Any] = configuration
    nested = raw.get("indicators")
    if isinstance(nested, Mapping):
        raw = nested

    normalized: dict[str, Any] = {
        name: _strict_bool(raw.get(name), name=name)
        for name in _ENABLE_FLAGS
    }
    for name, default in _DEFAULT_PERIODS.items():
        normalized[name] = _periods(raw.get(name), name=name, default=default)

    lookback = raw.get("volume_lookback", 20)
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 2 <= lookback <= MAX_PERIOD:
        raise ValueError("INDICATOR_PERIOD_OUT_OF_RANGE:volume_lookback")
    normalized["volume_lookback"] = lookback
    return normalized


def _number(value: Any, *, name: str, positive: bool = False, non_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"OHLCV_INVALID_NUMBER:{name}")
    number = float(value)
    if not math.isfinite(number) or abs(number) > _MAX_MAGNITUDE:
        raise ValueError(f"OHLCV_INVALID_NUMBER:{name}")
    if positive and number <= 0:
        raise ValueError(f"OHLCV_OUT_OF_RANGE:{name}")
    if non_negative and number < 0:
        raise ValueError(f"OHLCV_OUT_OF_RANGE:{name}")
    return number


def _get_field(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def _bar_time(row: Any) -> str | None:
    value = _get_field(row, "bar_end")
    if value is None:
        value = _get_field(row, "timestamp")
    if value is None:
        value = _get_field(row, "time")
    if value is None:
        return None
    if isinstance(value, datetime):
        point = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            point = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        except ValueError as exc:
            raise ValueError("OHLCV_INVALID_TIMESTAMP") from exc
        if point.tzinfo is None:
            point = point.replace(tzinfo=timezone.utc)
    else:
        raise ValueError("OHLCV_INVALID_TIMESTAMP")
    return point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any, *, name: str) -> str | None:
    if value is None:
        return None
    try:
        return _bar_time({"timestamp": value})
    except ValueError as exc:
        raise ValueError(f"INDICATOR_INVALID_TIMESTAMP:{name}") from exc


def _clean_label(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"INDICATOR_METADATA_REQUIRED:{name}")
    cleaned = value.strip()
    if len(cleaned) > 128 or any(ord(char) < 32 for char in cleaned):
        raise ValueError(f"INDICATOR_METADATA_INVALID:{name}")
    return cleaned


def _validated_bars(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ValueError("OHLCV_BARS_INVALID")
    if len(rows) > MAX_BARS:
        raise ValueError("OHLCV_BARS_EXCEED_MAX")
    bars: list[dict[str, Any]] = []
    timestamps: list[str | None] = []
    for index, row in enumerate(rows):
        if row is None:
            raise ValueError(f"OHLCV_BAR_INVALID:{index}")
        values = {
            key: _number(_get_field(row, key), name=f"{index}.{key}", positive=key in {"open", "high", "low", "close"}, non_negative=key == "volume")
            for key in ("open", "high", "low", "close", "volume")
        }
        if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"], values["high"]):
            raise ValueError(f"OHLCV_BAR_BOUNDS_INVALID:{index}")
        values["time"] = _bar_time(row)
        bars.append(values)
        timestamps.append(values["time"])

    present = [stamp for stamp in timestamps if stamp is not None]
    if len(present) == len(timestamps) and any(a >= b for a, b in zip(present, present[1:])):
        raise ValueError("OHLCV_BARS_NOT_OLDEST_FIRST")
    return bars


def _rounded(value: float) -> float:
    if not math.isfinite(value) or abs(value) > _MAX_MAGNITUDE:
        raise ValueError("INDICATOR_RESULT_OUT_OF_RANGE")
    result = float(f"{value:.12g}")
    if not math.isfinite(result):
        raise ValueError("INDICATOR_RESULT_OUT_OF_RANGE")
    return result


def _ema_series(values: Sequence[float], period: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) < period:
        return output
    current = sum(values[:period]) / period
    output[period - 1] = current
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        current += alpha * (values[index] - current)
        output[index] = current
    return output


def _rsi(values: Sequence[float], period: int) -> float | None:
    if len(values) <= period:
        return None
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _atr(bars: Sequence[dict[str, Any]], period: int) -> float | None:
    if len(bars) <= period:
        return None
    true_ranges = [
        max(
            bars[index]["high"] - bars[index]["low"],
            abs(bars[index]["high"] - bars[index - 1]["close"]),
            abs(bars[index]["low"] - bars[index - 1]["close"]),
        )
        for index in range(1, len(bars))
    ]
    current = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        current = ((period - 1) * current + true_range) / period
    return current


def _period_group(
    enabled: bool,
    periods: Sequence[int],
    available: Mapping[int, float | None],
    *,
    required_bars_offset: int = 0,
) -> dict[str, Any]:
    if not enabled:
        return {"status": "DISABLED", "periods": {}}
    values: dict[str, dict[str, Any]] = {}
    for period in periods:
        value = available.get(period)
        if value is None:
            values[str(period)] = {"status": "INSUFFICIENT_DATA", "required_bars": period + required_bars_offset}
        else:
            values[str(period)] = {"status": "AVAILABLE", "value": _rounded(value)}
    states = {row["status"] for row in values.values()}
    status = "AVAILABLE" if states == {"AVAILABLE"} else "INSUFFICIENT_DATA" if states == {"INSUFFICIENT_DATA"} else "PARTIAL"
    return {"status": status, "periods": values}


def _macd(closes: Sequence[float]) -> dict[str, Any]:
    fast_period, slow_period, signal_period = 12, 26, 9
    fast = _ema_series(closes, fast_period)
    slow = _ema_series(closes, slow_period)
    line: list[float | None] = [
        (fast_value - slow_value) if fast_value is not None and slow_value is not None else None
        for fast_value, slow_value in zip(fast, slow)
    ]
    first_valid = next((index for index, value in enumerate(line) if value is not None), None)
    if first_valid is None:
        return {"status": "INSUFFICIENT_DATA", "required_bars": slow_period + signal_period - 1}
    tail = [value for value in line[first_valid:] if value is not None]
    signal_tail = _ema_series(tail, signal_period)
    signal = signal_tail[-1] if signal_tail else None
    current_line = line[-1]
    if current_line is None or signal is None:
        return {"status": "INSUFFICIENT_DATA", "required_bars": slow_period + signal_period - 1}
    return {
        "status": "AVAILABLE",
        "fast_period": fast_period,
        "slow_period": slow_period,
        "signal_period": signal_period,
        "value": {
            "line": _rounded(current_line),
            "signal": _rounded(signal),
            "histogram": _rounded(current_line - signal),
        },
    }


def _derivative_record(enabled: bool, supplied: Any, *, key: str) -> dict[str, Any]:
    if not enabled:
        return {"status": "DISABLED"}
    if not isinstance(supplied, Mapping):
        return {"status": "UNAVAILABLE", "reason": "NOT_SUPPLIED"}
    if str(supplied.get("status") or "").upper() != "AVAILABLE":
        return {"status": "UNAVAILABLE", "reason": "SOURCE_UNAVAILABLE"}
    if supplied.get("verified") is not True:
        return {"status": "UNAVAILABLE", "reason": "UNVERIFIED_INPUT"}
    source = supplied.get("source")
    as_of = _timestamp(supplied.get("as_of"), name=key)
    if not isinstance(source, str) or not source.strip() or as_of is None:
        return {"status": "UNAVAILABLE", "reason": "PROVENANCE_REQUIRED"}
    try:
        value = _number(supplied.get("value"), name=key, non_negative=key == "open_interest")
    except ValueError:
        return {"status": "UNAVAILABLE", "reason": "INVALID_VALUE"}
    if key == "funding_rate" and abs(value) > 1.0:
        return {"status": "UNAVAILABLE", "reason": "INVALID_VALUE"}
    if len(source.strip()) > 128 or any(ord(char) < 32 for char in source):
        return {"status": "UNAVAILABLE", "reason": "INVALID_PROVENANCE"}
    result = {"status": "AVAILABLE", "value": _rounded(value), "source": source.strip(), "as_of": as_of}
    unit = supplied.get("unit")
    if isinstance(unit, str) and unit.strip() and len(unit.strip()) <= 32 and not any(ord(char) < 32 for char in unit):
        result["unit"] = unit.strip()
    return result


def build_indicator_snapshot(
    bars: Any,
    configuration: Any,
    *,
    source: str,
    timeframe: str,
    as_of: Any = None,
    verified_derivatives: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic snapshot of requested indicators from OHLCV.

    Bars must be ordered oldest-to-newest.  Their OHLCV values are always
    validated, even if every technical indicator switch is disabled.  The
    snapshot contains only latest values (never unbounded indicator series).
    """
    config = normalize_nofx_indicator_config(configuration)
    clean_source = _clean_label(source, name="source")
    clean_timeframe = _clean_label(timeframe, name="timeframe")
    validated = _validated_bars(bars)
    closes = [bar["close"] for bar in validated]
    supplied = verified_derivatives if isinstance(verified_derivatives, Mapping) else {}

    ema_values = {period: (_ema_series(closes, period)[-1] if closes else None) for period in config["ema_periods"]}
    rsi_values = {period: _rsi(closes, period) for period in config["rsi_periods"]}
    atr_values = {period: _atr(validated, period) for period in config["atr_periods"]}

    bollinger: dict[str, Any]
    if not config["enable_boll"]:
        bollinger = {"status": "DISABLED", "periods": {}}
    else:
        periods: dict[str, Any] = {}
        for period in config["boll_periods"]:
            if len(closes) < period:
                periods[str(period)] = {"status": "INSUFFICIENT_DATA", "required_bars": period}
                continue
            window = closes[-period:]
            middle = sum(window) / period
            variance = sum((value - middle) ** 2 for value in window) / period
            deviation = math.sqrt(variance)
            upper, lower = middle + 2.0 * deviation, middle - 2.0 * deviation
            periods[str(period)] = {
                "status": "AVAILABLE",
                "upper": _rounded(upper),
                "middle": _rounded(middle),
                "lower": _rounded(lower),
                "width_pct": _rounded((upper - lower) / middle * 100.0) if middle else 0.0,
            }
        states = {row["status"] for row in periods.values()}
        group_status = "AVAILABLE" if states == {"AVAILABLE"} else "INSUFFICIENT_DATA" if states == {"INSUFFICIENT_DATA"} else "PARTIAL"
        bollinger = {"status": group_status, "periods": periods}

    if not config["enable_volume"]:
        volume_ratio: dict[str, Any] = {"status": "DISABLED"}
    elif len(validated) <= config["volume_lookback"]:
        volume_ratio = {"status": "INSUFFICIENT_DATA", "required_bars": config["volume_lookback"] + 1}
    else:
        current_volume = validated[-1]["volume"]
        prior = [bar["volume"] for bar in validated[-config["volume_lookback"] - 1:-1]]
        average_volume = sum(prior) / len(prior)
        volume_ratio = {
            "status": "AVAILABLE" if average_volume > 0 else "UNAVAILABLE",
            "current_volume": _rounded(current_volume),
            "average_volume": _rounded(average_volume),
            "lookback": config["volume_lookback"],
            **({"ratio": _rounded(current_volume / average_volume)} if average_volume > 0 else {"reason": "ZERO_BASELINE_VOLUME"}),
        }

    latest_time = _timestamp(as_of, name="as_of") if as_of is not None else (validated[-1]["time"] if validated else None)
    derivatives = {
        "open_interest": _derivative_record(config["enable_oi"], supplied.get("open_interest"), key="open_interest"),
        "funding_rate": _derivative_record(config["enable_funding_rate"], supplied.get("funding_rate"), key="funding_rate"),
    }
    return {
        "schema_version": SNAPSHOT_VERSION,
        "source": clean_source,
        "timeframe": clean_timeframe,
        "as_of": latest_time,
        "bar_count": len(validated),
        "indicators": {
            "ema": _period_group(config["enable_ema"], config["ema_periods"], ema_values),
            "macd": _macd(closes) if config["enable_macd"] else {"status": "DISABLED"},
            "rsi": _period_group(config["enable_rsi"], config["rsi_periods"], rsi_values, required_bars_offset=1),
            "atr": _period_group(config["enable_atr"], config["atr_periods"], atr_values, required_bars_offset=1),
            "bollinger": bollinger,
            "volume_ratio": volume_ratio,
        },
        "derivatives": derivatives,
    }


__all__ = [
    "MAX_BARS",
    "MAX_PERIOD",
    "MAX_PERIODS",
    "SNAPSHOT_VERSION",
    "build_indicator_snapshot",
    "normalize_nofx_indicator_config",
]
