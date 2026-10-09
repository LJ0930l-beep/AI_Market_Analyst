"""Standalone structural and causal cross-check for the frozen V38 primary sample.

This verifier intentionally imports no project code. It validates the frozen plan,
dataset manifests, raw visible OHLCV rows, deterministic strata, partition labels,
pairing, and declared input windows with Python's standard library only. It does
not reconstruct the derived V36 feature frames or rehash the original Binance files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = ROOT / "configs/research/datasets/v38-stratified-purged-sample-plan-v1.json"
DEFAULT_DATASET = ROOT / "reports/v38+/dataset-stratified-purged-20261009-v1"
PLAN_SHA256 = "094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f"
DATASET_MANIFEST_SHA256 = "1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a"
SOURCE_DATABASE_SHA256 = "c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2"
SOURCE_MANIFEST_SHA256 = "2f348e6a190690e173f0237b5f5614102bebeb8e0573a1266014c04f5b25012a"
PARTITIONS = {
    "optimization": ("2025-10-01T00:00:00Z", "2026-04-01T00:00:00Z", 18),
    "validation": ("2026-04-01T00:00:00Z", "2026-07-01T00:00:00Z", 9),
    "untouched_test": ("2026-07-01T00:00:00Z", "2026-10-01T00:00:00Z", 9),
}
SYMBOLS = ("BTCUSDT", "ETHUSDT")
TIMEFRAME_SECONDS = {"15m": 900, "5m": 300, "1h": 3600, "4h": 14400}
EXPECTED_BAR_COUNTS = {"15m": 48, "5m": 48, "1h": 48, "4h": 47}
HASH_CHARS = frozenset("0123456789abcdef")

MANIFEST_FIELDS = frozenset({
    "availability_delay_seconds", "availability_evidence_grade", "base_commit",
    "completed_closes", "contexts_by_symbol_partition", "cross_partition_input_window_overlap_count",
    "dataset_id", "files", "gate_executable_trade_samples", "gemini_research_calls_used",
    "iid_or_independent_trade_claim", "manifest_sha256", "model_outputs",
    "optimization_validation_input_count", "orders_created", "outcome_labels_included",
    "paired_temporal_anchor_count", "parent_dataset_manifest_sha256s", "parent_plan_sha256s",
    "partition_counts", "plan_sha256", "points", "price_evidence_grade",
    "same_symbol_input_window_overlap_count", "schema_version", "source", "temporal_anchor_counts",
    "total_market_contexts", "untouched_test_hash_only_count", "untouched_test_payloads_included",
})
DESCRIPTOR_FIELDS = frozenset({
    "archive_file_sha256s", "availability_evidence_grade", "bar_count_by_timeframe",
    "candidate_days", "decision_id", "decision_time", "evidence_ref_count",
    "input_window_end_exclusive", "input_window_start", "market_input_sha256",
    "overlap_cluster_id", "overlap_seconds_with_previous", "overlaps_previous_input_window",
    "paired_anchor_group_id", "partition", "price_evidence_grade", "selected_day",
    "selection_key_sha256", "selection_key_version", "selection_plan_sha256", "stratum_id",
    "symbol", "warmup_crosses_partition_start",
})
INPUT_DOCUMENT_FIELDS = frozenset({
    "dataset_kind", "decision_points", "model_outputs_included", "outcome_labels_included",
    "partition_policy_sha256", "partition_scope", "plan_sha256", "schema_version",
    "source_database_sha256", "source_manifest_sha256",
})
VISIBLE_POINT_FIELDS = frozenset({"bars_by_timeframe", "decision_id", "decision_time", "partition", "symbol"})
BAR_FIELDS = frozenset({
    "available_at", "available_at_basis", "available_at_evidence_grade", "bar_end", "bar_start",
    "close", "high", "is_closed", "low", "open", "price_evidence_grade", "quality_status",
    "source", "source_database_sha256", "source_exchange", "source_file_hash", "source_file_hashes",
    "source_manifest_sha256", "symbol", "timeframe", "volume", "volume_unit",
})
SEALED_DOCUMENT_FIELDS = frozenset({
    "decision_points", "labels_or_evaluation_included", "market_input_payloads_included", "partition",
    "partition_policy_sha256", "persist_only_hashes_and_coverage", "plan_sha256", "schema_version",
    "source_database_sha256",
})


class IndependentAuditError(ValueError):
    """Stable failure code emitted by the standalone verifier."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise IndependentAuditError(code)


