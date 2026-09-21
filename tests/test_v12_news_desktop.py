from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.ai.ollama import OllamaProvider
from core.config import AppDataPaths
from core.desktop_runtime import (
    import_legacy_database,
    ownership_fingerprint,
    process_is_alive,
    remove_runtime_manifest_if_owned,
    verify_ownership,
    write_runtime_manifest,
)
from core.model_routing import DEFAULT_FAST_MODEL
from core.news_translation import NewsTranslationService, numeric_guard, sanitize_untrusted_text, source_hash
from core.providers.news import NewsEvent
from core.storage import SQLiteStore


def _bonsai_health(manifest_id: str = "Ternary-Bonsai-2-27B-PTQ1_0.gguf") -> dict[str, object]:
    return {
        "available": True,
        "model_available": True,
        "model_id": DEFAULT_FAST_MODEL,
        "actual_model_id": manifest_id,
        "model_identity_source": "verified_manifest",
        "models": [manifest_id],
    }


def _bonsai_receipt(kwargs: dict[str, object], manifest_id: str = "Ternary-Bonsai-2-27B-PTQ1_0.gguf") -> dict[str, object]:
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


def test_news_translation_cache_preserves_original_and_numeric_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(tmp_path / "news.sqlite3")
    store.initialize()
    event = NewsEvent(
        event_id="news-1",
        source="First Party Wire",
        published_at=datetime(2030, 1, 2, 12, tzinfo=timezone.utc),
        title="CPI release 2026-08-30: 3.5% vs 3.2% for BTCUSDT",
        summary_raw="The official release reports 3.5% actual and 3.2% forecast.",
        symbols=("BTCUSDT",),
    )
    translator = OllamaProvider()
    monkeypatch.setattr(translator, "health", lambda **_kwargs: _bonsai_health())
    calls = 0

    def generate_json(_messages, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "title_zh": "CPI 数据公布：2026-08-30 为 3.5%",
            "summary_zh": "BTCUSDT 现货为 3.5%，预测为 3.2%。",
        }, "{}", _bonsai_receipt(kwargs)

    monkeypatch.setattr(translator, "generate_json", generate_json)
    service = NewsTranslationService(store=store, llm_provider=translator)
    first = service.translate(event)
    second = service.translate(event)
    assert first.status == "translated"
    assert first.numeric_guard_passed is True
    assert second == first
    assert calls == 1
    assert first.original_title == event.title
    assert first.evidence["numeric_guard"]["passed"] is True
    assert numeric_guard("value -3.5% on BTCUSDT", "值 -3.5% 于 BTCUSDT").passed
    assert numeric_guard("Order flow at Aug. 14, 7 p.m. UTC", "8月14日 UTC 下午7点的订单流").passed
    assert not numeric_guard("Order flow at Aug. 14", "8月14日新增9个订单").passed
    breaking_title = "🇺🇸 JUST IN: Sen. Cynthia Lummis says Democrats voted against a crypto bill."
    assert numeric_guard(breaking_title, "🇺🇸 突发：参议员辛西娅·卢米斯称，民主党人投票反对一项加密法案。").passed
    assert numeric_guard("JUST IN: BTCUSDT is up", "突发：BTCUSDT 上涨").passed


@pytest.mark.parametrize(
    "manifest_id",
    [
        "Ternary-Bonsai-2-27B-PTQ1_0.gguf",
        r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf",
    ],
)
def test_news_translation_accepts_verified_bonsai_manifest_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_id: str,
) -> None:
    provider = OllamaProvider()
    health = {
        "available": True,
        "model_available": True,
        "model_id": DEFAULT_FAST_MODEL,
        "actual_model_id": manifest_id,
        "model_identity_source": "verified_manifest",
        "models": [manifest_id],
    }
    monkeypatch.setattr(provider, "health", lambda **_kwargs: health)
    calls: list[dict[str, object]] = []

    def generate_json(_messages, **kwargs):
        calls.append(kwargs)
        return {
            "title_zh": "CPI 3.5%",
            "summary_zh": "BTCUSDT 3.5%",
        }, "{}", _bonsai_receipt(kwargs, manifest_id)

    monkeypatch.setattr(provider, "generate_json", generate_json)
    service = NewsTranslationService(store=SQLiteStore(tmp_path / "news.sqlite3"), llm_provider=provider)
    payload, raw, metadata = service._call_fast_model(
        [{"role": "user", "content": "translate"}],
        input_hash="test-hash",
    )

    assert payload["title_zh"] == "CPI 3.5%"
    assert raw == "{}"
    assert metadata["model_id"] == DEFAULT_FAST_MODEL
    assert calls[0]["model_name"] == DEFAULT_FAST_MODEL


def test_news_source_is_sanitized_before_model_and_failed_guard_is_visible(tmp_path: Path) -> None:
    assert sanitize_untrusted_text("<script>alert(1)</script><b>safe</b>") == "safe"
    assert not numeric_guard("3.5% BTCUSDT", "3.4% BTCUSDT").passed
    store = SQLiteStore(tmp_path / "news-failed.sqlite3")
    store.initialize()

    class BadTranslator:
        def generate_json(self, messages, **kwargs):
            return {"title_zh": "没有数值"}, "{}", {}

    event = NewsEvent(
        event_id="news-2",
        source="Wire",
        published_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
        title="BTCUSDT reaches 42,000 USD",
        symbols=("BTCUSDT",),
    )
    artifact = NewsTranslationService(store=store, llm_provider=BadTranslator()).translate(event)
    assert artifact.status == "failed"
    assert artifact.numeric_guard_passed is False
    assert artifact.translated_title_zh is None
    assert artifact.model_id == "UNVERIFIED"


