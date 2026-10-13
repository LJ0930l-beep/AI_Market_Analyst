"""Independent offline audit for the frozen V38 stratified-purged dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.blind_labels import load_blind_label_protocol
from core.replay.pa_decision_quality_v38.dataset import canonical_sha256
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input
from core.replay.pa_decision_quality_v38.stratified_purged_dataset import (
    _verified_archive,
    load_stratified_purged_sample_plan,
    select_stratified_purged_calendar_points,
)


class StratifiedPurgedAuditError(ValueError):
    """Stable failure code for an invalid V38 stratified-purged artifact."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


MANIFEST_FIELDS = frozenset({
    "schema_version", "dataset_id", "plan_sha256", "parent_plan_sha256s",
    "parent_dataset_manifest_sha256s", "base_commit", "source", "partition_counts",
    "temporal_anchor_counts", "contexts_by_symbol_partition", "total_market_contexts",
    "optimization_validation_input_count", "untouched_test_hash_only_count",
    "paired_temporal_anchor_count", "same_symbol_input_window_overlap_count",
    "cross_partition_input_window_overlap_count", "iid_or_independent_trade_claim",
    "price_evidence_grade", "availability_evidence_grade", "availability_delay_seconds",
    "gate_executable_trade_samples", "completed_closes", "gemini_research_calls_used",
    "orders_created", "model_outputs", "outcome_labels_included",
    "untouched_test_payloads_included", "files", "points", "manifest_sha256",
})
INPUT_DOCUMENT_FIELDS = frozenset({
    "schema_version", "dataset_kind", "plan_sha256", "partition_policy_sha256",
    "source_database_sha256", "source_manifest_sha256", "partition_scope",
    "decision_points", "outcome_labels_included", "model_outputs_included",
})
INPUT_POINT_FIELDS = frozenset({
    "decision_id", "decision_time", "symbol", "partition", "bars_by_timeframe",
})
SEALED_DOCUMENT_FIELDS = frozenset({
    "schema_version", "partition", "plan_sha256", "partition_policy_sha256",
    "source_database_sha256", "persist_only_hashes_and_coverage",
    "market_input_payloads_included", "labels_or_evaluation_included", "decision_points",
})
POINT_DESCRIPTOR_FIELDS = frozenset({
    "decision_id", "decision_time", "symbol", "partition", "selection_plan_sha256",
    "selection_key_version", "selection_key_sha256", "stratum_id", "candidate_days",
    "selected_day", "paired_anchor_group_id", "input_window_start", "input_window_end_exclusive",
    "overlaps_previous_input_window", "overlap_cluster_id", "overlap_seconds_with_previous",
    "warmup_crosses_partition_start", "market_input_sha256", "bar_count_by_timeframe",
    "evidence_ref_count", "price_evidence_grade", "availability_evidence_grade",
    "archive_file_sha256s",
})


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise StratifiedPurgedAuditError(code)


