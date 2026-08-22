"""Bounded local runtime configuration shared by the API and release tools.

The product is intentionally local-first.  This module keeps the defaults and
input limits in one small, dependency-free place so a launcher, health route,
and tests describe the same boundary without enabling background work.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_VERSION = "1.1.0"
API_PHASE = 7
CURRENT_SCHEMA_VERSION = 11
DEFAULT_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_WEB_PORT = 4173
MAX_DATABASE_PATH_LENGTH = 512
MAX_CORS_ORIGINS = 16


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

    raw = os.environ.get("DATABASE_PATH", "data/market_analyst.sqlite3")
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
        "config_paths_redacted": True,
    }
