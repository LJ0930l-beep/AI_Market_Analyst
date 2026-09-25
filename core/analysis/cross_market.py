"""Source-labelled public cross-market references for the radar.

Yahoo chart quotes may be delayed and are never represented as exchange
execution prices. CoinGecko's global snapshot supplies BTC dominance. Failed
or old responses retain an explicit status instead of a fabricated zero.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
import threading
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen


_YAHOO_SYMBOLS = {"DXY": "DX-Y.NYB", "US10Y": "^TNX", "NQ": "NQ=F"}
_CACHE_LOCK = threading.RLock()
_CACHE: tuple[datetime, dict[str, Any]] | None = None


def _get_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "AI-Market-Analyst/2.0"})
    with urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise ValueError(f"HTTP_{response.status}")
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("CROSS_MARKET_INVALID_ENVELOPE")
    return result


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _yahoo_item(symbol: str, now: datetime, get_json: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    ticker = _YAHOO_SYMBOLS[symbol]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='')}?interval=1d&range=5d"
    result = get_json(url)
    meta = result["chart"]["result"][0]["meta"]
    value = _finite(meta.get("regularMarketPrice"))
    epoch = _finite(meta.get("regularMarketTime"))
    if value is None or value <= 0 or epoch is None:
        raise ValueError("CROSS_MARKET_QUOTE_MISSING")
    as_of = datetime.fromtimestamp(epoch, timezone.utc)
    if as_of > now + timedelta(minutes=5):
        raise ValueError("CROSS_MARKET_FUTURE_TIMESTAMP")
    previous = _finite(meta.get("previousClose") or meta.get("chartPreviousClose"))
    change = round((value / previous - 1) * 100, 3) if previous and previous > 0 else None
    age = now - as_of
    return {
        "symbol": symbol, "value": value, "change_pct": change,
        "status": "DELAYED" if age <= timedelta(minutes=30) else "LAST_CLOSE" if age <= timedelta(days=4) else "STALE",
        "source": f"Yahoo Finance · {ticker} · 延迟/参考行情", "as_of": as_of.isoformat(),
    }


def _dominance_item(now: datetime, get_json: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    data = get_json("https://api.coingecko.com/api/v3/global")["data"]
    value = _finite(data["market_cap_percentage"]["btc"])
    epoch = _finite(data.get("updated_at"))
    if value is None or not 0 < value < 100 or epoch is None:
        raise ValueError("BTC_DOMINANCE_MISSING")
    as_of = datetime.fromtimestamp(epoch, timezone.utc)
    if as_of > now + timedelta(minutes=5):
        raise ValueError("BTC_DOMINANCE_FUTURE_TIMESTAMP")
    return {
        "symbol": "BTC.D", "value": value, "change_pct": None,
        "status": "AVAILABLE" if now - as_of <= timedelta(minutes=30) else "STALE",
        "source": "CoinGecko Global · BTC 市值占比", "as_of": as_of.isoformat(),
    }


def fetch_cross_market(*, now: datetime | None = None, get_json: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    global _CACHE
    point = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fetch = get_json or _get_json
    if get_json is None:
        with _CACHE_LOCK:
            if _CACHE and point - _CACHE[0] < timedelta(seconds=90):
                return dict(_CACHE[1])

    tasks = {symbol: (lambda ticker=symbol: _yahoo_item(ticker, point, fetch)) for symbol in _YAHOO_SYMBOLS}
    tasks["BTC.D"] = lambda: _dominance_item(point, fetch)
    items: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {symbol: pool.submit(task) for symbol, task in tasks.items()}
        for symbol in ("DXY", "US10Y", "NQ", "BTC.D"):
            try:
                items.append(futures[symbol].result())
            except Exception:
                items.append({"symbol": symbol, "value": None, "change_pct": None, "status": "UNAVAILABLE", "source": "Yahoo Finance" if symbol != "BTC.D" else "CoinGecko Global", "as_of": None})
    available = [item for item in items if item["value"] is not None and item["status"] != "STALE"]
    as_of_values = [item["as_of"] for item in available if item["as_of"]]
    result = {
        "status": "AVAILABLE" if len(available) == 4 else "PARTIAL" if available else "UNAVAILABLE",
        "source": "Yahoo Finance 延迟/参考行情 + CoinGecko Global",
        "message": "宏观报价可能延迟；休市时标记为最近收盘。仅作背景参考，不作为 Gate 可执行价格。",
        "as_of": max(as_of_values) if as_of_values else None,
        "items": items, "synthetic": False,
    }
    if get_json is None:
        with _CACHE_LOCK:
            _CACHE = (point, result)
    return result
