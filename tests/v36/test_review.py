from __future__ import annotations

import copy

from core.replay.pa_decision_quality_v36.review import summarize
from core.replay.pa_decision_quality_v36.schema import SCHEMA_VERSION
from core.replay.pa_decision_quality_v36.validation import schema_version_for_experiment


def _valid_wait(ref):
    return {
        "context": {"market_regime": "TRADING_RANGE", "higher_timeframe_bias": "NEUTRAL"},
        "location": {"trade_location": "RANGE_MIDDLE"},
        "signal": {"setup": "NONE", "signal_quality": "NO_SIGNAL"},
        "action": "WAIT", "decision_rationale": "Wait for a confirmed trigger.",
        "counter_evidence": ["The range has no confirmed breakout."],
        "evidence_refs": [ref], "future_hypotheses": [],
    }


def test_outcomes_and_reviews_are_joined_only_to_the_exact_experiment():
    ref = "bar:15m:0123456789abcdef"
    records = [{
        "decision_id": "shared-decision",
        "decision_time": "2025-01-01T00:00:00Z",
        "context": {"frames": {"15m": {"evidence_refs": [ref]}}},
        "experiments": {
            "A0": {
                "status": "CACHE_MATCH",
                "analysis": {"action": "WAIT", "instrument_id": "BTCUSDT",
                             "reason": "Wait for a confirmed trigger.", "confidence": None,
                             "evidence_refs": [ref]},
                "analysis_schema_version": schema_version_for_experiment("A0"),
                "validation_evidence_refs": [ref],
                "requested_model_id": "v35-model", "actual_model_id": "v35-model",
                "lifecycle": {"gateway_acceptance": "ACCEPTED", "venue_fill": "NOT_OBSERVED",
                              "complete_close": "NOT_OBSERVED"},
                "token_usage": {"input": 10, "output": 5}, "latency_ms": 25,
            },
            "A1": {
                "status": "COMPLETED",
                "analysis": _valid_wait(ref), "analysis_schema_version": SCHEMA_VERSION,
                "requested_model_id": "v36-model", "actual_model_id": "v36-model",
                "lifecycle": {"gateway_acceptance": "NOT_OBSERVED", "venue_fill": "NOT_OBSERVED",
                              "complete_close": "NOT_OBSERVED"},
            },
        },
    }]
    outcomes = [
        {"experiment_id": "A0", "decision_id": "shared-decision", "complete_close": True,
         "closed_at": "2025-01-01T01:00:00Z", "net_pnl_usdt": "10",
         "net_r": "1.5", "fees_usdt": "1", "slippage_usdt": "2"},
        # This loss has no experiment key and must not be cloned into each arm.
        {"decision_id": "shared-decision", "complete_close": True,
         "closed_at": "2025-01-01T02:00:00Z", "net_pnl_usdt": "-99"},
        # A close timestamp before the decision is not a valid joined outcome.
        {"experiment_id": "A1", "decision_id": "shared-decision", "complete_close": True,
         "closed_at": "2024-12-31T23:00:00Z", "net_pnl_usdt": "-100"},
    ]
    reviews = [
        {"experiment_id": "A0", "decision_id": "shared-decision",
         "wait_assessment": {"assessment": "REASONABLE_WAIT", "evidence_refs": [ref]},
         "error_attributions": [{"category": "TARGET_UNSUPPORTED", "evidence_refs": [ref]}]},
        {"decision_id": "shared-decision",
         "wait_assessment": {"assessment": "MISSED_CANDIDATE", "evidence_refs": [ref]}},
    ]
    original_records = copy.deepcopy(records)
    original_outcomes = copy.deepcopy(outcomes)
    original_reviews = copy.deepcopy(reviews)

    report = summarize(records, outcomes=outcomes, reviews=reviews)
    a0 = report["experiments"]["A0"]
    a1 = report["experiments"]["A1"]

    assert report["outcome_join_count"] == 1
    assert a0["closed_trade_metrics"]["sample_count"] == 1
    assert a0["closed_trade_metrics"]["net_pnl_usdt"] == 10
    assert a1["closed_trade_metrics"]["sample_count"] == 0
    assert a0["wait_assessment_counts"] == {"REASONABLE_WAIT": 1}
    assert a1["wait_assessment_counts"] == {"INDETERMINATE": 1}
    assert a0["error_attribution_counts"] == {"TARGET_UNSUPPORTED": 1}
    assert a0["gateway_accepted_orders"] == 1
    assert a0["venue_fills"] == 0
    assert a0["complete_closes_observed"] == 0
    assert a0["model_cost_status"] == "UNKNOWN_NO_VERIFIED_PRICE_SOURCE"
    assert a0["valid_analyses"] == 1
    assert a0["valid_model_decisions"] == 1
    assert a0["cache_matches"] == 1
    assert a0["valid_cache_analyses"] == 1
    assert a0["verified_cache_model_decisions"] == 1
    assert records == original_records
    assert outcomes == original_outcomes
    assert reviews == original_reviews


