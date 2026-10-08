from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.replay.pa_decision_quality_v38 import blind_labels
from core.replay.pa_decision_quality_v38.blind_labels import (
    load_blind_label_protocol,
    validate_blind_label_batch,
    validate_blind_label_record,
)
from core.replay.pa_decision_quality_v38.dataset import canonical_sha256
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input

PROTOCOL_ID = "V38_PA_REFERENCE_BLIND_LABELS_20261009_V2"
DATASET_ID = "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"


def make_label(market_input, *, reviewer_id="rater_alpha", label_id="label_alpha"):
    _, digest = blind_labels.load_blind_label_protocol()
    refs = market_input["evidence_refs"]
    return {
        "schema_version": "pa-market-only-v38/blind-reference-label-1",
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": digest,
        "label_id": label_id,
        "dataset_id": DATASET_ID,
        "dataset_manifest_sha256": "1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a",
        "decision_id": market_input["decision_id"],
        "market_input_sha256": market_input["market_input_sha256"],
        "partition": market_input["partition"],
        "decision_time": market_input["decision_time"],
        "reviewer_id": reviewer_id,
        "labelled_at": "2026-10-10T00:00:00Z",
        "blinding": {
            "model_output_visible": False,
            "future_outcomes_visible": False,
            "experiment_arm_visible": False,
            "untouched_test_payload_visible": False,
        },
        "label": {
            "context_regime": "UNCERTAIN",
            "higher_timeframe_bias": "UNCERTAIN",
            "location": "UNKNOWN",
            "signal_setup": "UNKNOWN",
            "signal_quality": "UNKNOWN",
            "supported_market_bias": "UNKNOWN",
            "wait_reference": "UNKNOWN",
            "target_structure": "UNKNOWN",
            "evidence_refs": refs[:2],
            "target_evidence_refs": [],
            "counter_evidence_refs": [],
            "rationale": "Fixture annotation only; uncertainty is retained and no outcome is used.",
        },
    }


@pytest.fixture
def bind_synthetic_input_for_unit_test(monkeypatch):
    """Inject a test-only frozen binding while production always uses the V3 registry."""
    registered, _ = load_blind_label_protocol()

    def bind(market_input):
        protocol = deepcopy(registered)
        rows = protocol["eligible_input_bindings"][DATASET_ID]
        rows.append({
            "decision_id": market_input["decision_id"],
            "market_input_sha256": market_input["market_input_sha256"],
            "partition": market_input["partition"],
        })
        rows.sort(key=lambda row: row["decision_id"])
        digest = canonical_sha256(protocol)
        monkeypatch.setattr(blind_labels, "load_blind_label_protocol", lambda: (protocol, digest))

    return bind


def test_blind_label_protocol_is_hash_frozen_and_keeps_unknown_first_class():
    protocol, digest = load_blind_label_protocol()

    assert len(digest) == 64
    assert protocol["protocol_id"] == PROTOCOL_ID
    assert protocol["authorization"]["labels_generated"] is False
    assert protocol["authorization"]["model_calls_authorized"] is False
    plan_path = Path(__file__).resolve().parents[2] / "configs" / "research" / "experiments" / "v38-market-only-evaluation-v1.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["blind_reference_protocol"]["protocol_sha256"] == digest
    assert plan["blind_reference_protocol"]["eligible_dataset_id"] == DATASET_ID
    assert protocol["schema_version"] == "pa-market-only-v38/blind-reference-protocol-2"
    bindings = protocol["eligible_input_bindings"][DATASET_ID]
    assert len(bindings) == 54
    assert {partition: sum(row["partition"] == partition for row in bindings)
            for partition in ("optimization", "validation")} == {"optimization": 36, "validation": 18}
    assert len({row["decision_id"] for row in bindings}) == 54
    assert all(len(row["market_input_sha256"]) == 64 for row in bindings)
    assert "UNKNOWN" in protocol["label_fields"]["supported_market_bias"]
    assert protocol["rater_controls"]["minimum_independent_raters_per_context"] == 2


def test_valid_reference_annotation_binds_to_causal_input(market_point_factory, bind_synthetic_input_for_unit_test):
    market_input = build_market_only_input(market_point_factory())
    bind_synthetic_input_for_unit_test(market_input)
    record = make_label(market_input)

    assert validate_blind_label_record(record, market_input) == []


