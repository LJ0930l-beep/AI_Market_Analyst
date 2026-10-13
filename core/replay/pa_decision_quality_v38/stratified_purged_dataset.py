"""Frozen seeded, monthly-stratified V38 sample with purged causal windows."""
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
from core.replay.pa_decision_quality_v38.dataset import canonical_sha256, load_v38_sample_plan
from core.replay.pa_decision_quality_v38.market_only import build_market_only_input
from core.replay.pa_decision_quality_v38.nonoverlap_dataset import load_nonoverlap_sample_plan

ROOT = Path(__file__).resolve().parents[3]
STRATIFIED_PURGED_PLAN_PATH = ROOT / "configs" / "research" / "datasets" / "v38-stratified-purged-sample-plan-v1.json"
STRATIFIED_PURGED_PLAN_ID = "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"
STRATIFIED_PURGED_PLAN_SHA256 = "094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f"
EXPECTED_PARTITIONS = {
    "optimization": ("2025-10-01T00:00:00Z", "2026-04-01T00:00:00Z", 18),
    "validation": ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z", 9),
    "untouched_test": ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z", 9),
}
REGULAR_SLOTS = (("S1", (7, 8)), ("S2", (16, 17)), ("S3", (25, 26)))
FIRST_MONTH_SLOTS = (("S1", (9, 10)), ("S2", (18, 19)), ("S3", (27, 28)))
SYMBOLS = ("BTCUSDT", "ETHUSDT")


