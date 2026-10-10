from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta

import pytest

from core.replay.pa_decision_quality_v36.context import build_context
from core.replay.pa_decision_quality_v36.experiments import (
    EXPERIMENTS,
    a0_cache_identity,
    exact_cache_identity,
    prompt_sha256,
    safe_state_snapshot,
)
from core.replay.pa_decision_quality_v36.runner import (
    StudyError,
    diagnose_open_proposal,
    run_study,
    validate_model_call_gate,
)
from core.replay.pa_decision_quality_v36.schema import validate_analysis
from core.replay.pa_decision_quality_v36.validation import schema_version_for_experiment


def _open_proposal(side="LONG", **overrides):
    values = {"side": side, "order_type": "limit", "proposed_notional_usdt": "2000",
              "entry_price": "100", "stop_price": "99" if side == "LONG" else "101",
              "target_price": "104" if side == "LONG" else "96", "requested_leverage": "10"}
    values.update(overrides)
    return values


def _valid_v36_wait(evidence_ref):
    return {
        "context": {"market_regime": "UNCERTAIN", "higher_timeframe_bias": "NEUTRAL"},
        "location": {"trade_location": "UNKNOWN"},
        "signal": {"setup": "NONE", "signal_quality": "NO_SIGNAL"},
        "action": "WAIT", "decision_rationale": "No confirmed entry trigger.",
        "counter_evidence": ["No structure has been confirmed."],
        "evidence_refs": [evidence_ref], "future_hypotheses": [],
    }


