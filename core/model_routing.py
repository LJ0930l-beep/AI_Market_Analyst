"""Single-model routing for the local Bonsai 2 27B runtime.

The model never chooses its own tier. Fast and Smart are workload labels only;
both resolve to the one verified Bonsai model. Legacy environment overrides
are rejected and exposed as configuration warnings, never used to substitute
another installed model.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

MODEL_ROUTING_VERSION = "bonsai_route_v2"
DEFAULT_FAST_MODEL = "Bonsai-2-27B-PTQ1_0"
DEFAULT_SMART_MODEL = "Bonsai-2-27B-PTQ1_0"
DEFAULT_MODEL = DEFAULT_SMART_MODEL


def is_bonsai_model_identity(value: object) -> bool:
    """Match only the known Bonsai request alias or its exact GGUF manifest id.

    The local llama-server currently reports the artifact basename
    ``Ternary-Bonsai-2-27B-PTQ1_0.gguf``. Strip only that known prefix and the
    extension. Substring matching is deliberately forbidden because it can
    misclassify unrelated artifacts such as ``Other-Bonsai-2-27B...``.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    basename = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if basename.lower().endswith(".gguf"):
        basename = basename[:-5]
    if basename.lower().startswith("ternary-"):
        basename = basename[len("ternary-"):]
    return basename.casefold() == DEFAULT_MODEL.casefold()


def bonsai_manifest_entry_matches(model: dict[str, object], *, requested_model: str = DEFAULT_MODEL) -> bool:
    """Accept a manifest row only when its identity fields resolve exactly.

    If a row has a primary id/name/model that contradicts Bonsai, a matching
    alias cannot hide that contradiction.
    """
    if requested_model != DEFAULT_MODEL or not isinstance(model, dict):
        return False
    primary = [
        str(model.get(field)).strip()
        for field in ("id", "name", "model")
        if isinstance(model.get(field), str) and str(model.get(field)).strip()
    ]
    if primary and not all(is_bonsai_model_identity(item) for item in primary):
        return False
    aliases = model.get("aliases")
    alias_values = [str(item).strip() for item in aliases if isinstance(item, str) and item.strip()] if isinstance(aliases, list) else []
    return any(is_bonsai_model_identity(item) for item in (primary + alias_values))


def _bonsai_artifact_identity(value: object) -> str | None:
    """Return the canonical id only for an artifact identity, never an alias."""
    if not is_bonsai_model_identity(value) or not isinstance(value, str):
        return None
    basename = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if basename.lower().endswith(".gguf"):
        basename = basename[:-5]
    if basename.casefold() == DEFAULT_MODEL.casefold():
        return None
    if basename.lower().startswith("ternary-"):
        basename = basename[len("ternary-"):]
    return basename.casefold()


