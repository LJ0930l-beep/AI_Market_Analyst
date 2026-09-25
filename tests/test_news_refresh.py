from datetime import datetime, timezone

from core.news_engine import NewsCategory, NewsEvent
from core.news_refresh import refresh_public_news
from core.storage import SQLiteStore


class _Provider:
    provider_name = "fixture_public_rss"

    def get_events(self, instrument, limit=20):
        return [
            NewsEvent(
                event_id=f"news-{instrument.symbol}",
                source="Fixture wire",
                published_at=datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc),
                title=f"{instrument.symbol} public headline",
                symbols=(instrument.symbol,),
                category=NewsCategory.OTHER.value,
                sentiment=0.2,
                importance=30,
                summary_raw="Provider-backed fixture evidence.",
                url="https://example.test/news",
                credibility=60,
                impact_horizon="intraday",
                dedupe_hash=f"hash-{instrument.symbol}",
            )
        ]


class _UnavailableProvider:
    provider_name = "unavailable_rss"

    def get_events(self, instrument, limit=20):
        raise RuntimeError("network unavailable")


def test_public_news_refresh_persists_real_provider_evidence_and_get_remains_read_only(tmp_path):
    store = SQLiteStore(tmp_path / "news-refresh.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)

    payload = refresh_public_news(store, symbols=["BTCUSDT", "ETHUSDT"], provider=_Provider(), now=now)

    assert payload["status"] == "AVAILABLE"
    assert payload["event_count"] == 2
    saved = store.list_event_evidence(as_of=now.isoformat(), limit=10)
    assert {event["event_id"] for event in saved} == {"news-BTCUSDT", "news-ETHUSDT"}
    assert all(event["provider"] == "fixture_public_rss" for event in saved)


def test_public_news_refresh_keeps_prior_evidence_when_http_provider_fails(tmp_path):
    store = SQLiteStore(tmp_path / "news-refresh-failure.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
    refresh_public_news(store, symbols=["BTCUSDT"], provider=_Provider(), now=now)

    failed = refresh_public_news(store, symbols=["BTCUSDT"], provider=_UnavailableProvider(), now=now)

    assert failed["status"] == "UNAVAILABLE"
    assert store.list_event_evidence(as_of=now.isoformat(), limit=10)[0]["event_id"] == "news-BTCUSDT"


def test_old_search_feed_cache_does_not_turn_unrelated_hits_into_symbol_news(tmp_path):
    store = SQLiteStore(tmp_path / "news-relevance.sqlite3")
    store.initialize()
    now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    store.save_event_context({"events": [
        {
            "event_id": "unrelated", "provider": "google_news_rss",
            "affected_symbols": ["TSTBSCUSDT"], "title": "Crypto Profit Calculator",
            "published_at": now.isoformat(), "known_at": now.isoformat(),
        },
        {
            "event_id": "related", "provider": "google_news_rss",
            "affected_symbols": ["BTCUSDT"], "title": "Bitcoin futures launch",
            "published_at": now.isoformat(), "known_at": now.isoformat(),
        },
    ]})
    assert store.list_event_evidence(symbol="TSTBSCUSDT", as_of=now.isoformat()) == []
    assert [row["event_id"] for row in store.list_event_evidence(symbol="BTCUSDT", as_of=now.isoformat())] == ["related"]
