"""The misplaced-plan normalizer must stay wired into the decision path.

On 2026-09-26 this function existed with no call site at all, so every model
response that nested its plan object under ``strategy_analysis`` was rejected
with INVALID_ACTION_SCHEMA:output.strategy_analysis:fields and the cycle was
blocked. These tests pin both the behaviour and the wiring.
"""

import re
from pathlib import Path

import pytest

from core.trading.model_schemas import (
    AI_ACTION_SCHEMA,
    normalize_misplaced_strategy_plan,
    validate_schema,
)

COORDINATOR = Path(__file__).resolve().parents[1] / "core" / "trading" / "ai_session_coordinator.py"


def wait_payload(**analysis_extra):
    payload = {
        "action": "WAIT",
        "instrument_id": "BTCUSDT",
        "reason": "结构未确认",
        "confidence": 40,
        "strategy_analysis": {
            "missing_conditions": ["5m 未收盘"],
            "trigger_completion_pct": 42.0,
            **analysis_extra,
        },
    }
    return payload


def test_complete_nested_plan_is_relocated_and_then_passes_the_schema():
    payload = wait_payload(
        name="BTCUSDT_PLAN",
        thesis="回踩 EMA20 后承接",
        entry_conditions=["price 86000"],
        exit_conditions=["stop 85400"],
    )
    record = normalize_misplaced_strategy_plan(payload)

    assert record == {
        "field": "strategy_analysis",
        "normalized_fields": ["strategy_plan"],
        "source": "EXACT_KNOWN_MODEL_ALIAS",
    }
    assert payload["strategy_plan"]["thesis"] == "回踩 EMA20 后承接"
    assert "name" not in payload["strategy_analysis"]
    validate_schema(payload, AI_ACTION_SCHEMA)


def test_incomplete_nested_plan_is_left_for_the_strict_validator():
    payload = wait_payload(name="BTCUSDT_PLAN", thesis="只有部分字段")
    assert normalize_misplaced_strategy_plan(payload) is None
    with pytest.raises(ValueError, match=r"output\.strategy_analysis:fields"):
        validate_schema(payload, AI_ACTION_SCHEMA)


def test_conflicting_top_level_plan_is_not_silently_overwritten():
    payload = wait_payload(
        name="BTCUSDT_PLAN",
        thesis="嵌套版本",
        entry_conditions=["a"],
        exit_conditions=["b"],
    )
    payload["strategy_plan"] = {
        "name": "OTHER",
        "thesis": "顶层版本",
        "entry_conditions": ["c"],
        "exit_conditions": ["d"],
    }
    assert normalize_misplaced_strategy_plan(payload) is None
    assert payload["strategy_plan"]["thesis"] == "顶层版本"


def test_nested_trigger_price_is_promoted_to_the_top_level():
    payload = wait_payload(next_trigger_price=86123.5)
    record = normalize_misplaced_strategy_plan(payload)
    assert record is not None
    assert payload["next_trigger_price"] == 86123.5
    assert "next_trigger_price" not in payload["strategy_analysis"]
    validate_schema(payload, AI_ACTION_SCHEMA)


def test_schema_valid_six_key_analysis_survives_the_strict_block_key_set():
    # The coordinator's strict validator must accept exactly what the schema
    # allows; a prompt-compliant WAIT carrying next_trigger_price and
    # entry_condition used to be rejected downstream of validate_schema.
    source = COORDINATOR.read_text(encoding="utf-8")
    match = re.search(
        r"set\(strategy_analysis\) - \{\s*(.*?)\s*\}", source, re.S
    )
    assert match, "strict strategy_analysis key set not found"
    allowed = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    schema_keys = set(AI_ACTION_SCHEMA["properties"]["strategy_analysis"]["properties"])
    assert allowed == schema_keys


def test_the_normalizer_is_wired_before_every_decision_path_validation():
    source = COORDINATOR.read_text(encoding="utf-8")
    calls = source.count("normalize_misplaced_strategy_plan(")
    # one import-free call per validation site: first attempt and repair result
    assert calls >= 2, f"normalizer call sites: {calls}"
    for block in re.findall(
        r"normalize_misplaced_strategy_plan\((candidate_decoded|decoded)\)(.{0,400}?)"
        r"validate_schema\(\1, AI_ACTION_SCHEMA\)",
        source,
        re.S,
    ):
        assert "model_output_normalizations" in block[1]
