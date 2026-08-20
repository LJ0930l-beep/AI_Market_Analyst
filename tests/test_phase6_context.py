import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import API_PHASE, API_VERSION, create_app
from core.benchmarks import BenchmarkContextService, benchmark_metadata_for
from core.events import (
    EVENT_CLUSTER_VERSION,
    EVENT_SCHEMA_VERSION,
    EventEvidence,
    EventIntelligenceService,
    cluster_event_evidence,
    select_point_in_time_events,
    source_credibility,
)
from core.instruments import instrument_for
from core.memory import MEMORY_FEATURE_VERSION, MEMORY_VERSION, MarketMemoryService
from core.outcomes import Outcome, OutcomeStatus
from core.providers import FixtureNewsProvider, FixtureProvider, ProviderError
from core.signals import Action, SignalProposal
from core.storage import SQLiteStore
from core.time_rules import build_time_policy
from core.analysis_service import AnalysisService


POINT = datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc)


def make_event(
    event_id: str,
    *,
    source: str = "Reuters",
    title: str = "NVDA earnings outlook",
    known_at: datetime = POINT - timedelta(hours=2),
    published_at: datetime = POINT - timedelta(hours=3),
    event_at: datetime = POINT - timedelta(hours=1),
    importance: int = 80,
    sentiment: float | None = 0.4,
    primary_source: bool = False,
    revision_known_at: datetime | None = None,
) -> EventEvidence:
    return EventEvidence(
        event_id=event_id,
        source=source,
        source_type="official" if primary_source else "professional",
        category="earnings",
        event_at=event_at,
        published_at=published_at,
        known_at=known_at,
        retrieved_at=POINT,
        importance=importance,
        affected_symbols=("NVDA",),
        title=title,
        summary="typed deterministic event evidence",
        url="https://example.test/event/" + event_id,
        sentiment=sentiment,
        primary_source=primary_source,
        reported_credibility=70,
        credibility_score=source_credibility(source, primary_source=primary_source, reported=70),
        revision_known_at=revision_known_at,
        provider="injected",
        capability={"historical_known_time": True},
    )


def make_signal(prediction_id: str, generated_at: datetime, *, context: dict[str, object] | None = None) -> SignalProposal:
    return SignalProposal(
        prediction_id=prediction_id,
        instrument=instrument_for("NVDA"),
        analysis_timeframe="1h",
        generated_at=generated_at,
        action=Action.WAIT,
        entry_low=None,
        entry_high=None,
        stop=None,
        tp1=None,
        tp2=None,
        signal_validity_minutes=120,
        expected_hold_minutes=1440,
        max_hold_minutes=4320,
        reevaluate_at=generated_at + timedelta(minutes=60),
        invalidation=(),
        raw_confidence=0.5,
        reason_codes=("test",),
        summary="deterministic Phase 6 memory fixture",
        context_json=json.dumps(context or {}, sort_keys=True),
    )


def memory_context(action: str = "WAIT", *, trend: float = 0.5) -> dict[str, object]:
    return {
        "quant": {
            "market_regime": "bull_trend",
            "trend_score": trend,
            "momentum_score": 0.4,
            "volume_ratio": 1.2,
        },
        "market_context": {
            "benchmark_context": {"relative_strength": 0.3},
            "event_intelligence": {"clusters": []},
        },
        "risk_events": [],
        "action": action,
    }


class TypedEventProvider:
    provider_name = "injected_events"

    def __init__(self, events: list[EventEvidence]) -> None:
        self.events = events

    def get_events(self, _instrument, *, as_of: datetime, limit: int = 20):
        return self.events[:limit]


class FailingBenchmarkProvider:
    provider_name = "benchmark_unavailable"

    def get_bundle(self, *_args, **_kwargs):
        raise ProviderError("benchmark unavailable", code="provider_unavailable", provider=self.provider_name)


