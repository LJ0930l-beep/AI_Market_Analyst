from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from core.replay.pa_decision_quality_v38.dataset import canonical_sha256
from core.replay.pa_decision_quality_v38.nonoverlap_dataset import (
    NonOverlapDatasetError,
    _validate_nonoverlap_selection,
    load_nonoverlap_sample_plan,
    select_nonoverlap_calendar_points,
)
from scripts.audit_v38_nonoverlap_dataset import (
    MANIFEST_FIELDS,
    _validate_bar_series,
    _validate_input_point_binding,
    _verify_output_manifest,
)
from scripts.audit_v38_nonoverlap_dataset import (
    NonOverlapDatasetError as AuditError,
)

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_HASH = "a" * 64
DATABASE_HASH = "b" * 64
SOURCE_MANIFEST_HASH = "c" * 64


def test_frozen_sample_has_paired_fixed_anchors_and_disjoint_eight_day_windows():
    _plan, plan_hash = load_nonoverlap_sample_plan()
    points = select_nonoverlap_calendar_points()

    assert len(plan_hash) == 64
    assert len(points) == 80
    assert Counter(point["partition"] for point in points) == {
        "optimization": 40,
        "validation": 20,
        "untouched_test": 20,
    }
    anchors: dict[str, set[tuple[str, str]]] = defaultdict(set)
    by_symbol: dict[str, list[dict[str, str]]] = defaultdict(list)
    for point in points:
        anchors[point["paired_anchor_group_id"]].add((point["symbol"], point["decision_time"]))
        by_symbol[point["symbol"]].append(point)
        assert point["input_window_start"] < point["input_window_end_exclusive"]
        assert point["warmup_crosses_partition_start"] is False
    assert len(anchors) == 40
    assert all(
        {symbol for symbol, _ in pair} == {"BTCUSDT", "ETHUSDT"}
        and len({stamp for _, stamp in pair}) == 1 for pair in anchors.values()
    )
    for symbol_points in by_symbol.values():
        ordered = sorted(symbol_points, key=lambda row: row["decision_time"])
        for left, right in pairwise(ordered):
            assert left["input_window_end_exclusive"] <= right["input_window_start"]


def test_partition_boundary_label_mismatch_is_rejected():
    plan, _ = load_nonoverlap_sample_plan()
    points = select_nonoverlap_calendar_points()
    wrong = next(point for point in points if point["partition"] == "optimization").copy()
    wrong["partition"] = "validation"
    wrong["paired_anchor_group_id"] = wrong["paired_anchor_group_id"].replace(
        "optimization", "validation",
    )
    wrong["decision_id"] = wrong["decision_id"].replace("optimization", "validation")
    points[points.index(next(point for point in points if point["symbol"] == wrong["symbol"]
                             and point["decision_time"] == wrong["decision_time"]))] = wrong

    with pytest.raises(NonOverlapDatasetError, match="V38_NONOVERLAP_PARTITION_LABEL_MISMATCH"):
        _validate_nonoverlap_selection(points, plan)


