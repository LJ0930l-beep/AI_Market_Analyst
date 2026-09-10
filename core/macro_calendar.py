"""Public economic schedule cache. Never manufactures actuals or trading permission."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from urllib.request import Request, urlopen

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE_URL = "https://www.forexfactory.com/calendar?week=this"
_lock = threading.Lock()


def normalize_calendar(rows, now):
    if not isinstance(rows, list):
        raise ValueError("日历数据格式错误")
    events = []
    for row in rows[:1000]:
        if not isinstance(row, dict) or row.get("impact") not in {"High", "Medium"}:
            continue
        try:
            at = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
            if at.tzinfo is None:
                continue
            at = at.astimezone(timezone.utc)
            if not now - timedelta(days=7) <= at <= now + timedelta(days=14):
                continue
            title = str(row["title"])[:240]
            currency = str(row.get("country", ""))[:10]
        except (ValueError, KeyError, TypeError):
            continue
        identity = hashlib.sha256(f"{currency}|{title}|{at.isoformat()}".encode()).hexdigest()[:24]
        events.append({
            "event_id": "ff_" + identity, "title": title,
            "currency": currency, "event_time": at.isoformat(),
            "known_at": now.isoformat(), "fetched_at": now.isoformat(),
            "previous": str(row["previous"])[:100] if row.get("previous") not in (None, "") else None,
            "forecast": str(row["forecast"])[:100] if row.get("forecast") not in (None, "") else None,
            # The weekly export does NOT publish actual values. Do not infer them.
            "actual": None, "actual_status": "NOT_PROVIDED",
            "importance_stars": 3 if row["impact"] == "High" else 2,
            "source_url": SOURCE_URL, "provider": "Forex Factory",
            "origin": "public_calendar_schedule", "provider_verified": False,
            "directive": "NONE", "directive_expires_at": now.isoformat(),
            "ai_status": "NOT_ANALYZED", "schedule_only": True,
        })
    return sorted(events, key=lambda item: item["event_time"])


def calendar_status(store):
    state = store.get_scheduler_state("macro_calendar.status")
    state = dict(state) if isinstance(state, dict) else {"status": "NOT_FETCHED"}
    events = store.v2_records("macro_events")
    state["event_count"] = len(events)
    state["provider"] = "Forex Factory"
    state["source_url"] = SOURCE_URL
    state["actual_supported"] = False
    try:
        fetched = datetime.fromisoformat(state.get("last_success_at", ""))
        if datetime.now(timezone.utc) - fetched > timedelta(hours=6):
            state["status"] = "STALE"
    except (TypeError, ValueError):
        pass
    return state


def refresh_calendar(store, *, fetch=None, now=None):
    """Explicit/background write; GET handlers only read this cache.

    Fixed public host, bounded payload, timeout, cooldown and no redirect to
    configurable hosts. Failures retain last successful data and its timestamp.
    """
    now = now or datetime.now(timezone.utc)
    with _lock:
        prior = calendar_status(store)
        try:
            last = datetime.fromisoformat(prior.get("last_attempt_at", ""))
            if timedelta(0) <= now - last < timedelta(minutes=15):
                return prior
        except (ValueError, TypeError):
            pass
        state = {**prior, "last_attempt_at": now.isoformat()}
        try:
            if fetch is None:
                request = Request(FEED_URL, headers={"User-Agent": "AI-Market-Analyst/2.0 (public calendar)"})
                with urlopen(request, timeout=10) as response:
                    if not response.geturl().startswith("https://nfs.faireconomy.media/"):
                        raise ValueError("日历来源重定向异常")
                    raw = response.read(1_000_001)
                    if len(raw) > 1_000_000:
                        raise ValueError("日历响应超出大小限制")
                    rows = json.loads(raw)
            else:
                rows = fetch()
            events = normalize_calendar(rows, now)
            if not events:
                raise ValueError("数据源未返回当前时段可用的中高影响事件")
            with store._connect() as db:
                # Replace only our own cache, never manual imports or user evidence.
                db.execute("DELETE FROM macro_events WHERE json_extract(payload_json, '$.origin')='public_calendar_schedule'")
                for event in events:
                    db.execute("INSERT INTO macro_events VALUES(?,?,?) ON CONFLICT(event_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                               (event["event_id"], json.dumps(event, ensure_ascii=False), now.isoformat()))
            state.update(status="SCHEDULE_ONLY", last_success_at=now.isoformat(), error=None, event_count=len(events))
        except Exception as exc:
            # No raw network exception/URL credentials in user-visible status.
            state.update(status="UNAVAILABLE", error=f"公开日历更新失败（{type(exc).__name__}），保留上次缓存；请检查网络后重试。")
        store.set_scheduler_state("macro_calendar.status", state, updated_at=now.isoformat())
        return calendar_status(store)
