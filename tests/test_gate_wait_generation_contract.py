"""Gate WAIT generation and acceptance align; all model/SFT fixtures are isolated."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from core.model_client import ModelClient
from core.model_routing import DEFAULT_MODEL
from core.storage import SQLiteStore
from core.trading import ai_session_coordinator as coordinator_module
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.execution_gateway import ExecutionGateway, TradingMode
from core.trading.ledger import AccountLedger
from core.trading.model_schemas import AI_ACTION_SCHEMA, frozen_decision_schema, frozen_response_format, validate_schema
from core.trading.position_guardian import PositionGuardian
from core.trading.session_manager import SessionManager
from tests.test_autonomous_news_strategy import context_and_output


INPUTS = {"allowed_instruments": ["BTCUSDT"], "evidence_refs": ["market_snapshot:BTCUSDT:observed"]}
BASE_WAIT = {"action": "WAIT", "instrument_id": "BTCUSDT", "reason": "等待明确的回踩确认", "confidence": None}


def valid_wait(trigger="price"):
    return {
        **BASE_WAIT, "strategy_analysis": {"missing_conditions": ["已收盘回踩确认尚未成立"]},
        **({"next_trigger_price": 84863.0} if trigger == "price" else {"entry_condition": "等待已收盘回踩确认"}),
    }


@pytest.mark.parametrize("trigger", ["price", "condition"])
def test_gate_wait_requires_model_authored_conditions_and_either_trigger(trigger):
    decision = valid_wait(trigger)
    original = deepcopy(decision)
    validate_schema(decision, frozen_decision_schema(INPUTS, gate_wait=True))
    assert decision == original


@pytest.mark.parametrize("analysis", [None, {}, {"missing_conditions": []}, {"missing_conditions": [""]},
                                          {"missing_conditions": [" \t\n"]}, {"missing_conditions": ["\u3000"]}])
def test_gate_wait_rejects_missing_empty_and_whitespace_conditions(analysis):
    decision = {**valid_wait(), "strategy_analysis": analysis}
    with pytest.raises(ValueError, match="INVALID_ACTION_SCHEMA"):
        validate_schema(decision, frozen_decision_schema(INPUTS, gate_wait=True))


@pytest.mark.parametrize("trigger", [{}, {"next_trigger_price": None}, {"entry_condition": None},
                                     {"entry_condition": ""}, {"entry_condition": " \n"},
                                     {"next_trigger_price": 0}, {"next_trigger_price": -1},
                                     {"next_trigger_price": True}, {"next_trigger_price": float("inf")}])
def test_gate_wait_rejects_unknown_empty_or_invalid_next_trigger(trigger):
    decision = valid_wait()
    decision.pop("next_trigger_price")
    decision.update(trigger)
    with pytest.raises(ValueError, match="INVALID_ACTION_SCHEMA"):
        validate_schema(decision, frozen_decision_schema(INPUTS, gate_wait=True))


@pytest.mark.parametrize("action", [action for action in AI_ACTION_SCHEMA["properties"]["action"]["enum"] if action != "WAIT"])
def test_gate_wait_branch_adds_no_requirements_to_other_actions(action):
    decision = {**BASE_WAIT, "action": action}
    validate_schema(decision, frozen_decision_schema(INPUTS, gate_wait=True))


def test_non_gate_schema_and_global_schema_remain_unchanged():
    original = deepcopy(AI_ACTION_SCHEMA)
    validate_schema(BASE_WAIT, frozen_decision_schema(INPUTS))
    gated = frozen_decision_schema(INPUTS, gate_wait=True)
    assert "anyOf" not in AI_ACTION_SCHEMA
    assert AI_ACTION_SCHEMA == original
    assert gated["properties"]["action"] == original["properties"]["action"]


def test_wait_pinned_schema_cannot_generate_or_accept_an_open_action():
    pinned = deepcopy(AI_ACTION_SCHEMA)
    pinned["properties"]["action"]["enum"] = ["WAIT"]
    full = frozen_decision_schema(INPUTS, pinned, gate_wait=True)
    assert len(full["anyOf"]) == 2
    assert all(branch["properties"]["action"]["enum"] == ["WAIT"] for branch in full["anyOf"])
    with pytest.raises(ValueError, match="action:enum"):
        validate_schema({**valid_wait(), "action": "OPEN_LONG"}, full)


def test_native_branches_preserve_required_nonempty_facts_and_visible_identity():
    native = frozen_response_format(frozen_decision_schema(INPUTS, gate_wait=True))["json_schema"]["schema"]
    assert len(native["anyOf"]) == 3
    waits = [branch for branch in native["anyOf"] if branch["properties"]["action"]["enum"] == ["WAIT"]]
    assert len(waits) == 2
    for branch in native["anyOf"]:
        assert branch["properties"]["instrument_id"]["enum"] == INPUTS["allowed_instruments"]
        assert branch["properties"]["evidence_refs"]["items"]["enum"] == INPUTS["evidence_refs"]
    for branch in waits:
        analysis = branch["properties"]["strategy_analysis"]
        assert "strategy_analysis" in branch["required"]
        assert analysis["type"] == "object" and "missing_conditions" in analysis["required"]
        missing = analysis["properties"]["missing_conditions"]
        assert missing["minItems"] == 1 and missing["items"]["minLength"] == 1
        assert "pattern" not in missing["items"]
    validate_schema(valid_wait(), native)
    validate_schema(valid_wait("condition"), native)
    with pytest.raises(ValueError, match="anyOf"):
        validate_schema(BASE_WAIT, native)


@pytest.mark.parametrize("blank", [" ", "\t\n", "\u00a0", "\u3000"])
@pytest.mark.parametrize("field", ["missing_conditions", "entry_condition"])
def test_projected_contract_explicitly_defers_ascii_and_unicode_whitespace_to_local_validation(blank, field):
    full = frozen_decision_schema(INPUTS, gate_wait=True)
    native = frozen_response_format(full)["json_schema"]["schema"]
    decision = valid_wait("condition")
    if field == "missing_conditions":
        decision["strategy_analysis"][field] = [blank]
    else:
        decision[field] = blank
    # Structural checks of the projected schema document its exact boundary;
    # they do not claim to execute or prove the runtime grammar converter.
    validate_schema(decision, native)
    with pytest.raises(ValueError, match="anyOf"):
        validate_schema(decision, full)


@pytest.mark.parametrize("field", ["missing_conditions", "entry_condition"])
def test_native_projection_still_requires_nonempty_text(field):
    native = frozen_response_format(frozen_decision_schema(INPUTS, gate_wait=True))["json_schema"]["schema"]
    decision = valid_wait("condition")
    if field == "missing_conditions":
        decision["strategy_analysis"][field] = [""]
    else:
        decision[field] = ""
    with pytest.raises(ValueError, match="anyOf"):
        validate_schema(decision, native)


def test_projection_preserves_other_supported_patterns_and_full_local_unicode_constraint():
    schema = {"type": "string", "minLength": 1, "pattern": "^a+$"}
    assert frozen_response_format(schema)["json_schema"]["schema"] == schema
    full = frozen_decision_schema(INPUTS, gate_wait=True)
    waits = [branch for branch in full["anyOf"] if branch["properties"]["action"]["enum"] == ["WAIT"]]
    for branch in waits:
        missing = branch["properties"]["strategy_analysis"]["properties"]["missing_conditions"]
        assert missing["items"]["pattern"] == r"^[\s\S]*\S[\s\S]*$"


def test_anyof_and_pattern_support_standard_alternative_and_search_semantics():
    validate_schema([1], {"anyOf": [{"type": "array", "minItems": 1, "items": {"type": "integer"}}, {"type": "null"}]})
    validate_schema("prefix-confirmed-suffix", {"type": "string", "pattern": "confirmed"})
    with pytest.raises(ValueError, match="pattern"):
        validate_schema("unrelated", {"type": "string", "pattern": "confirmed"})


def test_gate_native_contract_adds_no_schema_text_to_model_messages(monkeypatch):
    client = ModelClient(base_url="http://127.0.0.1:8045/v1", model_name=DEFAULT_MODEL, retries=0)
    captured = []
    decision = valid_wait()
    def reply(**kwargs):
        captured.append(kwargs)
        return {"choices": [{"message": {"content": json.dumps(decision, ensure_ascii=False)}}]}
    monkeypatch.setattr(client, "chat_completion", reply)
    messages = [{"role": "system", "content": "JSON字段规则：WAIT必须给出缺失条件及下一触发"},
                {"role": "user", "content": json.dumps(INPUTS)}]
    schema = frozen_decision_schema(INPUTS, gate_wait=True)
    assert client.structured_analysis(messages, schema=schema, allow_syntax_repair=False) == decision
    assert captured[0]["messages"] == messages
    from core.trading.model_schemas import gemini_response_format
    assert captured[0]["response_format"] == gemini_response_format(schema)


@pytest.fixture
def isolated_gate(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "gate-wait-generation.sqlite3")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("news-paper", mode="PAPER", initial_deposit=Decimal("10000"))
    samples = []
    coordinator = AISessionCoordinator(
        store=store, service=SimpleNamespace(), ledger=ledger,
        guardian=PositionGuardian(store, ledger), session_manager=SessionManager(store),
        execution_gateway=ExecutionGateway(store, ledger=ledger),
        sft_sample_sink=lambda **sample: samples.append(sample),
    )
    def forbidden(*_args, **_kwargs):
        raise AssertionError("isolated test must never reach production SFT collection")
    monkeypatch.setattr(coordinator_module, "record_sft_sample", forbidden)
    monkeypatch.setattr(coordinator_module, "_load_market_radar_snapshot", lambda *_args: {"status": "NO_DATA"})
    context, _ = context_and_output(datetime.now(timezone.utc))
    context.mode, context.venue = TradingMode.TESTNET, "gate"
    yield coordinator, context, samples
    coordinator.close()
    ledger.close()


def install_model(coordinator, decision):
    class Model:
        model_id = "Bonsai-2-27B-PTQ1_0"
        context_length = 32768
        max_tokens = 1024
        calls = 0
        schemas = []
        def generate_json(self, _messages, **kwargs):
            self.calls += 1
            self.schemas.append(deepcopy(kwargs["schema"]))
            return deepcopy(decision), json.dumps(decision, ensure_ascii=False), {}
    model = Model()
    coordinator.model_provider = model
    return model


@pytest.mark.parametrize("trigger", ["price", "condition"])
def test_valid_gate_wait_is_recorded_exactly_once_in_injected_sft_sink(isolated_gate, trigger):
    coordinator, context, samples = isolated_gate
    decision = valid_wait(trigger)
    model = install_model(coordinator, decision)
    output = coordinator._model_output(context)
    assert output.action == "WAIT" and output.reason == decision["reason"]
    assert model.calls == 1
    assert len(model.schemas[0]["anyOf"]) == 3
    assert len(samples) == 1 and samples[0]["model_output"] == decision


@pytest.mark.parametrize("decision", [BASE_WAIT, {**valid_wait(), "strategy_analysis": {"missing_conditions": [" "]}},
                                      {**BASE_WAIT, "strategy_analysis": {"missing_conditions": ["等待确认"]}}])
def test_invalid_gate_wait_fails_first_call_without_retry_or_sft_sample(isolated_gate, decision):
    coordinator, context, samples = isolated_gate
    model = install_model(coordinator, decision)
    with pytest.raises(ValueError, match="INVALID_MODEL_OUTPUT_SCHEMA"):
        coordinator._model_output(context)
    assert model.calls == 1 and samples == []


def test_final_gate_wait_guard_still_precedes_sft_when_a_legacy_schema_is_supplied(isolated_gate, monkeypatch):
    coordinator, context, samples = isolated_gate
    # Exercise the independent final semantic guard with the genuine legacy
    # schema, rather than disabling validation or synthesizing WAIT facts.
    constructor = coordinator_module.frozen_decision_schema
    monkeypatch.setattr(coordinator_module, "frozen_decision_schema",
                        lambda inputs, base_schema=None, **_kwargs: constructor(inputs, base_schema))
    model = install_model(coordinator, BASE_WAIT)
    with pytest.raises(ValueError, match="WAIT_MISSING_CONDITIONS_REQUIRED"):
        coordinator._model_output(context)
    assert model.calls == 1 and samples == []
