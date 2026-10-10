from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def market_point_factory():
    durations = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
    decision_time = datetime(2025, 10, 15, 12, 0, tzinfo=UTC)

    def make_point(*, decision=None, **extra):
        decision = decision or decision_time
        bars_by_timeframe = {}
        for timeframe, minutes in durations.items():
            step = timedelta(minutes=minutes)
            last_end = decision - step
            rows = []
            for index in range(40):
                bar_end = last_end - step * (39 - index)
                center = 100 + index * 0.02
                rows.append({
                    "timeframe": timeframe,
                    "bar_start": (bar_end - step).isoformat(),
                    "bar_end": bar_end.isoformat(),
                    "available_at": (bar_end + timedelta(seconds=1)).isoformat(),
                    "is_closed": True,
                    "quality_status": "FROZEN_RESEARCH",
                    "source": "fixture:deterministic",
                    "price_evidence_grade": "NOT_VERIFIED_FIXTURE",
                    "available_at_evidence_grade": "NOT_VERIFIED_FIXTURE",
                    "open": center - 0.1,
                    "high": center + 0.5,
                    "low": center - 0.5,
                    "close": center + (0.1 if index % 2 == 0 else -0.1),
                    "volume": 10 + index,
                })
            bars_by_timeframe[timeframe] = rows
        point = {
            "decision_id": "v38-fixture-btc-20251015-1200z",
            "decision_time": decision.isoformat(),
            "symbol": "BTCUSDT",
            "partition": "optimization",
            "bars_by_timeframe": bars_by_timeframe,
        }
        point.update(extra)
        return point

    return make_point
