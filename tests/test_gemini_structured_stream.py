"""Actual provider/client boundary with isolated SSE fixtures, never real trades."""
import io
import json

import pytest

import core.model_client as client_module
import core.ai.ollama as provider_module
from core.model_client import ModelClient, ModelSchemaError
from core.model_routing import DEFAULT_MODEL


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "text/event-stream"}


def stream(content, *, model=DEFAULT_MODEL, finish="stop", done=True):
    event = {"model": model, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]}
    return b"data: " + json.dumps(event).encode() + b"\n\n" + (b"data: [DONE]\n\n" if done else b"")


def test_actual_provider_collects_full_schema_constrained_stream_and_auditable_timings(monkeypatch):
    captured = []
    def fake_open(request, *, timeout):
        captured.append((json.loads(request.data), request.get_header("Accept"), timeout))
        return Response(stream('{"action":"WAIT"}'))
    monkeypatch.setattr(client_module, "urlopen", fake_open)
    client = ModelClient(model_name=DEFAULT_MODEL, retries=0)
    monkeypatch.setattr(provider_module, "model_client", client)
    provider = provider_module.OllamaProvider(model_name=DEFAULT_MODEL, retries=0)
    monkeypatch.setattr(provider, "health", lambda **_: {"available": True, "model_available": True, "actual_model_id": DEFAULT_MODEL})
    schema = {"$id": "aima:frozen-decision:v1", "type": "object", "properties": {"action": {"const": "WAIT"}}, "required": ["action"]}
    decoded, raw, metadata = provider.generate_json([{"role": "user", "content": "fixture"}],
        model_name=DEFAULT_MODEL, prompt_version="fixture", input_hash="a" * 64,
        schema=schema, max_tokens=512, temperature=0, allow_syntax_repair=False)
    assert decoded == {"action": "WAIT"} and json.loads(raw) == decoded
    payload, accept, _ = captured[0]
    assert payload["stream"] is True and accept == "text/event-stream"
    assert payload["model"] == DEFAULT_MODEL and payload["reasoning_effort"] == "high"
    assert payload["max_tokens"] == 512 and payload["temperature"] == 0
    assert payload["response_format"]["type"] == "json_schema"
    trace = metadata["transport_trace"]
    assert trace["transport_mode"] == "SSE" and trace["phase"] == "COMPLETED"
    assert trace["stream_done"] is True and trace["stream_finish_reason"] == "stop"
    assert trace["stream_event_count"] == 1
    assert 0 <= trace["headers_wait_ms"] <= trace["elapsed_ms"]
    assert 0 <= trace["first_stream_event_ms"] <= trace["elapsed_ms"]
    assert 0 <= trace["first_content_ms"] <= trace["elapsed_ms"]
    assert "fixture" not in json.dumps(trace) and "WAIT" not in json.dumps(trace)


def test_actual_client_wire_schema_matches_prompt_and_retains_raw_ttl(monkeypatch):
    captured = []
    def fake_open(request, *, timeout):
        captured.append(json.loads(request.data))
        return Response(stream('{"ttl_seconds":"900"}'))
    monkeypatch.setattr(client_module, 'urlopen', fake_open)
    client = ModelClient(model_name=DEFAULT_MODEL, retries=0)
    schema = {'$id': 'aima:frozen-decision:v1', 'type': 'object',
              'properties': {'ttl_seconds': {'type': ['integer', 'null'], 'minimum': 60, 'maximum': 1800}}}
    output = client.structured_analysis([{'role': 'user', 'content': 'fixture'}],
                                       schema=schema, stream=True, allow_syntax_repair=False)
    assert output == {'ttl_seconds': '900'}  # Original representation is not rewritten by transport.
    payload = captured[0]
    wire = payload['response_format']['json_schema']['schema']
    assert wire['properties']['ttl_seconds']['type'] == 'object'
    assert json.loads(payload['messages'][0]['content'].split('\n', 1)[1]) == wire
    assert schema['properties']['ttl_seconds']['type'] == ['integer', 'null']


def test_ttl_text_guard_is_scoped_to_the_trading_wire_contract(monkeypatch):
    monkeypatch.setattr(client_module, 'urlopen', lambda request, **kw:
                        Response(stream('{"ttl_seconds":"90000"}')))
    client = ModelClient(model_name=DEFAULT_MODEL, retries=0)
    schema = {'$id': 'aima:frozen-decision:v1', 'type': 'object',
              'properties': {'ttl_seconds': {'type': ['integer', 'null'], 'minimum': 60, 'maximum': 1800}}}
    with pytest.raises(ModelSchemaError, match='TTL_TEXT_INVALID'):
        client.structured_analysis([{'role': 'user', 'content': 'fixture'}], schema=schema,
                                   stream=True, allow_syntax_repair=False)
    assert client.structured_analysis([{'role': 'user', 'content': 'generic'}],
                                     stream=True, allow_syntax_repair=False) == {'ttl_seconds': '90000'}


@pytest.mark.parametrize("body,code", [
    (stream('{"action":"OPEN_LONG"}', done=False), "EOF_BEFORE_DONE"),
    (stream('{"action":"OPEN_LONG"}', finish="length"), "FINISH_INVALID"),
    (stream('{"action":"OPEN_LONG"}', model="gemini-3.8-flash"), "IDENTITY_MISMATCH"),
])
def test_structured_client_never_returns_tradable_json_from_incomplete_stream(monkeypatch, body, code):
    calls = []
    def fake_open(request, *, timeout):
        calls.append(request)
        return Response(body)
    monkeypatch.setattr(client_module, "urlopen", fake_open)
    client = ModelClient(model_name=DEFAULT_MODEL, retries=2)
    with pytest.raises(ModelSchemaError, match=code) as failure:
        client.structured_analysis([{"role": "user", "content": "fixture"}], stream=True,
            model_name=DEFAULT_MODEL, retries=0, allow_syntax_repair=False)
    assert len(calls) == 1 and client.last_response_model is None
    assert failure.value.transport_trace["failed_phase"] == "READING_RESPONSE_BODY"
    assert failure.value.transport_trace["phase"] == "FAILED"
    assert failure.value.transport_trace["stream_done"] is False


def test_invalid_final_json_does_not_trigger_hidden_repair_when_disabled(monkeypatch):
    calls = []
    def fake_open(request, *, timeout):
        calls.append(request)
        return Response(stream('{"action":"OPEN_LONG"'))
    monkeypatch.setattr(client_module, "urlopen", fake_open)
    client = ModelClient(model_name=DEFAULT_MODEL, retries=0)
    with pytest.raises(ModelSchemaError, match="invalid JSON"):
        client.structured_analysis([{"role": "user", "content": "fixture"}], stream=True,
            allow_syntax_repair=False, retries=0)
    assert len(calls) == 1
