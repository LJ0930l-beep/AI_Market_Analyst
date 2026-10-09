from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.independent_verify_v38_gate2 import (
    DESCRIPTOR_FIELDS,
    PLAN_SHA256,
    SOURCE_DATABASE_SHA256,
    IndependentAuditError,
    _check_hash_only_document,
    _check_sealed_rows_match_descriptors,
    _utc,
    expected_anchor_schedule,
    partition_for,
    validate_bar_series,
)

ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = ROOT / "configs/research/datasets/v38-stratified-purged-sample-plan-v1.json"


def _plan() -> dict:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def _bar_row(*, available_at: str = "2025-10-10T00:16:00Z") -> dict:
    plan = _plan()
    return {
        "available_at": available_at,
        "available_at_basis": "ASSUMED_PROXY_60S_AFTER_BAR_END_NOT_OBSERVED",
        "available_at_evidence_grade": "ASSUMED_PROXY",
        "bar_end": "2025-10-10T00:15:00Z",
        "bar_start": "2025-10-10T00:00:00Z",
        "close": 101.0,
        "high": 102.0,
        "is_closed": True,
        "low": 99.0,
        "open": 100.0,
        "price_evidence_grade": "VERIFIED_ARCHIVE_RECONSTRUCTION",
        "quality_status": "FROZEN_RESEARCH",
        "source": "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVE_RECONSTRUCTED",
        "source_database_sha256": plan["source"]["source_database_sha256"],
        "source_exchange": "BINANCE_UM",
        "source_file_hash": "a" * 64,
        "source_file_hashes": ["a" * 64],
        "source_manifest_sha256": plan["source"]["source_manifest_sha256"],
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "volume": 2.0,
        "volume_unit": "BINANCE_BASE_ASSET_VOLUME",
    }


def test_partition_resolution_matches_frozen_half_open_edges():
    assert partition_for(_utc("2025-10-01T00:00:00Z")) == "optimization"
    assert partition_for(_utc("2026-04-01T00:00:00Z")) == "validation"
    assert partition_for(_utc("2026-07-01T00:00:00Z")) == "untouched_test"
    with pytest.raises(IndependentAuditError, match="DECISION_TIME_OUTSIDE_FROZEN_PARTITIONS"):
        partition_for(_utc("2026-10-01T00:00:00Z"))


def test_frozen_month_slot_schedule_recomputes_36_paired_anchors():
    plan = _plan()
    first = expected_anchor_schedule(plan)
    second = expected_anchor_schedule(plan)
    assert first == second
    assert len(first) == 36
    assert {partition: sum(key[0] == partition for key in first)
            for partition in ("optimization", "validation", "untouched_test")} == {
        "optimization": 18, "validation": 9, "untouched_test": 9,
    }
    assert plan["source"]["source_database_sha256"] == SOURCE_DATABASE_SHA256
    assert PLAN_SHA256 == "094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f"


def test_independent_bar_check_accepts_causal_closed_binance_bar():
    used = validate_bar_series(
        [_bar_row()], timeframe="15m", symbol="BTCUSDT",
        decision_time=_utc("2025-10-10T12:00:00Z"), plan=_plan(),
    )
    assert used == {"a" * 64}


def test_independent_bar_check_rejects_future_availability():
    row = _bar_row(available_at="2025-10-10T12:00:01Z")
    with pytest.raises(IndependentAuditError, match="VISIBLE_BAR_NOT_CAUSAL_OR_OUTSIDE_WINDOW"):
        validate_bar_series(
            [row], timeframe="15m", symbol="BTCUSDT",
            decision_time=_utc("2025-10-10T12:00:00Z"), plan=_plan(),
        )


def test_independent_bar_check_rejects_invalid_geometry_and_archive_binding():
    row = _bar_row()
    row["low"] = 103.0
    with pytest.raises(IndependentAuditError, match="VISIBLE_BAR_GEOMETRY_INVALID"):
        validate_bar_series(
            [row], timeframe="15m", symbol="BTCUSDT",
            decision_time=_utc("2025-10-10T12:00:00Z"), plan=_plan(),
        )
    row = _bar_row()
    row["source_database_sha256"] = "b" * 64
    with pytest.raises(IndependentAuditError, match="VISIBLE_BAR_SOURCE_BINDING_MISMATCH"):
        validate_bar_series(
            [row], timeframe="15m", symbol="BTCUSDT",
            decision_time=_utc("2025-10-10T12:00:00Z"), plan=_plan(),
        )


def test_hash_only_manifest_rejects_payload_fields():
    plan = _plan()
    row = {key: None for key in DESCRIPTOR_FIELDS}
    row.update({"partition": "untouched_test", "market_input_sha256": "c" * 64})
    document = {
        "decision_points": [row] * 18,
        "labels_or_evaluation_included": False,
        "market_input_payloads_included": False,
        "partition": "untouched_test",
        "partition_policy_sha256": plan["partition_policy_sha256"],
        "persist_only_hashes_and_coverage": True,
        "plan_sha256": PLAN_SHA256,
        "schema_version": "pa-market-only-v38/stratified-purged-untouched-test-sealed-1",
        "source_database_sha256": SOURCE_DATABASE_SHA256,
    }
    assert len(_check_hash_only_document(document, plan=plan)) == 18
    document["future_outcome"] = "forbidden"
    with pytest.raises(IndependentAuditError, match="SEALED_DOCUMENT_FIELDS_INVALID"):
        _check_hash_only_document(document, plan=plan)


def test_sealed_rows_must_match_frozen_manifest_descriptors():
    first = {
        "decision_time": "2026-07-10T12:00:00Z",
        "partition": "untouched_test",
        "symbol": "BTCUSDT",
        "market_input_sha256": "a" * 64,
    }
    second = {
        "decision_time": "2026-07-10T12:00:00Z",
        "partition": "untouched_test",
        "symbol": "ETHUSDT",
        "market_input_sha256": "b" * 64,
    }
    third = {
        "decision_time": "2026-07-11T12:00:00Z",
        "partition": "untouched_test",
        "symbol": "BTCUSDT",
        "market_input_sha256": "c" * 64,
    }
    descriptors = {
        (row["partition"], row["decision_time"], row["symbol"]): row
        for row in (first, second, third)
    }
    _check_sealed_rows_match_descriptors([first, second, third], descriptors)
    with pytest.raises(IndependentAuditError, match="SEALED_TEST_DESCRIPTOR_COUNT_INVALID"):
        _check_sealed_rows_match_descriptors([first, second], descriptors)
    with pytest.raises(IndependentAuditError, match="SEALED_TEST_DESCRIPTOR_DUPLICATE"):
        _check_sealed_rows_match_descriptors([first, first, third], descriptors)
    with pytest.raises(IndependentAuditError, match="SEALED_TEST_DESCRIPTOR_MISMATCH"):
        _check_sealed_rows_match_descriptors(
            [first, second, {**third, "market_input_sha256": "d" * 64}], descriptors,
        )


def test_naive_timestamp_is_rejected():
    with pytest.raises(IndependentAuditError, match="TIME_INVALID"):
        _utc("2025-10-10T12:00:00")
