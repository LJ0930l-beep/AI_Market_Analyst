"""Preregistered, offline agreement summaries for validated V38 blind labels."""
from __future__ import annotations

from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from core.replay.pa_decision_quality_v38.blind_label_packets import (
    DATASET_ID,
    RESULT_SCHEMA_VERSION,
    load_v38_visible_dataset,
)
from core.replay.pa_decision_quality_v38.blind_labels import (
    BlindLabelError,
    load_blind_label_protocol,
    validate_blind_label_batch,
)
from core.replay.pa_decision_quality_v38.dataset import canonical_sha256

METRICS_SCHEMA_VERSION = "pa-market-only-v38/blind-reference-metrics-1"
METRIC_POLICY_ID = "V38_BLIND_LABEL_AGREEMENT_METRICS_V1"
CATEGORICAL_FIELDS = (
    "context_regime",
    "higher_timeframe_bias",
    "location",
    "signal_setup",
    "signal_quality",
    "supported_market_bias",
    "wait_reference",
    "target_structure",
)
REFERENCE_FIELDS = (
    "evidence_refs",
    "target_evidence_refs",
    "counter_evidence_refs",
)
_BATCH_FIELDS = {
    "schema_version", "protocol_id", "protocol_sha256", "dataset_id",
    "dataset_manifest_sha256", "rater_count", "decision_count",
    "annotation_count", "validation_status", "annotations",
}


