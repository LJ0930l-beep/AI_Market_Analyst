"""Performance feedback must be honest, bounded and safe to serialise."""
import json
import math

import pytest

from core.analysis.ai_trade_analytics import (
    PERFORMANCE_HIGH_DRAWDOWN_PCT,
    PERFORMANCE_MIN_CLOSED_TRADES,
    PERFORMANCE_STRONG_PROFIT_FACTOR,
    PERFORMANCE_WEAK_PROFIT_FACTOR,
    _performance_stance,
    build_performance_context,
)
from core.storage import SQLiteStore
from core.trading.ai_led_engine import AICycleContext


def test_context_dataclass_carries_the_field():
    context = AICycleContext(
        cycle_id="c1", account_id="a1", generation=1, started_at="t", expires_at="t",
        allowed_instruments=(), mode="PAPER", venue="gate", session_id="s1",
    )
    assert context.performance_context == {}


def test_small_sample_is_reported_as_insufficient():
    stance, guidance = _performance_stance(
        PERFORMANCE_MIN_CLOSED_TRADES - 1, 100.0, 9.99, 0.0
    )
    assert stance == "INSUFFICIENT_SAMPLE"
    assert "样本不足" in guidance


def test_strong_result_is_performing_but_still_forbids_widening_risk():
    stance, guidance = _performance_stance(
        PERFORMANCE_MIN_CLOSED_TRADES,
        win_rate_pct=60.0,
        profit_factor=PERFORMANCE_STRONG_PROFIT_FACTOR + 0.2,
        max_drawdown_pct=1.0,
    )
    assert stance == "PERFORMING"
    assert "不要因为表现好就放宽止损" in guidance


def test_mid_band_result_stays_neutral_and_discourages_over_reaction():
    stance, guidance = _performance_stance(
        PERFORMANCE_MIN_CLOSED_TRADES, win_rate_pct=40.0, profit_factor=1.1,
        max_drawdown_pct=2.0,
    )
    assert stance == "NEUTRAL"
    assert "不要因为一两笔亏损就放弃仍然合格的信号" in guidance


def test_weak_result_raises_the_bar_and_blocks_revenge_trading():
    stance, guidance = _performance_stance(
        PERFORMANCE_MIN_CLOSED_TRADES, win_rate_pct=20.0,
        profit_factor=PERFORMANCE_WEAK_PROFIT_FACTOR - 0.1, max_drawdown_pct=3.0,
    )
    assert stance == "UNDERPERFORMING"
    assert "严禁为挽回亏损" in guidance


def test_high_drawdown_outranks_a_good_profit_factor():
    stance, guidance = _performance_stance(
        PERFORMANCE_MIN_CLOSED_TRADES, win_rate_pct=70.0, profit_factor=3.0,
        max_drawdown_pct=PERFORMANCE_HIGH_DRAWDOWN_PCT,
    )
    assert stance == "DRAWDOWN_PRIORITY"
    assert "控制回撤为先" in guidance


class _BrokenStore:
    def _connect(self):
        raise RuntimeError("ledger offline")


def test_unreadable_ledger_reports_unavailable_instead_of_zeroes():
    context = build_performance_context(_BrokenStore(), "gate_testnet")
    assert context["status"] == "UNAVAILABLE"
    assert context["reason"] == "RuntimeError"
    assert "win_rate_pct" not in context


def test_empty_ledger_is_available_and_safe_to_serialise(tmp_path):
    store = SQLiteStore(tmp_path / "perf.sqlite3")
    store.initialize()
    context = build_performance_context(store, "gate_testnet", venue="gate", mode="TESTNET")
    assert context["status"] == "AVAILABLE"
    assert context["stance"] == "INSUFFICIENT_SAMPLE"
    for key, value in context.items():
        if isinstance(value, float):
            assert math.isfinite(value), key
    json.dumps(context, ensure_ascii=False, allow_nan=False)


def test_guidance_is_chinese_and_quotes_the_actual_metrics():
    stance, guidance = _performance_stance(
        PERFORMANCE_MIN_CLOSED_TRADES, win_rate_pct=33.3, profit_factor=0.75,
        max_drawdown_pct=4.0,
    )
    assert stance == "UNDERPERFORMING"
    assert "0.75" in guidance and "33.3" in guidance
