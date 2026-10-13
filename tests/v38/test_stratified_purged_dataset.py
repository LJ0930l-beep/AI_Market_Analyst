from __future__ import annotations

import json
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from core.replay.pa_decision_quality_v38.blind_labels import load_blind_label_protocol
from core.replay.pa_decision_quality_v38.stratified_purged_dataset import (
    STRATIFIED_PURGED_PLAN_SHA256,
    StratifiedPurgedDatasetError,
    _validate_stratified_purged_selection,
    load_stratified_purged_sample_plan,
    select_stratified_purged_calendar_points,
)
from scripts.audit_v38_stratified_purged_dataset import (
    INPUT_POINT_FIELDS,
    MANIFEST_FIELDS,
    POINT_DESCRIPTOR_FIELDS,
    SEALED_DOCUMENT_FIELDS,
    StratifiedPurgedAuditError,
    _require_exact_fields,
    _validate_blind_label_input_bindings,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("fields", "error_code"),
    [
        (MANIFEST_FIELDS, "V38_STRATIFIED_PURGED_AUDIT_MANIFEST_FIELDS_INVALID"),
        (INPUT_POINT_FIELDS, "V38_STRATIFIED_PURGED_AUDIT_INPUT_POINT_FIELDS_INVALID"),
        (POINT_DESCRIPTOR_FIELDS, "V38_STRATIFIED_PURGED_AUDIT_DESCRIPTOR_FIELDS_INVALID"),
        (SEALED_DOCUMENT_FIELDS, "V38_STRATIFIED_PURGED_AUDIT_SEALED_FIELDS_INVALID"),
    ],
)
def test_audited_artifact_schemas_reject_unregistered_payload_fields(fields, error_code):
    artifact = {field: None for field in fields}
    _require_exact_fields(artifact, fields, error_code)

    artifact["label"] = {"future_outcome": "LONG"}
    with pytest.raises(StratifiedPurgedAuditError, match=error_code):
        _require_exact_fields(artifact, fields, error_code)


def test_independent_auditor_binds_protocol_registry_to_exact_visible_manifest_inputs():
    protocol, _ = load_blind_label_protocol()
    dataset_id = "V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1"
    descriptors = [dict(row) for row in protocol["eligible_input_bindings"][dataset_id]]
    manifest_sha256 = "1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a"

    assert _validate_blind_label_input_bindings(protocol, descriptors, manifest_sha256) == 54

    descriptors[0]["market_input_sha256"] = "0" * 64
    with pytest.raises(StratifiedPurgedAuditError, match="V38_STRATIFIED_PURGED_AUDIT_LABEL_INPUT_BINDINGS_MISMATCH"):
        _validate_blind_label_input_bindings(protocol, descriptors, manifest_sha256)


def test_seeded_monthly_strata_are_reproducible_and_cover_each_partition():
    plan, digest = load_stratified_purged_sample_plan()
    first = select_stratified_purged_calendar_points()
    second = select_stratified_purged_calendar_points()

    assert digest == STRATIFIED_PURGED_PLAN_SHA256
    assert first == second
    assert len(first) == 72
    assert Counter(row["partition"] for row in first) == {
        "optimization": 36,
        "validation": 18,
        "untouched_test": 18,
    }
    assert Counter((row["symbol"], row["partition"]) for row in first) == Counter({
        (symbol, partition): (18 if partition == "optimization" else 9)
        for symbol in ("BTCUSDT", "ETHUSDT")
        for partition in ("optimization", "validation", "untouched_test")
    })
    assert plan["selection"]["runtime_randomness"] is False


def test_every_monthly_slot_is_seed_selected_and_asset_paired():
    points = select_stratified_purged_calendar_points()
    by_anchor = defaultdict(list)
    strata = set()
    for row in points:
        stamp = datetime.fromisoformat(row["decision_time"])
        assert stamp.hour == 12 and stamp.minute == 0
        assert stamp.day in row["candidate_days"]
        assert row["selected_day"] == stamp.day
        assert row["stratum_id"].endswith(("-S1", "-S2", "-S3"))
        strata.add(row["stratum_id"])
        by_anchor[row["paired_anchor_group_id"]].append(row)
    assert len(strata) == 36
    assert all({row["symbol"] for row in pair} == {"BTCUSDT", "ETHUSDT"} for pair in by_anchor.values())
    assert all(len({row["decision_time"] for row in pair}) == 1 for pair in by_anchor.values())


def test_eight_day_windows_are_disjoint_inside_and_across_partitions():
    plan, _ = load_stratified_purged_sample_plan()
    points = select_stratified_purged_calendar_points()
    by_symbol = defaultdict(list)
    bounds = {item["id"]: item for item in plan["partitions"]}
    for row in points:
        start = datetime.fromisoformat(row["input_window_start"])
        end = datetime.fromisoformat(row["input_window_end_exclusive"])
        decision = datetime.fromisoformat(row["decision_time"])
        assert end == decision
        assert end - start == timedelta(days=8)
        assert start >= datetime.fromisoformat(bounds[row["partition"]]["start_utc_inclusive"])
        assert row["warmup_crosses_partition_start"] is False
        by_symbol[row["symbol"]].append(row)
    for rows in by_symbol.values():
        rows.sort(key=lambda row: row["decision_time"])
        assert all(
            datetime.fromisoformat(left["input_window_end_exclusive"])
            <= datetime.fromisoformat(right["input_window_start"])
            for left, right in pairwise(rows)
        )


def test_selection_validator_rejects_wrong_partition_and_modified_anchor():
    plan, digest = load_stratified_purged_sample_plan()
    points = select_stratified_purged_calendar_points()
    wrong_partition = deepcopy(points)
    wrong_partition[0]["partition"] = "validation"
    with pytest.raises(StratifiedPurgedDatasetError):
        _validate_stratified_purged_selection(wrong_partition, plan, digest)

    wrong_anchor = deepcopy(points)
    wrong_anchor[0]["selected_day"] = wrong_anchor[0]["selected_day"] + 1
    with pytest.raises(StratifiedPurgedDatasetError, match="V38_STRATIFIED_PURGED_STRATUM_BINDING_MISMATCH"):
        _validate_stratified_purged_selection(wrong_anchor, plan, digest)


def test_plan_hash_and_frozen_partition_boundaries_fail_closed(tmp_path):
    plan, _ = load_stratified_purged_sample_plan()
    tampered = deepcopy(plan)
    tampered["partitions"][0]["end_utc_exclusive"] = "2026-04-02T00:00:00Z"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")

    with pytest.raises(StratifiedPurgedDatasetError, match="V38_STRATIFIED_PURGED_PLAN_HASH_MISMATCH"):
        load_stratified_purged_sample_plan(path)


def test_registered_selection_plan_binds_existing_verified_archive_and_does_not_pool_rows():
    plan, _ = load_stratified_purged_sample_plan()
    assert plan["source"]["source_database_sha256"] == "c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2"
    assert plan["invariants"]["untouched_test_payloads_persisted"] is False
    assert plan["selection"]["iid_or_independent_trade_claim"] is False
    assert plan["partitions"][2]["expected_sealed_hash_count"] == 18
