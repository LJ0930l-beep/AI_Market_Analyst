from datetime import datetime, timedelta, timezone

from core.market_intelligence import _pulse_entry, build_market_intelligence
from core.news_engine import _parse_date
from core.storage import SQLiteStore


def test_cache_expiry_offline_restart_and_future(tmp_path):
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "trust.db")
    store.initialize()
    state = {
        "symbol": "BTCUSDT",
        "price": 100,
        "change_pct": 1,
        "data_as_of": now.isoformat(),
        "freshness_status": "fresh",
        "stale_after_seconds": 120,
    }
    store.save_realtime_state(state, now=now)
    assert (
        build_market_intelligence(store, as_of=now)["pulse"][0]["freshness"]["status"]
        == "fresh"
    )
    reopened = SQLiteStore(tmp_path / "trust.db")
    reopened.initialize()
    assert (
        build_market_intelligence(reopened, as_of=now + timedelta(seconds=121))[
            "pulse"
        ][0]["freshness"]["status"]
        == "stale"
    )
    future = _pulse_entry("BTCUSDT", {"realtime": state}, now - timedelta(seconds=1))
    assert future["freshness"]["status"] == "invalid_future"
    assert future["price"] is None


def test_unknown_news_date_never_becomes_now():
    assert _parse_date(None) is None
    assert _parse_date("not a timestamp") is None
    assert _parse_date("Mon, 07 Sep 2026 00:00:00") is None


def test_news_window_newest_first_not_macro_calendar():
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)

    class Store:
        def list_instruments(self):
            return []

        def list_watchlist_entries(self):
            return []

        def list_latest_prediction_records(self, _):
            return {}

        def list_realtime_states(self):
            return []

        def latest_daily_brief(self):
            return None

        def list_event_evidence(self, **_):
            return [
                {
                    "event_id": str(age),
                    "published_at": (now - timedelta(hours=age)).isoformat(),
                    "event_at": (now - timedelta(hours=age)).isoformat(),
                    "category": "macro",
                }
                for age in (47, 49, 1, 24, -1)
            ]

    view = build_market_intelligence(Store(), as_of=now)
    assert [item["event_id"] for item in view["news"]["items"]] == ["1", "24", "47"]
    assert view["calendar"]["status"] == "unavailable"