def _json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IndependentAuditError(code) from exc


def _canonical_sha(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IndependentAuditError("CANONICAL_JSON_INVALID") from exc
    return hashlib.sha256(payload).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise IndependentAuditError("FROZEN_FILE_UNAVAILABLE") from exc
    return digest.hexdigest()


def _is_sha(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and set(value.lower()) <= HASH_CHARS)


def _utc(value: Any, code: str = "TIME_INVALID") -> datetime:
    if not isinstance(value, str):
        raise IndependentAuditError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise IndependentAuditError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IndependentAuditError(code)
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def partition_for(point: datetime) -> str:
    """Resolve the frozen research-plan partition with half-open boundaries."""
    for name, (start_raw, end_raw, _) in PARTITIONS.items():
        if _utc(start_raw) <= point < _utc(end_raw):
            return name
    raise IndependentAuditError("DECISION_TIME_OUTSIDE_FROZEN_PARTITIONS")


def expected_anchor_schedule(plan: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Recreate the one-day-per-month-slot selection without importing project code."""
    _require(plan.get("plan_sha256") is None, "PLAN_MUST_NOT_CONTAIN_SELF_HASH")
    selection = plan.get("selection")
    partitions = plan.get("partitions")
    data = plan.get("data")
    _require(isinstance(selection, dict) and isinstance(partitions, list) and isinstance(data, dict),
             "PLAN_SELECTION_SHAPE_INVALID")
    _require(selection.get("algorithm") == "SEEDED_HASH_CHOICE_WITHIN_MONTHLY_TEMPORAL_STRATA_V1",
             "PLAN_SELECTION_ALGORITHM_INVALID")
    _require(selection.get("same_anchors_for_all_symbols") is True, "PLAN_PAIRING_POLICY_INVALID")
    slots = selection.get("slots_per_month")
    selection_version = selection.get("selection_key_version")
    descriptor_key_version = (selection_version.split(";", 1)[0]
                              if isinstance(selection_version, str) else None)
    _require(descriptor_key_version == "sha256(seed|partition|YYYY-MM|slot_id)",
             "PLAN_SELECTION_VERSION_INVALID")
    _require(isinstance(slots, list) and len(slots) == 3, "PLAN_SLOTS_INVALID")
    slot_by_id = {slot.get("slot_id"): slot for slot in slots if isinstance(slot, dict)}
    _require(set(slot_by_id) == {"S1", "S2", "S3"}, "PLAN_SLOTS_INVALID")
    _require(data.get("history_window_days") == 8, "PLAN_WINDOW_LENGTH_INVALID")
    _require(selection.get("input_window_start_inclusive") == "decision_time_minus_8_days"
             and selection.get("input_window_end_exclusive") == "decision_time", "PLAN_WINDOW_POLICY_INVALID")
    clock = time.fromisoformat(str(selection.get("decision_time_utc")))
    seed = selection.get("seed")
    _require(isinstance(seed, str) and seed, "PLAN_SEED_INVALID")

    by_id = {item.get("id"): item for item in partitions if isinstance(item, dict)}
    _require(set(by_id) == set(PARTITIONS), "PLAN_PARTITIONS_INVALID")
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for name, (start_expected, end_expected, anchors_expected) in PARTITIONS.items():
        part = by_id[name]
        _require(part.get("start_utc_inclusive") == start_expected
                 and part.get("end_utc_exclusive") == end_expected
                 and part.get("expected_anchor_count") == anchors_expected
                 and part.get("expected_context_count") == anchors_expected * len(SYMBOLS),
                 "PLAN_PARTITION_BOUNDARY_OR_COUNT_MISMATCH")
        start, end = _utc(start_expected), _utc(end_expected)
        month = date(start.year, start.month, 1)
        while datetime(month.year, month.month, 1, tzinfo=UTC) < end:
            first_month = month.year == start.year and month.month == start.month
            next_month = date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)
            month_end_day = (datetime(next_month.year, next_month.month, 1, tzinfo=UTC) - timedelta(days=1)).day
            for slot_id in ("S1", "S2", "S3"):
                slot = slot_by_id[slot_id]
                candidates_field = ("first_partition_month_candidates_inclusive" if first_month
                                    else "regular_month_day_candidates_inclusive")
                candidates = [day for day in slot.get(candidates_field, []) if isinstance(day, int) and day <= month_end_day]
                _require(bool(candidates), "PLAN_STRATUM_HAS_NO_CANDIDATES")
                key = f"{seed}|{name}|{month:%Y-%m}|{slot_id}"
                key_sha = hashlib.sha256(key.encode("ascii")).hexdigest()
                selected_day = candidates[int(key_sha[:8], 16) % len(candidates)]
                decision = datetime(month.year, month.month, selected_day,
                                    clock.hour, clock.minute, clock.second, tzinfo=UTC)
                _require(start <= decision < end and decision - timedelta(days=8) >= start,
                         "PLAN_ANCHOR_OR_WARMUP_OUTSIDE_PARTITION")
                record = {
                    "partition": name,
                    "decision_time": _stamp(decision),
                    "stratum_id": f"{name}-{month:%Y-%m}-{slot_id}",
                    "slot_id": slot_id,
                    "candidate_days": candidates,
                    "selected_day": selected_day,
                    "selection_key_sha256": key_sha,
                }
                key_pair = (name, record["decision_time"])
                _require(key_pair not in expected, "PLAN_DUPLICATE_ANCHOR")
                expected[key_pair] = record
            month = next_month
    _require(len(expected) == 36, "PLAN_ANCHOR_TOTAL_INVALID")
    return expected


def validate_bar_series(
    rows: Any, *, timeframe: str, symbol: str, decision_time: datetime, plan: dict[str, Any],
) -> set[str]:
    """Check raw bar chronology, geometry, source bindings, and strict availability."""
    _require(timeframe in TIMEFRAME_SECONDS and isinstance(rows, list) and rows,
             "VISIBLE_BAR_SERIES_INVALID")
    expected_source = plan.get("source", {})
    data = plan.get("data", {})
    duration = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    used_hashes: set[str] = set()
    previous_end: datetime | None = None
    for row in rows:
        _require(isinstance(row, dict) and set(row) == BAR_FIELDS, "VISIBLE_BAR_FIELDS_INVALID")
        _require(row.get("symbol") == symbol and row.get("timeframe") == timeframe,
                 "VISIBLE_BAR_IDENTITY_MISMATCH")
        _require(row.get("is_closed") is True, "VISIBLE_BAR_NOT_CLOSED")
        start, end, available = (_utc(row.get("bar_start")), _utc(row.get("bar_end")),
                                 _utc(row.get("available_at")))
        _require(end - start == duration, "VISIBLE_BAR_DURATION_INVALID")
        _require(start >= decision_time - timedelta(days=8) and end < decision_time
                 and available < decision_time, "VISIBLE_BAR_NOT_CAUSAL_OR_OUTSIDE_WINDOW")
        _require(available == end + timedelta(seconds=int(data.get("availability_delay_seconds", -1))),
                 "VISIBLE_BAR_AVAILABILITY_PROXY_MISMATCH")
        if previous_end is not None:
            _require(end > previous_end and start == previous_end, "VISIBLE_BAR_GAP_DUPLICATE_OR_ORDER_INVALID")
        previous_end = end
        _require(row.get("source_exchange") == "BINANCE_UM"
                 and row.get("source") == "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVE_RECONSTRUCTED",
                 "VISIBLE_BAR_SOURCE_INVALID")
        _require(row.get("source_database_sha256") == expected_source.get("source_database_sha256")
                 and row.get("source_manifest_sha256") == expected_source.get("source_manifest_sha256"),
                 "VISIBLE_BAR_SOURCE_BINDING_MISMATCH")
        _require(row.get("price_evidence_grade") == data.get("price_evidence_grade")
                 and row.get("available_at_evidence_grade") == data.get("availability_evidence_grade")
                 and row.get("available_at_basis") == "ASSUMED_PROXY_60S_AFTER_BAR_END_NOT_OBSERVED"
                 and row.get("volume_unit") == data.get("volume_unit"),
                 "VISIBLE_BAR_EVIDENCE_GRADE_MISMATCH")
        values: dict[str, float] = {}
        for field in ("open", "high", "low", "close", "volume"):
            raw = row.get(field)
            _require(not isinstance(raw, bool) and isinstance(raw, (int, float)) and math.isfinite(raw),
                     "VISIBLE_BAR_OHLCV_INVALID")
            values[field] = float(raw)
        _require(min(values["open"], values["high"], values["low"], values["close"]) > 0
                 and values["volume"] >= 0
                 and values["high"] >= max(values["open"], values["close"], values["low"])
                 and values["low"] <= min(values["open"], values["close"]),
                 "VISIBLE_BAR_GEOMETRY_INVALID")
        hashes = row.get("source_file_hashes")
        one_hash = row.get("source_file_hash")
        _require(isinstance(hashes, list) and bool(hashes) and all(_is_sha(item) for item in hashes)
                 and _is_sha(one_hash) and one_hash in hashes, "VISIBLE_BAR_ARCHIVE_HASH_FORMAT_INVALID")
        used_hashes.update(hashes)
    return used_hashes


def _check_hash_only_document(document: Any, *, plan: dict[str, Any]) -> list[dict[str, Any]]:
    _require(isinstance(document, dict) and set(document) == SEALED_DOCUMENT_FIELDS,
             "SEALED_DOCUMENT_FIELDS_INVALID")
    _require(document.get("schema_version") == "pa-market-only-v38/stratified-purged-untouched-test-sealed-1"
             and document.get("partition") == "untouched_test"
             and document.get("plan_sha256") == PLAN_SHA256
             and document.get("partition_policy_sha256") == plan.get("partition_policy_sha256")
             and document.get("source_database_sha256") == SOURCE_DATABASE_SHA256,
             "SEALED_DOCUMENT_BINDING_INVALID")
    _require(document.get("persist_only_hashes_and_coverage") is True
             and document.get("market_input_payloads_included") is False
             and document.get("labels_or_evaluation_included") is False,
             "SEALED_TEST_CONTENT_POLICY_INVALID")
    points = document.get("decision_points")
    _require(isinstance(points, list) and len(points) == 18, "SEALED_TEST_COUNT_INVALID")
    for row in points:
        _require(isinstance(row, dict) and set(row) == DESCRIPTOR_FIELDS,
                 "SEALED_TEST_ROW_FIELDS_INVALID")
        _require(row.get("partition") == "untouched_test" and _is_sha(row.get("market_input_sha256")),
                 "SEALED_TEST_ROW_BINDING_INVALID")
    return points


def _check_sealed_rows_match_descriptors(
    sealed_rows: list[dict[str, Any]],
    descriptors_by_key: dict[tuple[str, str, str], dict[str, Any]],
) -> None:
    """Bind each hash-only test row to the exact frozen manifest descriptor."""
    expected_keys = {key for key in descriptors_by_key if key[0] == "untouched_test"}
    _require(len(sealed_rows) == len(expected_keys), "SEALED_TEST_DESCRIPTOR_COUNT_INVALID")
    seen: set[tuple[str, str, str]] = set()
    for row in sealed_rows:
        key = (row.get("partition"), _stamp(_utc(row.get("decision_time"))), row.get("symbol"))
        _require(key not in seen, "SEALED_TEST_DESCRIPTOR_DUPLICATE")
        seen.add(key)
        _require(row == descriptors_by_key.get(key), "SEALED_TEST_DESCRIPTOR_MISMATCH")
    _require(seen == expected_keys, "SEALED_TEST_DESCRIPTOR_COVERAGE_MISMATCH")


def audit_primary_dataset(plan_path: Path, dataset_directory: Path) -> dict[str, Any]:
    plan = _json(plan_path, "FROZEN_PLAN_UNAVAILABLE")
    _require(isinstance(plan, dict) and plan.get("plan_id") == "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1",
             "FROZEN_PLAN_ID_INVALID")
    plan_sha = _canonical_sha(plan)
    _require(plan_sha == PLAN_SHA256, "FROZEN_PLAN_HASH_MISMATCH")
    _require(plan.get("source", {}).get("source_database_sha256") == SOURCE_DATABASE_SHA256
             and plan.get("source", {}).get("source_manifest_sha256") == SOURCE_MANIFEST_SHA256,
             "FROZEN_PLAN_SOURCE_BINDING_INVALID")
    schedule = expected_anchor_schedule(plan)
    selection_version = plan["selection"]["selection_key_version"]
    descriptor_key_version = selection_version.split(";", 1)[0]

    manifest_path = dataset_directory / "dataset-manifest.json"
    input_path = dataset_directory / "optimization-validation-inputs.json"
    sealed_path = dataset_directory / "untouched-test-sealed-manifest.json"
    manifest = _json(manifest_path, "DATASET_MANIFEST_UNAVAILABLE")
    _require(isinstance(manifest, dict) and set(manifest) == MANIFEST_FIELDS,
             "DATASET_MANIFEST_FIELDS_INVALID")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    _require(_canonical_sha(body) == manifest.get("manifest_sha256") == DATASET_MANIFEST_SHA256,
             "DATASET_MANIFEST_HASH_MISMATCH")
    _require(manifest.get("plan_sha256") == plan_sha
             and manifest.get("dataset_id") == plan.get("plan_id")
             and manifest.get("schema_version") == "pa-market-only-v38/stratified-purged-dataset-manifest-1",
             "DATASET_MANIFEST_PLAN_BINDING_INVALID")
    files = manifest.get("files")
    expected_file_names = {input_path.name, sealed_path.name}
    _require(isinstance(files, dict) and set(files) == expected_file_names,
             "DATASET_OUTPUT_FILE_LIST_INVALID")
    input_file_sha, sealed_file_sha = _file_sha(input_path), _file_sha(sealed_path)
    _require(files.get(input_path.name) == input_file_sha and files.get(sealed_path.name) == sealed_file_sha,
             "DATASET_OUTPUT_FILE_HASH_MISMATCH")

    inputs = _json(input_path, "VISIBLE_INPUTS_UNAVAILABLE")
    _require(isinstance(inputs, dict) and set(inputs) == INPUT_DOCUMENT_FIELDS,
             "VISIBLE_INPUT_DOCUMENT_FIELDS_INVALID")
    _require(inputs.get("schema_version") == "pa-market-only-v38/stratified-purged-market-input-dataset-1"
             and inputs.get("dataset_kind") == "MARKET_ONLY_CAUSAL_STRATIFIED_PURGED_PAIRED_CONTEXTS_NO_LABELS"
             and inputs.get("plan_sha256") == plan_sha
             and inputs.get("partition_policy_sha256") == plan.get("partition_policy_sha256")
             and inputs.get("source_database_sha256") == SOURCE_DATABASE_SHA256
             and inputs.get("source_manifest_sha256") == SOURCE_MANIFEST_SHA256
             and inputs.get("partition_scope") == ["optimization", "validation"]
             and inputs.get("outcome_labels_included") is False
             and inputs.get("model_outputs_included") is False,
             "VISIBLE_INPUT_DOCUMENT_BINDING_INVALID")
    visible = inputs.get("decision_points")
    _require(isinstance(visible, list) and len(visible) == 54, "VISIBLE_INPUT_COUNT_INVALID")
    sealed = _check_hash_only_document(_json(sealed_path, "SEALED_MANIFEST_UNAVAILABLE"), plan=plan)

    descriptors = manifest.get("points")
    _require(isinstance(descriptors, list) and len(descriptors) == 72,
             "DATASET_DESCRIPTOR_COUNT_INVALID")
    descriptors_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    descriptor_ids: set[str] = set()
    anchors_seen: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    windows_by_symbol: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    partition_counts: Counter[str] = Counter()
    anchors_by_partition: dict[str, set[str]] = defaultdict(set)
    for row in descriptors:
        _require(isinstance(row, dict) and set(row) == DESCRIPTOR_FIELDS,
                 "DATASET_DESCRIPTOR_FIELDS_INVALID")
        decision = _utc(row.get("decision_time"))
        when = _stamp(decision)
        partition, symbol = row.get("partition"), row.get("symbol")
        _require(partition_for(decision) == partition and symbol in SYMBOLS,
                 "DATASET_DESCRIPTOR_PARTITION_OR_SYMBOL_INVALID")
        anchor = schedule.get((partition, when))
        _require(anchor is not None, "DATASET_ANCHOR_NOT_IN_FROZEN_SCHEDULE")
        _require(row.get("selection_plan_sha256") == plan_sha
                 and row.get("selection_key_sha256") == anchor["selection_key_sha256"]
                 and row.get("selected_day") == anchor["selected_day"]
                 and row.get("candidate_days") == anchor["candidate_days"]
                 and row.get("stratum_id") == anchor["stratum_id"]
                 and row.get("selection_key_version") == descriptor_key_version,
                 "DATASET_STRATUM_SELECTION_MISMATCH")
        start, end = _utc(row.get("input_window_start")), _utc(row.get("input_window_end_exclusive"))
        _require(end == decision and start == decision - timedelta(days=8)
                 and _utc(PARTITIONS[partition][0]) <= start and end <= _utc(PARTITIONS[partition][1])
                 and row.get("warmup_crosses_partition_start") is False,
                 "DATASET_INPUT_WINDOW_INVALID")
        _require(row.get("overlaps_previous_input_window") is False
                 and isinstance(row.get("overlap_cluster_id"), str) and row["overlap_cluster_id"]
                 and row.get("overlap_seconds_with_previous") == 0,
                 "DATASET_OVERLAP_METADATA_INVALID")
        _require(isinstance(row.get("decision_id"), str) and row["decision_id"] not in descriptor_ids,
                 "DATASET_DECISION_ID_DUPLICATE")
        _require(_is_sha(row.get("market_input_sha256"))
                 and _is_sha_list(row.get("archive_file_sha256s")), "DATASET_DESCRIPTOR_HASH_INVALID")
        expected_counts = {"15m": 48, "5m": 48, "1h": 48, "4h": 47}
        _require(row.get("bar_count_by_timeframe") == expected_counts,
                 "DATASET_DESCRIPTOR_BAR_COUNTS_INVALID")
        _require(isinstance(row.get("evidence_ref_count"), int) and row["evidence_ref_count"] > 0,
                 "DATASET_DESCRIPTOR_EVIDENCE_COUNT_INVALID")
        _require(row.get("price_evidence_grade") == ["VERIFIED_ARCHIVE_RECONSTRUCTION"]
                 and row.get("availability_evidence_grade") == ["ASSUMED_PROXY"],
                 "DATASET_DESCRIPTOR_EVIDENCE_GRADE_INVALID")
        key = (partition, when, symbol)
        _require(key not in descriptors_by_key, "DATASET_DESCRIPTOR_DUPLICATE")
        descriptors_by_key[key] = row
        descriptor_ids.add(row["decision_id"])
        anchors_seen[(partition, when)].append(row)
        windows_by_symbol[symbol].append((start, end))
        partition_counts[partition] += 1
        anchors_by_partition[partition].add(when)

    expected_keys = {
        (part, when, symbol)
        for part, when in schedule
        for symbol in SYMBOLS
    }
    _require(set(descriptors_by_key) == expected_keys, "DATASET_SCHEDULE_COVERAGE_MISMATCH")
    _check_sealed_rows_match_descriptors(sealed, descriptors_by_key)
    _require(partition_counts == Counter({"optimization": 36, "validation": 18, "untouched_test": 18}),
             "DATASET_PARTITION_CONTEXT_COUNTS_INVALID")
    _require({name: len(anchors_by_partition[name]) for name in PARTITIONS}
             == {name: count for name, (_, _, count) in PARTITIONS.items()},
             "DATASET_PARTITION_ANCHOR_COUNTS_INVALID")
    for members in anchors_seen.values():
        group_ids = {row["paired_anchor_group_id"] for row in members}
        group_id = next(iter(group_ids)) if len(group_ids) == 1 else None
        _require(len(members) == 2 and {row["symbol"] for row in members} == set(SYMBOLS)
                 and isinstance(group_id, str) and bool(group_id), "DATASET_ASSET_PAIRING_INVALID")
    for symbol, windows in windows_by_symbol.items():
        windows.sort(key=lambda entry: entry[0])
        for previous, current in pairwise(windows):
            _require(previous[1] <= current[0], "DATASET_SAME_SYMBOL_WINDOWS_OVERLAP")

    visible_ids: set[str] = set()
    visible_counts: Counter[str] = Counter()
    visible_bars_by_timeframe: Counter[str] = Counter()
    used_archive_hashes: dict[str, set[str]] = defaultdict(set)
    for point in visible:
        _require(isinstance(point, dict) and set(point) == VISIBLE_POINT_FIELDS,
                 "VISIBLE_POINT_FIELDS_INVALID")
        decision = _utc(point.get("decision_time"))
        when = _stamp(decision)
        partition, symbol, decision_id = point.get("partition"), point.get("symbol"), point.get("decision_id")
        _require(partition in {"optimization", "validation"} and partition_for(decision) == partition
                 and symbol in SYMBOLS, "VISIBLE_POINT_PARTITION_OR_SYMBOL_INVALID")
        descriptor = descriptors_by_key.get((partition, when, symbol))
        _require(descriptor is not None and descriptor.get("decision_id") == decision_id,
                 "VISIBLE_POINT_DESCRIPTOR_BINDING_INVALID")
        _require(decision_id not in visible_ids, "VISIBLE_POINT_ID_DUPLICATE")
        visible_ids.add(decision_id)
        visible_counts[partition] += 1
        bar_groups = point.get("bars_by_timeframe")
        _require(isinstance(bar_groups, dict) and set(bar_groups) == set(TIMEFRAME_SECONDS),
                 "VISIBLE_POINT_TIMEFRAMES_INVALID")
        all_hashes: set[str] = set()
        for timeframe, rows in bar_groups.items():
            _require(len(rows) == EXPECTED_BAR_COUNTS[timeframe], "VISIBLE_BAR_COUNT_INVALID")
            visible_bars_by_timeframe[timeframe] += len(rows)
            all_hashes.update(validate_bar_series(
                rows, timeframe=timeframe, symbol=symbol, decision_time=decision, plan=plan,
            ))
        _require(all_hashes == set(descriptor["archive_file_sha256s"]),
                 "VISIBLE_POINT_ARCHIVE_HASH_BINDING_INVALID")
        four_hour_rows = bar_groups["4h"]
        _require(_utc(four_hour_rows[0]["bar_start"]) == decision - timedelta(days=8),
                 "VISIBLE_POINT_EIGHT_DAY_WARMUP_INVALID")
        used_archive_hashes[symbol].update(all_hashes)
    _require(visible_counts == Counter({"optimization": 36, "validation": 18}),
             "VISIBLE_PARTITION_COUNTS_INVALID")
    _require(visible_ids == {row["decision_id"] for row in descriptors
                             if row["partition"] in {"optimization", "validation"}},
             "VISIBLE_DESCRIPTOR_COVERAGE_INVALID")
    _require(not any(row["partition"] == "untouched_test" for row in visible),
             "SEALED_TEST_PAYLOAD_EXPOSED")
    _require(manifest.get("total_market_contexts") == 72
             and manifest.get("optimization_validation_input_count") == 54
             and manifest.get("untouched_test_hash_only_count") == 18
             and manifest.get("same_symbol_input_window_overlap_count") == 0
             and manifest.get("cross_partition_input_window_overlap_count") == 0
             and manifest.get("paired_temporal_anchor_count") == 36
             and manifest.get("untouched_test_payloads_included") is False
             and manifest.get("outcome_labels_included") is False
             and manifest.get("iid_or_independent_trade_claim") is False,
             "DATASET_DECLARED_INVARIANTS_INVALID")
    _require(manifest.get("source", {}).get("archive_files_verified") == 50
             and manifest.get("source", {}).get("network_calls") == 0,
             "DATASET_SOURCE_DECLARATION_INVALID")

    body_report = {
        "schema_version": "pa-market-only-v38/independent-primary-gate2-crosscheck-1",
        "status": "PASS_WITH_EVIDENCE_LIMITS",
        "audit_method": "STANDALONE_PYTHON_STDLIB_NO_PROJECT_IMPORTS",
        "auditor_code_sha256": _file_sha(Path(__file__)),
        "dataset_id": manifest["dataset_id"],
        "plan_sha256": plan_sha,
        "manifest_sha256": manifest["manifest_sha256"],
        "frozen_files": {
            "plan": _file_sha(plan_path),
            "dataset_manifest": _file_sha(manifest_path),
            "visible_inputs": input_file_sha,
            "sealed_manifest": sealed_file_sha,
        },
        "source": {
            "source_database_sha256_claim": SOURCE_DATABASE_SHA256,
            "source_manifest_sha256_claim": SOURCE_MANIFEST_SHA256,
            "raw_archive_files_rehashed": False,
            "model_input_feature_hashes_recomputed": False,
            "network_calls": 0,
        },
        "counts": {
            "total_contexts": len(descriptors),
            "visible_contexts_rechecked": len(visible),
            "visible_by_partition": dict(sorted(visible_counts.items())),
            "visible_bar_rows_by_timeframe": dict(sorted(visible_bars_by_timeframe.items())),
            "visible_bar_rows_total": sum(visible_bars_by_timeframe.values()),
            "sealed_hash_only_contexts": len(sealed),
            "paired_anchors_recomputed": len(anchors_seen),
            "same_symbol_window_overlaps": 0,
            "visible_archive_hashes_referenced_by_symbol": {
                symbol: len(used_archive_hashes[symbol]) for symbol in SYMBOLS
            },
            "labels": 0,
            "model_outputs": 0,
            "gate_executable_samples": 0,
        },
        "evidence": {
            "price_grade": "VERIFIED_ARCHIVE_RECONSTRUCTION_AS_DECLARED",
            "availability_grade": "ASSUMED_PROXY",
            "availability_basis": "BAR_END_PLUS_60_SECONDS_PROXY",
            "historical_gate_receive_time_verified": False,
            "point_in_time_bid_ask_available": False,
        },
        "checks": {
            "frozen_plan_and_manifest_hashes_match": True,
            "original_research_partition_boundaries_match": True,
            "seeded_month_slot_schedule_recomputed": True,
            "visible_partitions_and_rows_match_schedule": True,
            "visible_bars_closed_geometric_contiguous_and_causal": True,
            "proxy_availability_and_source_bindings_match": True,
            "paired_assets_and_eight_day_windows_match": True,
            "same_symbol_windows_do_not_overlap": True,
            "untouched_test_is_hash_coverage_only": True,
            "unregistered_outcome_model_or_account_fields_absent": True,
            "source_archive_bytes_and_derived_feature_frames_recomputed": False,
        },
        "limitations": [
            "This verifier independently checks raw visible bars and frozen sampling metadata but does not reopen or rehash the original source archive files.",
            "It does not reimplement V36 derived feature frames, so it does not independently recompute market_input_sha256 or evidence_ref_count.",
            "The source database and source manifest hashes are matched as declared metadata only; their original bytes are not independently available to this verifier.",
            "The Binance UM reconstruction and 60-second availability proxy are not historical Gate receive-time or point-in-time bid/ask evidence.",
            "Paired temporal contexts are not IID trade observations; zero labels and zero executable samples remain.",
        ],
    }
    body_report["report_sha256"] = _canonical_sha(body_report)
    return body_report


def _is_sha_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0 and all(_is_sha(item) for item in value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--dataset-directory", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, required=True,
                        help="New local report path; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        report = audit_primary_dataset(args.plan, args.dataset_directory)
        target = args.output
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
    except FileExistsError:
        print(json.dumps({"status": "OUTPUT_ALREADY_EXISTS"}))
        return 2
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "AUDIT_REJECTED",
                          "error_code": getattr(exc, "code", type(exc).__name__)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": report["status"], "output": str(args.output.resolve()),
                      "report_sha256": report["report_sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