def _binding_fixture() -> tuple[dict[str, str], dict[str, str], dict[str, tuple[datetime, datetime]]]:
    point = {
        "decision_time": "2026-05-10T12:00:00Z",
        "symbol": "BTCUSDT",
        "partition": "validation",
    }
    descriptor = {
        "decision_time": "2026-05-10T12:00:00Z",
        "symbol": "BTCUSDT",
        "partition": "validation",
        "input_window_start": "2026-05-02T12:00:00Z",
        "input_window_end_exclusive": "2026-05-10T12:00:00Z",
    }
    bounds = {
        "validation": (datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
    }
    return point, descriptor, bounds


def test_audit_binds_payload_symbol_partition_time_and_purge_window():
    point, descriptor, bounds = _binding_fixture()
    result = _validate_input_point_binding(point, descriptor, bounds, timedelta(days=8))
    assert result == datetime(2026, 5, 10, 12, tzinfo=UTC)

    wrong_partition = {**point, "partition": "optimization"}
    with pytest.raises(AuditError, match="V38_NONOVERLAP_AUDIT_INPUT_IDENTITY_MISMATCH"):
        _validate_input_point_binding(wrong_partition, descriptor, bounds, timedelta(days=8))

    wrong_window = {**descriptor, "input_window_start": "2026-05-03T12:00:00Z"}
    with pytest.raises(AuditError, match="V38_NONOVERLAP_AUDIT_PARTITION_OR_PURGE_INVALID"):
        _validate_input_point_binding(point, wrong_window, bounds, timedelta(days=8))


def _bar(*, available_at: str = "2026-07-08T08:01:00Z", open_value: object = 100.0) -> dict:
    return {
        "symbol": "BTCUSDT",
        "timeframe": "4h",
        "bar_start": "2026-07-08T04:00:00Z",
        "bar_end": "2026-07-08T08:00:00Z",
        "available_at": available_at,
        "available_at_basis": "ASSUMED_PROXY_60S_AFTER_BAR_END_NOT_OBSERVED",
        "available_at_evidence_grade": "ASSUMED_PROXY",
        "price_evidence_grade": "VERIFIED_ARCHIVE_RECONSTRUCTION",
        "quality_status": "FROZEN_RESEARCH",
        "is_closed": True,
        "volume_unit": "BINANCE_BASE_ASSET_VOLUME",
        "open": open_value,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 5.0,
        "source_file_hash": ARCHIVE_HASH,
        "source": "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVE_RECONSTRUCTED",
        "source_exchange": "BINANCE_UM",
        "source_file_hashes": [ARCHIVE_HASH],
        "source_database_sha256": DATABASE_HASH,
        "source_manifest_sha256": SOURCE_MANIFEST_HASH,
    }


def _validate_one_bar(bar: dict) -> set[str]:
    return _validate_bar_series(
        "4h", [bar], decision=datetime(2026, 7, 8, 12, tzinfo=UTC),
        history=timedelta(hours=8), delay_seconds=60, symbol="BTCUSDT",
        source_database_sha256=DATABASE_HASH,
        source_manifest_sha256=SOURCE_MANIFEST_HASH,
    )


def test_bar_availability_must_match_exact_delay_and_be_causal():
    assert _validate_one_bar(_bar()) == {ARCHIVE_HASH}

    with pytest.raises(AuditError, match="V38_NONOVERLAP_AUDIT_NONCAUSAL_OR_MISALIGNED_BAR"):
        _validate_one_bar(_bar(available_at="2026-07-08T08:01:00.900000Z"))
    with pytest.raises(AuditError, match="V38_NONOVERLAP_AUDIT_NONCAUSAL_OR_MISALIGNED_BAR"):
        _validate_one_bar(_bar(available_at="2026-07-08T12:01:00Z"))


@pytest.mark.parametrize("bad_value", [True, "100", 10**1000])
def test_bar_values_reject_boolean_string_and_overflow_inputs(bad_value):
    with pytest.raises(AuditError, match="V38_NONOVERLAP_AUDIT_BAR_VALUE_INVALID"):
        _validate_one_bar(_bar(open_value=bad_value))


def test_bar_schema_rejects_unexpected_outcome_fields():
    bar = {**_bar(), "outcome_label": "WIN"}
    with pytest.raises(AuditError, match="V38_NONOVERLAP_AUDIT_BAR_FIELDS_INVALID"):
        _validate_one_bar(bar)


def test_auditor_rejects_extra_dataset_files_and_unversioned_manifest_fields(tmp_path):
    input_bytes = b"{}\n"
    sealed_bytes = b"{}\n"
    (tmp_path / "optimization-validation-inputs.json").write_bytes(input_bytes)
    (tmp_path / "untouched-test-sealed-manifest.json").write_bytes(sealed_bytes)
    body = {field: None for field in MANIFEST_FIELDS if field != "manifest_sha256"}
    body["schema_version"] = "pa-market-only-v38/nonoverlap-dataset-manifest-1"
    body["source"] = {
        "archive_directory_role": None,
        "database_sha256": None,
        "manifest_sha256": None,
        "archive_files_verified": None,
        "used_archive_hash_count": None,
        "source_window_start": None,
        "source_window_end": None,
        "symbols": None,
        "complete_data": None,
        "network_calls": None,
    }
    body["files"] = {
        "optimization-validation-inputs.json": hashlib.sha256(input_bytes).hexdigest(),
        "untouched-test-sealed-manifest.json": hashlib.sha256(sealed_bytes).hexdigest(),
    }
    manifest = {**body, "manifest_sha256": canonical_sha256(body)}
    manifest_path = tmp_path / "dataset-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    _verify_output_manifest(tmp_path)
    (tmp_path / "extra-outcome.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AuditError, match="V38_NONOVERLAP_AUDIT_UNEXPECTED_OUTPUT_CONTENT"):
        _verify_output_manifest(tmp_path)

    (tmp_path / "extra-outcome.json").unlink()
    manifest["outcomes"] = []
    body_with_extra = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = canonical_sha256(body_with_extra)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AuditError, match="V38_NONOVERLAP_AUDIT_MANIFEST_FIELDS_INVALID"):
        _verify_output_manifest(tmp_path)


@pytest.mark.parametrize("script", [
    "scripts/build_v38_nonoverlap_dataset.py",
    "scripts/audit_v38_nonoverlap_dataset.py",
])
def test_nonoverlap_cli_help_is_runnable_as_a_direct_script(script):
    result = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
