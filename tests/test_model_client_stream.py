from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from core import model_client as model_client_module
from core.model_client import ModelClient, ModelClientError
from core.model_routing import DEFAULT_MODEL


class _JsonResponse:
    status = 200

    def __init__(self, value: dict[str, object]):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.value).encode("utf-8")


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        yield b'data: {"model":"Bonsai-2-27B-PTQ1_0","choices":[{"delta":{"content":"ok"}}]}\n'
        yield b"data: [DONE]\n"


def test_stream_uses_bounded_consult_output_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(model_client_module, "urlopen", fake_urlopen)
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_MODEL, retries=0)
    chunks = list(
        client.chat_completion_stream(
            [{"role": "user", "content": "hello"}],
            model_name=DEFAULT_MODEL,
            max_tokens=700,
            timeout_sec=12,
        )
    )

    assert len(chunks) == 1
    assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert captured["payload"]["model"] == DEFAULT_MODEL  # type: ignore[index]
    assert captured["payload"]["max_tokens"] == 700  # type: ignore[index]
    assert captured["timeout"] == 12
    assert client.last_response_model == DEFAULT_MODEL


def test_chat_completion_forwards_supported_reasoning_effort_only_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _JsonResponse({"choices": [{"message": {"content": "{}"}}]})

    monkeypatch.setattr(model_client_module, "urlopen", fake_urlopen)
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_MODEL, retries=0)
    client.chat_completion(
        [{"role": "user", "content": "decision"}],
        model_name=DEFAULT_MODEL,
        reasoning_effort="medium",
    )

    assert captured["payload"]["reasoning_effort"] == "medium"  # type: ignore[index]


def test_structured_analysis_forwards_provider_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_MODEL, retries=2)
    captured: dict[str, object] = {}

    def fake_chat_completion(*_args, **kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": '{"action":"WAIT"}'}}]}

    monkeypatch.setattr(client, "chat_completion", fake_chat_completion)
    result = client.structured_analysis(
        [{"role": "user", "content": "decision"}],
        model_name=DEFAULT_MODEL,
        timeout_sec=37.5,
        retries=0,
    )

    assert result == {"action": "WAIT"}
    assert captured["timeout_sec"] == 37.5
    assert captured["retries"] == 0


def test_chat_completion_honors_per_call_timeout_and_retry_count(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    def fake_urlopen(_request, *, timeout):
        timeouts.append(timeout)
        if len(timeouts) < 3:
            raise URLError("temporary failure")
        return _JsonResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(model_client_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(model_client_module.time, "sleep", lambda _seconds: None)
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_MODEL, retries=0)

    response = client.chat_completion(
        [{"role": "user", "content": "decision"}],
        model_name=DEFAULT_MODEL,
        timeout_sec=37.5,
        retries=2,
    )

    assert response["content"] == "ok"
    assert timeouts == [37.5, 37.5, 37.5]


def test_chat_completion_rejects_unknown_reasoning_effort_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_client_module, "urlopen", lambda *_args, **_kwargs: pytest.fail("unexpected network call"))
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_MODEL, retries=0)
    with pytest.raises(ModelClientError, match="MODEL_REASONING_EFFORT_INVALID"):
        client.chat_completion(
            [{"role": "user", "content": "decision"}],
            model_name=DEFAULT_MODEL,
            reasoning_effort="unknown",
        )


@pytest.mark.parametrize("response_model", ["Qwen3.5:9b", "Other-Bonsai-2-27B.gguf", None, 123, ""])
def test_stream_rejects_any_present_noncanonical_model_identity(response_model: object, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response(_Response):
        def __iter__(self):
            envelope = {"model": response_model, "choices": [{"delta": {"content": "bad"}}]}
            yield ("data: " + json.dumps(envelope) + "\n").encode("utf-8")

    monkeypatch.setattr(model_client_module, "urlopen", lambda *_args, **_kwargs: Response())
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_MODEL, retries=0)
    with pytest.raises(ModelClientError, match="MODEL_RESPONSE_IDENTITY_MISMATCH"):
        list(client.chat_completion_stream([{"role": "user", "content": "hello"}], model_name=DEFAULT_MODEL))


def test_stream_allows_missing_response_identity_for_verified_manifest_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response(_Response):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'

    monkeypatch.setattr(model_client_module, "urlopen", lambda *_args, **_kwargs: Response())
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_MODEL, retries=0)
    assert len(list(client.chat_completion_stream([{"role": "user", "content": "hello"}], model_name=DEFAULT_MODEL))) == 1
    assert client.last_response_model is None


@pytest.mark.parametrize("value", [0, -1, 2049, True, 1.5])
def test_stream_rejects_unbounded_or_invalid_max_tokens(value: object, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_urlopen(*_args, **_kwargs):
        raise AssertionError("invalid token budgets must fail before network access")

    monkeypatch.setattr(model_client_module, "urlopen", forbidden_urlopen)
    client = ModelClient(base_url="http://127.0.0.1:8080/v1", model_name=DEFAULT_MODEL, retries=0)
    with pytest.raises(ModelClientError, match="MODEL_MAX_TOKENS_INVALID"):
        list(
            client.chat_completion_stream(
                [{"role": "user", "content": "hello"}],
                model_name=DEFAULT_MODEL,
                max_tokens=value,  # type: ignore[arg-type]
            )
        )
