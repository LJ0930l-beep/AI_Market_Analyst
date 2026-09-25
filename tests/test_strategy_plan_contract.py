import pytest

from core.trading.ai_session_coordinator import derive_strategy_plan_from_text
from core.trading.model_schemas import AI_ACTION_SCHEMA, normalize_limit_ttl_alias, normalize_news_impact, normalize_wait_conditions, normalize_wait_symbol_alias, validate_schema


def test_wait_missing_conditions_are_preserved_in_supported_schema_field() -> None:
    decoded = {
        "action": "WAIT", "instrument_id": "SPCXUSDT", "reason": "等待价格确认",
        "confidence": 0.35, "missing_conditions": ["5m 收盘突破 149.08"],
        "next_trigger_price": 149.08, "evidence_refs": [],
    }
    assert normalize_wait_conditions(decoded) == {
        "field": "missing_conditions", "normalized_field": "strategy_analysis.missing_conditions",
    }
    validate_schema(decoded, AI_ACTION_SCHEMA)
    assert decoded["strategy_analysis"]["missing_conditions"] == ["5m 收盘突破 149.08"]


def test_authorized_wait_symbol_alias_is_auditable_and_non_executing_only() -> None:
    decoded = {
        "action": "WAIT", "symbol": "GOATUSDT", "reason": "等待放量突破",
        "confidence": 0.35, "missing_conditions": ["5m 放量突破"],
        "next_trigger_price": 0.0194, "entry_condition": "等待 5m 收盘确认",
        "evidence_refs": [],
    }
    assert normalize_wait_conditions(decoded)
    assert normalize_wait_symbol_alias(decoded, ("GOATUSDT", "BTCUSDT")) == {
        "field": "symbol", "normalized_field": "instrument_id", "value": "GOATUSDT",
    }
    validate_schema(decoded, AI_ACTION_SCHEMA)
    assert decoded["instrument_id"] == "GOATUSDT" and "symbol" not in decoded

    for action, allowed in (("OPEN_LONG", ("GOATUSDT",)), ("WAIT", ("BTCUSDT",))):
        invalid = {"action": action, "symbol": "GOATUSDT", "reason": "x", "confidence": 35}
        assert normalize_wait_symbol_alias(invalid, allowed) is None
        with pytest.raises(ValueError, match="INVALID_ACTION_SCHEMA:output:fields"):
            validate_schema(invalid, AI_ACTION_SCHEMA)

# The post-repair fallback matches this string exactly. Pinning it here means a
# future reformat of the error breaks a test instead of quietly disabling the
# fallback and sending OPEN cycles back to SYSTEM_BLOCKED.
STRATEGY_PLAN_TYPE_ERROR = "INVALID_ACTION_SCHEMA:output.strategy_plan:type"


def open_payload(strategy_plan, **overrides):
    payload = {
        "action": "OPEN_LONG",
        "instrument_id": "BTCUSDT",
        "reason": "5m EMA20 回踩确认，量能配合",
        "confidence": 78,
        "entry_price": 86000.0,
        "stop_price": 85400.0,
        "take_profit": 87200.0,
        "strategy_plan": strategy_plan,
    }
    payload.update(overrides)
    return payload


def test_flattened_strategy_plan_produces_exactly_the_tolerated_error():
    with pytest.raises(ValueError) as exc:
        validate_schema(open_payload("限价挂在 EMA20 回踩位"), AI_ACTION_SCHEMA)
    assert str(exc.value) == STRATEGY_PLAN_TYPE_ERROR


def test_derived_plan_reuses_only_model_supplied_facts_and_meets_the_contract():
    decoded = open_payload("限价挂在 EMA20 回踩位，破前低则离场")
    plan = derive_strategy_plan_from_text(decoded)

    assert plan["thesis"] == "限价挂在 EMA20 回踩位，破前低则离场"
    assert plan["entry_conditions"] == ["entry_price 86000.0"]
    assert plan["exit_conditions"] == ["stop_price 85400.0", "take_profit 87200.0"]

    decoded["strategy_plan"] = plan
    validate_schema(decoded, AI_ACTION_SCHEMA)


