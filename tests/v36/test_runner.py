from __future__ import annotations

import copy
import json

import pytest

from core.replay.pa_decision_quality_v36.experiments import a0_cache_identity, safe_state_snapshot
from core.replay.pa_decision_quality_v36.runner import (
    StudyError,
    diagnose_open_proposal,
    run_study,
    validate_model_call_gate,
)
from core.replay.pa_decision_quality_v36.schema import validate_analysis


def _open_proposal(side="LONG", **overrides):
    values = {"side": side, "order_type": "limit", "proposed_notional_usdt": "2000",
              "entry_price": "100", "stop_price": "99" if side == "LONG" else "101",
              "target_price": "104" if side == "LONG" else "96", "requested_leverage": "10"}
    values.update(overrides)
    return values


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_v35_shared_economics_accepts_valid_long_and_short_proposals(research_fixture, side):
    proposal = _open_proposal(side)
    result = diagnose_open_proposal(proposal, research_fixture["risk_inputs"](), mode="FIXED_NOTIONAL")
    assert result["status"] == "ELIGIBLE_PROPOSAL"
    assert result["economics"]["net_reward_risk_pass"] is True
    assert result["economics"]["stop_risk_within_budget"] is True
    assert result["risk_policy_resized_proposal"] is False


def test_over_budget_or_unaffordable_proposals_are_rejected_without_resizing(research_fixture):
    proposal = _open_proposal()
    original = copy.deepcopy(proposal)
    result = diagnose_open_proposal(proposal, research_fixture["risk_inputs"](equity="1000"), mode="FIXED_NOTIONAL")
    assert result["status"] == "REJECTED"
    assert "STOP_RISK_LIMIT_EXCEEDED" in result["reason_codes"]
    assert result["model_proposed_notional_usdt"] == "2000"
    assert result["proposal_unchanged"] is True
    assert proposal == original


def test_low_net_rr_precision_and_margin_constraints_are_rejected(research_fixture):
    base = _open_proposal(target_price="102")
    low_rr = diagnose_open_proposal(base, research_fixture["risk_inputs"](), mode="FIXED_NOTIONAL")
    assert low_rr["status"] == "REJECTED"
    assert "AI_NET_REWARD_RISK_TOO_LOW" in low_rr["reason_codes"]
    bad_tick = diagnose_open_proposal(_open_proposal(entry_price="100.05"), research_fixture["risk_inputs"](), mode="FIXED_NOTIONAL")
    assert bad_tick["status"] == "REJECTED"
    assert "GATE_PRICE_TICK_MISMATCH" in bad_tick["reason_codes"]
    poor_margin = diagnose_open_proposal(_open_proposal(), research_fixture["risk_inputs"](available_margin="1"), mode="FIXED_NOTIONAL")
    assert poor_margin["status"] == "REJECTED"
    assert "MARGIN_INSUFFICIENT" in poor_margin["reason_codes"]


def test_fixed_notional_mismatch_and_risk_budget_overrun_are_not_silently_resized(research_fixture):
    fixed = diagnose_open_proposal(_open_proposal(proposed_notional_usdt="1000"),
                                   research_fixture["risk_inputs"](), mode="FIXED_NOTIONAL")
    assert fixed["status"] == "REJECTED"
    assert fixed["reason_codes"] == ["FIXED_NOTIONAL_PROPOSAL_MISMATCH"]
    risk = diagnose_open_proposal(_open_proposal(proposed_notional_usdt="5000"),
                                  research_fixture["risk_inputs"](), mode="RISK_BUDGETED_NOTIONAL")
    assert risk["status"] == "REJECTED"
    assert "PROPOSAL_EXCEEDS_RISK_BUDGETED_CAPACITY" in risk["reason_codes"]
    assert risk["risk_policy_resized_proposal"] is False


