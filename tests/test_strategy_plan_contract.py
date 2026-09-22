import pytest

from core.trading.ai_session_coordinator import derive_strategy_plan_from_text
from core.trading.model_schemas import AI_ACTION_SCHEMA, validate_schema

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
