"""Exercise the production order path; all sinks are isolated local fixtures."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.model_routing import DEFAULT_SMART_MODEL
from core.replay.ai_template_runner import frozen_templates
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import AIStrategyBook, TEMPLATES
from core.trading.ai_led_engine import AICycleContext, AIActionOutput, AILedDecisionEngine
from core.trading.autonomous_strategy import CONTRACT
from core.trading.execution_gateway import TradingMode


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda item: item["id"])
@pytest.mark.parametrize("ai_leverage,venue_max,available,expected", [
    (10, 200, 1000, "SUBMITTED"), (150, 200, 1000, "SUBMITTED"),
    (250, 200, 1000, "SUBMITTED"), (2, 200, 5, "BLOCKED"),
])
def test_fixed_value_venue_leverage_and_no_silent_budget_resize(template, ai_leverage, venue_max, available, expected):
    now = datetime.now(timezone.utc)
    ctx = AICycleContext(
        cycle_id="isolated-fixed", account_id="gate_testnet", generation=1,
        started_at=now.isoformat(), expires_at=(now + timedelta(minutes=4)).isoformat(),
        allowed_instruments=("BTCUSDT",), decision_contract=CONTRACT,
        mode=TradingMode.TESTNET, venue="gate", environment="TESTNET",
        model_id=DEFAULT_SMART_MODEL, model_version=DEFAULT_SMART_MODEL,
        model_call_attempted=True, model_call_completed=True,
        model_inference_settings={"actual_model_id": DEFAULT_SMART_MODEL,
            "verified_manifest_model_id": DEFAULT_SMART_MODEL, "model_identity_source": "completion_response"},
        market_snapshots={"BTCUSDT": {"price": 100, "data_as_of": now.isoformat(),
            "market": {"contractSize": .01, "leverage_max": venue_max, "taker": .0005,
                "precision": {"amount": 1, "price": .1},
                "limits": {"amount": {"min": 1, "max": 100000}, "price": {"step": .1}}}}},
        account_truth={"status": "AVAILABLE", "equity": "10000", "available_margin": str(available),
                       "used_margin": "0", "positions": []},
        strategy_instructions={"template_id": template["id"], "execution": template["execution_defaults"]},
    )
    # AI amount drift cannot change the operator's fixed value.
    proposal = AIActionOutput(action="OPEN_LONG", instrument_id="BTCUSDT", reason="历史结构支持",
        entry_price=99.9, stop_price=95, take_profit=110,
        position_size_usdt=600, requested_leverage=ai_leverage, order_preference="LIMIT")
    submissions = []
    engine = object.__new__(AILedDecisionEngine)
    engine.gateway = SimpleNamespace(submit_intent=lambda intent, **kw: (
        submissions.append(intent) or {"status": "ACKNOWLEDGED", "order_id": "isolated-only"}))
    engine.ledger = SimpleNamespace(get_open_positions=lambda *a, **kw: [])
    engine.agent_policy_id = "isolated"
    engine._persist_cycle = lambda *a, **kw: None
    engine._live_execution_quote = lambda *a, **kw: None
    result = engine.execute_cycle(ctx, now=now, model_output=proposal)
    assert result.status == expected
    if expected == "BLOCKED":
        assert not submissions
        assert result.reason == "FIXED_NOTIONAL_MARGIN_INSUFFICIENT"
    else:
        intent = submissions[0]
        assert intent.leverage == min(ai_leverage, venue_max)
        actual = Decimal(str(intent.quantity)) * Decimal(str(intent.price)) * Decimal(".01")
        assert Decimal("2000") - Decimal(".999") < actual <= Decimal("2000")
        assert intent.selection_evidence["target_notional_usdt"] == "2000.0"
        assert intent.protection_plan.stop_price == 95
        assert intent.protection_plan.take_profit == 110


def test_research_and_new_accounts_share_all_five_fixed_templates(tmp_path):
    store = SQLiteStore(tmp_path / "isolated.sqlite3")
    store.initialize()
    active = AIStrategyBook(store).active("gate_testnet")
    for config in [active, *frozen_templates()]:
        assert config["execution"]["fixed_notional_usdt"] == 2000
        assert config["execution"]["max_notional_usdt"] == 2000
        assert config["execution"]["sizing_mode"] == "FIXED_NOTIONAL"
        assert config["execution"]["leverage_mode"] == "VENUE_LIMIT"