def is_verified_bonsai_receipt(
    receipt: object,
    *,
    expected_prompt_version: str | None = None,
    expected_input_hash: str | None = None,
) -> bool:
    """Validate a real inference receipt without trusting a display label.

    Every receipt must carry the exact request alias plus a verified manifest
    artifact. Trading callers additionally supply the prompt and input hash so
    a stale, incomplete, or unrelated receipt cannot authorize a decision.
    """
    if not isinstance(receipt, dict) or receipt.get("model_id") != DEFAULT_MODEL:
        return False
    source = receipt.get("model_identity_source")
    if source not in ("completion_response", "request_bound_to_verified_manifest"):
        return False
    actual = receipt.get("actual_model_id")
    manifest_identity = _bonsai_artifact_identity(receipt.get("verified_manifest_model_id"))
    if manifest_identity is None or not is_bonsai_model_identity(actual):
        return False
    model_version = receipt.get("model_version")
    if model_version is not None and not is_bonsai_model_identity(model_version):
        return False
    actual_identity = _bonsai_artifact_identity(actual)
    if actual_identity is None or actual_identity != manifest_identity:
        return False
    if model_version is not None:
        version_identity = _bonsai_artifact_identity(model_version)
        if version_identity is not None and version_identity != manifest_identity:
            return False
    if (expected_prompt_version is None) != (expected_input_hash is None):
        return False
    if expected_prompt_version is not None:
        prompt_version = receipt.get("prompt_version")
        input_hash = receipt.get("input_hash")
        if (
            not isinstance(expected_prompt_version, str)
            or not expected_prompt_version.strip()
            or prompt_version != expected_prompt_version
            or not isinstance(input_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", input_hash) is None
            or input_hash != expected_input_hash
            or receipt.get("parse_status") != "valid"
            or not isinstance(receipt.get("model_version"), str)
        ):
            return False
    return True


def is_trusted_bonsai_provider(provider: object) -> bool:
    """Allow only the application's unmodified Bonsai adapter on its pinned route.

    The exact type check intentionally rejects test doubles, wrappers, and
    third-party providers that can simply return Bonsai-looking metadata.
    """
    if provider is None:
        return False
    # Lazy import avoids coupling the routing constants to provider startup.
    from .ai.ollama import OllamaProvider

    if type(provider) is not OllamaProvider:
        return False
    if provider.model_name != DEFAULT_MODEL:
        return False
    if getattr(getattr(provider, "generate_json", None), "__func__", None) is not OllamaProvider.generate_json:
        return False
    return provider._route_error(DEFAULT_MODEL) is None


class ModelTier(str, Enum):
    FAST = "fast"
    SMART = "smart"


class ModelPreference(str, Enum):
    AUTO = "auto"
    FAST = "fast"
    SMART = "smart"


class ModelTask(str, Enum):
    WATCHLIST_SCAN = "watchlist_scan"
    SIGNAL_TRIGGER = "signal_trigger"
    SIMPLE_SUMMARY = "simple_summary"
    ASSISTANT = "assistant"
    SIGNAL_EXPLANATION = "signal_explanation"
    DAILY_BRIEF = "daily_brief"
    EVENT_ANALYSIS = "event_analysis"
    DEEP_RESEARCH = "deep_research"


FAST_TASKS = frozenset(
    {ModelTask.WATCHLIST_SCAN, ModelTask.SIGNAL_TRIGGER, ModelTask.SIMPLE_SUMMARY}
)


def _model_from_env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ModelRoutingConfig:
    fast_model: str = DEFAULT_FAST_MODEL
    smart_model: str = DEFAULT_SMART_MODEL
    rejected_overrides: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.fast_model != DEFAULT_FAST_MODEL or self.smart_model != DEFAULT_SMART_MODEL:
            raise ValueError("AI model routing is pinned to Bonsai-2-27B-PTQ1_0")

    @classmethod
    def from_env(cls) -> "ModelRoutingConfig":
        rejected: list[str] = []
        for name in ("FAST_MODEL", "SMART_MODEL"):
            value = os.environ.get(name)
            if value is not None and value.strip() != DEFAULT_MODEL:
                rejected.append(name)
        return cls(rejected_overrides=tuple(rejected))

    def capability(self) -> dict[str, object]:
        return {
            "version": MODEL_ROUTING_VERSION,
            "owner": "python",
            "fast_model": self.fast_model,
            "smart_model": self.smart_model,
            "client_model_override": False,
            "configuration_status": "OVERRIDES_REJECTED" if self.rejected_overrides else "PINNED",
            "rejected_overrides": list(self.rejected_overrides),
            "auto_policy": "task_to_tier",
            "fallback_policy": "no_fabricated_answer; explicit tier never silently changes",
        }


@dataclass(frozen=True, slots=True)
class ModelRoute:
    model_id: str
    tier: ModelTier
    preference: ModelPreference
    task: ModelTask
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "version": MODEL_ROUTING_VERSION,
            "model_id": self.model_id,
            "tier": self.tier.value,
            "preference": self.preference.value,
            "task": self.task.value,
            "reason": self.reason,
        }


def route_model(
    config: ModelRoutingConfig,
    *,
    preference: ModelPreference | str,
    task: ModelTask | str,
) -> ModelRoute:
    selected_preference = ModelPreference(preference)
    selected_task = ModelTask(task)
    if selected_preference is ModelPreference.FAST:
        tier = ModelTier.FAST
        reason = "explicit_fast_preference"
    elif selected_preference is ModelPreference.SMART:
        tier = ModelTier.SMART
        reason = "explicit_smart_preference"
    elif selected_task in FAST_TASKS:
        tier = ModelTier.FAST
        reason = "auto_fast_for_bounded_high_frequency_task"
    else:
        tier = ModelTier.SMART
        reason = "auto_smart_for_deep_research_task"
    return ModelRoute(
        model_id=config.fast_model if tier is ModelTier.FAST else config.smart_model,
        tier=tier,
        preference=selected_preference,
        task=selected_task,
        reason=reason,
    )