class StratifiedPurgedDatasetError(ValueError):
    """Stable failure code for the frozen V38 stratified-purged sample contract."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise StratifiedPurgedDatasetError(code)


def _utc(value: Any, code: str) -> datetime:
    if not isinstance(value, str):
        raise StratifiedPurgedDatasetError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StratifiedPurgedDatasetError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StratifiedPurgedDatasetError(code)
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StratifiedPurgedDatasetError(code) from exc
    if not isinstance(value, dict):
        raise StratifiedPurgedDatasetError(code)
    return value


def load_stratified_purged_sample_plan(
    path: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Load the immutable third sample plan without changing either parent dataset."""
    plan = _read_json(path or STRATIFIED_PURGED_PLAN_PATH, "V38_STRATIFIED_PURGED_PLAN_UNAVAILABLE")
    if (plan.get("schema_version") != "pa-market-only-v38/stratified-purged-sample-plan-1"
            or plan.get("plan_id") != STRATIFIED_PURGED_PLAN_ID):
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_PLAN_INVALID")
    digest = canonical_sha256(plan)
    if digest != STRATIFIED_PURGED_PLAN_SHA256:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_PLAN_HASH_MISMATCH")

    frozen, policy_sha256 = load_frozen_partition_policy()
    if policy_sha256 != plan.get("partition_policy_sha256"):
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_PARTITION_POLICY_MISMATCH")
    try:
        primary_parent, primary_parent_sha256 = load_v38_sample_plan()
        sensitivity_parent, sensitivity_parent_sha256 = load_nonoverlap_sample_plan()
    except (ValueError, OSError) as exc:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_PARENT_PLAN_UNAVAILABLE") from exc
    expected_parents = [
        {"plan_id": primary_parent["plan_id"], "plan_sha256": primary_parent_sha256},
        {"plan_id": sensitivity_parent["plan_id"], "plan_sha256": sensitivity_parent_sha256},
    ]
    if plan.get("parent_plans") != expected_parents:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_PARENT_PLAN_MISMATCH")
    declared = plan.get("partitions")
    if not isinstance(declared, list) or len(declared) != len(EXPECTED_PARTITIONS):
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_PARTITIONS_INVALID")
    frozen_by_id = {item["id"]: item for item in frozen["partitions"]}
    for index, (partition_id, (start, end, anchors)) in enumerate(EXPECTED_PARTITIONS.items()):
        item = declared[index]
        if (not isinstance(item, dict) or item.get("id") != partition_id
                or item.get("start_utc_inclusive") != start
                or item.get("end_utc_exclusive") != end
                or item.get("expected_anchor_count") != anchors
                or item.get("expected_context_count") != anchors * len(SYMBOLS)):
            raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_PARTITION_CONTRACT_MISMATCH")
        frozen_item = frozen_by_id.get(partition_id)
        if (not isinstance(frozen_item, dict)
                or _utc(frozen_item.get("start"), "V38_STRATIFIED_PURGED_POLICY_TIME_INVALID") != _utc(start, "V38_STRATIFIED_PURGED_PLAN_TIME_INVALID")
                or _utc(frozen_item.get("end"), "V38_STRATIFIED_PURGED_POLICY_TIME_INVALID") != _utc(end, "V38_STRATIFIED_PURGED_PLAN_TIME_INVALID")):
            raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_PARTITION_POLICY_MISMATCH")

    data = plan.get("data")
    if (not isinstance(data, dict) or data.get("symbols") != list(SYMBOLS)
            or data.get("history_window_days") != 8
            or timedelta(days=data.get("history_window_days", -1)) != HISTORY_WINDOW
            or data.get("availability_delay_seconds") != 60
            or data.get("not_gate_data") is not True):
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_DATA_CONTRACT_INVALID")
    selection = plan.get("selection")
    if (not isinstance(selection, dict)
            or selection.get("algorithm") != "SEEDED_HASH_CHOICE_WITHIN_MONTHLY_TEMPORAL_STRATA_V1"
            or selection.get("runtime_randomness") is not False
            or selection.get("outcome_based_selection") is not False
            or selection.get("direction_or_volatility_based_selection") is not False
            or selection.get("a3_trigger_based_selection") is not False
            or selection.get("model_output_based_selection") is not False
            or selection.get("minimum_anchor_spacing_days") != 8
            or selection.get("input_window_start_inclusive") != "decision_time_minus_8_days"
            or selection.get("input_window_end_exclusive") != "decision_time"):
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_SELECTION_CONTRACT_INVALID")
    slots = selection.get("slots_per_month")
    if not isinstance(slots, list) or len(slots) != 3:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_SLOTS_INVALID")
    expected_slots = [
        {"slot_id": slot_id, "regular_month_day_candidates_inclusive": list(days),
         "first_partition_month_candidates_inclusive": list(first_days)}
        for (slot_id, days), (_, first_days) in zip(REGULAR_SLOTS, FIRST_MONTH_SLOTS, strict=True)
    ]
    if slots != expected_slots:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_SLOTS_INVALID")
    source = plan.get("source")
    if not isinstance(source, dict) or source.get("source_database_sha256") != "c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2" or source.get("source_manifest_sha256") != "2f348e6a190690e173f0237b5f5614102bebeb8e0573a1266014c04f5b25012a":
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_SOURCE_BINDING_INVALID")
    invariants = plan.get("invariants")
    if not isinstance(invariants, dict) or any(invariants.get(key) is not False for key in (
        "same_symbol_input_windows_overlap", "cross_partition_input_windows_overlap",
        "untouched_test_payloads_persisted", "untouched_test_labels_or_results_created",
        "historical_v25_samples_modified", "model_outputs_included", "orders_created",
    )):
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_INVARIANTS_INVALID")
    return plan, digest


def _month_starts(start: datetime, end: datetime) -> list[datetime]:
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = []
    while cursor < end:
        result.append(cursor)
        cursor = cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 12 else cursor.replace(month=cursor.month + 1)
    return result


