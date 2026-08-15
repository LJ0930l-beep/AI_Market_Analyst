"""Binance public REST/Kline provider.

Only public market endpoints are used. No account, order, or key-bearing API is
implemented in this module.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..instruments import Instrument
from .base import Bar, ProviderError, Quote


class BinancePublicProvider:
    provider_name = "binance_public"
    stale = False
    _INTERVALS = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

    def __init__(self, base_url: str | None = None, timeout: float | None = None, retries: int = 2) -> None:
        self.base_url = (base_url or os.environ.get("BINANCE_BASE_URL", "https://api.binance.com")).rstrip("/")
        self.timeout = timeout or float(os.environ.get("MARKET_PROVIDER_TIMEOUT_SEC", "10"))
        self.retries = max(0, min(retries, 3))

    def _request(self, path: str, params: dict[str, object]) -> object:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": "ai-market-analyst/0.2"})
                with urlopen(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise ProviderError(
                            f"Binance returned HTTP {response.status}",
                            code="http_error",
                            provider=self.provider_name,
                        )
                    return json.loads(response.read().decode("utf-8"))
            except ProviderError:
                raise
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise ProviderError(
            f"Binance request failed: {last_error}",
            code="network_error",
            provider=self.provider_name,
        ) from last_error

    def _symbol(self, instrument: Instrument) -> str:
        if instrument.asset_type.value != "crypto":
            raise ProviderError("Binance provider only handles crypto instruments", code="unsupported_asset", provider=self.provider_name)
        return instrument.symbol.upper()

    def get_bars(self, instrument: Instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        interval = self._INTERVALS.get(timeframe.lower())
        if interval is None:
            raise ProviderError(f"unsupported Binance timeframe: {timeframe}", code="unsupported_timeframe", provider=self.provider_name)
        payload = self._request("/api/v3/klines", {"symbol": self._symbol(instrument), "interval": interval, "limit": min(max(limit, 1), 1000)})
        if not isinstance(payload, list) or not payload:
            raise ProviderError("Binance returned no klines", code="empty_data", provider=self.provider_name)
        bars: list[Bar] = []
        try:
            for row in payload:
                bars.append(
                    Bar(
                        timestamp=datetime.fromtimestamp(float(row[0]) / 1000, tz=timezone.utc),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
        except (IndexError, TypeError, ValueError) as exc:
            raise ProviderError("Binance returned malformed klines", code="invalid_payload", provider=self.provider_name) from exc
        return bars

    def get_quote(self, instrument: Instrument) -> Quote:
        payload = self._request("/api/v3/ticker/price", {"symbol": self._symbol(instrument)})
        try:
            price = float(payload["price"])  # type: ignore[index]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Binance returned malformed ticker", code="invalid_payload", provider=self.provider_name) from exc
        return Quote(instrument=instrument, timestamp=datetime.now(timezone.utc), price=price)
