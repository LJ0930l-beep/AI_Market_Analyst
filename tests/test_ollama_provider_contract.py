"""Bonsai-only inference routing and model provenance regression coverage."""

import json

import pytest

from core.ai.contracts import LLMError
from core.ai.ollama import OllamaProvider
from core.model_client import ModelClient, ModelClientError, ModelSchemaError
from core.model_routing import DEFAULT_SMART_MODEL

MANIFEST_ID = r"D:\RJ\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"


def test_generate_json_uses_verified_manifest_and_modelclient_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai import ollama as ollama_module

    provider = OllamaProvider(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL, retries=0)
    client = ollama_module.model_client
    monkeypatch.setattr(client, "is_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(client, "list_models", lambda **_kwargs: [{"id": MANIFEST_ID, "meta": {"n_ctx": 8192}}])
    monkeypatch.setattr(client, "structured_analysis", lambda *_args, **_kwargs: {"action": "WAIT"})
    client._response_state.model = None

    decoded, raw, metadata = provider.generate_json(
        [{"role": "user", "content": "safe smoke"}],
        model_name=DEFAULT_SMART_MODEL,
        prompt_version="test",
        input_hash="a" * 64,
        schema={"type": "object"},
    )

    assert decoded == {"action": "WAIT"}
    assert json.loads(raw) == decoded
    assert metadata["model_id"] == DEFAULT_SMART_MODEL
    assert metadata["actual_model_id"] == MANIFEST_ID
    assert metadata["model_identity_source"] == "request_bound_to_verified_manifest"
    assert metadata["verified_manifest_model_id"] == MANIFEST_ID


def test_generate_json_passes_provider_timeout_and_retries_to_model_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai import ollama as ollama_module

    provider = OllamaProvider(
        base_url="http://127.0.0.1:8080/v1",
        model_name=DEFAULT_SMART_MODEL,
        timeout=41.5,
        retries=0,
    )
    client = ollama_module.model_client
    captured: dict[str, object] = {}
    monkeypatch.setattr(client, "is_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(client, "list_models", lambda **_kwargs: [{"id": MANIFEST_ID, "meta": {"n_ctx": 8192}}])

    def fake_analysis(*_args, **kwargs):
        captured.update(kwargs)
        client._response_state.model = None
        return {"action": "WAIT"}

    monkeypatch.setattr(client, "structured_analysis", fake_analysis)
    provider.generate_json(
        [{"role": "user", "content": "safe contract test"}],
        model_name=DEFAULT_SMART_MODEL,
        prompt_version="test",
        input_hash="d" * 64,
        schema={"type": "object"},
    )

    assert captured["timeout_sec"] == 41.5
    assert captured["retries"] == 0


def test_provider_rejects_legacy_11434_and_non_bonsai_model_without_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai import ollama as ollama_module

    calls = 0

    def fake_analysis(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"action": "WAIT"}

    monkeypatch.setattr(ollama_module.model_client, "structured_analysis", fake_analysis)
    legacy = OllamaProvider(base_url="http://127.0.0.1:11434", model_name=DEFAULT_SMART_MODEL)
    assert legacy.health()["error_code"] == "MODEL_ENDPOINT_NOT_ALLOWED"
    with pytest.raises(LLMError) as endpoint_error:
        legacy.generate_json([], model_name=DEFAULT_SMART_MODEL, prompt_version="test", input_hash="b" * 64)
    assert endpoint_error.value.code == "MODEL_ENDPOINT_NOT_ALLOWED"

    other_model = OllamaProvider(base_url="http://127.0.0.1:8080/v1", model_name="qwen3.5:9b")
    assert other_model.health()["error_code"] == "MODEL_NOT_ALLOWED"
    with pytest.raises(LLMError) as model_error:
        other_model.generate_json([], model_name="qwen3.5:9b", prompt_version="test", input_hash="c" * 64)
    assert model_error.value.code == "MODEL_NOT_ALLOWED"
    assert calls == 0


def test_manifest_matching_is_exact_not_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai import ollama as ollama_module

    provider = OllamaProvider(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL)
    monkeypatch.setattr(ollama_module.model_client, "is_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(
        ollama_module.model_client,
        "list_models",
        lambda **_kwargs: [{"id": r"D:\models\Other-Ternary-Bonsai-2-27B-PTQ1_0.gguf", "meta": {"n_ctx": 8192}}],
    )
    result = provider.health()
    assert result["available"] is True
    assert result["model_available"] is False
    assert result["actual_model_id"] is None


def test_model_client_does_not_duplicate_schema_when_trade_prompt_has_compact_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL)
    seen = {}
    schema = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["WAIT"]}},
        "required": ["action"],
    }

    def fake_chat(messages, **_kwargs):
        seen["messages"] = messages
        return {"choices": [{"message": {"content": '{"action":"WAIT"}'}}]}

    monkeypatch.setattr(client, "chat_completion", fake_chat)
    result = client.structured_analysis(
        [{"role": "system", "content": "JSON字段规则：输出一个对象。"}, {"role": "user", "content": "事实"}],
        schema=schema,
        mode="FAST",
    )

    assert result == {"action": "WAIT"}
    system = seen["messages"][0]["content"]
    assert "JSON字段规则：" in system
    assert json.dumps(schema, ensure_ascii=False, separators=(",", ":")) not in system


