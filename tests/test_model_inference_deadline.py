"""Focused guards for the bounded Gemini decision request."""

from __future__ import annotations

import pytest

from core.ai.contracts import LLMError
from core.ai.ollama import OllamaProvider
from core.model_client import ModelClient, ModelSchemaError
from core.model_routing import DEFAULT_SMART_MODEL


MODEL_ID = DEFAULT_SMART_MODEL


def test_provider_passes_only_the_remaining_deadline_to_one_json_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai import ollama as ollama_module

    provider = OllamaProvider(
        base_url="http://127.0.0.1:8045/v1",
        model_name=DEFAULT_SMART_MODEL,
        timeout=160.0,
        retries=0,
    )
    client = ollama_module.model_client
    clock = iter((10.0, 12.5))
    monkeypatch.setattr(ollama_module.time, "monotonic", lambda: next(clock))
    health_calls: list[float | None] = []
    request: dict[str, object] = {}

    def fake_health(*, model_name: str | None = None, timeout_sec: float | None = None):
        health_calls.append(timeout_sec)
        return {
            "available": True,
            "model_available": True,
            "actual_model_id": MODEL_ID,
        }

    def fake_analysis(_messages, **kwargs):
        request.update(kwargs)
        client._response_state.model = MODEL_ID
        return {"action": "WAIT"}

    monkeypatch.setattr(provider, "health", fake_health)
    monkeypatch.setattr(client, "structured_analysis", fake_analysis)

    result, _raw, _metadata = provider.generate_json(
        [{"role": "user", "content": "decision facts"}],
        model_name=DEFAULT_SMART_MODEL,
        prompt_version="bounded-test",
        input_hash="a" * 64,
        deadline_monotonic=20.0,
        allow_syntax_repair=False,
    )

    assert result == {"action": "WAIT"}
    assert health_calls == [2.5]
    assert request["timeout_sec"] == pytest.approx(7.5)
    assert request["retries"] == 0
    assert request["allow_syntax_repair"] is False


def test_provider_skips_health_and_inference_when_deadline_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai import ollama as ollama_module

    provider = OllamaProvider(
        base_url="http://127.0.0.1:8045/v1",
        model_name=DEFAULT_SMART_MODEL,
        retries=0,
    )
    calls = {"health": 0, "inference": 0}

    def fake_health(**_kwargs):
        calls["health"] += 1
        return {"available": True, "model_available": True, "actual_model_id": MODEL_ID}

    def fake_analysis(*_args, **_kwargs):
        calls["inference"] += 1
        return {"action": "WAIT"}

    monkeypatch.setattr(ollama_module.time, "monotonic", lambda: 20.0)
    monkeypatch.setattr(provider, "health", fake_health)
    monkeypatch.setattr(ollama_module.model_client, "structured_analysis", fake_analysis)

    with pytest.raises(LLMError, match="MODEL_DEADLINE_EXCEEDED"):
        provider.generate_json(
            [],
            model_name=DEFAULT_SMART_MODEL,
            prompt_version="expired-test",
            input_hash="b" * 64,
            deadline_monotonic=20.0,
        )

    assert calls == {"health": 0, "inference": 0}


def test_model_client_does_not_start_syntax_repair_in_bounded_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ModelClient(base_url="http://127.0.0.1:8045/v1", model_name=DEFAULT_SMART_MODEL)
    calls = 0

    def invalid_json(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"choices": [{"message": {"content": "not-json"}}]}

    monkeypatch.setattr(client, "chat_completion", invalid_json)

    with pytest.raises(ModelSchemaError, match="invalid JSON") as caught:
        client.structured_analysis(
            [{"role": "user", "content": "facts"}],
            allow_syntax_repair=False,
            retries=0,
        )

    assert calls == 1
    assert caught.value.raw_response == "not-json"


def test_provider_preserves_malformed_completion_for_bounded_redacted_audit(monkeypatch):
    from core.ai import ollama as module
    provider = OllamaProvider(retries=0)
    monkeypatch.setattr(provider, 'health', lambda **kwargs: {'available': True, 'model_available': True, 'actual_model_id': MODEL_ID})
    def broken(*args, **kwargs):
        raise ModelSchemaError('bad JSON', raw_response='{"action":"OPEN_LONG", unfinished')
    monkeypatch.setattr(module.model_client, 'structured_analysis', broken)
    with pytest.raises(LLMError) as caught:
        provider.generate_json([], model_name=MODEL_ID, prompt_version='fixture-audit',
                               input_hash='f'*64, allow_syntax_repair=False)
    assert caught.value.raw_response == '{"action":"OPEN_LONG", unfinished'