class Phase6MigrationAndEvidenceTests(unittest.TestCase):
    def test_phase6_migration_is_idempotent_and_persists_typed_evidence_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "phase6.sqlite3"
            store = SQLiteStore(path)
            store.initialize()
            self.assertEqual(store.schema_version(), 9)
            self.assertEqual(store.get_benchmark_metadata("NVDA")["mapping_version"], "benchmark_mapping_v1")
            event = make_event("event-persist")
            store.save_event_context(
                {
                    "schema_version": EVENT_SCHEMA_VERSION,
                    "cluster_version": EVENT_CLUSTER_VERSION,
                    "as_of": POINT.isoformat(),
                    "events": [event.to_dict()],
                    "clusters": [cluster.to_dict() for cluster in cluster_event_evidence([event], as_of=POINT)],
                }
            )
            store.save_memory_feature(
                {
                    "feature_id": "market_memory_v1:memory-persist:2030-01-02T15:00:00+00:00",
                    "prediction_id": "memory-persist",
                    "source_type": "live",
                    "feature_as_of": POINT.isoformat(),
                    "representation_version": MEMORY_FEATURE_VERSION,
                    "features": {"symbol": "NVDA", "trend_score": 0.5},
                    "outcome": None,
                }
            )
            before = store.counts()
            reopened = SQLiteStore(path)
            reopened.initialize()
            self.assertEqual(reopened.schema_version(), 9)
            self.assertEqual(reopened.counts()["phase6_events"], before["phase6_events"])
            self.assertEqual(reopened.counts()["phase6_event_clusters"], before["phase6_event_clusters"])
            self.assertEqual(reopened.counts()["market_memory_features"], before["market_memory_features"])
            self.assertEqual(reopened.list_event_evidence(symbol="NVDA")[0]["event_id"], "event-persist")
            self.assertEqual(reopened.list_memory_features()[0]["representation_version"], MEMORY_FEATURE_VERSION)

    def test_benchmark_is_explicit_deterministic_as_of_and_has_honest_provider_fallback(self):
        instrument = instrument_for("NVDA")
        bars = FixtureProvider().get_bars(instrument, "1h", 120)
        cutoff = bars[-20].timestamp
        available = BenchmarkContextService(provider_factory=lambda _instrument: FixtureProvider()).build(
            instrument,
            target_bars=bars,
            target_quant=None,
            timeframe="1h",
            as_of=cutoff,
            target_provider="fixture",
        )
        self.assertEqual(available.status, "available")
        self.assertEqual(available.benchmark.benchmark_symbol, "SOXX")
        self.assertEqual(available.provenance["computed_by"], "python_deterministic")
        self.assertTrue(available.capability["future_bars_excluded"])
        self.assertLessEqual(available.freshness["target_data_as_of"], cutoff.isoformat())
        unavailable = BenchmarkContextService(provider_factory=lambda _instrument: FailingBenchmarkProvider()).build(
            instrument,
            target_bars=bars,
            target_quant=None,
            timeframe="1h",
            as_of=cutoff,
            target_provider="fixture",
        )
        self.assertEqual(unavailable.status, "unavailable")
        self.assertIsNone(unavailable.relative_performance)
        self.assertEqual(unavailable.capability["reason"], "provider_unavailable")
        crypto = instrument_for("ETHUSDT")
        crypto_bars = FixtureProvider().get_bars(crypto, "1h", 120)
        crypto_context = BenchmarkContextService(provider_factory=lambda _instrument: FixtureProvider()).build(
            crypto,
            target_bars=crypto_bars,
            target_quant=None,
            timeframe="1h",
            as_of=crypto_bars[-1].timestamp,
            target_provider="fixture",
        )
        self.assertEqual(crypto_context.benchmark.benchmark_symbol, "BTCUSDT")
        self.assertEqual(crypto_context.capability["total_market_context"], "unavailable")
        self.assertEqual(crypto_context.capability["dominance_context"], "unavailable")

    def test_event_point_in_time_selection_clustering_credibility_and_time_policy_are_repeatable(self):
        past = make_event("past", source="Reuters")
        primary = make_event("primary", source="Company IR", title="NVDA earnings outlook", primary_source=True, sentiment=-0.5)
        future_known = make_event("future-known", known_at=POINT + timedelta(minutes=1))
        future_published = make_event("future-published", published_at=POINT + timedelta(minutes=1))
        future_revision = make_event("future-revision", revision_known_at=POINT + timedelta(minutes=1))
        selected = select_point_in_time_events(
            [past, primary, future_known, future_published, future_revision],
            as_of=POINT,
        )
        self.assertEqual({event.event_id for event in selected}, {"past", "primary"})
        clusters = cluster_event_evidence(selected, as_of=POINT)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].source_count, 2)
        self.assertEqual(clusters[0].primary_source_count, 1)
        self.assertEqual(clusters[0].consensus, "confirmed")
        self.assertTrue(clusters[0].disagreement)
        self.assertEqual(clusters, cluster_event_evidence(selected, as_of=POINT))
        self.assertGreater(source_credibility("Company IR", primary_source=True, reported=10), source_credibility("unknown", reported=100))
        policy = build_time_policy("1h", price=100.0, atr14=1.0, market_regime="range", event_evidence=selected, now=POINT)
        self.assertTrue(policy.event_risk)
        self.assertIn("phase6_event_evidence", policy.reason_codes)

    def test_event_service_returns_capability_error_without_fabricating(self):
        class BrokenProvider:
            provider_name = "broken_events"

            def get_events(self, *_args, **_kwargs):
                raise TimeoutError("timeout")

        result = EventIntelligenceService(BrokenProvider(), clock=lambda: POINT).collect(instrument_for("NVDA"), as_of=POINT)
        self.assertFalse(result.available)
        self.assertEqual(result.error_code, "timeout")
        self.assertEqual(result.events, ())
        self.assertTrue(result.capability["future_evidence_excluded"])