def test_missing_data_and_weakened_v35_policy_block(research_fixture):
    missing = diagnose_open_proposal(_open_proposal(), {}, mode="FIXED_NOTIONAL")
    assert missing["status"] == "BLOCKED"
    assert "equity" in missing["missing_fields"]
    weak_rr = diagnose_open_proposal(_open_proposal(), research_fixture["risk_inputs"](min_net_rr="1.9"), mode="FIXED_NOTIONAL")
    assert weak_rr["reason_codes"] == ["V35_NET_RR_FLOOR_MUST_REMAIN_2_0"]
    weak_risk = diagnose_open_proposal(_open_proposal(), research_fixture["risk_inputs"](risk_per_trade_pct="0.26"), mode="FIXED_NOTIONAL")
    assert weak_risk["reason_codes"] == ["V35_STOP_RISK_CAP_MUST_NOT_EXCEED_0_25_PERCENT"]


def test_model_call_requires_explicit_positive_budget():
    validate_model_call_gate(run=False, max_decisions=None)
    for run, budget in ((True, None), (True, 0), (True, -1), (True, True), (False, 1)):
        with pytest.raises(StudyError):
            validate_model_call_gate(run=run, max_decisions=budget)


def test_default_study_is_offline_and_a0_requires_recomputed_exact_cache(research_fixture):
    point = research_fixture["point"]()
    point["a0_original"] = {
        "prompt": "V35 prompt", "model_input": {"candles": [1, 2]},
        "state_snapshot": point["state_snapshot"], "model_id": "gemini-high",
        "prompt_version": "v35-frozen-1",
    }
    identity = a0_cache_identity(point)
    cache = [{"identity": identity, "analysis": {"action": "WAIT"}, "raw_model_response": "cached"}]
    calls = []
    result = run_study([point], cache_rows=cache, model_caller=lambda *args: calls.append(args))
    experiments = result["decision_records"][0]["experiments"]
    assert result["run_manifest"]["model_calls_used"] == 0
    assert calls == []
    assert experiments["A0"]["status"] == "CACHE_MATCH"
    assert "raw_model_response" not in json.dumps(experiments["A0"])
    assert experiments["A1"]["status"] == "NOT_RUN_MODEL_CALLS_DISABLED"
    assert experiments["A2"]["status"] == "NOT_RUN_MODEL_CALLS_DISABLED"
    changed = copy.deepcopy(point)
    changed["a0_original"]["prompt"] += " changed"
    result_changed = run_study([changed], cache_rows=cache)
    assert result_changed["decision_records"][0]["experiments"]["A0"]["status"] == "NOT_RUN_NO_EXACT_CACHE"


def test_outcomes_never_enter_model_prompt_and_model_calls_obey_total_budget(research_fixture):
    point = research_fixture["point"]()
    point["risk_inputs"] = research_fixture["risk_inputs"]()
    point["secret_outcome"] = {"net_pnl_usdt": "999"}
    calls = []

    def caller(experiment_id, payload):
        calls.append((experiment_id, payload))
        ref = payload["causal_context"]["frames"]["15m"]["evidence_refs"][0]
        return {"model_id": "test-model", "raw_model_response": "raw response",
                "token_usage": {"input": 50, "output": 20}, "latency_ms": 10,
                "analysis": {"context": {"market_regime": "UNCERTAIN",
                                         "higher_timeframe_bias": "NEUTRAL"},
                             "location": {"trade_location": "UNKNOWN"},
                             "signal": {"setup": "NONE", "signal_quality": "NO_SIGNAL"},
                             "action": "WAIT",
                             "decision_rationale": "No verified entry trigger.",
                             "counter_evidence": ["no confirmed trigger"], "evidence_refs": [ref],
                             "future_hypotheses": []}}

    result = run_study([point], run=True, max_decisions=1, model_id="test-model", model_caller=caller)
    exp = result["decision_records"][0]["experiments"]
    assert result["run_manifest"]["model_calls_used"] == 1
    assert len(calls) == 1 and calls[0][0] == "A1"
    assert "secret_outcome" not in json.dumps(calls[0][1])
    assert calls[0][1]["execution_constraints"]["status"] == "COMPLETE"
    assert calls[0][1]["execution_constraints"]["values"]["min_net_rr"] == "2.0"
    assert exp["A1"]["status"] == "COMPLETED"
    assert exp["A2"]["status"] == "NOT_RUN_BUDGET_EXHAUSTED"


