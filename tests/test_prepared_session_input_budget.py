"""Offline lookup checks do not modify actual environment or run any model."""
import pytest
from core.trading.ai_session_coordinator import _session_context_length, MODEL_CONTEXT_LENGTH
from scripts.prepare_session_input_budget_patch import prepared_lookup, prepare, ROOT, TARGET


def test_activated_scan_reads_canonical_budget_with_unchanged_default(monkeypatch):
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    monkeypatch.setenv("AIMA_MODEL_INPUT_BUDGET", "12288")
    assert MODEL_CONTEXT_LENGTH == 8192
    assert _session_context_length() == 12288


def test_canonical_budget_takes_precedence_without_changing_source_or_old_default(monkeypatch):
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "8192")
    monkeypatch.setenv("AIMA_MODEL_INPUT_BUDGET", "12288")
    assert _session_context_length() == 12288
    monkeypatch.delenv("AIMA_MODEL_INPUT_BUDGET")
    assert _session_context_length() == 8192
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH")
    assert _session_context_length() == MODEL_CONTEXT_LENGTH


def test_canonical_invalid_budget_is_explicit_not_silently_replaced(monkeypatch):
    for raw in ("", "bad", "0", "-1", "12.5"):
        monkeypatch.setenv("AIMA_MODEL_INPUT_BUDGET", raw)
        with pytest.raises(ValueError, match="AI_APPLICATION_INPUT_BUDGET_INVALID"):
            _session_context_length()


def test_prepared_budget_reaches_instance_scoped_health_without_network(monkeypatch):
    from tests.test_prepared_provider_health_budget import install
    monkeypatch.setenv("AIMA_MODEL_INPUT_BUDGET", "12288")
    provider, _ = install(monkeypatch, cached_available=False, budget=_session_context_length())
    result = provider.health()
    assert result["context_length"] == result["configured_context_length"] == 12288
    assert result["available"] is result["model_available"] is False


def test_prepare_receipt_never_changes_live_source_or_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMA_MODEL_INPUT_BUDGET", "8192")
    before = (ROOT / TARGET).read_bytes()
    with pytest.raises(ValueError, match="PATCH_ANCHOR_CHANGED"):
        prepare(tmp_path)
    assert (ROOT / TARGET).read_bytes() == before
    assert _session_context_length() == 8192
