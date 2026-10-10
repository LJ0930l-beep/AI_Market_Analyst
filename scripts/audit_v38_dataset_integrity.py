"""Read-only integrity audit for a frozen V38 dataset and its local archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.dataset import (
    DatasetBuildError,
    canonical_sha256,
    load_v38_sample_plan,
    select_v38_calendar_points,
)
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input

SEALED_POINT_FIELDS = frozenset({
    "decision_id", "symbol", "partition", "decision_time", "selection_plan_sha256",
    "market_input_sha256", "bar_count_by_timeframe", "evidence_ref_count",
    "price_evidence_grade", "availability_evidence_grade", "archive_file_sha256s",
    "input_window_start", "input_window_end_exclusive", "overlaps_previous_input_window",
    "overlap_cluster_id", "overlap_seconds_with_previous", "warmup_crosses_partition_start",
})
PARTITION_ORDER = {"optimization": 0, "validation": 1, "untouched_test": 2}
REQUIRED_TIMEFRAMES = frozenset({"15m", "5m", "1h", "4h"})
HEX_256 = frozenset("0123456789abcdef")


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise DatasetBuildError(code)


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetBuildError(code) from exc


def _utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        raise DatasetBuildError(code)
    try:
        point = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DatasetBuildError(code) from exc
    if point.tzinfo is None or point.utcoffset() is None:
        raise DatasetBuildError(code)
    return point.astimezone(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise DatasetBuildError("V38_AUDIT_FILE_UNAVAILABLE") from exc
    return digest.hexdigest()


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value.lower()) <= HEX_256


def _verify_dataset_manifest(dataset_directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _read_json(dataset_directory / "dataset-manifest.json", "V38_AUDIT_DATASET_MANIFEST_INVALID")
    _require(isinstance(manifest, dict), "V38_AUDIT_DATASET_MANIFEST_INVALID")
    _require(manifest.get("schema_version") == "pa-market-only-v38/dataset-manifest-1",
             "V38_AUDIT_DATASET_SCHEMA_INVALID")
    expected_hash = manifest.get("manifest_sha256")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    _require(_valid_digest(expected_hash) and canonical_sha256(body) == expected_hash,
             "V38_AUDIT_DATASET_MANIFEST_HASH_MISMATCH")
    files = manifest.get("files")
    _require(isinstance(files, dict) and files, "V38_AUDIT_OUTPUT_FILES_MISSING")
    for name, expected in files.items():
        _require(
            isinstance(name, str) and Path(name).name == name and _valid_digest(expected),
            "V38_AUDIT_OUTPUT_FILE_RECORD_INVALID",
        )
        _require(_sha256_file(dataset_directory / name) == expected,
                 "V38_AUDIT_OUTPUT_FILE_HASH_MISMATCH")
    return manifest, files


def summarize_global_overlap(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Count connected input-window components across partitions, per symbol.

    Windows are half-open [input_window_start, input_window_end_exclusive).
    Connected components are overlap groups, not independent trade samples.
    """
    if not isinstance(points, list):
        raise DatasetBuildError("V38_OVERLAP_POINTS_INVALID")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_ids: set[str] = set()
    for row in points:
        if not isinstance(row, dict):
            raise DatasetBuildError("V38_OVERLAP_POINT_INVALID")
        decision_id = row.get("decision_id")
        symbol = row.get("symbol")
        partition = row.get("partition")
        if not isinstance(decision_id, str) or not decision_id or decision_id in seen_ids:
            raise DatasetBuildError("V38_OVERLAP_DECISION_ID_INVALID")
        if not isinstance(symbol, str) or not symbol.strip():
            raise DatasetBuildError("V38_OVERLAP_SYMBOL_INVALID")
        if not isinstance(partition, str) or partition not in PARTITION_ORDER:
            raise DatasetBuildError("V38_OVERLAP_PARTITION_INVALID")
        start = _utc(row.get("input_window_start"), "V38_OVERLAP_WINDOW_TIME_INVALID")
        end = _utc(row.get("input_window_end_exclusive"), "V38_OVERLAP_WINDOW_TIME_INVALID")
        if start >= end:
            raise DatasetBuildError("V38_OVERLAP_WINDOW_INVALID")
        seen_ids.add(decision_id)
        grouped[symbol].append({
            "decision_id": decision_id,
            "partition": partition,
            "start": start,
            "end": end,
        })

    parent = {decision_id: decision_id for decision_id in seen_ids}

    def find(decision_id: str) -> str:
        while parent[decision_id] != decision_id:
            parent[decision_id] = parent[parent[decision_id]]
            decision_id = parent[decision_id]
        return decision_id

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    cross_partition_pairs: list[dict[str, Any]] = []
    for rows in grouped.values():
        rows.sort(key=lambda row: (row["start"], row["end"], row["decision_id"]))
        active: list[dict[str, Any]] = []
        for current in rows:
            active = [row for row in active if row["end"] > current["start"]]
            for previous in active:
                overlap_start = max(previous["start"], current["start"])
                overlap_end = min(previous["end"], current["end"])
                if overlap_start >= overlap_end:
                    continue
                union(previous["decision_id"], current["decision_id"])
                if previous["partition"] != current["partition"]:
                    ordered_partitions = sorted(
                        {previous["partition"], current["partition"]},
                        key=PARTITION_ORDER.__getitem__,
                    )
                    cross_partition_pairs.append({
                        "decision_ids": [previous["decision_id"], current["decision_id"]],
                        "partitions": ordered_partitions,
                        "overlap_seconds": int((overlap_end - overlap_start).total_seconds()),
                    })
            active.append(current)

    components: dict[str, list[str]] = defaultdict(list)
    component_symbols: dict[str, str] = {}
    for symbol, rows in grouped.items():
        for row in rows:
            root = find(row["decision_id"])
            components[root].append(row["decision_id"])
            component_symbols[root] = symbol
    components_by_symbol = Counter(component_symbols.values())
    component_sizes_by_symbol: dict[str, list[int]] = defaultdict(list)
    for root, decision_ids in components.items():
        component_sizes_by_symbol[component_symbols[root]].append(len(decision_ids))
    partitions_by_component: dict[str, set[str]] = defaultdict(set)
    for symbol, rows in grouped.items():
        for row in rows:
            partitions_by_component[find(row["decision_id"])].add(row["partition"])
    spanning = sum(len(partitions) > 1 for partitions in partitions_by_component.values())
    by_partition_pair: Counter[str] = Counter()
    for pair in cross_partition_pairs:
        by_partition_pair["/".join(pair["partitions"])] += 1

    return {
        "window_semantics": "UTC_HALF_OPEN_INTERVALS",
        "component_scope": "GLOBAL_PER_SYMBOL_ACROSS_ALL_PARTITIONS",
        "global_overlap_component_count": len(components),
        "global_overlap_component_count_by_symbol": dict(sorted(components_by_symbol.items())),
        "global_overlap_component_sizes_by_symbol": {
            symbol: sorted(sizes, reverse=True)
            for symbol, sizes in sorted(component_sizes_by_symbol.items())
        },
        "components_spanning_multiple_partitions": spanning,
        "cross_partition_overlapping_pair_count": len(cross_partition_pairs),
        "cross_partition_pair_counts": dict(sorted(by_partition_pair.items())),
        "cross_partition_overlapping_pairs": sorted(
            cross_partition_pairs,
            key=lambda row: (row["partitions"], row["decision_ids"]),
        ),
        "row_count": len(points),
        "independent_sample_count_claimed": False,
    }


