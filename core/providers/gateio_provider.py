"""Read-only Gate USDT perpetual gateway. No credentials or order methods.

Symbols are resolved against CCXT market metadata, never by string replacement.
Construction is offline; all network calls are explicit and time bounded.
"""

from datetime import datetime, timezone
from threading import RLock

from .base import Bar, ProviderError, Quote


class GatePublicProvider:
    provider_name = "gate_public_swap"

    def __init__(self, exchange=None):
        if exchange is None:
            import ccxt

            exchange = ccxt.gate(
                {
                    "enableRateLimit": True,
                    "timeout": 10000,
                    "options": {
                        "defaultType": "swap",
                        "fetchMarkets": {"types": ["swap"]},
                    },
                }
            )
        self.exchange = exchange
        self._lock = RLock()

    def market(self, symbol):
        with self._lock:
            markets = self.exchange.load_markets()
        matches = [
            m
            for m in markets.values()
            if m.get("swap")
            and m.get("linear")
            and m.get("settle") == "USDT"
            and m.get("active") is True
            and symbol.upper()
            in {m["symbol"].upper(), m["id"].upper(), (m["base"] + m["quote"]).upper()}
        ]
        if len(matches) != 1:
            raise ProviderError(
                "active Gate USDT perpetual not uniquely resolved",
                code="unsupported_contract",
                provider=self.provider_name,
            )
        market = matches[0]
        if not market.get("contractSize") or not market.get("precision"):
            raise ProviderError(
                "missing contract sizing metadata",
                code="metadata_missing",
                provider=self.provider_name,
            )
        return market

    def get_quote(self, instrument):
        with self._lock:
            ticker = self.exchange.fetch_ticker(
                self.market(instrument.symbol)["symbol"]
            )
        # Gate ticker response does not always contain exchange timestamp.
        # Mark retrieval time explicitly; it is not a claimed last-trade time.
        timestamp = (
            datetime.fromtimestamp(ticker["timestamp"] / 1000, timezone.utc)
            if ticker.get("timestamp")
            else datetime.now(timezone.utc)
        )
        return Quote(
            instrument,
            timestamp,
            float(ticker["last"]),
            ticker.get("baseVolume"),
            ticker.get("percentage"),
        )

    def get_bars(self, instrument, timeframe, limit=240):
        if timeframe not in {"5m", "15m", "1h", "1d"}:
            raise ValueError("unsupported timeframe")
        with self._lock:
            rows = self.exchange.fetch_ohlcv(
                self.market(instrument.symbol)["symbol"],
                timeframe,
                limit=max(1, min(limit, 1000)),
            )
        return [
            Bar(
                datetime.fromtimestamp(row[0] / 1000, timezone.utc),
                *map(float, row[1:6]),
            )
            for row in rows
        ]

    def funding_context(self, symbol):
        with self._lock:
            market = self.market(symbol)
            rate = self.exchange.fetch_funding_rate(market["symbol"])
            history = self.exchange.fetch_funding_rate_history(
                market["symbol"], limit=100
            )
            oi = self.exchange.fetch_open_interest_history(
                market["symbol"], "1h", limit=24
            )
        return {
            "rate": rate,
            "history": history,
            "open_interest_history": oi,
            "source": self.provider_name,
        }
