"""Deterministic technical calculations owned by Python, never by an LLM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from ..providers.base import Bar


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"need at least {period} values")
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    for current in values[period:]:
        value = (current - value) * alpha + value
    return value


def _ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        raise ValueError(f"need at least {period} values")
    value = sum(values[:period]) / period
    result = [value]
    alpha = 2.0 / (period + 1)
    for current in values[period:]:
        value = (current - value) * alpha + value
        result.append(value)
    return result


def _rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        raise ValueError(f"need at least {period + 1} values")
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise ValueError(f"need at least {period + 1} bars")
    true_ranges: list[float] = []
    previous_close = bars[0].close
    for bar in bars[1:]:
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))
        previous_close = bar.close
    return sum(true_ranges[-period:]) / period


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class QuantSnapshot:
    symbol: str
    timeframe: str
    timestamp: datetime
    price: float
    ema20: float
    ema50: float
    rsi14: float
    macd: float
    macd_signal: float
    atr14: float
    volume_ratio: float
    support: float
    resistance: float
    market_regime: str
    trend_score: float
    momentum_score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp.isoformat(),
            "price": self.price,
            "ema20": self.ema20,
            "ema50": self.ema50,
            "rsi14": self.rsi14,
            "macd": self.macd,
            "macd_signal": self.macd_signal,
            "atr14": self.atr14,
            "volume_ratio": self.volume_ratio,
            "support": self.support,
            "resistance": self.resistance,
            "market_regime": self.market_regime,
            "trend_score": self.trend_score,
            "momentum_score": self.momentum_score,
        }


def build_quant_snapshot(bars: list[Bar], timeframe: str = "1h", symbol: str = "") -> QuantSnapshot:
    """Compute the compact, replayable quantitative context."""

    if len(bars) < 60:
        raise ValueError("at least 60 bars are required for the Phase 1 quant engine")
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    closes = [bar.close for bar in ordered]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    # Align the two EMA series at the end of the common suffix.
    macd_series = [fast[-len(slow) + i] - slow[i] for i in range(len(slow))]
    macd = macd_series[-1]
    macd_signal = _ema(macd_series, min(9, len(macd_series)))
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(ordered, 14)
    recent = ordered[-20:]
    support = min(bar.low for bar in recent)
    resistance = max(bar.high for bar in recent)
    volume_base = sum(bar.volume for bar in ordered[-20:]) / 20.0
    volume_recent = sum(bar.volume for bar in ordered[-5:]) / 5.0
    volume_ratio = volume_recent / volume_base if volume_base else 1.0
    trend_score = _clamp((ema20 / ema50 - 1.0) * 25.0)
    momentum_score = _clamp((closes[-1] / closes[-11] - 1.0) * 10.0)
    if ema20 > ema50 * 1.002 and trend_score > 0.05:
        regime = "bull_trend"
    elif ema20 < ema50 * 0.998 and trend_score < -0.05:
        regime = "bear_trend"
    else:
        regime = "range"
    values = (ema20, ema50, rsi14, macd, macd_signal, atr14, volume_ratio, support, resistance)
    if not all(isfinite(value) for value in values):
        raise ValueError("quant engine produced a non-finite value")
    return QuantSnapshot(
        symbol=symbol.strip().upper(),
        timeframe=timeframe,
        timestamp=ordered[-1].timestamp,
        price=closes[-1],
        ema20=ema20,
        ema50=ema50,
        rsi14=rsi14,
        macd=macd,
        macd_signal=macd_signal,
        atr14=atr14,
        volume_ratio=volume_ratio,
        support=support,
        resistance=resistance,
        market_regime=regime,
        trend_score=trend_score,
        momentum_score=momentum_score,
    )
