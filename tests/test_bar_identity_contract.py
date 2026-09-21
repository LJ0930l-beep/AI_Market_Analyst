"""Regression contract for the single-bar-identity rule on the trading path.

Gate persists one 15m bar under several identities (traded price ``last``, mark
price ``mark``, index price ``index``) plus a legacy mirror.  A trading-path
read that omits the identity filters receives all of them, which makes one
timestamp appear two or three times.

That is not cosmetic: ``BaseStrategy.evaluate`` refuses a series with repeated
timestamps, so an unfiltered read left every strategy permanently
``WARMING_UP`` and no candidate was ever produced -- the AI cycle then had
nothing to open.  These tests pin both halves of the fix.
"""

from datetime import datetime, timedelta, timezone

from core.instruments import read_trading_bars, trading_bar_filters
from core.providers.base import Bar
from core.quant.strategies import EMATrend
from core.storage import SQLiteStore


def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "identity.sqlite3")
    store.initialize()
    return store


def _bar(timestamp: datetime) -> dict:
    return {
        "timestamp": timestamp,
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "close": 101.0,
        "volume": 10.0,
        "is_closed": True,
    }


def test_trading_bar_reads_drop_mark_and_legacy_identities(tmp_path):
    """One symbol, one bar_start, three identities: only ``last`` may survive."""

    store = _store(tmp_path)
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    rows = [_bar(now - timedelta(minutes=15 * (index + 1))) for index in range(120)]

    store.upsert_market_bars(
        "BTCUSDT", "15m", rows, data_as_of=now, now=now,
        provider="gate", venue="gate", market_type="perpetual",
        native_symbol="BTC_USDT", settle_currency="USDT", price_type="last",
    )
    # The same timestamps again, under the mark-price identity.
    store.upsert_market_bars(
        "BTCUSDT", "15m", rows, data_as_of=now, now=now,
        provider="gate", venue="gate", market_type="perpetual",
        native_symbol="BTC_USDT", settle_currency="USDT", price_type="mark",
    )
    # And once more through the legacy mirror, which has no identity at all.
    store.upsert_market_bars("BTCUSDT", "15m", rows, provider="gate-public", data_as_of=now, now=now)

    unfiltered = store.list_market_bars("BTCUSDT", "15m", limit=400)
    unfiltered_timestamps = [row["bar_start"] for row in unfiltered]
    assert len(unfiltered_timestamps) != len(set(unfiltered_timestamps)), (
        "fixture must reproduce the duplicate-timestamp condition"
    )

    filtered = read_trading_bars(store, "BTCUSDT", "15m", limit=400)
    assert filtered, "traded-price series must not disappear"
    assert all(row["price_type"] == "last" for row in filtered)
    assert all(row["venue"] == "gate" and row["market_type"] == "perpetual" for row in filtered)
    timestamps = [row["bar_start"] for row in filtered]
    assert len(timestamps) == len(set(timestamps))
    assert len(timestamps) == 120


def test_trading_bar_filters_are_the_documented_identity():
    assert trading_bar_filters() == {
        "venue": "gate",
        "market_type": "perpetual",
        "price_type": "last",
    }


def test_read_trading_bars_tolerates_readers_without_identity_filters():
    """Minimal read-only doubles keep working, but only through this helper."""

    class PositionalOnly:
        def __init__(self):
            self.calls = []

        def list_market_bars(self, symbol, timeframe, *, limit):
            self.calls.append((symbol, timeframe, limit))
            return [{"bar_start": "2026-09-15T11:45:00+00:00"}]

    reader = PositionalOnly()
    assert read_trading_bars(reader, "BTCUSDT", "15m", limit=7) == [
        {"bar_start": "2026-09-15T11:45:00+00:00"}
    ]
    assert reader.calls == [("BTCUSDT", "15m", 7)]


def test_legacy_only_store_is_still_visible_to_the_trading_path(tmp_path):
    """Paper and simulated accounts have no Gate identity and must not vanish.

    ``upsert_market_bars`` without explicit identity arguments always stores the
    legacy identity, no matter which provider is named.  A hard Gate
    requirement therefore made every paper account and every fixture invisible,
    which silently blocked calibration and the candidate scan before the model
    was ever called.
    """

    store = _store(tmp_path)
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    rows = [_bar(now - timedelta(minutes=15 * (index + 1))) for index in range(30)]
    store.upsert_market_bars("BTCUSDT", "15m", rows, provider="local-paper-fixture", data_as_of=now, now=now)

    assert store.list_market_bars("BTCUSDT", "15m", limit=100, **trading_bar_filters()) == []

    visible = read_trading_bars(store, "BTCUSDT", "15m", limit=100)
    assert len(visible) == 30
    timestamps = [row["bar_start"] for row in visible]
    assert len(timestamps) == len(set(timestamps))


def test_identity_collapse_still_refuses_to_interleave(tmp_path):
    """The legacy fallback must not become a back door for mixed identities."""

    store = _store(tmp_path)
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    rows = [_bar(now - timedelta(minutes=15 * (index + 1))) for index in range(30)]
    # A mark-price series plus the legacy mirror, and no Gate ``last`` series.
    store.upsert_market_bars(
        "BTCUSDT", "15m", rows, data_as_of=now, now=now,
        provider="gate", venue="gate", market_type="perpetual",
        native_symbol="BTC_USDT", settle_currency="USDT", price_type="mark",
    )
    store.upsert_market_bars("BTCUSDT", "15m", rows, provider="legacy-mirror", data_as_of=now, now=now)

    unfiltered = store.list_market_bars("BTCUSDT", "15m", limit=200)
    assert len({row["instrument_key"] for row in unfiltered}) == 2

    visible = read_trading_bars(store, "BTCUSDT", "15m", limit=200)
    assert len({str(row.get("instrument_key") or "") for row in visible}) == 1
    timestamps = [row["bar_start"] for row in visible]
    assert len(timestamps) == len(set(timestamps)) == 30


def test_duplicate_timestamps_are_not_reported_as_insufficient_warmup():
    """The old shared branch printed "(112 < 60)", which contradicts itself."""
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=15)
    bars = [
        Bar(now - step * (index + 1), 100.0, 102.0, 99.0, 101.0, 10.0, is_closed=True)
        for index in range(80)
    ]
    strategy = EMATrend()
    context = {"timeframe": "15m", "market_type": "crypto"}

    # 80 distinct bars already clears the 60-bar warmup, so only the duplicate
    # check can fire once the series is mixed.
    assert strategy.evaluate("BTCUSDT", bars, now=now, context=context) is not None or (
        strategy.last_status != "WARMING_UP"
    )

    strategy = EMATrend()
    assert strategy.evaluate("BTCUSDT", bars + bars, now=now, context=context) is None
    assert strategy.last_status == "WARMING_UP"
    assert "Duplicate bar timestamps" in strategy.last_reason
    assert "Insufficient warmup bars" not in strategy.last_reason
