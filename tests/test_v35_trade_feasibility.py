import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.trading.entry_economics import (
    calculate_gate_entry_economics,
    evaluate_gate_entry_economics,
)
from core.trading.trade_feasibility import (
    FIXED_NOTIONAL,
    RISK_BUDGETED_NOTIONAL,
    diagnose_trade_proposal,
)


def proposal(**overrides):
    values = {
        "mode": FIXED_NOTIONAL,
        "side": "LONG",
        "order_type": "limit",
        "proposed_notional_usdt": "2000",
        "fixed_notional_usdt": "2000",
        "entry_price": "100",
        "quote": "100",
        "stop_price": "95",
        "target_price": "113",
        "requested_leverage": 5,
        "equity": "100000",
        "available_margin": "100000",
        "risk_per_trade_pct": "0.15",
        "min_net_rr": "2.0",
        "taker_fee_rate": "0.00075",
        "slippage_rate": "0.001",
        "contract_size": "1",
        "amount_step": "1",
        "price_tick": "0.1",
        "min_amount": "1",
        "max_amount": "10000",
        "max_leverage": "20",
        "max_margin_pct": "100",
        "max_notional": "5000",
    }
    values.update(overrides)
    return diagnose_trade_proposal(**values)


@pytest.mark.parametrize(("side", "order_type"), [
    ("LONG", "limit"), ("LONG", "market"),
    ("SHORT", "limit"), ("SHORT", "market"),
])
def test_diagnostic_uses_shared_gate_cost_and_risk_calculation(side, order_type):
    if side == "LONG":
        stop, target = "95", "113"
    else:
        stop, target = "105", "87"
    quote = "100.01" if order_type == "market" else "100"
    result = proposal(side=side, order_type=order_type, quote=quote,
                      stop_price=stop, target_price=target)
    economics = calculate_gate_entry_economics(
        side=side, order_type=order_type, quote=quote,
        entry_price="100", stop_price=stop, target_price=target,
        quantity=result["quantity"], contract_size="1", price_tick="0.1",
        amount_step="1", taker_fee_rate="0.00075", slippage_rate="0.001",
        equity="100000", risk_per_trade_pct="0.15",
    )
    assert result["estimated_stop_risk_usdt"] == str(economics.estimated_stop_risk_usdt)
    assert result["net_reward_risk"] == str(economics.net_reward_risk)
    assert result["lifecycle"] == {
        "stage": "PROPOSAL_ONLY",
        "gateway_acceptance": "NOT_OBSERVED",
        "venue_fill": "NOT_OBSERVED",
        "complete_close": "NOT_OBSERVED",
    }
    assert result["risk_policy_resized_proposal"] is False


@pytest.mark.parametrize("mode", [FIXED_NOTIONAL, RISK_BUDGETED_NOTIONAL])
def test_over_budget_order_is_rejected_without_resizing(mode):
    result = proposal(mode=mode, equity="1000", available_margin="1000")
    assert result["feasible"] is False
    assert result["status"] == "REJECTED"
    assert "STOP_RISK_LIMIT_EXCEEDED" in result["reason_codes"]
    assert result["evaluated_notional_target_usdt"] == "2000"
    assert result["risk_policy_resized_proposal"] is False


def test_fixed_mode_reports_risk_budget_minimum_equity():
    result = proposal(equity="1000000", available_margin="1000000")
    expected = Decimal(result["estimated_stop_risk_usdt"]) * 100 / Decimal("0.15")
    assert Decimal(result["minimum_viable_equity_usdt"]) == expected


def test_higher_fees_slippage_and_wider_stops_raise_risk_and_reduce_net_rr():
    low_cost = proposal(taker_fee_rate="0.0001", slippage_rate="0.0001")
    high_cost = proposal(taker_fee_rate="0.001", slippage_rate="0.002")
    wide_stop = proposal(stop_price="90")
    assert Decimal(high_cost["estimated_stop_risk_usdt"]) > Decimal(low_cost["estimated_stop_risk_usdt"])
    assert Decimal(high_cost["net_reward_risk"]) < Decimal(low_cost["net_reward_risk"])
    assert Decimal(wide_stop["estimated_stop_risk_usdt"]) > Decimal(low_cost["estimated_stop_risk_usdt"])


