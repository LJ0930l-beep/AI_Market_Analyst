"""Read-only auditor for the frozen V38 paired nonoverlap dataset."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.dataset import DatasetBuildError, canonical_sha256
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input
from core.replay.pa_decision_quality_v38.nonoverlap_dataset import (
    NonOverlapDatasetError,
    load_nonoverlap_sample_plan,
    select_nonoverlap_calendar_points,
)
from scripts.audit_v38_dataset_integrity import (
    _read_json,
    _sha256_file,
    _valid_digest,
    _verify_raw_archives,
    summarize_global_overlap,
    summarize_partition_overlap,
)

TIMEFRAMES = frozenset({"15m", "5m", "1h", "4h"})
TIMEFRAME_SECONDS = {"15m": 900, "5m": 300, "1h": 3600, "4h": 14400}
MAX_CONTEXT_BARS = 48
MANIFEST_FIELDS = frozenset({
    "schema_version", "dataset_id", "plan_sha256", "parent_dataset_manifest_sha256", "base_commit",
    "source", "partition_counts", "temporal_anchor_counts", "contexts_by_symbol_partition",
    "total_market_contexts", "optimization_validation_input_count", "untouched_test_hash_only_count",
    "paired_temporal_anchor_count", "same_symbol_input_window_overlap_count",
    "cross_partition_input_window_overlap_count", "iid_or_independent_trade_claim",
    "price_evidence_grade", "availability_evidence_grade", "availability_delay_seconds",
    "gate_executable_trade_samples", "completed_closes", "model_outputs", "model_calls_used",
    "orders_created", "outcome_labels_included", "untouched_test_payloads_included", "files", "points",
    "manifest_sha256",
})
SOURCE_FIELDS = frozenset({
    "archive_directory_role", "database_sha256", "manifest_sha256", "archive_files_verified",
    "used_archive_hash_count", "source_window_start", "source_window_end", "symbols", "complete_data",
    "network_calls",
})
INPUT_DOCUMENT_FIELDS = frozenset({
    "schema_version", "dataset_kind", "plan_sha256", "partition_policy_sha256", "source_database_sha256",
    "source_manifest_sha256", "partition_scope", "decision_points", "outcome_labels_included",
    "model_outputs_included",
})
SEALED_DOCUMENT_FIELDS = frozenset({
    "schema_version", "partition", "plan_sha256", "partition_policy_sha256", "source_database_sha256",
    "persist_only_hashes_and_coverage", "market_input_payloads_included", "labels_or_evaluation_included",
    "decision_points",
})
INPUT_POINT_FIELDS = frozenset({
    "decision_id", "decision_time", "symbol", "partition", "bars_by_timeframe",
})
BAR_FIELDS = frozenset({
    "symbol", "timeframe", "bar_start", "bar_end", "available_at", "available_at_basis",
    "available_at_evidence_grade", "source", "source_exchange", "source_file_hash", "source_file_hashes",
    "source_manifest_sha256", "source_database_sha256", "price_evidence_grade", "quality_status",
    "is_closed", "volume_unit", "open", "high", "low", "close", "volume",
})
DESCRIPTOR_FIELDS = frozenset({
    "decision_id", "decision_time", "symbol", "partition", "selection_plan_sha256",
    "selection_key_version", "paired_anchor_group_id", "input_window_start", "input_window_end_exclusive",
    "overlaps_previous_input_window", "overlap_cluster_id", "overlap_seconds_with_previous",
    "warmup_crosses_partition_start", "market_input_sha256", "bar_count_by_timeframe", "evidence_ref_count",
    "price_evidence_grade", "availability_evidence_grade", "archive_file_sha256s",
})
SEALED_FIELDS = frozenset({
    "decision_id", "decision_time", "symbol", "partition", "selection_plan_sha256",
    "selection_key_version", "paired_anchor_group_id", "input_window_start",
    "input_window_end_exclusive", "overlaps_previous_input_window", "overlap_cluster_id",
    "overlap_seconds_with_previous", "warmup_crosses_partition_start", "market_input_sha256",
    "bar_count_by_timeframe", "evidence_ref_count", "price_evidence_grade",
    "availability_evidence_grade", "archive_file_sha256s",
})


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise NonOverlapDatasetError(code)


def _require_exact_fields(value: Any, expected: frozenset[str], code: str) -> None:
    _require(isinstance(value, dict) and set(value) == expected, code)


def _utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        raise NonOverlapDatasetError(code)
    try:
        point = datetime.fromisoformat(value)
    except ValueError as exc:
        raise NonOverlapDatasetError(code) from exc
    if point.tzinfo is None or point.utcoffset() is None:
        raise NonOverlapDatasetError(code)
    return point.astimezone(UTC)


def _verify_output_manifest(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _read_json(directory / "dataset-manifest.json", "V38_NONOVERLAP_AUDIT_MANIFEST_INVALID")
    _require_exact_fields(manifest, MANIFEST_FIELDS, "V38_NONOVERLAP_AUDIT_MANIFEST_FIELDS_INVALID")
    _require(manifest.get("schema_version") == "pa-market-only-v38/nonoverlap-dataset-manifest-1",
             "V38_NONOVERLAP_AUDIT_MANIFEST_SCHEMA_INVALID")
    _require_exact_fields(manifest.get("source"), SOURCE_FIELDS,
                          "V38_NONOVERLAP_AUDIT_SOURCE_FIELDS_INVALID")
    claimed = manifest.get("manifest_sha256")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    _require(_valid_digest(claimed) and canonical_sha256(body) == claimed,
             "V38_NONOVERLAP_AUDIT_MANIFEST_HASH_MISMATCH")
    files = manifest.get("files")
    _require(isinstance(files, dict) and set(files) == {
        "optimization-validation-inputs.json", "untouched-test-sealed-manifest.json",
    }, "V38_NONOVERLAP_AUDIT_OUTPUT_FILES_INVALID")
    expected_names = {*files, "dataset-manifest.json"}
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise NonOverlapDatasetError("V38_NONOVERLAP_AUDIT_DIRECTORY_UNREADABLE") from exc
    _require({entry.name for entry in entries} == expected_names
             and all(entry.is_file() for entry in entries),
             "V38_NONOVERLAP_AUDIT_UNEXPECTED_OUTPUT_CONTENT")
    for name, expected in files.items():
        _require(_valid_digest(expected)
                 and _sha256_file(directory / name) == expected,
                 "V38_NONOVERLAP_AUDIT_OUTPUT_FILE_HASH_MISMATCH")
    return manifest, files


def _verify_source(directory: Path, manifest: dict[str, Any], plan: dict[str, Any]) -> tuple[dict[str, Any], int]:
    source_manifest = _read_json(directory / "manifest.json", "V38_NONOVERLAP_AUDIT_SOURCE_MANIFEST_INVALID")
    source_record = manifest.get("source")
    _require(isinstance(source_manifest, dict) and isinstance(source_record, dict),
             "V38_NONOVERLAP_AUDIT_SOURCE_RECORD_INVALID")
    source_hash = canonical_sha256(source_manifest)
    database_hash = _sha256_file(directory / "research.sqlite3")
    _require(source_hash == plan["source"]["source_manifest_sha256"]
             and source_hash == source_record.get("manifest_sha256")
             and database_hash == plan["source"]["source_database_sha256"]
             and database_hash == source_record.get("database_sha256")
             and database_hash == source_manifest.get("dataset_sha256"),
             "V38_NONOVERLAP_AUDIT_SOURCE_HASH_MISMATCH")
    verified_count, verified_hashes = _verify_raw_archives(directory, source_manifest)
    return {"manifest_sha256": source_hash, "database_sha256": database_hash,
            "verified_hashes": verified_hashes}, verified_count


def _validate_input_point_binding(
    point: dict[str, Any], descriptor: dict[str, Any],
    partition_bounds: dict[str, tuple[datetime, datetime]], history: timedelta,
) -> datetime:
    """Reject metadata drift before rebuilding the account-free input hash."""
    partition = point.get("partition")
    _require(point.get("symbol") == descriptor.get("symbol")
             and partition == descriptor.get("partition")
             and isinstance(partition, str) and partition in partition_bounds,
             "V38_NONOVERLAP_AUDIT_INPUT_IDENTITY_MISMATCH")
    decision = _utc(point.get("decision_time"), "V38_NONOVERLAP_DECISION_TIME_INVALID")
    descriptor_time = _utc(descriptor.get("decision_time"), "V38_NONOVERLAP_DECISION_TIME_INVALID")
    _require(decision == descriptor_time, "V38_NONOVERLAP_AUDIT_DECISION_TIME_MISMATCH")
    start, end = partition_bounds[partition]
    _require(start <= decision - history and decision < end
             and _utc(descriptor.get("input_window_start"), "V38_NONOVERLAP_WINDOW_TIME_INVALID")
             == decision - history
             and _utc(descriptor.get("input_window_end_exclusive"), "V38_NONOVERLAP_WINDOW_TIME_INVALID")
             == decision,
             "V38_NONOVERLAP_AUDIT_PARTITION_OR_PURGE_INVALID")
    return decision


def _validate_bar_series(
    timeframe: str, series: list[Any], *, decision: datetime, history: timedelta,
    delay_seconds: int, symbol: str, source_database_sha256: str, source_manifest_sha256: str,
) -> set[str]:
    """Validate full, contiguous bars, exact availability delay, and source bindings."""
    width_seconds = TIMEFRAME_SECONDS[timeframe]
    width = timedelta(seconds=width_seconds)
    expected_count = min(int(history.total_seconds()) // width_seconds - 1, MAX_CONTEXT_BARS)
    _require(len(series) == expected_count,
             "V38_NONOVERLAP_AUDIT_BAR_COUNT_MISMATCH")
    archive_hashes: set[str] = set()
    for index, bar in enumerate(series):
        _require_exact_fields(bar, BAR_FIELDS, "V38_NONOVERLAP_AUDIT_BAR_FIELDS_INVALID")
        _require(bar.get("symbol") == symbol and bar.get("timeframe") == timeframe
                 and bar.get("available_at_basis") == "ASSUMED_PROXY_60S_AFTER_BAR_END_NOT_OBSERVED"
                 and bar.get("available_at_evidence_grade") == "ASSUMED_PROXY"
                 and bar.get("price_evidence_grade") == "VERIFIED_ARCHIVE_RECONSTRUCTION"
                 and bar.get("quality_status") == "FROZEN_RESEARCH"
                 and bar.get("volume_unit") == "BINANCE_BASE_ASSET_VOLUME"
                 and bar.get("is_closed") is True,
                 "V38_NONOVERLAP_AUDIT_BAR_CONTRACT_INVALID")
        expected_start = decision - width * (expected_count + 1) + index * width
        bar_start = _utc(bar.get("bar_start"), "V38_NONOVERLAP_AUDIT_BAR_TIME_INVALID")
        bar_end = _utc(bar.get("bar_end"), "V38_NONOVERLAP_AUDIT_BAR_TIME_INVALID")
        available = _utc(bar.get("available_at"), "V38_NONOVERLAP_AUDIT_BAR_TIME_INVALID")
        _require(bar_start == expected_start and bar_end == expected_start + width
                 and bar_end < decision and available < decision
                 and available - bar_end == timedelta(seconds=delay_seconds)
                 and int(bar_start.timestamp()) % width_seconds == 0,
                 "V38_NONOVERLAP_AUDIT_NONCAUSAL_OR_MISALIGNED_BAR")
        values: list[float] = []
        for key in ("open", "high", "low", "close", "volume"):
            raw_value = bar.get(key)
            _require(isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool),
                     "V38_NONOVERLAP_AUDIT_BAR_VALUE_INVALID")
            try:
                values.append(float(raw_value))
            except (OverflowError, TypeError, ValueError) as exc:
                raise NonOverlapDatasetError("V38_NONOVERLAP_AUDIT_BAR_VALUE_INVALID") from exc
        _require(all(math.isfinite(value) for value in values),
                 "V38_NONOVERLAP_AUDIT_BAR_NONFINITE")
        opening, high, low, close, volume = values
        _require(min(opening, high, low, close) > 0 and volume >= 0
                 and high >= max(opening, close, low) and low <= min(opening, close),
                 "V38_NONOVERLAP_AUDIT_BAR_GEOMETRY_INVALID")
        hashes = bar.get("source_file_hashes")
        _require(isinstance(hashes, list) and hashes and all(_valid_digest(digest) for digest in hashes),
                 "V38_NONOVERLAP_AUDIT_BAR_SOURCE_HASHES_MISSING")
        _require(hashes == sorted(set(hashes)), "V38_NONOVERLAP_AUDIT_BAR_SOURCE_HASHES_UNSORTED")
        for digest in hashes:
            archive_hashes.add(digest)
        expected_file_hash = hashes[0] if len(hashes) == 1 else canonical_sha256(hashes)
        _require(bar.get("source") == "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVE_RECONSTRUCTED"
                 and bar.get("source_exchange") == "BINANCE_UM"
                 and _valid_digest(bar.get("source_file_hash"))
                 and bar.get("source_file_hash") == expected_file_hash
                 and bar.get("source_database_sha256") == source_database_sha256
                 and bar.get("source_manifest_sha256") == source_manifest_sha256,
                 "V38_NONOVERLAP_AUDIT_BAR_SOURCE_BINDING_INVALID")
    return archive_hashes


def audit_nonoverlap_dataset(
    dataset_directory: Path,
    archive_directory: Path,
    *,
    rebuild_directory: Path | None = None,
) -> dict[str, Any]:
    dataset_directory = Path(dataset_directory).resolve()
    archive_directory = Path(archive_directory).resolve()
    manifest, output_files = _verify_output_manifest(dataset_directory)
    plan, plan_sha256 = load_nonoverlap_sample_plan()
    _require(manifest.get("dataset_id") == plan["plan_id"]
             and manifest.get("plan_sha256") == plan_sha256
             and manifest.get("parent_dataset_manifest_sha256") == plan["parent_dataset_manifest_sha256"],
             "V38_NONOVERLAP_AUDIT_PLAN_BINDING_INVALID")
    _require(manifest.get("total_market_contexts") == 80
             and manifest.get("optimization_validation_input_count") == 60
             and manifest.get("untouched_test_hash_only_count") == 20
             and manifest.get("model_calls_used") == 0
             and manifest.get("orders_created") == 0
             and manifest.get("outcome_labels_included") is False
             and manifest.get("untouched_test_payloads_included") is False
             and manifest.get("same_symbol_input_window_overlap_count") == 0
             and manifest.get("cross_partition_input_window_overlap_count") == 0
             and manifest.get("iid_or_independent_trade_claim") is False,
             "V38_NONOVERLAP_AUDIT_SCOPE_INVALID")

    input_doc = _read_json(dataset_directory / "optimization-validation-inputs.json",
                           "V38_NONOVERLAP_AUDIT_INPUT_INVALID")
    sealed_doc = _read_json(dataset_directory / "untouched-test-sealed-manifest.json",
                            "V38_NONOVERLAP_AUDIT_SEALED_INVALID")
    _require_exact_fields(input_doc, INPUT_DOCUMENT_FIELDS,
                          "V38_NONOVERLAP_AUDIT_INPUT_FIELDS_INVALID")
    _require(input_doc.get("schema_version") == "pa-market-only-v38/nonoverlap-market-input-dataset-1"
             and input_doc.get("partition_scope") == ["optimization", "validation"]
             and input_doc.get("plan_sha256") == plan_sha256
             and input_doc.get("source_database_sha256") == manifest["source"]["database_sha256"]
             and input_doc.get("outcome_labels_included") is False
             and input_doc.get("model_outputs_included") is False,
             "V38_NONOVERLAP_AUDIT_INPUT_CONTRACT_INVALID")
    _require_exact_fields(sealed_doc, SEALED_DOCUMENT_FIELDS,
                          "V38_NONOVERLAP_AUDIT_SEALED_FIELDS_INVALID")
    _require(sealed_doc.get("schema_version") == "pa-market-only-v38/nonoverlap-untouched-test-sealed-1"
             and sealed_doc.get("partition") == "untouched_test"
             and sealed_doc.get("plan_sha256") == plan_sha256
             and sealed_doc.get("source_database_sha256") == manifest["source"]["database_sha256"]
             and sealed_doc.get("persist_only_hashes_and_coverage") is True
             and sealed_doc.get("market_input_payloads_included") is False
             and sealed_doc.get("labels_or_evaluation_included") is False,
             "V38_NONOVERLAP_AUDIT_SEALED_CONTRACT_INVALID")
    input_points = input_doc.get("decision_points")
    sealed_points = sealed_doc.get("decision_points")
    descriptors = manifest.get("points")
    _require(isinstance(input_points, list) and len(input_points) == 60
             and isinstance(sealed_points, list) and len(sealed_points) == 20
             and isinstance(descriptors, list) and len(descriptors) == 80,
             "V38_NONOVERLAP_AUDIT_POINT_COUNTS_INVALID")

    selections = select_nonoverlap_calendar_points()
    selected_by_id = {row["decision_id"]: row for row in selections}
    descriptor_by_id: dict[str, dict[str, Any]] = {}
    declared_archive_hashes: set[str] = set()
    for row in descriptors:
        _require(isinstance(row, dict) and isinstance(row.get("decision_id"), str)
                 and row["decision_id"] not in descriptor_by_id,
                 "V38_NONOVERLAP_AUDIT_DESCRIPTOR_INVALID")
        _require_exact_fields(row, DESCRIPTOR_FIELDS,
                              "V38_NONOVERLAP_AUDIT_DESCRIPTOR_FIELDS_INVALID")
        descriptor_by_id[row["decision_id"]] = row
        selected = selected_by_id.get(row["decision_id"])
        _require(selected is not None
                 and all(row.get(field) == selected.get(field) for field in selected),
                 "V38_NONOVERLAP_AUDIT_SELECTION_MISMATCH")
        archive_hashes = row.get("archive_file_sha256s")
        _require(isinstance(archive_hashes, list) and archive_hashes
                 and all(_valid_digest(digest) for digest in archive_hashes),
                 "V38_NONOVERLAP_AUDIT_DESCRIPTOR_ARCHIVE_HASHES_INVALID")
        _require(archive_hashes == sorted(set(archive_hashes)),
                 "V38_NONOVERLAP_AUDIT_DESCRIPTOR_ARCHIVE_HASHES_UNSORTED")
        declared_archive_hashes.update(archive_hashes)
    _require(set(descriptor_by_id) == set(selected_by_id),
             "V38_NONOVERLAP_AUDIT_SELECTION_COVERAGE_MISMATCH")

    expected_payload_ids = {
        row["decision_id"] for row in selections if row["partition"] in {"optimization", "validation"}
    }
    input_ids: set[str] = set()
    timeframe_counts: Counter[str] = Counter()
    recomputed_hashes = 0
    causal_bars = 0
    used_archive_hashes = set(declared_archive_hashes)
    observed_bar_archive_hashes: set[str] = set()
    delay = plan["data"]["availability_delay_seconds"]
    history = timedelta(days=plan["selection"]["history_window_days"])
    plan_partitions = {
        row["id"]: (_utc(row["start_utc_inclusive"], "V38_NONOVERLAP_AUDIT_PLAN_TIME_INVALID"),
                    _utc(row["end_utc_exclusive"], "V38_NONOVERLAP_AUDIT_PLAN_TIME_INVALID"))
        for row in plan["partitions"]
    }
    for point in input_points:
        _require_exact_fields(point, INPUT_POINT_FIELDS,
                              "V38_NONOVERLAP_AUDIT_INPUT_POINT_FIELDS_INVALID")
        decision_id = point["decision_id"]
        descriptor = descriptor_by_id.get(decision_id)
        _require(decision_id in expected_payload_ids and decision_id not in input_ids
                 and descriptor is not None,
                 "V38_NONOVERLAP_AUDIT_INPUT_POINT_BINDING_INVALID")
        input_ids.add(decision_id)
        decision = _validate_input_point_binding(point, descriptor, plan_partitions, history)
        market_input = build_market_only_input(point)
        _require(market_input.get("market_input_sha256") == descriptor.get("market_input_sha256"),
                 "V38_NONOVERLAP_AUDIT_INPUT_HASH_MISMATCH")
        provenance_hashes = market_input.get("provenance", {}).get("archive_file_sha256s")
        _require(provenance_hashes == descriptor.get("archive_file_sha256s"),
                 "V38_NONOVERLAP_AUDIT_ARCHIVE_PROVENANCE_MISMATCH")
        recomputed_hashes += 1
        bars = point.get("bars_by_timeframe")
        _require(isinstance(bars, dict) and set(bars) == TIMEFRAMES,
                 "V38_NONOVERLAP_AUDIT_TIMEFRAME_SET_INVALID")
        actual_counts: dict[str, int] = {}
        for timeframe, series in bars.items():
            _require(isinstance(series, list), "V38_NONOVERLAP_AUDIT_BAR_LIST_INVALID")
            actual_counts[timeframe] = len(series)
            observed_bar_archive_hashes.update(_validate_bar_series(
                timeframe, series, decision=decision, history=history, delay_seconds=delay,
                symbol=point["symbol"],
                source_database_sha256=manifest["source"]["database_sha256"],
                source_manifest_sha256=manifest["source"]["manifest_sha256"],
            ))
            timeframe_counts[timeframe] += len(series)
            causal_bars += len(series)
        _require(actual_counts == descriptor.get("bar_count_by_timeframe"),
                 "V38_NONOVERLAP_AUDIT_BAR_COUNT_MISMATCH")
        _require(len(market_input.get("evidence_refs", [])) == descriptor.get("evidence_ref_count"),
                 "V38_NONOVERLAP_AUDIT_EVIDENCE_REF_COUNT_MISMATCH")

    _require(input_ids == expected_payload_ids, "V38_NONOVERLAP_AUDIT_INPUT_COVERAGE_MISMATCH")
    _require(observed_bar_archive_hashes <= used_archive_hashes,
             "V38_NONOVERLAP_AUDIT_BAR_ARCHIVE_PROVENANCE_MISMATCH")
    expected_sealed_ids = {
        row["decision_id"] for row in selections if row["partition"] == "untouched_test"
    }
    sealed_ids: set[str] = set()
    for row in sealed_points:
        _require(isinstance(row, dict) and set(row) == SEALED_FIELDS,
                 "V38_NONOVERLAP_AUDIT_SEALED_FIELDS_INVALID")
        decision_id = row.get("decision_id")
        _require(decision_id in expected_sealed_ids and decision_id not in sealed_ids,
                 "V38_NONOVERLAP_AUDIT_SEALED_BINDING_INVALID")
        sealed_ids.add(decision_id)
        descriptor = descriptor_by_id[decision_id]
        _require(row == {field: descriptor[field] for field in SEALED_FIELDS},
                 "V38_NONOVERLAP_AUDIT_SEALED_DESCRIPTOR_MISMATCH")
    _require(sealed_ids == expected_sealed_ids, "V38_NONOVERLAP_AUDIT_SEALED_COVERAGE_MISMATCH")

    global_overlap = summarize_global_overlap(descriptors)
    partition_overlap = summarize_partition_overlap(descriptors)
    _require(global_overlap["cross_partition_overlapping_pair_count"] == 0
             and global_overlap["global_overlap_component_count"] == 80
             and all(all(size == 1 for size in group["component_sizes_descending"])
                     for group in partition_overlap.values()),
             "V38_NONOVERLAP_AUDIT_OVERLAP_INVARIANT_FAILED")

    source, raw_file_count = _verify_source(archive_directory, manifest, plan)
    _require(used_archive_hashes <= source["verified_hashes"],
             "V38_NONOVERLAP_AUDIT_USED_ARCHIVE_HASH_NOT_VERIFIED")
    _require(raw_file_count == manifest["source"]["archive_files_verified"],
             "V38_NONOVERLAP_AUDIT_ARCHIVE_COUNT_MISMATCH")

    rebuild = {"provided": False}
    if rebuild_directory is not None:
        other, _ = _verify_output_manifest(Path(rebuild_directory).resolve())
        matched = other.get("manifest_sha256") == manifest.get("manifest_sha256")
        _require(matched, "V38_NONOVERLAP_AUDIT_DETERMINISTIC_REBUILD_MISMATCH")
        rebuild = {"provided": True, "manifest_sha256": other["manifest_sha256"],
                   "matches_retained_dataset": True}

    report: dict[str, Any] = {
        "schema_version": "pa-market-only-v38/nonoverlap-integrity-audit-1",
        "status": "PASS_WITH_CAVEATS",
        "dataset": {
            "manifest_sha256": manifest["manifest_sha256"],
            "plan_sha256": plan_sha256,
            "total_contexts": len(descriptors),
            "optimization_validation_payloads": len(input_points),
            "untouched_test_hash_only_rows": len(sealed_points),
            "partition_counts": dict(sorted(Counter(row["partition"] for row in descriptors).items())),
            "paired_temporal_anchor_count": manifest["paired_temporal_anchor_count"],
            "output_files_verified": len(output_files),
        },
        "source": {
            "exchange_product": "BINANCE_UM_PUBLIC_ARCHIVES",
            "database_sha256": source["database_sha256"],
            "manifest_sha256": source["manifest_sha256"],
            "archive_files_hash_and_size_verified": raw_file_count,
            "used_archive_hash_count": len(used_archive_hashes),
            "network_calls": 0,
        },
        "checks": {
            "selected_input_hashes_recomputed": recomputed_hashes,
            "ohlcv_bars_causally_and_geometrically_checked": causal_bars,
            "bar_counts_by_timeframe": dict(sorted(timeframe_counts.items())),
            "all_used_archive_hashes_listed_and_verified": True,
            "warmup_crosses_partition_boundaries": False,
            "same_symbol_input_window_overlap_count": global_overlap["cross_partition_overlapping_pair_count"],
            "model_calls_used": 0,
            "orders_created": 0,
        },
        "overlap": {
            **global_overlap,
            "partition_group_breakdown": partition_overlap,
            "same_symbol_windows_are_nonoverlapping": True,
            "cross_asset_temporal_rows_are_paired_not_independent": True,
        },
        "deterministic_rebuild": rebuild,
        "limitations": [
            "Price history is reconstructed from Binance UM archives, not historical Gate prices or Gate receive times.",
            "Availability uses a 60-second proxy, not observed receive timestamps.",
            "Ten validation and ten untouched-test temporal anchors are a small pilot, not a powered profitability study.",
            "BTC and ETH rows at each anchor are paired and may be correlated; nonoverlapping bars do not establish IID observations.",
            "No account state, order-book quotes, contract snapshots, labels, model outputs, fills, or completed closes are present.",
        ],
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _output_path(value: str) -> Path:
    output = (ROOT / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    allowed = (ROOT / "reports" / "v38+" / "verification").resolve()
    try:
        output.parent.relative_to(allowed)
    except ValueError as exc:
        raise NonOverlapDatasetError("V38_NONOVERLAP_AUDIT_OUTPUT_MUST_BE_UNDER_VERIFICATION") from exc
    if output.exists():
        raise NonOverlapDatasetError("V38_NONOVERLAP_AUDIT_OUTPUT_EXISTS_REFUSE_OVERWRITE")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path, help="Existing frozen nonoverlap dataset.")
    parser.add_argument("--archive", required=True, type=Path, help="Existing local verified archive directory.")
    parser.add_argument("--rebuild-dataset", type=Path, help="Optional separate rebuild to compare manifest hashes.")
    parser.add_argument("--output", required=True,
                        help="Write-once report path under reports/v38+/verification.")
    args = parser.parse_args(argv)
    try:
        output_path = _output_path(str(args.output))
        report = audit_nonoverlap_dataset(
            args.dataset, args.archive, rebuild_directory=args.rebuild_dataset,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    except (DatasetBuildError, NonOverlapDatasetError, KeyError, OSError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": report["status"],
        "report_sha256": report["report_sha256"],
        "dataset_manifest_sha256": report["dataset"]["manifest_sha256"],
        "total_contexts": report["dataset"]["total_contexts"],
        "paired_temporal_anchor_count": report["dataset"]["paired_temporal_anchor_count"],
        "overlap_component_count": report["overlap"]["global_overlap_component_count"],
        "cross_partition_overlap_pair_count": report["overlap"]["cross_partition_overlapping_pair_count"],
        "model_calls_used": 0,
        "network_calls": 0,
        "orders_created": 0,
        "output": str(output_path),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
