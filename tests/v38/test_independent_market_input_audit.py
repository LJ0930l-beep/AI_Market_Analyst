from __future__ import annotations

import ast
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.replay.pa_decision_quality_v38.market_only import build_market_only_input
from scripts import independent_verify_v38_market_inputs as verifier


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _point() -> dict:
    decision = datetime(2025, 10, 12, 12, tzinfo=UTC)
    source_hash = "a" * 64
    database_hash = verifier.SOURCE_DATABASE_SHA256
    manifest_hash = verifier.SOURCE_MANIFEST_SHA256
    bars_by_timeframe = {}
    for timeframe, minutes in (("15m", 15), ("5m", 5), ("1h", 60), ("4h", 240)):
        duration = timedelta(minutes=minutes)
        end = decision - duration
        rows = []
        for index in range(36):
            bar_end = end - duration * (35 - index)
            close = 100.0 + index * 0.07 + (0.45 if index % 7 == 0 else 0.0)
            rows.append({
                "available_at": (bar_end + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
                "available_at_basis": "BAR_END_PLUS_60_SECONDS_PROXY",
                "available_at_evidence_grade": "ASSUMED_PROXY",
                "bar_end": bar_end.isoformat().replace("+00:00", "Z"),
                "bar_start": (bar_end - duration).isoformat().replace("+00:00", "Z"),
                "close": close,
                "high": close + 0.5,
                "is_closed": True,
                "low": close - 0.5,
                "open": close - 0.1,
                "price_evidence_grade": "VERIFIED_ARCHIVE_RECONSTRUCTION",
                "quality_status": "FROZEN_RESEARCH",
                "source": "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVE_RECONSTRUCTED",
                "source_database_sha256": database_hash,
                "source_exchange": "BINANCE_UM",
                "source_file_hash": source_hash,
                "source_file_hashes": [source_hash],
                "source_manifest_sha256": manifest_hash,
                "symbol": "BTCUSDT",
                "timeframe": timeframe,
                "volume": 12.5 + index,
                "volume_unit": "BINANCE_BASE_ASSET_VOLUME",
            })
        bars_by_timeframe[timeframe] = rows
    return {
        "decision_id": "fixture-optimization-20251012-btcusdt",
        "decision_time": decision.isoformat().replace("+00:00", "Z"),
        "symbol": "BTCUSDT",
        "partition": "optimization",
        "bars_by_timeframe": bars_by_timeframe,
    }


def _fixture_dataset(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, dict]:
    plan_path = tmp_path / "plan.json"
    plan = {
        "schema_version": "pa-market-only-v38/stratified-purged-sample-plan-1",
        "partition_policy_sha256": verifier.PARTITION_POLICY_SHA256,
        "source": {
            "source_database_sha256": verifier.SOURCE_DATABASE_SHA256,
            "source_manifest_sha256": verifier.SOURCE_MANIFEST_SHA256,
        },
    }
    plan_bytes = _canonical_bytes(plan)
    plan_path.write_bytes(plan_bytes)
    plan_sha = verifier._canonical_sha(plan)
    monkeypatch.setattr(verifier, "PLAN_SHA256", plan_sha)

    point = _point()
    expected = build_market_only_input(point)
    descriptor = {
        "decision_id": point["decision_id"],
        "partition": point["partition"],
        "symbol": point["symbol"],
        "market_input_sha256": expected["market_input_sha256"],
        "evidence_ref_count": len(expected["evidence_refs"]),
    }
    inputs = {
        "schema_version": "pa-market-only-v38/stratified-purged-market-input-dataset-1",
        "dataset_kind": "MARKET_ONLY_CAUSAL_STRATIFIED_PURGED_PAIRED_CONTEXTS_NO_LABELS",
        "partition_scope": ["optimization", "validation"],
        "plan_sha256": plan_sha,
        "partition_policy_sha256": plan["partition_policy_sha256"],
        "source_database_sha256": plan["source"]["source_database_sha256"],
        "source_manifest_sha256": plan["source"]["source_manifest_sha256"],
        "model_outputs_included": False,
        "outcome_labels_included": False,
        "decision_points": [point],
    }
    input_bytes = _canonical_bytes(inputs)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    input_path = dataset / "optimization-validation-inputs.json"
    input_path.write_bytes(input_bytes)
    manifest = {
        "schema_version": "pa-market-only-v38/stratified-purged-dataset-manifest-1",
        "dataset_id": "FIXTURE_V38_VISIBLE_INPUTS",
        "plan_sha256": plan_sha,
        "partition_policy_sha256": plan["partition_policy_sha256"],
        "partition_counts": {"optimization": 1, "untouched_test": 18, "validation": 0},
        "optimization_validation_input_count": 1,
        "untouched_test_payloads_included": False,
        "untouched_test_hash_only_count": 18,
        "model_outputs": 0,
        "gemini_research_calls_used": 0,
        "orders_created": 0,
        "files": {
            "optimization-validation-inputs.json": _sha(input_bytes),
            # The sealed file must remain unopened by the visible-input auditor.
            "untouched-test-sealed-manifest.json": "e" * 64,
        },
        "points": [descriptor],
    }
    manifest["manifest_sha256"] = verifier._canonical_sha(manifest)
    manifest_bytes = _canonical_bytes(manifest)
    manifest_path = dataset / "dataset-manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(verifier, "DATASET_MANIFEST_SHA256", manifest["manifest_sha256"])
    monkeypatch.setattr(verifier, "VISIBLE_INPUTS_SHA256", _sha(input_bytes))
    monkeypatch.setattr(verifier, "EXPECTED_VISIBLE_POINT_COUNT", 1)
    monkeypatch.setattr(verifier, "EXPECTED_PARTITION_COUNTS", manifest["partition_counts"])
    return plan_path, dataset, input_path, expected


def test_independent_reconstruction_matches_frozen_v38_market_input():
    point = _point()

    reconstructed = verifier.reconstruct_market_only_input(point)
    built = build_market_only_input(point)

    assert reconstructed == built
    assert reconstructed["market_input_sha256"] == verifier._canonical_sha({
        key: value for key, value in reconstructed.items() if key != "market_input_sha256"
    })


def test_future_and_equal_time_rows_do_not_change_reconstructed_input():
    point = _point()
    baseline = verifier.reconstruct_market_only_input(point)
    future = json.loads(json.dumps(point))
    for timeframe, rows in future["bars_by_timeframe"].items():
        duration = timedelta(minutes={"5m": 5, "15m": 15, "1h": 60, "4h": 240}[timeframe])
        end = datetime.fromisoformat(point["decision_time"])
        future_end = end + duration
        rows.append({**rows[-1],
                     "bar_end": future_end.isoformat().replace("+00:00", "Z"),
                     "bar_start": (future_end - duration).isoformat().replace("+00:00", "Z"),
                     "available_at": (future_end + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
                     "close": 999999.0, "high": 1000000.0, "low": 999998.0, "open": 999999.0})

    assert verifier.reconstruct_market_only_input(future) == baseline


def test_late_duplicate_bar_metadata_cannot_change_market_input_provenance():
    point = _point()
    reference_auditor = verifier.reconstruct_market_only_input(point)
    reference_builder = build_market_only_input(point)
    late_duplicate = json.loads(json.dumps(point))
    row = late_duplicate["bars_by_timeframe"]["15m"][-1]
    late_duplicate["bars_by_timeframe"]["15m"].append({
        **row,
        "available_at": point["decision_time"],
        "available_at_basis": "NOT_AVAILABLE_AT_DECISION",
        "available_at_evidence_grade": "UNVERIFIED",
        "source_file_hash": "f" * 64,
        "source_file_hashes": ["f" * 64],
    })

    assert verifier.reconstruct_market_only_input(late_duplicate) == reference_auditor
    assert build_market_only_input(late_duplicate) == reference_builder


def test_visible_input_audit_reconstructs_hashes_without_opening_sealed_payload(tmp_path, monkeypatch):
    plan_path, dataset, _, expected = _fixture_dataset(tmp_path, monkeypatch)

    report = verifier.audit_visible_market_inputs(plan_path, dataset)

    assert report["status"] == "VERIFIED"
    assert report["counts"] == {
        "visible_inputs_reconstructed": 1,
        "market_input_hashes_matched": 1,
        "evidence_ref_counts_matched": 1,
    }
    assert report["sealed_test_payloads_read"] is False
    assert report["operations"] == {
        "network_calls": 0,
        "model_calls": 0,
        "orders_created": 0,
        "account_or_database_opened": False,
        "untouched_test_payloads_read": False,
        "source_writes": 0,
    }
    assert report["visible_input_hashes"][0]["market_input_sha256"] == expected["market_input_sha256"]


def test_auditor_never_opens_sealed_untouched_test_document(tmp_path, monkeypatch):
    plan_path, dataset, _, _ = _fixture_dataset(tmp_path, monkeypatch)
    original_open = Path.open
    original_read_text = Path.read_text

    def guarded_open(path, *args, **kwargs):
        if path.name == "untouched-test-sealed-manifest.json":
            raise AssertionError("sealed untouched-test payload was opened")
        return original_open(path, *args, **kwargs)

    def guarded_read_text(path, *args, **kwargs):
        if path.name == "untouched-test-sealed-manifest.json":
            raise AssertionError("sealed untouched-test payload was read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert verifier.audit_visible_market_inputs(plan_path, dataset)["status"] == "VERIFIED"


def test_visible_input_audit_rejects_frozen_file_hash_mismatch(tmp_path, monkeypatch):
    plan_path, dataset, input_path, _ = _fixture_dataset(tmp_path, monkeypatch)
    input_path.write_bytes(input_path.read_bytes() + b" ")

    report = verifier.audit_visible_market_inputs(plan_path, dataset)

    assert report["status"] == "FAILED_WITH_EVIDENCE"
    assert "VISIBLE_INPUTS_SHA256_MISMATCH" in {item["code"] for item in report["findings"]}


def test_visible_input_audit_rejects_descriptor_hash_mismatch_after_rebinding_file_hashes(tmp_path, monkeypatch):
    plan_path, dataset, input_path, _ = _fixture_dataset(tmp_path, monkeypatch)
    manifest_path = dataset / "dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("manifest_sha256")
    inputs = json.loads(input_path.read_text(encoding="utf-8"))
    inputs["decision_points"][0]["bars_by_timeframe"]["15m"][8]["close"] += 0.01
    inputs["decision_points"][0]["bars_by_timeframe"]["15m"][8]["high"] += 0.01
    input_bytes = _canonical_bytes(inputs)
    input_path.write_bytes(input_bytes)
    manifest["files"]["optimization-validation-inputs.json"] = _sha(input_bytes)
    manifest["manifest_sha256"] = verifier._canonical_sha(manifest)
    manifest_bytes = _canonical_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(verifier, "VISIBLE_INPUTS_SHA256", _sha(input_bytes))
    monkeypatch.setattr(verifier, "DATASET_MANIFEST_SHA256", manifest["manifest_sha256"])

    report = verifier.audit_visible_market_inputs(plan_path, dataset)

    assert report["status"] == "FAILED_WITH_EVIDENCE"
    assert "MARKET_INPUT_HASH_MISMATCH" in {item["code"] for item in report["findings"]}


def test_visible_input_audit_rejects_evidence_reference_count_mismatch(tmp_path, monkeypatch):
    plan_path, dataset, _, _ = _fixture_dataset(tmp_path, monkeypatch)
    manifest_path = dataset / "dataset-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("manifest_sha256")
    manifest["points"][0]["evidence_ref_count"] += 1
    manifest["manifest_sha256"] = verifier._canonical_sha(manifest)
    manifest_bytes = _canonical_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(verifier, "DATASET_MANIFEST_SHA256", manifest["manifest_sha256"])

    report = verifier.audit_visible_market_inputs(plan_path, dataset)

    assert "EVIDENCE_REF_COUNT_MISMATCH" in {item["code"] for item in report["findings"]}


def test_reconstruction_rejects_invalid_evidence_and_account_fields():
    point = _point()
    point["equity_usdt"] = 1000
    with pytest.raises(verifier.IndependentAuditError, match="MARKET_ONLY_ACCOUNT_OR_EXECUTION_INPUT_FORBIDDEN"):
        verifier.reconstruct_market_only_input(point)


def test_independent_auditor_imports_no_project_modules():
    tree = ast.parse(Path(verifier.__file__).read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= sys.stdlib_module_names


def test_cli_writes_redacted_report_exclusively_and_does_not_overwrite(tmp_path, monkeypatch, capsys):
    plan_path, dataset, _, _ = _fixture_dataset(tmp_path, monkeypatch)
    output = tmp_path / "audit.json"

    result = verifier.main(["--plan", str(plan_path), "--dataset", str(dataset), "--output", str(output)])

    assert result == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "VERIFIED"
    assert str(tmp_path) not in output.read_text(encoding="utf-8")
    assert "VERIFIED" in capsys.readouterr().out
    assert verifier.main(["--plan", str(plan_path), "--dataset", str(dataset), "--output", str(output)]) != 0
