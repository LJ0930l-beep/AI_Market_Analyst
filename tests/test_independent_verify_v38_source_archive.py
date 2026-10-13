from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import independent_verify_v38_gate2 as verifier


def _months(start: str, count: int) -> list[str]:
    year, month = map(int, start.split("-"))
    result = []
    for _ in range(count):
        result.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def _fixture_source_bundle(tmp_path: Path, monkeypatch) -> tuple[Path, dict[str, Path]]:
    root = tmp_path / "source-bundle"
    cache = root / "raw_cache"
    cache.mkdir(parents=True)
    database = b"synthetic opaque sqlite fixture\n"
    (root / "research.sqlite3").write_bytes(database)
    database_sha = hashlib.sha256(database).hexdigest()
    monkeypatch.setattr(verifier, "SOURCE_ARCHIVE_DATABASE_SHA256", database_sha)

    records = []
    archive_paths: dict[str, Path] = {}
    for kind, start, count in (("klines", "2025-09", 13), ("fundingRate", "2025-10", 12)):
        for symbol in verifier.SYMBOLS:
            for month in _months(start, count):
                filename = verifier._source_archive_filename(kind, symbol, month)
                if kind == "klines":
                    url = f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1m/{filename}"
                else:
                    url = f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{symbol}/{filename}"
                archive_bytes = f"synthetic archive fixture {kind} {symbol} {month}\n".encode()
                archive_sha = hashlib.sha256(archive_bytes).hexdigest()
                record = {
                    "bytes": len(archive_bytes),
                    "checksum_url": f"{url}.CHECKSUM",
                    "fetched_at": "2026-10-09T00:00:00Z",
                    "kind": kind,
                    "month": month,
                    "official_checksum_verified": True,
                    "sha256": archive_sha,
                    "symbol": symbol,
                    "url": url,
                }
                archive_path = cache / filename
                archive_path.write_bytes(archive_bytes)
                (cache / f"{filename}.CHECKSUM").write_text(
                    f"{archive_sha}  {filename}\n", encoding="ascii",
                )
                provenance = {key: record[key] for key in verifier.SOURCE_PROVENANCE_FIELDS}
                (cache / f"{filename}.provenance.json").write_text(
                    json.dumps(provenance, sort_keys=True), encoding="utf-8",
                )
                records.append(record)
                archive_paths[filename] = archive_path

    manifest = {
        "analysis_type": "TECHNICAL_RULE_PROXY_NOT_REAL_AI_OR_GATE",
        "archived_files": records,
        "complete_data": True,
        "coverage": {"fixture": True},
        "dataset_sha256": database_sha,
        "exchange": "BINANCE_UM",
        "frozen_at": "2026-10-09T00:00:00Z",
        "historical_availability_assumption": "fixture only",
        "not_gate_data": True,
        "research_only": True,
        "schema_version": verifier.SOURCE_MANIFEST_SCHEMA,
        "source_type": "OFFICIAL_MONTHLY_PUBLIC_ARCHIVES",
        "symbols": list(verifier.SYMBOLS),
        "warmup_days": 8,
        "warmup_start": "2025-09-01T00:00:00Z",
        "window_end": "2026-10-01T00:00:00Z",
        "window_start": "2025-10-01T00:00:00Z",
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
    (root / "manifest.json").write_bytes(manifest_bytes)
    monkeypatch.setattr(
        verifier, "SOURCE_ARCHIVE_MANIFEST_SHA256", hashlib.sha256(manifest_bytes).hexdigest(),
    )
    return root, archive_paths


def test_source_archive_audit_rehashes_local_fixture_and_discloses_no_source_path(tmp_path, monkeypatch):
    root, _ = _fixture_source_bundle(tmp_path, monkeypatch)

    report = verifier.audit_source_archive_directory(root)

    assert report["status"] == "VERIFIED"
    assert report["source_root_path_disclosed"] is False
    assert len(report["auditor_code_sha256"]) == 64
    assert report["source_hashes"]["manifest_observed_sha256"] == report["source_hashes"]["manifest_expected_sha256"]
    assert report["source_hashes"]["database_observed_sha256"] == report["source_hashes"]["database_expected_sha256"]
    assert report["counts"] == {
        "manifest_files_verified": 1,
        "source_databases_verified": 1,
        "archive_records_declared": 50,
        "archive_records_by_kind": {"fundingRate": 24, "klines": 26},
        "archives_verified": 50,
        "archive_bytes_rehashed": sum(path.stat().st_size for path in (root / "raw_cache").glob("*.zip")),
        "checksum_sidecars_matched": 50,
        "provenance_sidecars_matched": 50,
        "raw_cache_files_found": 150,
    }
    assert report["operations"] == {
        "network_calls": 0,
        "model_calls": 0,
        "orders_created": 0,
        "database_opened": False,
        "archives_extracted": False,
        "source_writes": 0,
    }
    assert str(root) not in json.dumps(report)


def test_source_archive_audit_rejects_changed_archive_bytes(tmp_path, monkeypatch):
    root, archives = _fixture_source_bundle(tmp_path, monkeypatch)
    next(iter(archives.values())).write_bytes(b"tampered archive")

    report = verifier.audit_source_archive_directory(root)

    assert report["status"] == "FAILED_WITH_EVIDENCE"
    assert report["counts"]["archives_verified"] == 49
    assert any(item["code"] == "SOURCE_ARCHIVE_SHA256_OR_LENGTH_MISMATCH" for item in report["findings"])


def test_source_archive_audit_rejects_missing_and_extra_cache_files(tmp_path, monkeypatch):
    root, archives = _fixture_source_bundle(tmp_path, monkeypatch)
    missing_archive = next(iter(archives.values()))
    missing_archive.unlink()
    (root / "raw_cache" / "unexpected.bin").write_bytes(b"extra")

    report = verifier.audit_source_archive_directory(root)

    assert report["status"] == "FAILED_WITH_EVIDENCE"
    assert any(item["code"] == "RAW_CACHE_FILESET_MISMATCH" for item in report["findings"])
    assert any(item["code"] == "SOURCE_FILE_MISSING" for item in report["findings"])


def test_source_archive_audit_revalidates_checksum_and_provenance_sidecars(tmp_path, monkeypatch):
    root, archives = _fixture_source_bundle(tmp_path, monkeypatch)
    filename = next(iter(archives))
    (root / "raw_cache" / f"{filename}.CHECKSUM").write_text("not a checksum\n", encoding="ascii")
    provenance_path = root / "raw_cache" / f"{filename}.provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["sha256"] = "0" * 64
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    report = verifier.audit_source_archive_directory(root)

    assert report["status"] == "FAILED_WITH_EVIDENCE"
    assert report["counts"]["checksum_sidecars_matched"] == 49
    assert report["counts"]["provenance_sidecars_matched"] == 49
    assert {item["code"] for item in report["findings"]} >= {
        "SOURCE_CHECKSUM_SIDECAR_MISMATCH", "SOURCE_PROVENANCE_SIDECAR_MISMATCH",
    }


def test_source_archive_audit_rejects_manifest_schema_even_with_matching_new_hash(tmp_path, monkeypatch):
    root, _ = _fixture_source_bundle(tmp_path, monkeypatch)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["complete_data"] = False
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(
        verifier, "SOURCE_ARCHIVE_MANIFEST_SHA256", hashlib.sha256(manifest_bytes).hexdigest(),
    )

    report = verifier.audit_source_archive_directory(root)

    assert report["status"] == "FAILED_WITH_EVIDENCE"
    assert any(
        item["code"] == "SOURCE_MANIFEST_IDENTITY_OR_DATABASE_BINDING_INVALID"
        for item in report["findings"]
    )


def test_source_archive_audit_rejects_malformed_record_without_crashing(tmp_path, monkeypatch):
    root, _ = _fixture_source_bundle(tmp_path, monkeypatch)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["archived_files"][0]["kind"] = []
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(
        verifier, "SOURCE_ARCHIVE_MANIFEST_SHA256", hashlib.sha256(manifest_bytes).hexdigest(),
    )

    report = verifier.audit_source_archive_directory(root)

    assert report["status"] == "FAILED_WITH_EVIDENCE"
    assert any(item["code"] == "SOURCE_ARCHIVE_RECORD_INVALID" for item in report["findings"])


def test_source_archive_audit_reports_actual_incomplete_manifest_record_count(tmp_path, monkeypatch):
    root, _ = _fixture_source_bundle(tmp_path, monkeypatch)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["archived_files"].pop()
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setattr(
        verifier, "SOURCE_ARCHIVE_MANIFEST_SHA256", hashlib.sha256(manifest_bytes).hexdigest(),
    )

    report = verifier.audit_source_archive_directory(root)

    assert report["status"] == "FAILED_WITH_EVIDENCE"
    assert report["counts"]["archive_records_declared"] == 49
    assert any(item["code"] == "SOURCE_ARCHIVE_RECORD_COUNT_INVALID" for item in report["findings"])


def test_source_archive_audit_classifies_missing_bundle_as_blocked(tmp_path):
    report = verifier.audit_source_archive_directory(tmp_path / "unavailable-source")

    assert report["status"] == "BLOCKED_WITH_EVIDENCE"
    assert report["counts"]["archives_verified"] == 0
    assert str(tmp_path) not in json.dumps(report)


def test_source_archive_cli_rejects_report_inside_source_before_writing(tmp_path, monkeypatch):
    root, _ = _fixture_source_bundle(tmp_path, monkeypatch)
    report_path = root / "raw_cache" / "unsafe-report.json"

    result = verifier.main([
        "--source-archive-dir", str(root),
        "--source-archive-report", str(report_path),
    ])

    assert result == 2
    assert not report_path.exists()


def test_source_archive_cli_writes_new_redacted_report_and_returns_success(tmp_path, monkeypatch, capsys):
    root, _ = _fixture_source_bundle(tmp_path, monkeypatch)
    report_path = tmp_path / "reports" / "source-audit.json"

    result = verifier.main([
        "--source-archive-dir", str(root),
        "--source-archive-report", str(report_path),
    ])

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "VERIFIED"
    assert str(root) not in report_path.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"


def test_source_archive_cli_does_not_overwrite_report(tmp_path, monkeypatch):
    root, _ = _fixture_source_bundle(tmp_path, monkeypatch)
    report_path = tmp_path / "existing-report.json"
    report_path.write_text("keep this", encoding="utf-8")

    result = verifier.main([
        "--source-archive-dir", str(root),
        "--source-archive-report", str(report_path),
    ])

    assert result == 2
    assert report_path.read_text(encoding="utf-8") == "keep this"
