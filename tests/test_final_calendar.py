from datetime import datetime, timedelta, timezone

from core.macro_calendar import calendar_status, normalize_calendar, refresh_calendar
from core.storage import SQLiteStore
from core.agent_execution import MacroGuard


def test_calendar_provenance_and_no_invented_actual_or_permission(tmp_path):
    now = datetime.now(timezone.utc)
    rows = [{"title": "Core CPI y/y", "country": "USD", "impact": "High",
             "date": now.isoformat(), "previous": "0", "forecast": "0", "actual": "999"}]
    store = SQLiteStore(tmp_path / "calendar.db")
    store.initialize()
    result = refresh_calendar(store, fetch=lambda: rows, now=now)
    assert result["status"] == "SCHEDULE_ONLY"
    event = store.v2_records("macro_events")[0]
    assert event["actual"] is None
    assert event["previous"] == "0"
    assert event["forecast"] == "0"
    assert not event["provider_verified"]
    assert MacroGuard([event]).check("LONG", now) == "MACRO_UNAVAILABLE"
    # Reads and cooldown must not call a provider or change the evidence.
    assert calendar_status(store)["event_count"] == 1
    assert refresh_calendar(store, fetch=lambda: 1 / 0, now=now)["status"] == "SCHEDULE_ONLY"
    assert store.v2_records("macro_events") == [event]


def test_calendar_failure_preserves_cache_and_manual_import(tmp_path):
    now = datetime.now(timezone.utc)
    store = SQLiteStore(tmp_path / "calendar.db")
    store.initialize()
    with store._connect() as db:
        db.execute("INSERT INTO macro_events VALUES('manual','{\"event_id\":\"manual\",\"origin\":\"explicit_local_import\"}',?)", (now.isoformat(),))
    row = {"title": "CPI", "country": "USD", "impact": "High", "date": now.isoformat()}
    refresh_calendar(store, fetch=lambda: [row], now=now)
    events = store.v2_records("macro_events")
    failed = refresh_calendar(store, fetch=lambda: {"bad": "schema"}, now=now + timedelta(minutes=16))
    assert failed["status"] == "UNAVAILABLE"
    assert failed["last_success_at"] == now.isoformat()
    assert store.v2_records("macro_events") == events


def test_calendar_rejects_ambiguous_dates_and_old_data():
    now = datetime.now(timezone.utc)
    assert normalize_calendar([
        {"title": "CPI", "impact": "High", "date": "2026-09-09T00:00:00"},
        {"title": "CPI", "impact": "High", "date": (now - timedelta(days=30)).isoformat()},
        {"title": "CPI", "impact": "High", "date": "bad"},
    ], now) == []


def test_trader_style_does_not_invent_measured_scores():
    from core.analysis.ai_trade_analytics import _generate_trader_style_dna
    result = _generate_trader_style_dna(win_rate=0, avg_leverage=35, profit_factor=1,
        max_drawdown=0, trades_count=0, strategy_summary=[])
    assert result["discipline_score"] is None
    assert result["avg_leverage"] is None
    assert all(value is None for value in result["leverage_distribution"].values())
    assert "暂无正收益" in result["best_strategy"]