@pytest.mark.parametrize("side,prices", [
    ("OPEN_LONG", {"entry_price": "100", "stop_price": "99", "target_price": "104"}),
    ("OPEN_SHORT", {"entry_price": "100", "stop_price": "101", "target_price": "96"}),
])
def test_open_analysis_requires_executable_fields_and_directional_price_geometry(side, prices):
    risk = {
        "entry_trigger": "close above level", "invalidation": "below swing",
        "target_structure": "prior high", "target_evidence_refs": ["valid-ref"],
        "net_reward_risk": "2.5", "order_type": "LIMIT",
        "proposed_notional_usdt": "2000", "requested_leverage": "10", **prices,
    }
    analysis = {
        "context": {"market_regime": "BULL_TREND", "higher_timeframe_bias": "BULLISH"},
        "location": {"trade_location": "TREND_PULLBACK"},
        "signal": {"setup": "H2", "signal_quality": "CONDITIONAL"}, "action": side,
        "risk_reward": risk, "counter_evidence": ["opposing structure"],
        "decision_rationale": "A confirmed test of support has a structural invalidation.",
        "evidence_refs": ["valid-ref"], "future_hypotheses": [],
    }
    assert validate_analysis(analysis, {"valid-ref"}) == []
    malformed = copy.deepcopy(analysis)
    malformed["risk_reward"].pop("requested_leverage")
    if side == "OPEN_LONG":
        malformed["risk_reward"]["stop_price"] = "102"
    else:
        malformed["risk_reward"]["target_price"] = "102"
    errors = validate_analysis(malformed, {"valid-ref"})
    assert "OPEN_REQUESTED_LEVERAGE_MISSING" in errors
    assert "OPEN_PRICE_GEOMETRY_INVALID" in errors


def test_malformed_unhashable_model_labels_fail_as_schema_errors():
    errors = validate_analysis({
        "context": {"market_regime": [], "higher_timeframe_bias": {}},
        "location": {"trade_location": []},
        "signal": {"setup": {}, "signal_quality": []},
        "action": [],
        "decision_rationale": "malformed labels",
        "counter_evidence": [],
        "evidence_refs": [],
        "future_hypotheses": [],
    }, set())
    assert "ACTION_INVALID" in errors
    assert "MARKET_REGIME_INVALID" in errors
    assert "SIGNAL_INVALID" in errors


def test_existing_position_management_actions_use_redacted_current_state_refs():
    state = safe_state_snapshot({
        "positions": [{
            "position_id": "private-position-id", "symbol": "ETH_USDT", "side": "LONG",
            "quantity": "1", "entry_price": "100", "stop_price": "90",
            "target_price": "120", "opened_at": "2025-01-01T00:00:00Z",
        }],
        "working_orders": [{
            "order_id": "private-order-id", "symbol": "ETH_USDT", "side": "LONG",
            "order_type": "LIMIT", "price": "95", "quantity": "1",
            "status": "OPEN", "created_at": "2025-01-01T00:01:00Z",
        }],
    })
    assert state is not None
    serialized_state = json.dumps(state)
    assert "private-position-id" not in serialized_state
    assert "private-order-id" not in serialized_state
    position_ref = state["positions"][0]["position_ref"]
    order_ref = state["working_orders"][0]["order_ref"]
    evidence = "bar:15m:0123456789abcdef"

    def decision(action, management):
        return {
            "context": {"market_regime": "BULL_TREND", "higher_timeframe_bias": "BULLISH"},
            "location": {"trade_location": "STRUCTURE_LEVEL"},
            "signal": {"setup": "NONE", "signal_quality": "CONDITIONAL"},
            "action": action, "management": management,
            "decision_rationale": "Manage only the referenced current position.",
            "counter_evidence": ["Price may continue in the current direction."],
            "evidence_refs": [evidence], "future_hypotheses": [],
        }

    actions = [
        ("CLOSE_POSITION", {"position_ref": position_ref}),
        ("REDUCE_POSITION", {"position_ref": position_ref, "reduce_fraction": "0.5"}),
        ("TIGHTEN_STOP", {"position_ref": position_ref, "new_stop_price": "92"}),
        ("UPDATE_PROTECTION", {"position_ref": position_ref, "new_take_profit": "118"}),
        ("CANCEL_ORDER", {"order_ref": order_ref}),
    ]
    for action, fields in actions:
        assert validate_analysis(decision(action, fields), {evidence}, state_snapshot=state) == []

    loosening = decision("TIGHTEN_STOP", {"position_ref": position_ref, "new_stop_price": "89"})
    errors = validate_analysis(loosening, {evidence}, state_snapshot=state)
    assert "STOP_UPDATE_MUST_TIGHTEN" in errors
    wrong_order = decision("CANCEL_ORDER", {"order_ref": "order:unknown"})
    assert "MANAGEMENT_ORDER_REFERENCE_INVALID" in validate_analysis(
        wrong_order, {evidence}, state_snapshot=state,
    )


