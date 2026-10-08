"""Offline cache defect reproduction; no model call or running-source mutation."""
from copy import deepcopy
from types import SimpleNamespace
import pytest

from core.ai import ollama
from core.model_routing import DEFAULT_MODEL
from scripts.prepare_model_health_budget_patch import prepared_health, prepare, ROOT, TARGET


def install(monkeypatch, *, cached_available=True, budget=32768):
    provider = ollama.OllamaProvider(context_length=budget)
    monkeypatch.setattr(provider, "_route_error", lambda target=None: None)
    monkeypatch.setattr(ollama.time, "monotonic", lambda: 101.0)
    cached = {"available": cached_available, "model_available": cached_available,
        "actual_model_id": DEFAULT_MODEL if cached_available else None,
        "model_identity_source": "completion_probe" if cached_available else None,
        "context_length": 8192, "configured_context_length": 8192,
        "context_length_source": "APPLICATION_INPUT_BUDGET", "weight_digest": None,
        "digest_status": "REMOTE_WEIGHTS_NOT_EXPOSED", "models": [DEFAULT_MODEL],
        "error_code": None if cached_available else "MODEL_UNAVAILABLE",
        "rejected_legacy_overrides": ["other_adapter_override"]}
    monkeypatch.setattr(ollama, "_GEMINI_HEALTH_CACHE", {(provider.base_url, DEFAULT_MODEL): (100.0, cached)})
    # Cache-hit paths must not reach any model method, even for unavailable health.
    monkeypatch.setattr(ollama, "model_client", SimpleNamespace())
    return provider, cached


def test_actual_health_uses_own_cross_adapter_application_budget(monkeypatch):
    provider, _ = install(monkeypatch)
    current = provider.health()
    assert provider.context_length == current["context_length"] == 32768


def test_default_adapter_uses_same_canonical_budget_as_scan_configuration(monkeypatch):
    from core.trading.ai_session_coordinator import _session_context_length
    monkeypatch.setenv('AIMA_MODEL_INPUT_BUDGET', '12288')
    monkeypatch.setenv('OLLAMA_CONTEXT_LENGTH', '8192')
    assert ollama.OllamaProvider().context_length == _session_context_length() == 12288
    assert ollama.OllamaProvider(context_length=16384).context_length == 16384
    for invalid in ('', 'bad', '0', '-1'):
        monkeypatch.setenv('AIMA_MODEL_INPUT_BUDGET', invalid)
        with pytest.raises(ValueError):
            ollama.OllamaProvider()


def test_prepared_health_uses_instance_budget_preserves_verified_identity_and_cache(monkeypatch):
    provider, cached = install(monkeypatch)
    original = deepcopy(cached)
    result = provider.health()
    assert result["context_length"] == result["configured_context_length"] == provider.context_length
    assert result["context_length_source"] == "APPLICATION_INPUT_BUDGET"
    assert result["actual_model_id"] == DEFAULT_MODEL and result["model_identity_source"] == "completion_probe"
    assert result["rejected_legacy_overrides"] == list(provider.rejected_legacy_overrides)
    result["models"].append("caller-mutation")
    assert cached == original


def test_unavailable_cache_cannot_be_upgraded_to_healthy_by_budget_projection(monkeypatch):
    provider, cached = install(monkeypatch, cached_available=False, budget=16384)
    result = provider.health()
    assert result["available"] is False and result["model_available"] is False
    assert result["actual_model_id"] is None and result["error_code"] == cached["error_code"]
    assert result["context_length"] == 16384


def test_health_preparation_does_not_modify_running_source(tmp_path):
    before = (ROOT / TARGET).read_bytes()
    with pytest.raises(ValueError, match="PATCH_ANCHOR_CHANGED"):
        prepare(tmp_path)
    assert (ROOT / TARGET).read_bytes() == before