class Phase6MemoryAndApiTests(unittest.TestCase):
    def test_memory_excludes_future_outcomes_and_self_match_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "memory.sqlite3")
            store.initialize()
            context = memory_context()
            resolved_ids = []
            for index in range(4):
                prediction_id = f"memory-{index}"
                signal = make_signal(prediction_id, POINT - timedelta(days=index + 1), context=context)
                store.save_prediction(signal)
                if index < 3:
                    resolved_ids.append(prediction_id)
                    settled_at = POINT - timedelta(hours=index + 1)
                else:
                    settled_at = POINT + timedelta(hours=1)
                store.save_outcome(Outcome(prediction_id, OutcomeStatus.TP1, settled_at, 105.0, 1.5, 1.5, 0.0, 1, False))
            future_data = replace(
                make_signal("future-data", POINT - timedelta(days=5), context=context),
                data_as_of=POINT + timedelta(minutes=1),
            )
            store.save_prediction(future_data)
            store.save_outcome(Outcome("future-data", OutcomeStatus.TP1, POINT - timedelta(minutes=1), 105.0, 1.5, 1.5, 0.0, 1, False))
            incomplete = make_signal("incomplete", POINT - timedelta(days=6), context={})
            store.save_prediction(incomplete)
            store.save_outcome(Outcome("incomplete", OutcomeStatus.TP1, POINT - timedelta(minutes=2), 105.0, 1.5, 1.5, 0.0, 1, False))
            service = MarketMemoryService(store, min_resolved_samples=2)
            first = service.query(
                symbol="NVDA",
                timeframe="1h",
                as_of=POINT,
                query_prediction=make_signal("query", POINT - timedelta(hours=1), context=context).to_dict(),
                exclude_prediction_id="memory-0",
                top_k=5,
            )
            second = service.query(
                symbol="NVDA",
                timeframe="1h",
                as_of=POINT,
                query_prediction=make_signal("query", POINT - timedelta(hours=1), context=context).to_dict(),
                exclude_prediction_id="memory-0",
                top_k=5,
            )
            self.assertEqual(first.version, MEMORY_VERSION)
            self.assertEqual(first.status, "ready")
            self.assertEqual(first.resolved_sample_count, 2)
            self.assertNotIn("memory-0", {match.prediction_id for match in first.matches})
            self.assertNotIn("memory-3", {match.prediction_id for match in first.matches if match.outcome_status})
            self.assertNotIn("future-data", {match.prediction_id for match in first.matches})
            self.assertNotIn("incomplete", {match.prediction_id for match in first.matches})
            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertTrue(first.capability["future_evidence_excluded"])
            materialized = service.materialize(as_of=POINT)
            self.assertEqual(materialized["status"], "materialized")
            self.assertGreaterEqual(int(materialized["count"]), 4)
            reopened = SQLiteStore(store.path)
            reopened.initialize()
            self.assertGreaterEqual(len(reopened.list_memory_features(as_of=POINT.isoformat())), 4)

    def test_context_get_is_read_only_and_explicit_analysis_is_the_write_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "api.sqlite3")
            store.initialize()
            event_provider = TypedEventProvider([make_event("api-event")])
            service = AnalysisService(
                market_provider_factory=lambda _instrument: FixtureProvider(),
                benchmark_provider_factory=lambda _instrument: FixtureProvider(),
                news_provider=FixtureNewsProvider(),
                event_provider=event_provider,
                llm_provider=None,
                store=store,
            )
            app = create_app(store=store, analysis_service=service, event_provider=event_provider, news_provider=FixtureNewsProvider(), llm_provider=None)
            client = TestClient(app)
            before = store.counts()
            health = client.get("/health/context")
            context = client.get("/instruments/NVDA/context?limit=120")
            events = client.get("/instruments/NVDA/events?limit=120")
            memory = client.get("/instruments/NVDA/memory?limit=120")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["phase"], API_PHASE)
            self.assertEqual(health.json()["api_version"], API_VERSION)
            self.assertEqual(context.status_code, 200)
            self.assertEqual(events.status_code, 200)
            self.assertEqual(memory.status_code, 200)
            payload = context.json()
            self.assertEqual(payload["benchmark_context"]["version"], "benchmark_context_v1")
            self.assertEqual(payload["events"]["schema_version"], EVENT_SCHEMA_VERSION)
            self.assertTrue(payload["provenance"]["read_only"])
            self.assertEqual(before, store.counts())
            bounded = client.get("/instruments/NVDA/context?limit=120&as_of=2025-12-30T00:00:00Z")
            self.assertEqual(bounded.status_code, 200)
            self.assertLessEqual(bounded.json()["quote"]["timestamp"], "2025-12-30T00:00:00+00:00")
            self.assertLessEqual(bounded.json()["data_as_of"], "2025-12-30T00:00:00+00:00")
            self.assertEqual(before, store.counts())

            analysis = client.post("/analysis/NVDA", json={"timeframe": "1h", "limit": 120})
            self.assertEqual(analysis.status_code, 200)
            analysis_payload = analysis.json()
            self.assertEqual(analysis_payload["benchmark_context"]["version"], "benchmark_context_v1")
            self.assertEqual(analysis_payload["events"]["schema_version"], EVENT_SCHEMA_VERSION)
            self.assertEqual(store.counts()["predictions"], before["predictions"] + 1)
            self.assertEqual(store.counts()["paper_trades"], before["paper_trades"])
            self.assertEqual(store.counts()["outcomes"], before["outcomes"])
            self.assertEqual(store.counts()["alerts"], before["alerts"])


if __name__ == "__main__":
    unittest.main()
