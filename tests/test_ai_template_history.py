from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3
import json
import os

import pytest

from core.replay.ai_history import (ReplayHistory, SCHEMA_VERSION, archived_news,
                                    manifest_hash, normalize_bar, normalize_contract,
                                    _cached_public_json, freeze_public_history)

NOW = datetime(2026, 10, 2, 0, 15, tzinfo=timezone.utc)


def contract():
    return normalize_contract("ETHUSDT", {"quanto_multiplier": "0.01", "order_price_round": "0.01",
        "order_size_min": "0.1", "order_size_max": "10000", "leverage_max": "200", "enable_decimal": True})


def bar(start=NOW-timedelta(minutes=5), close=101):
    return normalize_bar("ETHUSDT", "5m", {"t": int(start.timestamp()), "o": "100", "h": "102",
        "l": "99", "c": str(close), "v": "12.5"}, NOW+timedelta(days=1), contract())


def history(rows, news=None):
    data = {"schema_version": SCHEMA_VERSION, "symbols": ["ETHUSDT"], "window_start": NOW.isoformat(),
            "window_end": (NOW+timedelta(minutes=15)).isoformat(), "decision_points": [NOW.isoformat()],
            "bars": rows, "news": news or [], "contracts": {"ETHUSDT": contract()}}
    data["manifest_sha256"] = manifest_hash(data)
    return ReplayHistory(data)


def test_decimal_contract_sizes_not_truncated_to_zero():
    result = contract()
    assert result["min_size"] == result["amount_step"] == 0.1
    assert result["market"]["precision"]["amount"] == 0.1


def test_native_bar_units_identity_and_honest_availability():
    result = bar()
    assert result["base_volume"] == 0.125
    assert result["available_at"] == NOW.isoformat()
    assert result["fetched_at"] != result["available_at"]
    assert result["availability_basis"] == "HISTORICAL_BAR_CLOSE_ASSUMPTION"
    from core.trading.price_action_structure import has_verified_gate_bar_identity
    assert has_verified_gate_bar_identity(result)


def test_future_bar_and_late_revision_are_not_visible():
    early = bar()
    late = deepcopy(early)
    late.update(close=102, available_at=(NOW+timedelta(minutes=1)).isoformat(), revision_id="late")
    future = bar(NOW)
    reader = history([early, late, future])
    assert reader.latest_bars("ETHUSDT", "5m")[0]["close"] == 101
    assert len(reader.latest_bars("ETHUSDT", "5m")) == 1
    reader.set_as_of(NOW+timedelta(minutes=1))
    assert reader.latest_bars("ETHUSDT", "5m")[0]["close"] == 102


def test_revision_choice_happens_before_limit():
    rows = [bar(NOW-timedelta(minutes=5*i)) for i in range(1, 40)]
    for row in rows:
        row["available_at"] = NOW.isoformat()
    later = deepcopy(rows[-1])
    later["available_at"] = (NOW+timedelta(days=2)).isoformat()
    reader = history(rows+[later])
    assert len(reader.latest_bars("ETHUSDT", "5m", limit=32)) == 32


def test_manifest_tamper_and_synthetic_fail():
    reader = history([bar()])
    tampered = deepcopy(reader.payload)
    tampered["bars"][0]["close"] = 500
    with pytest.raises(ValueError, match="INTEGRITY"):
        ReplayHistory(tampered)
    tampered["bars"][0]["synthetic"] = True
    tampered["manifest_sha256"] = manifest_hash(tampered)
    with pytest.raises(ValueError, match="SYNTHETIC"):
        ReplayHistory(tampered)


def test_news_uses_knowledge_time_not_only_publication():
    item = {"revision_id": "n1", "published_at": (NOW-timedelta(hours=1)).isoformat(),
            "known_at": (NOW+timedelta(minutes=1)).isoformat(), "affected_symbols": ["ETHUSDT"]}
    reader = history([bar()], [item])
    assert reader.news_as_of(NOW, ["ETHUSDT"]) == []
    assert len(reader.news_as_of(NOW+timedelta(minutes=1), ["ETHUSDT"])) == 1
    assert reader.news_as_of(NOW+timedelta(minutes=1), ["BTCUSDT"]) == []


def test_archived_news_database_is_read_only_and_uses_revision_time(tmp_path):
    path = tmp_path / "source.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE phase6_events(event_id,title,summary,published_at,known_at,revision_known_at,"
            "affected_symbols_json,source,source_type,category,importance,url,credibility_score,primary_source)")
        db.execute("INSERT INTO phase6_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "n1", "headline", "summary", NOW.isoformat(), NOW.isoformat(), (NOW+timedelta(hours=1)).isoformat(),
            '["ETHUSDT"]', "source", "news", "crypto", 2, "https://example.org/a?api_key=hidden", 80, 1))
    before = path.read_bytes()
    assert archived_news(path, NOW, NOW+timedelta(minutes=1)) == []
    news = archived_news(path, NOW, NOW+timedelta(hours=2))
    assert news[0]["url"] == "https://example.org/a"
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("o", "0"), ("h", "98"), ("v", "-1"), ("c", "nan")])
def test_invalid_public_prices_fail(field, value):
    row = {"t": int(NOW.timestamp()), "o": "100", "h": "102", "l": "99", "c": "101", "v": "1"}
    row[field] = value
    with pytest.raises(ValueError, match="BAR_INVALID"):
        normalize_bar("ETHUSDT", "5m", row, NOW, contract())


def test_history_window_requires_hour_alignment_before_any_fetch(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid window must not perform network calls")
    with pytest.raises(ValueError, match="WINDOW_INVALID"):
        freeze_public_history(tmp_path, NOW, NOW + timedelta(hours=1), fetch=forbidden)


def test_cache_retains_fetch_time_and_refreshes_stale_rules(tmp_path):
    path = tmp_path / "cached.json"
    calls = []
    def fetch(endpoint):
        calls.append(endpoint)
        return {"value": 2}
    first, observed, basis = _cached_public_json(path, "/contracts/ETH_USDT", None, fetch)
    second, retained, basis2 = _cached_public_json(path, "/contracts/ETH_USDT", None, fetch)
    assert first == second == {"value": 2}
    assert observed == retained and basis == basis2 == "RECORDED_PUBLIC_FETCH_TIME"
    assert len(calls) == 1
    cached = json.loads(path.read_text())
    cached["fetched_at"] = NOW.isoformat()
    path.write_text(json.dumps(cached))
    _cached_public_json(path, "/contracts/ETH_USDT", None, fetch, max_age_seconds=900)
    assert len(calls) == 2


def test_legacy_cache_exposes_mtime_instead_of_inventing_new_fetch(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text('[{"value":1}]')
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    def forbidden(*args):
        raise AssertionError("immutable historical cache does not refetch")
    rows, observed, basis = _cached_public_json(path, "/candlesticks", {}, forbidden)
    assert rows == [{"value": 1}] and observed == NOW
    assert basis == "LEGACY_CACHE_FILE_MTIME_PROXY"