def _require_exact_fields(value: Any, expected_fields: frozenset[str], code: str) -> None:
    """Reject undeclared metadata fields that could smuggle labels into a sealed artifact."""
    _require(isinstance(value, dict) and set(value) == expected_fields, code)


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StratifiedPurgedAuditError(code) from exc
    if not isinstance(value, dict):
        raise StratifiedPurgedAuditError(code)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise StratifiedPurgedAuditError("V38_STRATIFIED_PURGED_AUDIT_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StratifiedPurgedAuditError("V38_STRATIFIED_PURGED_AUDIT_TIME_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StratifiedPurgedAuditError("V38_STRATIFIED_PURGED_AUDIT_TIME_INVALID")
    return parsed


def _overlap_summary(descriptors: list[dict[str, Any]]) -> dict[str, int]:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for descriptor in descriptors:
        by_symbol[descriptor["symbol"]].append(descriptor)
    same_symbol_pairs = 0
    cross_partition_pairs = 0
    for rows in by_symbol.values():
        rows.sort(key=lambda row: row["input_window_start"])
        for index, left in enumerate(rows):
            left_end = _utc(left["input_window_end_exclusive"])
            for right in rows[index + 1:]:
                right_start = _utc(right["input_window_start"])
                if right_start >= left_end:
                    break
                same_symbol_pairs += 1
                if left["partition"] != right["partition"]:
                    cross_partition_pairs += 1
    return {
        "same_symbol_overlapping_pairs": same_symbol_pairs,
        "cross_partition_overlapping_pairs": cross_partition_pairs,
    }


def _validate_blind_label_input_bindings(
    protocol: dict[str, Any], descriptors: list[dict[str, Any]], manifest_sha256: str,
) -> int:
    dataset_id = "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"
    _require(protocol.get("eligible_dataset_manifest_sha256", {}).get(dataset_id) == manifest_sha256,
             "V38_STRATIFIED_PURGED_AUDIT_LABEL_MANIFEST_BINDING_MISMATCH")
    rows = protocol.get("eligible_input_bindings", {}).get(dataset_id)
    _require(isinstance(rows, list), "V38_STRATIFIED_PURGED_AUDIT_LABEL_BINDINGS_INVALID")
    registered = {
        row["decision_id"]: (row["market_input_sha256"], row["partition"])
        for row in rows if isinstance(row, dict)
    }
    expected = {
        row["decision_id"]: (row["market_input_sha256"], row["partition"])
        for row in descriptors if row.get("partition") in {"optimization", "validation"}
    }
    _require(len(registered) == 54 and registered == expected,
             "V38_STRATIFIED_PURGED_AUDIT_LABEL_INPUT_BINDINGS_MISMATCH")
    return len(registered)


def audit_stratified_purged_v38_dataset(
    dataset_directory: Path, archive_directory: Path,
) -> dict[str, Any]:
    plan, plan_sha256 = load_stratified_purged_sample_plan()
    dataset_directory = Path(dataset_directory).resolve()
    archive_directory = Path(archive_directory).resolve()
    source_manifest, source_manifest_sha256 = _verified_archive(archive_directory, plan)

    manifest_path = dataset_directory / "dataset-manifest.json"
    manifest = _read_json(manifest_path, "V38_STRATIFIED_PURGED_AUDIT_MANIFEST_INVALID")
    _require_exact_fields(manifest, MANIFEST_FIELDS, "V38_STRATIFIED_PURGED_AUDIT_MANIFEST_FIELDS_INVALID")
    claimed_manifest_hash = manifest.get("manifest_sha256")
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    _require(manifest.get("schema_version") == "pa-market-only-v38/stratified-purged-dataset-manifest-1"
             and canonical_sha256(manifest_body) == claimed_manifest_hash,
             "V38_STRATIFIED_PURGED_AUDIT_MANIFEST_HASH_MISMATCH")
    _require(manifest.get("dataset_id") == plan["plan_id"]
             and manifest.get("plan_sha256") == plan_sha256
             and manifest.get("base_commit") == plan["base_commit"],
             "V38_STRATIFIED_PURGED_AUDIT_PLAN_BINDING_MISMATCH")
    _require(manifest.get("parent_plan_sha256s") == [item["plan_sha256"] for item in plan["parent_plans"]]
             and manifest.get("parent_dataset_manifest_sha256s") == [
                 "ca0786a4acdec7a7410b7df89e947bc2b698377fed69a13fd865fe4d1187bd99",
                 "1d76f1cd0e2eaa658eb8d32decc434a10e151cb7119a6b2a4f85801639537785",
             ], "V38_STRATIFIED_PURGED_AUDIT_PARENT_BINDING_MISMATCH")
    _require(manifest.get("source", {}).get("database_sha256") == source_manifest["dataset_sha256"]
             and manifest.get("source", {}).get("manifest_sha256") == source_manifest_sha256
             and manifest.get("source", {}).get("archive_files_verified") == len(source_manifest["archived_files"])
             and manifest.get("source", {}).get("network_calls") == 0,
             "V38_STRATIFIED_PURGED_AUDIT_SOURCE_BINDING_MISMATCH")
    _require(manifest.get("partition_counts") == {
                 "optimization": 36, "validation": 18, "untouched_test": 18,
             }
             and manifest.get("temporal_anchor_counts") == {
                 "optimization": 18, "validation": 9, "untouched_test": 9,
             }
             and manifest.get("contexts_by_symbol_partition") == {
                 "BTCUSDT:optimization": 18, "BTCUSDT:validation": 9, "BTCUSDT:untouched_test": 9,
                 "ETHUSDT:optimization": 18, "ETHUSDT:validation": 9, "ETHUSDT:untouched_test": 9,
             }
             and manifest.get("total_market_contexts") == 72
             and manifest.get("optimization_validation_input_count") == 54
             and manifest.get("untouched_test_hash_only_count") == 18
             and manifest.get("paired_temporal_anchor_count") == 36
             and manifest.get("same_symbol_input_window_overlap_count") == 0
             and manifest.get("cross_partition_input_window_overlap_count") == 0
             and manifest.get("iid_or_independent_trade_claim") is False
             and manifest.get("outcome_labels_included") is False
             and manifest.get("untouched_test_payloads_included") is False
             and manifest.get("gemini_research_calls_used") == 0
             and manifest.get("orders_created") == 0,
             "V38_STRATIFIED_PURGED_AUDIT_COUNTS_OR_AUTHORITY_INVALID")

    file_hashes = manifest.get("files")
    expected_filenames = {"optimization-validation-inputs.json", "untouched-test-sealed-manifest.json"}
    _require(isinstance(file_hashes, dict) and set(file_hashes) == expected_filenames,
             "V38_STRATIFIED_PURGED_AUDIT_FILE_LIST_INVALID")
    for name, expected_hash in file_hashes.items():
        _require(_sha256_file(dataset_directory / name) == expected_hash,
                 "V38_STRATIFIED_PURGED_AUDIT_OUTPUT_FILE_HASH_MISMATCH")

    inputs = _read_json(dataset_directory / "optimization-validation-inputs.json",
                        "V38_STRATIFIED_PURGED_AUDIT_INPUTS_INVALID")
    _require_exact_fields(inputs, INPUT_DOCUMENT_FIELDS, "V38_STRATIFIED_PURGED_AUDIT_INPUT_FIELDS_INVALID")
    _require(inputs.get("schema_version") == "pa-market-only-v38/stratified-purged-market-input-dataset-1"
             and inputs.get("plan_sha256") == plan_sha256
             and inputs.get("source_database_sha256") == source_manifest["dataset_sha256"]
             and inputs.get("source_manifest_sha256") == source_manifest_sha256
             and inputs.get("partition_scope") == ["optimization", "validation"]
             and inputs.get("outcome_labels_included") is False
             and inputs.get("model_outputs_included") is False,
             "V38_STRATIFIED_PURGED_AUDIT_INPUT_DOCUMENT_INVALID")
    input_points = inputs.get("decision_points")
    _require(isinstance(input_points, list) and len(input_points) == 54,
             "V38_STRATIFIED_PURGED_AUDIT_INPUT_COUNT_INVALID")
    for point in input_points:
        _require_exact_fields(point, INPUT_POINT_FIELDS, "V38_STRATIFIED_PURGED_AUDIT_INPUT_POINT_FIELDS_INVALID")
    visible_ids = {row.get("decision_id") for row in input_points if isinstance(row, dict)}
    expected_visible_ids = {
        row["decision_id"] for row in select_stratified_purged_calendar_points()
        if row["partition"] in {"optimization", "validation"}
    }
    _require(len(visible_ids) == 54 and visible_ids == expected_visible_ids,
             "V38_STRATIFIED_PURGED_AUDIT_VISIBLE_COVERAGE_INVALID")

    sealed = _read_json(dataset_directory / "untouched-test-sealed-manifest.json",
                        "V38_STRATIFIED_PURGED_AUDIT_SEALED_INVALID")
    _require_exact_fields(sealed, SEALED_DOCUMENT_FIELDS, "V38_STRATIFIED_PURGED_AUDIT_SEALED_FIELDS_INVALID")
    _require(sealed.get("schema_version") == "pa-market-only-v38/stratified-purged-untouched-test-sealed-1"
             and sealed.get("partition") == "untouched_test"
             and sealed.get("plan_sha256") == plan_sha256
             and sealed.get("persist_only_hashes_and_coverage") is True
             and sealed.get("market_input_payloads_included") is False
             and sealed.get("labels_or_evaluation_included") is False,
             "V38_STRATIFIED_PURGED_AUDIT_SEALED_DOCUMENT_INVALID")
    sealed_points = sealed.get("decision_points")
    _require(isinstance(sealed_points, list) and len(sealed_points) == 18,
             "V38_STRATIFIED_PURGED_AUDIT_SEALED_COUNT_INVALID")
    for point in sealed_points:
        _require_exact_fields(point, POINT_DESCRIPTOR_FIELDS,
                              "V38_STRATIFIED_PURGED_AUDIT_SEALED_POINT_FIELDS_INVALID")
    sealed_ids = {row.get("decision_id") for row in sealed_points if isinstance(row, dict)}
    expected_sealed_ids = {
        row["decision_id"] for row in select_stratified_purged_calendar_points()
        if row["partition"] == "untouched_test"
    }
    _require(len(sealed_ids) == 18 and sealed_ids == expected_sealed_ids,
             "V38_STRATIFIED_PURGED_AUDIT_SEALED_COVERAGE_INVALID")
    _require(all("bars_by_timeframe" not in row and "market_input" not in row
                 and "label" not in row and "analysis" not in row
                 for row in sealed_points), "V38_STRATIFIED_PURGED_AUDIT_SEALED_PAYLOAD_LEAK")

    descriptors = manifest.get("points")
    _require(isinstance(descriptors, list) and len(descriptors) == 72,
             "V38_STRATIFIED_PURGED_AUDIT_DESCRIPTOR_COUNT_INVALID")
    for descriptor in descriptors:
        _require_exact_fields(descriptor, POINT_DESCRIPTOR_FIELDS,
                              "V38_STRATIFIED_PURGED_AUDIT_DESCRIPTOR_FIELDS_INVALID")
    descriptor_by_id = {row.get("decision_id"): row for row in descriptors if isinstance(row, dict)}
    _require(len(descriptor_by_id) == 72, "V38_STRATIFIED_PURGED_AUDIT_DUPLICATE_DESCRIPTOR")
    selected = select_stratified_purged_calendar_points()
    expected_by_id = {row["decision_id"]: row for row in selected}
    _require(set(descriptor_by_id) == set(expected_by_id),
             "V38_STRATIFIED_PURGED_AUDIT_SELECTION_COVERAGE_INVALID")
    label_protocol, label_protocol_sha256 = load_blind_label_protocol()
    label_binding_count = _validate_blind_label_input_bindings(
        label_protocol, descriptors, claimed_manifest_hash,
    )

    used_archive_hashes: set[str] = set()
    verified_archive_hashes = {
        row["sha256"] for row in source_manifest["archived_files"] if isinstance(row, dict)
    }
    visible_by_id = {}
    for point in input_points:
        if not isinstance(point, dict):
            raise StratifiedPurgedAuditError("V38_STRATIFIED_PURGED_AUDIT_INPUT_POINT_INVALID")
        decision_id = point.get("decision_id")
        expected = expected_by_id.get(decision_id)
        descriptor = descriptor_by_id.get(decision_id)
        _require(expected is not None and descriptor is not None
                 and point.get("partition") in {"optimization", "validation"},
                 "V38_STRATIFIED_PURGED_AUDIT_INPUT_IDENTITY_INVALID")
        for field in ("symbol", "partition", "decision_time"):
            _require(point.get(field) == expected[field] == descriptor[field],
                     "V38_STRATIFIED_PURGED_AUDIT_INPUT_IDENTITY_MISMATCH")
        market_input = build_market_only_input(point)
        _require(market_input["market_input_sha256"] == descriptor.get("market_input_sha256"),
                 "V38_STRATIFIED_PURGED_AUDIT_INPUT_HASH_MISMATCH")
        _require({
            timeframe: len(point["bars_by_timeframe"][timeframe]) for timeframe in plan["data"]["timeframes"]
        } == descriptor.get("bar_count_by_timeframe"),
                 "V38_STRATIFIED_PURGED_AUDIT_BAR_COUNT_MISMATCH")
        _require(len(market_input["evidence_refs"]) == descriptor.get("evidence_ref_count"),
                 "V38_STRATIFIED_PURGED_AUDIT_EVIDENCE_REF_COUNT_MISMATCH")
        hashes = set(market_input["provenance"]["archive_file_sha256s"])
        _require(hashes <= verified_archive_hashes
                 and hashes == set(descriptor.get("archive_file_sha256s", [])),
                 "V38_STRATIFIED_PURGED_AUDIT_ARCHIVE_PROVENANCE_MISMATCH")
        used_archive_hashes.update(hashes)
        visible_by_id[decision_id] = point

    for row in sealed_points:
        _require(isinstance(row, dict), "V38_STRATIFIED_PURGED_AUDIT_SEALED_ROW_INVALID")
        expected = expected_by_id.get(row.get("decision_id"))
        descriptor = descriptor_by_id.get(row.get("decision_id"))
        _require(expected is not None and descriptor is not None
                 and row.get("partition") == "untouched_test"
                 and row.get("partition") == expected["partition"],
                 "V38_STRATIFIED_PURGED_AUDIT_SEALED_IDENTITY_INVALID")
        _require(row == {key: descriptor[key] for key in row},
                 "V38_STRATIFIED_PURGED_AUDIT_SEALED_HASH_OR_COVERAGE_MISMATCH")

    all_descriptors = list(descriptor_by_id.values())
    overlap = _overlap_summary(all_descriptors)
    _require(overlap == {"same_symbol_overlapping_pairs": 0, "cross_partition_overlapping_pairs": 0},
             "V38_STRATIFIED_PURGED_AUDIT_WINDOWS_OVERLAP")
    for descriptor in all_descriptors:
        expected = expected_by_id[descriptor["decision_id"]]
        for field in (
            "selection_plan_sha256", "selection_key_version", "selection_key_sha256", "stratum_id",
            "candidate_days", "selected_day", "paired_anchor_group_id", "input_window_start",
            "input_window_end_exclusive", "overlaps_previous_input_window", "overlap_cluster_id",
            "overlap_seconds_with_previous", "warmup_crosses_partition_start",
        ):
            _require(descriptor.get(field) == expected.get(field),
                     "V38_STRATIFIED_PURGED_AUDIT_SELECTION_DESCRIPTOR_MISMATCH")
        _require(descriptor.get("overlaps_previous_input_window") is False
                 and descriptor.get("overlap_seconds_with_previous") == 0
                 and descriptor.get("warmup_crosses_partition_start") is False,
                 "V38_STRATIFIED_PURGED_AUDIT_OVERLAP_METADATA_INVALID")

    _require(used_archive_hashes <= verified_archive_hashes,
             "V38_STRATIFIED_PURGED_AUDIT_USED_ARCHIVE_UNVERIFIED")
    report: dict[str, Any] = {
        "schema_version": "pa-market-only-v38/stratified-purged-integrity-audit-1",
        "status": "PASS_WITH_EVIDENCE_CAVEATS",
        "dataset_id": plan["plan_id"],
        "plan_sha256": plan_sha256,
        "manifest_sha256": claimed_manifest_hash,
        "blind_label_protocol_sha256": label_protocol_sha256,
        "source": {
            "archive_files_verified": len(source_manifest["archived_files"]),
            "source_database_sha256": source_manifest["dataset_sha256"],
            "source_manifest_sha256": source_manifest_sha256,
            "used_archive_hash_count": len(used_archive_hashes),
            "network_calls": 0,
        },
        "counts": {
            "total_contexts": len(descriptors),
            "optimization_contexts": 36,
            "validation_contexts": 18,
            "untouched_test_hash_only_contexts": 18,
            "visible_inputs_recomputed": len(visible_by_id),
            "paired_anchors": len({row["paired_anchor_group_id"] for row in descriptors}),
            **overlap,
            "gate_executable_samples": 0,
            "completed_closes": 0,
            "labels": 0,
            "gemini_research_calls": 0,
            "orders": 0,
        },
        "evidence": {
            "price_evidence_grade": plan["data"]["price_evidence_grade"],
            "availability_evidence_grade": plan["data"]["availability_evidence_grade"],
            "availability_basis": plan["data"]["availability_basis"],
            "historical_gate_receive_time_verified": False,
            "point_in_time_bid_ask_available": False,
        },
        "checks": {
            "plan_and_parent_hashes_match": True,
            "partition_labels_and_windows_valid": True,
            "all_visible_inputs_rebuilt_and_causal": True,
            "all_referenced_archives_verified": True,
            "untouched_test_payloads_absent": True,
            "same_symbol_and_cross_partition_windows_disjoint": True,
            "no_outcome_labels_or_model_outputs": True,
            "blind_label_registry_matches_visible_manifest": label_binding_count == 54,
            "no_network_calls_or_orders": True,
        },
        "limitations": [
            "Binance UM price reconstruction is not historical Gate price or receive-time evidence.",
            "Availability uses the 60-second bar-end proxy.",
            "Paired assets and separated windows do not make temporal anchors IID trade samples.",
            "There are zero account snapshots, point-in-time bid/ask quotes, proposals, fills, or complete closes.",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-directory", type=Path, required=True)
    parser.add_argument("--archive-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    allowed = (ROOT / "reports" / "v38+" / "verification").resolve()
    output = args.output.resolve()
    try:
        output.relative_to(allowed)
        report = audit_stratified_purged_v38_dataset(args.dataset_directory, args.archive_directory)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        print(json.dumps({"status": "OUTPUT_EXISTS_REFUSE_OVERWRITE"}))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "AUDIT_FAILED", "error_code": getattr(exc, "code", "V38_STRATIFIED_PURGED_AUDIT_FAILED")}))
        return 2
    print(json.dumps({
        "status": report["status"],
        "manifest_sha256": report["manifest_sha256"],
        "visible_inputs_recomputed": report["counts"]["visible_inputs_recomputed"],
        "sealed_contexts": report["counts"]["untouched_test_hash_only_contexts"],
        "overlaps": report["counts"]["same_symbol_overlapping_pairs"],
        "report_sha256": report["report_sha256"],
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