def _anchor_strata(plan: dict[str, Any]) -> list[dict[str, Any]]:
    selection = plan["selection"]
    seed = selection["seed"]
    clock = time.fromisoformat(selection["decision_time_utc"])
    window = timedelta(days=plan["data"]["history_window_days"])
    strata: list[dict[str, Any]] = []
    for partition in plan["partitions"]:
        partition_id = partition["id"]
        partition_start = _utc(partition["start_utc_inclusive"], "V38_STRATIFIED_PURGED_TIME_INVALID")
        partition_end = _utc(partition["end_utc_exclusive"], "V38_STRATIFIED_PURGED_TIME_INVALID")
        months = _month_starts(partition_start, partition_end)
        for month in months:
            first_month = (month.year, month.month) == (partition_start.year, partition_start.month)
            slot_spec = FIRST_MONTH_SLOTS if first_month else REGULAR_SLOTS
            for slot_id, day_candidates in slot_spec:
                candidates = [day for day in day_candidates if day <= (month.replace(
                    year=month.year + 1, month=1
                ) - timedelta(days=1)).day]
                _require(bool(candidates), "V38_STRATIFIED_PURGED_SLOT_HAS_NO_CANDIDATE")
                key = f"{seed}|{partition_id}|{month:%Y-%m}|{slot_id}"
                digest = hashlib.sha256(key.encode("ascii")).hexdigest()
                selected_day = candidates[int(digest[:8], 16) % len(candidates)]
                decision = datetime.combine(month.date().replace(day=selected_day), clock, UTC)
                window_start = decision - window
                _require(partition_start <= window_start and decision < partition_end,
                         "V38_STRATIFIED_PURGED_WARMUP_CROSSES_PARTITION")
                _require(partition_for_time(decision, [
                    {"id": item["id"], "start": item["start_utc_inclusive"], "end": item["end_utc_exclusive"]}
                    for item in plan["partitions"]
                ]) == partition_id, "V38_STRATIFIED_PURGED_PARTITION_LABEL_MISMATCH")
                strata.append({
                    "partition": partition_id,
                    "month": month.strftime("%Y-%m"),
                    "slot_id": slot_id,
                    "stratum_id": f"{partition_id}-{month:%Y-%m}-{slot_id}",
                    "selection_key_sha256": digest,
                    "candidate_days": candidates,
                    "selected_day": selected_day,
                    "decision_time": _stamp(decision),
                    "input_window_start": _stamp(window_start),
                    "input_window_end_exclusive": _stamp(decision),
                })
    return strata


def select_stratified_purged_calendar_points() -> list[dict[str, Any]]:
    """Select one seeded anchor per month/slot, paired across BTC and ETH."""
    plan, plan_sha256 = load_stratified_purged_sample_plan()
    strata = _anchor_strata(plan)
    points: list[dict[str, Any]] = []
    for stratum in strata:
        partition = stratum["partition"]
        date = _utc(stratum["decision_time"], "V38_STRATIFIED_PURGED_TIME_INVALID").strftime("%Y%m%d")
        pair_id = f"v38sp-{partition}-{date}-{stratum['slot_id'].lower()}-1200z"
        for symbol in plan["data"]["symbols"]:
            points.append({
                "decision_id": f"{pair_id}-{symbol.lower()}",
                "decision_time": stratum["decision_time"],
                "symbol": symbol,
                "partition": partition,
                "selection_plan_sha256": plan_sha256,
                "selection_key_version": "sha256(seed|partition|YYYY-MM|slot_id)",
                "selection_key_sha256": stratum["selection_key_sha256"],
                "stratum_id": stratum["stratum_id"],
                "candidate_days": stratum["candidate_days"],
                "selected_day": stratum["selected_day"],
                "paired_anchor_group_id": pair_id,
                "input_window_start": stratum["input_window_start"],
                "input_window_end_exclusive": stratum["input_window_end_exclusive"],
                "overlaps_previous_input_window": False,
                "overlap_cluster_id": f"{symbol}-{partition}-purged-{date}",
                "overlap_seconds_with_previous": 0,
                "warmup_crosses_partition_start": False,
            })
    points.sort(key=lambda item: (
        list(EXPECTED_PARTITIONS).index(item["partition"]), item["decision_time"], item["symbol"],
    ))
    _validate_stratified_purged_selection(points, plan, plan_sha256)
    return points


