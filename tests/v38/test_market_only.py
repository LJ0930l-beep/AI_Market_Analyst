from __future__ import annotations

import socket
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from core.replay.pa_decision_quality_v36.runner import run_study
from core.replay.pa_decision_quality_v38.market_only import (
    ANALYSIS_SCHEMA_VERSION,
    MarketOnlyError,
    build_market_only_input,
    review_market_only_records,
    run_offline_stub,
    validate_market_only_analysis,
)


def test_market_only_input_has_no_account_or_execution_state(market_point_factory):
    request = build_market_only_input(market_point_factory())

    assert request["track"] == "MARKET_ONLY"
    assert request["authority"] == {
        "production_authority": False,
        "order_creation_authorized": False,
        "account_state_included": False,
        "execution_constraints_included": False,
    }
    assert set(request["evidence_refs"])
    assert "state_snapshot" not in request
    assert "risk_inputs" not in request
    assert set(request["data_available_through_by_timeframe"]) == {"5m", "15m", "1h", "4h"}


def test_market_only_rejects_account_risk_and_unrecognized_point_fields(market_point_factory):
    point = market_point_factory(state_snapshot={"equity_usdt": "10000"})

    with pytest.raises(MarketOnlyError, match="MARKET_ONLY_ACCOUNT_OR_EXECUTION_INPUT_FORBIDDEN"):
        build_market_only_input(point)

    point = market_point_factory(risk_inputs={"available_margin": "9000"})
    with pytest.raises(MarketOnlyError, match="MARKET_ONLY_ACCOUNT_OR_EXECUTION_INPUT_FORBIDDEN"):
        build_market_only_input(point)


def test_market_only_rejects_pre_confirmation_availability(market_point_factory):
    point = market_point_factory()
    final_bar = point["bars_by_timeframe"]["5m"][-1]
    final_bar["available_at"] = (final_bar_end(final_bar) - timedelta(seconds=1)).isoformat()

    with pytest.raises(MarketOnlyError, match="BAR_AVAILABILITY_PRECEDES_CONFIRMATION"):
        build_market_only_input(point)


def final_bar_end(bar):
    return datetime.fromisoformat(bar["bar_end"])


def test_market_only_partition_labels_are_checked_against_frozen_utc_boundaries(market_point_factory):
    point = market_point_factory(decision=final_bar_end({"bar_end": "2026-04-01T00:00:00+00:00"}))
    point["partition"] = "optimization"

    with pytest.raises(MarketOnlyError, match="PARTITION_LABEL_MISMATCH"):
        build_market_only_input(point)


def test_market_only_input_hash_ignores_future_bars_and_binds_past_bars(market_point_factory):
    point = market_point_factory()
    original = build_market_only_input(point)

    future = deepcopy(point)
    last = future["bars_by_timeframe"]["5m"][-1].copy()
    decision = final_bar_end({"bar_end": point["decision_time"]})
    step = timedelta(minutes=5)
    last_end = decision + step
    future_row = last.copy()
    future_row.update(
        bar_start=(last_end - step).isoformat(),
        bar_end=last_end.isoformat(),
        available_at=(last_end + timedelta(seconds=1)).isoformat(),
        open=999999.0, high=1000000.0, low=999998.0, close=999999.5, volume=999999.0,
    )
    future["bars_by_timeframe"]["5m"].append(future_row)
    appended_future = build_market_only_input(future)
    assert appended_future["market_input_sha256"] == original["market_input_sha256"]

    altered_past = deepcopy(point)
    altered_past["bars_by_timeframe"]["5m"][10]["close"] += 0.01
    assert build_market_only_input(altered_past)["market_input_sha256"] != original["market_input_sha256"]


def test_market_only_analysis_rejects_order_risk_and_unverified_evidence_fields(market_point_factory):
    result = run_offline_stub([market_point_factory()])
    record = result["records"][0]
    analysis = deepcopy(record["analysis"])
    refs = set(record["market_input"]["evidence_refs"])
    assert validate_market_only_analysis(analysis, valid_evidence_refs=refs) == []
    assert analysis["schema_version"] == ANALYSIS_SCHEMA_VERSION

    analysis["risk_reward"] = {"net_reward_risk": 3.0}
    assert "MARKET_ONLY_EXECUTION_OR_RISK_FIELD_FORBIDDEN" in validate_market_only_analysis(
        analysis, valid_evidence_refs=refs,
    )

    analysis = deepcopy(record["analysis"])
    analysis["action"] = "OPEN_LONG"
    assert "MARKET_ONLY_ACTION_INVALID" in validate_market_only_analysis(
        analysis, valid_evidence_refs=refs,
    )

    analysis = deepcopy(record["analysis"])
    analysis["market_view"]["evidence_refs"] = ["bar:forged"]
    assert "MARKET_ONLY_EVIDENCE_REF_INVALID" in validate_market_only_analysis(
        analysis, valid_evidence_refs=refs,
    )


