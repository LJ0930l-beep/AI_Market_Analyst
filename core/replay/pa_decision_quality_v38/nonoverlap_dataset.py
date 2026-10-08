"""Frozen V38 paired calendar sample with nonoverlapping 8-day input windows."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

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
from core.replay.pa_decision_quality_v38.dataset import (
    SAMPLE_PLAN_PATH,
    canonical_sha256,
    load_v38_sample_plan,
)
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input

ROOT = Path(__file__).resolve().parents[3]
NONOVERLAP_PLAN_PATH = ROOT / "configs" / "research" / "datasets" / "v38-nonoverlap-sample-plan-v1.json"
NONOVERLAP_PLAN_SHA256 = "561e2b78bb046468c2d07d6378f913b35aee0f551ab50c8568c8021ca336bf9a"
NONOVERLAP_PLAN_ID = "V38_BINANCE_BTC_ETH_NONOVERLAP_CONTEXTS_20261009_V1"
EXPECTED_PARTITIONS = {
    "optimization": ("2025-10-01T00:00:00Z", "2026-04-01T00:00:00Z", 20),
    "validation": ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z", 10),
    "untouched_test": ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z", 10),
}
TIMEFRAMES = ("15m", "5m", "1h", "4h")
HEX_256 = frozenset("0123456789abcdef")


class NonOverlapDatasetError(ValueError):
    """Stable failure code for the immutable V38 nonoverlap sample contract."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise NonOverlapDatasetError(code)


def _utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        raise NonOverlapDatasetError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise NonOverlapDatasetError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NonOverlapDatasetError(code)
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NonOverlapDatasetError("V38_NONOVERLAP_PLAN_UNAVAILABLE") from exc
    if not isinstance(value, dict):
        raise NonOverlapDatasetError("V38_NONOVERLAP_PLAN_INVALID")
    return value


