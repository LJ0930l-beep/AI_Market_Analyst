from datetime import datetime, timedelta, timezone

import pytest

from core.instruments import instrument_for
from core.providers.base import Bar, Quote
from core.providers.gateio_provider import GatePublicProvider


class _GateCandleResponse(GatePublicProvider):
    def __init__(self, rows):
        super().__init__(testnet=True, retries=0)
        self.rows = rows
        self.requested_path = None
        self.requested_params = None

    def _request(self, path, params):
        self.requested_path = path
        self.requested_params = params
        return self.rows


def test_gate_testnet_provider_reads_closed_and_forming_4h_bars():
    now = datetime.now(timezone.utc)
    four_hours = 4 * 60 * 60
    current_bar_start = int(now.timestamp()) // four_hours * four_hours
    provider = _GateCandleResponse(
        [
            {
                "t": str(current_bar_start - four_hours),
                "o": "100",
                "h": "105",
                "l": "98",
                "c": "103",
                "v": "12",
            },
            {
                "t": str(current_bar_start),
                "o": "103",
                "h": "106",
                "l": "102",
                "c": "104",
                "v": "7",
            },
        ]
    )

    bars = provider.get_bars(instrument_for("BTCUSDT"), "4h", limit=2)

    assert provider.requested_path == "/futures/usdt/candlesticks"
    assert provider.requested_params == {
        "contract": "BTC_USDT",
        "interval": "4h",
        "limit": 2,
    }
    assert len(bars) == 2
    assert bars[0].bar_end == datetime.fromtimestamp(current_bar_start, timezone.utc)
    assert bars[0].is_closed is True
    assert bars[1].bar_end == datetime.fromtimestamp(current_bar_start + four_hours, timezone.utc)
    assert bars[1].is_closed is False
    assert all(bar.volume_unit == "contracts" for bar in bars)
    assert all(bar.source == "gate_native_rest:last" for bar in bars)


def test_gate_provider_still_rejects_unsupported_timeframes_before_request():
    provider = _GateCandleResponse([])

    with pytest.raises(ValueError, match="unsupported timeframe"):
        provider.get_bars(instrument_for("BTCUSDT"), "2h", limit=2)

    assert provider.requested_path is None


def test_gate_bootstrap_includes_1h_and_4h_background_by_default():
    provider = GatePublicProvider(testnet=True, retries=0)
    instrument = instrument_for("BTCUSDT")
    now = datetime.now(timezone.utc)
    seconds = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    calls = []

    provider.market = lambda _symbol: {"id": "BTC_USDT"}
    provider.get_quote = lambda current_instrument: Quote(current_instrument, now, 100.0)

    def native_bars(symbol, timeframe, limit, *, price_type="last"):
        calls.append((symbol, timeframe, limit, price_type))
        bar_end = now.replace(minute=0, second=0, microsecond=0)
        bar_end -= timedelta(seconds=seconds[timeframe] * 2)
        return [
            Bar(
                bar_end - timedelta(seconds=seconds[timeframe]),
                99.0,
                101.0,
                98.0,
                100.0,
                10.0,
                bar_end=bar_end,
                is_closed=True,
            )
        ]

    provider._native_bars = native_bars
    provider.funding_context = lambda _symbol: {}
    provider.order_book = lambda _symbol, limit=20: {"bids": [], "asks": []}
    provider.trades = lambda _symbol, limit=100: []
    provider.liquidations = lambda _symbol, limit=100: []

    result = provider.bootstrap_symbol("BTCUSDT", instrument=instrument, min_15m=1)

    assert set(result["bars"]) == {"5m", "15m", "1h", "4h", "1d"}
    assert ("BTCUSDT", "4h", 120, "last") in calls


def test_gate_native_bars_backfills_older_rows_with_testnet_range_query():
    now = datetime.now(timezone.utc)
    interval = 15 * 60
    base = int((now - timedelta(hours=12)).timestamp()) // interval * interval

    def row(timestamp):
        return {
            "t": str(timestamp),
            "o": "100",
            "h": "102",
            "l": "99",
            "c": "101",
            "v": "4",
        }

    class PaginatedGateProvider(GatePublicProvider):
        def __init__(self):
            super().__init__(testnet=True, retries=0)
            self.requests = []

        def _request(self, path, params):
            self.requests.append((path, dict(params)))
            if "limit" in params:
                return [row(base + 2 * interval), row(base + 3 * interval)]
            # Gate range queries may include an overlap at the upper bound;
            # the provider must keep only candles older than the first page.
            return [row(base), row(base + interval), row(base + 2 * interval)]

    provider = PaginatedGateProvider()

    bars = provider._native_bars("BTCUSDT", "15m", 4)

    assert len(bars) == 4
    assert [int(bar.timestamp.timestamp()) for bar in bars] == [
        base,
        base + interval,
        base + 2 * interval,
        base + 3 * interval,
    ]
    assert all(bar.source == "gate_native_rest:last" for bar in bars)
    assert provider.requests[0][1] == {
        "contract": "BTC_USDT",
        "interval": "15m",
        "limit": 4,
    }
    assert provider.requests[1][1] == {
        "contract": "BTC_USDT",
        "interval": "15m",
        "from": base - 2 * interval,
        "to": base + 2 * interval - 1,
    }