def test_leverage_changes_margin_estimate_but_never_stop_loss_risk():
    low_leverage = proposal(requested_leverage=1)
    high_leverage = proposal(requested_leverage=20)
    assert low_leverage["estimated_stop_risk_usdt"] == high_leverage["estimated_stop_risk_usdt"]
    assert Decimal(low_leverage["estimated_opening_margin_usdt"]) > Decimal(
        high_leverage["estimated_opening_margin_usdt"]
    )


def test_risk_budgeted_capacity_is_reported_but_does_not_shrink_the_proposal():
    result = proposal(mode=RISK_BUDGETED_NOTIONAL, equity="1000", available_margin="1000")
    assert Decimal(result["risk_budgeted_max_notional_usdt"]) < Decimal(2000)
    assert result["risk_budgeted_proposal_within_capacity"] is False
    assert "PROPOSAL_EXCEEDS_RISK_BUDGETED_CAPACITY" in result["reason_codes"]
    assert result["model_proposed_notional_usdt"] == "2000"
    assert result["evaluated_notional_target_usdt"] == "2000"
    assert result["risk_policy_resized_proposal"] is False


def test_budget_capacity_honors_strategy_notional_cap_with_fractional_contract_steps():
    result = proposal(
        mode=RISK_BUDGETED_NOTIONAL, side="LONG", order_type="limit",
        entry_price="100", quote="100", stop_price="99.9", target_price="101",
        contract_size="0.01", amount_step="0.1", price_tick="0.01",
        proposed_notional_usdt="2000", equity="1000000",
        available_margin="1000000", max_margin_pct="100", max_notional="2000",
    )
    capacity = Decimal(result["risk_budgeted_max_notional_usdt"])
    assert Decimal(1900) < capacity <= Decimal(2000)


def test_margin_and_contract_limits_are_independent_from_stop_risk():
    margin = proposal(available_margin="1")
    leverage = proposal(requested_leverage=21)
    minimum = proposal(min_amount="100")
    maximum = proposal(max_amount="10")
    min_notional = proposal(min_notional="5000")
    assert "MARGIN_INSUFFICIENT" in margin["reason_codes"]
    assert "GATE_LEVERAGE_LIMIT_EXCEEDED" in leverage["reason_codes"]
    assert "GATE_AMOUNT_BELOW_CONTRACT_MINIMUM" in minimum["reason_codes"]
    assert "GATE_AMOUNT_ABOVE_CONTRACT_MAXIMUM" in maximum["reason_codes"]
    assert "GATE_NOTIONAL_BELOW_CONTRACT_MINIMUM" in min_notional["reason_codes"]


def test_incomplete_equity_margin_or_contract_data_fails_closed_with_reasons():
    result = proposal(equity=None, available_margin=None, max_amount=None, max_leverage=None)
    assert result["feasible"] is False
    assert result["status"] == "BLOCKED"
    assert "ACCOUNT_EQUITY_UNAVAILABLE" in result["reason_codes"]
    assert "AVAILABLE_MARGIN_UNAVAILABLE" in result["reason_codes"]
    assert "GATE_CONTRACT_LIMITS_UNAVAILABLE" in result["reason_codes"]
    assert result["minimum_viable_equity_usdt"] is not None


@pytest.mark.parametrize("bad", [None, "NaN", "Infinity", "-1"])
def test_invalid_financial_inputs_fail_closed(bad):
    result = proposal(taker_fee_rate=bad)
    assert result["feasible"] is False
    assert result["status"] == "INVALID"
    assert result["reason_codes"] == ["TRADE_FEASIBILITY_INVALID"]


def test_price_and_amount_precision_rejections_are_explicit():
    misaligned = proposal(entry_price="100.05")
    amount = proposal(mode=RISK_BUDGETED_NOTIONAL,
                      amount_step="3", proposed_notional_usdt="1")
    assert "GATE_PRICE_TICK_MISMATCH" in misaligned["reason_codes"]
    assert amount["status"] == "REJECTED"
    assert "QUANTITY_BELOW_AMOUNT_STEP" in amount["reason_codes"]