def test_unknown_experiment_and_bad_refs_fail_closed():
    report = summarize(
        [{"decision_id": "d", "decision_time": "2025-01-01T00:00:00Z",
          "context": {"frames": {"15m": {"evidence_refs": []}}},
          "experiments": {"A0": {"status": "COMPLETED",
                                 "analysis": {"action": "WAIT"},
                                 "lifecycle": {}}}}],
        outcomes=[{"experiment_id": "A9", "decision_id": "d", "complete_close": True,
                   "closed_at": "2025-01-02T00:00:00Z", "net_pnl_usdt": "1"}],
        reviews=[{"experiment_id": "A0", "decision_id": "d",
                  "wait_assessment": {"assessment": "MISSED_CANDIDATE",
                                      "evidence_refs": ["untrusted-ref"]},
                  "error_attributions": [{"category": "DIRECTION_ERROR",
                                          "evidence_refs": 17}]}],
    )
    assert report["outcome_join_count"] == 0
    assert report["experiments"]["A0"]["closed_trade_metrics"]["sample_count"] == 0
    assert report["experiments"]["A0"]["wait_assessment_counts"] == {"INDETERMINATE": 1}
    assert report["experiments"]["A0"]["error_attribution_counts"] == {"UNDETERMINED": 1}


def test_management_proposal_count_is_separate_from_order_acceptance():
    report = summarize([{
        "decision_id": "manage-1", "decision_time": "2025-01-01T00:00:00Z",
        "context": {"frames": {"15m": {"evidence_refs": ["bar:15m:0123456789abcdef"]}}},
        "experiments": {"A1": {
            "status": "COMPLETED",
            "analysis": {"action": "CLOSE_POSITION"},
            "lifecycle": {"proposal": "NOT_OBSERVED", "management_proposal": "OBSERVED",
                          "gateway_acceptance": "NOT_OBSERVED", "venue_fill": "NOT_OBSERVED",
                          "complete_close": "NOT_OBSERVED"},
        }},
    }])
    metrics = report["experiments"]["A1"]
    assert metrics["management_proposals"] == 1
    assert metrics["gateway_accepted_orders"] == 0
    assert metrics["venue_fills"] == 0
    assert metrics["complete_closes_observed"] == 0


def test_research_metrics_count_revalidated_caches_and_verified_model_identity():
    refs = ["bar:15m:0123456789abcdef"]
    records = []
    cache_items = [
        {"analysis": _valid_wait(refs[0]), "analysis_schema_version": SCHEMA_VERSION,
         "actual_model_id": "model-a", "requested_model_id": "model-a"},
        {"analysis": {**_valid_wait(refs[0]), "evidence_refs": ["bar:15m:forged"]},
         "analysis_schema_version": SCHEMA_VERSION,
         "actual_model_id": "model-a", "requested_model_id": "model-a"},
        {"analysis": _valid_wait(refs[0]), "analysis_schema_version": "v36/old-schema",
         "actual_model_id": "model-a", "requested_model_id": "model-a"},
        {"analysis": _valid_wait(refs[0]), "analysis_schema_version": SCHEMA_VERSION,
         "actual_model_id": "model-b", "requested_model_id": "model-a"},
        {"analysis": _valid_wait(refs[0]), "analysis_schema_version": SCHEMA_VERSION,
         "requested_model_id": "model-a"},
    ]
    for index, cached in enumerate(cache_items):
        records.append({
            "decision_id": f"cache-{index}",
            "decision_time": "2025-01-01T00:00:00Z",
            "context": {"frames": {"15m": {"evidence_refs": refs}}},
            "experiments": {"A1": {"status": "CACHE_MATCH", **cached}},
        })

    report = summarize(records, outcomes=[
        {"experiment_id": "A1", "decision_id": "cache-0", "complete_close": True,
         "closed_at": "2025-01-01T01:00:00Z", "net_pnl_usdt": "5"},
        {"experiment_id": "A1", "decision_id": "cache-1", "complete_close": True,
         "closed_at": "2025-01-01T01:00:00Z", "net_pnl_usdt": "-99"},
        {"experiment_id": "A1", "decision_id": "cache-2", "complete_close": True,
         "closed_at": "2025-01-01T01:00:00Z", "net_pnl_usdt": "-88"},
    ])
    metrics = report["experiments"]["A1"]

    assert metrics["cache_matches"] == 5
    assert metrics["valid_cache_analyses"] == 3
    assert metrics["verified_cache_model_decisions"] == 1
    assert metrics["invalid_cache_analyses"] == 2
    assert metrics["schema_version_mismatches"] == 1
    assert metrics["valid_analyses"] == 3
    assert metrics["valid_model_decisions"] == 1
    assert metrics["model_identity_counts"] == {"MATCHED": 3, "MISMATCH": 1, "UNVERIFIED": 1}
    assert metrics["wait_decisions"] == 1
    assert metrics["raw_action_counts"] == {"WAIT": 5}
    assert report["outcome_join_count"] == 1
    assert metrics["closed_trade_metrics"]["net_pnl_usdt"] == 5
    assert metrics["outcomes_skipped_invalid_decision"] == 2
