"""Windows desktop runtime boundaries for the packaged V1.2 application.

The Tauri shell owns the sidecar process.  This module owns only the data
location, explicit legacy import, and the portable ownership fingerprint used
by tests and release diagnostics.  It deliberately has no process-name based
kill or arbitrary frontend command execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppDataPaths, ConfigurationError, app_data_paths
from .storage import SQLiteStore


LEGACY_DATABASE_RELATIVE_PATH = Path("data") / "market_analyst.sqlite3"
OWNERSHIP_VERSION = "owned_sidecar_v1"


@dataclass(frozen=True, slots=True)
class LegacyImportReport:
    requested: bool
    imported: bool
    source: str
    destination: str
    reason: str
    schema_version: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "imported": self.imported,
            "source": self.source,
            "destination": self.destination,
            "reason": self.reason,
            "schema_version": self.schema_version,
        }


def ensure_app_data_layout() -> AppDataPaths:
    """Create the user-writable directories used by a packaged desktop run."""

    return app_data_paths(create=True)


def _safe_existing_file(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise ConfigurationError(f"{label} must not be a symlink")
    _safe_parent_directory(path, label=label)
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise ConfigurationError(f"{label} does not exist") from None
    if not resolved.is_file() or resolved.is_symlink():
        raise ConfigurationError(f"{label} must be a regular file")
    return resolved


def _safe_parent_directory(path: Path, *, label: str) -> Path:
    """Reject symlinked existing parents before creating an import target."""

    current = path.parent
    existing: list[Path] = []
    while True:
        if current.exists():
            existing.append(current)
        if current == current.parent:
            break
        current = current.parent
    if any(item.is_symlink() for item in existing):
        raise ConfigurationError(f"{label} parents must not be symlinks")
    return path.parent.resolve(strict=False)


def import_legacy_database(
    *,
    source: str | Path | None = None,
    destination: str | Path | None = None,
) -> LegacyImportReport:
    """Import the V1.1 checkout database only when the user explicitly asks.

    A pre-existing destination is never overwritten.  The SQLite backup API is
    used so the source remains untouched and the normal V1.2 migration runs on
    the imported copy.
    """

    paths = ensure_app_data_layout()
    source_path = Path(source) if source is not None else Path.cwd() / LEGACY_DATABASE_RELATIVE_PATH
    destination_path = Path(destination) if destination is not None else paths.database
    source_label = source_path.name
    destination_label = destination_path.name
    if destination_path.exists():
        _safe_existing_file(destination_path, label="destination database")
        store = SQLiteStore(destination_path)
        store.initialize()
        return LegacyImportReport(True, False, source_label, destination_label, "destination_exists", store.schema_version())
    try:
        source_file = _safe_existing_file(source_path, label="legacy database")
    except ConfigurationError:
        return LegacyImportReport(True, False, source_label, destination_label, "source_not_found")
    safe_parent = _safe_parent_directory(destination_path, label="destination database")
    safe_parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(str(source_file))
    target_db = sqlite3.connect(str(destination_path))
    try:
        source_db.backup(target_db)
        target_db.commit()
    finally:
        target_db.close()
        source_db.close()
    store = SQLiteStore(destination_path)
    store.initialize()
    return LegacyImportReport(True, True, source_label, destination_label, "imported", store.schema_version())


def ownership_fingerprint(
    *,
    pid: int,
    executable: str | Path,
    started_at: datetime,
    command_line: str,
) -> dict[str, object]:
    """Build a stable, privacy-safe ownership record for the owned sidecar."""

    if pid <= 0 or not command_line.strip():
        raise ValueError("owned sidecar identity is incomplete")
    executable_path = Path(executable).resolve(strict=False)
    start = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=timezone.utc)
    normalized_command = " ".join(command_line.split())
    return {
        "version": OWNERSHIP_VERSION,
        "pid": int(pid),
        "executable": str(executable_path),
        "started_at_utc": start.astimezone(timezone.utc).isoformat(),
        "command_line_sha256": hashlib.sha256(normalized_command.encode("utf-8")).hexdigest(),
    }


def verify_ownership(record: dict[str, object], *, pid: int, executable: str | Path, started_at: datetime, command_line: str) -> bool:
    """Fail closed unless every recorded sidecar identity component matches."""

    try:
        recorded_pid = int(record.get("pid", -1))
    except (TypeError, ValueError):
        return False
    if record.get("version") != OWNERSHIP_VERSION or recorded_pid != pid:
        return False
    expected_executable = Path(str(record.get("executable", ""))).resolve(strict=False)
    actual_executable = Path(executable).resolve(strict=False)
    if os.path.normcase(str(expected_executable)) != os.path.normcase(str(actual_executable)):
        return False
    expected_start = str(record.get("started_at_utc", ""))
    actual_start = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=timezone.utc)
    if expected_start != actual_start.astimezone(timezone.utc).isoformat():
        return False
    normalized_command = " ".join(command_line.split())
    actual_hash = hashlib.sha256(normalized_command.encode("utf-8")).hexdigest()
    return actual_hash == str(record.get("command_line_sha256", "")).lower()


def write_runtime_manifest(paths: AppDataPaths, *, sidecar: dict[str, object], port: int) -> Path:
    """Write a bounded diagnostic manifest without credentials or raw paths."""

    if not 1024 <= int(port) <= 65_535:
        raise ValueError("sidecar port is outside the local range")
    manifest = {
        "version": "desktop_runtime_v1",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "port": int(port),
        "sidecar": sidecar,
        "monitoring_default_enabled": False,
        "auto_start_default_enabled": False,
        "resume_monitoring_default_enabled": False,
    }
    path = paths.runtime / "runtime.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = [
    "LEGACY_DATABASE_RELATIVE_PATH",
    "LegacyImportReport",
    "OWNERSHIP_VERSION",
    "ensure_app_data_layout",
    "import_legacy_database",
    "ownership_fingerprint",
    "verify_ownership",
    "write_runtime_manifest",
]
