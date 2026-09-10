"""Ollama provider compatibility and provenance regression coverage."""

import json

import pytest

from core.ai.contracts import LLMError
from core.ai.ollama import OllamaProvider


def test_generate_json_falls_back_only_after_explicit_schema_grammar_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaProvider(model_name="qwen3.5:9b", retries=0, max_tokens=32)
    calls: list[dict[str, object]] = []

    def fake_request(_method: str, _path: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append(payload)
        if len(calls) == 1:
            raise LLMError(
                "Ollama rejected the supplied JSON schema grammar",
                code="MODEL_SCHEMA_UNSUPPORTED",
                raw_response='{"error":"failed to parse grammar"}',
            )
        return {
            "model": "qwen3.5:9b",
            "message": {"role": "assistant", "content": '{"action":"WAIT"}'},
        }

    monkeypatch.setattr(provider, "_request", fake_request)
    schema = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["WAIT"]}},
        "required": ["action"],
        "additionalProperties": False,
    }
    decoded, raw, metadata = provider.generate_json(
        [{"role": "user", "content": "safe smoke"}],
        model_name="qwen3.5:9b",
        prompt_version="test",
        input_hash="a" * 64,
        schema=schema,
    )

    assert decoded == {"action": "WAIT"}
    assert json.loads(raw) == decoded
    assert len(calls) == 2
    assert calls[0]["format"] == schema
    assert calls[1]["format"] == "json"
    assert metadata["schema_enforcement"] == "json_mode_local_validation"
    assert metadata["schema_version"] == "json_mode_local_validation"


def test_generate_json_does_not_downgrade_arbitrary_model_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaProvider(model_name="qwen3.5:9b", retries=0)
    calls = 0

    def fake_request(_method: str, _path: str, _payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise LLMError("model unavailable", code="MODEL_UNAVAILABLE")

    monkeypatch.setattr(provider, "_request", fake_request)
    with pytest.raises(LLMError, match="model unavailable"):
        provider.generate_json(
            [{"role": "user", "content": "safe smoke"}],
            model_name="qwen3.5:9b",
            prompt_version="test",
            input_hash="b" * 64,
            schema={"type": "object"},
        )
    assert calls == 1