def test_management_proposal_is_recorded_without_order_lifecycle_claims(research_fixture):
    point = research_fixture["point"](state={
        "positions": [{
            "position_id": "private-position-id", "symbol": "ETH_USDT", "side": "LONG",
            "quantity": "1", "entry_price": "100", "stop_price": "90",
            "opened_at": "2025-01-01T00:00:00Z",
        }],
        "working_orders": [],
    })

    def caller(experiment_id, payload):
        assert experiment_id == "A1"
        position_ref = payload["state_snapshot"]["positions"][0]["position_ref"]
        evidence_ref = payload["causal_context"]["frames"]["15m"]["evidence_refs"][0]
        return {"model_id": "test-model", "analysis": {
            "context": {"market_regime": "BULL_TREND", "higher_timeframe_bias": "BULLISH"},
            "location": {"trade_location": "STRUCTURE_LEVEL"},
            "signal": {"setup": "NONE", "signal_quality": "CONDITIONAL"},
            "action": "CLOSE_POSITION", "management": {"position_ref": position_ref},
            "decision_rationale": "The current position should be closed based on the cited structure.",
            "counter_evidence": ["The trend may continue."], "evidence_refs": [evidence_ref],
            "future_hypotheses": [],
        }}

    result = run_study([point], run=True, max_decisions=1,
                       model_id="test-model", model_caller=caller)
    experiment = result["decision_records"][0]["experiments"]["A1"]
    assert experiment["status"] == "COMPLETED"
    assert experiment["lifecycle"] == {
        "proposal": "NOT_OBSERVED", "management_proposal": "OBSERVED",
        "gateway_acceptance": "NOT_OBSERVED", "venue_fill": "NOT_OBSERVED",
        "complete_close": "NOT_OBSERVED",
    }


def test_timeout_is_classified_and_proposal_lifecycle_stays_separate(research_fixture):
    point = research_fixture["point"]()

    def timeout(*_):
        raise TimeoutError("synthetic timeout")

    result = run_study([point], run=True, max_decisions=1, model_id="test", model_caller=timeout)
    experiment = result["decision_records"][0]["experiments"]["A1"]
    assert experiment["status"] == "MODEL_TIMEOUT"
    assert experiment["error_code"] == "MODEL_TIMEOUT"
    assert experiment["lifecycle"]["gateway_acceptance"] == "NOT_OBSERVED"


def test_a3_is_non_production_failed_breakout_and_uses_same_risk_check(research_fixture):
    point = research_fixture["point"](
        input_bars=research_fixture["failed_breakout_frames"](),
        decision_time=research_fixture["decision_time"],
    )
    result = run_study([point])
    a3 = result["decision_records"][0]["experiments"]["A3"]
    assert a3["status"] == "RESEARCH_CANDIDATE"
    assert a3["candidate"]["production_authority"] is False
    assert a3["candidate"]["side"] == "SHORT"
    assert a3["candidate"]["trigger"]["entry_after_confirmation"] is True
    assert a3["risk_preflight"]["status"] == "BLOCKED"
    assert a3["lifecycle"]["venue_fill"] == "NOT_OBSERVED"

    point["risk_inputs"] = research_fixture["risk_inputs"]()
    point["proposed_notional_usdt"] = "2000"
    point["requested_leverage"] = 10
    point["risk_inputs"] = research_fixture["risk_inputs"](
        equity="30000", available_margin="29000", quote="104",
    )
    result = run_study([point])
    preflight = result["decision_records"][0]["experiments"]["A3"]["risk_preflight"]
    assert preflight["status"] == "ELIGIBLE_PROPOSAL"
    assert preflight["economics"]["estimated_stop_risk_usdt"] <= "75.00"