def test_derived_plan_without_model_prices_stays_contract_valid():
    decoded = open_payload("等待结构确认", entry_price=None, stop_price=None, take_profit=None)
    plan = derive_strategy_plan_from_text(decoded)
    assert plan["entry_conditions"] == ["等待结构确认"]
    assert plan["exit_conditions"] == ["等待结构确认"]
    decoded["strategy_plan"] = plan
    validate_schema(decoded, AI_ACTION_SCHEMA)


def test_derive_returns_none_when_the_model_supplied_no_text():
    assert derive_strategy_plan_from_text({"strategy_plan": "   "}) is None
    assert derive_strategy_plan_from_text({}) is None


def test_other_violations_are_not_mistaken_for_the_tolerated_one():
    decoded = open_payload("fine", confidence="not-a-number")
    with pytest.raises(ValueError) as exc:
        validate_schema(decoded, AI_ACTION_SCHEMA)
    assert STRATEGY_PLAN_TYPE_ERROR not in str(exc.value)


def test_exact_numeric_news_impact_is_auditable_without_relaxing_schema():
    decoded = open_payload(
        {"name": "EMA 回踩", "thesis": "突破后回踩", "entry_conditions": ["回踩 EMA20"], "exit_conditions": ["跌破结构止损"]},
        news_context={"impact": 1.0, "summary": "WIF 新闻利好"},
    )
    with pytest.raises(ValueError, match=r"output\.news_context\.impact:type"):
        validate_schema(decoded, AI_ACTION_SCHEMA)
    assert normalize_news_impact(decoded) == {"field": "news_context.impact", "raw": 1.0, "normalized": "POSITIVE"}
    validate_schema(decoded, AI_ACTION_SCHEMA)


@pytest.mark.parametrize("impact, expected", [(-2 / 3, "NEGATIVE"), (0.5, "POSITIVE"), (0.1, "NEUTRAL")])
def test_bounded_model_news_score_projects_to_label(impact, expected):
    decoded = {"news_context": {"impact": impact, "summary": "headline"}}
    assert normalize_news_impact(decoded) == {"field": "news_context.impact", "raw": impact, "normalized": expected}
    assert decoded["news_context"]["impact"] == expected


@pytest.mark.parametrize("impact", [-1.1, 1.1, True, "1", float("nan")])
def test_ambiguous_news_impact_remains_invalid(impact):
    decoded = {"news_context": {"impact": impact, "summary": "headline"}}
    assert normalize_news_impact(decoded) is None
    assert decoded["news_context"]["impact"] is impact


def test_limit_ttl_alias_preserves_exact_model_value_and_schema():
    decoded = open_payload(
        {"name": "EMA 回踩", "thesis": "突破后回踩", "entry_conditions": ["回踩 EMA20"], "exit_conditions": ["跌破结构止损"]},
        order_preference="LIMIT", limit_price=86000.0, limit_ttl_seconds=480,
    )
    with pytest.raises(ValueError, match=r"output:fields"):
        validate_schema(decoded, AI_ACTION_SCHEMA)
    assert normalize_limit_ttl_alias(decoded) == {
        "field": "limit_ttl_seconds", "raw": 480, "normalized_field": "ttl_seconds",
    }
    assert decoded["ttl_seconds"] == 480
    assert "limit_ttl_seconds" not in decoded
    validate_schema(decoded, AI_ACTION_SCHEMA)


@pytest.mark.parametrize("value", [True, 0, 59, 1801, 480.0, "480"])
def test_invalid_limit_ttl_alias_does_not_weaken_schema(value):
    decoded = {"limit_ttl_seconds": value}
    assert normalize_limit_ttl_alias(decoded) is None
    assert decoded == {"limit_ttl_seconds": value}


def test_conflicting_limit_ttl_fields_are_not_silently_overwritten():
    decoded = {"ttl_seconds": 900, "limit_ttl_seconds": 480}
    assert normalize_limit_ttl_alias(decoded) is None
    assert decoded["ttl_seconds"] == 900