def _validate_stratified_purged_selection(
    points: list[dict[str, Any]], plan: dict[str, Any], plan_sha256: str,
) -> None:
    _require(isinstance(points, list), "V38_STRATIFIED_PURGED_POINTS_INVALID")
    expected_strata = _anchor_strata(plan)
    expected_by_id: dict[tuple[str, str, str], dict[str, Any]] = {
        (item["partition"], item["decision_time"], symbol): item
        for item in expected_strata for symbol in plan["data"]["symbols"]
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    paired: dict[str, set[tuple[str, str]]] = defaultdict(set)
    observed_ids: set[tuple[str, str, str]] = set()
    for point in points:
        _require(isinstance(point, dict), "V38_STRATIFIED_PURGED_POINT_INVALID")
        symbol, partition = point.get("symbol"), point.get("partition")
        decision = _utc(point.get("decision_time"), "V38_STRATIFIED_PURGED_TIME_INVALID")
        key = (partition, _stamp(decision), symbol)
        _require(key in expected_by_id and key not in observed_ids,
                 "V38_STRATIFIED_PURGED_POINT_NOT_IN_FROZEN_SAMPLE")
        observed_ids.add(key)
        expected = expected_by_id[key]
        pair_id = f"v38sp-{partition}-{decision:%Y%m%d}-{expected['slot_id'].lower()}-1200z"
        _require(point.get("decision_id") == f"{pair_id}-{symbol.lower()}"
                 and point.get("paired_anchor_group_id") == pair_id,
                 "V38_STRATIFIED_PURGED_POINT_IDENTITY_INVALID")
        for field in (
            "selection_plan_sha256", "selection_key_version", "selection_key_sha256", "stratum_id",
            "candidate_days", "selected_day", "input_window_start", "input_window_end_exclusive",
        ):
            _require(point.get(field) == (plan_sha256 if field == "selection_plan_sha256" else
                     "sha256(seed|partition|YYYY-MM|slot_id)" if field == "selection_key_version" else
                     expected[field]), "V38_STRATIFIED_PURGED_STRATUM_BINDING_MISMATCH")
        _require(point.get("overlaps_previous_input_window") is False
                 and point.get("overlap_seconds_with_previous") == 0
                 and point.get("warmup_crosses_partition_start") is False,
                 "V38_STRATIFIED_PURGED_OVERLAP_METADATA_INVALID")
        _require(partition_for_time(decision, [
            {"id": item["id"], "start": item["start_utc_inclusive"], "end": item["end_utc_exclusive"]}
            for item in plan["partitions"]
        ]) == partition, "V38_STRATIFIED_PURGED_PARTITION_LABEL_MISMATCH")
        start = _utc(point["input_window_start"], "V38_STRATIFIED_PURGED_WINDOW_TIME_INVALID")
        end = _utc(point["input_window_end_exclusive"], "V38_STRATIFIED_PURGED_WINDOW_TIME_INVALID")
        bounds = next(item for item in plan["partitions"] if item["id"] == partition)
        _require(start == decision - HISTORY_WINDOW and end == decision
                 and start >= _utc(bounds["start_utc_inclusive"], "V38_STRATIFIED_PURGED_WINDOW_TIME_INVALID")
                 and end < _utc(bounds["end_utc_exclusive"], "V38_STRATIFIED_PURGED_WINDOW_TIME_INVALID"),
                 "V38_STRATIFIED_PURGED_WINDOW_OR_PARTITION_INVALID")
        grouped[(symbol, partition)].append(point)
        paired[point["paired_anchor_group_id"]].add((symbol, point["decision_time"]))

    _require(observed_ids == set(expected_by_id), "V38_STRATIFIED_PURGED_SAMPLE_COVERAGE_INVALID")
    _require(len(points) == 72 and len(paired) == 36,
             "V38_STRATIFIED_PURGED_SAMPLE_COUNT_INVALID")
    for pair in paired.values():
        _require({symbol for symbol, _ in pair} == set(plan["data"]["symbols"])
                 and len({stamp for _, stamp in pair}) == 1,
                 "V38_STRATIFIED_PURGED_PAIRING_INVALID")
    for (symbol, _partition), rows in grouped.items():
        rows.sort(key=lambda item: item["decision_time"])
        previous_end: datetime | None = None
        for point in rows:
            start = _utc(point["input_window_start"], "V38_STRATIFIED_PURGED_WINDOW_TIME_INVALID")
            if previous_end is not None:
                _require(start >= previous_end, "V38_STRATIFIED_PURGED_WINDOWS_OVERLAP")
            previous_end = _utc(point["input_window_end_exclusive"], "V38_STRATIFIED_PURGED_WINDOW_TIME_INVALID")
        expected = EXPECTED_PARTITIONS[_partition][2]
        _require(len(rows) == expected * len(plan["selection"]["slots_per_month"]) // 3,
                 "V38_STRATIFIED_PURGED_SYMBOL_PARTITION_COUNT_INVALID")


def _write_json_exclusive(path: Path, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    try:
        with path.open("xb") as stream:
            stream.write(encoded)
    except FileExistsError as exc:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_OUTPUT_EXISTS_REFUSE_OVERWRITE") from exc
    return hashlib.sha256(encoded).hexdigest()


def _verified_archive(archive_directory: Path, plan: dict[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        source_manifest, _database_path, source_manifest_sha256, _archives = _manifest(archive_directory)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_ARCHIVE_INVALID") from exc
    source = plan["source"]
    if (source_manifest_sha256 != source["source_manifest_sha256"]
            or source_manifest.get("dataset_sha256") != source["source_database_sha256"]
            or source_manifest.get("symbols") != plan["data"]["symbols"]
            or source_manifest.get("complete_data") is not True
            or source_manifest.get("exchange") != "BINANCE_UM"
            or source_manifest.get("source_type") != "OFFICIAL_MONTHLY_PUBLIC_ARCHIVES"):
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_ARCHIVE_BINDING_MISMATCH")
    coverage = source_manifest.get("coverage", {})
    if not all(
        coverage.get(symbol, {}).get("bars", {}).get("complete") is True
        and coverage.get(symbol, {}).get("bars", {}).get("gap_count") == 0
        for symbol in plan["data"]["symbols"]
    ):
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_ARCHIVE_COVERAGE_INVALID")
    records = source_manifest.get("archived_files")
    if not isinstance(records, list) or not records:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_ARCHIVE_FILES_MISSING")
    for record in records:
        _verify_raw_archive(archive_directory, record)
    return source_manifest, source_manifest_sha256


def build_stratified_purged_v38_dataset(
    archive_directory: Path, output_directory: Path,
) -> dict[str, Any]:
    """Build a new causal sample under ignored reports; never downloads or overwrites."""
    plan, plan_sha256 = load_stratified_purged_sample_plan()
    archive_directory = Path(archive_directory).resolve()
    output_directory = Path(output_directory).resolve()
    allowed_root = (ROOT / "reports" / "v38+").resolve()
    try:
        output_directory.relative_to(allowed_root)
    except ValueError as exc:
        raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_OUTPUT_MUST_BE_UNDER_IGNORED_REPORTS") from exc
    if output_directory.exists():
        if any(output_directory.iterdir()):
            raise StratifiedPurgedDatasetError("V38_STRATIFIED_PURGED_OUTPUT_DIRECTORY_NOT_EMPTY")
    else:
        output_directory.mkdir(parents=True)

    source_manifest, source_manifest_sha256 = _verified_archive(archive_directory, plan)
    selections = select_stratified_purged_calendar_points()
    inputs: list[dict[str, Any]] = []
    sealed: list[dict[str, Any]] = []
    descriptors: list[dict[str, Any]] = []
    used_archive_hashes: set[str] = set()
    for selection in selections:
        reconstructed = build_reconstructed_point(
            archive_directory,
            symbol=selection["symbol"],
            decision_time=selection["decision_time"],
            decision_id=selection["decision_id"],
            partition=selection["partition"],
            availability_delay_seconds=plan["data"]["availability_delay_seconds"],
        )
        projected = {
            "decision_id": reconstructed["decision_id"],
            "decision_time": reconstructed["decision_time"],
            "symbol": reconstructed["symbol"],
            "partition": reconstructed["partition"],
            "bars_by_timeframe": reconstructed["bars_by_timeframe"],
        }
        market_input = build_market_only_input(projected)
        provenance = market_input["provenance"]
        used_archive_hashes.update(provenance["archive_file_sha256s"])
        descriptor = {
            **selection,
            "market_input_sha256": market_input["market_input_sha256"],
            "bar_count_by_timeframe": {
                timeframe: len(projected["bars_by_timeframe"][timeframe])
                for timeframe in plan["data"]["timeframes"]
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
                    "selection_key_version", "selection_key_sha256", "stratum_id", "candidate_days",
                    "selected_day", "paired_anchor_group_id", "input_window_start",
                    "input_window_end_exclusive", "overlaps_previous_input_window", "overlap_cluster_id",
                    "overlap_seconds_with_previous", "warmup_crosses_partition_start", "market_input_sha256",
                    "bar_count_by_timeframe", "evidence_ref_count", "price_evidence_grade",
                    "availability_evidence_grade", "archive_file_sha256s",
                )
            })
        else:
            inputs.append(projected)

    _require(len(descriptors) == 72 and len(inputs) == 54 and len(sealed) == 18,
             "V38_STRATIFIED_PURGED_RECONSTRUCTED_COUNT_INVALID")
    verified_archive_hashes = {
        record["sha256"] for record in source_manifest["archived_files"] if isinstance(record, dict)
    }
    _require(used_archive_hashes <= verified_archive_hashes,
             "V38_STRATIFIED_PURGED_USED_ARCHIVE_NOT_VERIFIED")
    _require(all(
        item["price_evidence_grade"] == [plan["data"]["price_evidence_grade"]]
        and item["availability_evidence_grade"] == [plan["data"]["availability_evidence_grade"]]
        for item in descriptors
    ), "V38_STRATIFIED_PURGED_EVIDENCE_GRADE_MISMATCH")

    input_document = {
        "schema_version": "pa-market-only-v38/stratified-purged-market-input-dataset-1",
        "dataset_kind": "MARKET_ONLY_CAUSAL_STRATIFIED_PURGED_PAIRED_CONTEXTS_NO_LABELS",
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
        "schema_version": "pa-market-only-v38/stratified-purged-untouched-test-sealed-1",
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
    partition_counts = Counter(item["partition"] for item in descriptors)
    contexts_by_symbol_partition = Counter((item["symbol"], item["partition"]) for item in descriptors)
    anchor_counts = {item["id"]: item["expected_anchor_count"] for item in plan["partitions"]}
    manifest: dict[str, Any] = {
        "schema_version": "pa-market-only-v38/stratified-purged-dataset-manifest-1",
        "dataset_id": plan["plan_id"],
        "plan_sha256": plan_sha256,
        "parent_plan_sha256s": [item["plan_sha256"] for item in plan["parent_plans"]],
        "parent_dataset_manifest_sha256s": [
            "ca0786a4acdec7a7410b7df89e947bc2b698377fed69a13fd865fe4d1187bd99",
            "1d76f1cd0e2eaa658eb8d32decc434a10e151cb7119a6b2a4f85801639537785",
        ],
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
        "partition_counts": dict(sorted(partition_counts.items())),
        "temporal_anchor_counts": anchor_counts,
        "contexts_by_symbol_partition": {
            f"{symbol}:{partition}": count
            for (symbol, partition), count in sorted(contexts_by_symbol_partition.items())
        },
        "total_market_contexts": len(descriptors),
        "optimization_validation_input_count": len(inputs),
        "untouched_test_hash_only_count": len(sealed),
        "paired_temporal_anchor_count": sum(anchor_counts.values()),
        "same_symbol_input_window_overlap_count": 0,
        "cross_partition_input_window_overlap_count": 0,
        "iid_or_independent_trade_claim": False,
        "price_evidence_grade": plan["data"]["price_evidence_grade"],
        "availability_evidence_grade": plan["data"]["availability_evidence_grade"],
        "availability_delay_seconds": plan["data"]["availability_delay_seconds"],
        "gate_executable_trade_samples": 0,
        "completed_closes": 0,
        "model_outputs": 0,
        "gemini_research_calls_used": 0,
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