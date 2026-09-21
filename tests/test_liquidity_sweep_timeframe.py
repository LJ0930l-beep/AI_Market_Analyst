from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from core.providers.base import Bar
from core.quant.strategies import LiquiditySweep


def _bar(timestamp: datetime, *, timeframe_minutes: int, open_: float = 100.0, high: float = 101.0,
         low: float = 99.5, close: float = 100.0, is_closed: bool | None = True,
         bar_end: datetime | None = None) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        bar_end=bar_end or timestamp + timedelta(minutes=timeframe_minutes),
        is_closed=is_closed,
    )


def _valid_sweep_case(timeframe: str) -> tuple[datetime, list[Bar], list[Bar]]:
    minutes = 5 if timeframe == "5m" else 15
    step = timedelta(minutes=minutes)
    end = datetime(2026, 9, 21, 12, 30, tzinfo=UTC)
    signal_bars = [
        _bar(end - step * (64 - index), timeframe_minutes=minutes)
        for index in range(64)
    ]
    # The final two bars form a bearish-to-bullish engulfing pair. The bullish
    # signal bar also sweeps the preceding range low and closes back above it.
    signal_bars[-2] = _bar(
        signal_bars[-2].timestamp,
        timeframe_minutes=minutes,
        open_=100.4,
        high=101.0,
        low=99.5,
        close=100.0,
    )
    signal_bars[-1] = _bar(
        signal_bars[-1].timestamp,
        timeframe_minutes=minutes,
        open_=99.9,
        high=101.0,
        low=98.9,
        close=100.5,
    )

    if timeframe == "5m":
        small_bars = signal_bars
    else:
        small_bars = [
            _bar(end - timedelta(minutes=15), timeframe_minutes=5),
            _bar(
                end - timedelta(minutes=10),
                timeframe_minutes=5,
                open_=100.4,
                high=101.0,
                low=99.5,
                close=100.0,
            ),
            _bar(
                end - timedelta(minutes=5),
                timeframe_minutes=5,
                open_=99.9,
                high=101.0,
                low=98.9,
                close=100.5,
            ),
        ]
    return end, signal_bars, small_bars


@pytest.mark.parametrize("timeframe", ["5m", "15m"])
def test_liquidity_sweep_uses_effective_signal_interval(timeframe: str) -> None:
    now, bars, lower_bars = _valid_sweep_case(timeframe)
    strategy = LiquiditySweep()
    strategy.signal_timeframe = timeframe

    proposal = strategy.evaluate(
        "BTCUSDT",
        bars,
        now=now,
        context={
            "timeframe": timeframe,
            "signal_timeframe": timeframe,
            "market_type": "crypto",
            "closed_5m": lower_bars,
        },
    )

    assert proposal is not None, strategy.last_reason
    assert proposal.side == "LONG"
    assert proposal.signal_timeframe == timeframe
    assert proposal.source_bar_at == bars[-1].timestamp.isoformat()
    assert datetime.fromisoformat(proposal.expires_at) == now + timedelta(minutes=5 if timeframe == "5m" else 15)
    assert proposal.stop < proposal.entry < proposal.targets[0]


@pytest.mark.parametrize("invalid_final_bar", ["open", "future_end"])
def test_open_or_future_lower_timeframe_confirmation_is_rejected(invalid_final_bar: str) -> None:
    now, bars, lower_bars = _valid_sweep_case("15m")
    last = lower_bars[-1]
    if invalid_final_bar == "open":
        lower_bars[-1] = replace(last, is_closed=False)
    else:
        lower_bars[-1] = replace(last, bar_end=now + timedelta(minutes=5))

    strategy = LiquiditySweep()
    strategy.signal_timeframe = "15m"
    proposal = strategy.evaluate(
        "BTCUSDT",
        bars,
        now=now,
        context={
            "timeframe": "15m",
            "signal_timeframe": "15m",
            "market_type": "crypto",
            "closed_5m": lower_bars,
        },
    )

    assert proposal is None
    assert strategy.last_status == "NO_TRIGGER"


@pytest.mark.parametrize("invalid_signal_bar", ["open", "future_end"])
def test_open_or_future_signal_bar_cannot_trigger_sweep(invalid_signal_bar: str) -> None:
    now, bars, lower_bars = _valid_sweep_case("5m")
    last = bars[-1]
    if invalid_signal_bar == "open":
        bars[-1] = replace(last, is_closed=False)
        lower_bars[-1] = bars[-1]
    else:
        bars[-1] = replace(last, bar_end=now + timedelta(minutes=5))
        lower_bars[-1] = bars[-1]

    strategy = LiquiditySweep()
    strategy.signal_timeframe = "5m"
    proposal = strategy.evaluate(
        "BTCUSDT",
        bars,
        now=now,
        context={
            "timeframe": "5m",
            "signal_timeframe": "5m",
            "market_type": "crypto",
            "closed_5m": lower_bars,
        },
    )

    assert proposal is None
