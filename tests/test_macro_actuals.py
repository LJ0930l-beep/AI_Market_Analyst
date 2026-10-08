"""Offline fixtures for period-bound official macro actual qualification."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.macro_actuals import (
    BLS_SERIES, actual_from_bls, format_percent, map_macro_event,
    parse_eurostat_flash,
)
from core.macro_calendar import official_macro_news, refresh_calendar
from core.storage.sqlite import SQLiteStore


def bls_payload():
    return {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [
            {"seriesID": BLS_SERIES["unemployment"], "data": [
                {"year": "2026", "period": "M09", "value": "4.2"},
            ]},
            {"seriesID": BLS_SERIES["nonfarm_level"], "data": [
                {"year": "2026", "period": "M09", "value": "159044"},
                {"year": "2026", "period": "M08", "value": "159015"},
            ]},
            {"seriesID": BLS_SERIES["hourly_earnings"], "data": [
                {"year": "2026", "period": "M09", "value": "37.81"},
                {"year": "2026", "period": "M08", "value": "37.76"},
                {"year": "2025", "period": "M09", "value": "36.71"},
            ]},
        ]},
    }


def eurostat_html(*, publication="2026-10-02T09:00:00Z", reference="September 2026"):
    return f'''<!doctype html><html><head><title>Annual inflation up in the euro area</title>
    <script type="application/ld+json">{{"@type":"NewsArticle","headline":"Annual inflation up in the euro area",
    "description":"Flash estimate - {reference}","datePublished":"{publication}",
    "publisher":{{"name":"Eurostat","url":"https://ec.europa.eu/eurostat"}}}}</script></head><body>
    <p>Flash estimate - {reference}</p><table>
    <tr><th></th><th>Weights</th><th colspan="7">Annual rate</th><th>Monthly rate</th></tr>
    <tr><th></th><th>2026</th><th>Sep25</th><th>Apr26</th><th>May26</th><th>Jun26</th>
    <th>Jul26</th><th>Aug26</th><th>Sep26</th><th>Sep26</th></tr>
    <tr><th>All-items HICP</th><td>1000</td><td>2.2</td><td>3.0</td><td>3.2</td><td>2.8</td>
    <td>2.9</td><td>3.2</td><td>3.8e</td><td>0.6e</td></tr>
    </table></body></html>'''


def test_only_exact_headline_titles_are_mapped():
    cases = [
        ({"currency": "USD", "title": "Non-Farm Employment Change"}, "bls_nonfarm_change"),
        ({"currency": "USD", "title": "Unemployment Rate"}, "bls_unemployment_rate"),
        ({"currency": "USD", "title": "Average Hourly Earnings m/m"}, "bls_hourly_earnings_mom"),
        ({"currency": "USD", "title": "Average Hourly Earnings y/y"}, "bls_hourly_earnings_yoy"),
        ({"currency": "EUR", "title": "CPI Flash Estimate y/y"}, "eurostat_hicp_yoy"),
        ({"currency": "EUR", "title": "CPI Flash Estimate m/m"}, "eurostat_hicp_mom"),
        ({"currency": "USD", "title": "ADP Non-Farm Employment Change"}, None),
        ({"currency": "EUR", "title": "Core CPI Flash Estimate y/y"}, "eurostat_core_hicp_yoy"),
        ({"currency": "EUR", "title": "Spanish Flash CPI y/y"}, None),
    ]
    for event, expected in cases:
        assert map_macro_event(event) == expected


def test_bls_release_values_use_matching_month_series_and_half_up_rounding():
    data = {
        BLS_SERIES["unemployment"]: {"2026-09": "4.2"},
        BLS_SERIES["nonfarm_level"]: {"2026-09": "159044", "2026-08": "159015"},
        BLS_SERIES["hourly_earnings"]: {
            "2026-09": "37.81", "2026-08": "37.76", "2025-09": "36.71",
        },
    }
    assert actual_from_bls("bls_unemployment_rate", "2026-09", data)["actual"] == "4.2%"
    nfp = actual_from_bls("bls_nonfarm_change", "2026-09", data)
    assert (nfp["actual"], nfp["actual_value"], nfp["actual_unit"]) == ("29K", "29", "thousand_persons")
    assert actual_from_bls("bls_hourly_earnings_mom", "2026-09", data)["actual"] == "0.1%"
    assert actual_from_bls("bls_hourly_earnings_yoy", "2026-09", data)["actual"] == "3.0%"
    assert format_percent(Decimal("0.05")) == "0.1"
    with pytest.raises(KeyError, match="EXPECTED_PERIOD"):
        actual_from_bls("bls_nonfarm_change", "2026-10", data)


def test_eurostat_flash_uses_period_matched_colspan_and_month_columns():
    event = {"currency": "EUR", "title": "CPI Flash Estimate y/y",
             "event_time": "2026-10-02T09:00:00+00:00"}
    result = parse_eurostat_flash(
        eurostat_html(), event,
        fetched_at=datetime(2026, 10, 2, 9, 0, 2, tzinfo=timezone.utc),
        now=datetime(2026, 10, 2, 9, 1, tzinfo=timezone.utc),
    )
    assert result["actual"] == "3.8%"  # Sep 2026, not the first annual-history column (2.2)
    assert result["actual_value"] == "3.8"
    assert result["actual_reference_period"] == "2026-09"
    assert result["actual_is_estimate"] is True  # Eurostat's 3.8e marker
    monthly = parse_eurostat_flash(
        eurostat_html(), {**event, "title": "CPI Flash Estimate m/m"},
        fetched_at=datetime(2026, 10, 2, 9, 0, 2, tzinfo=timezone.utc),
        now=datetime(2026, 10, 2, 9, 1, tzinfo=timezone.utc),
    )
    assert monthly["actual"] == "0.6%"
    assert monthly["actual_basis"] == "month_over_month"


def test_eurostat_flash_rejects_wrong_publication_day_or_reference_period():
    event = {"currency": "EUR", "title": "CPI Flash Estimate y/y",
             "event_time": "2026-10-02T09:00:00+00:00"}
    fetched = datetime(2026, 10, 2, 10, tzinfo=timezone.utc)
    now = datetime(2026, 10, 2, 10, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="EVENT_MISMATCH"):
        parse_eurostat_flash(eurostat_html(publication="2026-10-01T09:00:00Z"), event,
                             fetched_at=fetched, now=now)
    with pytest.raises(ValueError, match="REFERENCE_PERIOD_MISMATCH"):
        parse_eurostat_flash(eurostat_html(reference="August 2026"), event,
                             fetched_at=fetched, now=now)


def test_eurostat_month_end_release_maps_to_same_month():
    event = {"currency": "EUR", "title": "CPI Flash Estimate y/y",
             "event_time": "2026-09-30T09:00:00+00:00"}
    result = parse_eurostat_flash(
        eurostat_html(publication="2026-09-30T09:00:00Z", reference="September 2026"), event,
        fetched_at=datetime(2026, 9, 30, 9, 0, 2, tzinfo=timezone.utc),
        now=datetime(2026, 9, 30, 9, 1, tzinfo=timezone.utc),
    )
    assert result["actual_reference_period"] == "2026-09"


def test_bls_writer_availability_is_completion_time_and_reader_has_asof_cutoff(tmp_path):
    store = SQLiteStore(tmp_path / "macro-actuals.db")
    store.initialize()
    started = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)
    completed = started + timedelta(seconds=2)
    rows = [
        {"title": "Non-Farm Employment Change", "country": "USD", "impact": "High",
         "date": started.isoformat(), "previous": "162K", "forecast": "89K", "actual": "999K"},
        {"title": "Unemployment Rate", "country": "USD", "impact": "High",
         "date": started.isoformat(), "previous": "4.3%", "forecast": "4.3%"},
        {"title": "Average Hourly Earnings m/m", "country": "USD", "impact": "High",
         "date": started.isoformat(), "previous": "0.3%", "forecast": "0.3%"},
        {"title": "Average Hourly Earnings y/y", "country": "USD", "impact": "High",
         "date": started.isoformat(), "previous": "3.7%", "forecast": "3.7%"},
    ]
    calls = []

    def fetch_series(start_year, end_year):
        calls.append((start_year, end_year))
        return bls_payload(), completed

    refresh_calendar(
        store, fetch=lambda: rows, now=started,
        actual_provider={"bls_fetch": fetch_series, "clock": lambda: completed},
    )
    events = store.v2_records("macro_events")
    assert len(events) == 4
    assert all(event["actual_status"] == "VERIFIED" for event in events)
    nfp = next(event for event in events if event["title"] == "Non-Farm Employment Change")
    assert nfp["actual"] == "29K" and nfp["actual_value"] == "29"
    assert (nfp["previous"], nfp["forecast"]) == ("162K", "89K")
    assert nfp["actual_available_at"] == completed.isoformat()
    assert nfp["known_at"] == completed.isoformat()
    assert nfp["actual_reference_period"] == "2026-09"
    assert calls == [(2025, 2026)]  # One v1 batch covers all four headline facts.

    assert official_macro_news(store, now=started + timedelta(seconds=1)) == []
    revision = official_macro_news(store, now=started + timedelta(seconds=3))[0]
    assert revision["scope"] == "MARKET_WIDE" and revision["impact"] == "UNKNOWN"
    assert len(revision["macro_releases"]) == 4
    release = next(item for item in revision["macro_releases"] if item["title"] == nfp["title"])
    assert release["available_at"] == completed.isoformat()
    assert release["published_at"] == started.isoformat()
    assert release["actual"] == "29K"
    assert release["source_url"].startswith("https://www.bls.gov/news.release/archives/")


def test_bls_period_mismatch_retries_after_15m_and_then_uses_one_batch(tmp_path):
    store = SQLiteStore(tmp_path / "macro-retry.db")
    store.initialize()
    started = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)
    current_clock = [started]
    attempts = []
    rows = [{"title": "Non-Farm Employment Change", "country": "USD", "impact": "High",
             "date": started.isoformat(), "previous": "162K", "forecast": "89K"}]
    missing_period = bls_payload()
    missing_period["Results"]["series"][1]["data"] = [
        {"year": "2026", "period": "M08", "value": "159015"},
    ]

    def fetch_series(start_year, end_year):
        attempts.append((start_year, end_year))
        current_clock[0] += timedelta(seconds=2)
        payload = missing_period if len(attempts) == 1 else bls_payload()
        return payload, current_clock[0]

    providers = {"bls_fetch": fetch_series, "clock": lambda: current_clock[0]}
    refresh_calendar(store, fetch=lambda: rows, now=started, actual_provider=providers)
    first = store.v2_records("macro_events")[0]
    assert first["actual_status"] == "PERIOD_MISMATCH"
    retry_at = started + timedelta(minutes=16)
    current_clock[0] = retry_at
    refresh_calendar(store, fetch=lambda: rows, now=retry_at, actual_provider=providers)
    retried = store.v2_records("macro_events")[0]
    assert retried["actual_status"] == "VERIFIED"
    assert retried["actual"] == "29K"
    assert len(attempts) == 2


def test_stale_actual_is_retained_with_error_after_later_fetch_failure(tmp_path):
    store = SQLiteStore(tmp_path / "macro-stale.db")
    store.initialize()
    started = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)
    rows = [{"title": "Non-Farm Employment Change", "country": "USD", "impact": "High",
             "date": started.isoformat(), "previous": "162K", "forecast": "89K"}]
    current_clock = [started + timedelta(seconds=2)]
    refresh_calendar(store, fetch=lambda: rows, now=started,
                     actual_provider={"bls_fetch": lambda *_: (bls_payload(), current_clock[0]),
                                      "clock": lambda: current_clock[0]})
    first = store.v2_records("macro_events")[0]
    old_available = first["actual_available_at"]
    with store._connect() as db:
        first["actual_status"] = "STALE"
        db.execute("UPDATE macro_events SET payload_json=? WHERE event_id=?",
                   (__import__("json").dumps(first), first["event_id"]))
    store.set_scheduler_state("macro_actuals.bls_snapshot", {}, updated_at=started.isoformat())
    retry_at = started + timedelta(hours=7)
    current_clock[0] = retry_at + timedelta(seconds=2)
    result = refresh_calendar(
        store, fetch=lambda: rows, now=retry_at,
        actual_provider={"bls_fetch": lambda *_: (_ for _ in ()).throw(OSError("offline")),
                         "clock": lambda: current_clock[0]},
    )
    retained = store.v2_records("macro_events")[0]
    assert result["status"] == "SCHEDULE_ONLY"
    assert retained["actual_status"] == "STALE"
    assert retained["actual"] == "29K"
    assert retained["actual_available_at"] == old_available
    assert retained["known_at"] == old_available
    assert retained["actual_error"].startswith("BLS_FETCH_FAILED:")


def test_cached_schedule_can_receive_actual_while_schedule_feed_is_down(tmp_path):
    store = SQLiteStore(tmp_path / "macro-feed-fallback.db")
    store.initialize()
    started = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)
    rows = [{"title": "Non-Farm Employment Change", "country": "USD", "impact": "High",
             "date": started.isoformat(), "previous": "162K", "forecast": "89K"}]
    first = refresh_calendar(store, fetch=lambda: rows, now=started)
    original_last_success = first["last_success_at"]
    current_clock = [started + timedelta(minutes=16, seconds=2)]
    fallback = refresh_calendar(
        store, fetch=lambda: (_ for _ in ()).throw(OSError("schedule unavailable")),
        now=started + timedelta(minutes=16),
        actual_provider={"bls_fetch": lambda *_: (bls_payload(), current_clock[0]),
                         "clock": lambda: current_clock[0]},
    )
    event = store.v2_records("macro_events")[0]
    assert fallback["status"] == "SCHEDULE_FETCH_FAILED"
    assert fallback["last_success_at"] == original_last_success
    assert fallback["actual_verified_count"] == 1
    assert event["actual_status"] == "VERIFIED" and event["actual"] == "29K"


def test_official_news_does_not_read_stale_future_or_untrusted_actual_rows(tmp_path):
    store = SQLiteStore(tmp_path / "macro-reader.db")
    store.initialize()
    now = datetime.now(timezone.utc)
    base = {
        "event_id": "official", "title": "Non-Farm Employment Change", "currency": "USD",
        "event_time": now.isoformat(), "origin": "public_calendar_schedule", "actual_status": "VERIFIED",
        "actual": "29K", "actual_value": "29", "actual_unit": "thousand_persons",
        "actual_reference_period": "2026-09", "actual_provider": "BLS",
        "actual_source_url": "https://www.bls.gov/news.release/archives/empsit_10022026.htm",
        "actual_published_at": now.isoformat(), "actual_available_at": now.isoformat(),
        "known_at": now.isoformat(), "forecast": "89K", "previous": "162K",
    }
    records = [
        base,
        {**base, "event_id": "future-known", "actual_available_at": (now + timedelta(seconds=1)).isoformat(),
         "known_at": (now + timedelta(seconds=1)).isoformat()},
        {**base, "event_id": "stale", "actual_status": "STALE"},
        {**base, "event_id": "wrong-host", "actual_source_url": "https://example.com/fake"},
        {**base, "event_id": "old", "actual_published_at": (now - timedelta(hours=49)).isoformat(),
         "event_time": (now - timedelta(hours=49)).isoformat(),
         "actual_available_at": (now - timedelta(hours=49)).isoformat(),
         "known_at": (now - timedelta(hours=49)).isoformat()},
    ]
    with store._connect() as db:
        for item in records:
            db.execute("INSERT INTO macro_events VALUES(?,?,?)", (
                item["event_id"], __import__("json").dumps(item), now.isoformat()))
    result = official_macro_news(store, now=now)
    assert len(result) == 1
    assert [item["event_id"] for item in result[0]["macro_releases"]] == ["official"]


def test_core_flash_selects_exact_exclusion_row_not_headline_or_energy_only():
    html=eurostat_html().replace('</table>', '<tr><th>energy</th><td>100</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>9.9e</td><td>0.1e</td></tr><tr><th>energy, food, alcohol &amp; tobacco</th><td>700</td><td>2</td><td>2</td><td>2</td><td>2</td><td>2</td><td>2</td><td>2.5e</td><td>0.2e</td></tr></table>')
    at=datetime(2026,10,2,9,tzinfo=timezone.utc)
    result=parse_eurostat_flash(html,{'currency':'EUR','title':'Core CPI Flash Estimate y/y','event_time':at.isoformat()},fetched_at=at+timedelta(seconds=1),now=at+timedelta(seconds=2))
    assert result['actual']=='2.5%'
    assert result['actual_basis']=='year_over_year'
    assert result['actual_is_estimate']


def test_new_registry_enriches_cached_schedule_during_cooldown_without_refetch(tmp_path):
    from core.macro_calendar import ACTUAL_ADAPTER_VERSION
    store=SQLiteStore(tmp_path/'registry-bootstrap.db');store.initialize()
    at=datetime(2026,10,2,12,30,tzinfo=timezone.utc)
    rows=[{'title':'Unemployment Rate','country':'USD','impact':'High','date':at.isoformat()}]
    original=refresh_calendar(store,fetch=lambda:rows,now=at)
    completed=at+timedelta(seconds=4)
    result=refresh_calendar(store,fetch=lambda:pytest.fail('cooldown must not refetch schedule'),now=at+timedelta(seconds=2),actual_provider={'bls_fetch':lambda *_:(bls_payload(),completed),'clock':lambda:completed},clock=lambda:completed)
    assert result['refresh_deferred']
    assert result['actual_adapter_version']==ACTUAL_ADAPTER_VERSION
    assert result['last_success_at']==original['last_success_at']
    assert result['actual_verified_count']==1
    assert store.v2_records('macro_events')[0]['actual']=='4.2%'
