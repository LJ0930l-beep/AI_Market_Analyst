"""Deterministic V1.1 routing for the two local Qwen tiers.

The model never chooses its own tier.  This module owns the small, auditable
policy used by consultation and explicit AI artifacts.  Model identifiers are
server configuration and are never accepted from browser payloads.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

MODEL_ROUTING_VERSION = "qwen_route_v1"
DEFAULT_FAST_MODEL = "qwen3.5:4b"
DEFAULT_SMART_MODEL = "qwen3.5:9b"


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

    @classmethod
    def from_env(cls) -> "ModelRoutingConfig":
        return cls(
            fast_model=_model_from_env("FAST_MODEL", DEFAULT_FAST_MODEL),
            smart_model=_model_from_env("SMART_MODEL", DEFAULT_SMART_MODEL),
        )

    def capability(self) -> dict[str, object]:
        return {
            "version": MODEL_ROUTING_VERSION,
            "owner": "python",
            "fast_model": self.fast_model,
            "smart_model": self.smart_model,
            "client_model_override": False,
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
