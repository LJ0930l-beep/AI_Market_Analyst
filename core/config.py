"""Local application configuration with Gemini 3.8 Flash via Antigravity Tools.

The API and credential storage remain local. Inference uses the operator's
Google account through the authenticated loopback relay; cloud access is required.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

APP_VERSION = "2.0.0"
API_PHASE = 7
CURRENT_SCHEMA_VERSION = 14
DEFAULT_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
DEFAULT_WEB_PORT = 4173
MAX_DATABASE_PATH_LENGTH = 512
MAX_CORS_ORIGINS = 16
APP_DATA_DIRECTORY_NAME = "AI Market Analyst"

# Model & Server Endpoints
DEFAULT_MODEL_NAME = "gemini-3.8-flash-high"
DEFAULT_BASE_URL = "http://127.0.0.1:8045/v1"
DEFAULT_TIMEOUT_SEC = 180.0
DEFAULT_CONTEXT_LENGTH = 8192  # Safe default for RTX 4060 8GB


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
        "local_only": False,
        "cloud_required": True,
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


@dataclass(frozen=True)
class GenerationPreset:
    mode: str
    thinking_enabled: bool
    temperature: float
    top_p: float
    top_k: int
    max_tokens: int
    description: str


# MODE A: Deep market analysis, research, strategy deduction (Thinking Enabled)
PRESET_ANALYSIS = GenerationPreset(
    mode="ANALYSIS",
    thinking_enabled=True,
    temperature=1.0,
    top_p=0.95,
    top_k=20,
    max_tokens=4096,
    description="Market analysis, strategy deduction, news catalyst evaluation, complex regime synthesis",
)

# MODE B: Fast data formatting, structured extraction, JSON repair (Thinking Disabled)
PRESET_FAST = GenerationPreset(
    mode="FAST",
    thinking_enabled=False,
    temperature=0.7,
    top_p=0.8,
    top_k=20,
    max_tokens=2048,
    description="Fast format translation, data structuring, metric categorization, strict JSON outputs",
)


@dataclass
class ModelConfig:
    model_name: str = field(default_factory=lambda: os.environ.get("AIMA_MODEL_NAME", DEFAULT_MODEL_NAME))
    base_url: str = field(default_factory=lambda: os.environ.get("AIMA_MODEL_BASE_URL", DEFAULT_BASE_URL).rstrip("/"))
    api_key: str = field(default_factory=lambda: relay_api_key(), repr=False)
    timeout_sec: float = field(default_factory=lambda: float(os.environ.get("AIMA_MODEL_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)))
    context_length: int = field(default_factory=lambda: int(os.environ.get("AIMA_MODEL_INPUT_BUDGET", DEFAULT_CONTEXT_LENGTH)))
    host: str = "127.0.0.1"
    port: int = field(default_factory=lambda: int(os.environ.get("AIMA_MODEL_PORT", "8045")))

    # Preset registry
    presets: Dict[str, GenerationPreset] = field(default_factory=lambda: {
        "ANALYSIS": PRESET_ANALYSIS,
        "FAST": PRESET_FAST,
    })

    def get_preset(self, mode: str | None = None) -> GenerationPreset:
        if not mode:
            return self.presets["ANALYSIS"]
        normalized = mode.strip().upper()
        return self.presets.get(normalized, self.presets["ANALYSIS"])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_name": self.model_name,
            "base_url": self.base_url,
            "timeout_sec": self.timeout_sec,
            "context_length": self.context_length,
            "host": self.host,
            "port": self.port,
        }


def relay_api_key() -> str:
    """Read the operator's local relay key without copying it into source/builds."""
    override = os.environ.get("AIMA_MODEL_API_KEY")
    if override:
        return override
    try:
        path = Path.home() / ".antigravity_tools" / "gui_config.json"
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        value = payload.get("proxy", {}).get("api_key")
        return value if isinstance(value, str) else ""
    except (OSError, ValueError, AttributeError):
        return ""


# Global singleton instance
config = ModelConfig()