def _group_partition_points(points: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in points:
        if not isinstance(row, dict):
            raise DatasetBuildError("V38_OVERLAP_POINT_INVALID")
        symbol, partition = row.get("symbol"), row.get("partition")
        if not isinstance(symbol, str) or not symbol.strip():
            raise DatasetBuildError("V38_OVERLAP_SYMBOL_INVALID")
        if not isinstance(partition, str) or partition not in PARTITION_ORDER:
            raise DatasetBuildError("V38_OVERLAP_PARTITION_INVALID")
        grouped[(symbol, partition)].append(row)
    return grouped


def count_partition_scoped_components(points: list[dict[str, Any]]) -> int:
    """Recompute the legacy overlap count within each symbol/partition group."""
    grouped = _group_partition_points(points)
    return sum(
        summarize_global_overlap(group)["global_overlap_component_count"]
        for group in grouped.values()
    )


def summarize_partition_overlap(points: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return row counts and window-connected component sizes per symbol/partition."""
    grouped = _group_partition_points(points)
    result: dict[str, dict[str, Any]] = {}
    for (symbol, partition), rows in sorted(grouped.items()):
        overlap = summarize_global_overlap(rows)
        sizes = overlap["global_overlap_component_sizes_by_symbol"].get(symbol, [])
        result[f"{symbol}:{partition}"] = {
            "row_count": len(rows),
            "component_count": overlap["global_overlap_component_count"],
            "component_sizes_descending": sizes,
            "independent_sample_count_claimed": False,
        }
    return result


def _verify_raw_archives(
    archive_directory: Path, source_manifest: dict[str, Any],
) -> tuple[int, set[str]]:
    records = source_manifest.get("archived_files")
    _require(isinstance(records, list) and records, "V38_AUDIT_ARCHIVE_RECORDS_MISSING")
    verified_hashes: set[str] = set()
    for record in records:
        _require(isinstance(record, dict), "V38_AUDIT_ARCHIVE_RECORD_INVALID")
        url = record.get("url")
        filename = Path(urlsplit(url).path).name if isinstance(url, str) else ""
        expected_hash = record.get("sha256")
        expected_size = record.get("bytes")
        _require(bool(filename) and _valid_digest(expected_hash)
                 and type(expected_size) is int and expected_size > 0
                 and record.get("official_checksum_verified") is True,
                 "V38_AUDIT_ARCHIVE_RECORD_INVALID")
        path = archive_directory / "raw_cache" / filename
        _require(path.is_file(), "V38_AUDIT_ARCHIVE_FILE_MISSING")
        _require(path.stat().st_size == expected_size and _sha256_file(path) == expected_hash,
                 "V38_AUDIT_ARCHIVE_FILE_HASH_MISMATCH")
        verified_hashes.add(expected_hash)
    return len(records), verified_hashes


def _verify_document_source_binding(
    input_document: dict[str, Any],
    sealed_document: dict[str, Any],
    source_record: dict[str, Any],
    plan_sha256: str,
) -> None:
    _require(input_document.get("plan_sha256") == plan_sha256
             and sealed_document.get("plan_sha256") == plan_sha256
             and input_document.get("source_database_sha256") == source_record["database_sha256"]
             and sealed_document.get("source_database_sha256") == source_record["database_sha256"]
             and input_document.get("source_manifest_sha256") == source_record["manifest_sha256"],
             "V38_AUDIT_INPUT_SOURCE_BINDING_INVALID")


def audit_v38_dataset(
    dataset_directory: Path,
    archive_directory: Path,
    *,
    rebuild_directory: Path | None = None,
) -> dict[str, Any]:
    """Verify local V38 inputs and report cross-partition overlap without mutation."""
    dataset_directory = Path(dataset_directory).resolve()
    archive_directory = Path(archive_directory).resolve()
    manifest, output_file_hashes = _verify_dataset_manifest(dataset_directory)
    plan, plan_sha256 = load_v38_sample_plan()
    source_record = manifest.get("source")
    _require(isinstance(source_record, dict)
             and _valid_digest(source_record.get("database_sha256"))
             and _valid_digest(source_record.get("manifest_sha256")),
             "V38_AUDIT_SOURCE_RECORD_INVALID")
    source_manifest = _read_json(archive_directory / "manifest.json", "V38_AUDIT_SOURCE_MANIFEST_INVALID")
    _require(isinstance(source_manifest, dict)
             and source_manifest.get("exchange") == "BINANCE_UM"
             and source_manifest.get("source_type") == "OFFICIAL_MONTHLY_PUBLIC_ARCHIVES"
             and source_manifest.get("complete_data") is True
             and source_manifest.get("symbols") == plan["data"]["symbols"],
             "V38_AUDIT_SOURCE_IDENTITY_INVALID")
    source_manifest_hash = canonical_sha256(source_manifest)
    database_path = archive_directory / "research.sqlite3"
    source_database_hash = _sha256_file(database_path)
    _require(source_database_hash == source_manifest.get("dataset_sha256")
             and source_database_hash == source_record["database_sha256"],
             "V38_AUDIT_SOURCE_DATABASE_HASH_MISMATCH")
    _require(source_manifest_hash == source_record["manifest_sha256"],
             "V38_AUDIT_SOURCE_MANIFEST_HASH_MISMATCH")
    _require(manifest.get("dataset_id") == plan["plan_id"]
             and manifest.get("plan_sha256") == plan_sha256,
             "V38_AUDIT_SAMPLE_PLAN_MISMATCH")

    input_path = dataset_directory / "optimization-validation-inputs.json"
    sealed_path = dataset_directory / "untouched-test-sealed-manifest.json"
    input_document = _read_json(input_path, "V38_AUDIT_INPUT_DOCUMENT_INVALID")
    sealed_document = _read_json(sealed_path, "V38_AUDIT_SEALED_DOCUMENT_INVALID")
    _require(isinstance(input_document, dict)
             and input_document.get("schema_version") == "pa-market-only-v38/market-input-dataset-1"
             and input_document.get("partition_scope") == ["optimization", "validation"]
             and input_document.get("outcome_labels_included") is False
             and input_document.get("model_outputs_included") is False,
             "V38_AUDIT_INPUT_DOCUMENT_CONTRACT_INVALID")
    _require(isinstance(sealed_document, dict)
             and sealed_document.get("schema_version") == "pa-market-only-v38/untouched-test-sealed-1"
             and sealed_document.get("partition") == "untouched_test"
             and sealed_document.get("market_input_payloads_included") is False
             and sealed_document.get("labels_or_evaluation_included") is False
             and sealed_document.get("persist_only_hashes_and_coverage") is True,
             "V38_AUDIT_SEALED_DOCUMENT_CONTRACT_INVALID")
    _verify_document_source_binding(input_document, sealed_document, source_record, plan_sha256)

    points = manifest.get("points")
    input_points = input_document.get("decision_points")
    sealed_points = sealed_document.get("decision_points")
    _require(isinstance(points, list) and isinstance(input_points, list) and isinstance(sealed_points, list),
             "V38_AUDIT_POINT_LIST_INVALID")
    _require(len(points) == 180 and len(input_points) == 120 and len(sealed_points) == 60
             and manifest.get("total_market_contexts") == 180
             and manifest.get("optimization_validation_input_count") == 120
             and manifest.get("untouched_test_hash_only_count") == 60,
             "V38_AUDIT_POINT_COUNTS_INVALID")
    _require(manifest.get("model_calls_used") == 0 and manifest.get("orders_created") == 0
             and manifest.get("gate_executable_trade_samples") == 0
             and manifest.get("completed_closes") == 0
             and manifest.get("outcome_labels_included") is False
             and manifest.get("untouched_test_payloads_included") is False,
             "V38_AUDIT_NON_TRADING_SCOPE_INVALID")

    descriptor_by_id: dict[str, dict[str, Any]] = {}
    for row in points:
        if not isinstance(row, dict):
            raise DatasetBuildError("V38_AUDIT_DESCRIPTOR_INVALID")
        decision_id = row.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id or decision_id in descriptor_by_id:
            raise DatasetBuildError("V38_AUDIT_DECISION_ID_DUPLICATE")
        descriptor_by_id[decision_id] = row

    partition_ranges = {
        row["id"]: (_utc(row["start_utc_inclusive"], "V38_AUDIT_PLAN_TIME_INVALID"),
                    _utc(row["end_utc_exclusive"], "V38_AUDIT_PLAN_TIME_INVALID"))
        for row in plan["partitions"]
    }
    timeframe_bar_counts: Counter[str] = Counter()
    input_counts: Counter[tuple[str, str]] = Counter()
    input_ids: set[str] = set()
    input_hash_recomputed = 0
    causal_rows_checked = 0
    archive_hashes_used: set[str] = set()
    history_window = timedelta(days=plan["context"]["history_window_days"])

    for point in input_points:
        if not isinstance(point, dict):
            raise DatasetBuildError("V38_AUDIT_INPUT_POINT_INVALID")
        decision_id = point.get("decision_id")
        descriptor = descriptor_by_id.get(decision_id)
        if descriptor is None or decision_id in input_ids:
            raise DatasetBuildError("V38_AUDIT_INPUT_DECISION_BINDING_INVALID")
        input_ids.add(decision_id)
        decision_time = _utc(point.get("decision_time"), "V38_AUDIT_DECISION_TIME_INVALID")
        partition = point.get("partition")
        symbol = point.get("symbol")
        _require(isinstance(partition, str) and partition in partition_ranges
                 and isinstance(symbol, str) and bool(symbol.strip()),
                 "V38_AUDIT_PARTITION_OR_SYMBOL_INVALID")
        bounds = partition_ranges[partition]
        _require(bounds[0] <= decision_time < bounds[1],
                 "V38_AUDIT_PARTITION_BOUNDARY_INVALID")
        _require((partition, symbol, point.get("decision_time"))
                 == (descriptor.get("partition"), descriptor.get("symbol"), descriptor.get("decision_time")),
                 "V38_AUDIT_DESCRIPTOR_BINDING_MISMATCH")

        market_input = build_market_only_input(point)
        _require(market_input.get("market_input_sha256") == descriptor.get("market_input_sha256"),
                 "V38_AUDIT_MARKET_INPUT_HASH_MISMATCH")
        _require(market_input.get("provenance", {}).get("availability_evidence_grade")
                 == [plan["data"]["availability_evidence_grade"]],
                 "V38_AUDIT_AVAILABILITY_GRADE_MISMATCH")
        input_hash_recomputed += 1
        input_counts[(symbol, partition)] += 1

        bars_by_timeframe = point.get("bars_by_timeframe")
        _require(isinstance(bars_by_timeframe, dict)
                 and set(bars_by_timeframe) == REQUIRED_TIMEFRAMES,
                 "V38_AUDIT_TIMEFRAME_SET_INVALID")
        actual_counts: dict[str, int] = {}
        for timeframe, bars in bars_by_timeframe.items():
            _require(isinstance(bars, list), "V38_AUDIT_BAR_LIST_INVALID")
            ends: set[str] = set()
            actual_counts[timeframe] = len(bars)
            for bar in bars:
                _require(isinstance(bar, dict), "V38_AUDIT_BAR_INVALID")
                end = _utc(bar.get("bar_end"), "V38_AUDIT_BAR_TIME_INVALID")
                available = _utc(bar.get("available_at"), "V38_AUDIT_BAR_TIME_INVALID")
                _require(end < decision_time and available < decision_time and available >= end,
                         "V38_AUDIT_NON_CAUSAL_BAR")
                _require((available - end).total_seconds() == plan["data"]["availability_delay_seconds"],
                         "V38_AUDIT_AVAILABILITY_PROXY_MISMATCH")
                end_key = bar["bar_end"]
                _require(end_key not in ends, "V38_AUDIT_DUPLICATE_BAR")
                ends.add(end_key)
                values = [float(bar[key]) for key in ("open", "high", "low", "close", "volume")]
                _require(all(math.isfinite(value) for value in values), "V38_AUDIT_BAR_NONFINITE")
                opening, high, low, close, volume = values
                _require(min(opening, high, low, close) > 0 and volume >= 0
                         and high >= max(opening, close, low)
                         and low <= min(opening, close),
                         "V38_AUDIT_BAR_GEOMETRY_INVALID")
                file_hashes = bar.get("source_file_hashes")
                _require(isinstance(file_hashes, list) and file_hashes,
                         "V38_AUDIT_BAR_SOURCE_HASHES_MISSING")
                for digest in file_hashes:
                    _require(_valid_digest(digest), "V38_AUDIT_BAR_SOURCE_HASH_INVALID")
                    archive_hashes_used.add(digest)
                _require(bar.get("source_database_sha256") == manifest["source"]["database_sha256"]
                         and bar.get("source_manifest_sha256") == manifest["source"]["manifest_sha256"],
                         "V38_AUDIT_BAR_SOURCE_BINDING_INVALID")
                timeframe_bar_counts[timeframe] += 1
                causal_rows_checked += 1
        _require(actual_counts == descriptor.get("bar_count_by_timeframe"),
                 "V38_AUDIT_BAR_COUNT_MISMATCH")
        _require(len(market_input.get("evidence_refs", [])) == descriptor.get("evidence_ref_count"),
                 "V38_AUDIT_EVIDENCE_REF_COUNT_MISMATCH")
        expected_window_start = decision_time - history_window
        _require(_utc(descriptor.get("input_window_start"), "V38_AUDIT_WINDOW_TIME_INVALID")
                 == expected_window_start
                 and _utc(descriptor.get("input_window_end_exclusive"), "V38_AUDIT_WINDOW_TIME_INVALID")
                 == decision_time,
                 "V38_AUDIT_INPUT_WINDOW_INVALID")

    expected_input_ids = {
        row["decision_id"] for row in points if row.get("partition") in {"optimization", "validation"}
    }
    _require(input_ids == expected_input_ids, "V38_AUDIT_OPT_VAL_COVERAGE_MISMATCH")
    for symbol in plan["data"]["symbols"]:
        for partition in ("optimization", "validation"):
            _require(input_counts[(symbol, partition)] == 30, "V38_AUDIT_OPT_VAL_PARTITION_COUNT_INVALID")

    expected_sealed_ids = {
        row["decision_id"] for row in points if row.get("partition") == "untouched_test"
    }
    sealed_ids: set[str] = set()
    for row in sealed_points:
        if not isinstance(row, dict) or set(row) != SEALED_POINT_FIELDS:
            raise DatasetBuildError("V38_AUDIT_SEALED_POINT_FIELDS_INVALID")
        decision_id = row.get("decision_id")
        if decision_id in sealed_ids or decision_id not in expected_sealed_ids:
            raise DatasetBuildError("V38_AUDIT_SEALED_DECISION_BINDING_INVALID")
        sealed_ids.add(decision_id)
        descriptor = descriptor_by_id[decision_id]
        if row != {field: descriptor[field] for field in SEALED_POINT_FIELDS}:
            raise DatasetBuildError("V38_AUDIT_SEALED_DESCRIPTOR_MISMATCH")
    _require(sealed_ids == expected_sealed_ids, "V38_AUDIT_SEALED_COVERAGE_MISMATCH")
    for symbol in plan["data"]["symbols"]:
        _require(sum(row.get("symbol") == symbol for row in sealed_points) == 30,
                 "V38_AUDIT_SEALED_PARTITION_COUNT_INVALID")

    selected = select_v38_calendar_points()
    _require(
        {(row["decision_id"], row["partition"], row["decision_time"]) for row in selected}
        == {(row["decision_id"], row["partition"], row["decision_time"]) for row in points},
        "V38_AUDIT_FROZEN_SELECTION_MISMATCH",
    )

    overlap = summarize_global_overlap(points)
    scoped_overlap_count = count_partition_scoped_components(points)
    _require(manifest.get("overlap_cluster_count") == scoped_overlap_count,
             "V38_AUDIT_STORED_OVERLAP_COUNT_MISMATCH")
    raw_archive_count, verified_archive_hashes = _verify_raw_archives(archive_directory, source_manifest)
    _require(archive_hashes_used <= verified_archive_hashes,
             "V38_AUDIT_USED_ARCHIVE_HASH_NOT_IN_SOURCE_MANIFEST")
    _require(raw_archive_count == manifest["source"]["archive_files_verified"]
             and manifest["source"].get("network_calls") == 0,
             "V38_AUDIT_ARCHIVE_COUNT_OR_NETWORK_CLAIM_INVALID")

    rebuild_evidence: dict[str, Any] = {"provided": False}
    if rebuild_directory is not None:
        rebuild_directory = Path(rebuild_directory).resolve()
        rebuild_manifest, _ = _verify_dataset_manifest(rebuild_directory)
        matched = rebuild_manifest.get("manifest_sha256") == manifest["manifest_sha256"]
        _require(matched, "V38_AUDIT_DETERMINISTIC_REBUILD_MISMATCH")
        rebuild_evidence = {
            "provided": True,
            "manifest_sha256": rebuild_manifest["manifest_sha256"],
            "matches_retained_dataset": True,
        }

    result: dict[str, Any] = {
        "schema_version": "pa-market-only-v38/dataset-integrity-audit-1",
        "status": "PASS_WITH_CAVEATS",
        "scope": "Read-only archive and dataset integrity, partition coverage, causal bars, and overlap metadata.",
        "dataset": {
            "manifest_sha256": manifest["manifest_sha256"],
            "plan_sha256": plan_sha256,
            "total_contexts": len(points),
            "optimization_validation_payloads": len(input_points),
            "untouched_test_hash_only_rows": len(sealed_points),
            "partition_counts": dict(sorted(Counter(row["partition"] for row in points).items())),
            "symbol_partition_counts": {
                f"{symbol}:{partition}": sum(
                    row["symbol"] == symbol and row["partition"] == partition for row in points
                )
                for symbol in plan["data"]["symbols"]
                for partition in PARTITION_ORDER
            },
            "output_files_verified": len(output_file_hashes),
        },
        "source": {
            "exchange_product": "BINANCE_UM_PUBLIC_ARCHIVES",
            "database_sha256": source_database_hash,
            "manifest_sha256": source_manifest_hash,
            "archive_files_hash_and_size_verified": raw_archive_count,
            "network_calls": 0,
        },
        "checks": {
            "selected_input_hashes_recomputed": input_hash_recomputed,
            "ohlcv_bars_causally_and_geometrically_checked": causal_rows_checked,
            "bar_counts_by_timeframe": dict(sorted(timeframe_bar_counts.items())),
            "distinct_archive_hashes_used_by_selected_inputs": len(archive_hashes_used),
            "all_used_archive_hashes_listed_and_verified": True,
            "availability_delay_seconds_checked": plan["data"]["availability_delay_seconds"],
            "untouched_test_contains_payloads_or_labels": False,
            "model_calls_used": 0,
            "orders_created": 0,
        },
        "overlap": {
            "stored_manifest_cluster_count_scope": "PER_SYMBOL_AND_PARTITION",
            "stored_manifest_cluster_count": manifest.get("overlap_cluster_count"),
            "recomputed_partition_scoped_component_count": scoped_overlap_count,
            "partition_group_breakdown": summarize_partition_overlap(points),
            **overlap,
        },
        "deterministic_rebuild": rebuild_evidence,
        "limitations": [
            "The archive reconstructs Binance UM prices, not historical Gate prices or Gate receive times.",
            "The 60-second availability interval is an assumed proxy, not an observed timestamp.",
            "Overlap components describe shared input windows and are not independent trade samples.",
            "The dataset has no account state, order-book quotes, contract snapshots, fills, labels, or completed closes.",
        ],
    }
    result["report_sha256"] = canonical_sha256(result)
    return result


def _output_path(value: str) -> Path:
    target = (ROOT / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    allowed_root = (ROOT / "reports" / "v38+" / "verification").resolve()
    try:
        target.parent.relative_to(allowed_root)
    except ValueError as exc:
        raise DatasetBuildError("V38_AUDIT_OUTPUT_MUST_BE_UNDER_VERIFICATION") from exc
    if target.exists():
        raise DatasetBuildError("V38_AUDIT_OUTPUT_EXISTS_REFUSE_OVERWRITE")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path, help="Existing frozen V38 dataset directory.")
    parser.add_argument("--archive", required=True, type=Path, help="Existing local Binance UM archive directory.")
    parser.add_argument("--rebuild-dataset", type=Path,
                        help="Optional fresh offline rebuild to compare by manifest hash.")
    parser.add_argument("--output", required=True,
                        help="New output JSON path under reports/v38+/verification; existing files are not overwritten.")
    args = parser.parse_args(argv)
    try:
        output_path = _output_path(str(args.output))
        report = audit_v38_dataset(args.dataset, args.archive, rebuild_directory=args.rebuild_dataset)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    except (DatasetBuildError, KeyError, OSError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": report["status"],
        "report_sha256": report["report_sha256"],
        "dataset_manifest_sha256": report["dataset"]["manifest_sha256"],
        "global_overlap_component_count": report["overlap"]["global_overlap_component_count"],
        "cross_partition_overlapping_pair_count": report["overlap"]["cross_partition_overlapping_pair_count"],
        "network_calls": 0,
        "model_calls_used": 0,
        "orders_created": 0,
        "output": str(output_path),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
