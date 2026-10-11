"""Frozen, outcome-blind sampling and local reconstruction for V38 market contexts."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from core.replay.binance_history import file_sha256
from core.replay.pa_decision_quality_v37.evidence_builder import (
    HISTORY_WINDOW,
    _manifest,
    _verify_raw_archive,
    build_reconstructed_point,
)
from core.replay.pa_decision_quality_v37.partitioning import (
    load_frozen_partition_policy,
    partition_for_time,
)
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input

ROOT = Path(__file__).resolve().parents[3]
SAMPLE_PLAN_PATH = ROOT / "configs" / "research" / "datasets" / "v38-market-sample-plan-v1.json"
SAMPLE_PLAN_SHA256 = "e08254d847ccfe6280d3cd209bbc53478efcb8eb60a93e54316b48e70dd4e02a"
DATASET_SCHEMA_VERSION = "pa-market-only-v38/dataset-manifest-1"
SELECTION_SCHEMA_VERSION = "pa-market-only-v38/selection-1"
EXPECTED_PARTITION_POLICY_SHA256 = "5169daa812c643cb35b40e3c5327bfbadf30b0c6b9c7db78b172de5c561f7ac7"
EXPECTED_PARTITIONS = {
    "optimization": ("2025-10-01T00:00:00Z", "2026-04-01T00:00:00Z"),
    "validation": ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z"),
    "untouched_test": ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z"),
}


class DatasetBuildError(ValueError):
    """Stable failure code for a frozen V38 data preparation contract."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DatasetBuildError("V38_CANONICAL_JSON_INVALID") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def load_v38_sample_plan(path: Path | None = None) -> tuple[dict[str, Any], str]:
    try:
        plan = json.loads((path or SAMPLE_PLAN_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetBuildError("V38_SAMPLE_PLAN_UNAVAILABLE") from exc
    if (not isinstance(plan, dict)
            or plan.get("schema_version") != "pa-market-only-v38/sample-plan-1"
            or plan.get("plan_id") != "V38_BINANCE_BTC_ETH_MARKET_CONTEXTS_20261009_V1"):
        raise DatasetBuildError("V38_SAMPLE_PLAN_INVALID")
    digest = canonical_sha256(plan)
    if digest != SAMPLE_PLAN_SHA256:
        raise DatasetBuildError("V38_SAMPLE_PLAN_HASH_MISMATCH")
    frozen, frozen_hash = load_frozen_partition_policy()
    if plan.get("partition_policy_sha256") != frozen_hash or frozen_hash != EXPECTED_PARTITION_POLICY_SHA256:
        raise DatasetBuildError("V38_PARTITION_POLICY_HASH_MISMATCH")
    declared = {
        item.get("id"): (item.get("start_utc_inclusive"), item.get("end_utc_exclusive"))
        for item in plan.get("partitions", []) if isinstance(item, dict)
    }
    if declared != EXPECTED_PARTITIONS:
        raise DatasetBuildError("V38_SAMPLE_PLAN_PARTITION_BOUNDARY_MISMATCH")
    policy_partitions = {
        item["id"]: (_utc(item["start"], "V38_PARTITION_TIME_INVALID"),
                     _utc(item["end"], "V38_PARTITION_TIME_INVALID"))
        for item in frozen["partitions"]
    }
    for partition, (start, end) in EXPECTED_PARTITIONS.items():
        if policy_partitions.get(partition) != (_utc(start, "V38_PARTITION_TIME_INVALID"),
                                                _utc(end, "V38_PARTITION_TIME_INVALID")):
            raise DatasetBuildError("V38_SAMPLE_PLAN_PARTITION_BOUNDARY_MISMATCH")
    return plan, digest


def _utc(value: Any, code: str) -> datetime:
    if isinstance(value, datetime):
        point = value
    elif isinstance(value, str):
        try:
            point = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise DatasetBuildError(code) from exc
    else:
        raise DatasetBuildError(code)
    if point.tzinfo is None or point.utcoffset() is None:
        raise DatasetBuildError(code)
    return point.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _month_starts(start: datetime, end: datetime) -> list[datetime]:
    point = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = []
    while point < end:
        months.append(point)
        point = point.replace(year=point.year + 1, month=1) if point.month == 12 else point.replace(month=point.month + 1)
    return months


def _partition_month_entries(plan: dict[str, Any]) -> list[dict[str, Any]]:
    frozen, _ = load_frozen_partition_policy()
    declared = {item["id"]: item for item in plan["partitions"]}
    result = []
    for partition in frozen["partitions"]:
        start, end = _utc(partition["start"], "V38_PARTITION_TIME_INVALID"), _utc(
            partition["end"], "V38_PARTITION_TIME_INVALID",
        )
        item = declared[partition["id"]]
        months = _month_starts(start, end)
        count = item["points_per_symbol"]
        per_month = item["points_per_month"]
        if len(months) * per_month != count:
            raise DatasetBuildError("V38_MONTHLY_SAMPLE_QUOTA_INVALID")
        result.append({"id": partition["id"], "start": start, "end": end,
                       "months": months, "per_month": per_month, "points_per_symbol": count})
    return result


def select_v38_calendar_points() -> list[dict[str, Any]]:
    """Choose a fixed number of UTC calendar anchors without reading market outcomes."""
    plan, plan_sha256 = load_v38_sample_plan()
    symbols = plan["data"].get("symbols")
    if symbols != ["BTCUSDT", "ETHUSDT"]:
        raise DatasetBuildError("V38_SYMBOL_SET_INVALID")
    selection = plan.get("selection", {})
    seed = selection.get("seed")
    min_gap = selection.get("minimum_decision_gap_hours_within_symbol_partition")
    if (not isinstance(seed, str) or not seed
            or isinstance(min_gap, bool) or not isinstance(min_gap, int) or min_gap <= 0
            or selection.get("outcome_based_selection") is not False
            or selection.get("a3_trigger_based_selection") is not False
            or selection.get("model_output_based_selection") is not False):
        raise DatasetBuildError("V38_SELECTION_POLICY_INVALID")
    schedule = _partition_month_entries(plan)
    frozen_policy, _ = load_frozen_partition_policy()
    selected: list[dict[str, Any]] = []
    for symbol in symbols:
        for window in schedule:
            candidates: list[tuple[str, datetime, str]] = []
            for month in window["months"]:
                month_end = month.replace(year=month.year + 1, month=1) if month.month == 12 else month.replace(month=month.month + 1)
                cursor = month.date()
                while cursor < month_end.date():
                    decision_time = datetime.combine(cursor, time(12, 0), UTC)
                    if window["start"] <= decision_time < window["end"]:
                        key = f"{seed}|{symbol}|{window['id']}|{cursor.isoformat()}"
                        candidates.append((hashlib.sha256(key.encode("ascii")).hexdigest(), decision_time,
                                           month.strftime("%Y-%m")))
                    cursor += timedelta(days=1)
            candidates.sort(key=lambda item: (item[0], item[1]))
            month_counts: Counter[str] = Counter()
            chosen: list[datetime] = []
            for _, decision_time, month_id in candidates:
                if month_counts[month_id] >= window["per_month"]:
                    continue
                if any(abs((decision_time - old).total_seconds()) < min_gap * 3600 for old in chosen):
                    continue
                if partition_for_time(decision_time, frozen_policy["partitions"]) != window["id"]:
                    raise DatasetBuildError("V38_PARTITION_LABEL_MISMATCH")
                chosen.append(decision_time)
                month_counts[month_id] += 1
            expected_months = {month.strftime("%Y-%m"): window["per_month"] for month in window["months"]}
            if dict(month_counts) != expected_months:
                raise DatasetBuildError("V38_MONTHLY_SAMPLE_QUOTA_UNMET")
            for decision_time in sorted(chosen):
                point_date = decision_time.strftime("%Y%m%d")
                selected.append({
                    "decision_id": f"v38-{symbol.lower()}-{window['id']}-{point_date}-1200z",
                    "symbol": symbol,
                    "partition": window["id"],
                    "decision_time": _stamp(decision_time),
                    "selection_plan_sha256": plan_sha256,
                    "selection_key_version": "sha256(seed|symbol|partition|YYYY-MM-DD)",
                })
    selected.sort(key=lambda item: (item["partition"], item["symbol"], item["decision_time"]))
    counts = Counter((item["symbol"], item["partition"]) for item in selected)
    if len(selected) != 180 or any(counts[(symbol, part)] != 30
                                   for symbol in symbols for part in EXPECTED_PARTITIONS):
        raise DatasetBuildError("V38_TOTAL_SAMPLE_COUNT_INVALID")
    return selected


def _annotate_overlap(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        grouped[(point["symbol"], point["partition"])].append(point)
    result = []
    for (symbol, partition), rows in sorted(grouped.items()):
        rows.sort(key=lambda item: item["decision_time"])
        cluster = 0
        previous_window_end: datetime | None = None
        for row in rows:
            decision = _utc(row["decision_time"], "V38_DECISION_TIME_INVALID")
            window_start = decision - HISTORY_WINDOW
            overlaps = previous_window_end is not None and window_start < previous_window_end
            if previous_window_end is not None and not overlaps:
                cluster += 1
            result.append({
                **row,
                "input_window_start": _stamp(window_start),
                "input_window_end_exclusive": _stamp(decision),
                "overlaps_previous_input_window": overlaps,
                "overlap_cluster_id": f"{symbol}-{partition}-cluster-{cluster + 1:02d}",
                "overlap_seconds_with_previous": max(
                    0, int((previous_window_end - window_start).total_seconds())
                ) if overlaps and previous_window_end else 0,
                "warmup_crosses_partition_start": window_start < _utc(
                    next(item["start_utc_inclusive"] for item in load_v38_sample_plan()[0]["partitions"]
                         if item["id"] == partition), "V38_PARTITION_TIME_INVALID",
                ),
            })
            previous_window_end = decision
    return sorted(result, key=lambda item: (item["partition"], item["symbol"], item["decision_time"]))


def _write_new_json(path: Path, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    try:
        with path.open("xb") as stream:
            stream.write(encoded)
    except FileExistsError as exc:
        raise DatasetBuildError("V38_OUTPUT_EXISTS_REFUSE_OVERWRITE") from exc
    return hashlib.sha256(encoded).hexdigest()


def build_v38_dataset(archive_directory: Path, output_directory: Path) -> dict[str, Any]:
    """Build 180 contexts from the existing verified local archive; never fetch or label outcomes."""
    plan, plan_sha256 = load_v38_sample_plan()
    archive_directory = Path(archive_directory).resolve()
    output_directory = Path(output_directory).resolve()
    reports_root = (ROOT / "reports" / "v38+").resolve()
    try:
        output_directory.relative_to(reports_root)
    except ValueError as exc:
        raise DatasetBuildError("V38_OUTPUT_MUST_BE_UNDER_IGNORED_REPORTS_DIRECTORY") from exc
    if output_directory.exists():
        if any(output_directory.iterdir()):
            raise DatasetBuildError("V38_OUTPUT_DIRECTORY_NOT_EMPTY_REFUSE_OVERWRITE")
    else:
        output_directory.mkdir(parents=True)

    source_manifest, database_path, source_manifest_sha256, _archives = _manifest(archive_directory)
    if (file_sha256(database_path) != source_manifest.get("dataset_sha256")
            or source_manifest.get("complete_data") is not True
            or source_manifest.get("exchange") != "BINANCE_UM"
            or source_manifest.get("symbols") != plan["data"]["symbols"]
            or source_manifest.get("source_type") != "OFFICIAL_MONTHLY_PUBLIC_ARCHIVES"):
        raise DatasetBuildError("V38_VERIFIED_ARCHIVE_IDENTITY_OR_COVERAGE_INVALID")
    if not all(
        source_manifest.get("coverage", {}).get(symbol, {}).get("bars", {}).get("complete") is True
        and source_manifest.get("coverage", {}).get(symbol, {}).get("bars", {}).get("gap_count") == 0
        for symbol in plan["data"]["symbols"]
    ):
        raise DatasetBuildError("V38_VERIFIED_ARCHIVE_BAR_COVERAGE_INCOMPLETE")
    archive_records = source_manifest.get("archived_files")
    if not isinstance(archive_records, list) or not archive_records:
        raise DatasetBuildError("V38_VERIFIED_ARCHIVE_FILES_MISSING")
    for archive_record in archive_records:
        _verify_raw_archive(archive_directory, archive_record)

    chosen = _annotate_overlap(select_v38_calendar_points())
    optimization_validation: list[dict[str, Any]] = []
    untouched_test: list[dict[str, Any]] = []
    descriptors: list[dict[str, Any]] = []
    for selection in chosen:
        point = build_reconstructed_point(
            archive_directory,
            symbol=selection["symbol"],
            decision_time=selection["decision_time"],
            decision_id=selection["decision_id"],
            partition=selection["partition"],
            availability_delay_seconds=plan["data"]["availability_delay_seconds"],
        )
        projected = {
            "decision_id": point["decision_id"],
            "decision_time": point["decision_time"],
            "symbol": point["symbol"],
            "partition": point["partition"],
            "bars_by_timeframe": point["bars_by_timeframe"],
        }
        market_input = build_market_only_input(projected)
        counts = {timeframe: len(point["bars_by_timeframe"][timeframe])
                  for timeframe in ("15m", "5m", "1h", "4h")}
        descriptor = {
            **selection,
            "market_input_sha256": market_input["market_input_sha256"],
            "bar_count_by_timeframe": counts,
            "evidence_ref_count": len(market_input["evidence_refs"]),
            "price_evidence_grade": market_input["provenance"]["price_evidence_grade"],
            "availability_evidence_grade": market_input["provenance"]["availability_evidence_grade"],
            "archive_file_sha256s": market_input["provenance"]["archive_file_sha256s"],
        }
        descriptors.append(descriptor)
        if selection["partition"] == "untouched_test":
            untouched_test.append({
                key: descriptor[key]
                for key in (
                    "decision_id", "symbol", "partition", "decision_time", "selection_plan_sha256",
                    "market_input_sha256", "bar_count_by_timeframe", "evidence_ref_count",
                    "price_evidence_grade", "availability_evidence_grade", "archive_file_sha256s",
                    "input_window_start", "input_window_end_exclusive", "overlaps_previous_input_window",
                    "overlap_cluster_id", "overlap_seconds_with_previous", "warmup_crosses_partition_start",
                )
            })
        else:
            optimization_validation.append(projected)

    if len(descriptors) != 180 or len(optimization_validation) != 120 or len(untouched_test) != 60:
        raise DatasetBuildError("V38_RECONSTRUCTED_SAMPLE_COUNT_INVALID")
    counts_by_symbol_partition = Counter((item["symbol"], item["partition"]) for item in descriptors)
    expected_counts = {(symbol, part): 30 for symbol in plan["data"]["symbols"] for part in EXPECTED_PARTITIONS}
    if dict(counts_by_symbol_partition) != expected_counts:
        raise DatasetBuildError("V38_RECONSTRUCTED_PARTITION_COUNTS_INVALID")

    input_document = {
        "schema_version": "pa-market-only-v38/market-input-dataset-1",
        "dataset_kind": "MARKET_ONLY_CAUSAL_CONTEXTS_NO_LABELS",
        "plan_sha256": plan_sha256,
        "partition_policy_sha256": EXPECTED_PARTITION_POLICY_SHA256,
        "source_database_sha256": source_manifest["dataset_sha256"],
        "source_manifest_sha256": source_manifest_sha256,
        "partition_scope": ["optimization", "validation"],
        "decision_points": optimization_validation,
        "outcome_labels_included": False,
        "model_outputs_included": False,
    }
    untouched_document = {
        "schema_version": "pa-market-only-v38/untouched-test-sealed-1",
        "partition": "untouched_test",
        "plan_sha256": plan_sha256,
        "partition_policy_sha256": EXPECTED_PARTITION_POLICY_SHA256,
        "source_database_sha256": source_manifest["dataset_sha256"],
        "persist_only_hashes_and_coverage": True,
        "market_input_payloads_included": False,
        "labels_or_evaluation_included": False,
        "decision_points": untouched_test,
    }
    input_hash = _write_new_json(output_directory / "optimization-validation-inputs.json", input_document)
    test_hash = _write_new_json(output_directory / "untouched-test-sealed-manifest.json", untouched_document)
    partition_counts = Counter(item["partition"] for item in descriptors)
    overlap_counts = Counter(item["overlap_cluster_id"] for item in descriptors)
    manifest: dict[str, Any] = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_id": plan["plan_id"],
        "plan_sha256": plan_sha256,
        "base_commit": plan["base_commit"],
        "source": {
            "archive_directory_role": "LOCAL_VERIFIED_PUBLIC_BINANCE_UM_ARCHIVE",
            "database_sha256": source_manifest["dataset_sha256"],
            "manifest_sha256": source_manifest_sha256,
            "archived_file_count": len(archive_records),
            "archive_files_verified": len(archive_records),
            "source_window_start": source_manifest.get("window_start"),
            "source_window_end": source_manifest.get("window_end"),
            "symbols": plan["data"]["symbols"],
            "complete_data": True,
            "network_calls": 0,
        },
        "partitions": {key: value for key, value in sorted(partition_counts.items())},
        "points_by_symbol_partition": {
            f"{symbol}:{partition}": count
            for (symbol, partition), count in sorted(counts_by_symbol_partition.items())
        },
        "total_market_contexts": len(descriptors),
        "optimization_validation_input_count": len(optimization_validation),
        "untouched_test_hash_only_count": len(untouched_test),
        "overlap_cluster_count": len(overlap_counts),
        "overlap_cluster_sizes": dict(sorted(Counter(item["overlap_cluster_id"] for item in descriptors).items())),
        "price_evidence_grade": plan["data"]["price_evidence_grade"],
        "availability_evidence_grade": plan["data"]["availability_evidence_grade"],
        "availability_delay_seconds": plan["data"]["availability_delay_seconds"],
        "gate_executable_trade_samples": 0,
        "completed_closes": 0,
        "model_outputs": 0,
        "model_calls_used": 0,
        "orders_created": 0,
        "a0_v25_exact_recovered": 0,
        "a0_v25_relabeling_permitted": False,
        "outcome_labels_included": False,
        "untouched_test_payloads_included": False,
        "files": {
            "optimization-validation-inputs.json": input_hash,
            "untouched-test-sealed-manifest.json": test_hash,
        },
        "points": descriptors,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    _write_new_json(output_directory / "dataset-manifest.json", manifest)
    return manifest
