from __future__ import annotations

import pytest

from core.replay.pa_decision_quality_v38.json_response import (
    RAW_JSON_ONLY_V1,
    RAW_OR_SINGLE_JSON_FENCE_V1,
    parse_json_completion,
)
from core.replay.pa_decision_quality_v38.market_only import MarketOnlyError


def test_raw_json_only_parser_preserves_one_object() -> None:
    raw = '{"action":"WAIT","schema_version":"v1"}'
    assert parse_json_completion(raw, parser_version=RAW_JSON_ONLY_V1) == {
        "action": "WAIT", "schema_version": "v1",
    }


def test_fence_parser_accepts_exact_single_json_fence_without_rewriting_source() -> None:
    raw = ' \n```json\n{"action":"WAIT"}\n``` \n'
    parsed = parse_json_completion(raw, parser_version=RAW_OR_SINGLE_JSON_FENCE_V1)
    assert parsed == {"action": "WAIT"}
    assert raw == ' \n```json\n{"action":"WAIT"}\n``` \n'


@pytest.mark.parametrize(
    "raw",
    [
        "Here is the JSON:\n```json\n{\"action\":\"WAIT\"}\n```",
        "```json\n{\"action\":\"WAIT\"}\n```\nextra",
        "```python\n{\"action\":\"WAIT\"}\n```",
        "```json {\"action\":\"WAIT\"} ```",
        "```json\n{\"action\":\"WAIT\"}\n```\n```json\n{}\n```",
    ],
)
def test_fence_parser_rejects_non_single_or_non_json_envelopes(raw: str) -> None:
    with pytest.raises(MarketOnlyError, match="GEMINI_JSON_ENVELOPE_INVALID"):
        parse_json_completion(raw, parser_version=RAW_OR_SINGLE_JSON_FENCE_V1)


def test_fence_parser_does_not_repair_invalid_inner_json() -> None:
    with pytest.raises(MarketOnlyError, match="GEMINI_JSON_COMPLETION_INVALID"):
        parse_json_completion(
            '```json\n{"action":}\n```',
            parser_version=RAW_OR_SINGLE_JSON_FENCE_V1,
        )


@pytest.mark.parametrize("raw", ["[]", "null", "true", "42"])
def test_fence_parser_requires_object_root(raw: str) -> None:
    with pytest.raises(MarketOnlyError, match="GEMINI_JSON_COMPLETION_NOT_OBJECT"):
        parse_json_completion(raw, parser_version=RAW_OR_SINGLE_JSON_FENCE_V1)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_fence_parser_rejects_duplicate_keys_and_non_json_constants(raw: str) -> None:
    with pytest.raises(MarketOnlyError, match="GEMINI_JSON_COMPLETION_INVALID"):
        parse_json_completion(raw, parser_version=RAW_OR_SINGLE_JSON_FENCE_V1)


def test_fence_parser_rejects_unknown_version() -> None:
    with pytest.raises(MarketOnlyError, match="GEMINI_JSON_PARSER_VERSION_UNSUPPORTED"):
        parse_json_completion("{}", parser_version="LATEST")
