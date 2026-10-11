from __future__ import annotations

import copy

import pytest

from core.replay.pa_decision_quality_v38 import blind_label_metrics as metrics
from core.replay.pa_decision_quality_v38.blind_label_metrics import (
    BlindLabelMetricsError,
    _field_metrics,
    summarize_v38_blind_label_batch,
)
from core.replay.pa_decision_quality_v38.blind_label_packets import (
    DATASET_ID,
    RESULT_SCHEMA_VERSION,
)


def _matrix(records: dict[str, tuple[str, ...]], rater_ids: tuple[str, ...]):
    return {
        decision: dict(zip(rater_ids, ratings, strict=True))
        for decision, ratings in records.items()
    }


def test_two_rater_metrics_report_cohen_kappa_unknown_and_prevalence():
    decision_ids = ["d1", "d2", "d3"]
    raters = ["rater_a", "rater_b"]
    labels = _matrix({
        "d1": ("A", "A"),
        "d2": ("B", "A"),
        "d3": ("UNKNOWN", "UNKNOWN"),
    }, tuple(raters))

    result = _field_metrics(
        "context_regime", decision_ids, raters, labels, ("A", "B", "UNKNOWN"),
    )

    assert result["agreement_coefficient"] == {
        "name": "COHEN_KAPPA",
        "status": "ESTIMABLE",
        "value": 0.5,
        "observed_agreement": 0.666666666667,
        "expected_agreement": 0.333333333333,
    }
    assert result["raw_agreement"]["matching_pairs"] == 2
    assert result["raw_agreement"]["possible_pairs"] == 3
    assert result["unknown_count"] == 2
    assert result["unknown_fraction"] == 0.333333333333
    assert result["class_prevalence_by_rater"]["rater_a"]["B"] == {
        "count": 1, "fraction": 0.333333333333,
    }
    assert result["unanimous_decision_count"] == 2


def test_single_class_cohen_kappa_is_not_estimable_not_perfect():
    decision_ids = ["d1", "d2", "d3"]
    raters = ["rater_a", "rater_b"]
    labels = _matrix({
        decision: ("UNKNOWN", "UNKNOWN") for decision in decision_ids
    }, tuple(raters))

    result = _field_metrics(
        "context_regime", decision_ids, raters, labels, ("A", "B", "UNKNOWN"),
    )

    assert result["raw_agreement"]["fraction"] == 1.0
    assert result["agreement_coefficient"]["status"] == "NOT_ESTIMABLE"
    assert result["agreement_coefficient"]["value"] is None
    assert result["agreement_coefficient"]["reason"] == "SINGLE_CLASS_MARGINALS"


def test_three_rater_metrics_use_nominal_krippendorff_alpha_and_pairwise_raw():
    decision_ids = ["d1", "d2"]
    raters = ["rater_a", "rater_b", "rater_c"]
    labels = _matrix({
        "d1": ("A", "A", "B"),
        "d2": ("B", "A", "B"),
    }, tuple(raters))

    result = _field_metrics(
        "context_regime", decision_ids, raters, labels, ("A", "B", "UNKNOWN"),
    )

    assert result["agreement_coefficient"] == {
        "name": "KRIPPENDORFF_ALPHA_NOMINAL",
        "status": "ESTIMABLE",
        "value": -0.111111111111,
        "observed_disagreement": 0.666666666667,
        "expected_disagreement": 0.6,
    }
    assert result["raw_agreement"] == {
        "definition": "ALL_RATER_PAIR_RAW_AGREEMENT",
        "matching_pairs": 2,
        "possible_pairs": 6,
        "fraction": 0.333333333333,
    }
    assert result["unanimous_decision_count"] == 0
    assert len(result["pairwise_raw_agreement_by_rater_pair"]) == 3


