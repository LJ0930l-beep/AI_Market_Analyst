from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def research_fixture():
    decision = datetime(2025, 1, 5, 11, 55, tzinfo=UTC)
    minutes = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}

    def make_bars(timeframe, *, count=40, last_end=None, base=100.0, inject_pivot=False):
        step = timedelta(minutes=minutes[timeframe])
        end = last_end or decision - timedelta(minutes=1)
        rows = []
        for index in range(count):
            bar_end = end - step * (count - index - 1)
            center = base + index * 0.02 + (0.3 if index % 4 == 1 else -0.15 if index % 4 == 3 else 0.0)
            high = center + 0.5
            low = center - 0.5
            if inject_pivot and index == count - 5:
                high = center + 8.0
            if inject_pivot and index == count - 9:
                low = center - 8.0
            opening = center - 0.1
            close = center + 0.1 if index % 2 == 0 else center - 0.1
            rows.append({
                "timeframe": timeframe,
                "bar_start": (bar_end - step).isoformat(),
                "bar_end": bar_end.isoformat(),
                "available_at": (bar_end + timedelta(seconds=1)).isoformat(),
                "is_closed": True,
                "quality_status": "FROZEN_RESEARCH",
                "source": "fixture:deterministic",
                "open": opening, "high": high, "low": low, "close": close,
                "volume": 10.0 + index,
            })
        return rows

    def frames(*, count=40, last_end=None, inject_pivot=False):
        return {tf: make_bars(tf, count=count, last_end=last_end, inject_pivot=inject_pivot)
                for tf in ("15m", "5m", "1h", "4h")}

    def point(*, decision_time=None, input_bars=None, state=None):
        return {
            "decision_id": "fixture-decision-1",
            "decision_time": (decision_time or decision).isoformat(),
            "bars_by_timeframe": input_bars or frames(),
            "state_snapshot": state or {
                "equity_usdt": "10000", "available_margin_usdt": "9000",
                "allowed_instruments": ["ETH_USDT"], "positions": [], "working_orders": [],
            },
        }

    def risk_inputs(**overrides):
        values = {
            "equity": "10000", "risk_per_trade_pct": "0.25", "min_net_rr": "2.0",
            "taker_fee_rate": "0.00075", "slippage_rate": "0.0002", "contract_size": "1",
            "amount_step": "0.01", "price_tick": "0.1", "quote": "100",
            "available_margin": "9000", "max_margin_pct": "100", "min_amount": "0.01",
            "max_amount": "1000", "min_notional": "1", "max_notional": "100000",
            "max_leverage": "50", "fixed_notional_usdt": "2000",
        }
        values.update(overrides)
        return values

    def failed_breakout_frames():
        end_15 = datetime(2025, 1, 5, 11, 45, tzinfo=UTC)
        end_5 = datetime(2025, 1, 5, 11, 50, tzinfo=UTC)
        bars_15 = make_bars("15m", count=40, last_end=end_15, base=100.0)
        for row in bars_15:
            row.update(open=100.0, high=105.0, low=95.0, close=100.0)
        bars_5 = make_bars("5m", count=51, last_end=end_5, base=100.0)
        for row in bars_5:
            row.update(open=100.0, high=101.0, low=99.0, close=100.0)
        bars_5[-2].update(open=104.0, high=107.0, low=103.0, close=106.0)
        bars_5[-1].update(open=106.0, high=106.5, low=103.0, close=104.0)
        result = {"15m": bars_15, "5m": bars_5}
        result["1h"] = make_bars("1h", count=40, last_end=end_5)
        result["4h"] = make_bars("4h", count=40, last_end=end_5)
        return result

    return {"decision_time": decision, "make_bars": make_bars, "frames": frames,
            "point": point, "risk_inputs": risk_inputs,
            "failed_breakout_frames": failed_breakout_frames}
