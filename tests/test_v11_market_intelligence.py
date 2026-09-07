from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.consult import ConsultConfig, QwenConsultService
from core.instruments import instrument_for
from core.market_intelligence import build_market_intelligence
from core.model_routing import ModelRoutingConfig, route_model
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.signals import Action, build_signal
from core.storage import SQLiteStore


class FakeBriefTransport:
    provider_name = "fixture_local_qwen"
    model_name = "fixture-smart"

    async def stream(self, _messages):
        yield "Evidence-limited daily brief."


def config() -> ConsultConfig:
    return ConsultConfig(
        enabled=True,
        base_url="http://127.0.0.1:11434",
        model_name="fixture-smart",
        max_message_chars=20_000,
        max_total_message_chars=24_000,
        fast_model_name="fixture-fast",
        smart_model_name="fixture-smart",
    )


class V11MarketIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "v11.sqlite3"
        self.store = SQLiteStore(self.path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def save_nvda(self) -> datetime:
        instrument = instrument_for("NVDA")
        bars = FixtureProvider().get_bars(instrument, "1h", 120)
        quant = build_quant_snapshot(bars, "1h", symbol="NVDA")
        now = datetime.now(timezone.utc)
        data_as_of = now - timedelta(minutes=5)
        context = {
            "price": {"last": quant.price, "change_pct": 1.25},
            "quant": {**quant.to_dict(), "market_regime": "trend"},
            "provider_snapshot": {"provider": "saved_fixture", "data_as_of": data_as_of.isoformat(), "stale": False},
        }
        signal = build_signal(
            instrument,
            quant,
            generated_at=now,
            data_as_of=data_as_of,
            prediction_id="v11-nvda",
            model_id="fixture",
            context_json=json.dumps(context, sort_keys=True),
            force_action=Action.LONG,
            summary="Saved fixture evidence.",
        )
        self.store.save_instrument(instrument)
        self.store.upsert_watchlist_entry("NVDA", now=now)
        self.store.save_prediction(signal)
        return now

    def test_router_is_deterministic_and_server_owned(self) -> None:
        routing = ModelRoutingConfig("qwen-fast", "qwen-smart")
        self.assertEqual(route_model(routing, preference="auto", task="watchlist_scan").model_id, "qwen-fast")
        deep = route_model(routing, preference="auto", task="assistant")
        self.assertEqual((deep.model_id, deep.tier.value, deep.reason), ("qwen-smart", "smart", "auto_smart_for_deep_research_task"))
        self.assertEqual(route_model(routing, preference="fast", task="daily_brief").model_id, "qwen-fast")

    def test_v11_settings_and_brief_survive_restart(self) -> None:
        self.assertEqual(self.store.schema_version(), 14)
        self.store.upsert_app_setting("ui.language", "zh-CN")
        self.store.upsert_app_setting("ai.response_language", "follow_ui")
        self.store.upsert_app_setting("notifications.language", "zh-CN")
        self.store.upsert_app_setting("ai.model_preference", "smart")
        self.store.save_daily_brief({
            "brief_id": "brief-1", "generated_at": "2026-08-21T00:00:00+00:00",
            "as_of": "2026-08-21T00:00:00+00:00", "language": "zh-CN",
            "model_id": "qwen-smart", "model_tier": "smart", "route_reason": "explicit_smart_preference",
            "source_hash": "abc", "sources": ["saved"], "missing": [], "content": "本地证据简报。",
            "capability": {"explicit_generation": True},
        })
        reopened = SQLiteStore(self.path)
        reopened.initialize()
        self.assertEqual(reopened.get_app_setting("ui.language")["value"], "zh-CN")
        self.assertEqual(reopened.latest_daily_brief(language="zh-CN")["content"], "本地证据简报。")

    def test_market_intelligence_get_is_read_only_and_missing_values_are_honest(self) -> None:
        now = self.save_nvda()
        before = self.store.counts()
        first = build_market_intelligence(self.store, as_of=now)
        second = build_market_intelligence(self.store, as_of=now)
        self.assertEqual(first, second)
        self.assertEqual(self.store.counts(), before)
        pulse = {item["symbol"]: item for item in first["pulse"]}
        self.assertEqual(pulse["NVDA"]["status"], "available")
        self.assertEqual(pulse["NASDAQ"]["status"], "unavailable")
        self.assertIsNone(pulse["NASDAQ"]["price"])

    def test_api_daily_brief_is_explicit_and_routes_to_smart_without_domain_writes(self) -> None:
        self.save_nvda()
        service = QwenConsultService(config(), transport=FakeBriefTransport())
        client = TestClient(create_app(store=self.store, llm_provider=None, consult_service=service))
        before = self.store.counts()
        view = client.get("/market-intelligence")
        self.assertEqual(view.status_code, 200)
        self.assertTrue(view.json()["read_only"])
        self.assertEqual(self.store.counts(), before)
        generated = client.post("/daily-brief/generate", json={"language": "en", "model_preference": "auto"})
        self.assertEqual(generated.status_code, 200)
        brief = generated.json()["brief"]
        self.assertEqual(brief["model_id"], "fixture-smart")
        self.assertEqual(brief["model_tier"], "smart")
        after = self.store.counts()
        self.assertEqual(after["daily_briefs"], before["daily_briefs"] + 1)
        for table in ("predictions", "paper_trades", "outcomes", "calibration_results"):
            self.assertEqual(after[table], before[table])


if __name__ == "__main__":
    unittest.main()
