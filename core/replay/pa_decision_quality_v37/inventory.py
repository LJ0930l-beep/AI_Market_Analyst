"""Privacy-preserving inventory of local historical research sources."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .evidence_builder import open_sqlite_readonly, sha256_file

IDENTITY_KEYS = {
    "experiment_id", "model_id", "actual_model_id", "requested_model_id",
    "prompt", "original_prompt", "model_input", "state_snapshot",
    "prompt_version", "analysis_schema_version", "schema_version",
    "prompt_sha256", "prompt_hash", "data_sha256", "input_hash",
    "state_sha256", "state_snapshot_sha256", "evidence_refs",
}
PRIVATE_TABLES = {
    "gate_remote_account_snapshots", "gate_trades", "gate_orders",
    "gate_order_intents", "trade_fills", "simulated_positions",
    "gate_testnet_e2e_runs",
}
PILOT_DIRECTORY_NAMES = (
    "gemini-year-research-v25-recovery-2000-20261007",
    "gemini-year-research-v31-known-retest-2000-20261007",
    "gemini-year-research-v32-regime-news-2000-20261007",
    "gemini-year-research-v33-main15-entry5-2000-20261008",
)
CODE_SOURCE_PATHS = (
    "core/replay/gemini_research.py",
    "core/replay/ai_template_runner.py",
    "core/replay/ai_history.py",
    "scripts/run_gemini_year_research.py",
    "scripts/export_gemini_response_files.py",
    "scripts/run_pa_decision_quality_v36.py",
    "scripts/review_pa_decision_quality_v36.py",
)


def _key_presence(value: Any, found: Counter[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in IDENTITY_KEYS:
                found[str(key)] += 1
            _key_presence(item, found)
    elif isinstance(value, list):
        for item in value:
            _key_presence(item, found)


def _database_summary(path: Path, relative_to: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": path.resolve().relative_to(relative_to.resolve()).as_posix()
        if path.resolve().is_relative_to(relative_to.resolve()) else str(path.resolve()),
        "exists": path.is_file(),
        "sensitivity": "PRIVATE_LOCAL_DATABASE_NOT_FOR_PUBLIC_UPLOAD",
    }
    if not path.is_file():
        return record
    record.update({"bytes": path.stat().st_size, "sha256": sha256_file(path)})
    db = open_sqlite_readonly(path)
    try:
        tables = sorted(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))
        record["table_count"] = len(tables)
        record["sensitive_tables_present"] = sorted(PRIVATE_TABLES.intersection(tables))
        table = "ai_template_replay_decisions"
        if table in tables:
            columns = {row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')}
            summary: dict[str, Any] = {"row_count": int(db.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0])}
            if {"status", "as_of"}.issubset(columns):
                summary["status_windows"] = [dict(row) for row in db.execute(
                    f'SELECT status,COUNT(*) AS rows,MIN(as_of) AS first_as_of,MAX(as_of) AS last_as_of '
                    f'FROM "{table}" GROUP BY status ORDER BY status'
                )]
            json_columns = [column for column in ("decision_json", "context_json", "result_json")
                            if column in columns]
            field_presence: Counter[str] = Counter()
            selected = ",".join('"' + column + '"' for column in json_columns)
            if selected:
                for row in db.execute(f'SELECT {selected} FROM "{table}"'):
                    for column in json_columns:
                        raw = row[column]
                        if not isinstance(raw, str):
                            continue
                        try:
                            value = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        _key_presence(value, field_presence)
            summary["identity_key_occurrences"] = dict(sorted(field_presence.items()))
            summary["identity_key_values_exposed"] = False
            record["replay_decision_table"] = summary
    except sqlite3.Error as exc:
        record["metadata_error"] = type(exc).__name__
    finally:
        db.close()
    return record


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def build_source_inventory(project_root: Path, local_data_root: Path) -> dict[str, Any]:
    """Inspect source presence and metadata without emitting raw prompt/account values."""
    project_root = Path(project_root).resolve()
    local_data_root = Path(local_data_root).resolve()
    code_sources = []
    for relative in CODE_SOURCE_PATHS:
        path = project_root / Path(relative)
        code_sources.append({
            "path": relative,
            "exists": path.is_file(),
            "sha256": sha256_file(path) if path.is_file() else None,
            "execution_performed": False,
        })

    sample_path = project_root / "docs/research/v25-price-action-sample-20261008.json"
    v25_sample: dict[str, Any] = {"path": "docs/research/v25-price-action-sample-20261008.json",
                                  "exists": sample_path.is_file()}
    if sample_path.is_file():
        sample = _read_json(sample_path) or {}
        scans = sample.get("scans") if isinstance(sample.get("scans"), list) else []
        closed = sample.get("closed_trades") if isinstance(sample.get("closed_trades"), list) else []
        action_counts = sample.get("action_counts") if isinstance(sample.get("action_counts"), dict) else {}
        row_key_counts: Counter[str] = Counter()
        for row in scans:
            if isinstance(row, dict):
                for key in row:
                    row_key_counts[str(key)] += 1
        v25_sample.update({
            "bytes": sample_path.stat().st_size,
            "sha256": sha256_file(sample_path),
            "scan_count": len(scans),
            "closed_trade_summary_count": len(closed),
            "action_counts": {str(key): int(value) for key, value in sorted(action_counts.items())
                              if isinstance(value, int)},
            "scan_field_presence_counts": dict(sorted(row_key_counts.items())),
            "original_prompt_or_response_text_present": False,
            "sensitivity": "TRACKED_SANITIZED_SUMMARY",
        })

    local_databases = []
    for directory_name in PILOT_DIRECTORY_NAMES:
        folder = local_data_root / "reports" / directory_name
        if not folder.is_dir():
            local_databases.append({"path": f"reports/{directory_name}", "exists": False})
            continue
        for path in sorted(folder.glob("pilot-*/results.sqlite3")):
            local_databases.append(_database_summary(path, local_data_root))
    main_database = _database_summary(local_data_root / "data/market_analyst.sqlite3", local_data_root)
    if main_database.get("exists"):
        db = open_sqlite_readonly(local_data_root / "data/market_analyst.sqlite3")
        try:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "market_bar_versions" in tables:
                columns = {row["name"] for row in db.execute("PRAGMA table_info(market_bar_versions)")}
                needed = {"source", "timeframe", "quality_status", "bar_start"}
                if needed.issubset(columns):
                    main_database["market_bar_coverage"] = [dict(row) for row in db.execute(
                        "SELECT source,timeframe,quality_status,COUNT(*) AS rows,"
                        "MIN(bar_start) AS first_bar_start,MAX(bar_start) AS last_bar_start "
                        "FROM market_bar_versions GROUP BY source,timeframe,quality_status "
                        "ORDER BY source,timeframe,quality_status"
                    )]
        finally:
            db.close()

    proxy_directory = local_data_root / "reports/btc-eth-year-proxy-20261004"
    proxy_manifest_path = proxy_directory / "manifest.json"
    proxy: dict[str, Any] = {"path": "reports/btc-eth-year-proxy-20261004", "exists": proxy_directory.is_dir()}
    manifest = _read_json(proxy_manifest_path)
    if manifest is not None:
        proxy.update({
            "manifest_sha256": sha256_file(proxy_manifest_path),
            "database_sha256": sha256_file(proxy_directory / "research.sqlite3"),
            "manifest_dataset_sha256_matches_database": (
                manifest.get("dataset_sha256") == sha256_file(proxy_directory / "research.sqlite3")
            ),
            "exchange": manifest.get("exchange"),
            "source_type": manifest.get("source_type"),
            "not_gate_data": manifest.get("not_gate_data") is True,
            "research_only": manifest.get("research_only") is True,
            "window_start": manifest.get("window_start"),
            "window_end": manifest.get("window_end"),
            "warmup_start": manifest.get("warmup_start"),
            "symbols": manifest.get("symbols"),
        })
        archives = manifest.get("archived_files") if isinstance(manifest.get("archived_files"), list) else []
        verified_archives = []
        for item in archives:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            name = Path(urlsplit(url).path).name if isinstance(url, str) else ""
            archive_path = proxy_directory / "raw_cache" / name
            exists = archive_path.is_file()
            actual = sha256_file(archive_path) if exists else None
            verified_archives.append({
                "symbol": item.get("symbol"),
                "kind": item.get("kind"),
                "month": item.get("month"),
                "path": f"raw_cache/{name}" if name else None,
                "bytes": archive_path.stat().st_size if exists else None,
                "sha256": actual,
                "manifest_sha256": item.get("sha256"),
                "official_checksum_verified_in_manifest": item.get("official_checksum_verified") is True,
                "file_matches_manifest": bool(exists and actual == item.get("sha256")
                                               and archive_path.stat().st_size == item.get("bytes")),
                "url": url,
            })
        proxy["archived_files"] = verified_archives
        proxy["all_archived_files_verified"] = bool(verified_archives) and all(
            item["file_matches_manifest"] and item["official_checksum_verified_in_manifest"]
            for item in verified_archives
        )
        proxy["available_at_is_historical_observation"] = False

    audit_path = proxy_directory / "independent-proxy-audit.json"
    audit = _read_json(audit_path)
    if audit is not None:
        proxy["independent_proxy_audit"] = {
            "path": "reports/btc-eth-year-proxy-20261004/independent-proxy-audit.json",
            "sha256": sha256_file(audit_path),
            "status": audit.get("status"),
            "analysis_type": audit.get("analysis_type"),
            "archives_rechecked": audit.get("archives_rechecked"),
            "failures_count": len(audit.get("failures", [])) if isinstance(audit.get("failures"), list) else None,
        }

    v33_directory = project_root / "evidence/gemini-price-action-v33-first50-20261008"
    v33_manifest_path = v33_directory / "manifest.json"
    v33_manifest = _read_json(v33_manifest_path)
    v33_archive: dict[str, Any] = {"path": "evidence/gemini-price-action-v33-first50-20261008",
                                  "exists": v33_directory.is_dir(),
                                  "is_v25_evidence": False}
    if v33_manifest is not None:
        file_entries = v33_manifest.get("files", v33_manifest.get("entries", []))
        v33_archive.update({
            "manifest_sha256": sha256_file(v33_manifest_path),
            "manifest_top_level_keys": sorted(v33_manifest),
            "manifest_file_entry_count": len(file_entries) if isinstance(file_entries, list) else None,
        })
    v33_readme = v33_directory / "README.md"
    if v33_readme.is_file():
        v33_archive["readme_sha256"] = sha256_file(v33_readme)

    return {
        "schema_version": "pa-decision-quality-v37/source-inventory-1",
        "inventory_method": "READ_ONLY_METADATA_AND_REDACTED_KEY_PRESENCE_ONLY",
        "raw_prompt_values_emitted": False,
        "account_or_order_values_emitted": False,
        "source_data_modified": False,
        "code_sources": code_sources,
        "tracked_v25_sample": v25_sample,
        "local_research_databases": local_databases,
        "local_market_database": main_database,
        "binance_proxy_archive": proxy,
        "tracked_v33_archive": v33_archive,
        "classification_legend": {
            "EXACT_OBSERVED": "Original time-stamped source is verifiable.",
            "VERIFIED_ARCHIVE_RECONSTRUCTION": "Rebuilt from verified historical market archive; not the market feed actually seen at the past decision time.",
            "ASSUMED_PROXY": "Depends on an explicit assumption such as bar availability delay.",
            "UNAVAILABLE": "Required historical evidence cannot be verified from inspected local sources.",
        },
    }