def test_translation_rejects_forged_receipt_and_does_not_cache_bonsai_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = OllamaProvider()
    monkeypatch.setattr(provider, "health", lambda **_kwargs: _bonsai_health())
    calls = 0

    def forged_generate_json(_messages, **kwargs):
        nonlocal calls
        calls += 1
        receipt = _bonsai_receipt(kwargs)
        receipt["input_hash"] = "another-request"
        return {"title_zh": "CPI 3.5%", "summary_zh": "BTCUSDT 3.5%"}, "{}", receipt

    monkeypatch.setattr(provider, "generate_json", forged_generate_json)
    store = SQLiteStore(tmp_path / "forged-news.sqlite3")
    store.initialize()
    event = NewsEvent(
        event_id="forged-news",
        source="Wire",
        published_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
        title="CPI 3.5% for BTCUSDT",
        symbols=("BTCUSDT",),
    )

    artifact = NewsTranslationService(store=store, llm_provider=provider).translate(event)

    assert calls == 1
    assert artifact.status == "failed"
    assert artifact.model_id == "UNVERIFIED"
    assert artifact.translated_title_zh is None
    cached = store.list_localized_news_artifacts(news_id=event.event_id, locale="zh-CN", limit=1)[0]
    assert cached["model_id"] == "UNVERIFIED"


def test_translation_ignores_legacy_success_cache_without_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SQLiteStore(tmp_path / "legacy-news.sqlite3")
    store.initialize()
    event = NewsEvent(
        event_id="legacy-news",
        source="Wire",
        published_at=datetime(2030, 1, 2, tzinfo=timezone.utc),
        title="CPI 3.5% for BTCUSDT",
        symbols=("BTCUSDT",),
    )
    digest = source_hash(
        source=event.source,
        title=event.title,
        summary=None,
        published_at=event.published_at.isoformat(),
        structured_values={},
    )
    store.save_localized_news_artifact({
        "news_id": event.event_id,
        "locale": "zh-CN",
        "source_language": "en",
        "original_title": event.title,
        "original_summary": None,
        "translated_title_zh": "伪造的缓存标题 CPI 3.5% BTCUSDT",
        "translated_summary_zh": None,
        "evidence": {},
        "source_hash": digest,
        "model_id": DEFAULT_FAST_MODEL,
        "prompt_version": "news_translation_v1",
        "translated_at": datetime(2030, 1, 2, tzinfo=timezone.utc).isoformat(),
        "numeric_guard_passed": True,
        "status": "translated",
    })

    provider = OllamaProvider()
    monkeypatch.setattr(provider, "health", lambda **_kwargs: _bonsai_health())
    calls = 0

    def generate_json(_messages, **kwargs):
        nonlocal calls
        calls += 1
        return {"title_zh": "CPI 数据 3.5% BTCUSDT"}, "{}", _bonsai_receipt(kwargs)

    monkeypatch.setattr(provider, "generate_json", generate_json)
    artifact = NewsTranslationService(store=store, llm_provider=provider).translate(event)

    assert calls == 1
    assert artifact.status == "translated"
    assert artifact.translated_title_zh == "CPI 数据 3.5% BTCUSDT"
    assert artifact.evidence["model_metadata"]["input_hash"]


def test_explicit_legacy_import_and_owned_process_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    destination = tmp_path / "appdata" / "data" / "market_analyst.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE legacy_marker(value TEXT)")
    connection.execute("INSERT INTO legacy_marker VALUES ('v11')")
    connection.commit()
    connection.close()
    report = import_legacy_database(source=source, destination=destination)
    assert report.imported is True
    assert SQLiteStore(destination).schema_version() == 14
    connection = sqlite3.connect(destination)
    assert connection.execute("SELECT value FROM legacy_marker").fetchone()[0] == "v11"
    connection.close()
    second = import_legacy_database(source=source, destination=destination)
    assert second.reason == "destination_exists"

    started = datetime(2030, 1, 2, tzinfo=timezone.utc)
    record = ownership_fingerprint(pid=42, executable=tmp_path / "sidecar.exe", started_at=started, command_line="sidecar.exe --port 18765")
    assert verify_ownership(record, pid=42, executable=tmp_path / "sidecar.exe", started_at=started, command_line="sidecar.exe --port 18765")
    assert not verify_ownership(record, pid=43, executable=tmp_path / "sidecar.exe", started_at=started, command_line="sidecar.exe --port 18765")


def test_runtime_manifest_cleanup_is_generation_scoped(tmp_path: Path) -> None:
    paths = AppDataPaths(
        root=tmp_path,
        data=tmp_path / "data",
        logs=tmp_path / "logs",
        backups=tmp_path / "backups",
        runtime=tmp_path / "runtime",
    )
    paths.runtime.mkdir(parents=True)
    sidecar = {
        "version": "owned_sidecar_v1",
        "pid": 42,
        "instance_id": "instance-a",
        "executable": "sidecar.exe",
        "started_at_utc": "2030-01-02T00:00:00+00:00",
        "command_line_sha256": "0" * 64,
    }
    manifest = write_runtime_manifest(paths, sidecar=sidecar, port=18765)
    assert not remove_runtime_manifest_if_owned(paths, pid=42, instance_id="instance-b")
    assert manifest.exists()
    assert remove_runtime_manifest_if_owned(paths, pid=42, instance_id="instance-a")
    assert not manifest.exists()
    assert process_is_alive(os.getpid())
    assert not process_is_alive(2_147_483_647)