def load_nonoverlap_sample_plan(
    path: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Load the new, hash-frozen plan without changing the original V38 plan."""
    plan = _read_plan(path or NONOVERLAP_PLAN_PATH)
    if (plan.get("schema_version") != "pa-market-only-v38/nonoverlap-sample-plan-1"
            or plan.get("plan_id") != NONOVERLAP_PLAN_ID):
        raise NonOverlapDatasetError("V38_NONOVERLAP_PLAN_INVALID")
    digest = canonical_sha256(plan)
    if digest != NONOVERLAP_PLAN_SHA256:
        raise NonOverlapDatasetError("V38_NONOVERLAP_PLAN_HASH_MISMATCH")

    parent_plan, parent_hash = load_v38_sample_plan(path=SAMPLE_PLAN_PATH)
    if (plan.get("parent_plan_id") != parent_plan["plan_id"]
            or plan.get("parent_plan_sha256") != parent_hash):
        raise NonOverlapDatasetError("V38_NONOVERLAP_PARENT_PLAN_MISMATCH")
    frozen_policy, policy_hash = load_frozen_partition_policy()
    if plan.get("partition_policy_sha256") != policy_hash:
        raise NonOverlapDatasetError("V38_NONOVERLAP_PARTITION_POLICY_MISMATCH")

    declared = plan.get("partitions")
    if not isinstance(declared, list) or len(declared) != len(EXPECTED_PARTITIONS):
        raise NonOverlapDatasetError("V38_NONOVERLAP_PARTITIONS_INVALID")
    expected_order = list(EXPECTED_PARTITIONS)
    for index, item in enumerate(declared):
        if not isinstance(item, dict):
            raise NonOverlapDatasetError("V38_NONOVERLAP_PARTITIONS_INVALID")
        partition_id = expected_order[index]
        start, end, anchors = EXPECTED_PARTITIONS[partition_id]
        if (item.get("id") != partition_id
                or item.get("start_utc_inclusive") != start
                or item.get("end_utc_exclusive") != end
                or item.get("first_decision_offset_days") != 8
                or item.get("decision_spacing_days") != 9
                or item.get("expected_anchor_count") != anchors
                or item.get("expected_contexts_per_symbol") != anchors):
            raise NonOverlapDatasetError("V38_NONOVERLAP_PARTITION_CONTRACT_MISMATCH")

    policy_bounds = {
        item["id"]: (_utc(item["start"], "V38_NONOVERLAP_POLICY_TIME_INVALID"),
                     _utc(item["end"], "V38_NONOVERLAP_POLICY_TIME_INVALID"))
        for item in frozen_policy["partitions"]
    }
    for partition_id, (start, end, _) in EXPECTED_PARTITIONS.items():
        if policy_bounds.get(partition_id) != (
            _utc(start, "V38_NONOVERLAP_PLAN_TIME_INVALID"),
            _utc(end, "V38_NONOVERLAP_PLAN_TIME_INVALID"),
        ):
            raise NonOverlapDatasetError("V38_NONOVERLAP_PARTITION_POLICY_MISMATCH")

    selection = plan.get("selection")
    expected_selection = {
        "algorithm": "FIXED_UTC_N_DAY_GRID_FROM_PARTITION_START",
        "decision_time_utc": "12:00:00",
        "history_window_days": 8,
        "purge_history_inside_partition": True,
        "same_temporal_anchors_for_all_symbols": True,
        "outcome_based_selection": False,
        "market_direction_or_volatility_based_selection": False,
        "model_output_based_selection": False,
        "a3_trigger_based_selection": False,
        "runtime_randomness": False,
        "cross_symbol_rows_are_paired": True,
        "iid_or_independent_trade_claim": False,
    }
    if selection != expected_selection:
        raise NonOverlapDatasetError("V38_NONOVERLAP_SELECTION_CONTRACT_MISMATCH")
    if timedelta(days=selection["history_window_days"]) != HISTORY_WINDOW:
        raise NonOverlapDatasetError("V38_NONOVERLAP_HISTORY_WINDOW_DRIFT")
    if plan.get("data", {}).get("symbols") != ["BTCUSDT", "ETHUSDT"]:
        raise NonOverlapDatasetError("V38_NONOVERLAP_SYMBOL_SET_INVALID")
    invariants = plan.get("invariants")
    if not isinstance(invariants, dict) or any(value is not False for value in (
        invariants.get("same_symbol_input_windows_overlap"),
        invariants.get("cross_partition_input_windows_overlap"),
        invariants.get("untouched_test_payloads_included"),
        invariants.get("historical_v25_samples_modified"),
    )):
        raise NonOverlapDatasetError("V38_NONOVERLAP_INVARIANTS_INVALID")
    if (invariants.get("input_window_start_inclusive") != "decision_time_minus_8_days"
            or invariants.get("input_window_end_exclusive") != "decision_time"
            or invariants.get("minimum_anchor_spacing_days") != 9):
        raise NonOverlapDatasetError("V38_NONOVERLAP_INVARIANTS_INVALID")
    source = plan.get("source")
    if not isinstance(source, dict) or any(
        not isinstance(source.get(field), str)
        or len(source[field]) != 64
        or not set(source[field]) <= HEX_256
        for field in ("source_database_sha256", "source_manifest_sha256")
    ):
        raise NonOverlapDatasetError("V38_NONOVERLAP_SOURCE_BINDING_INVALID")
    return plan, digest


def select_nonoverlap_calendar_points() -> list[dict[str, Any]]:
    """Select paired BTC/ETH anchors with purged, disjoint 8-day history windows."""
    plan, plan_sha256 = load_nonoverlap_sample_plan()
    symbols = plan["data"]["symbols"]
    decision_clock = time.fromisoformat(plan["selection"]["decision_time_utc"])
    history_window = timedelta(days=plan["selection"]["history_window_days"])
    selected: list[dict[str, Any]] = []
    anchors_by_partition: dict[str, list[datetime]] = {}

    for partition in plan["partitions"]:
        partition_id = partition["id"]
        start = _utc(partition["start_utc_inclusive"], "V38_NONOVERLAP_PARTITION_TIME_INVALID")
        end = _utc(partition["end_utc_exclusive"], "V38_NONOVERLAP_PARTITION_TIME_INVALID")
        spacing = timedelta(days=partition["decision_spacing_days"])
        decision = datetime.combine(
            (start + timedelta(days=partition["first_decision_offset_days"])).date(),
            decision_clock,
            UTC,
        )
        anchors: list[datetime] = []
        while decision < end:
            _require(decision - history_window >= start,
                     "V38_NONOVERLAP_WARMUP_CROSSES_PARTITION")
            if partition_for_time(decision, [
                {"id": item["id"], "start": item["start_utc_inclusive"],
                 "end": item["end_utc_exclusive"]}
                for item in plan["partitions"]
            ]) != partition_id:
                raise NonOverlapDatasetError("V38_NONOVERLAP_PARTITION_LABEL_MISMATCH")
            if anchors and decision - anchors[-1] < history_window:
                raise NonOverlapDatasetError("V38_NONOVERLAP_ANCHOR_SPACING_INVALID")
            anchors.append(decision)
            decision += spacing
        if len(anchors) != partition["expected_anchor_count"]:
            raise NonOverlapDatasetError("V38_NONOVERLAP_ANCHOR_COUNT_INVALID")
        anchors_by_partition[partition_id] = anchors

        for anchor in anchors:
            pair_id = f"v38n-{partition_id}-{anchor:%Y%m%d}-1200z"
            for symbol in symbols:
                window_start = anchor - history_window
                selected.append({
                    "decision_id": f"{pair_id}-{symbol.lower()}",
                    "decision_time": _stamp(anchor),
                    "symbol": symbol,
                    "partition": partition_id,
                    "selection_plan_sha256": plan_sha256,
                    "selection_key_version": "fixed-utc-grid-from-partition-start-v1",
                    "paired_anchor_group_id": pair_id,
                    "input_window_start": _stamp(window_start),
                    "input_window_end_exclusive": _stamp(anchor),
                    "overlaps_previous_input_window": False,
                    "overlap_cluster_id": f"{symbol}-{partition_id}-nonoverlap-{anchor:%Y%m%d}",
                    "overlap_seconds_with_previous": 0,
                    "warmup_crosses_partition_start": False,
                })

    if set(anchors_by_partition) != set(EXPECTED_PARTITIONS):
        raise NonOverlapDatasetError("V38_NONOVERLAP_PARTITION_COVERAGE_INVALID")
    result = sorted(selected, key=lambda item: (
        list(EXPECTED_PARTITIONS).index(item["partition"]), item["decision_time"], item["symbol"],
    ))
    if len(result) != 80:
        raise NonOverlapDatasetError("V38_NONOVERLAP_TOTAL_CONTEXT_COUNT_INVALID")
    _validate_nonoverlap_selection(result, plan)
    return result


def _validate_nonoverlap_selection(points: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    anchors: dict[str, set[tuple[str, str]]] = defaultdict(set)
    partitions = {item["id"]: item for item in plan["partitions"]}
    policy_partitions = [
        {"id": item["id"], "start": item["start_utc_inclusive"], "end": item["end_utc_exclusive"]}
        for item in plan["partitions"]
    ]
    history_window = timedelta(days=plan["selection"]["history_window_days"])
    expected_anchor_times: dict[str, set[str]] = {}
    for partition_id, item in partitions.items():
        start = _utc(item["start_utc_inclusive"], "V38_NONOVERLAP_PARTITION_TIME_INVALID")
        end = _utc(item["end_utc_exclusive"], "V38_NONOVERLAP_PARTITION_TIME_INVALID")
        decision = datetime.combine(
            (start + timedelta(days=item["first_decision_offset_days"])).date(),
            time.fromisoformat(plan["selection"]["decision_time_utc"]),
            UTC,
        )
        expected_times: set[str] = set()
        spacing = timedelta(days=item["decision_spacing_days"])
        while decision < end:
            expected_times.add(_stamp(decision))
            decision += spacing
        expected_anchor_times[partition_id] = expected_times

    for point in points:
        _require(isinstance(point, dict), "V38_NONOVERLAP_POINT_INVALID")
        symbol, partition_id = point.get("symbol"), point.get("partition")
        _require(isinstance(symbol, str) and symbol in plan["data"]["symbols"]
                 and isinstance(partition_id, str) and partition_id in partitions,
                 "V38_NONOVERLAP_POINT_IDENTITY_INVALID")
        decision = _utc(point.get("decision_time"), "V38_NONOVERLAP_DECISION_TIME_INVALID")
        _require(partition_for_time(decision, policy_partitions) == partition_id,
                 "V38_NONOVERLAP_PARTITION_LABEL_MISMATCH")
        _require(_stamp(decision) in expected_anchor_times[partition_id],
                 "V38_NONOVERLAP_ANCHOR_TIME_INVALID")
        pair_id = f"v38n-{partition_id}-{decision:%Y%m%d}-1200z"
        _require(point.get("paired_anchor_group_id") == pair_id
                 and point.get("decision_id") == f"{pair_id}-{symbol.lower()}",
                 "V38_NONOVERLAP_POINT_ID_INVALID")
        grouped[(point["symbol"], point["partition"])].append(point)
        anchors[point["paired_anchor_group_id"]].add((point["symbol"], point["decision_time"]))
        start = _utc(point["input_window_start"], "V38_NONOVERLAP_WINDOW_TIME_INVALID")
        end = _utc(point["input_window_end_exclusive"], "V38_NONOVERLAP_WINDOW_TIME_INVALID")
        bounds = partitions[point["partition"]]
        _require(start == decision - history_window and end == decision
                 and start >= _utc(bounds["start_utc_inclusive"], "V38_NONOVERLAP_PARTITION_TIME_INVALID")
                 and end < _utc(bounds["end_utc_exclusive"], "V38_NONOVERLAP_PARTITION_TIME_INVALID"),
                 "V38_NONOVERLAP_WINDOW_OR_PARTITION_INVALID")

    symbols = plan["data"]["symbols"]
    expected_per_partition = {
        item["id"]: item["expected_anchor_count"] for item in plan["partitions"]
    }
    for partition_id, expected_anchors in expected_per_partition.items():
        observed_anchor_ids = {
            point["paired_anchor_group_id"] for point in points if point["partition"] == partition_id
        }
        _require(len(observed_anchor_ids) == expected_anchors,
                 "V38_NONOVERLAP_ANCHOR_COUNT_INVALID")
        observed_times = {
            point["decision_time"] for point in points if point["partition"] == partition_id
        }
        _require(observed_times == expected_anchor_times[partition_id],
                 "V38_NONOVERLAP_ANCHOR_COVERAGE_INVALID")
        for anchor_id in observed_anchor_ids:
            paired = anchors[anchor_id]
            _require({symbol for symbol, _ in paired} == set(symbols) and len({t for _, t in paired}) == 1,
                     "V38_NONOVERLAP_ASSET_PAIR_INVALID")

    for (symbol, partition_id), rows in grouped.items():
        rows.sort(key=lambda row: row["decision_time"])
        previous_end: datetime | None = None
        for row in rows:
            start = _utc(row["input_window_start"], "V38_NONOVERLAP_WINDOW_TIME_INVALID")
            end = _utc(row["input_window_end_exclusive"], "V38_NONOVERLAP_WINDOW_TIME_INVALID")
            if previous_end is not None:
                _require(start >= previous_end, "V38_NONOVERLAP_WINDOWS_OVERLAP")
            previous_end = end
        expected = expected_per_partition[partition_id]
        _require(len(rows) == expected, "V38_NONOVERLAP_SYMBOL_PARTITION_COUNT_INVALID")


def _write_json_exclusive(path: Path, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    try:
        with path.open("xb") as stream:
            stream.write(encoded)
    except FileExistsError as exc:
        raise NonOverlapDatasetError("V38_NONOVERLAP_OUTPUT_EXISTS_REFUSE_OVERWRITE") from exc
    return hashlib.sha256(encoded).hexdigest()


def _verified_archive(archive_directory: Path, plan: dict[str, Any]) -> tuple[dict[str, Any], Path, str]:
    try:
        source_manifest, database_path, source_manifest_sha256, _archives = _manifest(archive_directory)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise NonOverlapDatasetError("V38_NONOVERLAP_ARCHIVE_INVALID") from exc
    source = plan["source"]
    if (source_manifest_sha256 != source["source_manifest_sha256"]
            or source_manifest.get("dataset_sha256") != source["source_database_sha256"]
            or source_manifest.get("symbols") != plan["data"]["symbols"]
            or source_manifest.get("complete_data") is not True
            or source_manifest.get("exchange") != "BINANCE_UM"
            or source_manifest.get("source_type") != "OFFICIAL_MONTHLY_PUBLIC_ARCHIVES"):
        raise NonOverlapDatasetError("V38_NONOVERLAP_ARCHIVE_BINDING_MISMATCH")
    coverage = source_manifest.get("coverage", {})
    if not all(
        coverage.get(symbol, {}).get("bars", {}).get("complete") is True
        and coverage.get(symbol, {}).get("bars", {}).get("gap_count") == 0
        for symbol in plan["data"]["symbols"]
    ):
        raise NonOverlapDatasetError("V38_NONOVERLAP_ARCHIVE_COVERAGE_INVALID")
    records = source_manifest.get("archived_files")
    if not isinstance(records, list) or not records:
        raise NonOverlapDatasetError("V38_NONOVERLAP_ARCHIVE_FILES_MISSING")
    for record in records:
        _verify_raw_archive(archive_directory, record)
    return source_manifest, database_path, source_manifest_sha256


def build_nonoverlap_v38_dataset(archive_directory: Path, output_directory: Path) -> dict[str, Any]:
    """Build paired market-only contexts to a new local directory; never fetch or label outcomes."""
    plan, plan_sha256 = load_nonoverlap_sample_plan()
    archive_directory = Path(archive_directory).resolve()
    output_directory = Path(output_directory).resolve()
    allowed_root = (ROOT / "reports" / "v38+").resolve()
    try:
        output_directory.relative_to(allowed_root)
    except ValueError as exc:
        raise NonOverlapDatasetError("V38_NONOVERLAP_OUTPUT_MUST_BE_UNDER_IGNORED_REPORTS") from exc
    if output_directory.exists():
        if any(output_directory.iterdir()):
            raise NonOverlapDatasetError("V38_NONOVERLAP_OUTPUT_DIRECTORY_NOT_EMPTY")
    else:
        output_directory.mkdir(parents=True)

    source_manifest, _database_path, source_manifest_sha256 = _verified_archive(archive_directory, plan)
    selections = select_nonoverlap_calendar_points()
    inputs: list[dict[str, Any]] = []
    sealed: list[dict[str, Any]] = []
    descriptors: list[dict[str, Any]] = []
    used_archive_hashes: set[str] = set()
    for selection in selections:
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
        provenance = market_input["provenance"]
        used_archive_hashes.update(provenance["archive_file_sha256s"])
        descriptor = {
            **selection,
            "market_input_sha256": market_input["market_input_sha256"],
            "bar_count_by_timeframe": {
                timeframe: len(point["bars_by_timeframe"][timeframe]) for timeframe in TIMEFRAMES
            },
            "evidence_ref_count": len(market_input["evidence_refs"]),
            "price_evidence_grade": provenance["price_evidence_grade"],
            "availability_evidence_grade": provenance["availability_evidence_grade"],
            "archive_file_sha256s": provenance["archive_file_sha256s"],
        }
        descriptors.append(descriptor)
        if selection["partition"] == "untouched_test":
            sealed.append({
                field: descriptor[field]
                for field in (
                    "decision_id", "decision_time", "symbol", "partition", "selection_plan_sha256",
                    "selection_key_version", "paired_anchor_group_id", "input_window_start",
                    "input_window_end_exclusive", "overlaps_previous_input_window", "overlap_cluster_id",
                    "overlap_seconds_with_previous", "warmup_crosses_partition_start", "market_input_sha256",
                    "bar_count_by_timeframe", "evidence_ref_count", "price_evidence_grade",
                    "availability_evidence_grade", "archive_file_sha256s",
                )
            })
        else:
            inputs.append(projected)

    _require(len(descriptors) == 80 and len(inputs) == 60 and len(sealed) == 20,
             "V38_NONOVERLAP_RECONSTRUCTED_COUNT_INVALID")
    verified_archive_hashes = {
        record["sha256"] for record in source_manifest["archived_files"] if isinstance(record, dict)
    }
    _require(used_archive_hashes <= verified_archive_hashes,
             "V38_NONOVERLAP_USED_ARCHIVE_NOT_VERIFIED")
    _require(all(
        item["price_evidence_grade"] == [plan["data"]["price_evidence_grade"]]
        and item["availability_evidence_grade"] == [plan["data"]["availability_evidence_grade"]]
        for item in descriptors
    ), "V38_NONOVERLAP_EVIDENCE_GRADE_MISMATCH")

    input_document = {
        "schema_version": "pa-market-only-v38/nonoverlap-market-input-dataset-1",
        "dataset_kind": "MARKET_ONLY_CAUSAL_PAIRED_CONTEXTS_NO_LABELS",
        "plan_sha256": plan_sha256,
        "partition_policy_sha256": plan["partition_policy_sha256"],
        "source_database_sha256": source_manifest["dataset_sha256"],
        "source_manifest_sha256": source_manifest_sha256,
        "partition_scope": ["optimization", "validation"],
        "decision_points": inputs,
        "outcome_labels_included": False,
        "model_outputs_included": False,
    }
    sealed_document = {
        "schema_version": "pa-market-only-v38/nonoverlap-untouched-test-sealed-1",
        "partition": "untouched_test",
        "plan_sha256": plan_sha256,
        "partition_policy_sha256": plan["partition_policy_sha256"],
        "source_database_sha256": source_manifest["dataset_sha256"],
        "persist_only_hashes_and_coverage": True,
        "market_input_payloads_included": False,
        "labels_or_evaluation_included": False,
        "decision_points": sealed,
    }
    input_sha256 = _write_json_exclusive(output_directory / "optimization-validation-inputs.json", input_document)
    sealed_sha256 = _write_json_exclusive(output_directory / "untouched-test-sealed-manifest.json", sealed_document)
    counts = Counter(item["partition"] for item in descriptors)
    contexts_by_symbol_partition = Counter((item["symbol"], item["partition"]) for item in descriptors)
    anchors_by_partition = {
        item["id"]: item["expected_anchor_count"] for item in plan["partitions"]
    }
    manifest: dict[str, Any] = {
        "schema_version": "pa-market-only-v38/nonoverlap-dataset-manifest-1",
        "dataset_id": plan["plan_id"],
        "plan_sha256": plan_sha256,
        "parent_dataset_manifest_sha256": plan["parent_dataset_manifest_sha256"],
        "base_commit": plan["base_commit"],
        "source": {
            "archive_directory_role": "LOCAL_VERIFIED_PUBLIC_BINANCE_UM_ARCHIVE",
            "database_sha256": source_manifest["dataset_sha256"],
            "manifest_sha256": source_manifest_sha256,
            "archive_files_verified": len(source_manifest["archived_files"]),
            "used_archive_hash_count": len(used_archive_hashes),
            "source_window_start": source_manifest.get("window_start"),
            "source_window_end": source_manifest.get("window_end"),
            "symbols": plan["data"]["symbols"],
            "complete_data": True,
            "network_calls": 0,
        },
        "partition_counts": dict(sorted(counts.items())),
        "temporal_anchor_counts": anchors_by_partition,
        "contexts_by_symbol_partition": {
            f"{symbol}:{partition}": count
            for (symbol, partition), count in sorted(contexts_by_symbol_partition.items())
        },
        "total_market_contexts": len(descriptors),
        "optimization_validation_input_count": len(inputs),
        "untouched_test_hash_only_count": len(sealed),
        "paired_temporal_anchor_count": sum(anchors_by_partition.values()),
        "same_symbol_input_window_overlap_count": 0,
        "cross_partition_input_window_overlap_count": 0,
        "iid_or_independent_trade_claim": False,
        "price_evidence_grade": plan["data"]["price_evidence_grade"],
        "availability_evidence_grade": plan["data"]["availability_evidence_grade"],
        "availability_delay_seconds": plan["data"]["availability_delay_seconds"],
        "gate_executable_trade_samples": 0,
        "completed_closes": 0,
        "model_outputs": 0,
        "model_calls_used": 0,
        "orders_created": 0,
        "outcome_labels_included": False,
        "untouched_test_payloads_included": False,
        "files": {
            "optimization-validation-inputs.json": input_sha256,
            "untouched-test-sealed-manifest.json": sealed_sha256,
        },
        "points": descriptors,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    _write_json_exclusive(output_directory / "dataset-manifest.json", manifest)
    return manifest
