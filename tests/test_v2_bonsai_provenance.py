from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.ai.ollama import OllamaProvider
from core.model_routing import DEFAULT_FAST_MODEL
from core.news_translation import NewsTranslationService
from core.storage import SQLiteStore


def _client(tmp_path, provider: object | None = None, translation_service: object | None = None) -> tuple[TestClient, SQLiteStore]:
    store = SQLiteStore(tmp_path / "macro-provenance.sqlite3")
    store.initialize()
    service = translation_service or NewsTranslationService(store=store, llm_provider=provider)
    app = FastAPI()
    app.include_router(router_for(lambda: store, lambda: None, lambda: service))
    return TestClient(app, headers={"Host": "localhost", "Origin": "http://localhost:5173"}), store


def _payload() -> dict[str, Any]:
    known_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    return {
        "event_id": "macro-cpi-provenance",
        "title": "US CPI release",
        "source_url": "https://www.bls.gov/cpi/",
        "event_time": known_at,
        "known_at": known_at,
        "forecast": "3.2%",
        "actual": "3.5%",
    }


def _verified_health(**_kwargs: object) -> dict[str, object]:
    manifest_id = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    return {
        "available": True,
        "model_available": True,
        "model_id": DEFAULT_FAST_MODEL,
        "actual_model_id": manifest_id,
        "model_identity_source": "verified_manifest",
        "models": [manifest_id],
    }


def _receipt(kwargs: dict[str, object]) -> dict[str, object]:
    manifest_id = "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    return {
        "model_id": DEFAULT_FAST_MODEL,
        "model_version": manifest_id,
        "actual_model_id": manifest_id,
        "model_identity_source": "request_bound_to_verified_manifest",
        "verified_manifest_model_id": manifest_id,
        "prompt_version": kwargs["prompt_version"],
        "input_hash": kwargs["input_hash"],
        "latency_ms": 1.0,
        "parse_status": "valid",
    }


class InjectedModel:
    calls = 0

    def generate_json(self, *_args: object, **_kwargs: object):
        self.calls += 1
        return {
            "directive": "FORBID_LONG",
            "valid_duration_minutes": 60,
            "summary": "forged result",
            "counterevidence": [],
        }, "{}", {"model_id": DEFAULT_FAST_MODEL}


class InjectedTranslationService:
    llm_provider = object()
    calls = 0

    def _call_fast_model(self, *_args: object, **_kwargs: object):
        self.calls += 1
        return {
            "directive": "FORBID_LONG",
            "valid_duration_minutes": 60,
            "summary": "forged service result",
            "counterevidence": [],
        }, "{}", _receipt({"prompt_version": "macro_guard_v2", "input_hash": "fake"})


def test_macro_event_does_not_call_or_label_injected_provider_as_bonsai(tmp_path) -> None:
    provider = InjectedModel()
    client, _store = _client(tmp_path, provider)

    response = client.post("/v2/macro-events", json=_payload())

    assert response.status_code == 200
    event = response.json()
    assert provider.calls == 0
    assert event["ai_status"] == "MODEL_OR_SCHEMA_UNAVAILABLE"
    assert event["directive"] == "NONE"
    assert "model_id" not in event


def test_macro_event_rejects_injected_translation_service(tmp_path) -> None:
    service = InjectedTranslationService()
    client, _store = _client(tmp_path, translation_service=service)

    response = client.post("/v2/macro-events", json=_payload())

    assert response.status_code == 200
    event = response.json()
    assert service.calls == 0
    assert event["ai_status"] == "MODEL_OR_SCHEMA_UNAVAILABLE"
    assert "model_id" not in event


def test_macro_event_requires_receipt_bound_to_exact_prompt(tmp_path, monkeypatch) -> None:
    provider = OllamaProvider()
    monkeypatch.setattr(provider, "health", _verified_health)
    calls = 0

    def forged_generate(_messages, **kwargs):
        nonlocal calls
        calls += 1
        receipt = _receipt(kwargs)
        receipt["input_hash"] = "other-request"
        return {
            "directive": "FORBID_LONG",
            "valid_duration_minutes": 60,
            "summary": "should be rejected",
            "counterevidence": [],
        }, "{}", receipt

    monkeypatch.setattr(provider, "generate_json", forged_generate)
    client, store = _client(tmp_path, provider)

    response = client.post("/v2/macro-events", json=_payload())

    assert response.status_code == 200
    event = response.json()
    assert calls == 1
    assert event["ai_status"] == "MODEL_OR_SCHEMA_UNAVAILABLE"
    assert event["directive"] == "NONE"
    assert "model_id" not in event
    assert "model_id" not in store.v2_records("macro_events")[0]


def test_macro_event_persists_bonsai_label_only_with_verified_receipt(tmp_path, monkeypatch) -> None:
    provider = OllamaProvider()
    monkeypatch.setattr(provider, "health", _verified_health)
    calls: list[dict[str, object]] = []

    def valid_generate(_messages, **kwargs):
        calls.append(kwargs)
        return {
            "directive": "FORBID_LONG",
            "valid_duration_minutes": 60,
            "summary": "CPI surprised to the upside.",
            "counterevidence": ["one-month reading may be noisy"],
        }, "{}", _receipt(kwargs)

    monkeypatch.setattr(provider, "generate_json", valid_generate)
    client, store = _client(tmp_path, provider)

    response = client.post("/v2/macro-events", json=_payload())

    assert response.status_code == 200
    event = response.json()
    assert len(calls) == 1
    assert calls[0]["model_name"] == DEFAULT_FAST_MODEL
    assert calls[0]["prompt_version"] == "macro_guard_v2"
    assert calls[0]["temperature"] == 0.0
    assert event["ai_status"] == "VALIDATED_RULE"
    assert event["directive"] == "FORBID_LONG"
    assert event["model_id"] == DEFAULT_FAST_MODEL
    assert event["model_metadata"]["input_hash"] == calls[0]["input_hash"]
    assert event["model_metadata"]["model_identity_source"] == "request_bound_to_verified_manifest"
    assert store.v2_records("macro_events")[0]["model_id"] == DEFAULT_FAST_MODEL
