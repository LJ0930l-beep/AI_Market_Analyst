from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.replay.pa_decision_quality_v38.dataset import DatasetBuildError
from scripts.audit_v38_dataset_integrity import (
    _verify_document_source_binding,
    count_partition_scoped_components,
    summarize_global_overlap,
    summarize_partition_overlap,
)

ROOT = Path(__file__).resolve().parents[2]


def _point(decision_id: str, symbol: str, partition: str, decision: datetime) -> dict[str, str]:
    return {
        "decision_id": decision_id,
        "symbol": symbol,
        "partition": partition,
        "input_window_start": (decision - timedelta(days=8)).isoformat().replace("+00:00", "Z"),
        "input_window_end_exclusive": decision.isoformat().replace("+00:00", "Z"),
    }


def test_global_overlap_summary_counts_cross_partition_pairs_and_components():
    points = [
        _point("btc-opt", "BTCUSDT", "optimization", datetime(2026, 3, 30, 12, tzinfo=UTC)),
        _point("btc-val", "BTCUSDT", "validation", datetime(2026, 4, 4, 12, tzinfo=UTC)),
        _point("btc-val-later", "BTCUSDT", "validation", datetime(2026, 4, 12, 12, tzinfo=UTC)),
        _point("btc-test", "BTCUSDT", "untouched_test", datetime(2026, 4, 21, 12, tzinfo=UTC)),
        _point("eth-val", "ETHUSDT", "validation", datetime(2026, 6, 30, 12, tzinfo=UTC)),
        _point("eth-test", "ETHUSDT", "untouched_test", datetime(2026, 7, 1, 12, tzinfo=UTC)),
    ]

    result = summarize_global_overlap(points)

    assert result["global_overlap_component_count"] == 4
    assert result["global_overlap_component_count_by_symbol"] == {"BTCUSDT": 3, "ETHUSDT": 1}
    assert result["components_spanning_multiple_partitions"] == 2
    assert result["cross_partition_overlapping_pair_count"] == 2
    assert result["cross_partition_pair_counts"] == {
        "optimization/validation": 1,
        "validation/untouched_test": 1,
    }
    assert result["independent_sample_count_claimed"] is False
    assert count_partition_scoped_components(points) == 6
    partition_summary = summarize_partition_overlap(points)
    assert partition_summary["BTCUSDT:validation"] == {
        "row_count": 2,
        "component_count": 2,
        "component_sizes_descending": [1, 1],
        "independent_sample_count_claimed": False,
    }


def test_global_overlap_summary_rejects_unhashable_partition_label():
    point = _point("invalid-partition", "BTCUSDT", "validation", datetime(2026, 6, 1, tzinfo=UTC))
    point["partition"] = {"unexpected": "object"}

    with pytest.raises(DatasetBuildError, match="V38_OVERLAP_PARTITION_INVALID"):
        summarize_global_overlap([point])


@pytest.mark.parametrize("bad_window", [
    {"input_window_start": "2026-01-01T00:00:00", "input_window_end_exclusive": "2026-01-02T00:00:00Z"},
    {"input_window_start": "2026-01-02T00:00:00Z", "input_window_end_exclusive": "2026-01-01T00:00:00Z"},
])
def test_global_overlap_summary_rejects_invalid_window_boundaries(bad_window):
    point = {
        "decision_id": "invalid",
        "symbol": "BTCUSDT",
        "partition": "validation",
        **bad_window,
    }

    with pytest.raises(DatasetBuildError, match="V38_OVERLAP_WINDOW"):
        summarize_global_overlap([point])


def test_dataset_documents_bind_to_frozen_source_without_expanding_sealed_manifest():
    source = {
        "database_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
    }
    input_document = {
        "plan_sha256": "c" * 64,
        "source_database_sha256": source["database_sha256"],
        "source_manifest_sha256": source["manifest_sha256"],
    }
    sealed_document = {
        "plan_sha256": "c" * 64,
        "source_database_sha256": source["database_sha256"],
    }

    _verify_document_source_binding(input_document, sealed_document, source, "c" * 64)

    input_document["source_manifest_sha256"] = "d" * 64
    with pytest.raises(DatasetBuildError, match="V38_AUDIT_INPUT_SOURCE_BINDING_INVALID"):
        _verify_document_source_binding(input_document, sealed_document, source, "c" * 64)


@pytest.mark.parametrize("script", [
    "scripts/build_v38_market_dataset.py",
    "scripts/run_market_only_v38.py",
    "scripts/audit_v38_dataset_integrity.py",
])
def test_v38_cli_help_is_runnable_as_a_direct_script(script):
    result = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
