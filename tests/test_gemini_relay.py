"""Model migration boundaries; no broker, account database or network access."""
import io
import json
from urllib.error import HTTPError

import pytest

from core.model_client import ModelClient, ModelClientError
from core.model_routing import (
    DEFAULT_MODEL, configured_manifest_entry_matches, is_configured_model_identity,
    is_verified_model_receipt, is_verified_bonsai_receipt,
)


class Response(io.BytesIO):
    status = 200


def test_gemini_schema_keeps_array_type_and_required_wait_condition_without_merging_actions():
    from core.trading.model_schemas import frozen_decision_schema, gemini_response_format, validate_schema
    frozen = frozen_decision_schema({"allowed_instruments": ["BTCUSDT"], "evidence_refs": []}, gate_wait=True)
    projected = gemini_response_format(frozen)["json_schema"]["schema"]
    assert "anyOf" not in projected
    assert projected["properties"]["evidence_refs"]["type"] == "array"
    assert "enum" not in projected["properties"]["evidence_refs"]
    assert projected["properties"]["confidence"]["type"] == "number"
    assert "entry_condition" in projected["required"]
    assert projected["properties"]["entry_condition"]["type"] == "string"
    assert "entry_condition" not in projected["properties"]["strategy_analysis"]["properties"]
    assert "strategy_analysis" in projected["required"]
    assert projected["properties"]["strategy_analysis"]["type"] == "object"
    assert "missing_conditions" in projected["properties"]["strategy_analysis"]["required"]
    with pytest.raises(ValueError):
        validate_schema({"action": "WAIT", "instrument_id": "BTCUSDT", "reason": "unknown", "confidence": 0}, frozen)


@pytest.mark.parametrize("action", ["OPEN_LONG", "HOLD", "CANCEL_ORDER"])
def test_gemini_wait_container_does_not_force_missing_conditions_for_other_actions(action):
    from core.trading.model_schemas import frozen_decision_schema, gemini_response_format, validate_schema
    frozen = frozen_decision_schema({"allowed_instruments": ["BTCUSDT"], "evidence_refs": []}, gate_wait=True)
    decision = {"action": action, "instrument_id": "BTCUSDT", "reason": "model decision", "confidence": 75,
                "entry_condition": "model-authored condition", "strategy_analysis": {"missing_conditions": []}}
    validate_schema(decision, gemini_response_format(frozen)["json_schema"]["schema"])
    validate_schema(decision, frozen)


def test_gemini_wait_container_preserves_full_wait_semantics_and_input_schema():
    from copy import deepcopy
    from core.trading.model_schemas import frozen_decision_schema, gemini_response_format, validate_schema
    frozen = frozen_decision_schema({"allowed_instruments": ["BTCUSDT"], "evidence_refs": []}, gate_wait=True)
    before = deepcopy(frozen)
    native = gemini_response_format(frozen)["json_schema"]["schema"]
    decision = {"action": "WAIT", "instrument_id": "BTCUSDT", "reason": "wait", "confidence": 0,
                "entry_condition": "wait for confirmed breakout", "strategy_analysis": {"missing_conditions": []}}
    validate_schema(decision, native)
    with pytest.raises(ValueError, match="anyOf"):
        validate_schema(decision, frozen)
    decision["strategy_analysis"]["missing_conditions"] = ["closed breakout not confirmed"]
    validate_schema(decision, native)
    validate_schema(decision, frozen)
    assert frozen == before


def test_manifest_selects_requested_alias_without_ambiguous_variant_matches():
    rows = [{"id": name} for name in [DEFAULT_MODEL, "gemini-3.8-flash", "gemini-3.8-flash-low"]]
    assert [r for r in rows if configured_manifest_entry_matches(r)] == [rows[0]]
    assert not configured_manifest_entry_matches({"id": "gemini-3.7-flash", "aliases": [DEFAULT_MODEL]})


