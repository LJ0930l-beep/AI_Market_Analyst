"""Deterministic regressions for the 2026-09 institutional audit.

These tests intentionally assert the business invariants from the audit
contract, rather than preserving the values emitted by the old reproduction
script.  They are kept separate from the historical AT/RT suites so a future
compatibility change cannot silently erase an audit regression.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core.analysis.strategy_evaluator import TradeOutcome, evaluate_strategy_effectiveness, run_counterfactual_comparison
from core.analysis.strategy_replay import run_strategy_bar_replay
from core.events import _is_primary_source, source_credibility
from core.instruments import canonical_instrument_key, instrument_for
from core.news_engine import _classify
from core.providers.base import Bar, Quote
from core.quant.strategies import BaseStrategy, EMATrend
from core.security.local_guard import is_allowed_origin
from core.storage import SQLiteStore
from core.strategy_monitoring import StrategyMonitoringService


UTC = timezone.utc
START = datetime(2026, 9, 1, tzinfo=UTC)


class _LowPriceSignal(BaseStrategy):
    strategy_id = "low_price_regression"
    signal_timeframe = "15m"
    warmup_bars = 2

    def match(self, bars, context):
        return "LONG", 0.00000090, "fixture low-price signal"


def _bars(count: int = 64, *, scale: float = 1.0, timeframe_minutes: int = 15) -> list[Bar]:
    return [
        Bar(
            START + timedelta(minutes=timeframe_minutes * index),
            (100.0 + index * 0.1) * scale,
            (100.1 + index * 0.1) * scale,
            (99.9 + index * 0.1) * scale,
            (100.0 + index * 0.1) * scale,
            100.0,
        )
        for index in range(count)
    ]


def _stored_rows(bars: list[Bar]) -> list[dict[str, object]]:
    return [
        {
            "timestamp": item.timestamp.isoformat(),
            "bar_end": (item.timestamp + timedelta(minutes=15)).isoformat(),
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
            "volume": item.volume,
            "data_as_of": (item.timestamp + timedelta(minutes=15)).isoformat(),
            "available_at": (item.timestamp + timedelta(minutes=15)).isoformat(),
            "is_closed": True,
            "provider": "deterministic-fixture",
        }
        for item in bars
    ]


class _GateStrategyFixture:
    provider_name = "gate_public_swap"

    def __init__(self):
        self.bar_requests = []

    def market(self, symbol: str) -> dict[str, object]:
        return {
            "id": f"{symbol[:3]}_USDT",
            "symbol": f"{symbol[:3]}/USDT:USDT",
            "swap": True,
            "type": "swap",
            "settle": "USDT",
        }

    def get_quote(self, instrument):
        return Quote(instrument, datetime.now(UTC), 100.0, 1_000.0, 0.0)

    def get_bars(self, _instrument, timeframe: str, limit: int = 240) -> list[Bar]:
        self.bar_requests.append((timeframe, limit))
        minutes = {"5m": 5, "15m": 15, "1h": 60}[timeframe]
        return [
            Bar(
                START + timedelta(minutes=minutes * index),
                100.0,
                100.2,
                99.8,
                100.0,
                100.0,
            )
            for index in range(limit)
        ]


def test_strategy_monitoring_persists_gate_native_identity_without_degrading(tmp_path):
    store = SQLiteStore(tmp_path / "strategy-monitoring.db")
    store.initialize()
    store.save_instrument(instrument_for("BTCUSDT"))
    store.upsert_watchlist_entry("BTCUSDT")
    store.set_strategy_subscription("BTCUSDT", "ema_trend", True, {})

    service = StrategyMonitoringService(store=store, llm_provider=None)
    service.gate = _GateStrategyFixture()
    result = service.run(symbols=("BTCUSDT",), now=START + timedelta(days=1))

    assert result.status == "COMPLETED"
    assert result.items and result.items[0]["status"] != "DEGRADED"
    rows = store.range_bars("BTCUSDT", "15m", limit=1)
    assert rows[0]["instrument_key"] == canonical_instrument_key(
        "gate", "perpetual", "BTC_USDT", "USDT", "last"
    )


def test_ai_refresh_replaces_the_complete_candidate_window(tmp_path):
    store = SQLiteStore(tmp_path / "ai-refresh-window.db")
    store.initialize()
    store.save_instrument(instrument_for("BTCUSDT"))
    fixture = _GateStrategyFixture()
    service = StrategyMonitoringService(store=store, llm_provider=None)
    service.gate = fixture

    result = service.run(
        symbols=("BTCUSDT",),
        now=START + timedelta(days=7),
        analysis_only=True,
        ai_interval=5,
    )

    assert result.status == "COMPLETED"
    assert set(fixture.bar_requests) == {("5m", 240), ("15m", 240), ("1h", 240)}


def test_low_price_quantization_is_positive_and_revalidated():
    proposal = _LowPriceSignal().evaluate(
        "PEPEUSDT",
        _bars(4, scale=1e-8),
        now=START + timedelta(minutes=15 * 4),
        context={"tick_size": "0.00000001", "step_size": "1"},
    )
    assert proposal is not None
    assert proposal.entry > 0
    assert proposal.stop > 0
    assert all(target > 0 for target in proposal.targets)


def test_latest_bars_limit_one_returns_newest_and_cross_venue_identity_is_kept(tmp_path):
    store = SQLiteStore(tmp_path / "identity.db")
    store.initialize()
    bars = _bars(2)
    store.upsert_market_bars(
        "BTCUSDT",
        "15m",
        [bars[0]],
        provider="gate-public-spot",
        data_as_of=START,
        now=START + timedelta(days=1),
        instrument_key="gate:spot:BTCUSDT:USDT:last",
        market_type="spot",
        native_symbol="BTCUSDT",
        settle_currency="USDT",
        price_type="last",
    )
    store.upsert_market_bars(
        "BTCUSDT",
        "15m",
        [bars[0]],
        provider="gate-public-perp",
        data_as_of=START,
        now=START + timedelta(days=1),
        instrument_key="gate:perpetual:BTCUSDT:USDT:last",
        market_type="perpetual",
        native_symbol="BTCUSDT",
        settle_currency="USDT",
        price_type="last",
    )
    store.upsert_market_bars("BTCUSDT", "15m", [bars[1]], provider="gate-public-spot", data_as_of=START, now=START + timedelta(days=1))

    latest = store.list_market_bars("BTCUSDT", "15m", limit=1)
    assert latest[0]["bar_start"] == bars[1].timestamp.isoformat()
    identity_rows = store.range_bars("BTCUSDT", "15m", limit=20)
    assert {row["instrument_key"] for row in identity_rows} >= {
        "gate:spot:BTCUSDT:USDT:last",
        "gate:perpetual:BTCUSDT:USDT:last",
    }


def test_replay_missing_required_context_is_not_zero_trade_evaluated():
    result = run_strategy_bar_replay(
        symbol="BTCUSDT",
        timeframe="15m",
        strategy_id="funding_extreme",
        bars=_stored_rows(_bars()),
    )
    assert result["status"] in {"NOT_RUN_MISSING_CONTEXT", "SIGNAL_RESEARCH_ONLY"}
    assert result.get("trades") != []


def test_partial_exit_with_remaining_quantity_is_not_closed_trade():
    result = evaluate_strategy_effectiveness(
        [
            {"position_id": "open-position", "quantity": 10, "price": 100, "fee": 1, "stop": 90},
            {"position_id": "open-position", "quantity": 5, "price": 110, "fee": 1, "is_exit": True},
        ],
        min_samples=1,
    )
    assert result["total_closed_trades"] == 0


def test_short_mae_uses_high_and_mfe_uses_low():
    result = evaluate_strategy_effectiveness(
        [
            TradeOutcome(
                "short-1",
                "ema_trend",
                "BTCUSDT",
                "SHORT",
                100,
                110,
                80,
                [{"price": 90, "quantity": 1}],
                "2026-09-01T00:00:00+00:00",
                "2026-09-02T00:00:00+00:00",
                1,
                0,
                highest_price=105,
                lowest_price=80,
            )
        ],
        min_samples=1,
    )
    assert result["avg_mae_pct"] == pytest.approx(5.0)
    assert result["avg_mfe_pct"] == pytest.approx(20.0)


def test_counterfactual_stress_recomputes_costs_and_unreviewed_is_not_pass():
    result = run_counterfactual_comparison(
        baseline_trades=[{"trade_id": "loss", "pnl": -100.0, "fee": 10.0, "notional": 1000.0}],
        ai_reviews=[],
    )
    assert result["branches"]["AI_FILTERED"]["trade_count"] == 0
    assert result["stress_tests"]["double_fees"]["strategy_baseline_pnl"] == pytest.approx(-110.0)


def test_source_registry_does_not_trust_path_or_source_text():
    spoofed_url = "https://evil.invalid/sec.gov/fake"
    assert _is_primary_source("official sec.gov", spoofed_url) is False
    assert source_credibility("official sec.gov", primary_source=False, reported=0) < 95


def test_local_origin_is_exact_not_prefix():
    assert is_allowed_origin("http://localhost.evil.invalid") is False
    assert is_allowed_origin("http://localhost:5173") is True


def test_news_negation_changes_direction():
    category, sentiment, _importance, _credibility, _horizon = _classify("Profit did not beat expectations", "")
    assert category == "earnings"
    assert sentiment < 0


def test_ema_rejects_overbought_long_signal():
    closes = [100 + index * 0.1 for index in range(60)] + [104, 108]
    bars = [
        Bar(
            START + timedelta(minutes=15 * index),
            close,
            close + 0.1,
            close - 0.1,
            close,
            200 if index == 61 else 100,
        )
        for index, close in enumerate(closes)
    ]
    strategy = EMATrend()
    proposal = strategy.evaluate("BTCUSDT", bars, now=START + timedelta(minutes=15 * 62))
    assert proposal is None
    assert strategy.last_status == "NO_TRIGGER"