def _fixture_batch():
    protocol_id = "TEST_PROTOCOL"
    protocol_sha = "a" * 64
    manifest_sha = "b" * 64
    decision_ids = ["decision-1", "decision-2", "decision-3"]
    raters = ["reviewer_a", "reviewer_b"]
    records = []
    for decision_index, decision in enumerate(decision_ids):
        for rater_index, rater in enumerate(raters):
            category = ("A", "B", "UNKNOWN")[(decision_index + rater_index) % 3]
            label = {
                field: category for field in metrics.CATEGORICAL_FIELDS
            }
            label.update({
                "evidence_refs": [f"bar:{decision}"],
                "target_evidence_refs": [],
                "counter_evidence_refs": [],
                "rationale": "Synthetic fixture label for metric formula testing.",
            })
            records.append({
                "schema_version": "pa-market-only-v38/blind-reference-label-1",
                "protocol_id": protocol_id,
                "protocol_sha256": protocol_sha,
                "label_id": f"label-{rater}-{decision}",
                "dataset_id": DATASET_ID,
                "decision_id": decision,
                "dataset_manifest_sha256": manifest_sha,
                "market_input_sha256": "c" * 64,
                "partition": "optimization" if decision_index < 2 else "validation",
                "decision_time": "2026-01-01T00:00:00Z",
                "reviewer_id": rater,
                "labelled_at": "2026-10-09T00:00:00Z",
                "blinding": {
                    "model_output_visible": False,
                    "future_outcomes_visible": False,
                    "experiment_arm_visible": False,
                    "untouched_test_payload_visible": False,
                },
                "label": label,
            })
    batch = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "protocol_id": protocol_id,
        "protocol_sha256": protocol_sha,
        "dataset_id": DATASET_ID,
        "dataset_manifest_sha256": manifest_sha,
        "rater_count": len(raters),
        "decision_count": len(decision_ids),
        "annotation_count": len(records),
        "validation_status": "VALIDATED_MINIMUM_RATER_COVERAGE",
        "annotations": records,
    }
    protocol = {
        "protocol_id": protocol_id,
        "rater_controls": {"minimum_independent_raters_per_context": 2},
        "label_fields": {
            field: ["A", "B", "UNKNOWN"] for field in metrics.CATEGORICAL_FIELDS
        },
    }
    points = {decision: {} for decision in decision_ids}
    market_inputs = {decision: {} for decision in decision_ids}
    return batch, protocol, protocol_sha, manifest_sha, points, market_inputs


def test_batch_summary_revalidates_bindings_and_emits_no_consensus(monkeypatch, tmp_path):
    batch, protocol, protocol_sha, manifest_sha, points, market_inputs = _fixture_batch()
    monkeypatch.setattr(metrics, "load_blind_label_protocol", lambda: (protocol, protocol_sha))
    monkeypatch.setattr(
        metrics, "load_v38_visible_dataset",
        lambda _path: ({}, {}, points, market_inputs, manifest_sha, "d" * 64),
    )
    monkeypatch.setattr(metrics, "validate_blind_label_batch", lambda *_args, **_kwargs: [])

    result = summarize_v38_blind_label_batch(tmp_path, batch)

    assert result["schema_version"] == metrics.METRICS_SCHEMA_VERSION
    assert result["metric_policy_id"] == "V38_BLIND_LABEL_AGREEMENT_METRICS_V1"
    assert result["decision_count"] == 3 and result["rater_count"] == 2
    assert set(result["agreement_by_field"]) == set(metrics.CATEGORICAL_FIELDS)
    assert result["evidence_reference_integrity"]["status"] == "VALIDATED"
    assert result["decision_quality_status"] == "NOT_ESTIMATED_NO_MODEL_DECISIONS"
    assert result["trading_performance_status"] == "NOT_ESTIMABLE_FROM_MARKET_LABELS"
    assert result["consensus_labels_created"] is False
    assert "consensus_labels" not in result


def test_batch_summary_rejects_stale_binding_incomplete_rater_and_bad_records(
    monkeypatch, tmp_path,
):
    batch, protocol, protocol_sha, manifest_sha, points, market_inputs = _fixture_batch()
    monkeypatch.setattr(metrics, "load_blind_label_protocol", lambda: (protocol, protocol_sha))
    monkeypatch.setattr(
        metrics, "load_v38_visible_dataset",
        lambda _path: ({}, {}, points, market_inputs, manifest_sha, "d" * 64),
    )
    monkeypatch.setattr(metrics, "validate_blind_label_batch", lambda *_args, **_kwargs: [])

    stale = copy.deepcopy(batch)
    stale["dataset_manifest_sha256"] = "0" * 64
    with pytest.raises(BlindLabelMetricsError, match="V38_LABEL_METRICS_BATCH_BINDING_INVALID"):
        summarize_v38_blind_label_batch(tmp_path, stale)

    incomplete = copy.deepcopy(batch)
    incomplete["annotations"].pop()
    incomplete["annotation_count"] -= 1
    with pytest.raises(BlindLabelMetricsError, match="V38_LABEL_METRICS_RATER_COVERAGE_INVALID"):
        summarize_v38_blind_label_batch(tmp_path, incomplete)

    invalid = copy.deepcopy(batch)
    monkeypatch.setattr(
        metrics, "validate_blind_label_batch", lambda *_args, **_kwargs: ["bad evidence"],
    )
    with pytest.raises(BlindLabelMetricsError, match="V38_LABEL_METRICS_LABEL_VALIDATION_FAILED"):
        summarize_v38_blind_label_batch(tmp_path, invalid)