def test_gate_evaluator_and_replay_entry_share_the_same_economics():
    from core.replay.ai_template_runner import _ReplayGateway

    expected = evaluate_gate_entry_economics(
        side="LONG", order_type="limit", quote="100", entry_price="100",
        stop_price="95", target_price="113", quantity="20",
        contract_size="1", price_tick="0.1", amount_step="1",
        taker_fee_rate="0.00075", slippage_rate="0.001",
        equity="100000", risk_per_trade_pct="0.15", min_net_rr="2.0",
    )
    seen = []

    class Account:
        def account_truth(self, *_args, **_kwargs):
            return {"equity": 100000}

        def apply_decision(self, decision, _as_of, _market):
            seen.append(decision)
            return {"status": "ACCEPTED", "action": decision["action"]}

    strategy = {"template_id": "price_action_structure", "execution": {
        "risk_per_trade_pct": 0.15, "min_net_rr": 2.0,
    }}
    gateway = _ReplayGateway(Account(), object(), strategy)
    gateway.prices = {"BTCUSDT": 100.0}
    gateway.current_decision = {"action": "OPEN_LONG"}
    gateway.as_of = datetime(2026, 1, 1, tzinfo=UTC)
    intent = SimpleNamespace(
        reduce_only=False, side="LONG", order_type="limit", price=100,
        quantity=20, leverage=5, limit_price=100, ttl_seconds=900,
        protection_plan=SimpleNamespace(stop_price=95, take_profit=113),
        position_id=None,
    )
    market = {
        "price": 100, "fee_rate": 0.00075, "slippage": 0.001,
        "market": {"contractSize": 1,
                   "precision": {"amount": 1, "price": 0.1},
                   "limits": {"price": {"step": 0.1}}},
    }
    result = gateway.submit_intent(intent, market_snapshot=market)
    assert result["simulation_event"]["entry_economics"] == expected.audit_dict()
    assert seen[0]["_require_exact_quantity"] is True


def test_replay_rejects_margin_cap_instead_of_silently_shrinking_gate_order():
    from core.replay.ai_simulation import ReplayAccount

    base = {
        "action": "OPEN_LONG", "instrument_id": "BTCUSDT",
        "order_preference": "MARKET", "requested_leverage": 10,
        "quantity": 2, "stop_price": 99, "take_profit": 102,
    }
    market = {"instrument_id": "BTCUSDT", "price": 100,
              "contract_rules": {"contract_size": 1, "amount_step": 0.1,
                                 "price_tick": 0.1, "min_size": 0.1,
                                 "max_size": 1000, "leverage_max": 20}}
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strict = ReplayAccount(initial_equity=50, margin_cap_pct=5, account_id="v35-strict")
    rejected = strict.apply_decision({**base, "_require_exact_quantity": True}, now, market)
    assert rejected["status"] == "REJECTED"
    assert rejected["reason"] == "MARGIN_BUDGET_INSUFFICIENT"
    assert strict.orders == {}
    assert strict.positions == {}

    generic = ReplayAccount(initial_equity=50, margin_cap_pct=5, account_id="v35-generic")
    accepted = generic.apply_decision(base, now, market)
    assert accepted["status"] == "ACCEPTED"
    assert accepted["quantity"] < base["quantity"]


def test_v25_report_uses_proposal_fields_and_preserves_original_outcomes():
    from scripts.generate_v35_feasibility_reports import build_reports

    sample_path = Path(__file__).resolve().parents[1] / "docs/research/v25-price-action-sample-20261008.json"
    original = sample_path.read_bytes()
    sample = json.loads(original.decode("utf-8"))
    summary, comparison = build_reports(sample, original)
    assert hashlib.sha256(sample_path.read_bytes()).hexdigest() == hashlib.sha256(original).hexdigest()
    assert comparison["decision_data_boundary"]["lookahead_used"] is False
    assert comparison["historical_outcomes_modified"] is False
    assert len(comparison["trades"]) == 10
    assert summary["current_account_diagnostic"]["equity_usdt"] is None
    assert summary["current_account_diagnostic"]["private_exchange_request_count"] == 0
    source_trade = sample["closed_trades"][0]
    report_trade = comparison["trades"][0]
    assert report_trade["original_sampled_outcome_unchanged"]["net_pnl_usdt"] == source_trade["net_pnl_usdt"]
    assert report_trade["original_sampled_outcome_unchanged"]["net_outcome"] == source_trade["net_outcome"]