class BlindLabelMetricsError(ValueError):
    """Stable rejection code for an invalid, stale, or incomplete label batch."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise BlindLabelMetricsError(code)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 12)


def _not_estimable(name: str, reason: str, **details: float | None) -> dict[str, Any]:
    return {
        "name": name,
        "status": "NOT_ESTIMABLE",
        "value": None,
        "reason": reason,
        **details,
    }


def _cohen_kappa(
    decisions: list[str],
    rater_a: str,
    rater_b: str,
    labels_by_decision: dict[str, dict[str, str]],
    categories: tuple[str, ...],
) -> dict[str, Any]:
    name = "COHEN_KAPPA"
    count_a = Counter(labels_by_decision[decision][rater_a] for decision in decisions)
    count_b = Counter(labels_by_decision[decision][rater_b] for decision in decisions)
    denominator = len(decisions)
    if denominator < 2:
        return _not_estimable(name, "INSUFFICIENT_DECISIONS")
    observed_count = sum(
        labels_by_decision[decision][rater_a] == labels_by_decision[decision][rater_b]
        for decision in decisions
    )
    observed = observed_count / denominator
    expected = sum(count_a[value] * count_b[value] for value in categories) / (denominator * denominator)
    if 1.0 - expected <= 1e-15:
        return _not_estimable(
            name,
            "SINGLE_CLASS_MARGINALS",
            observed_agreement=round(observed, 12),
            expected_agreement=round(expected, 12),
        )
    value = (observed - expected) / (1.0 - expected)
    return {
        "name": name,
        "status": "ESTIMABLE",
        "value": round(value, 12),
        "observed_agreement": round(observed, 12),
        "expected_agreement": round(expected, 12),
    }


def _krippendorff_alpha_nominal(
    decisions: list[str],
    raters: list[str],
    labels_by_decision: dict[str, dict[str, str]],
    categories: tuple[str, ...],
) -> dict[str, Any]:
    name = "KRIPPENDORFF_ALPHA_NOMINAL"
    pairs_per_decision = len(raters) * (len(raters) - 1) // 2
    possible_pairs = len(decisions) * pairs_per_decision
    total_ratings = len(decisions) * len(raters)
    if len(raters) < 2 or len(decisions) < 2 or possible_pairs == 0:
        return _not_estimable(name, "INSUFFICIENT_RATINGS")

    disagreement_count = 0
    for decision in decisions:
        values = [labels_by_decision[decision][rater] for rater in raters]
        disagreement_count += sum(left != right for left, right in combinations(values, 2))
    observed_disagreement = disagreement_count / possible_pairs

    pooled = Counter(
        labels_by_decision[decision][rater]
        for decision in decisions
        for rater in raters
    )
    if total_ratings < 2:
        return _not_estimable(name, "INSUFFICIENT_RATINGS")
    expected_disagreement = sum(
        pooled[value] * (total_ratings - pooled[value]) for value in categories
    ) / (total_ratings * (total_ratings - 1))
    if expected_disagreement <= 1e-15:
        return _not_estimable(
            name,
            "SINGLE_CLASS_MARGINALS",
            observed_disagreement=round(observed_disagreement, 12),
            expected_disagreement=round(expected_disagreement, 12),
        )
    alpha = 1.0 - observed_disagreement / expected_disagreement
    return {
        "name": name,
        "status": "ESTIMABLE",
        "value": round(alpha, 12),
        "observed_disagreement": round(observed_disagreement, 12),
        "expected_disagreement": round(expected_disagreement, 12),
    }


def _field_metrics(
    field: str,
    decisions: list[str],
    raters: list[str],
    labels_by_decision: dict[str, dict[str, str]],
    categories: tuple[str, ...],
) -> dict[str, Any]:
    raters_by_pair = list(combinations(raters, 2))
    pairwise: dict[str, dict[str, Any]] = {}
    matching_pair_count = 0
    possible_pair_count = len(decisions) * len(raters_by_pair)
    unanimous_count = 0
    for rater_a, rater_b in raters_by_pair:
        matched = sum(
            labels_by_decision[decision][rater_a] == labels_by_decision[decision][rater_b]
            for decision in decisions
        )
        pair_key = f"{rater_a}|{rater_b}"
        pairwise[pair_key] = {
            "matching_decisions": matched,
            "decision_count": len(decisions),
            "raw_agreement": _rate(matched, len(decisions)),
        }
        matching_pair_count += matched

    for decision in decisions:
        values = {labels_by_decision[decision][rater] for rater in raters}
        unanimous_count += len(values) == 1

    prevalence_by_rater: dict[str, dict[str, dict[str, int | float | None]]] = {}
    pooled = Counter(
        labels_by_decision[decision][rater]
        for decision in decisions
        for rater in raters
    )
    for rater in raters:
        counts = Counter(labels_by_decision[decision][rater] for decision in decisions)
        prevalence_by_rater[rater] = {
            category: {
                "count": counts[category],
                "fraction": _rate(counts[category], len(decisions)),
            }
            for category in categories
        }

    rating_count = len(decisions) * len(raters)
    unknown_count = pooled["UNKNOWN"]
    if len(raters) == 2:
        coefficient = _cohen_kappa(
            decisions, raters[0], raters[1], labels_by_decision, categories,
        )
        raw_agreement_name = "COHEN_PAIR_RAW_AGREEMENT"
    else:
        coefficient = _krippendorff_alpha_nominal(
            decisions, raters, labels_by_decision, categories,
        )
        raw_agreement_name = "ALL_RATER_PAIR_RAW_AGREEMENT"

    return {
        "field": field,
        "decision_count": len(decisions),
        "rater_count": len(raters),
        "rating_count": rating_count,
        "agreement_coefficient": coefficient,
        "raw_agreement": {
            "definition": raw_agreement_name,
            "matching_pairs": matching_pair_count,
            "possible_pairs": possible_pair_count,
            "fraction": _rate(matching_pair_count, possible_pair_count),
        },
        "unanimous_decision_count": unanimous_count,
        "unanimous_decision_fraction": _rate(unanimous_count, len(decisions)),
        "class_counts_all_raters": {
            category: pooled[category] for category in categories
        },
        "class_prevalence_by_rater": prevalence_by_rater,
        "unknown_count": unknown_count,
        "unknown_fraction": _rate(unknown_count, rating_count),
        "pairwise_raw_agreement_by_rater_pair": pairwise,
    }


def summarize_v38_blind_label_batch(
    dataset_directory: Path,
    batch: Any,
) -> dict[str, Any]:
    """Revalidate one imported label batch, then compute only preregistered metrics.

    The returned summary never chooses a consensus label and does not estimate
    model quality, trading performance, or any outcome unavailable in V38 data.
    """
    if not isinstance(batch, dict) or set(batch) != _BATCH_FIELDS:
        raise BlindLabelMetricsError("V38_LABEL_METRICS_BATCH_FIELDS_INVALID")

    try:
        protocol, protocol_sha = load_blind_label_protocol()
        _manifest, _inputs, points_by_id, market_inputs, manifest_sha, _visible_sha = (
            load_v38_visible_dataset(dataset_directory)
        )
    except (BlindLabelError, OSError, TypeError, ValueError) as exc:
        raise BlindLabelMetricsError("V38_LABEL_METRICS_INPUTS_INVALID") from exc

    expected_decisions = set(points_by_id)
    records = batch.get("annotations")
    _require(
        batch.get("schema_version") == RESULT_SCHEMA_VERSION
        and batch.get("validation_status") == "VALIDATED_MINIMUM_RATER_COVERAGE"
        and batch.get("protocol_id") == protocol["protocol_id"]
        and batch.get("protocol_sha256") == protocol_sha
        and batch.get("dataset_id") == DATASET_ID
        and batch.get("dataset_manifest_sha256") == manifest_sha,
        "V38_LABEL_METRICS_BATCH_BINDING_INVALID",
    )
    _require(
        isinstance(records, list)
        and type(batch.get("decision_count")) is int
        and batch["decision_count"] == len(expected_decisions)
        and type(batch.get("annotation_count")) is int
        and batch["annotation_count"] == len(records)
        and len(records) > 0,
        "V38_LABEL_METRICS_BATCH_COUNTS_INVALID",
    )

    records_by_rater: dict[str, set[str]] = {}
    labels_by_decision: dict[str, dict[str, dict[str, Any]]] = {
        decision_id: {} for decision_id in expected_decisions
    }
    for record in records:
        if not isinstance(record, dict):
            raise BlindLabelMetricsError("V38_LABEL_METRICS_ANNOTATION_INVALID")
        rater = record.get("reviewer_id")
        decision = record.get("decision_id")
        label = record.get("label")
        if (not isinstance(rater, str) or not isinstance(decision, str)
                or decision not in expected_decisions or not isinstance(label, dict)):
            raise BlindLabelMetricsError("V38_LABEL_METRICS_ANNOTATION_INVALID")
        rater_decisions = records_by_rater.setdefault(rater, set())
        if decision in rater_decisions or rater in labels_by_decision[decision]:
            raise BlindLabelMetricsError("V38_LABEL_METRICS_DUPLICATE_ANNOTATION")
        rater_decisions.add(decision)
        # Detailed enum and evidence membership checks follow below using the
        # frozen source input, before these values enter the metric matrix.
        labels_by_decision[decision][rater] = label

    minimum_raters = protocol["rater_controls"]["minimum_independent_raters_per_context"]
    raters = sorted(records_by_rater)
    _require(
        type(batch.get("rater_count")) is int
        and batch["rater_count"] == len(raters)
        and len(raters) >= minimum_raters
        and all(records_by_rater[rater] == expected_decisions for rater in raters),
        "V38_LABEL_METRICS_RATER_COVERAGE_INVALID",
    )
    _require(
        all(set(labels_by_decision[decision]) == set(raters) for decision in expected_decisions),
        "V38_LABEL_METRICS_DECISION_COVERAGE_INVALID",
    )

    validation_errors = validate_blind_label_batch(
        records, market_inputs, required_decision_ids=expected_decisions,
    )
    _require(not validation_errors, "V38_LABEL_METRICS_LABEL_VALIDATION_FAILED")

    decisions = sorted(expected_decisions)
    agreement_by_field: dict[str, dict[str, Any]] = {}
    for field in CATEGORICAL_FIELDS:
        allowed = protocol["label_fields"].get(field)
        _require(
            isinstance(allowed, list) and allowed
            and all(isinstance(value, str) for value in allowed),
            "V38_LABEL_METRICS_PROTOCOL_INVALID",
        )
        categories = tuple(sorted(set(allowed)))
        field_matrix = {
            decision: {
                rater: labels_by_decision[decision][rater][field]
                for rater in raters
            }
            for decision in decisions
        }
        agreement_by_field[field] = _field_metrics(
            field, decisions, raters, field_matrix, categories,
        )

    references_by_role = {
        field: sum(
            len(record["label"][field])
            for record in records
        )
        for field in REFERENCE_FIELDS
    }
    reference_count = sum(references_by_role.values())
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "metric_policy_id": METRIC_POLICY_ID,
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_sha,
        "dataset_id": DATASET_ID,
        "dataset_manifest_sha256": manifest_sha,
        "source_label_batch_sha256": canonical_sha256(batch),
        "decision_count": len(decisions),
        "rater_count": len(raters),
        "annotation_count": len(records),
        "agreement_by_field": agreement_by_field,
        "evidence_reference_integrity": {
            "status": "VALIDATED",
            "reference_occurrences_by_role": references_by_role,
            "reference_occurrences": reference_count,
            "invalid_reference_occurrences": 0,
            "invalid_reference_fraction": _rate(0, reference_count),
        },
        "unknown_coverage": {
            field: {
                "unknown_ratings": metrics["unknown_count"],
                "all_ratings": metrics["rating_count"],
                "fraction": metrics["unknown_fraction"],
            }
            for field, metrics in agreement_by_field.items()
        },
        "consensus_labels_created": False,
        "decision_quality_status": "NOT_ESTIMATED_NO_MODEL_DECISIONS",
        "trading_performance_status": "NOT_ESTIMABLE_FROM_MARKET_LABELS",
    }
