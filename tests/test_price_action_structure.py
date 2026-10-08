from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from core.trading.autonomous_strategy import compact_technical, technical_context
from core.trading.price_action_structure import build_price_action_structure
from core.storage import SQLiteStore


def _native_bars(now: datetime, timeframe: str = "15m", count: int = 40) -> list[dict]:
    minutes = {"15m": 15, "1h": 60}[timeframe]
    cadence = timedelta(minutes=minutes)
    latest = now.replace(second=0, microsecond=0)
    latest -= timedelta(minutes=latest.minute % minutes)
    rows = []
    for index in range(count):
        end = latest - cadence * (count - index - 1)
        rows.append({
            "bar_start": (end - cadence).isoformat(),
            "bar_end": end.isoformat(),
            "available_at": end.isoformat(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 10.0,
            "is_closed": True,
            "synthetic": False,
            "quality_status": "VALID",
            "venue": "gate",
            "market_type": "perpetual",
            "price_type": "last",
            "provider": "gate",
            "source": "gate_native_rest:last",
        })
    return rows


def _structure_fixture(now: datetime) -> list[dict]:
    rows = _native_bars(now, "15m", 40)
    rows[10]["high"] = 110.0  # confirmed at bar 12, then broken by a close
    rows[15].update(open=100.0, high=112.0, low=100.0, close=111.0)
    rows[16].update(open=111.0, high=112.0, low=110.0, close=111.0)
    rows[17].update(open=111.0, high=112.0, low=108.0, close=109.0)
    rows[20].update(open=100.0, high=101.0, low=90.0, close=100.0)
    rows[24].update(open=100.0, high=101.0, low=88.0, close=95.0)
    rows[-1].update(open=100.0, high=500.0, low=0.5, close=100.0)
    return rows


def _mirror_rows(rows: list[dict], midpoint: float = 200.0) -> list[dict]:
    mirrored = deepcopy(rows)
    for row in mirrored:
        opening, high, low, close = (row[key] for key in ("open", "high", "low", "close"))
        row.update(open=midpoint - opening, high=midpoint - low,
                   low=midpoint - high, close=midpoint - close)
    return mirrored


def _summary(rows: list[dict]) -> dict:
    return build_price_action_structure(rows, "15m", rows[-1]["bar_end"])


def test_price_action_structure_is_symmetric_and_uses_only_confirmed_closed_evidence():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    rows = _structure_fixture(now)
    long_summary = _summary(rows)
    short_summary = _summary(_mirror_rows(rows))

    assert long_summary["status"] == short_summary["status"] == "READY"
    assert long_summary["bos"]["side"] == "LONG"
    assert short_summary["bos"]["side"] == "SHORT"
    assert long_summary["breakout_retest"]["side"] == "LONG"
    assert short_summary["breakout_retest"]["side"] == "SHORT"
    assert long_summary["sweep_reclaim"]["side"] == "LONG"
    assert short_summary["sweep_reclaim"]["side"] == "SHORT"
    assert long_summary["bos"]["state"] == "INVALIDATED"
    assert long_summary["breakout_retest"]["state"] == "INVALIDATED"
    assert long_summary["bos"]["pivot_confirmed_at"] < long_summary["bos"]["confirmed_at"]
    assert long_summary["breakout_retest"]["bos_at"] < long_summary["breakout_retest"]["confirmed_at"]
    assert long_summary["as_of"] == rows[-1]["bar_end"].replace("+00:00", "Z")
    assert long_summary["source"] == "gate_native_rest:last"
    assert long_summary["closed_bar_count"] == 40
    assert long_summary["prior_range"]["high"] < rows[-1]["high"]
    assert long_summary["prior_range"]["low"] > rows[-1]["low"]
    assert all(
        event_time <= long_summary["as_of"]
        for key in ("bos", "sweep_reclaim", "breakout_retest")
        for event_time in (
            [long_summary[key]["confirmed_at"]]
            + ([long_summary[key]["pivot_confirmed_at"]]
               if "pivot_confirmed_at" in long_summary[key] else [])
            + ([long_summary[key]["bos_at"]]
               if "bos_at" in long_summary[key] else [])
        )
    )
    for summary in (long_summary, short_summary):
        assert len(json.dumps(summary, separators=(",", ":"))) <= 1_400
        assert summary["as_of"] >= summary["last_closed_at"]
        assert summary["bos"]["state"] == "INVALIDATED"
        assert summary["breakout_retest"]["state"] == "INVALIDATED"
        assert summary["bos"]["pivot_confirmed_at"]
        assert summary["bos"]["bar_at"]


@pytest.mark.parametrize(
    "mutation, expected_reason",
    [
        (lambda rows: rows.pop(4), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows.pop(10), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows[10].update(bar_start=rows[10]["bar_end"]), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows[10].update(is_closed="false"), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows[10].update(synthetic=True), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows[10].update(provider="untrusted"), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows[10].update(market_type="spot"), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows[10].update(price_type="mark"), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows[10].update(payload_json=json.dumps({"provider": "binance", "source": "gate_native_rest:last"})), "NONCONTIGUOUS_CLOSED_BARS"),
        (lambda rows: rows[10].update(high=1.0), "NONCONTIGUOUS_CLOSED_BARS"),
    ],
)
def test_price_action_summary_fails_closed_on_incomplete_or_invalid_history(mutation, expected_reason):
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    rows = _native_bars(now, "15m", 40)
    mutation(rows)

    result = _summary(rows)

    assert result["status"] == "UNAVAILABLE"
    assert result["reason"] == expected_reason
    assert "bos" not in result and "sweep_reclaim" not in result


def test_price_action_summary_requires_at_least_32_valid_bars():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    assert _summary(_native_bars(now, "15m", 31))["reason"] == "INSUFFICIENT_VALID_CLOSED_BARS"


def test_price_action_summary_rejects_future_bars_and_unverified_source():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    future_rows = _native_bars(now, "15m", 40)
    future_end = datetime.fromisoformat(future_rows[-1]["bar_end"]) + timedelta(minutes=15)
    future_rows.append({**future_rows[-1], "bar_start": future_rows[-1]["bar_end"],
                        "bar_end": future_end.isoformat(), "available_at": future_end.isoformat()})
    assert build_price_action_structure(future_rows, "15m", _native_bars(now, "15m", 40)[-1]["bar_end"])["reason"] == "FUTURE_BAR"

    unverified = _native_bars(now, "15m", 40)
    for row in unverified:
        row["source"] = "fixture"
    assert _summary(unverified)["reason"] == "INSUFFICIENT_VALID_CLOSED_BARS"


def test_reconstructed_late_bars_are_not_backdated_as_known_for_bos():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    rows = _structure_fixture(now)
    bos_bar_end = datetime.fromisoformat(rows[15]["bar_end"])
    late_pivot_available = bos_bar_end + timedelta(minutes=1)
    rows[12]["available_at"] = late_pivot_available.isoformat()
    rows[12]["quality_status"] = "RECONSTRUCTED_LATE"
    point = datetime.fromisoformat(rows[-1]["bar_end"]) + timedelta(minutes=2)

    late_pivot_result = build_price_action_structure(rows, "15m", point)
    assert late_pivot_result["status"] == "READY"
    assert late_pivot_result["bos"] is None

    # If the break candle itself only becomes available after the pivot's
    # delayed source, the event is knowable then and records that actual time.
    rows[15]["available_at"] = (bos_bar_end + timedelta(minutes=2)).isoformat()
    rows[15]["quality_status"] = "RECONSTRUCTED_LATE"
    late_break_result = build_price_action_structure(rows, "15m", point)
    assert late_break_result["as_of"] == point.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert late_break_result["last_closed_at"] == rows[-1]["bar_end"].replace("+00:00", "Z")
    assert late_break_result["bos"]["bar_at"] == rows[15]["bar_end"].replace("+00:00", "Z")
    assert late_break_result["bos"]["pivot_confirmed_at"] == late_pivot_available.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert late_break_result["bos"]["confirmed_at"] == (bos_bar_end + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_price_action_summary_is_opt_in_and_survives_compact_technical_projection():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    signal = _structure_fixture(now)
    context = _native_bars(now, "1h", 40)
    store = SimpleNamespace(
        latest_bars=lambda _symbol, timeframe, *, limit, **_filters: {
            "15m": signal, "1h": context,
        }[timeframe][-limit:]
    )

    without_pa = technical_context(store, ("BTCUSDT",), now, timeframes=("15m", "1h"))
    with_pa = technical_context(
        store, ("BTCUSDT",), now, timeframes=("15m", "1h"), include_price_action=True,
    )
    assert "price_action" not in without_pa["BTCUSDT"]["timeframes"]["15m"]
    for timeframe in ("15m", "1h"):
        assert with_pa["BTCUSDT"]["timeframes"][timeframe]["price_action"]["status"] == "READY"

    compact = compact_technical(with_pa, signal_timeframe="15m")
    assert compact["BTCUSDT"]["timeframes"]["15m"]["price_action"] == with_pa["BTCUSDT"]["timeframes"]["15m"]["price_action"]
    assert compact["BTCUSDT"]["timeframes"]["1h"]["price_action"] == with_pa["BTCUSDT"]["timeframes"]["1h"]["price_action"]


def test_price_action_accepts_real_versioned_sqlite_row_shape_without_top_level_provider(tmp_path):
    """Exercise the production market_bar_versions row contract, not a row double."""
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "price-action-market-bars.sqlite3")
    store.initialize()
    source_rows = _native_bars(now, "15m", 40)
    written = store.upsert_market_bars(
        "BTCUSDT", "15m", source_rows,
        provider="gate_public_swap", data_as_of=now, now=now,
        venue="gate", market_type="perpetual", native_symbol="BTC_USDT",
        settle_currency="USDT", price_type="last",
    )
    assert written == 40

    rows = store.latest_bars("BTCUSDT", "15m", limit=240, venue="gate",
                             market_type="perpetual", price_type="last")
    assert len(rows) == 40
    assert "provider" not in rows[-1]
    assert rows[-1]["instrument_key"] == "gate:perpetual:BTC_USDT:USDT:last"
    assert len(rows[-1]["raw_hash"]) == 64
    assert json.loads(rows[-1]["payload_json"])["provider"] == "gate_public_swap"
    summary = build_price_action_structure(rows, "15m", now)
    assert summary["status"] == "READY"
    assert summary["source"] == "gate_native_rest:last"
    assert summary["closed_bar_count"] == 40

    contextual = technical_context(
        store, ("BTCUSDT",), now, timeframes=("15m",), include_price_action=True,
        nofx_indicators={"enable_ema": True, "ema_periods": [20]},
    )["BTCUSDT"]["timeframes"]["15m"]
    assert contextual["nofx_indicator_snapshot"]["source"] == "gate_native_rest:last"
    assert contextual["price_action"]["status"] == "READY"

    bad_identity_rows = [dict(row) for row in rows]
    bad_identity_rows[10]["raw_hash"] = ""
    assert build_price_action_structure(bad_identity_rows, "15m", now)["reason"] == "NONCONTIGUOUS_CLOSED_BARS"


def test_price_action_rejects_synthetic_flag_stored_inside_sqlite_payload(tmp_path):
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "price-action-synthetic-payload.sqlite3")
    store.initialize()
    store.upsert_market_bars(
        "BTCUSDT", "15m", _native_bars(now, "15m", 40),
        provider="gate_public_swap", data_as_of=now, now=now,
        venue="gate", market_type="perpetual", native_symbol="BTC_USDT",
        settle_currency="USDT", price_type="last",
    )
    rows = store.latest_bars("BTCUSDT", "15m", limit=240, venue="gate",
                             market_type="perpetual", price_type="last")
    target = rows[10]
    payload = json.loads(target["payload_json"])
    payload["synthetic"] = True
    with store._connect() as db:
        db.execute(
            "UPDATE market_bar_versions SET payload_json=? WHERE instrument_key=? AND timeframe=? AND bar_start=?",
            (json.dumps(payload, sort_keys=True), target["instrument_key"], target["timeframe"], target["bar_start"]),
        )
    rows = store.latest_bars("BTCUSDT", "15m", limit=240, venue="gate",
                             market_type="perpetual", price_type="last")
    assert build_price_action_structure(rows, "15m", now)["reason"] == "NONCONTIGUOUS_CLOSED_BARS"
    frame = technical_context(
        store, ("BTCUSDT",), now, timeframes=("15m",), include_price_action=True,
    )["BTCUSDT"]["timeframes"]["15m"]
    assert frame["status"] == "INSUFFICIENT_OR_STALE"
    assert frame["price_action"]["status"] == "UNAVAILABLE"
