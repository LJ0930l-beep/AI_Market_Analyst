import json
import os
import sqlite3
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import API_VERSION, create_app, get_store
from core.ai import MockLLMProvider
from core.ai.prompts import PROMPT_VERSION
from core.analysis_service import AnalysisService
from core.instruments import instrument_for
from core.outcomes import settle_prediction
from core.providers import Bar, FixtureNewsProvider, FixtureProvider, ProviderError
from core.quant import build_quant_snapshot
from core.signals import Action, build_signal
from core.storage import SQLiteStore


class Phase4APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp.name) / "api.sqlite3")
        self.store.initialize()
        self._seed_store()
        class ExplodingNewsProvider:
            provider_name = "must_not_be_called"

            def get_events(self, *_args, **_kwargs):
                raise AssertionError("snapshot must not fetch news")

        class ExplodingModelProvider:
            provider_name = "must_not_be_called"

            def analyze_market(self, *_args, **_kwargs):
                raise AssertionError("snapshot must not invoke a model")

        self.snapshot_service = AnalysisService(
            market_provider_factory=lambda _instrument: FixtureProvider(),
            news_provider=ExplodingNewsProvider(),
            llm_provider=ExplodingModelProvider(),
            store=self.store,
        )
        self.app = create_app(
            store=self.store,
            news_provider=FixtureNewsProvider(),
            llm_provider=MockLLMProvider(),
            snapshot_service=self.snapshot_service,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.temp.cleanup()

    def _signal(
        self,
        symbol: str,
        prediction_id: str,
        *,
        action: Action,
        source_type: str = "live",
        replay_run_id: str | None = None,
        generated_at: datetime | None = None,
    ):
        instrument = instrument_for(symbol)
        bars = FixtureProvider().get_bars(instrument, "1h", 120)
        quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
        return build_signal(
            instrument,
            quant,
            generated_at=generated_at or bars[-1].timestamp,
            prediction_id=prediction_id,
            force_action=action,
            model_id="test-model",
            prompt_version="test-prompt-v1",
            source_type=source_type,
            replay_run_id=replay_run_id,
        )

    def _seed_store(self):
        self.store.create_replay_run(
            run_id="replay-test",
            model_id="test-model",
            prompt_version="test-prompt-v1",
            symbols=["NVDA"],
            timeframes=["1h"],
            sampling_policy={"samples": 1},
            manifest_hash="manifest-test",
            status="COMPLETED",
        )
        long_signal = self._signal("NVDA", "p-long", action=Action.LONG)
        wait_signal = self._signal("AAPL", "p-wait", action=Action.WAIT)
        replay_signal = self._signal(
            "NVDA",
            "p-replay",
            action=Action.SHORT,
            source_type="replay",
            replay_run_id="replay-test",
        )
        now = datetime.now(timezone.utc)
        fresh_signal = self._signal("TSLA", "p-fresh", action=Action.LONG, generated_at=now)
        expired_signal = self._signal("AMD", "p-expired", action=Action.LONG, generated_at=now)
        for signal in (long_signal, wait_signal, replay_signal, fresh_signal, expired_signal):
            self.store.save_prediction(signal)

        entry = (long_signal.entry_low + long_signal.entry_high) / 2.0
        future_bar = Bar(
            long_signal.generated_at + timedelta(hours=1),
            entry,
            long_signal.tp1 * 1.01,
            (entry + long_signal.stop) / 2.0,
            long_signal.tp1,
            100.0,
        )
        self.store.save_outcome(settle_prediction(long_signal, [future_bar]))
        self.store.follow_prediction("p-long", long_signal.generated_at.isoformat())
        self.store.save_replay_sample(
            run_id="replay-test",
            symbol="NVDA",
            timeframe="1h",
            as_of=replay_signal.generated_at.isoformat(),
            capability_flags={"news_history_available": False},
            prediction_id="p-replay",
            status="COMPLETED",
        )
        self._expire_prediction("p-expired")

    def _expire_prediction(self, prediction_id: str):
        expired_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        connection = sqlite3.connect(self.store.path)
        try:
            row = connection.execute(
                "SELECT payload_json FROM predictions WHERE prediction_id = ?",
                (prediction_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            payload = json.loads(row[0])
            payload["signal_valid_until"] = expired_at
            connection.execute(
                "UPDATE predictions SET payload_json = ? WHERE prediction_id = ?",
                (json.dumps(payload, sort_keys=True), prediction_id),
            )
            connection.commit()
        finally:
            connection.close()

    def test_factory_metadata_cors_and_injected_services(self):
        with patch.dict(
            os.environ,
            {
                "API_CORS_ORIGINS": "http://localhost:4173",
                "API_CORS_ALLOW_CREDENTIALS": "true",
            },
            clear=False,
        ):
            model = MockLLMProvider()
            app = create_app(
                store=self.store,
                news_provider=FixtureNewsProvider(),
                llm_provider=model,
            )
            client = TestClient(app)
            self.assertEqual(app.title, "AI Market Analyst")
            self.assertEqual(app.version, API_VERSION)
            metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
            self.assertEqual(metadata["project"]["version"], API_VERSION)
            self.assertEqual(PROMPT_VERSION, "phase2-json-v8")
            self.assertEqual(client.get("/health").json()["phase"], 4)

            cors = client.options(
                "/health",
                headers={
                    "Origin": "http://localhost:4173",
                    "Access-Control-Request-Method": "GET",
                },
            )
            self.assertEqual(cors.status_code, 200)
            self.assertEqual(cors.headers["access-control-allow-origin"], "http://localhost:4173")
            self.assertEqual(cors.headers["access-control-allow-credentials"], "true")

            news = client.get("/instruments/NVDA/news")
            self.assertEqual(news.status_code, 200)
            self.assertEqual(news.json()["provider"], "fixture_news")
            model_health = client.get("/health/model")
            self.assertEqual(model_health.status_code, 200)
            self.assertEqual(model_health.json()["provider"], "mock_llm")

        with patch.dict(
            os.environ,
            {
                "API_CORS_ORIGINS": "*",
                "API_CORS_ALLOW_CREDENTIALS": "true",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError):
                create_app(store=self.store)

        analysis_service = AnalysisService(
            market_provider_factory=lambda _instrument: FixtureProvider(),
            news_provider=FixtureNewsProvider(),
            llm_provider=MockLLMProvider(),
            store=self.store,
        )
        analysis_app = create_app(
            store=self.store,
            analysis_service=analysis_service,
            news_provider=FixtureNewsProvider(),
            llm_provider=None,
        )
        before_predictions = self.store.counts()["predictions"]
        result = TestClient(analysis_app).post("/analysis/AAPL", json={"timeframe": "1h", "limit": 120})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["signal"]["instrument"]["symbol"], "AAPL")
        self.assertEqual(self.store.counts()["predictions"], before_predictions + 1)

    def test_snapshot_is_bounded_provenance_rich_and_side_effect_free(self):
        before = self.store.counts()
        first = self.client.get("/instruments/NVDA/snapshot", params={"timeframe": "15M", "limit": 60})

        self.assertEqual(first.status_code, 200)
        payload = first.json()
        self.assertEqual(
            set(payload),
            {"symbol", "timeframe", "response_time", "data_as_of", "provider_snapshot", "quote", "quant", "time_policy", "bars"},
        )
        self.assertEqual(payload["symbol"], "NVDA")
        self.assertEqual(payload["timeframe"], "15m")
        self.assertEqual(payload["provider_snapshot"]["provider"], "fixture")
        self.assertTrue(payload["provider_snapshot"]["stale"])
        self.assertEqual(payload["quant"]["symbol"], "NVDA")
        self.assertEqual(payload["quant"]["timeframe"], "15m")
        self.assertIsNone(payload["time_policy"])
        self.assertEqual(set(payload["quote"]), {"timestamp", "price", "change_pct", "high", "low"})
        self.assertEqual(len(payload["bars"]), 60)

        timestamps = [datetime.fromisoformat(row["timestamp"]) for row in payload["bars"]]
        self.assertEqual(timestamps, sorted(timestamps))
        for row in payload["bars"]:
            self.assertEqual(set(row), {"timestamp", "open", "high", "low", "close", "volume"})
            self.assertLessEqual(max(row["open"], row["close"]), row["high"])
            self.assertGreaterEqual(min(row["open"], row["close"]), row["low"])
            self.assertGreaterEqual(row["volume"], 0.0)
        self.assertEqual(self.store.counts(), before)

        second = self.client.get("/instruments/NVDA/snapshot", params={"timeframe": "15m", "limit": 60})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["bars"], payload["bars"])
        self.assertEqual(second.json()["quant"], payload["quant"])
        self.assertEqual(self.store.counts(), before)

        lower_clamped = self.client.get("/instruments/NVDA/snapshot", params={"limit": 1})
        upper_clamped = self.client.get("/instruments/NVDA/snapshot", params={"limit": 999})
        self.assertEqual(lower_clamped.status_code, 200)
        self.assertEqual(upper_clamped.status_code, 200)
        self.assertEqual(len(lower_clamped.json()["bars"]), 60)
        self.assertEqual(len(upper_clamped.json()["bars"]), 500)
        self.assertEqual(self.store.counts(), before)

    def test_snapshot_invalid_inputs_and_provider_or_quant_errors_are_structured(self):
        invalid_symbol = self.client.get("/instruments/NOT_SUPPORTED/snapshot")
        invalid_timeframe = self.client.get("/instruments/NVDA/snapshot", params={"timeframe": "weekly"})
        invalid_limit = self.client.get("/instruments/NVDA/snapshot", params={"limit": "not-an-integer"})
        for response, code in (
            (invalid_symbol, "INVALID_SYMBOL"),
            (invalid_timeframe, "INVALID_TIMEFRAME"),
            (invalid_limit, "INVALID_LIMIT"),
        ):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], code)

        class FailingProvider:
            provider_name = "failing_fixture"

            def get_quote(self, _instrument):
                raise ProviderError("fixture unavailable", code="timeout", provider=self.provider_name)

            def get_bars(self, _instrument, _timeframe, _limit=200):
                raise AssertionError("quote failure should stop the provider request")

        provider_service = AnalysisService(
            market_provider_factory=lambda _instrument: FailingProvider(),
            news_provider=FixtureNewsProvider(),
            llm_provider=None,
            store=self.store,
        )
        provider_response = TestClient(create_app(store=self.store, snapshot_service=provider_service)).get(
            "/instruments/NVDA/snapshot"
        )
        self.assertEqual(provider_response.status_code, 502)
        self.assertEqual(provider_response.json()["error"]["code"], "SNAPSHOT_PROVIDER_ERROR")

        class ShortHistoryProvider:
            provider_name = "short_fixture"

            def get_quote(self, instrument):
                return FixtureProvider().get_quote(instrument)

            def get_bars(self, instrument, timeframe, limit=200):
                return FixtureProvider().get_bars(instrument, timeframe, limit)[:59]

        quant_service = AnalysisService(
            market_provider_factory=lambda _instrument: ShortHistoryProvider(),
            news_provider=FixtureNewsProvider(),
            llm_provider=None,
            store=self.store,
        )
        quant_response = TestClient(create_app(store=self.store, snapshot_service=quant_service)).get(
            "/instruments/NVDA/snapshot"
        )
        self.assertEqual(quant_response.status_code, 502)
        self.assertEqual(quant_response.json()["error"]["code"], "SNAPSHOT_QUANT_ERROR")

    def test_predictions_filters_detail_and_structured_errors(self):
        filtered = self.client.get("/predictions", params={"symbol": "nvda", "action": "long", "limit": 0})
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual([item["prediction_id"] for item in filtered.json()], ["p-long"])

        replay = self.client.get("/predictions", params={"source_type": "replay", "replay_run_id": "replay-test"})
        self.assertEqual([item["prediction_id"] for item in replay.json()], ["p-replay"])

        detail = self.client.get("/predictions/p-long")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["prediction_id"], "p-long")
        self.assertEqual(detail.json()["paper_trade"]["status"], "OPEN")
        self.assertEqual(detail.json()["outcome"]["status"], "TP1")

        clamped = self.client.get("/predictions", params={"limit": 100000})
        self.assertEqual(clamped.status_code, 200)
        self.assertLessEqual(len(clamped.json()), 500)

        invalid_filter = self.client.get("/predictions", params={"action": "BUY"})
        self.assertEqual(invalid_filter.status_code, 400)
        self.assertEqual(invalid_filter.json()["error"]["code"], "INVALID_ACTION")
        self.assertIn("message", invalid_filter.json()["error"])

        invalid_pagination = self.client.get("/predictions", params={"limit": "not-an-int"})
        self.assertEqual(invalid_pagination.status_code, 422)
        self.assertEqual(invalid_pagination.json()["error"]["code"], "VALIDATION_ERROR")

        missing = self.client.get("/predictions/missing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "PREDICTION_NOT_FOUND")

    def test_paper_trade_outcome_reads_and_follow_is_idempotent(self):
        trades = self.client.get("/paper-trades", params={"symbol": "NVDA", "status": "open"})
        self.assertEqual(trades.status_code, 200)
        self.assertEqual(len(trades.json()), 1)
        self.assertEqual(trades.json()[0]["prediction"]["prediction_id"], "p-long")
        self.assertEqual(trades.json()[0]["outcome"]["status"], "TP1")

        trade_detail = self.client.get("/paper-trades/p-long")
        self.assertEqual(trade_detail.status_code, 200)
        self.assertEqual(trade_detail.json()["status"], "OPEN")
        self.assertEqual(trade_detail.json()["prediction"]["prediction_id"], "p-long")

        outcomes = self.client.get("/outcomes", params={"status": "tp1", "action": "LONG"})
        self.assertEqual(outcomes.status_code, 200)
        self.assertEqual(outcomes.json()[0]["prediction"]["prediction_id"], "p-long")
        self.assertEqual(outcomes.json()[0]["paper_trade"]["prediction_id"], "p-long")

        outcome_detail = self.client.get("/outcomes/p-long")
        self.assertEqual(outcome_detail.status_code, 200)
        self.assertEqual(outcome_detail.json()["status"], "TP1")
        self.assertEqual(outcome_detail.json()["outcome"]["prediction_id"], "p-long")

        wait_follow = self.client.post("/predictions/p-wait/follow")
        self.assertEqual(wait_follow.status_code, 409)
        self.assertEqual(wait_follow.json()["error"]["code"], "PREDICTION_NOT_ACTIONABLE")
        self.assertIsNone(self.store.get_paper_trade_record("p-wait"))

        expired_follow = self.client.post("/predictions/p-expired/follow")
        self.assertEqual(expired_follow.status_code, 409)
        self.assertEqual(expired_follow.json()["error"]["code"], "SIGNAL_EXPIRED")
        self.assertIsNone(self.store.get_paper_trade_record("p-expired"))

        first = self.client.post("/predictions/p-fresh/follow")
        followed_at = self.store.get_paper_trade_record("p-fresh")["followed_at"]
        self._expire_prediction("p-fresh")
        second = self.client.post("/predictions/p-fresh/follow")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["real_order"])
        self.assertFalse(second.json()["real_order"])
        self.assertEqual(self.store.get_paper_trade_record("p-fresh")["followed_at"], followed_at)
        self.assertEqual(self.store.counts()["paper_trades"], 2)
        self.assertEqual(self.client.get("/paper-trades/p-fresh").json()["status"], "OPEN")

        missing_trade = self.client.get("/paper-trades/p-replay")
        self.assertEqual(missing_trade.status_code, 404)
        self.assertEqual(missing_trade.json()["error"]["code"], "PAPER_TRADE_NOT_FOUND")

    def test_replay_and_existing_phase3_routes_remain_available(self):
        runs = self.client.get("/replay/runs", params={"status": "completed"})
        self.assertEqual(runs.status_code, 200)
        self.assertEqual(runs.json()[0]["run_id"], "replay-test")

        run = self.client.get("/replay/runs/replay-test")
        self.assertEqual(run.status_code, 200)
        self.assertEqual(len(run.json()["samples"]), 1)

        calibration = self.client.get("/predictions/p-long/calibration")
        self.assertEqual(calibration.status_code, 200)
        self.assertEqual(calibration.json()["prediction_id"], "p-long")

        performance = self.client.get("/performance/summary", params={"source_type": "live"})
        self.assertEqual(performance.status_code, 200)
        self.assertEqual(performance.json()["scope"]["source_type"], "live")

        health = self.client.get("/health/providers")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(len(health.json()["routes"]), 6)

        invalid_run_filter = self.client.get("/replay/runs", params={"status": "unknown"})
        self.assertEqual(invalid_run_filter.status_code, 400)
        self.assertEqual(invalid_run_filter.json()["error"]["code"], "INVALID_REPLAY_STATUS")

        invalid_timeframe = self.client.get("/performance/summary", params={"timeframe": "weekly"})
        self.assertEqual(invalid_timeframe.status_code, 400)
        self.assertEqual(invalid_timeframe.json()["error"]["code"], "INVALID_TIMEFRAME")

        invalid_summary_symbol = self.client.get("/performance/summary", params={"symbol": "NOT_SUPPORTED"})
        invalid_by_symbol = self.client.get("/performance/by-symbol/NOT_SUPPORTED")
        self.assertEqual(invalid_summary_symbol.status_code, 400)
        self.assertEqual(invalid_by_symbol.status_code, 400)
        self.assertEqual(invalid_summary_symbol.json()["error"]["code"], "INVALID_SYMBOL")
        self.assertEqual(invalid_by_symbol.json()["error"]["code"], "INVALID_SYMBOL")

        created = self.client.post("/replay/runs", json={"symbols": ["AAPL"], "timeframes": ["1h"], "samples": 1})
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["prompt_version"], PROMPT_VERSION)

    def test_internal_errors_do_not_expose_exception_text(self):
        class ExplodingStore:
            def list_prediction_payloads(self, **_kwargs):
                raise RuntimeError("sentinel-internal-store-error")

        app = create_app()
        app.dependency_overrides[get_store] = lambda: ExplodingStore()
        response = TestClient(app, raise_server_exceptions=False).get("/predictions")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("sentinel-internal-store-error", response.text)


if __name__ == "__main__":
    unittest.main()
