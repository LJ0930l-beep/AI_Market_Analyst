from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.trading.entry_economics import (
    EntryEconomicsError,
    effective_min_net_rr,
    evaluate_gate_entry_economics,
    quantity_for_notional,
)


def evaluate(**overrides):
    values = {
        "side": "LONG",
        "order_type": "limit",
        "quote": "100",
        "entry_price": "100",
        "stop_price": "95",
        "target_price": "112",
        "quantity": "20",
        "contract_size": "1",
        "price_tick": "0.1",
        "amount_step": "1",
        "taker_fee_rate": "0.00075",
        "slippage_rate": "0.001",
        "equity": "100000",
        "risk_per_trade_pct": "0.15",
        "min_net_rr": "2.0",
    }
    values.update(overrides)
    return evaluate_gate_entry_economics(**values)


def test_low_net_rr_proposal_is_rejected_after_taker_fees_and_slippage():
    # Gross reward/risk is just over 2:1, but conservative costs pull net RR below 2.
    with pytest.raises(EntryEconomicsError, match="AI_NET_REWARD_RISK_TOO_LOW"):
        evaluate(target_price="110")


def test_price_action_net_rr_floor_cannot_be_lowered_but_other_templates_keep_their_policy():
    assert effective_min_net_rr("price_action_structure", "1.5") == Decimal("2.0")
    assert effective_min_net_rr("price_action_structure", "2.4") == Decimal("2.4")
    assert effective_min_net_rr("other_template", "1.5") == Decimal("1.5")


def test_stop_risk_uses_equity_percentage_not_available_margin():
    # A 2,000-USDT notional with this stop risks far more than 0.15% of 1,000 USDT.
    with pytest.raises(EntryEconomicsError, match="STOP_RISK_LIMIT_EXCEEDED"):
        evaluate(equity="1000")


def test_precision_floor_is_the_only_quantity_reduction_and_is_used_in_risk():
    quantity = quantity_for_notional(
        notional_usdt="2000", entry_price="100", order_type="limit", side="LONG",
        contract_size="1", amount_step="0.3", slippage_rate="0.001",
    )
    assert quantity == Decimal("19.8")
    result = evaluate(quantity=quantity, amount_step="0.3", target_price="113", equity="100000")
    assert result.quantity == Decimal("19.8")
    assert result.notional_usdt == Decimal("1980.0")
    assert result.estimated_stop_risk_usdt > 0
    assert result.net_reward_risk >= Decimal("2.0")


def test_market_entry_slippage_and_price_tick_are_part_of_the_same_evaluation():
    result = evaluate(order_type="market", quote="100.01", entry_price="99.9", target_price="113", equity="100000")
    assert result.entry_price == Decimal("100.1")
    assert result.estimated_stop_risk_usdt > Decimal(20)


def test_model_authored_limit_and_protection_prices_are_not_silently_rounded():
    with pytest.raises(EntryEconomicsError) as caught:
        evaluate(entry_price="99.95")
    assert caught.value.code == "GATE_PRICE_TICK_MISMATCH"
    with pytest.raises(EntryEconomicsError) as caught:
        evaluate(target_price="112.05")
    assert caught.value.code == "GATE_PRICE_TICK_MISMATCH"


def test_missing_costs_or_misaligned_contract_amount_fail_closed():
    with pytest.raises(EntryEconomicsError) as missing_cost:
        evaluate(taker_fee_rate=None)
    assert missing_cost.value.code == "GATE_ENTRY_ECONOMICS_INVALID"
    with pytest.raises(EntryEconomicsError, match="GATE_AMOUNT_STEP_MISMATCH"):
        evaluate(quantity="19.9", amount_step="1")


@pytest.mark.parametrize("failure,target,equity,expected", [
    ("RISK", 113, 1000.0, "STOP_RISK_LIMIT_EXCEEDED"),
    ("RR", 110, 100000.0, "AI_NET_REWARD_RISK_TOO_LOW"),
])
def test_replay_execution_sink_rechecks_risk_and_rr_before_simulated_fill(failure, target, equity, expected):
    from core.replay.ai_template_runner import _ReplayGateway
    from core.trading.execution_gateway import GatewayError

    applied = []

    class FakeAccount:
        def account_truth(self, *_args, **_kwargs):
            return {"equity": equity}

        def apply_decision(self, decision, _as_of, _market):
            applied.append(decision)
            return {"status": "FILLED", "action": decision["action"]}

    strategy = {"template_id": "price_action_structure",
                "execution": {"risk_per_trade_pct": 0.15, "min_net_rr": 1.5}}
    gateway = _ReplayGateway(FakeAccount(), object(), strategy)
    gateway.prices = {"BTCUSDT": 100.0}
    gateway.current_decision = {"action": "OPEN_LONG"}
    gateway.as_of = None
    intent = SimpleNamespace(
        reduce_only=False, side="LONG", order_type="limit", price=100,
        quantity=20, leverage=5, limit_price=100, ttl_seconds=900,
        protection_plan=SimpleNamespace(stop_price=95, take_profit=target),
        position_id=None,
    )
    market = {
        "price": 100,
        "fee_rate": 0.00075,
        "slippage": 0.001,
        "market": {
            "contractSize": 1,
            "precision": {"amount": 1, "price": 0.1},
            "limits": {"price": {"step": 0.1}},
        },
    }
    with pytest.raises(GatewayError) as caught:
        gateway.submit_intent(intent, market_snapshot=market)
    assert caught.value.code == expected
    assert applied == []
