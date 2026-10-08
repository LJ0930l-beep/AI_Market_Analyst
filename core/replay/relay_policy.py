"""Freeze non-secret Antigravity inference settings; never expose credentials."""
import json
from pathlib import Path
from core.replay.ai_history import digest


DEFAULT_RELAY_CONFIG = Path.home() / ".antigravity_tools/gui_config.json"


def read_relay_policy(path=DEFAULT_RELAY_CONFIG):
    try:
        proxy = json.loads(Path(path).read_text(encoding="utf-8"))["proxy"]
        budget = proxy["thinking_budget"]
        return {"schema_version": "antigravity_inference_policy_v1",
            "thinking": {key: budget[key] for key in (
                "control_source", "flash_mode", "flash_low", "flash_medium", "flash_high", "flash_tiered")},
            "request_timeout": proxy["request_timeout"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("RESEARCH_RELAY_POLICY_UNAVAILABLE") from exc


def relay_policy_guard(expected, before_call, path=DEFAULT_RELAY_CONFIG):
    expected_hash = digest(expected)
    def check():
        from core.replay.ai_template_runner import ReplayPaused
        try:
            current_hash = digest(read_relay_policy(path))
        except ValueError as exc:
            raise ReplayPaused("RESEARCH_RELAY_POLICY_UNAVAILABLE") from exc
        if current_hash != expected_hash:
            raise ReplayPaused("RELAY_INFERENCE_POLICY_DRIFT_REQUIRES_NEW_EXPERIMENT")
        before_call()
    return check
