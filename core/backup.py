"""Safe, local SQLite backup and restore artifacts for the V1.0 release.

The artifact is a directory containing a SQLite-consistent ``database.sqlite3``
and a small manifest.  Restore validates every byte and the SQLite integrity
check before it creates a safety backup or touches the target.  All cleanup is
file-scoped; this module deliberately has no recursive-delete path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import APP_VERSION, CURRENT_SCHEMA_VERSION
from .storage import SQLiteStore

BACKUP_FORMAT_VERSION = "phase7_backup_v1"
DATABASE_FILENAME = "database.sqlite3"
MANIFEST_FILENAME = "manifest.json"
_MAX_MANIFEST_BYTES = 1_048_576
_DATABASE_TABLES = (
    "instruments",
    "market_snapshots",
    "predictions",
    "paper_trades",
    "outcomes",
    "news_events",
    "provider_snapshots",
    "model_runs",
    "replay_runs",
    "replay_samples",
    "performance_snapshots",
    "calibration_results",
    "calibration_buckets",
    "watchlist_entries",
    "app_settings",
    "scheduler_runs",
    "scheduler_items",
    "scheduler_cache_entries",
    "alerts",
    "benchmark_metadata",
    "phase6_events",
    "phase6_event_clusters",
    "market_memory_features",
)


class BackupError(RuntimeError):
    """Raised when a backup artifact or restore target is unsafe or invalid."""


def _absolute(path: str | Path) -> Path:
    value = Path(path)
    if str(value) == ":memory:":
        raise BackupError("in-memory databases cannot be backed up")
    return Path(os.path.abspath(str(value)))


def _reject_symlink_components(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise BackupError(f"symlink path is not allowed: {path}")
        if current == current.parent:
            return
        current = current.parent


def _reject_broad_path(path: Path) -> None:
    resolved = path.resolve(strict=False)
    forbidden = {
        Path(resolved.anchor).resolve() if resolved.anchor else resolved,
        Path.home().resolve(),
        Path.cwd().resolve(),
        Path(tempfile.gettempdir()).resolve(),
    }
    if resolved in forbidden:
        raise BackupError(f"broad or reserved path is not allowed: {path}")
    if not resolved.name or resolved.name in {".", ".."}:
        raise BackupError(f"path must name a file or dedicated directory: {path}")


def _validate_database_path(path: str | Path, *, must_exist: bool) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    _reject_broad_path(candidate)
    if candidate.suffix.lower() not in {".sqlite3", ".sqlite", ".db"}:
        raise BackupError("database path must use .sqlite3, .sqlite, or .db")
    if must_exist and not candidate.is_file():
        raise BackupError(f"database file does not exist: {candidate}")
    if candidate.exists() and not candidate.is_file():
        raise BackupError(f"database path is not a regular file: {candidate}")
    if not candidate.parent.is_dir():
        raise BackupError(f"database parent directory does not exist: {candidate.parent}")
    return candidate


def _validate_directory(path: str | Path, *, must_exist: bool, empty: bool = False) -> Path:
    candidate = _absolute(path)
    _reject_symlink_components(candidate)
    _reject_broad_path(candidate)
    if must_exist and not candidate.is_dir():
        raise BackupError(f"directory does not exist: {candidate}")
    if candidate.exists() and not candidate.is_dir():
        raise BackupError(f"path is not a directory: {candidate}")
    if not candidate.exists():
        if not candidate.parent.is_dir():
            raise BackupError(f"directory parent does not exist: {candidate.parent}")
        candidate.mkdir()
    if empty and any(candidate.iterdir()):
        raise BackupError(f"backup destination must be empty: {candidate}")
    return candidate


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_sqlite_metadata(path: Path) -> tuple[int, dict[str, int]]:
    """Read integrity/schema/count evidence without running migrations."""

    try:
        uri = f"{path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"unable to open SQLite database read-only: {path}") from exc
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise BackupError(f"SQLite integrity check failed for {path}")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "schema_migrations" not in tables:
            raise BackupError("SQLite database has no schema_migrations table")
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        schema_version = int(row[0] or 0)
        if schema_version > CURRENT_SCHEMA_VERSION:
            raise BackupError(
                f"database schema {schema_version} is newer than supported schema {CURRENT_SCHEMA_VERSION}"
            )
        counts: dict[str, int] = {}
        for table in _DATABASE_TABLES:
            if table in tables:
                counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return schema_version, counts
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite metadata validation failed for {path}") from exc
    finally:
        connection.close()


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def backup_database(source: str | Path, destination: str | Path) -> dict[str, Any]:
    """Create a validated consistent backup artifact and return its manifest."""

    source_path = _validate_database_path(source, must_exist=True)
    destination_path = _validate_directory(destination, must_exist=False, empty=True)
    if _is_within(source_path, destination_path) or _is_within(destination_path, source_path):
        raise BackupError("backup destination cannot contain or be the database path")
    database_path = destination_path / DATABASE_FILENAME
    manifest_path = destination_path / MANIFEST_FILENAME
    temporary = destination_path / f".{DATABASE_FILENAME}.{uuid.uuid4().hex}.tmp"
    try:
        SQLiteStore(source_path).backup_to(temporary)
        schema_version, counts = _read_sqlite_metadata(temporary)
        digest = _sha256(temporary)
        created_at = datetime.now(timezone.utc).isoformat()
        manifest: dict[str, Any] = {
            "format_version": BACKUP_FORMAT_VERSION,
            "app_version": APP_VERSION,
            "schema_version": schema_version,
            "created_at": created_at,
            "database_filename": DATABASE_FILENAME,
            "database_sha256": digest,
            "source_name": source_path.name,
            "counts": counts,
        }
        _write_json_atomically(manifest_path, manifest)
        os.replace(temporary, database_path)
        return manifest
    except BackupError:
        if temporary.exists():
            temporary.unlink()
        if manifest_path.exists() and not database_path.exists():
            manifest_path.unlink()
        raise
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        if manifest_path.exists() and not database_path.exists():
            manifest_path.unlink()
        raise BackupError(f"SQLite backup failed: {exc.__class__.__name__}") from exc


def _load_manifest(artifact: Path) -> tuple[dict[str, Any], Path]:
    manifest_path = artifact / MANIFEST_FILENAME
    database_path = artifact / DATABASE_FILENAME
    if not manifest_path.is_file() or manifest_path.is_symlink() or not database_path.is_file() or database_path.is_symlink():
        raise BackupError("backup artifact must contain a regular manifest.json and database.sqlite3")
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise BackupError("backup manifest exceeds the bounded size limit")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BackupError("backup manifest must be a JSON object")
    if payload.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError("unsupported backup format version")
    if not isinstance(payload.get("app_version"), str) or not payload["app_version"].startswith(("1.", "2.")):
        raise BackupError("backup manifest has an incompatible application version")
    if payload.get("database_filename") != DATABASE_FILENAME:
        raise BackupError("backup manifest database filename is unsafe")
    digest = payload.get("database_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        raise BackupError("backup manifest checksum is invalid")
    if _sha256(database_path) != digest.lower():
        raise BackupError("backup database checksum does not match the manifest")
    schema_version, counts = _read_sqlite_metadata(database_path)
    declared_schema = payload.get("schema_version")
    if not isinstance(declared_schema, int) or declared_schema != schema_version:
        raise BackupError("backup manifest schema version does not match the database")
    declared_counts = payload.get("counts")
    if not isinstance(declared_counts, dict):
        raise BackupError("backup manifest counts are missing or invalid")
    try:
        normalized_counts = {str(key): int(value) for key, value in declared_counts.items()}
    except (TypeError, ValueError) as exc:
        raise BackupError("backup manifest counts are invalid") from exc
    if normalized_counts != counts:
        raise BackupError("backup manifest counts do not match the database")
    return payload, database_path


def _copy_and_validate(source: Path, destination: Path) -> None:
    shutil.copyfile(source, destination)
    with destination.open("r+b") as stream:
        os.fsync(stream.fileno())
    _read_sqlite_metadata(destination)


def _safety_destination(target: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return target.parent / f"{target.name}.pre-restore-{stamp}-{uuid.uuid4().hex[:8]}"


def _restore_failure_destination(target: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return target.parent / f"{target.name}.restore-failed-{stamp}-{uuid.uuid4().hex[:8]}"


def _assert_restore_target_offline(target: Path) -> None:
    """Fail closed when target WAL/SHM state could mix with a replacement."""

    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        _reject_symlink_components(sidecar)
        if sidecar.exists():
            raise BackupError(
                "restore requires an offline SQLite target; "
                f"active sidecar {sidecar.name} exists. Stop the owned launcher "
                "and close all database connections before retrying."
            )
    if not target.exists():
        return
    try:
        connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True, timeout=0.2)
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(
            "restore requires an offline SQLite target; journal mode could not be inspected"
        ) from exc
    try:
        mode_row = connection.execute("PRAGMA journal_mode").fetchone()
        mode = str(mode_row[0]).lower() if mode_row else "unknown"
    except sqlite3.Error as exc:
        raise BackupError(
            "restore requires an offline SQLite target; journal mode could not be inspected"
        ) from exc
    finally:
        connection.close()
    if mode == "wal":
        raise BackupError(
            "restore requires an offline SQLite target; WAL journal mode is active. "
            "Stop the owned launcher and close all database connections before retrying."
        )


def _quarantine_new_target(target: Path) -> Path | None:
    """Move only a newly created failed target, retaining forensic evidence."""

    quarantine = _restore_failure_destination(target)
    try:
        os.replace(target, quarantine)
        return quarantine
    except Exception:
        if target.is_file() and not target.is_symlink():
            try:
                target.unlink()
            except OSError:
                pass
        return None


def restore_database(artifact: str | Path, target: str | Path) -> dict[str, Any]:
    """Validate an artifact, preserve the target, then atomically restore it."""

    artifact_path = _validate_directory(artifact, must_exist=True)
    manifest, source_path = _load_manifest(artifact_path)
    target_path = _validate_database_path(target, must_exist=False)
    if _is_within(target_path, artifact_path):
        raise BackupError("restore target cannot be inside the backup artifact")
    if target_path.exists() and target_path.is_symlink():
        raise BackupError("restore target may not be a symlink")
    _assert_restore_target_offline(target_path)
    target_existed = target_path.exists()
    safety_path: Path | None = None
    if target_existed:
        safety_path = _safety_destination(target_path)
        backup_database(target_path, safety_path)
    temporary = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.restore")
    replaced = False
    try:
        _copy_and_validate(source_path, temporary)
        os.replace(temporary, target_path)
        replaced = True
        _read_sqlite_metadata(target_path)
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        if replaced and safety_path is not None:
            recovery_stage = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.recover")
            try:
                _copy_and_validate(safety_path / DATABASE_FILENAME, recovery_stage)
                os.replace(recovery_stage, target_path)
            except Exception as recovery_exc:
                if recovery_stage.exists():
                    recovery_stage.unlink()
                raise BackupError(
                    f"restore failed and safety recovery failed; safety artifact remains at {safety_path}"
                ) from recovery_exc
        if replaced and not target_existed:
            quarantine_path = _quarantine_new_target(target_path)
            if quarantine_path is not None:
                raise BackupError(
                    "restore failed after replacing a previously absent target; "
                    f"failed database quarantined at {quarantine_path.name}"
                ) from exc
            raise BackupError(
                "restore failed after replacing a previously absent target; "
                "the exact failed target could not be quarantined or removed"
            ) from exc
        safety_note = f"; safety backup: {safety_path}" if safety_path is not None else ""
        raise BackupError(f"restore failed before completion{ safety_note}") from exc
    return {
        "restored": True,
        "target": target_path.name,
        "manifest": manifest,
        "safety_backup": str(safety_path) if safety_path is not None else None,
    }


def read_database_counts(path: str | Path) -> dict[str, int]:
    """Return table counts after a read-only integrity validation."""

    _schema_version, counts = _read_sqlite_metadata(_validate_database_path(path, must_exist=True))
    return counts
