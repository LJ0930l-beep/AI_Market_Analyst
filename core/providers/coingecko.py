"""Small CoinGecko public fallback for crypto price/history context."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..instruments import Instrument
from .base import Bar, ProviderError, Quote


class CoinGeckoPublicProvider:
    provider_name = "coingecko_public"
    stale = False
    _IDS = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum"}

    def __init__(self, base_url: str | None = None, timeout: float | None = None, retries: int = 1) -> None:
        self.base_url = (base_url or os.environ.get("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3")).rstrip("/")
        self.timeout = timeout or float(os.environ.get("MARKET_PROVIDER_TIMEOUT_SEC", "10"))
        self.retries = max(0, min(retries, 2))

    def _request(self, path: str, params: dict[str, object]) -> object:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": "ai-market-analyst/0.2"})
                with urlopen(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise ProviderError(f"CoinGecko returned HTTP {response.status}", code="http_error", provider=self.provider_name)
                    return json.loads(response.read().decode("utf-8"))
            except ProviderError:
                raise
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise ProviderError(f"CoinGecko request failed: {last_error}", code="network_error", provider=self.provider_name) from last_error

    def _id(self, instrument: Instrument) -> str:
        try:
            return self._IDS[instrument.symbol]
        except KeyError as exc:
            raise ProviderError(f"unsupported CoinGecko instrument: {instrument.symbol}", code="unsupported_asset", provider=self.provider_name) from exc

    def get_quote(self, instrument: Instrument) -> Quote:
        payload = self._request("/simple/price", {"ids": self._id(instrument), "vs_currencies": "usd"})
        try:
            price = float(payload[self._id(instrument)]["usd"])  # type: ignore[index]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("CoinGecko returned malformed price", code="invalid_payload", provider=self.provider_name) from exc
        return Quote(instrument=instrument, timestamp=datetime.now(timezone.utc), price=price)

    def get_bars(self, instrument: Instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        if timeframe.lower() not in {"1h", "4h", "1d"}:
            raise ProviderError(f"CoinGecko fallback does not support {timeframe}", code="unsupported_timeframe", provider=self.provider_name)
        days = max(2, min(90, (limit // 24) + 2))
        payload = self._request(f"/coins/{self._id(instrument)}/market_chart", {"vs_currency": "usd", "days": days, "interval": "hourly"})
        try:
            prices = payload["prices"]  # type: ignore[index]
            points = [(datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc), float(price)) for ms, price in prices]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("CoinGecko returned malformed history", code="invalid_payload", provider=self.provider_name) from exc
        points = points[-limit:]
        if not points:
            raise ProviderError("CoinGecko returned no history", code="empty_data", provider=self.provider_name)
        bars: list[Bar] = []
        previous = points[0][1]
        for timestamp, price in points:
            high = max(previous, price)
            low = min(previous, price)
            bars.append(Bar(timestamp, previous, high, low, price, 0.0))
            previous = price
        return bars

