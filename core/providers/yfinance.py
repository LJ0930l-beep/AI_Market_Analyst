"""Public Yahoo Finance market adapter with an optional yfinance fast path.

The provider keeps the Phase 1 contract stable. If the optional ``yfinance``
package is not installed, it uses Yahoo's public chart endpoint directly; no
account, order, or credential API is involved.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..instruments import Instrument
from .base import Bar, ProviderError, Quote


class YFinanceProvider:
    provider_name = "yfinance"
    stale = False

    _INTERVALS = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "1h", "1d": "1d"}
    _RANGES = {"5m": "60d", "15m": "60d", "1h": "730d", "4h": "730d", "1d": "2y"}

    def __init__(self, period: str = "1y", *, base_url: str | None = None, timeout: float | None = None, retries: int = 1) -> None:
        self.period = period
        self.base_url = (base_url or os.environ.get("YAHOO_CHART_BASE_URL", "https://query1.finance.yahoo.com/v8/finance/chart")).rstrip("/")
        self.timeout = timeout if timeout is not None else float(os.environ.get("MARKET_PROVIDER_TIMEOUT_SEC", "10"))
        self.retries = max(0, min(retries, 2))

    def _ticker(self, instrument: Instrument):
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - exercised in minimal environments
            raise ProviderError(
                "optional yfinance package is not installed; use Yahoo public chart fallback",
                code="dependency_missing",
                provider=self.provider_name,
            ) from exc
        return yf.Ticker(instrument.symbol)

    def _request_chart(self, instrument: Instrument, timeframe: str, limit: int) -> dict[str, object]:
        interval = self._INTERVALS.get(timeframe.lower())
        if interval is None:
            raise ProviderError(f"unsupported Yahoo timeframe: {timeframe}", code="unsupported_timeframe", provider=self.provider_name)
        multiplier = 4 if timeframe.lower() == "4h" else 1
        params = {"range": self._RANGES[timeframe.lower()], "interval": interval, "includePrePost": "false", "events": "div,splits", "includeTimestamps": "true"}
        url = f"{self.base_url}/{quote(instrument.symbol)}?{urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": "ai-market-analyst/0.2"})
                with urlopen(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise ProviderError(f"Yahoo returned HTTP {response.status}", code="http_error", provider=self.provider_name)
                    payload = json.loads(response.read().decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ProviderError("Yahoo returned malformed chart envelope", code="invalid_payload", provider=self.provider_name)
                    payload["_requested_limit"] = limit * multiplier
                    return payload
            except ProviderError:
                raise
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise ProviderError(f"Yahoo chart request failed: {last_error}", code="network_error", provider=self.provider_name) from last_error

    @staticmethod
    def _bars_from_chart(payload: dict[str, object], *, timeframe: str, limit: int) -> list[Bar]:
        try:
            chart = payload["chart"]
            result = chart["result"][0]  # type: ignore[index]
            timestamps = result["timestamp"]  # type: ignore[index]
            quote_data = result["indicators"]["quote"][0]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Yahoo returned malformed chart data", code="invalid_payload", provider="yfinance") from exc
        bars: list[Bar] = []
        try:
            for index, raw_timestamp in enumerate(timestamps):
                open_ = quote_data["open"][index]
                high = quote_data["high"][index]
                low = quote_data["low"][index]
                close = quote_data["close"][index]
                volume = quote_data.get("volume", [0] * len(timestamps))[index]
                if any(value is None for value in (open_, high, low, close)):
                    continue
                bars.append(
                    Bar(
                        timestamp=datetime.fromtimestamp(float(raw_timestamp), tz=timezone.utc),
                        open=float(open_),
                        high=float(high),
                        low=float(low),
                        close=float(close),
                        volume=float(volume or 0.0),
                    )
                )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Yahoo returned malformed OHLCV rows", code="invalid_payload", provider="yfinance") from exc
        if timeframe.lower() == "4h":
            bars = _resample_four_hour(bars)
        return bars[-limit:]

    def _get_yahoo_bars(self, instrument: Instrument, timeframe: str, limit: int) -> list[Bar]:
        payload = self._request_chart(instrument, timeframe, limit)
        bars = self._bars_from_chart(payload, timeframe=timeframe, limit=limit)
        if not bars:
            raise ProviderError(f"no market data for {instrument.symbol}", code="empty_data", provider=self.provider_name)
        return bars

    def get_bars(self, instrument: Instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        timeframe = timeframe.lower()
        if timeframe not in self._INTERVALS:
            raise ProviderError(f"unsupported Yahoo timeframe: {timeframe}", code="unsupported_timeframe", provider=self.provider_name)
        try:
            ticker = self._ticker(instrument)
        except ProviderError as dependency_error:
            if dependency_error.code != "dependency_missing":
                raise
            return self._get_yahoo_bars(instrument, timeframe, limit)
        interval = self._INTERVALS[timeframe]
        try:
            frame = ticker.history(period=self.period, interval=interval, auto_adjust=False)
        except Exception as exc:  # pragma: no cover - network/provider dependent
            return self._get_yahoo_bars(instrument, timeframe, limit) if timeframe != "4h" else self._get_yahoo_bars(instrument, timeframe, limit)
        if frame is None or frame.empty:
            return self._get_yahoo_bars(instrument, timeframe, limit)
        bars: list[Bar] = []
        for index, row in frame.tail(limit * (4 if timeframe == "4h" else 1)).iterrows():
            timestamp = index.to_pydatetime() if hasattr(index, "to_pydatetime") else index
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            bars.append(
                Bar(
                    timestamp=timestamp.astimezone(timezone.utc),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0)),
                )
            )
        if timeframe == "4h":
            bars = _resample_four_hour(bars)
        return bars[-limit:]

    def get_quote(self, instrument: Instrument) -> Quote:
        bars = self.get_bars(instrument, "1d", limit=2)
        last = bars[-1]
        previous = bars[-2].close if len(bars) > 1 else None
        return Quote(
            instrument,
            last.timestamp,
            last.close,
            last.volume,
            ((last.close / previous) - 1.0) * 100.0 if previous else None,
            last.high,
            last.low,
        )


def _resample_four_hour(bars: list[Bar]) -> list[Bar]:
    groups: dict[datetime, list[Bar]] = {}
    for bar in bars:
        bucket_hour = (bar.timestamp.hour // 4) * 4
        bucket = bar.timestamp.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
        groups.setdefault(bucket, []).append(bar)
    result: list[Bar] = []
    for bucket in sorted(groups):
        group = groups[bucket]
        result.append(Bar(bucket, group[0].open, max(item.high for item in group), min(item.low for item in group), group[-1].close, sum(item.volume for item in group)))
    return result
