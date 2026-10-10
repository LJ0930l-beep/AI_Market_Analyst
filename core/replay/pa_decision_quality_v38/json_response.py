"""Strict, versioned JSON completion-envelope parsing for V38 research pilots."""
from __future__ import annotations

import json
import re
from typing import Any

from .market_only import MarketOnlyError

RAW_JSON_ONLY_V1 = "RAW_JSON_ONLY_V1"
RAW_OR_SINGLE_JSON_FENCE_V1 = "RAW_OR_SINGLE_JSON_FENCE_V1"
_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n```[ \t]*", re.DOTALL)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def parse_json_completion(content: str, *, parser_version: str) -> dict[str, Any]:
    """Parse one object using an explicitly selected envelope policy.

    The returned value is separate from ``content``. Callers must retain and
    hash the exact original completion; this helper never rewrites it.
    """
    if not isinstance(content, str):
        raise MarketOnlyError("GEMINI_JSON_COMPLETION_NOT_TEXT")
    stripped = content.strip()
    if parser_version == RAW_JSON_ONLY_V1:
        candidate = stripped
    elif parser_version == RAW_OR_SINGLE_JSON_FENCE_V1:
        if "```" in stripped:
            match = _JSON_FENCE.fullmatch(stripped)
            if match is None or stripped.count("```") != 2:
                raise MarketOnlyError("GEMINI_JSON_ENVELOPE_INVALID")
            candidate = match.group(1)
        else:
            candidate = stripped
    else:
        raise MarketOnlyError("GEMINI_JSON_PARSER_VERSION_UNSUPPORTED")

    try:
        value = json.loads(
            candidate,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise MarketOnlyError("GEMINI_JSON_COMPLETION_INVALID") from exc
    if not isinstance(value, dict):
        raise MarketOnlyError("GEMINI_JSON_COMPLETION_NOT_OBJECT")
    return value