def test_untouched_test_cannot_receive_annotations(market_point_factory):
    point = market_point_factory(
        decision=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        partition="untouched_test",
    )
    market_input = build_market_only_input(point)
    record = make_label(market_input)

    assert "BLIND_LABEL_TEST_PARTITION_FORBIDDEN" in validate_blind_label_record(record, market_input)


def test_annotation_rejects_future_available_market_input(market_point_factory):
    market_input = build_market_only_input(market_point_factory())
    record = make_label(market_input)
    tampered_input = deepcopy(market_input)
    tampered_input["data_available_through_by_timeframe"]["5m"] = tampered_input["decision_time"]
    without_hash = {key: value for key, value in tampered_input.items() if key != "market_input_sha256"}
    tampered_input["market_input_sha256"] = canonical_sha256(without_hash)
    record["market_input_sha256"] = tampered_input["market_input_sha256"]

    assert "BLIND_LABEL_MARKET_INPUT_INVALID" in validate_blind_label_record(record, tampered_input)


def test_annotation_rejects_model_outcome_and_unregistered_evidence_fields(market_point_factory):
    market_input = build_market_only_input(market_point_factory())
    record = make_label(market_input)
    record["model_output"] = {"action": "WAIT"}
    record["label"]["target_evidence_refs"] = ["bar:forged"]

    errors = validate_blind_label_record(record, market_input)
    assert "BLIND_LABEL_RECORD_FIELDS_INVALID" in errors
    assert "BLIND_LABEL_TARGET_EVIDENCE_REFS_INVALID" in errors


def test_target_claim_requires_supported_market_evidence(market_point_factory):
    market_input = build_market_only_input(market_point_factory())
    record = make_label(market_input)
    record["label"]["target_structure"] = "PRIOR_SWING"

    assert "BLIND_LABEL_TARGET_EVIDENCE_REQUIRED" in validate_blind_label_record(record, market_input)


def test_two_independent_raters_may_disagree_without_forced_consensus(market_point_factory, bind_synthetic_input_for_unit_test):
    market_input = build_market_only_input(market_point_factory())
    bind_synthetic_input_for_unit_test(market_input)
    first = make_label(market_input, reviewer_id="rater_alpha", label_id="label_alpha")
    second = make_label(market_input, reviewer_id="rater_beta", label_id="label_beta")
    second["label"]["context_regime"] = "TRADING_RANGE"
    second["label"]["supported_market_bias"] = "NEUTRAL"

    errors = validate_blind_label_batch(
        [first, second], {market_input["decision_id"]: market_input},
        required_decision_ids={market_input["decision_id"]},
    )
    assert errors == []
    assert first["label"]["context_regime"] != second["label"]["context_regime"]


def test_batch_rejects_duplicate_rater_and_incomplete_independent_coverage(market_point_factory, bind_synthetic_input_for_unit_test):
    market_input = build_market_only_input(market_point_factory())
    bind_synthetic_input_for_unit_test(market_input)
    first = make_label(market_input, reviewer_id="rater_alpha", label_id="label_alpha")
    duplicate = make_label(market_input, reviewer_id="rater_alpha", label_id="label_beta")

    errors = validate_blind_label_batch(
        [first, duplicate], {market_input["decision_id"]: market_input},
        required_decision_ids={market_input["decision_id"], "missing_decision"},
    )
    assert any("BLIND_LABEL_DUPLICATE_RATER_FOR_DECISION" in error for error in errors)
    assert any("BLIND_LABEL_RATER_COVERAGE_INCOMPLETE" in error for error in errors)


def test_preregistration_freezes_arms_denominators_and_no_call_limit():
    root = Path(__file__).resolve().parents[2]
    path = root / "configs" / "research" / "experiments" / "v38-market-only-evaluation-v1.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    arms = {arm["arm_id"]: arm for arm in plan["arms"]}

    assert set(arms) == {"A1_MARKET_ONLY", "A2_MARKET_ONLY", "A3_FROZEN_RULE", "B0_NEW_BASELINE"}
    assert arms["A1_MARKET_ONLY"]["status"] == "NOT_RUN_NO_MODEL_BUDGET_AUTHORIZATION"
    for arm in (arms["A1_MARKET_ONLY"], arms["A2_MARKET_ONLY"]):
        prompt_path = root / arm["prompt_path"]
        assert hashlib.sha256(prompt_path.read_bytes()).hexdigest() == arm["prompt_file_sha256"]
    assert arms["A2_MARKET_ONLY"]["model_id"] is None
    assert arms["A3_FROZEN_RULE"]["execution_candidate_claim_permitted"] is False
    assert arms["B0_NEW_BASELINE"]["included"] is False
    assert plan["primary_dataset"]["counts"]["visible_contexts_for_future_study"] == 54
    assert plan["primary_dataset"]["counts"]["untouched_test_hash_only"] == 18
    assert len(plan["prior_datasets_for_separate_reporting"]) == 2
    assert all("NOT_POOLED" in item["use"] or "DIAGNOSTIC_ONLY" in item["use"]
               for item in plan["prior_datasets_for_separate_reporting"])
    assert plan["authorization_and_observed_counts"]["paid_model_calls_authorized"] is False
    assert plan["authorization_and_observed_counts"]["gemini_research_calls_used"] == 0
    assert plan["authorization_and_observed_counts"]["codex_capability_probe"]["provider_request_or_billing_status"] == "UNVERIFIED"
    assert all(metric["result"] is None for metric in plan["metrics"])