def test_api_reports_current_high_probe_as_available_and_rejects_a_downgrade(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    from core.storage import SQLiteStore
    from core.ai.ollama import OllamaProvider
    store = SQLiteStore(tmp_path / "isolated-api.sqlite3")
    store.initialize()
    provider = OllamaProvider()
    health = {"provider": "antigravity_gemini", "available": True, "model_available": True,
              "model_id": DEFAULT_MODEL, "actual_model_id": DEFAULT_MODEL,
              "model_identity_source": "completion_probe"}
    monkeypatch.setattr(provider, "health", lambda **kwargs: dict(health))
    app = create_app(store=store, llm_provider=provider, market_hydration_enabled=False,
                     daily_brief_schedule_enabled=False)
    client = TestClient(app)
    result = client.get("/health/model").json()
    assert result["available"] is True and result["consult"]["available"] is True
    assert result["model_id"] == result["actual_model_id"] == DEFAULT_MODEL
    assert result["checked_at"]
    health["actual_model_id"] = "gemini-3.8-flash-low"
    result = client.get("/health/model").json()
    assert result["available"] is False and result["consult"]["available"] is False


@pytest.mark.parametrize("identity", ["gemini-3.8-flash-low", "gemini-3.7-flash", "Bonsai-2-27B-PTQ1_0", "gemini-3.8-flash-fake", "foo/gemini-3.8-flash"])
def test_wrong_release_or_disguised_identity_is_not_accepted(identity):
    assert not is_configured_model_identity(identity)


def test_receipt_is_bound_to_completed_remote_response_and_request():
    receipt = {"model_id": DEFAULT_MODEL, "actual_model_id": DEFAULT_MODEL,
               "model_version": DEFAULT_MODEL, "verified_manifest_model_id": DEFAULT_MODEL,
               "model_identity_source": "completion_response", "prompt_version": "test-v1",
               "input_hash": "a" * 64, "parse_status": "valid"}
    assert is_verified_model_receipt(receipt, expected_prompt_version="test-v1", expected_input_hash="a" * 64)
    assert not is_verified_bonsai_receipt(receipt)
    assert not is_verified_model_receipt(receipt, expected_prompt_version="test-v1", expected_input_hash="b" * 64)
    assert not is_verified_model_receipt({**receipt, "model_identity_source": "request_bound_to_verified_manifest"})
    assert not is_verified_model_receipt({**receipt, "actual_model_id": "gemini-3.5-flash-low"})


def test_client_uses_relay_and_maps_legacy_no_thinking_to_supported_low(monkeypatch):
    captured = []
    def open_response(request, **kwargs):
        captured.append(json.loads(request.data))
        return Response(json.dumps({"model": DEFAULT_MODEL, "choices": [{"message": {"content": '{"ok":true}'}}]}).encode())
    monkeypatch.setattr("core.model_client.urlopen", open_response)
    client = ModelClient(retries=0)
    assert client.structured_analysis([{"role": "user", "content": "connection only"}], reasoning_effort="none") == {"ok": True}
    assert captured[0]["model"] == DEFAULT_MODEL
    assert captured[0]["reasoning_effort"] == "high"
    assert "top_k" not in captured[0]
    assert client.last_response_model == DEFAULT_MODEL
    assert ModelClient(base_url="http://127.0.0.1:8080/v1")._configuration_error() == "MODEL_ENDPOINT_NOT_ALLOWED"
    assert ModelClient(base_url="http://evil.example:8045/v1")._configuration_error() == "MODEL_ENDPOINT_NOT_ALLOWED"


@pytest.mark.parametrize("returned", [None, "gemini-3.5-flash-low", "Bonsai-2-27B-PTQ1_0"])
def test_success_http_cannot_hide_missing_or_downgraded_model(monkeypatch, returned):
    monkeypatch.setattr("core.model_client.urlopen", lambda *args, **kwargs: Response(json.dumps({"model": returned, "choices": []}).encode()))
    client = ModelClient(retries=0)
    with pytest.raises(ModelClientError, match="MODEL_RESPONSE_IDENTITY_MISMATCH"):
        client.chat_completion([{"role": "user", "content": "test"}])
    assert client.last_response_model is None


def test_region_failure_is_named_and_never_retried_or_replaced(monkeypatch):
    calls = []
    def refuse(request, **kwargs):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(json.dumps({"error": {"message": "User location is not supported for the API use."}}).encode()))
    monkeypatch.setattr("core.model_client.urlopen", refuse)
    with pytest.raises(ModelClientError, match="MODEL_UPSTREAM_REGION_UNSUPPORTED"):
        ModelClient(retries=2).chat_completion([{"role": "user", "content": "test"}])
    assert calls == ["http://127.0.0.1:8045/v1/chat/completions"]
