from decimal import Decimal

import pytest

from scripts.review_gemini_proposal_cost_math import scenario, review


@pytest.mark.parametrize("side,e,s,t", [("LONG", 100, 95, 110), ("SHORT", 100, 105, 90)])
def test_zero_cost_matches_gross_and_both_legs_reduce_space(side, e, s, t):
    zero = scenario(side, e, s, t, 0, 0)
    charged = scenario(side, e, s, t, ".001", ".002")
    assert Decimal(zero["cost_adjusted_reward_to_loss"]) == 2
    assert Decimal(charged["cost_adjusted_reward_to_loss"]) < 2
    assert Decimal(charged["entry_fee_per_unit"]) == Decimal(".1")
    expected_stop = Decimal(str(s)) * (Decimal(".998") if side == "LONG" else Decimal("1.002"))
    assert Decimal(charged["assumed_stop_execution_price"]) == expected_stop
    assert Decimal(charged["stop_exit_fee_per_unit"]) == expected_stop * Decimal(".001")


def test_notional_price_scale_and_leverage_are_not_rr_inputs():
    small = scenario("SHORT", 100, 105, 90, ".001", ".002")
    large = scenario("SHORT", 1000, 1050, 900, ".001", ".002")
    assert small["cost_adjusted_reward_to_loss"] == large["cost_adjusted_reward_to_loss"]


def test_target_profit_can_turn_negative():
    result = scenario("LONG", 100, 99, "100.01", ".001", ".001")
    assert Decimal(result["reward_per_underlying_unit"]) < 0


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", -1, None, "oops"])
def test_invalid_cost_not_silently_zero(bad):
    with pytest.raises(ValueError):
        scenario("LONG", 100, 95, 110, bad, 0)


@pytest.mark.parametrize("side,s,t", [("LONG", 105, 110), ("SHORT", 105, 110), ("WAIT", 95, 110)])
def test_invalid_side_or_protection_rejected(side, s, t):
    with pytest.raises(ValueError):
        scenario(side, 100, s, t, 0, 0)


@pytest.mark.parametrize("partition", ["validation", "untouched_test", None])
def test_observer_refuses_non_optimization_inputs(tmp_path, partition):
    import hashlib
    import json
    plan = {"pilot_windows": [{"partition": partition}]}
    fingerprint = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    (tmp_path / "research-plan.json").write_text(json.dumps({"plan": plan,
        "plan_sha256": fingerprint}), encoding="utf-8")
    (tmp_path / "trade-casebook.json").write_text(json.dumps({"plan_sha256": fingerprint,
        "case_count": 0, "cases": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="OPTIMIZATION_ONLY"):
        review(tmp_path)