def test_annotation_must_name_the_frozen_dataset_manifest(market_point_factory, bind_synthetic_input_for_unit_test):
    market_input = build_market_only_input(market_point_factory())
    bind_synthetic_input_for_unit_test(market_input)
    record = make_label(market_input)
    record["dataset_manifest_sha256"] = "0" * 64

    assert "BLIND_LABEL_DATASET_MANIFEST_MISMATCH" in validate_blind_label_record(record, market_input)


def test_predecessor_datasets_cannot_be_used_for_reference_labels(market_point_factory):
    market_input = build_market_only_input(market_point_factory())
    for dataset_id, manifest_sha256 in (
        ("V38_BINANCE_BTC_ETH_MARKET_CONTEXTS_20261009_V1", "ca0786a4acdec7a7410b7df89e947bc2b698377fed69a13fd865fe4d1187bd99"),
        ("V38_BINANCE_BTC_ETH_NONOVERLAP_CONTEXTS_20261009_V1", "1d76f1cd0e2eaa658eb8d32decc434a10e151cb7119a6b2a4f85801639537785"),
    ):
        record = make_label(market_input)
        record["dataset_id"] = dataset_id
        record["dataset_manifest_sha256"] = manifest_sha256

        assert "BLIND_LABEL_DATASET_INVALID" in validate_blind_label_record(record, market_input)


def test_registered_prompt_files_are_non_executable_and_have_zero_call_authority():
    root = Path(__file__).resolve().parents[2]
    for prompt_name in ("v38-market-only-a1-v1.json", "v38-market-only-a2-v1.json"):
        prompt = json.loads((root / "configs" / "research" / "prompts" / prompt_name).read_text(encoding="utf-8"))
        assert prompt["model_selection"]["provider"] is None
        assert prompt["model_selection"]["model_id"] is None
        assert prompt["call_authorization"] == {
            "authorized": False,
            "maximum_calls": 0,
            "maximum_budget_usdt": None,
        }
        assert prompt["output_schema_version"] == "pa-market-only-v38/analysis-1"


def test_synthetic_input_cannot_claim_membership_in_frozen_v3_dataset(market_point_factory):
    market_input = build_market_only_input(market_point_factory())
    record = make_label(market_input)

    assert "BLIND_LABEL_INPUT_NOT_IN_REGISTERED_DATASET" in validate_blind_label_record(record, market_input)


def test_registered_decision_id_with_different_input_hash_is_rejected(market_point_factory):
    protocol, _ = load_blind_label_protocol()
    registered = protocol["eligible_input_bindings"][DATASET_ID][0]
    market_input = build_market_only_input(market_point_factory())
    market_input["decision_id"] = registered["decision_id"]
    without_hash = {key: value for key, value in market_input.items() if key != "market_input_sha256"}
    market_input["market_input_sha256"] = canonical_sha256(without_hash)
    record = make_label(market_input)

    assert "BLIND_LABEL_INPUT_NOT_IN_REGISTERED_DATASET" in validate_blind_label_record(record, market_input)


def test_malformed_decision_id_and_evidence_refs_fail_closed_without_raising(
    market_point_factory, bind_synthetic_input_for_unit_test,
):
    market_input = build_market_only_input(market_point_factory())
    bind_synthetic_input_for_unit_test(market_input)
    record = make_label(market_input)
    record["decision_id"] = {"unhashable": "id"}
    record["label"]["evidence_refs"] = [{"unhashable": "reference"}]

    errors = validate_blind_label_record(record, market_input)

    assert "BLIND_LABEL_DECISION_ID_INVALID" in errors
    assert "BLIND_LABEL_EVIDENCE_REFS_INVALID" in errors
