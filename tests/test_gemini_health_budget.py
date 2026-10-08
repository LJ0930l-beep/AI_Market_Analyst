"""Bounded High health probes; transport failures do not prove identity drift."""
from types import SimpleNamespace

import pytest

from core.ai import ollama
from core.model_client import ModelTimeoutError
from core.model_routing import DEFAULT_MODEL


def install_probe(monkeypatch, *, elapsed=12.679, failure=None):
    provider = ollama.OllamaProvider()
    clock = [100.0]
    calls = []
    monkeypatch.setattr(provider, "_route_error", lambda target=None: None)
    monkeypatch.setattr(ollama, "_GEMINI_HEALTH_CACHE", {})
    monkeypatch.setattr(ollama.time, "monotonic", lambda: clock[0])

    def manifest(**kwargs):
        calls.append(("manifest", kwargs["timeout"]))
        clock[0] += 2
        return [{"id": DEFAULT_MODEL}]

    def completion(*args, **kwargs):
        calls.append(("probe", kwargs["timeout_sec"]))
        if failure:
            raise failure
        clock[0] += elapsed
        return {"ok": True}

    monkeypatch.setattr(ollama, "model_client", SimpleNamespace(
        list_models=manifest, structured_analysis=completion, last_response_model=DEFAULT_MODEL))
    return provider, calls


def test_high_probe_longer_than_ten_seconds_is_still_verified_within_shared_budget(monkeypatch):
    provider, calls = install_probe(monkeypatch)
    result = provider.health()
    assert result["model_available"] is True
    assert result["actual_model_id"] == DEFAULT_MODEL
    assert result["model_identity_source"] == "completion_probe"
    assert calls == [("manifest", 5), ("probe", 28)]


@pytest.mark.parametrize("budget,elapsed", [(6, 5), (None, 29)])
def test_probe_cannot_exceed_whole_health_budget(monkeypatch, budget, elapsed):
    provider, calls = install_probe(monkeypatch, elapsed=elapsed)
    result = provider.health(timeout_sec=budget)
    assert result["model_available"] is False
    assert result["actual_model_id"] is None
    assert result["error_type"] == "TimeoutError"
    assert result["error_code"] == "MODEL_HEALTH_DEADLINE_EXCEEDED"
    assert calls[1][1] == (budget or 30) - 2


def test_transport_error_is_preserved_without_an_identity_or_fallback(monkeypatch):
    provider, calls = install_probe(monkeypatch, failure=ModelTimeoutError("timed out"))
    result = provider.health()
    assert result["model_available"] is False and result["actual_model_id"] is None
    assert result["error_type"] == "ModelTimeoutError"
    assert result["error_code"] == "timed out"
    assert len(calls) == 2
