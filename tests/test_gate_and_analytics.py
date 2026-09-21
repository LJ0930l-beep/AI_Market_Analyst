from datetime import datetime, timezone
import pytest
from core.storage import SQLiteStore
from core.analysis.ai_trade_analytics import (
    analyze_ai_trading_ledger,
    compute_ai_recommended_leverage,
    INITIAL_SIMULATED_CAPITAL,
)
from core.trading.gate_live_client import (
    GateLiveTrader,
    save_gate_credentials_to_store,
    get_gate_credentials_from_store,
)


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "test_gate.db")
    s.initialize()
    return s


def test_ai_recommended_leverage_bounds():
    # Tight stop -> higher leverage
    lev, reason = compute_ai_recommended_leverage("liquidity_sweep", 0.9, 100.0, 99.7)
    assert 50 <= lev <= 100
    assert "止损" in reason

    # Wide stop -> lower leverage
    lev2, reason2 = compute_ai_recommended_leverage("session_vwap", 0.8, 100.0, 94.0)
    assert 5 <= lev2 <= 20
    assert "降低杠杆" in reason2 or "保守" in reason2


def test_ai_trading_analytics_empty(store):
    analysis = analyze_ai_trading_ledger(store)
    account = analysis["account"]
    assert account["initial_capital_usdt"] == 1000.0
    assert account["current_equity_usdt"] == 1000.0
    assert account["total_trades"] == 0
    assert "style_dna" in analysis
    assert len(analysis["strategy_matrix"]) >= 4
    matrix_ids = {s["strategy_id"] for s in analysis["strategy_matrix"]}
    assert "aggressive_breakout" in matrix_ids
    assert "aggressive_impulse" in matrix_ids
    assert "conservative_pullback" in matrix_ids
    assert "conservative_defense" in matrix_ids


def test_gate_credentials_and_dry_run_trader(store):
    # Credentials save and retrieve
    creds = save_gate_credentials_to_store(store, "test_api_key_12345", "test_secret_67890", live_enabled=False)
    assert creds["configured"] is True
    assert "test" in creds["api_key_masked"] and "2345" in creds["api_key_masked"]

    loaded = get_gate_credentials_from_store(store)
    assert loaded["configured"] is True
    assert loaded["api_key"] == "test_api_key_12345"

    # Dry run order placement
    trader = GateLiveTrader("test_key", "test_secret", live_trading_enabled=False)
    order = trader.place_order(
        symbol="BTCUSDT",
        side="LONG",
        amount=1.5,
        price=68000.0,
        leverage=75,
    )
    assert order["status"] == "DRY_RUN_ACKNOWLEDGED"
    assert order["dry_run"] is True
    assert order["leverage"] == 75
