import tempfile
import unittest
from pathlib import Path

from core.ai import LLMError, MockLLMProvider, OllamaProvider
from core.analysis_service import AnalysisError, AnalysisService
from core.instruments import instrument_for
from core.model_routing import DEFAULT_MODEL
from core.providers import FixtureNewsProvider, FixtureProvider
from core.storage import SQLiteStore


class AnalysisServiceTests(unittest.TestCase):
    @staticmethod
    def _factory(_instrument):
        return FixtureProvider()

    def test_mock_fixture_flow_is_persisted_only_as_demo_source(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "phase2.sqlite3")
            result = AnalysisService(
                market_provider_factory=self._factory,
                news_provider=FixtureNewsProvider(),
                llm_provider=MockLLMProvider(),
                store=store,
            ).analyze(instrument_for("NVDA"), timeframe="1h", limit=120, source_type="demo")
            self.assertEqual(result.bundle.snapshot.provider, "fixture")
            self.assertTrue(result.bundle.snapshot.stale)
            self.assertEqual(result.news.provider, "fixture_news")
            self.assertEqual(result.model_status["available"], True)
            self.assertEqual(result.signal.model_id, "mock-llm")
            self.assertEqual(result.signal.input_hash, result.context.input_hash())
            self.assertEqual(result.signal.data_as_of, result.bundle.data_as_of)
            counts = store.counts()
            self.assertEqual(counts["predictions"], 1)
            self.assertEqual(counts["news_events"], 1)
            self.assertEqual(counts["provider_snapshots"], 1)
            self.assertEqual(counts["model_runs"], 1)
            records = store.list_prediction_records(source_type="demo")
            self.assertEqual(records[0]["prediction"]["model_id"], "mock-llm")
            self.assertEqual(store.list_prediction_records(source_type="live"), [])

    def test_live_analysis_rejects_mock_before_provider_or_storage_side_effects(self):
        class ExplodingProvider:
            def get_bundle(self, *_args):
                raise AssertionError("mock model must be rejected before market data is fetched")

        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "live-mock.sqlite3")
            service = AnalysisService(
                market_provider_factory=lambda _instrument: ExplodingProvider(),
                news_provider=FixtureNewsProvider(),
                llm_provider=MockLLMProvider(),
                store=store,
            )
            before = store.counts()
            with self.assertRaises(AnalysisError) as error:
                service.analyze(instrument_for("NVDA"), timeframe="1h", limit=120)
            self.assertEqual(error.exception.code, "MODEL_PROVIDER_NOT_ALLOWED")
            self.assertEqual(store.counts(), before)

    def test_production_analysis_rejects_provider_that_only_claims_bonsai(self):
        class ForgedBonsaiProvider:
            provider_name = "bonsai_llama_server"
            model_id = DEFAULT_MODEL
            model_name = DEFAULT_MODEL

        service = AnalysisService(
            market_provider_factory=self._factory,
            news_provider=FixtureNewsProvider(),
            llm_provider=ForgedBonsaiProvider(),
        )
        for source_type, run_id in (("live", None), ("replay", "forged-run")):
            with self.assertRaises(AnalysisError) as error:
                service.analyze(
                    instrument_for("NVDA"),
                    timeframe="1h",
                    limit=120,
                    source_type=source_type,
                    replay_run_id=run_id,
                )
            self.assertEqual(error.exception.code, "MODEL_PROVIDER_NOT_ALLOWED")

    def test_live_analysis_rejects_bonsai_label_without_inference_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "unverified-live.sqlite3")
            provider = OllamaProvider(model_name=DEFAULT_MODEL)
            mock_provider = MockLLMProvider()
            provider.analyze_market = lambda context, policy, **kwargs: mock_provider.analyze_market(  # type: ignore[method-assign]
                context, policy, **kwargs
            )
            service = AnalysisService(
                market_provider_factory=self._factory,
                news_provider=FixtureNewsProvider(),
                llm_provider=provider,
                store=store,
            )
            with self.assertRaises(AnalysisError) as error:
                service.analyze(instrument_for("NVDA"), timeframe="1h", limit=120)
            self.assertEqual(error.exception.code, "MODEL_RECEIPT_UNVERIFIED")
            self.assertEqual(store.counts()["predictions"], 0)

    def test_missing_model_is_explicit_wait_not_a_fake_action(self):
        result = AnalysisService(
            market_provider_factory=self._factory,
            news_provider=FixtureNewsProvider(),
            llm_provider=None,
        ).analyze(instrument_for("AAPL"), timeframe="1h", limit=120)
        self.assertEqual(result.signal.action.value, "WAIT")
        self.assertEqual(result.signal.parse_status, "MODEL_NOT_CONFIGURED")
        self.assertFalse(result.model_status["available"])

    def test_demo_source_type_is_valid_and_kept_out_of_live_predictions(self):
        result = AnalysisService(
            market_provider_factory=self._factory,
            news_provider=FixtureNewsProvider(),
            llm_provider=MockLLMProvider(),
        ).analyze(instrument_for("NVDA"), timeframe="1h", limit=120, source_type="demo")
        self.assertEqual(result.signal.source_type, "demo")
        self.assertEqual(result.signal.model_id, "mock-llm")

    def test_mock_replay_source_is_distinct_and_requires_its_run_id(self):
        service = AnalysisService(
            market_provider_factory=self._factory,
            news_provider=FixtureNewsProvider(),
            llm_provider=MockLLMProvider(),
        )
        result = service.analyze(
            instrument_for("NVDA"),
            timeframe="15m",
            limit=120,
            source_type="mock_replay",
            replay_run_id="mock-run-1",
        )
        self.assertEqual(result.signal.source_type, "mock_replay")
        self.assertEqual(result.signal.replay_run_id, "mock-run-1")
        with self.assertRaises(ValueError):
            service.analyze(instrument_for("NVDA"), source_type="mock_replay")

    def test_failed_json_never_becomes_an_executable_prediction(self):
        class AlwaysInvalidModel:
            provider_name = "ollama"
            model_name = "Bonsai-2-27B-PTQ1_0"

            def analyze_market(self, context, policy, *, repair=False):
                raise LLMError("invalid JSON", code="parse_error", raw_response="not-json")

        result = AnalysisService(
            market_provider_factory=self._factory,
            news_provider=FixtureNewsProvider(),
            llm_provider=AlwaysInvalidModel(),
        ).analyze(instrument_for("AAPL"), timeframe="1h", limit=120, source_type="demo")
        self.assertEqual(result.signal.action.value, "WAIT")
        self.assertEqual(result.signal.parse_status, "repair_failed")
        self.assertIsNone(result.signal.entry_low)
        self.assertIsNone(result.signal.stop)

    def test_news_failure_keeps_technical_context_with_lower_confidence_cap(self):
        class FailingNews:
            provider_name = "failing_news"

            def get_events(self, instrument, limit=20):
                raise TimeoutError("news timeout")

        result = AnalysisService(
            market_provider_factory=self._factory,
            news_provider=FailingNews(),
            llm_provider=MockLLMProvider(),
        ).analyze(instrument_for("NVDA"), timeframe="1h", limit=120, source_type="demo")
        self.assertFalse(result.news.available)
        self.assertEqual(result.news.error_code, "timeout")
        self.assertLessEqual(result.signal.raw_confidence, 0.60)
        self.assertIn("news_unavailable", result.signal.reason_codes)

    def test_market_snapshot_is_read_only_and_uses_deterministic_quant(self):
        class ExplodingNews:
            def get_events(self, *_args, **_kwargs):
                raise AssertionError("snapshot must not fetch news")

        class ExplodingModel:
            def analyze_market(self, *_args, **_kwargs):
                raise AssertionError("snapshot must not invoke a model")

        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "snapshot.sqlite3")
            store.initialize()
            service = AnalysisService(
                market_provider_factory=self._factory,
                news_provider=ExplodingNews(),
                llm_provider=ExplodingModel(),
                store=store,
            )
            before = store.counts()

            result = service.market_snapshot(instrument_for("NVDA"), timeframe="15m", limit=60)

            self.assertEqual(store.counts(), before)
            self.assertEqual(result.instrument.symbol, "NVDA")
            self.assertEqual(result.timeframe, "15m")
            self.assertEqual(len(result.bundle.bars), 60)
            self.assertEqual(result.quant.timeframe, "15m")
            self.assertEqual(result.bundle.snapshot.provider, "fixture")
            self.assertEqual(
                [bar.timestamp for bar in result.bundle.bars],
                sorted(bar.timestamp for bar in result.bundle.bars),
            )
            for bar in result.bundle.bars:
                self.assertLessEqual(max(bar.open, bar.close), bar.high)
                self.assertGreaterEqual(min(bar.open, bar.close), bar.low)
                self.assertGreaterEqual(bar.volume, 0.0)


if __name__ == "__main__":
    unittest.main()