def test_model_client_rejects_empty_final_content_without_sending_empty_syntax_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL)
    calls = 0

    def empty_completion(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "", "reasoning_content": "private analysis"},
            }],
            "usage": {"completion_tokens": 1000},
        }

    monkeypatch.setattr(client, "chat_completion", empty_completion)
    with pytest.raises(ModelSchemaError, match="empty completion content"):
        client.structured_analysis(
            [{"role": "system", "content": "Return JSON."}, {"role": "user", "content": "facts"}],
            schema={"type": "object"},
        )
    assert calls == 1


def test_model_client_rejects_empty_self_repair_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL)
    calls = 0

    def invalid_then_empty(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        content = "not JSON" if calls == 1 else ""
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(client, "chat_completion", invalid_then_empty)
    with pytest.raises(ModelSchemaError, match="self-repair returned empty completion content"):
        client.structured_analysis(
            [{"role": "user", "content": "facts"}],
            schema={"type": "object"},
        )
    assert calls == 2


def test_model_client_rejects_non_bonsai_route_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name="qwen3.5:9b")
    calls = 0

    def forbidden(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("network must not be reached")

    monkeypatch.setattr("core.model_client.urlopen", forbidden)
    assert client.is_healthy() is False
    with pytest.raises(Exception, match="MODEL_CONFIGURATION_MISMATCH"):
        client.chat_completion([], model_name=DEFAULT_SMART_MODEL)
    assert calls == 0


def test_model_client_counts_tokens_only_through_the_pinned_loopback_route(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import model_client as model_client_module

    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"tokens":[1,2,3,4]}'

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(model_client_module, "urlopen", fake_urlopen)
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL)

    assert client.count_tokens("中文 prompt", timeout_sec=3.5) == 4
    assert captured == {
        "url": "http://127.0.0.1:8080/tokenize",
        "body": {"content": "中文 prompt"},
        "timeout": 3.5,
    }


def test_model_client_token_counter_fails_closed_for_nonlocal_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import model_client as model_client_module

    monkeypatch.setattr(
        model_client_module,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("tokenizer must not be called for an untrusted route"),
    )
    client = ModelClient(base_url="https://example.com/v1", model_name=DEFAULT_SMART_MODEL)
    with pytest.raises(ModelClientError, match="MODEL_ENDPOINT_NOT_ALLOWED"):
        client.count_tokens("prompt")


def test_model_client_rejects_non_bonsai_response_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL)

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"model": "qwen3.5:9b", "choices": []}).encode()

    monkeypatch.setattr("core.model_client.urlopen", lambda *_args, **_kwargs: _Response())
    with pytest.raises(ModelClientError, match="MODEL_RESPONSE_IDENTITY_MISMATCH"):
        client.chat_completion([], model_name=DEFAULT_SMART_MODEL)


def test_model_client_absent_response_identity_remains_unclaimed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL)

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

    monkeypatch.setattr("core.model_client.urlopen", lambda *_args, **_kwargs: _Response())
    client.chat_completion([], model_name=DEFAULT_SMART_MODEL)
    assert client.last_response_model is None


def test_bridge_health_uses_actual_model_id_and_effective_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai import ollama as ollama_module

    provider = OllamaProvider(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_SMART_MODEL, context_length=32768)
    monkeypatch.setattr(ollama_module.model_client, "is_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(
        ollama_module.model_client,
        "list_models",
        lambda **_kwargs: [{"id": MANIFEST_ID, "meta": {"n_ctx": 8192, "n_ctx_train": 262144}}],
    )
    monkeypatch.setattr(ollama_module, "_local_model_digest", lambda _path: None)

    result = provider.health()

    assert result["available"] is True
    assert result["model_available"] is True
    assert result["model_id"] == DEFAULT_SMART_MODEL
    assert result["actual_model_id"] == MANIFEST_ID
    assert result["model_identity_source"] == "verified_manifest"
    assert result["context_length"] == 8192
    assert result["configured_context_length"] == 32768
    assert result["weight_digest"] is None
    assert result["digest_status"] == "UNKNOWN_NOT_PROVIDED"


def test_bridge_health_rejects_unmatched_model_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai import ollama as ollama_module

    provider = OllamaProvider(base_url="http://127.0.0.1:8080/v1", model_name="qwen3.5:9b")
    monkeypatch.setattr(ollama_module.model_client, "is_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(ollama_module.model_client, "list_models", lambda **_kwargs: [{"id": MANIFEST_ID, "meta": {"n_ctx": 8192}}])

    result = provider.health()

    assert result["available"] is False
    assert result["model_available"] is False
    assert result["actual_model_id"] is None
    assert result["context_length"] is None