@pytest.mark.parametrize(("field", "value"), [
    ("action", []),
    ("market_regime", {}),
    ("higher_timeframe_bias", []),
    ("location", {}),
    ("signal_setup", []),
    ("signal_quality", {}),
    ("indicative_bias", []),
    ("candidate_kind", []),
    ("target_kind", {}),
])
def test_analysis_validation_rejects_unhashable_enum_values(market_point_factory, field, value):
    record = run_offline_stub([market_point_factory()])["records"][0]
    analysis = deepcopy(record["analysis"])
    view = analysis["market_view"]
    if field == "action":
        analysis["action"] = value
    elif field.startswith("signal_"):
        view["signal"][field.removeprefix("signal_")] = value
    elif field == "candidate_kind":
        view["candidate_setup"]["kind"] = value
    elif field == "target_kind":
        view["target_structure"]["kind"] = value
    else:
        view[field] = value

    errors = validate_market_only_analysis(
        analysis, valid_evidence_refs=set(record["market_input"]["evidence_refs"]),
    )
    assert errors


def test_market_only_input_rejects_unhashable_partition(market_point_factory):
    point = market_point_factory()
    point["partition"] = []
    with pytest.raises(MarketOnlyError, match="PARTITION_INVALID"):
        build_market_only_input(point)


def test_offline_stub_is_deterministic_and_has_zero_external_calls_or_orders(
    market_point_factory, monkeypatch,
):
    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("OFFLINE_MARKET_ONLY_ATTEMPTED_NETWORK")

    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_network)
    points = [market_point_factory()]
    first = run_offline_stub(points)
    second = run_offline_stub(points)

    assert first == second
    assert first["metrics"]["model_calls_used"] == 0
    assert first["metrics"]["orders_created"] == 0
    assert first["metrics"]["eligible_proposals_created"] == 0
    assert first["metrics"]["valid_analysis_records"] == 1
    assert first["metrics"]["action_counts"] == {"WAIT": 1}
    assert first["metrics"]["decision_count_denominator"] == 1
    assert first["records"][0]["analysis_source"] == "OFFLINE_STUB_FIXTURE"
    assert first["records"][0]["authority"]["net_rr_verified"] is False


def test_review_revalidates_market_input_analysis_and_lifecycle(market_point_factory):
    result = run_offline_stub([market_point_factory(), market_point_factory(
        decision_id="v38-fixture-btc-20251016-1200z",
    )])
    summary = review_market_only_records(result["records"])
    assert summary == result["metrics"]
    assert summary["decision_count_denominator"] == 2
    assert summary["valid_analysis_records"] == 2

    tampered = deepcopy(result["records"])
    tampered[0]["market_input"]["provenance"]["account"] = "fabricated"
    rejected = review_market_only_records(tampered)
    assert rejected["invalid_or_unverified_records"] == 1
    assert rejected["market_quality_claim_permitted"] is False


def test_normal_v36_a1_a2_remain_fail_closed_without_account_economics(market_point_factory):
    point = market_point_factory()
    result = run_study([point])
    experiments = result["decision_records"][0]["experiments"]

    assert experiments["A0"]["status"] == "NOT_RUN_NO_EXACT_CACHE"
    assert experiments["A1"]["status"] == "NOT_RUN_STATE_SNAPSHOT_MISSING"
    assert experiments["A2"]["status"] == "NOT_RUN_STATE_SNAPSHOT_MISSING"
    assert result["run_manifest"]["model_calls_used"] == 0
    assert result["decision_records"][0]["lifecycle"] == {
        "proposal": "NOT_OBSERVED",
        "gateway_acceptance": "NOT_OBSERVED",
        "venue_fill": "NOT_OBSERVED",
        "complete_close": "NOT_OBSERVED",
    }
