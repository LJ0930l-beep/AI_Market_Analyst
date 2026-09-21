from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.providers.base import Bar
from core.quant.strategies import FundingExtreme
from core.storage import SQLiteStore
from core.trading.candidate_scanner import CandidateScanner


def _bar_rows(end: datetime, timeframe: str, count: int = 80) -> list[dict[str, object]]:
    minutes = 5 if timeframe == "5m" else 15
    step = timedelta(minutes=minutes)
    rows: list[dict[str, object]] = []
    for index in range(count):
        bar_end = end - step * (count - index - 1)
        row: dict[str, object] = {
            "bar_start": (bar_end - step).isoformat(),
            "bar_end": bar_end.isoformat(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.0,
            "volume": 100.0,
            "is_closed": True,
            "available_at": bar_end.isoformat(),
            "data_as_of": bar_end.isoformat(),
            "quality_status": "VALID",
            "provider": "gate_native_rest",
        }
        rows.append(row)

    # The last two candles form a bearish-to-bullish engulfing pair. The
    # final bullish bar wicks through the prior range low then reclaims it.
    rows[-2].update({"open": 100.4, "high": 101.0, "low": 99.5, "close": 100.0})
    rows[-1].update({"open": 99.9, "high": 101.0, "low": 98.9, "close": 100.5})
    return rows


def test_ai_profile_strategies_are_not_pruned_by_single_monitoring_subscription(tmp_path):
    store = SQLiteStore(tmp_path / "candidate-subscriptions.db")
    store.initialize()
    scanner = CandidateScanner(store)
    scanner.store.list_strategy_subscriptions = lambda _enabled_only=False: [
        {"symbol": "BTCUSDT", "strategy_id": "ema_trend", "params": {"custom": True}}
    ]

    subscriptions = scanner._subscriptions(
        ("BTCUSDT",), ("liquidity_sweep", "ema_trend", "bollinger_squeeze")
    )

    assert set(subscriptions) == {
        ("BTCUSDT", "liquidity_sweep"),
        ("BTCUSDT", "ema_trend"),
        ("BTCUSDT", "bollinger_squeeze"),
    }
    assert subscriptions[("BTCUSDT", "ema_trend")]["params"] == {"custom": True}
    assert subscriptions[("BTCUSDT", "liquidity_sweep")]["params"] == {}


@pytest.mark.parametrize("timeframe", ["5m", "15m"])
def test_liquidity_sweep_candidate_uses_configured_signal_and_closed_5m_context(
    tmp_path, timeframe
):
    now = datetime(2026, 9, 21, 12, 30, tzinfo=UTC)
    store = SQLiteStore(tmp_path / f"sweep-{timeframe}.db")
    store.initialize()
    scanner = CandidateScanner(store)
    reads: list[str] = []

    def bars(_symbol: str, requested: str, *, now: datetime, limit: int):
        reads.append(requested)
        return _bar_rows(now, requested)[-limit:]

    scanner._bars = bars  # type: ignore[method-assign]
    scanner.has_pending_candidate = lambda _account, _candidate: False  # type: ignore[method-assign]

    candidate = scanner.scan(
        account_id="gate_testnet",
        provider="gate",
        environment="testnet",
        symbols=("BTCUSDT",),
        now=now,
        strategy_ids=("liquidity_sweep",),
        signal_timeframe_override=timeframe,
    )[0]

    assert candidate["status"] == "PROPOSAL", candidate["reason"]
    assert candidate["proposal"]["signal_timeframe"] == timeframe
    assert candidate["context_timeframe"]["signal"] == timeframe
    assert candidate["proposal"]["side"] == "LONG"
    assert candidate["proposal"]["expires_at"] == (now + timedelta(minutes=5 if timeframe == "5m" else 15)).isoformat()
    # A 5m signal is also the required 5m evidence; for a 15m signal the
    # scanner must fetch its separate lower-timeframe confirmation stream.
    assert reads.count("5m") == 1
    if timeframe == "15m":
        assert reads.count("15m") == 1


def test_funding_extreme_5m_does_not_consume_derivatives_after_signal_close():
    end = datetime(2026, 9, 21, 12, 30, tzinfo=UTC)
    step = timedelta(minutes=5)
    bars = [
        Bar(
            end - step * (60 - index),
            100.0,
            100.2,
            99.8,
            100.0,
            100.0,
            bar_end=end - step * (59 - index),
            is_closed=True,
            available_at=end,
        )
        for index in range(60)
    ]
    rates = [
        {
            "timestamp": int((end - timedelta(hours=30 - index)).timestamp() * 1000),
            "fundingRate": 0.0001,
            "unit": "decimal_fraction",
        }
        for index in range(24)
    ]
    # This extreme report is deliberately newer than the closed 5m decision
    # bar. A legacy hard-coded 15m cutoff would include it and create a signal.
    rates.append({
        "timestamp": int((end + timedelta(minutes=10)).timestamp() * 1000),
        "fundingRate": 0.001,
        "unit": "decimal_fraction",
    })
    oi = [
        {"timestamp": int((end - timedelta(hours=2)).timestamp() * 1000), "openInterestAmount": 1000, "unit": "contracts"},
        {"timestamp": int((end - timedelta(hours=1)).timestamp() * 1000), "openInterestAmount": 1020, "unit": "contracts"},
        {"timestamp": int((end + timedelta(minutes=10)).timestamp() * 1000), "openInterestAmount": 1080, "unit": "contracts"},
    ]

    strategy = FundingExtreme()
    strategy.signal_timeframe = "5m"
    proposal = strategy.evaluate(
        "BTCUSDT",
        bars,
        now=end,
        context={
            "timeframe": "5m",
            "signal_timeframe": "5m",
            "market_type": "crypto",
            "funding_history": rates,
            "oi_history": oi,
        },
    )

    assert proposal is None
    assert strategy.last_status == "NO_TRIGGER"
