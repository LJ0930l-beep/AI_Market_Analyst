"""Public economic schedule cache. Never manufactures actuals or trading permission."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .macro_actuals import (
    BLS_API_URL, BLS_SERIES, actual_from_bls,
    bls_release_url, eurostat_flash_url, fetch_bls_series,
    fetch_eurostat_page, iso_utc, map_macro_event, month_before,
    parse_bls_response, parse_eurostat_flash, required_bls_periods,
    eurostat_flash_reference_period, utc_datetime,
)
from .macro_qualitative_sources import map_qualitative_event, fetch_official_text

ACTUAL_ADAPTER_VERSION = "official-macro-v2-20261003"


def _external_adapter(event):
    from .macro_release_sources import map_release_event, fetch_release_actual
    from .macro_international_sources import map_international_event, fetch_international_actual
    for family, mapper, reader in (("release", map_release_event, fetch_release_actual),
                                  ("international", map_international_event, fetch_international_actual)):
        metric = mapper(event)
        if metric:
            return family, metric, reader
    return None


def _mapped_official_metric(event):
    local = map_macro_event(event)
    if local:
        return local
    adapter = _external_adapter(event)
    return adapter[1] if adapter else None

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
            "known_at": now.isoformat(), "schedule_known_at": now.isoformat(),
            "fetched_at": now.isoformat(),
            "previous": str(row["previous"])[:100] if row.get("previous") not in (None, "") else None,
            "forecast": str(row["forecast"])[:100] if row.get("forecast") not in (None, "") else None,
            # The weekly export does NOT publish actual values. Do not infer them.
            "actual": None, "actual_value": None, "actual_display": None,
            "actual_status": "NOT_PROVIDED",
            "actual_provider": None, "actual_source_url": None,
            "actual_data_url": None, "actual_reference_period": None,
            "actual_unit": None, "actual_basis": None, "actual_method": None,
            "actual_is_estimate": None, "actual_published_at": None,
            "actual_available_at": None, "actual_fetched_at": None,
            "actual_error": None,
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
    state["actual_supported"] = True
    state["actual_providers"] = sorted({str(item["actual_provider"]) for item in events
                                        if item.get("actual_status") == "VERIFIED" and item.get("actual_provider")})
    state["actual_adapter_version"] = state.get("actual_adapter_version")
    state["qualitative_event_count"] = sum(bool(map_qualitative_event(item)) for item in events)
    state["qualitative_verified_count"] = sum(item.get("qualitative_status") == "OFFICIAL_TEXT_AVAILABLE" for item in events)
    state["qualitative_recording_count"] = sum(item.get("qualitative_status") == "OFFICIAL_RECORDING_AVAILABLE" for item in events)
    state["numeric_event_count"] = len(events) - state["qualitative_event_count"]
    try:
        next_refresh = utc_datetime(state["last_attempt_at"]) + timedelta(minutes=15)
        state["next_refresh_at"] = iso_utc(next_refresh)
        state["retry_after_seconds"] = max(0, int((next_refresh - datetime.now(timezone.utc)).total_seconds()))
    except (KeyError, ValueError, TypeError):
        state["retry_after_seconds"] = 0
    status_counts = {}
    for event in events:
        status = str(event.get("actual_status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1
    state["actual_status_counts"] = status_counts
    state["actual_verified_count"] = status_counts.get("VERIFIED", 0)
    state["actual_error_count"] = sum(
        status_counts.get(status, 0)
        for status in ("SOURCE_FETCH_FAILED", "PERIOD_MISMATCH", "STALE")
    )
    try:
        fetched = datetime.fromisoformat(state.get("last_success_at", ""))
        if datetime.now(timezone.utc) - fetched > timedelta(hours=6):
            if state.get("status") == "SCHEDULE_FETCH_FAILED":
                state["schedule_cache_status"] = "STALE"
            else:
                state["status"] = "STALE"
    except (TypeError, ValueError):
        pass
    return state


_ACTUAL_FIELDS = (
    "actual", "actual_value", "actual_display", "actual_status", "actual_provider", "actual_source_url",
    "actual_data_url", "actual_reference_period", "actual_unit",
    "actual_basis", "actual_method", "actual_is_estimate",
    "actual_published_at", "actual_available_at", "actual_fetched_at",
    "actual_published_at_source", "actual_error", "actual_series_ids",
    "actual_data_year_range",
    "qualitative_status", "qualitative_provider", "qualitative_source_url",
    "qualitative_title", "qualitative_summary", "qualitative_published_at", "qualitative_available_at",
    "qualitative_error",
    "qualitative_published_at_source", "qualitative_conference_url",
)
_ACTUAL_RETRY = timedelta(hours=6)
_EARLY_RETRY = timedelta(minutes=15)
_EARLY_RETRY_WINDOW = timedelta(hours=2)
_EARLY_RETRY_LIMIT = 8


def _stored_actual_state(store, key, default):
    value = store.get_scheduler_state(key)
    return value if isinstance(value, type(default)) else default


def _snapshot_values(snapshot):
    series = snapshot.get("series") if isinstance(snapshot, dict) else None
    values = {}
    if not isinstance(series, dict):
        return values
    for series_id, rows in series.items():
        if isinstance(rows, dict):
            values[series_id] = {
                period: str(item.get("value"))
                for period, item in rows.items()
                if isinstance(item, dict) and item.get("value") is not None
            }
    return values


def _snapshot_row(snapshot, series_id, period):
    try:
        row = snapshot["series"][series_id][period]
        return row if isinstance(row, dict) and row.get("value") is not None else None
    except (KeyError, TypeError):
        return None


def _snapshot_covers(snapshot, metric, reference_period, event_time, now):
    try:
        for period in required_bls_periods(metric, reference_period):
            series_id = (
                BLS_SERIES["unemployment"] if metric == "bls_unemployment_rate"
                else BLS_SERIES["nonfarm_level"] if metric == "bls_nonfarm_change"
                else BLS_SERIES["hourly_earnings"]
            )
            row = _snapshot_row(snapshot, series_id, period)
            if row is None:
                return False
            fetched_at = utc_datetime(row.get("fetched_at"))
            if fetched_at < event_time or fetched_at > now:
                return False
        return True
    except (TypeError, ValueError):
        return False


def _completion(value):
    """Accept provider (payload, completion_time) tuples used by live and fixture readers."""
    if isinstance(value, tuple) and len(value) == 2:
        payload, completed_at = value
        return payload, utc_datetime(completed_at)
    return value, datetime.now(timezone.utc)


def _mark_actual_error(event, status, error):
    event["actual_error"] = str(error)[:180]
    # A previously verified observation remains visible but is explicitly stale
    # if a later refresh that needed it failed.
    event["actual_status"] = "STALE" if event.get("actual") is not None else status


def _in_actual_backoff(attempt, now):
    if not isinstance(attempt, dict):
        return False
    try:
        attempted_at = utc_datetime(attempt.get("attempted_at"))
        first_at = utc_datetime(attempt.get("first_attempt_at", attempt.get("attempted_at")))
        age = now - first_at
        count = int(attempt.get("attempt_count", 1))
        delay = _EARLY_RETRY if age < _EARLY_RETRY_WINDOW and count < _EARLY_RETRY_LIMIT else _ACTUAL_RETRY
        return timedelta(0) <= now - attempted_at < delay
    except (TypeError, ValueError, OverflowError):
        return False


def _record_actual_attempt(attempts, key, at, status, error=None, *, increment=False):
    previous = attempts.get(key) if isinstance(attempts.get(key), dict) else {}
    attempts[key] = {
        "first_attempt_at": previous.get("first_attempt_at") or iso_utc(at),
        "attempted_at": iso_utc(at),
        "attempt_count": max(0, int(previous.get("attempt_count", 0) or 0)) + int(increment),
        "status": status,
        "error": error,
    }


def _carry_forward_actuals(events, prior_events, now, *, schedule_refreshed=True):
    prior_by_id = {
        item.get("event_id"): item for item in prior_events
        if isinstance(item, dict) and item.get("origin") == "public_calendar_schedule"
    }
    for event in events:
        prior = prior_by_id.get(event.get("event_id"))
        if not prior:
            continue
        for key in _ACTUAL_FIELDS:
            if key in prior:
                event[key] = prior[key]
        if event.get("actual") is not None:
            event["known_at"] = prior.get("known_at", prior.get("actual_available_at", event["known_at"]))
        if schedule_refreshed:
            event["schedule_known_at"] = now.isoformat()
    return events


def _apply_bls_result(event, metric, reference_period, snapshot, now):
    values = _snapshot_values(snapshot)
    result = actual_from_bls(metric, reference_period, values)
    required_periods = required_bls_periods(metric, reference_period)
    series_id = (
        BLS_SERIES["unemployment"] if metric == "bls_unemployment_rate"
        else BLS_SERIES["nonfarm_level"] if metric == "bls_nonfarm_change"
        else BLS_SERIES["hourly_earnings"]
    )
    rows = [_snapshot_row(snapshot, series_id, period) for period in required_periods]
    if any(row is None for row in rows):
        raise KeyError("BLS_EXPECTED_PERIOD_MISSING")
    completed_at = max(utc_datetime(row["fetched_at"]) for row in rows)
    if completed_at > now:
        raise ValueError("BLS_SOURCE_NOT_YET_KNOWN_AS_OF")
    event_time = utc_datetime(event["event_time"])
    if completed_at < event_time:
        raise ValueError("BLS_SOURCE_PRECEDES_RELEASE")
    fetched_at = max(utc_datetime(row["fetched_at"]) for row in rows)
    event.update(result)
    event["actual_display"] = result["actual"]
    event.update({
        "actual_status": "VERIFIED", "actual_provider": "BLS",
        "actual_source_url": bls_release_url(event_time),
        "actual_data_url": BLS_API_URL,
        "actual_series_ids": [BLS_SERIES["unemployment"] if metric == "bls_unemployment_rate"
                              else BLS_SERIES["nonfarm_level"] if metric == "bls_nonfarm_change"
                              else BLS_SERIES["hourly_earnings"]],
        "actual_data_year_range": [min(int(p[:4]) for p in required_periods), max(int(p[:4]) for p in required_periods)],
        "actual_reference_period": reference_period,
        "actual_published_at": event_time.isoformat(),
        "actual_published_at_source": "scheduled_release_time",
        "actual_available_at": iso_utc(completed_at),
        "actual_fetched_at": iso_utc(fetched_at),
        "actual_error": None, "known_at": iso_utc(completed_at),
    })


def _sync_actuals(store, events, prior_events, now, providers, clock, *, schedule_refreshed=True):
    """Enrich only exact mapped releases; cached values never move their known_at forward."""
    events = _carry_forward_actuals(events, prior_events, now, schedule_refreshed=schedule_refreshed)
    snapshot = _stored_actual_state(store, "macro_actuals.bls_snapshot", {})
    attempts = _stored_actual_state(store, "macro_actuals.attempts", {})
    snapshot = dict(snapshot)
    attempts = dict(attempts)
    bls_fetch = providers.get("bls_fetch", fetch_bls_series)
    eurostat_fetch = providers.get("eurostat_fetch", fetch_eurostat_page)

    bls_pending = []
    eurostat_pending = []
    external_pending = []
    for event in events:
        metric = map_macro_event(event)
        if metric is None:
            source = map_qualitative_event(event)
            adapter = _external_adapter(event) if source is None else None
            if source:
                event.update(actual=None, actual_value=None, actual_display=None,
                             actual_status="NON_NUMERIC_EVENT", actual_error=None)
                if event.get("qualitative_status") in {"OFFICIAL_TEXT_AVAILABLE", "OFFICIAL_RECORDING_AVAILABLE"}:
                    continue
                if utc_datetime(event["event_time"]) > now:
                    event["qualitative_status"] = "PENDING_RELEASE"
                    continue
                if str(event.get("currency", "")).upper() == "AUD":
                    from .macro_international_sources import fetch_rba_text
                    text_reader = fetch_rba_text
                else:
                    text_reader = fetch_official_text
                external_pending.append((event, "qualitative", "official_text", text_reader))
                continue
            if adapter:
                if event.get("actual_status") == "VERIFIED" and event.get("actual") is not None:
                    continue
                if utc_datetime(event["event_time"]) > now:
                    event.update(actual_status="PENDING_RELEASE", actual_error=None)
                    continue
                family, external_metric, reader = adapter
                external_pending.append((event, family, external_metric, reader))
                continue
            if event.get("actual") is None:
                event["actual_status"] = "UNMAPPED"
                event["actual_error"] = "NO_SUPPORTED_OFFICIAL_ACTUAL_MAPPING"
            continue
        if event.get("actual_status") == "VERIFIED" and event.get("actual") is not None:
            continue
        event_time = utc_datetime(event["event_time"])
        if event_time > now:
            event["actual_status"] = "PENDING_RELEASE"
            event["actual_error"] = None
            continue
        reference_period = (month_before(event_time) if metric.startswith("bls_")
                            else eurostat_flash_reference_period(event_time))
        if metric.startswith("bls_"):
            bls_pending.append((event, metric, reference_period))
        else:
            eurostat_pending.append((event, metric, reference_period))

    # The normalized series cache is durable across process restarts. A new
    # reference period gets one batch request even when the previous cache is
    # still fresh; failed periods then back off for six hours.
    needing_bls = [
        item for item in bls_pending
        if not _snapshot_covers(snapshot, item[1], item[2], utc_datetime(item[0]["event_time"]), now)
    ]
    to_fetch = []
    for item in needing_bls:
        event, metric, ref = item
        attempt_key = f"bls:{ref}"
        previous_attempt = attempts.get(attempt_key)
        if _in_actual_backoff(previous_attempt, now):
            _mark_actual_error(event, previous_attempt.get("status", "SOURCE_FETCH_FAILED"),
                              previous_attempt.get("error", "BLS_RETRY_BACKOFF"))
            continue
        to_fetch.append(item)

    if to_fetch:
        refs = [period for _, metric, ref in to_fetch for period in required_bls_periods(metric, ref)]
        start_year = min(int(period[:4]) for period in refs)
        end_year = max(int(period[:4]) for period in refs)
        try:
            response, fetched_at = _completion(bls_fetch(start_year, end_year))
            write_now = utc_datetime((providers.get("clock") or clock)())
            if fetched_at > write_now:
                raise ValueError("BLS_PROVIDER_TIMESTAMP_IN_FUTURE")
            parsed = parse_bls_response(response)
            previous_series = snapshot.get("series", {}) if isinstance(snapshot.get("series"), dict) else {}
            merged = {series_id: dict(rows) for series_id, rows in previous_series.items() if isinstance(rows, dict)}
            for series_id, rows in parsed.items():
                target = merged.setdefault(series_id, {})
                for period, value in rows.items():
                    target[period] = {"value": value, "fetched_at": iso_utc(fetched_at)}
            snapshot = {"series": merged, "last_fetched_at": iso_utc(fetched_at)}
            for ref in sorted({ref for _, _, ref in to_fetch}):
                _record_actual_attempt(attempts, f"bls:{ref}", fetched_at, "PERIOD_MISMATCH", None, increment=True)
            for event, metric, ref in to_fetch:
                attempt_key = f"bls:{ref}"
                try:
                    _apply_bls_result(event, metric, ref, snapshot, write_now)
                    _record_actual_attempt(attempts, attempt_key, fetched_at, "VERIFIED")
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    status = "PERIOD_MISMATCH" if "PERIOD" in str(exc) or "MISSING" in str(exc) else "SOURCE_FETCH_FAILED"
                    _mark_actual_error(event, status, str(exc) or type(exc).__name__)
                    _record_actual_attempt(attempts, attempt_key, fetched_at, status, event["actual_error"])
        except Exception as exc:
            completed_at = utc_datetime((providers.get("clock") or clock)())
            for ref in sorted({ref for _, _, ref in to_fetch}):
                _record_actual_attempt(attempts, f"bls:{ref}", completed_at, "SOURCE_FETCH_FAILED",
                                       f"BLS_FETCH_FAILED:{type(exc).__name__}", increment=True)
            for event, _, _ in to_fetch:
                _mark_actual_error(event, "SOURCE_FETCH_FAILED", f"BLS_FETCH_FAILED:{type(exc).__name__}")

    # If cached series already covers a newly scheduled metric, no request is
    # needed. It must still be both published and fetched no earlier than the
    # release itself before it can be shown as an actual.
    for event, metric, ref in bls_pending:
        if event.get("actual_status") == "VERIFIED":
            continue
        write_now = utc_datetime((providers.get("clock") or clock)())
        if _snapshot_covers(snapshot, metric, ref, utc_datetime(event["event_time"]), write_now):
            try:
                _apply_bls_result(event, metric, ref, snapshot, write_now)
                _record_actual_attempt(attempts, f"bls:{ref}", utc_datetime(event["actual_available_at"]), "VERIFIED")
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                status = "PERIOD_MISMATCH" if "PERIOD" in str(exc) or "MISSING" in str(exc) else "SOURCE_FETCH_FAILED"
                _mark_actual_error(event, status, str(exc) or type(exc).__name__)

    # One exact Eurostat dated flash article can supply both annual and monthly
    # HICP facts. Fetch at most once per publication date in this refresh.
    page_cache = {}
    for event, metric, _ref in eurostat_pending:
        event_time = utc_datetime(event["event_time"])
        attempt_key = f"eurostat:{event_time:%Y-%m-%d}"
        previous_attempt = attempts.get(attempt_key)
        if previous_attempt and previous_attempt.get("status") != "VERIFIED" and _in_actual_backoff(previous_attempt, now):
            _mark_actual_error(event, previous_attempt.get("status", "SOURCE_FETCH_FAILED"),
                              previous_attempt.get("error", "EUROSTAT_RETRY_BACKOFF"))
            continue
        url = eurostat_flash_url(event_time)
        if url not in page_cache:
            try:
                page_cache[url] = ("ok", _completion(eurostat_fetch(url)))
            except Exception as exc:
                page_cache[url] = ("error", f"EUROSTAT_FETCH_FAILED:{type(exc).__name__}")
        state, result = page_cache[url]
        if state == "error":
            _mark_actual_error(event, "SOURCE_FETCH_FAILED", result)
            if attempt_key not in page_cache.get("attempted_dates", set()):
                _record_actual_attempt(attempts, attempt_key, utc_datetime(clock()), event["actual_status"],
                                       event["actual_error"], increment=True)
                page_cache.setdefault("attempted_dates", set()).add(attempt_key)
            continue
        html, fetched_at = result
        try:
            write_now = utc_datetime((providers.get("clock") or clock)())
            if fetched_at > write_now:
                raise ValueError("EUROSTAT_PROVIDER_TIMESTAMP_IN_FUTURE")
            actual = parse_eurostat_flash(html, event, fetched_at=fetched_at, now=write_now)
            if attempt_key not in page_cache.get("attempted_dates", set()):
                _record_actual_attempt(attempts, attempt_key, fetched_at, "PERIOD_MISMATCH", None, increment=True)
                page_cache.setdefault("attempted_dates", set()).add(attempt_key)
            event.update(actual)
            event["actual_display"] = actual["actual"]
            event.update({
                "actual_status": "VERIFIED", "actual_provider": "Eurostat",
                "actual_source_url": url, "actual_data_url": None,
                "actual_published_at_source": "official_release_page",
                "actual_available_at": iso_utc(fetched_at), "actual_fetched_at": iso_utc(fetched_at),
                "actual_error": None, "known_at": iso_utc(fetched_at),
            })
            _record_actual_attempt(attempts, attempt_key, fetched_at, "VERIFIED")
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            status = "PERIOD_MISMATCH" if any(token in str(exc) for token in ("PERIOD", "EVENT_MISMATCH", "NOT_FLASH")) else "SOURCE_FETCH_FAILED"
            _mark_actual_error(event, status, str(exc) or type(exc).__name__)
            if attempt_key not in page_cache.get("attempted_dates", set()):
                _record_actual_attempt(attempts, attempt_key, fetched_at, status, event["actual_error"], increment=True)
                page_cache.setdefault("attempted_dates", set()).add(attempt_key)
            else:
                _record_actual_attempt(attempts, attempt_key, fetched_at, status, event["actual_error"])

    # Independent official sources are fetched concurrently; DB writes stay on
    # the scheduler thread. Per-event attempts persist across restarts.
    jobs = []
    for event, family, metric, reader in external_pending:
        key = f"{ACTUAL_ADAPTER_VERSION}:{metric}:{event['event_id']}"
        previous = attempts.get(key)
        if _in_actual_backoff(previous, now):
            if family == "qualitative":
                event.update(qualitative_status="OFFICIAL_TEXT_PENDING", qualitative_error=previous.get("error"))
            else:
                _mark_actual_error(event, previous.get("status", "SOURCE_FETCH_FAILED"), previous.get("error", "OFFICIAL_RETRY_BACKOFF"))
            continue
        jobs.append((event, family, reader, key))
    def read_external(job):
        event, family, reader, key = job
        try:
            selected = providers.get(f"{family}_fetch")
            result = selected(event) if selected else reader(event, now=now, clock=providers.get("clock") or clock)
            return result, None
        except Exception as exc:
            return None, exc
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(read_external, jobs))
    for (event, family, _reader, key), (result, error) in zip(jobs, results):
        completed = utc_datetime((providers.get("clock") or clock)())
        try:
            if error:
                raise error
            if not isinstance(result, dict):
                raise ValueError("OFFICIAL_ADAPTER_RESULT_INVALID")
            if family == "qualitative":
                observed = utc_datetime(result["qualitative_available_at"])
                if result.get("qualitative_status") not in {"OFFICIAL_TEXT_AVAILABLE", "OFFICIAL_RECORDING_AVAILABLE"} or not utc_datetime(event["event_time"]) <= observed <= completed:
                    raise ValueError("OFFICIAL_TEXT_TIMESTAMP_INVALID")
                event.update(result, qualitative_error=None)
            else:
                observed = utc_datetime(result["actual_available_at"])
                if (result.get("actual") in (None, "") or result.get("actual_status") != "VERIFIED"
                        or not utc_datetime(result["actual_published_at"]) <= observed <= completed):
                    raise ValueError("OFFICIAL_ACTUAL_RESULT_INVALID")
                event.update(result, actual_display=result["actual"], actual_error=None, known_at=iso_utc(completed))
            _record_actual_attempt(attempts, key, completed, "VERIFIED", increment=True)
        except Exception as exc:
            reason = str(exc)[:180] if isinstance(exc, ValueError) else f"OFFICIAL_FETCH_FAILED:{type(exc).__name__}"
            status = "PERIOD_MISMATCH" if any(word in reason for word in ("PERIOD", "EVENT_MISMATCH")) else "SOURCE_FETCH_FAILED"
            if family == "qualitative":
                event.update(qualitative_status="OFFICIAL_TEXT_PENDING", qualitative_error=reason)
            else:
                _mark_actual_error(event, status, reason)
            _record_actual_attempt(attempts, key, completed, status, reason, increment=True)

    sync_completed_at = utc_datetime((providers.get("clock") or clock)())
    store.set_scheduler_state("macro_actuals.bls_snapshot", snapshot, updated_at=sync_completed_at.isoformat())
    store.set_scheduler_state("macro_actuals.attempts", attempts, updated_at=sync_completed_at.isoformat())
    return events


def _parse_aware(value):
    try:
        return utc_datetime(value)
    except (TypeError, ValueError):
        return None


def _official_actual_url(event):
    parsed = urlparse(str(event.get("actual_source_url", "")))
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if event.get("actual_provider") == "BLS" and map_macro_event(event):
        return host == "www.bls.gov" and re_full_bls_archive(path)
    if event.get("actual_provider") == "Eurostat":
        return host == "ec.europa.eu" and path.startswith("/eurostat/en/web/products-euro-indicators/w/2-") and path.endswith("-ap")
    adapter = _external_adapter(event)
    if adapter:
        family, _metric, _reader = adapter
        if family == "release":
            from .macro_release_sources import official_release_url
            return official_release_url(event, event.get("actual_source_url"))
        from .macro_international_sources import official_international_url
        return official_international_url(event, event.get("actual_source_url"))
    return False


def re_full_bls_archive(path):
    import re
    return bool(re.fullmatch(r"/news\.release/archives/empsit_\d{8}\.htm", path))


def official_macro_news(store, *, now=None):
    """Read recent, verified official actuals without fetching or changing state."""
    as_of = utc_datetime(now or datetime.now(timezone.utc))
    cutoff = as_of - timedelta(hours=48)
    eligible = []
    for event in store.v2_records("macro_events", limit=500):
        if not isinstance(event, dict) or event.get("origin") != "public_calendar_schedule":
            continue
        if event.get("actual_status") != "VERIFIED" or event.get("actual") in (None, ""):
            continue
        if not _official_actual_url(event) or _mapped_official_metric(event) is None:
            continue
        published_at = _parse_aware(event.get("actual_published_at") or event.get("event_time"))
        available_at = _parse_aware(event.get("actual_available_at"))
        known_at = _parse_aware(event.get("known_at"))
        if not published_at or not available_at or not known_at:
            continue
        if not (cutoff <= published_at <= as_of and cutoff <= available_at <= as_of and known_at <= as_of):
            continue
        if available_at < published_at:
            continue
        reference_period = event.get("actual_reference_period")
        if not reference_period or not event.get("actual_unit"):
            continue
        if known_at < available_at:
            continue
        eligible.append((published_at, available_at, known_at, event))
    eligible.sort(key=lambda row: (row[0], row[1], row[2], str(row[3].get("event_id", ""))), reverse=True)
    selected = eligible[:4]
    if not selected:
        return []
    releases = []
    for published_at, available_at, known_at, event in selected:
        releases.append({
            "event_id": event.get("event_id"),
            "currency": event.get("currency"),
            "title": event.get("title"),
            "reference_period": event.get("actual_reference_period"),
            "actual": event.get("actual"),
            "actual_value": event.get("actual_value"),
            "forecast": event.get("forecast"),
            "previous": event.get("previous"),
            "unit": event.get("actual_unit"),
            "basis": event.get("actual_basis"),
            "provider": event.get("actual_provider"),
            "source_url": event.get("actual_source_url"),
            "schedule_source_url": event.get("source_url"),
            "published_at": iso_utc(published_at),
            "published_at_source": event.get("actual_published_at_source"),
            "available_at": iso_utc(available_at),
            "actual_available_at": iso_utc(available_at),
            "known_at": iso_utc(known_at),
            "actual_method": event.get("actual_method"),
            "actual_is_estimate": bool(event.get("actual_is_estimate")),
            "actual_status": "VERIFIED",
        })
    revision_hash = hashlib.sha256(json.dumps(
        [(item["event_id"], item["reference_period"], item["actual"], item["available_at"]) for item in releases],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:24]
    latest_published = max(row[0] for row in selected)
    latest_known = max(row[2] for row in selected)
    return [{
        "revision_id": f"official_macro_{revision_hash}",
        "scope": "MARKET_WIDE", "symbol": None, "symbols": [],
        "title": "官方宏观实际数据",
        "summary": "以下仅为统计机构已发布数值及日历预期对照，不包含方向判断。",
        "url": releases[0]["source_url"], "source": " / ".join(sorted({item["provider"] for item in releases})),
        "impact": "UNKNOWN", "published_at": iso_utc(latest_published),
        "known_at": iso_utc(latest_known), "macro_releases": releases,
    }]


def refresh_calendar(store, *, fetch=None, now=None, actual_provider=None, clock=None):
    """Explicit/background write; GET handlers only read this cache.

    Fixed public host, bounded payload, timeout, cooldown and no redirect to
    configurable hosts. Failures retain last successful data and its timestamp.
    """
    now = now or datetime.now(timezone.utc)
    clock = clock or (lambda: datetime.now(timezone.utc))
    should_sync_actuals = actual_provider is not None or fetch is None
    with _lock:
        prior = calendar_status(store)
        # A newly installed source registry enriches existing schedules once,
        # even if the old process left its schedule cooldown in durable state.
        cached = [item for item in store.v2_records("macro_events", limit=500)
                  if isinstance(item, dict) and item.get("origin") == "public_calendar_schedule"]
        if should_sync_actuals and cached and prior.get("actual_adapter_version") != ACTUAL_ADAPTER_VERSION:
            events = _sync_actuals(store, cached, cached, now, dict(actual_provider or {}), clock,
                                   schedule_refreshed=False)
            completed = utc_datetime(clock())
            with store._connect() as db:
                for event in events:
                    db.execute("UPDATE macro_events SET payload_json=?,updated_at=? WHERE event_id=?",
                               (json.dumps(event, ensure_ascii=False), iso_utc(completed), event["event_id"]))
            prior.update(actual_adapter_version=ACTUAL_ADAPTER_VERSION, actual_last_sync_at=iso_utc(completed))
            store.set_scheduler_state("macro_calendar.status", prior, updated_at=iso_utc(completed))
        try:
            last = datetime.fromisoformat(prior.get("last_attempt_at", ""))
            if timedelta(0) <= now - last < timedelta(minutes=15):
                result = calendar_status(store)
                result.update(refresh_deferred=True, retry_after_seconds=max(1, int((last + timedelta(minutes=15) - now).total_seconds())))
                return result
        except (ValueError, TypeError):
            pass
        state = {**prior, "last_attempt_at": now.isoformat(), "refresh_deferred": False}
        schedule_loaded = False
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
            schedule_completed_at = utc_datetime(clock()) if fetch is None or actual_provider is not None else now
            events = normalize_calendar(rows, schedule_completed_at)
            if not events:
                raise ValueError("数据源未返回当前时段可用的中高影响事件")
            schedule_loaded = True
            # Unit tests that inject a schedule fixture remain offline unless
            # they explicitly inject actual-source readers as well.
            if should_sync_actuals:
                prior_events = store.v2_records("macro_events", limit=500)
                events = _sync_actuals(store, events, prior_events, now,
                                       dict(actual_provider or {}), clock)
            write_timestamp = utc_datetime(clock()) if fetch is None or actual_provider is not None else now
            with store._connect() as db:
                # Replace only our own cache, never manual imports or user evidence.
                db.execute("DELETE FROM macro_events WHERE json_extract(payload_json, '$.origin')='public_calendar_schedule'")
                for event in events:
                    db.execute("INSERT INTO macro_events VALUES(?,?,?) ON CONFLICT(event_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                               (event["event_id"], json.dumps(event, ensure_ascii=False), write_timestamp.isoformat()))
            state.update(status="SCHEDULE_ONLY", last_success_at=schedule_completed_at.isoformat(), error=None, event_count=len(events))
            if should_sync_actuals:
                state["actual_last_sync_at"] = write_timestamp.isoformat()
                state["actual_adapter_version"] = ACTUAL_ADAPTER_VERSION
                state["actual_sync_error"] = None
        except Exception as exc:
            # If the schedule source is unavailable, already cached events can
            # still qualify a newly published official actual. Their schedule
            # timestamps and last-success marker remain untouched.
            cached = [item for item in store.v2_records("macro_events", limit=500)
                      if isinstance(item, dict) and item.get("origin") == "public_calendar_schedule"]
            if not schedule_loaded and should_sync_actuals and cached:
                try:
                    cached_events = _sync_actuals(
                        store, cached, cached, now, dict(actual_provider or {}), clock,
                        schedule_refreshed=False,
                    )
                    write_timestamp = utc_datetime(clock())
                    with store._connect() as db:
                        for event in cached_events:
                            db.execute(
                                "UPDATE macro_events SET payload_json=?,updated_at=? WHERE event_id=?",
                                (json.dumps(event, ensure_ascii=False), write_timestamp.isoformat(), event["event_id"]),
                            )
                    state.update(status="SCHEDULE_FETCH_FAILED",
                                 error=f"公开日历更新失败（{type(exc).__name__}），保留上次日程缓存。")
                    state["actual_last_sync_at"] = write_timestamp.isoformat()
                    state["actual_adapter_version"] = ACTUAL_ADAPTER_VERSION
                    state["actual_sync_error"] = next(
                        (event.get("actual_error") for event in cached_events if event.get("actual_error")), None)
                    state["event_count"] = len(cached_events)
                except Exception as actual_exc:
                    state.update(status="SCHEDULE_FETCH_FAILED",
                                 error=f"公开日历更新失败（{type(exc).__name__}），实际值同步失败（{type(actual_exc).__name__}）；保留上次缓存。")
            else:
                # No raw network exception/URL credentials in user-visible status.
                state.update(status="UNAVAILABLE", error=f"公开日历更新失败（{type(exc).__name__}），保留上次缓存；请检查网络后重试。")
        store.set_scheduler_state("macro_calendar.status", state, updated_at=now.isoformat())
        return calendar_status(store)
