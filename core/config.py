"""Bounded local runtime configuration shared by the API and release tools.

The product is intentionally local-first.  This module keeps the defaults and
input limits in one small, dependency-free place so a launcher, health route,
and tests describe the same boundary without enabling background work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_VERSION = "1.2.1"
API_PHASE = 7
CURRENT_SCHEMA_VERSION = 13
DEFAULT_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_WEB_PORT = 4173
MAX_DATABASE_PATH_LENGTH = 512
MAX_CORS_ORIGINS = 16
APP_DATA_DIRECTORY_NAME = "AI Market Analyst"


class ConfigurationError(ValueError):
    """Raised when an environment value would leave the local safety boundary."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ConfigurationError(f"{name} must be a boolean")
    return normalized in {"1", "true", "yes", "on"}


def database_path_from_env() -> Path:
    """Return the configured database path with bounded, local path syntax.

    The application may use an absolute path chosen by the operator, but it
    never treats an empty value, a control-character path, or a symlinked
    database as an implicit data location.  Existing parent directories are
    checked as well; an operator can deliberately choose a new normal folder.
    """

    raw = os.environ.get("DATABASE_PATH")
    if raw is None:
        raw = str(app_data_paths().database)
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigurationError("DATABASE_PATH must be a non-empty local path")
    if len(raw) > MAX_DATABASE_PATH_LENGTH or any(ord(char) < 32 for char in raw):
        raise ConfigurationError("DATABASE_PATH is too long or contains control characters")
    if raw == ":memory:":
        return Path(raw)
    path = Path(os.path.abspath(os.path.expanduser(raw)))
    current = path
    existing_parts: list[Path] = []
    while True:
        if current.exists():
            existing_parts.append(current)
        if current == current.parent:
            break
        current = current.parent
    if any(item.is_symlink() for item in existing_parts):
        raise ConfigurationError("DATABASE_PATH and its existing parents must not be symlinks")
    resolved = path.resolve(strict=False)
    if resolved.name in {"", ".", ".."} or resolved.is_dir():
        raise ConfigurationError("DATABASE_PATH must name a database file")
    return resolved


@dataclass(frozen=True, slots=True)
class AppDataPaths:
    """The user-writable Windows application data boundary.

    The project checkout is used only for development assets and explicit
    legacy import.  Runtime data is kept outside Program Files so upgrades and
    uninstall do not silently destroy the user's local research ledger.
    """

    root: Path
    data: Path
    logs: Path
    backups: Path
    runtime: Path

    @property
    def database(self) -> Path:
        return self.data / "market_analyst.sqlite3"


def _validated_local_path(raw: str, *, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigurationError(f"{label} must be a non-empty local path")
    if len(raw) > MAX_DATABASE_PATH_LENGTH or any(ord(char) < 32 for char in raw):
        raise ConfigurationError(f"{label} is too long or contains control characters")
    candidate = Path(os.path.abspath(os.path.expanduser(raw)))
    current = candidate
    existing_parts: list[Path] = []
    while True:
        if current.exists():
            existing_parts.append(current)
        if current == current.parent:
            break
        current = current.parent
    if any(item.is_symlink() for item in existing_parts):
        raise ConfigurationError(f"{label} and its existing parents must not be symlinks")
    return candidate.resolve(strict=False)


def app_data_paths(*, create: bool = False) -> AppDataPaths:
    """Resolve the per-user runtime directories without reading frontend input."""

    configured = os.environ.get("AIMA_DATA_ROOT")
    if configured is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            local_app_data = str(Path.home() / "AppData" / "Local")
        configured = str(Path(local_app_data) / APP_DATA_DIRECTORY_NAME)
    root = _validated_local_path(configured, label="AIMA_DATA_ROOT")
    paths = AppDataPaths(
        root=root,
        data=root / "data",
        logs=root / "logs",
        backups=root / "backups",
        runtime=root / "runtime",
    )
    if create:
        for directory in (paths.root, paths.data, paths.logs, paths.backups, paths.runtime):
            directory.mkdir(parents=True, exist_ok=True)
    return paths


def redact_path(path: str | Path) -> str:
    """Return a privacy-safe path label for health and audit evidence."""

    value = Path(path)
    return value.name if value.name else "<database>"


def runtime_capabilities() -> dict[str, object]:
    return {
        "local_only": True,
        "cloud_required": False,
        "telemetry": False,
        "external_notifications": False,
        "broker_or_real_order": False,
        "private_keys": False,
        "scheduler_default_enabled": False,
        "model_analysis_concurrency": 1,
        "backup_format": "phase7_backup_v1",
        "app_data": "%LOCALAPPDATA%/AI Market Analyst/{data,logs,backups,runtime}",
        "explicit_legacy_import": True,
        "monitoring_default_enabled": False,
        "auto_start_default_enabled": False,
        "resume_monitoring_default_enabled": False,
        "owned_sidecar_only_shutdown": True,
        "config_paths_redacted": True,
    }