def _v36_cache_row(point, experiment_id, analysis, *, actual_model_id="test-model",
                   declared_schema_version=None):
    context = build_context(point["bars_by_timeframe"], point["decision_time"])
    state = safe_state_snapshot(point["state_snapshot"])
    canonical_state = json.dumps(state, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
    decision_time = point["decision_time"].replace("+00:00", "Z")
    spec = EXPERIMENTS[experiment_id]
    identity = exact_cache_identity(
        experiment_id=experiment_id, model_id="test-model",
        prompt_sha=prompt_sha256(
            experiment_id, context, state, point.get("risk_inputs"),
            str(point.get("risk_mode") or "FIXED_NOTIONAL"),
        ),
        data_sha=context.input_sha256, decision_time=decision_time,
        state_sha=hashlib.sha256(canonical_state.encode("utf-8")).hexdigest(),
        prompt_version=spec["prompt_version"],
        analysis_schema_version=spec["analysis_schema_version"],
    )
    return {
        "identity": identity, "status": "COMPLETED", "analysis": analysis,
        "analysis_schema_version": declared_schema_version or spec["analysis_schema_version"],
        "requested_model_id": "test-model", "actual_model_id": actual_model_id,
    }


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
    ref = "market_snapshot:ETH_USDT:observed"
    point["a0_original"] = {
        "prompt": "V35 prompt", "model_input": {"candles": [1, 2], "evidence_refs": [ref]},
        "state_snapshot": point["state_snapshot"], "model_id": "gemini-high",
        "prompt_version": "v35-frozen-1",
        "analysis_schema_version": schema_version_for_experiment("A0"),
    }
    identity = a0_cache_identity(point)
    analysis = {"action": "WAIT", "instrument_id": "ETH_USDT",
                "reason": "Wait for a confirmed setup.", "confidence": None,
                "evidence_refs": [ref]}
    cache = [{"identity": identity, "analysis": analysis,
              "analysis_schema_version": identity["analysis_schema_version"],
              "actual_model_id": "gemini-high", "status": "COMPLETED",
              "raw_model_response": "cached"}]
    calls = []
    result = run_study([point], cache_rows=cache, model_caller=lambda *args: calls.append(args))
    experiments = result["decision_records"][0]["experiments"]
    assert result["run_manifest"]["model_calls_used"] == 0
    assert calls == []
    assert experiments["A0"]["status"] == "CACHE_MATCH"
    assert experiments["A0"]["analysis_validation"]["status"] == "VALID"
    assert experiments["A0"]["model_identity_status"] == "MATCHED"
    assert "raw_model_response" not in json.dumps(experiments["A0"])
    assert experiments["A1"]["status"] == "NOT_RUN_MODEL_CALLS_DISABLED"
    assert experiments["A2"]["status"] == "NOT_RUN_MODEL_CALLS_DISABLED"
    changed = copy.deepcopy(point)
    changed["a0_original"]["prompt"] += " changed"
    result_changed = run_study([changed], cache_rows=cache)
    assert result_changed["decision_records"][0]["experiments"]["A0"]["status"] == "NOT_RUN_NO_EXACT_CACHE"


def test_a0_cache_uses_v35_schema_version_and_original_evidence_allowlist(research_fixture):
    point = research_fixture["point"]()
    ref = "market_snapshot:ETH_USDT:observed"
    point["a0_original"] = {
        "prompt": "V35 prompt", "model_input": {"evidence_refs": [ref]},
        "state_snapshot": point["state_snapshot"], "model_id": "gemini-high",
        "prompt_version": "v35-frozen-1",
        "analysis_schema_version": schema_version_for_experiment("A0"),
    }
    analysis = {"action": "WAIT", "instrument_id": "ETH_USDT",
                "reason": "Wait for a confirmed setup.", "confidence": None,
                "evidence_refs": ["market_snapshot:BTC_USDT:unknown"]}
    identity = a0_cache_identity(point)
    result = run_study([point], cache_rows=[{
        "identity": identity, "analysis": analysis,
        "analysis_schema_version": identity["analysis_schema_version"],
        "actual_model_id": "gemini-high", "status": "COMPLETED",
    }])
    item = result["decision_records"][0]["experiments"]["A0"]

    assert item["status"] == "CACHE_MATCH"
    assert item["analysis_validation"]["status"] == "INVALID"
    assert "V35_EVIDENCE_REFERENCE_INVALID" in item["analysis_validation"]["errors"]
    assert item["model_identity_status"] == "MATCHED"
    assert item["lifecycle"]["proposal"] == "NOT_OBSERVED"


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
    assert exp["A1"]["model_identity_status"] == "MATCHED"
    assert exp["A1"]["actual_model_id"] == "test-model"
    assert exp["A2"]["status"] == "NOT_RUN_BUDGET_EXHAUSTED"


@pytest.mark.parametrize("actual_model_id,expected_status,expected_identity", [
    ("test-model", "COMPLETED", "MATCHED"),
    ("different-model", "MODEL_ID_MISMATCH", "MISMATCH"),
    (None, "MODEL_ID_UNVERIFIED", "UNVERIFIED"),
])
def test_response_model_identity_requires_reported_actual_id(
    research_fixture, actual_model_id, expected_status, expected_identity,
):
    point = research_fixture["point"]()

    def caller(_experiment_id, payload):
        evidence_ref = payload["causal_context"]["frames"]["15m"]["evidence_refs"][0]
        return {"model_id": actual_model_id, "analysis": _valid_v36_wait(evidence_ref)}

    result = run_study([point], run=True, max_decisions=1,
                       model_id="test-model", model_caller=caller)
    item = result["decision_records"][0]["experiments"]["A1"]
    assert item["status"] == expected_status
    assert item["requested_model_id"] == "test-model"
    assert item["actual_model_id"] == actual_model_id
    assert item["model_identity_status"] == expected_identity
    assert item["analysis_validation"]["status"] == "VALID"
    if actual_model_id is None:
        assert item["model_id"] is None
        assert "MODEL_ID_UNVERIFIED" in item["validation_errors"]
    if expected_identity != "MATCHED":
        assert "risk_preflight" not in item
        assert item["lifecycle"]["gateway_acceptance"] == "NOT_OBSERVED"


def test_non_object_model_response_is_unverified_and_never_promotes_request_id(research_fixture):
    point = research_fixture["point"]()
    result = run_study(
        [point], run=True, max_decisions=1, model_id="requested-model",
        model_caller=lambda *_args: ["not", "an", "object"],
    )
    item = result["decision_records"][0]["experiments"]["A1"]

    assert item["status"] == "INVALID_MODEL_RESPONSE"
    assert item["actual_model_id"] is None
    assert item["model_id"] is None
    assert item["requested_model_id"] == "requested-model"
    assert item["model_identity_status"] == "UNVERIFIED"
    assert "risk_preflight" not in item


@pytest.mark.parametrize("corruption,expected_error", [
    ("evidence", "EVIDENCE_REFERENCE_INVALID"),
    ("schema", "ANALYSIS_SCHEMA_VERSION_MISMATCH"),
])
def test_exact_v36_cache_match_is_revalidated(corruption, expected_error, research_fixture):
    point = research_fixture["point"]()
    context = build_context(point["bars_by_timeframe"], point["decision_time"])
    evidence_ref = min(context.evidence_refs)
    analysis = _valid_v36_wait(evidence_ref)
    declared_schema = None
    if corruption == "evidence":
        analysis["evidence_refs"] = ["bar:15m:not-in-this-context"]
    else:
        declared_schema = "pa-decision-quality-v36/older"
    cache = _v36_cache_row(
        point, "A1", analysis, declared_schema_version=declared_schema,
    )
    calls = []

    result = run_study([point], cache_rows=[cache], model_id="test-model",
                        model_caller=lambda *args: calls.append(args))
    item = result["decision_records"][0]["experiments"]["A1"]

    assert result["run_manifest"]["model_calls_used"] == 0
    assert calls == []
    assert item["status"] == "CACHE_MATCH"
    assert item["analysis_validation"]["status"] == "INVALID"
    assert expected_error in item["analysis_validation"]["errors"]
    assert item["model_identity_status"] == "MATCHED"
    assert "risk_preflight" not in item


def test_exact_v36_cache_match_preserves_response_identity_and_revalidated_analysis(research_fixture):
    point = research_fixture["point"]()
    context = build_context(point["bars_by_timeframe"], point["decision_time"])
    analysis = _valid_v36_wait(min(context.evidence_refs))
    cache = _v36_cache_row(point, "A1", analysis)
    result = run_study([point], cache_rows=[cache], model_id="test-model")
    item = result["decision_records"][0]["experiments"]["A1"]
    assert item["status"] == "CACHE_MATCH"
    assert item["analysis_validation"]["status"] == "VALID"
    assert item["model_identity_status"] == "MATCHED"
    assert item["actual_model_id"] == "test-model"
    assert result["run_manifest"]["model_calls_used"] == 0


@pytest.mark.parametrize("actual_model_id,expected_identity", [
    ("different-model", "MISMATCH"),
    (None, "UNVERIFIED"),
])
def test_exact_cache_hit_does_not_promote_missing_or_mismatched_response_identity(
    research_fixture, actual_model_id, expected_identity,
):
    point = research_fixture["point"]()
    context = build_context(point["bars_by_timeframe"], point["decision_time"])
    cache = _v36_cache_row(
        point, "A1", _valid_v36_wait(min(context.evidence_refs)),
        actual_model_id=actual_model_id,
    )
    result = run_study([point], cache_rows=[cache], model_id="test-model")
    item = result["decision_records"][0]["experiments"]["A1"]

    assert item["status"] == "CACHE_MATCH"
    assert item["analysis_validation"]["status"] == "VALID"
    assert item["model_identity_status"] == expected_identity
    assert item["model_id"] == actual_model_id
    assert "risk_preflight" not in item


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
    assert experiment["model_id"] is None
    assert experiment["requested_model_id"] == "test"
    assert experiment["model_identity_status"] == "UNVERIFIED"
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
    point["execution_quote"] = {
        "observed_at": (research_fixture["decision_time"] - timedelta(seconds=30)).isoformat(),
        "source": "fixture:point-in-time-best-bid-ask",
        "best_bid": "104",
        "best_ask": "104.1",
    }
    point["risk_inputs"] = research_fixture["risk_inputs"](
        equity="30000", available_margin="29000", quote="104",
    )
    result = run_study([point])
    preflight = result["decision_records"][0]["experiments"]["A3"]["risk_preflight"]
    assert preflight["status"] == "ELIGIBLE_PROPOSAL"
    assert preflight["economics"]["estimated_stop_risk_usdt"] <= "75.00"


def test_a3_does_not_reuse_an_expired_failed_breakout(research_fixture):
    frames = research_fixture["failed_breakout_frames"]()
    decision_time = datetime(2025, 1, 5, 12, 25, tzinfo=research_fixture["decision_time"].tzinfo)
    frames["5m"].extend(research_fixture["make_bars"](
        "5m", count=6, last_end=decision_time - timedelta(minutes=5),
    ))
    frames["15m"].extend(research_fixture["make_bars"](
        "15m", count=2, last_end=decision_time - timedelta(minutes=10),
    ))
    point = research_fixture["point"](
        input_bars=frames,
        decision_time=decision_time,
    )

    result = run_study([point])
    a3 = result["decision_records"][0]["experiments"]["A3"]

    assert a3["status"] == "NO_CANDIDATE"
    assert a3["candidate"]["reason_code"] == "FAILED_BREAKOUT_SIGNAL_EXPIRED"


def test_a3_does_not_treat_confirmation_close_as_executable_price(research_fixture):
    point = research_fixture["point"](
        input_bars=research_fixture["failed_breakout_frames"](),
    )

    result = run_study([point])
    candidate = result["decision_records"][0]["experiments"]["A3"]["candidate"]

    assert candidate["proposal"]["entry_price"] is None
    assert candidate["proposal"]["historical_confirmation_close"] == 104.0
    assert candidate["execution"]["status"] == "CURRENT_QUOTE_REQUIRED"


@pytest.mark.parametrize("quote_time_kind", ["stale", "before_confirmation", "at_decision"])
def test_a3_rejects_stale_or_noncausal_execution_quotes(research_fixture, quote_time_kind):
    point = research_fixture["point"](
        input_bars=research_fixture["failed_breakout_frames"](),
    )
    decision_time = datetime.fromisoformat(point["decision_time"])
    signal_available_at = datetime.fromisoformat(
        point["bars_by_timeframe"]["5m"][-1]["available_at"],
    )
    observed_at = {
        "stale": decision_time - timedelta(seconds=61),
        "before_confirmation": signal_available_at - timedelta(seconds=1),
        "at_decision": decision_time,
    }[quote_time_kind]
    point["execution_quote"] = {
        "observed_at": observed_at.isoformat(), "source": "fixture:quote",
        "best_bid": "104", "best_ask": "104.1",
    }

    result = run_study([point])
    a3 = result["decision_records"][0]["experiments"]["A3"]

    assert a3["status"] == "RESEARCH_CANDIDATE"
    assert a3["candidate"]["execution"]["status"] == "QUOTE_REJECTED"
    assert a3["candidate"]["proposal"]["entry_price"] is None
    assert a3["risk_preflight"]["status"] == "BLOCKED"


@pytest.mark.parametrize("side,expected_price", [("SHORT", 104.0), ("LONG", 96.1)])
def test_a3_uses_directional_side_of_current_point_in_time_quote(
    research_fixture, side, expected_price,
):
    frames = research_fixture["failed_breakout_frames"]()
    if side == "LONG":
        frames["5m"][-2].update(open=96.0, high=97.0, low=93.0, close=94.0)
        frames["5m"][-1].update(open=94.0, high=97.0, low=94.0, close=96.0)
    point = research_fixture["point"](input_bars=frames)
    decision_time = datetime.fromisoformat(point["decision_time"])
    point["execution_quote"] = {
        "observed_at": (decision_time - timedelta(seconds=30)).isoformat(),
        "source": "fixture:point-in-time-best-bid-ask",
        "best_bid": "95.9" if side == "LONG" else "104.0",
        "best_ask": "96.1" if side == "LONG" else "104.8",
    }

    result = run_study([point])
    candidate = result["decision_records"][0]["experiments"]["A3"]["candidate"]

    assert candidate["side"] == side
    assert candidate["execution"]["status"] == "POINT_IN_TIME_QUOTE_VALIDATED"
    assert candidate["proposal"]["entry_price"] == expected_price
    assert candidate["proposal"]["historical_confirmation_close"] == (96.0 if side == "LONG" else 104.0)


def test_a3_accepts_a_quote_at_the_inclusive_maximum_quote_age(research_fixture):
    point = research_fixture["point"](
        input_bars=research_fixture["failed_breakout_frames"](),
    )
    decision_time = datetime.fromisoformat(point["decision_time"])
    point["execution_quote"] = {
        "observed_at": (decision_time - timedelta(seconds=60)).isoformat(),
        "source": "fixture:point-in-time-best-bid-ask",
        "best_bid": "104", "best_ask": "104.1",
    }

    result = run_study([point])
    candidate = result["decision_records"][0]["experiments"]["A3"]["candidate"]

    assert candidate["execution"]["status"] == "POINT_IN_TIME_QUOTE_VALIDATED"
    assert candidate["execution"]["maximum_quote_age_seconds"] == 60
    assert candidate["proposal"]["entry_price"] == 104.0


def test_a3_rejects_quote_when_entry_trigger_is_not_active(research_fixture):
    point = research_fixture["point"](
        input_bars=research_fixture["failed_breakout_frames"](),
    )
    decision_time = datetime.fromisoformat(point["decision_time"])
    point["execution_quote"] = {
        "observed_at": (decision_time - timedelta(seconds=30)).isoformat(),
        "source": "fixture:point-in-time-best-bid-ask",
        "best_bid": "105", "best_ask": "105.1",
    }

    result = run_study([point])
    a3 = result["decision_records"][0]["experiments"]["A3"]

    assert a3["status"] == "NO_CANDIDATE"
    assert a3["candidate"]["reason_code"] == "FAILED_BREAKOUT_TRIGGER_NOT_ACTIVE_AT_CURRENT_QUOTE"


def test_a3_expiration_is_inclusive_at_the_frozen_ten_minute_boundary(research_fixture):
    frames = research_fixture["failed_breakout_frames"]()
    expires_at = datetime(2025, 1, 5, 12, 0, 1, tzinfo=research_fixture["decision_time"].tzinfo)
    # Keep the point-in-time frame fresh without touching the target, stop, or breakout level.
    frames["5m"].extend(research_fixture["make_bars"](
        "5m", count=1, last_end=datetime(2025, 1, 5, 11, 55, tzinfo=expires_at.tzinfo),
    ))
    point = research_fixture["point"](input_bars=frames, decision_time=expires_at)

    result = run_study([point])
    a3 = result["decision_records"][0]["experiments"]["A3"]

    assert a3["status"] == "NO_CANDIDATE"
    assert a3["candidate"]["reason_code"] == "FAILED_BREAKOUT_SIGNAL_EXPIRED"
    assert a3["candidate"]["signal"]["expires_at"] == expires_at.isoformat().replace("+00:00", "Z")


def test_a3_rejects_signal_availability_before_confirmation(research_fixture):
    frames = research_fixture["failed_breakout_frames"]()
    confirmation_time = datetime.fromisoformat(frames["5m"][-1]["bar_end"])
    frames["5m"][-1]["available_at"] = (confirmation_time - timedelta(seconds=1)).isoformat()
    point = research_fixture["point"](input_bars=frames)

    result = run_study([point])
    a3 = result["decision_records"][0]["experiments"]["A3"]

    assert a3["status"] == "NO_CANDIDATE"
    assert a3["candidate"]["reason_code"] == (
        "FAILED_BREAKOUT_SIGNAL_AVAILABILITY_PRECEDES_CONFIRMATION"
    )


@pytest.mark.parametrize("invalidation", ["rebreakout_close", "target_touch"])
def test_a3_invalidates_signal_when_a_later_closed_bar_breaks_the_setup(
    research_fixture, invalidation,
):
    frames = research_fixture["failed_breakout_frames"]()
    later = research_fixture["make_bars"](
        "5m", count=1, last_end=datetime(2025, 1, 5, 11, 55, tzinfo=research_fixture["decision_time"].tzinfo),
    )[0]
    if invalidation == "rebreakout_close":
        later.update(open=104.0, high=107.0, low=103.0, close=106.0)
    else:
        later.update(open=104.0, high=106.0, low=95.0, close=100.0)
    frames["5m"].append(later)
    decision_time = datetime(2025, 1, 5, 11, 56, tzinfo=research_fixture["decision_time"].tzinfo)
    point = research_fixture["point"](input_bars=frames, decision_time=decision_time)

    result = run_study([point])
    a3 = result["decision_records"][0]["experiments"]["A3"]

    assert a3["status"] == "NO_CANDIDATE"
    assert a3["candidate"]["reason_code"] == "FAILED_BREAKOUT_SIGNAL_INVALIDATED"


def test_a3_future_bars_cannot_change_a_valid_point_in_time_quote_candidate(research_fixture):
    point = research_fixture["point"](
        input_bars=research_fixture["failed_breakout_frames"](),
    )
    decision_time = datetime.fromisoformat(point["decision_time"])
    point["execution_quote"] = {
        "observed_at": (decision_time - timedelta(seconds=30)).isoformat(),
        "source": "fixture:point-in-time-best-bid-ask",
        "best_bid": "104", "best_ask": "104.1",
    }
    baseline = run_study([point])["decision_records"][0]["experiments"]["A3"]["candidate"]
    future_point = copy.deepcopy(point)
    for timeframe in ("5m", "15m", "1h", "4h"):
        step = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}[timeframe]
        future_point["bars_by_timeframe"][timeframe].extend(
            research_fixture["make_bars"](
                timeframe, count=1,
                last_end=decision_time + timedelta(minutes=step),
            ),
        )
    with_future = run_study([future_point])["decision_records"][0]["experiments"]["A3"]["candidate"]

    assert with_future == baseline
