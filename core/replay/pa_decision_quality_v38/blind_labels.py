"""Blinded human price-action reference-label contracts for V38 research."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.replay.pa_decision_quality_v38.dataset import canonical_sha256
from core.replay.pa_decision_quality_v38.market_only import (
    validate_stored_market_only_input,
)

ROOT = Path(__file__).resolve().parents[3]
PROTOCOL_PATH = ROOT / "configs" / "research" / "labels" / "v38-pa-reference-label-protocol-v2.json"
PROTOCOL_ID = "V38_PA_REFERENCE_BLIND_LABELS_20261009_V2"
PROTOCOL_SCHEMA_VERSION = "pa-market-only-v38/blind-reference-protocol-2"
PROTOCOL_SHA256 = "14827250890d37e33fc0b25121ca0414065e9e835c6beef830c95ed2a3fda64c"

RECORD_FIELDS = {
    "schema_version", "protocol_id", "protocol_sha256", "label_id", "dataset_id",
    "decision_id", "dataset_manifest_sha256", "market_input_sha256", "partition", "decision_time", "reviewer_id",
    "labelled_at", "blinding", "label",
}
LABEL_FIELDS = {
    "context_regime", "higher_timeframe_bias", "location", "signal_setup", "signal_quality",
    "supported_market_bias", "wait_reference", "target_structure", "evidence_refs",
    "target_evidence_refs", "counter_evidence_refs", "rationale",
}
BLINDING_FIELDS = {
    "model_output_visible", "future_outcomes_visible", "experiment_arm_visible",
    "untouched_test_payload_visible",
}
TARGET_WITH_EVIDENCE = {"PRIOR_SWING", "RANGE_EXTREME", "TREND_MEASURED_MOVE"}
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class BlindLabelError(ValueError):
    """Stable failure code for a malformed or unsafe blind reference annotation."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def load_blind_label_protocol(path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Load the hash-frozen label protocol; changed definitions fail closed."""
    try:
        protocol = json.loads((path or PROTOCOL_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BlindLabelError("BLIND_LABEL_PROTOCOL_UNAVAILABLE") from exc
    if not isinstance(protocol, dict):
        raise BlindLabelError("BLIND_LABEL_PROTOCOL_INVALID")
    digest = canonical_sha256(protocol)
    if (protocol.get("schema_version") != PROTOCOL_SCHEMA_VERSION
            or protocol.get("protocol_id") != PROTOCOL_ID):
        raise BlindLabelError("BLIND_LABEL_PROTOCOL_INVALID")
    if digest != PROTOCOL_SHA256:
        raise BlindLabelError("BLIND_LABEL_PROTOCOL_HASH_MISMATCH")
    eligible = protocol.get("eligible_datasets")
    bindings = protocol.get("eligible_input_bindings")
    if eligible != ["V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"] or not isinstance(bindings, dict) or set(bindings) != set(eligible):
        raise BlindLabelError("BLIND_LABEL_INPUT_BINDINGS_INVALID")
    rows = bindings[eligible[0]]
    if not isinstance(rows, list) or len(rows) != 54:
        raise BlindLabelError("BLIND_LABEL_INPUT_BINDINGS_INVALID")
    decision_ids: list[str] = []
    partition_counts = {"optimization": 0, "validation": 0}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"decision_id", "market_input_sha256", "partition"}
                or not isinstance(row.get("decision_id"), str) or not _TOKEN.fullmatch(row["decision_id"])
                or not isinstance(row.get("market_input_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["market_input_sha256"])
                or row.get("partition") not in partition_counts):
            raise BlindLabelError("BLIND_LABEL_INPUT_BINDINGS_INVALID")
        decision_ids.append(row["decision_id"])
        partition_counts[row["partition"]] += 1
    if len(set(decision_ids)) != 54 or decision_ids != sorted(decision_ids) or partition_counts != {"optimization": 36, "validation": 18}:
        raise BlindLabelError("BLIND_LABEL_INPUT_BINDINGS_INVALID")
    return protocol, digest


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _unique_refs(value: Any, valid_refs: set[str], *, required: bool) -> bool:
    return (
        isinstance(value, list)
        and (bool(value) or not required)
        and all(isinstance(ref, str) and ref in valid_refs for ref in value)
        and len(value) == len(set(value))
    )


def validate_blind_label_record(record: Any, market_input: Any) -> list[str]:
    """Validate one independent annotation against its exact causal market input.

    This validates annotation data only. It has no model, provider, exchange, or
    order interface and never computes a consensus label.
    """
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["BLIND_LABEL_RECORD_INVALID"]
    try:
        protocol, digest = load_blind_label_protocol()
    except BlindLabelError as exc:
        return [exc.code]
    if not validate_stored_market_only_input(market_input):
        errors.append("BLIND_LABEL_MARKET_INPUT_INVALID")
        market_input = {}
    if set(record) != RECORD_FIELDS:
        errors.append("BLIND_LABEL_RECORD_FIELDS_INVALID")
    if record.get("schema_version") != "pa-market-only-v38/blind-reference-label-1":
        errors.append("BLIND_LABEL_SCHEMA_VERSION_INVALID")
    if record.get("protocol_id") != protocol["protocol_id"] or record.get("protocol_sha256") != digest:
        errors.append("BLIND_LABEL_PROTOCOL_BINDING_INVALID")
    dataset_id = record.get("dataset_id")
    if dataset_id not in protocol.get("eligible_datasets", []):
        errors.append("BLIND_LABEL_DATASET_INVALID")
    else:
        expected_manifest = protocol.get("eligible_dataset_manifest_sha256", {}).get(dataset_id)
        if record.get("dataset_manifest_sha256") != expected_manifest:
            errors.append("BLIND_LABEL_DATASET_MANIFEST_MISMATCH")
        rows = protocol.get("eligible_input_bindings", {}).get(dataset_id, [])
        by_decision_id = {
            row["decision_id"]: row for row in rows
            if isinstance(row, dict) and isinstance(row.get("decision_id"), str)
        }
        record_decision_id = record.get("decision_id")
        binding = by_decision_id.get(record_decision_id) if isinstance(record_decision_id, str) else None
        if (binding is None
                or record.get("market_input_sha256") != binding.get("market_input_sha256")
                or market_input.get("market_input_sha256") != binding.get("market_input_sha256")
                or record.get("partition") != binding.get("partition")):
            errors.append("BLIND_LABEL_INPUT_NOT_IN_REGISTERED_DATASET")

    partition = record.get("partition")
    if partition in protocol.get("forbidden_partitions", []):
        errors.append("BLIND_LABEL_TEST_PARTITION_FORBIDDEN")
    elif partition not in protocol.get("allowed_partitions", []):
        errors.append("BLIND_LABEL_PARTITION_INVALID")
    if (record.get("partition") != market_input.get("partition")
            or record.get("decision_id") != market_input.get("decision_id")
            or record.get("market_input_sha256") != market_input.get("market_input_sha256")
            or record.get("decision_time") != market_input.get("decision_time")):
        errors.append("BLIND_LABEL_INPUT_BINDING_MISMATCH")

    for field in ("label_id", "decision_id", "reviewer_id"):
        value = record.get(field)
        if not isinstance(value, str) or not _TOKEN.fullmatch(value):
            errors.append(f"BLIND_LABEL_{field.upper()}_INVALID")
    if record.get("reviewer_id") and not isinstance(record.get("reviewer_id"), str):
        errors.append("BLIND_LABEL_REVIEWER_ID_INVALID")

    decision_time = _utc(record.get("decision_time"))
    labelled_at = _utc(record.get("labelled_at"))
    if decision_time is None or labelled_at is None:
        errors.append("BLIND_LABEL_TIME_INVALID")
    elif labelled_at < decision_time:
        errors.append("BLIND_LABEL_TIMESTAMP_PRECEDES_DECISION")

    if record.get("blinding") != {
        "model_output_visible": False,
        "future_outcomes_visible": False,
        "experiment_arm_visible": False,
        "untouched_test_payload_visible": False,
    }:
        errors.append("BLIND_LABEL_BLINDNESS_CONTRACT_INVALID")

    label = record.get("label")
    if not isinstance(label, dict) or set(label) != LABEL_FIELDS:
        errors.append("BLIND_LABEL_FIELDS_INVALID")
        return sorted(set(errors))
    enum_fields = (
        "context_regime", "higher_timeframe_bias", "location", "signal_setup",
        "signal_quality", "supported_market_bias", "wait_reference", "target_structure",
    )
    label_contract = protocol.get("label_fields", {})
    for field in enum_fields:
        allowed = label_contract.get(field)
        if not isinstance(allowed, list) or label.get(field) not in allowed:
            errors.append(f"BLIND_LABEL_{field.upper()}_INVALID")

    valid_refs = set(market_input.get("evidence_refs", []))
    if not _unique_refs(label.get("evidence_refs"), valid_refs, required=True):
        errors.append("BLIND_LABEL_EVIDENCE_REFS_INVALID")
    for field, required in (("target_evidence_refs", False), ("counter_evidence_refs", False)):
        if not _unique_refs(label.get(field), valid_refs, required=required):
            errors.append(f"BLIND_LABEL_{field.upper()}_INVALID")
    if (label.get("target_structure") in TARGET_WITH_EVIDENCE
            and not label.get("target_evidence_refs")):
        errors.append("BLIND_LABEL_TARGET_EVIDENCE_REQUIRED")
    raw_label_refs = label.get("evidence_refs")
    all_label_refs = (
        set(raw_label_refs)
        if isinstance(raw_label_refs, list) and all(isinstance(ref, str) for ref in raw_label_refs)
        else set()
    )
    for field in ("target_evidence_refs", "counter_evidence_refs"):
        refs = label.get(field)
        if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs) and not set(refs) <= all_label_refs:
            errors.append("BLIND_LABEL_FIELD_EVIDENCE_NOT_IN_LABEL_REFS")

    rationale = label.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
        errors.append("BLIND_LABEL_RATIONALE_INVALID")
    return sorted(set(errors))


def validate_blind_label_batch(
    records: Any,
    market_inputs_by_decision: dict[str, dict[str, Any]],
    *,
    required_decision_ids: set[str] | None = None,
) -> list[str]:
    """Validate independent coverage while preserving every rater's disagreement."""
    if not isinstance(records, list) or not isinstance(market_inputs_by_decision, dict):
        return ["BLIND_LABEL_BATCH_INVALID"]
    errors: list[str] = []
    seen_label_ids: set[str] = set()
    raters_by_decision: dict[str, set[str]] = {}
    for index, record in enumerate(records):
        decision_id = record.get("decision_id") if isinstance(record, dict) else None
        market_input = market_inputs_by_decision.get(decision_id) if isinstance(decision_id, str) else None
        row_errors = validate_blind_label_record(record, market_input)
        errors.extend(f"ROW_{index}:{code}" for code in row_errors)
        if not isinstance(record, dict):
            continue
        label_id = record.get("label_id")
        reviewer_id = record.get("reviewer_id")
        if isinstance(label_id, str):
            if label_id in seen_label_ids:
                errors.append(f"ROW_{index}:BLIND_LABEL_DUPLICATE_LABEL_ID")
            seen_label_ids.add(label_id)
        if isinstance(decision_id, str) and isinstance(reviewer_id, str):
            raters = raters_by_decision.setdefault(decision_id, set())
            if reviewer_id in raters:
                errors.append(f"ROW_{index}:BLIND_LABEL_DUPLICATE_RATER_FOR_DECISION")
            raters.add(reviewer_id)

    required = required_decision_ids if required_decision_ids is not None else set(raters_by_decision)
    minimum = 2
    try:
        protocol, _ = load_blind_label_protocol()
        minimum = protocol["rater_controls"]["minimum_independent_raters_per_context"]
    except (BlindLabelError, KeyError, TypeError):
        errors.append("BLIND_LABEL_PROTOCOL_INVALID")
    for decision_id in sorted(required):
        if len(raters_by_decision.get(decision_id, set())) < minimum:
            errors.append(f"{decision_id}:BLIND_LABEL_RATER_COVERAGE_INCOMPLETE")
    return sorted(set(errors))
