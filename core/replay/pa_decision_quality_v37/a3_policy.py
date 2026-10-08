"""Immutable, outcome-independent A3 freshness and quote-entry policy."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

A3_POLICY_PATH = (
    Path(__file__).resolve().parents[3]
    / "configs" / "research" / "rules" / "a3-failed-breakout-freshness-v2.json"
)
A3_POLICY_SHA256 = "6d88b00b8fe6e37bb51efc7aa820542db7cc8c9f753837ebf4416707af8736e1"


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_a3_signal_policy(path: Path | None = None) -> tuple[dict[str, Any], str]:
    source_path = path or A3_POLICY_PATH
    try:
        policy = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("V37_A3_POLICY_UNAVAILABLE") from exc
    if not isinstance(policy, dict) or (
        policy.get("schema_version") != "aima-a3-signal-policy-v2"
        or policy.get("rule_id") != "FAILED_BREAKOUT_20X15M_5M_REENTRY_FRESH_V2"
    ):
        raise ValueError("V37_A3_POLICY_INVALID")
    digest = _canonical_sha256(policy)
    if digest != A3_POLICY_SHA256:
        raise ValueError("V37_A3_POLICY_HASH_MISMATCH")
    if policy.get("signal", {}).get("maximum_age_seconds") != 600:
        raise ValueError("V37_A3_SIGNAL_TTL_INVALID")
    if policy.get("execution", {}).get("maximum_quote_age_seconds") != 60:
        raise ValueError("V37_A3_QUOTE_TTL_INVALID")
    return deepcopy(policy), digest
