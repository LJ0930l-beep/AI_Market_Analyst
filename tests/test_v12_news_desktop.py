from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.desktop_runtime import import_legacy_database, ownership_fingerprint, verify_ownership
from core.news_translation import NewsTranslationService, numeric_guard, sanitize_untrusted_text
from core.providers.news import NewsEvent
from core.storage import SQLiteStore


class FakeFastTranslator:
    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, messages, **kwargs):
        self.calls += 1
        return {
            "title_zh": "CPI 数据公布：2026-08-30 为 3.5%",
            "summary_zh": "BTCUSDT 现货为 3.5%，预测为 3.2%。",
        }, "{}", {"model_id": "qwen3.5:4b", "latency_ms": 1.0}


def test_news_translation_cache_preserves_original_and_numeric_tokens(tmp_path: Path) -> None:
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
    translator = FakeFastTranslator()
    service = NewsTranslationService(store=store, llm_provider=translator)
    first = service.translate(event)
    second = service.translate(event)
    assert first.status == "translated"
    assert first.numeric_guard_passed is True
    assert second == first
    assert translator.calls == 1
    assert first.original_title == event.title
    assert first.evidence["numeric_guard"]["passed"] is True
    assert numeric_guard("value -3.5% on BTCUSDT", "值 -3.5% 于 BTCUSDT").passed
    assert numeric_guard("Order flow at Aug. 14, 7 p.m. UTC", "8月14日 UTC 下午7点的订单流").passed
    assert not numeric_guard("Order flow at Aug. 14", "8月14日新增9个订单").passed


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
    assert artifact.status == "failed_numeric_guard"
    assert artifact.numeric_guard_passed is False
    assert artifact.translated_title_zh == "没有数值"


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
    assert SQLiteStore(destination).schema_version() == 13
    connection = sqlite3.connect(destination)
    assert connection.execute("SELECT value FROM legacy_marker").fetchone()[0] == "v11"
    connection.close()
    second = import_legacy_database(source=source, destination=destination)
    assert second.reason == "destination_exists"

    started = datetime(2030, 1, 2, tzinfo=timezone.utc)
    record = ownership_fingerprint(pid=42, executable=tmp_path / "sidecar.exe", started_at=started, command_line="sidecar.exe --port 18765")
    assert verify_ownership(record, pid=42, executable=tmp_path / "sidecar.exe", started_at=started, command_line="sidecar.exe --port 18765")
    assert not verify_ownership(record, pid=43, executable=tmp_path / "sidecar.exe", started_at=started, command_line="sidecar.exe --port 18765")
