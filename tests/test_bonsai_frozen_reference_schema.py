"""Frozen grammar identities are bound to the fitted model call."""
from copy import deepcopy
import json
import pytest
from core.trading.model_schemas import AI_ACTION_SCHEMA, frozen_decision_schema, frozen_response_format, gemini_response_format, validate_schema
from core.model_client import ModelClient, ModelClientError
from core.model_routing import DEFAULT_MODEL

INPUTS={"allowed_instruments":["BTCUSDT"],"evidence_refs":["market_snapshot:BTCUSDT:0123456789abcdef"]}
GOOD={"action":"WAIT","instrument_id":"BTCUSDT","reason":"wait for next close","confidence":None,"evidence_refs":INPUTS["evidence_refs"]}

def test_typo_and_hidden_ref_fail_without_mutating_global_schema():
    before=deepcopy(AI_ACTION_SCHEMA)
    schema=frozen_decision_schema(INPUTS)
    validate_schema(GOOD,schema)
    for bad in ("market_snapshot:BTCUSDT:0123456789abdef","news_revision:hidden"):
        with pytest.raises(ValueError,match="evidence_refs.*enum"):
            validate_schema(dict(GOOD,evidence_refs=[bad]),schema)
    with pytest.raises(ValueError,match="instrument_id:enum"):
        validate_schema(dict(GOOD,instrument_id="ETHUSDT"),schema)
    assert AI_ACTION_SCHEMA==before

def test_native_projection_preserves_enums_and_local_numeric_limits():
    full=frozen_decision_schema(INPUTS)
    grammar=frozen_response_format(full)["json_schema"]["schema"]
    assert grammar["properties"]["instrument_id"]["enum"]==["BTCUSDT"]
    assert grammar["properties"]["evidence_refs"]["items"]["enum"]==INPUTS["evidence_refs"]
    assert "anyOf" in grammar["properties"]["confidence"]
    with pytest.raises(ValueError,match="confidence:range"):
        validate_schema(dict(GOOD,confidence=101),full)
    empty=frozen_response_format(frozen_decision_schema(dict(INPUTS,evidence_refs=[])))
    assert empty["json_schema"]["schema"]["properties"]["evidence_refs"]=={"enum":[[]]}

def test_client_sends_native_projection_with_no_silent_fallback(monkeypatch):
    client=ModelClient(base_url="http://127.0.0.1:8045/v1",model_name=DEFAULT_MODEL,retries=0)
    calls=[]
    def reply(**kwargs):
        calls.append(kwargs)
        return {"choices":[{"message":{"content":json.dumps(GOOD)}}]}
    monkeypatch.setattr(client,"chat_completion",reply)
    full=frozen_decision_schema(INPUTS)
    assert client.structured_analysis([{"role":"system","content":"JSON字段规则：test"},{"role":"user","content":"test"}],schema=full,allow_syntax_repair=False)==GOOD
    assert calls[0]["response_format"]==gemini_response_format(full)
    assert client.last_schema_enforcement=="json_schema"
    def reject(**kwargs):
        calls.append(kwargs)
        raise ModelClientError("native grammar failed")
    monkeypatch.setattr(client,"chat_completion",reject)
    with pytest.raises(ModelClientError,match="native grammar"):
        client.structured_analysis([{"role":"user","content":"test"}],schema=full,allow_syntax_repair=False)
    assert len(calls)==2
    assert client.last_schema_enforcement is None

def test_repair_pins_are_intersected_with_visible_identity():
    pinned=deepcopy(AI_ACTION_SCHEMA)
    pinned["properties"]["instrument_id"]["enum"]=["BTCUSDT"]
    assert frozen_decision_schema(dict(INPUTS,allowed_instruments=["BTCUSDT","ETHUSDT"]),pinned)["properties"]["instrument_id"]["enum"]==["BTCUSDT"]
