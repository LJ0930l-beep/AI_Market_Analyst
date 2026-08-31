from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.instruments import instrument_for
from core.providers import Bar
from core.realtime import BinanceRealtimeStream


class FakeSocket:
    def __init__(self) -> None:
        self.sent = False
        self.closed = False

    def recv(self):
        if not self.sent:
            self.sent = True
            return json.dumps({
                "stream": "btcusdt@kline_15m",
                "data": {"k": {"s": "BTCUSDT", "t": 1_893_916_800_000, "o": "100", "h": "101", "l": "99", "c": "100.5", "v": "12", "x": False}},
            })
        raise RuntimeError("simulated disconnect")

    def close(self):
        self.closed = True


class FakeRestProvider:
    provider_name = "binance_public_rest_test"

    def __init__(self) -> None:
        self.calls = 0

    def get_bars(self, _instrument, _timeframe, limit=200):
        self.calls += 1
        return [
            Bar(datetime(2030, 1, 2, 11, 45, tzinfo=timezone.utc), 99, 100, 98, 99.5, 10),
            Bar(datetime(2030, 1, 2, 12, tzinfo=timezone.utc), 99.5, 101, 99, 100.5, 12),
        ][-limit:]


def test_realtime_stream_reconnects_and_rest_backfills_after_disconnect() -> None:
    socket = FakeSocket()
    rest = FakeRestProvider()
    stream = BinanceRealtimeStream(
        symbols=["btcusdt"],
        websocket_factory=lambda _url, _timeout: socket,
        rest_provider=rest,
        sleep=lambda _seconds: None,
    )
    bars: list[tuple[str, bool]] = []

    def on_bar(symbol: str, _bar: Bar, closed: bool) -> None:
        bars.append((symbol, closed))
        if closed and len(bars) >= 3:
            stream.stop()

    stream.run_forever(on_bar=on_bar)
    assert rest.calls == 1
    assert bars[0] == ("BTCUSDT", False)
    assert bars[1:] == [("BTCUSDT", True), ("BTCUSDT", True)]
    assert stream.reconnect_count == 1
    assert socket.closed is True
    assert stream.state("stopped").to_dict()["public_only"] is True


def test_realtime_cache_limit_and_public_stream_url() -> None:
    stream = BinanceRealtimeStream(symbols=["BTCUSDT", "ethusdt"], timeframe="1h", max_symbols=50)
    assert "btcusdt@kline_1h" in stream.url
    assert "ethusdt@kline_1h" in stream.url
    assert stream.state().symbols == ("BTCUSDT", "ETHUSDT")
    assert instrument_for("SOLUSDT").asset_type.value == "crypto"
