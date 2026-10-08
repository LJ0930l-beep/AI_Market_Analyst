"""Causal breakout facts from isolated closed-bar fixtures, without model or venue calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.trading.autonomous_strategy import (
    build_nofx_gate_system_prompt,
    compact_technical,
    technical_context,
)


NOW = datetime(2026, 9, 27, 0, 20, tzinfo=timezone.utc)
SYMBOL = "BTCUSDT"


def closed_bars(count=32):
    rows = []
    for index in reversed(range(count)):
        end = NOW - timedelta(minutes=5 * index)
        rows.append({
            "bar_start": (end - timedelta(minutes=5)).isoformat(),
            "bar_end": end.isoformat(), "available_at": end.isoformat(),
            "open": 84750.0, "high": 84876.0, "low": 84720.0, "close": 84800.0,
            "volume": 10.0, "is_closed": True, "synthetic": False, "quality_status": "VALID",
        })
    return rows


def frame_for(rows):
    store = SimpleNamespace(list_market_bars=lambda *_args, **_kwargs: rows)
    return technical_context(store, (SYMBOL,), NOW, interval=5, timeframes=("5m",))[SYMBOL]["timeframes"]["5m"]


def breakout_rows():
    rows = closed_bars()
    # A high older than the reference must not become the new trigger threshold.
    rows[0]["high"] = 90000.0
    rows[-1].update({
        "open": 84850.0, "high": 84916.5, "low": 84830.0, "close": 84880.0, "volume": 36.965,
    })
    return rows


def test_close_breaks_previous_high_even_when_trigger_bar_sets_higher_range_high():
    frame = frame_for(breakout_rows())
    fact = frame["closed_bar_breakout"]
    assert frame["status"] == "READY"
    assert frame["indicators"]["resistance20"] == 84916.5
    assert frame["indicators"]["support20"] == 84720.0
    assert frame["indicators"]["volume_ratio20"] == pytest.approx(3.6965)
    assert fact == {
        "status": "READY", "lookback": 20, "n": 20, "as_of": NOW.isoformat(),
        "bar_end": NOW.isoformat(), "close": 84880.0,
        "ref_end": (NOW - timedelta(minutes=5)).isoformat(),
        "prev_high": 84876.0, "prev_low": 84720.0, "up": True, "down": False,
    }
    assert fact["close"] < frame["indicators"]["resistance20"]


@pytest.mark.parametrize("close", [84875.0, 84876.0])
def test_high_wick_or_equal_close_does_not_count_as_closed_upward_breakout(close):
    rows = breakout_rows()
    rows[-1]["close"] = close
    fact = frame_for(rows)["closed_bar_breakout"]
    assert fact["status"] == "READY"
    assert fact["prev_high"] == 84876.0
    assert fact["up"] is False
    assert fact["down"] is False


@pytest.mark.parametrize("close, expected", [(84710.0, True), (84720.0, False), (84721.0, False)])
def test_closed_downward_breakout_uses_previous_low_and_strict_comparison(close, expected):
    rows = closed_bars()
    rows[-1].update({"open": 84750.0, "high": 84780.0, "low": 84680.0, "close": close})
    frame = frame_for(rows)
    fact = frame["closed_bar_breakout"]
    assert frame["indicators"]["support20"] == 84680.0
    assert fact["prev_low"] == 84720.0
    assert fact["down"] is expected
    assert fact["up"] is False


@pytest.mark.parametrize("unusable", ["future", "unclosed", "unavailable"])
def test_breakout_reference_cannot_see_future_unclosed_or_unavailable_bars(unusable):
    rows = breakout_rows()
    expected = frame_for(rows)
    injected = deepcopy(rows[-2])
    injected.update({"open": 95000.0, "high": 100000.0, "low": 90000.0, "close": 99000.0})
    if unusable == "future":
        injected.update({
            "bar_start": NOW.isoformat(), "bar_end": (NOW + timedelta(minutes=5)).isoformat(),
            "available_at": NOW.isoformat(),
        })
    elif unusable == "unclosed":
        injected["is_closed"] = False
    else:
        injected["available_at"] = (NOW + timedelta(seconds=1)).isoformat()
    actual = frame_for([*rows, injected])
    assert actual["bar_count"] == expected["bar_count"]
    assert actual["closed_bar_breakout"] == expected["closed_bar_breakout"]
    assert actual["bars"] == expected["bars"]


@pytest.mark.parametrize("count", [0, 1, 20])
def test_insufficient_prior_samples_are_unknown_instead_of_false(count):
    fact = frame_for(closed_bars(count))["closed_bar_breakout"]
    assert fact["status"] == "INSUFFICIENT"
    assert fact["n"] == max(count - 1, 0)
    assert fact["prev_high"] is None and fact["prev_low"] is None
    assert fact["up"] is None and fact["down"] is None


def test_exactly_twenty_prior_bars_suffice_for_reference_without_changing_frame_readiness():
    frame = frame_for(closed_bars(21))
    assert frame["status"] == "INSUFFICIENT_OR_STALE"
    assert frame["closed_bar_breakout"]["status"] == "READY"
    assert frame["closed_bar_breakout"]["n"] == 20


def test_gapped_reference_is_unknown_instead_of_asserting_a_twenty_bar_breakout():
    rows = breakout_rows()
    rows.pop(-10)
    fact = frame_for(rows)["closed_bar_breakout"]
    assert fact["status"] == "UNKNOWN" and fact["n"] == 20
    assert fact["prev_high"] is None and fact["prev_low"] is None
    assert fact["up"] is None and fact["down"] is None


def test_stale_trigger_is_unknown():
    rows = breakout_rows()[:-2]
    fact = frame_for(rows)["closed_bar_breakout"]
    assert fact["status"] == "UNKNOWN"
    assert fact["bar_end"] == (NOW - timedelta(minutes=10)).isoformat()
    assert fact["up"] is None and fact["down"] is None


def test_compact_input_preserves_causal_facts_and_reuses_existing_observation_fields():
    frame = frame_for(breakout_rows())
    raw = {SYMBOL: {"status": "READY", "timeframes": {"5m": frame}}}
    compact = compact_technical(raw, signal_timeframe="5m")[SYMBOL]["timeframes"]["5m"]
    assert compact["closed_bar_breakout"] == {
        "status": "READY", "n": 20, "ref_end": (NOW - timedelta(minutes=5)).isoformat(),
        "prev_high": 84876.0, "prev_low": 84720.0, "up": True, "down": False,
    }
    assert compact["candles"][-1][3] == frame["closed_bar_breakout"]["close"]
    assert compact["last_closed_at"] == frame["closed_bar_breakout"]["bar_end"]


def test_gate_prompt_describes_reference_without_making_breakout_an_entry_requirement():
    prompt = build_nofx_gate_system_prompt({})
    assert "support20/resistance20 含末根" in prompt
    assert "此前20根（排除末根）" in prompt
    assert "up/down 是末根收盘严格突破比较" in prompt
    assert "非READY未知，仅供自判，不是必备开仓条件" in prompt
