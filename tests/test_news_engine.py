import unittest
from datetime import datetime, timedelta, timezone

from core.instruments import instrument_for
from core.news_engine import NewsEngine, cluster_news_events, dedupe_news_events
from core.providers import FixtureNewsProvider, NewsEvent


class NewsEngineTests(unittest.TestCase):
    def test_duplicate_titles_and_urls_are_collapsed(self):
        published = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = NewsEvent(
            event_id="newer",
            source="test",
            published_at=published + timedelta(hours=1),
            title="AAPL earnings beat estimates",
            symbols=("AAPL",),
            category="earnings",
            sentiment=0.5,
            importance=80,
            url="https://example.test/story",
        )
        older = NewsEvent(
            event_id="older",
            source="test",
            published_at=published,
            title="AAPL earnings beat estimates",
            symbols=("AAPL",),
            category="earnings",
            sentiment=0.4,
            importance=75,
            url="https://example.test/story",
        )
        events = dedupe_news_events([older, newer])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "newer")

    def test_fixture_news_result_is_structured_and_available(self):
        instrument = instrument_for("NVDA")
        result = NewsEngine(FixtureNewsProvider()).collect(instrument)
        self.assertTrue(result.available)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.provider, "fixture_news")
        self.assertEqual(result.events[0].symbols, ("NVDA",))
        self.assertGreaterEqual(result.events[0].importance, 0)
        self.assertIn("events", result.to_dict())
        self.assertEqual(len(result.clusters), 1)

    def test_related_sources_are_visible_as_an_event_cluster(self):
        published = datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = [
            NewsEvent("one", "source-a", published, "AAPL earnings beat estimates", ("AAPL",), category="earnings", importance=80),
            NewsEvent("two", "source-b", published + timedelta(minutes=5), "AAPL earnings beat estimates today", ("AAPL",), category="earnings", importance=85),
        ]
        clusters = cluster_news_events(events)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].importance, 85)

    def test_invalid_category_is_rejected_at_boundary(self):
        with self.assertRaises(ValueError):
            NewsEvent(
                event_id="bad",
                source="test",
                published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                title="bad event",
                symbols=("AAPL",),
                category="not-a-contract-category",
            )


if __name__ == "__main__":
    unittest.main()
