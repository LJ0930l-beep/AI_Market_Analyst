"""Gate public market-data adapter.

The production Gate route is native REST first.  CCXT remains available only
when an exchange object is explicitly injected by an isolated test harness.
This keeps Gate's contract identity, closed-bar semantics and derivative-data
availability auditable instead of silently substituting another venue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import RLock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..instruments import AssetType, Instrument, TradingHours
from .base import Bar, ProviderError, Quote


GATE_PUBLIC_API_BASE_URL = "https://api.gateio.ws/api/v4"
GATE_TESTNET_PUBLIC_API_BASE_URL = "https://api-testnet.gateapi.io/api/v4"
_TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def _iso(value: datetime) -> str:
    point = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc).isoformat()


def _number(value: Any, *, name: str, positive: bool = False, non_negative: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"Gate field {name} is not numeric", code="schema_invalid", provider="gate") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise ProviderError(f"Gate field {name} is not finite", code="schema_invalid", provider="gate")
    if positive and result <= 0:
        raise ProviderError(f"Gate field {name} must be positive", code="schema_invalid", provider="gate")
    if non_negative and result < 0:
        raise ProviderError(f"Gate field {name} must be non-negative", code="schema_invalid", provider="gate")
    return result


class GatePublicProvider:
    """Read-only Gate USDT perpetual data provider.

    ``exchange=`` is an explicit dependency-injection seam for deterministic
    tests.  Without it every request uses Gate's official REST API and no
    synthetic or cross-venue fallback is attempted.
    """

    provider_name = "gate_public_swap"
    provider = "gate"
    environment = "TESTNET_PUBLIC"

    def __init__(
        self,
        exchange: Any | None = None,
        *,
        base_url: str | None = None,
        testnet: bool = False,
        timeout: float = 10.0,
        retries: int = 1,
    ) -> None:
        self.exchange = exchange
        self.testnet = bool(testnet)
        self.environment = "TESTNET_PUBLIC" if self.testnet else "LIVE_PUBLIC"
        self.base_url = (base_url or (GATE_TESTNET_PUBLIC_API_BASE_URL if self.testnet else GATE_PUBLIC_API_BASE_URL)).rstrip("/")
        self.timeout = max(1.0, float(timeout))
        self.retries = max(0, min(int(retries), 2))
        self._lock = RLock()
        self.last_snapshot: dict[str, Any] = {}

    @property
    def uses_native_rest(self) -> bool:
        return self.exchange is None

    @staticmethod
    def _compact(symbol: str) -> str:
        raw = str(symbol or "").strip().upper()
        if ":" in raw:
            raw = raw.split(":", 1)[0]
        if "/" in raw:
            base, quote = raw.split("/", 1)
            raw = f"{base}{quote}"
        return raw.replace("_", "").replace("-", "")

    @classmethod
    def _contract_id(cls, symbol: str) -> str:
        compact = cls._compact(symbol)
        if not compact.endswith("USDT") or len(compact) <= 4:
            raise ProviderError("Gate USDT perpetual symbol is required", code="unsupported_contract", provider="gate")
        return f"{compact[:-4]}_USDT"

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "ai-market-analyst/2.0"})
                with urlopen(request, timeout=self.timeout) as response:
                    if int(response.status) != 200:
                        raise ProviderError(f"Gate public HTTP {response.status}", code="http_error", provider="gate")
                    return json.loads(response.read().decode("utf-8"))
            except ProviderError:
                raise
            except HTTPError as exc:
                last_error = exc
                if exc.code in {400, 404, 422}:
                    raise ProviderError("Gate public request was rejected", code="request_rejected", provider="gate") from exc
            except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retries:
                continue
        raise ProviderError("Gate public data unavailable", code="network_unavailable", provider="gate") from last_error

    def _native_market(self, symbol: str) -> dict[str, Any]:
        contract = self._contract_id(symbol)
        payload = self._request(f"/futures/usdt/contracts/{contract}")
        if not isinstance(payload, dict):
            raise ProviderError("Gate contract metadata is not an object", code="schema_invalid", provider="gate")
        if str(payload.get("status") or "trading").lower() not in {"trading", "online"}:
            raise ProviderError("Gate contract is not trading", code="contract_inactive", provider="gate")
        contract_size = _number(payload.get("quanto_multiplier"), name="quanto_multiplier", positive=True)
        price_tick = _number(payload.get("order_price_round"), name="order_price_round", positive=True)
        amount_step = _number(payload.get("order_size_min"), name="order_size_min", positive=True)
        amount_max = _number(payload.get("order_size_max"), name="order_size_max", positive=True)
        return {
            "id": str(payload.get("name") or contract).upper(),
            "symbol": f"{contract[:-5]}/USDT:USDT",
            "base": contract[:-5],
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "swap": True,
            "future": False,
            "contract": True,
            "linear": True,
            "active": True,
            "contractSize": contract_size,
            "precision": {"price": price_tick, "amount": amount_step},
            "limits": {"amount": {"min": amount_step, "max": amount_max}},
            "taker": _number(payload.get("taker_fee_rate"), name="taker_fee_rate", non_negative=True),
            "maker": payload.get("maker_fee_rate"),
            "mark_price": _number(payload.get("mark_price"), name="mark_price", positive=True),
            "index_price": _number(payload.get("index_price"), name="index_price", positive=True),
            "funding_rate": payload.get("funding_rate"),
            "funding_next_apply": payload.get("funding_next_apply"),
            "source": "gate_native_rest_contract",
            "raw": payload,
        }

    def market(self, symbol: str) -> dict[str, Any]:
        if self.exchange is not None:
            with self._lock:
                markets = self.exchange.load_markets()
            compact = self._compact(symbol)
            matches = [
                market for market in markets.values()
                if market.get("swap") and market.get("linear") and str(market.get("settle") or "").upper() == "USDT"
                and market.get("active") is True
                and compact in {
                    str(market.get("symbol") or "").upper().replace("/", "").replace(":USDT", ""),
                    str(market.get("id") or "").upper().replace("_", ""),
                    f"{str(market.get('base') or '').upper()}{str(market.get('quote') or '').upper()}",
                }
            ]
            if len(matches) != 1:
                raise ProviderError("active Gate USDT perpetual not uniquely resolved", code="unsupported_contract", provider=self.provider_name)
            market = dict(matches[0])
            if not market.get("contractSize") or not market.get("precision"):
                raise ProviderError("missing contract sizing metadata", code="metadata_missing", provider=self.provider_name)
            return market
        return self._native_market(symbol)

    def _native_ticker(self, symbol: str) -> dict[str, Any]:
        contract = self._contract_id(symbol)
        payload = self._request("/futures/usdt/tickers", {"contract": contract})
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if not isinstance(payload, dict):
            raise ProviderError("Gate ticker response is not an object", code="schema_invalid", provider="gate")
        return payload

    def get_quote(self, instrument: Instrument) -> Quote:
        if self.exchange is not None:
            with self._lock:
                ticker = self.exchange.fetch_ticker(self.market(instrument.symbol)["symbol"])
            timestamp = datetime.fromtimestamp(ticker["timestamp"] / 1000, timezone.utc) if ticker.get("timestamp") else datetime.now(timezone.utc)
            return Quote(instrument, timestamp, float(ticker["last"]), ticker.get("baseVolume"), ticker.get("percentage"), ticker.get("high"), ticker.get("low"))
        ticker = self._native_ticker(instrument.symbol)
        observed = datetime.now(timezone.utc)
        price = _number(ticker.get("last"), name="last", positive=True)
        self.last_snapshot = {"provider": "gate", "environment": self.environment, "native_symbol": ticker.get("contract"), "data_as_of": _iso(observed), "source": "gate_native_rest_ticker"}
        return Quote(
            instrument,
            observed,
            price,
            _number(ticker.get("volume_24h_base"), name="volume_24h_base", non_negative=True) if ticker.get("volume_24h_base") is not None else None,
            float(ticker["change_percentage"]) if ticker.get("change_percentage") is not None else None,
            float(ticker["high_24h"]) if ticker.get("high_24h") is not None else None,
            float(ticker["low_24h"]) if ticker.get("low_24h") is not None else None,
        )

    @staticmethod
    def _bar_from_row(row: dict[str, Any], timeframe: str, *, fetched_at: datetime, price_type: str = "last") -> Bar:
        start_seconds = _number(row.get("t"), name="t", non_negative=True)
        start = datetime.fromtimestamp(start_seconds, timezone.utc)
        end = start + timedelta(seconds=_TIMEFRAME_SECONDS[timeframe])
        values = [_number(row.get(key), name=key, positive=key in {"o", "h", "l", "c"}, non_negative=key == "v") for key in ("o", "h", "l", "c", "v")]
        if values[2] > min(values[0], values[3]) or values[1] < max(values[0], values[3]):
            raise ProviderError("Gate candlestick violates OHLC bounds", code="schema_invalid", provider="gate")
        raw = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
        revision = hashlib.sha256(f"gate:{price_type}:{timeframe}:{raw}".encode("utf-8")).hexdigest()[:32]
        return Bar(
            start,
            *values,
            bar_end=end,
            event_at=start,
            event_time=start,
            first_received_at=fetched_at,
            available_at=fetched_at,
            fetched_at=fetched_at,
            revision_id=revision,
            volume_unit="contracts",
            source=f"gate_native_rest:{price_type}",
            is_closed=end <= fetched_at,
        )

    def _native_bars(self, symbol: str, timeframe: str, limit: int, *, price_type: str = "last") -> list[Bar]:
        if timeframe not in _TIMEFRAME_SECONDS:
            raise ValueError("unsupported timeframe")
        fetched_at = datetime.now(timezone.utc)
        params: dict[str, Any] = {"contract": self._contract_id(symbol), "interval": timeframe, "limit": max(1, min(int(limit), 1000))}
        if price_type in {"mark", "index"}:
            params["price_type"] = price_type
        payload = self._request("/futures/usdt/candlesticks", params)
        if not isinstance(payload, list):
            raise ProviderError("Gate candlestick response is not a list", code="schema_invalid", provider="gate")
        rows: dict[float, Bar] = {}
        for row in payload:
            if not isinstance(row, dict):
                raise ProviderError("Gate candlestick row is not an object", code="schema_invalid", provider="gate")
            bar = self._bar_from_row(row, timeframe, fetched_at=fetched_at, price_type=price_type)
            rows[bar.timestamp.timestamp()] = bar
        bars = sorted(rows.values(), key=lambda item: item.timestamp)
        if not bars:
            raise ProviderError("Gate returned no candlesticks", code="empty_data", provider="gate")
        return bars

    def get_bars(self, instrument: Instrument, timeframe: str, limit: int = 240) -> list[Bar]:
        if self.exchange is not None:
            if timeframe not in _TIMEFRAME_SECONDS:
                raise ValueError("unsupported timeframe")
            with self._lock:
                rows = self.exchange.fetch_ohlcv(self.market(instrument.symbol)["symbol"], timeframe, limit=max(1, min(int(limit), 1000)))
            fetched_at = datetime.now(timezone.utc)
            return [
                Bar(
                    datetime.fromtimestamp(row[0] / 1000, timezone.utc),
                    *map(float, row[1:6]),
                    bar_end=datetime.fromtimestamp(row[0] / 1000, timezone.utc) + timedelta(seconds=_TIMEFRAME_SECONDS[timeframe]),
                    available_at=fetched_at,
                    fetched_at=fetched_at,
                    first_received_at=fetched_at,
                    source="gate_ccxt_injected",
                    is_closed=datetime.fromtimestamp(row[0] / 1000, timezone.utc) + timedelta(seconds=_TIMEFRAME_SECONDS[timeframe]) <= fetched_at,
                )
                for row in rows
            ]
        price_type = str(getattr(instrument, "price_type", "last") or "last").lower()
        return self._native_bars(instrument.symbol, timeframe, limit, price_type=price_type)

    def _funding_history(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        payload = self._request("/futures/usdt/funding_rate", {"contract": self._contract_id(symbol), "limit": max(1, min(int(limit), 100))})
        if not isinstance(payload, list):
            raise ProviderError("Gate funding response is not a list", code="schema_invalid", provider="gate")
        return [{"timestamp": int(_number(row.get("t"), name="funding_time") * 1000), "fundingRate": _number(row.get("r"), name="funding_rate"), "unit": "decimal_fraction", "raw": row} for row in payload if isinstance(row, dict)]

    def _open_interest_history(self, symbol: str, limit: int = 24) -> list[dict[str, Any]]:
        payload = self._request("/futures/usdt/contract_stats", {"contract": self._contract_id(symbol), "interval": "1h", "limit": max(1, min(int(limit), 100))})
        if not isinstance(payload, list):
            raise ProviderError("Gate contract stats response is not a list", code="schema_invalid", provider="gate")
        return [{"timestamp": int(_number(row.get("time"), name="open_interest_time") * 1000), "openInterestAmount": _number(row.get("open_interest"), name="open_interest", non_negative=True), "unit": "contracts", "raw": row} for row in payload if isinstance(row, dict) and row.get("open_interest") is not None]

    def funding_context(self, symbol: str) -> dict[str, Any]:
        if self.exchange is not None:
            with self._lock:
                market = self.market(symbol)
                rate = self.exchange.fetch_funding_rate(market["symbol"])
                history = self.exchange.fetch_funding_rate_history(market["symbol"], limit=100)
                oi = self.exchange.fetch_open_interest_history(market["symbol"], "1h", limit=24)
            return {"rate": rate, "history": history, "open_interest_history": oi, "source": self.provider_name, "provider": "gate", "environment": self.environment}
        contract = self._contract_id(symbol)
        metadata = self._request(f"/futures/usdt/contracts/{contract}")
        if not isinstance(metadata, dict):
            raise ProviderError("Gate funding metadata is not an object", code="schema_invalid", provider="gate")
        return {"rate": {"fundingRate": _number(metadata.get("funding_rate"), name="funding_rate"), "fundingTimestamp": metadata.get("funding_next_apply")}, "history": self._funding_history(symbol), "open_interest_history": self._open_interest_history(symbol), "source": self.provider_name, "provider": "gate", "environment": self.environment, "native_symbol": contract, "data_status": "AVAILABLE", "raw_contract": metadata}

    def order_book(self, symbol: str, *, limit: int = 20) -> dict[str, Any]:
        if self.exchange is not None:
            with self._lock:
                return dict(self.exchange.fetch_order_book(self.market(symbol)["symbol"], limit=max(1, min(int(limit), 100))))
        payload = self._request("/futures/usdt/order_book", {"contract": self._contract_id(symbol), "interval": "0", "limit": max(1, min(int(limit), 100)), "with_id": "true"})
        if not isinstance(payload, dict) or not isinstance(payload.get("asks"), list) or not isinstance(payload.get("bids"), list):
            raise ProviderError("Gate order book response is invalid", code="schema_invalid", provider="gate")
        return {"provider": "gate", "environment": self.environment, "native_symbol": self._contract_id(symbol), "sequence": payload.get("id"), "event_at": payload.get("current") or payload.get("update"), "asks": payload["asks"], "bids": payload["bids"], "source": "gate_native_rest_order_book", "raw": payload}

    def trades(self, symbol: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if self.exchange is not None:
            with self._lock:
                return list(self.exchange.fetch_trades(self.market(symbol)["symbol"], limit=max(1, min(int(limit), 1000))))
        payload = self._request("/futures/usdt/trades", {"contract": self._contract_id(symbol), "limit": max(1, min(int(limit), 1000))})
        if not isinstance(payload, list):
            raise ProviderError("Gate trades response is not a list", code="schema_invalid", provider="gate")
        return [{"provider": "gate", "environment": self.environment, "native_symbol": self._contract_id(symbol), "trade_id": row.get("id"), "event_at": row.get("create_time_ms") or row.get("create_time"), "price": row.get("price"), "size": row.get("size"), "side": "BUY" if float(row.get("size") or 0) > 0 else "SELL", "source": "gate_native_rest_trades", "raw": row} for row in payload if isinstance(row, dict)]

    def liquidations(self, symbol: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if self.exchange is not None:
            return []
        payload = self._request("/futures/usdt/liq_orders", {"contract": self._contract_id(symbol), "limit": max(1, min(int(limit), 1000))})
        if not isinstance(payload, list):
            raise ProviderError("Gate liquidation response is not a list", code="schema_invalid", provider="gate")
        return [{"provider": "gate", "environment": self.environment, "native_symbol": self._contract_id(symbol), "event_at": row.get("time") or row.get("create_time"), "source": "gate_native_rest_liquidations", "raw": row} for row in payload if isinstance(row, dict)]

    def adl_risk_states(self) -> dict[str, Any]:
        payload = self._request("/futures/usdt/adl_risk_states")
        if not isinstance(payload, dict):
            raise ProviderError("Gate ADL response is not an object", code="schema_invalid", provider="gate")
        return {"provider": "gate", "environment": self.environment, "source": "gate_native_rest_adl", **payload}

    def bootstrap_symbol(
        self,
        symbol: str,
        *,
        instrument: Instrument | None = None,
        timeframes: tuple[str, ...] = ("5m", "15m", "1h", "1d"),
        min_15m: int = 600,
    ) -> dict[str, Any]:
        """Fetch bounded native Gate bars and derivative context for readiness."""

        if self.exchange is not None:
            raise ProviderError("native Gate bootstrap requires the REST provider", code="native_rest_required", provider="gate")
        normalized = tuple(dict.fromkeys(str(item).lower() for item in timeframes))
        if any(item not in _TIMEFRAME_SECONDS for item in normalized):
            raise ProviderError("unsupported Gate bootstrap timeframe", code="unsupported_timeframe", provider="gate")
        instrument = instrument or Instrument(symbol=self._compact(symbol), asset_type=AssetType.CRYPTO, exchange="GATE", currency="USDT", timezone="UTC", trading_hours=TradingHours.AROUND_THE_CLOCK, contract_type="perp")
        market = self.market(symbol)
        quote = self.get_quote(instrument)
        bars: dict[str, list[Bar]] = {}
        required = max(1, int(min_15m))
        for timeframe in normalized:
            limit = required if timeframe == "15m" else (240 if timeframe == "5m" else 120 if timeframe == "1h" else 60)
            bars[timeframe] = self._native_bars(symbol, timeframe, limit, price_type="last")
        if len(bars.get("15m", [])) < required:
            raise ProviderError("Gate 15m bootstrap sample is incomplete", code="bootstrap_insufficient", provider="gate")
        mark_bars = self._native_bars(symbol, "15m", required, price_type="mark")
        index_bars = self._native_bars(symbol, "15m", required, price_type="index")
        funding = self.funding_context(symbol)
        book = self.order_book(symbol, limit=20)
        recent_trades = self.trades(symbol, limit=100)
        return {
            "provider": "gate",
            "environment": self.environment,
            "symbol": self._compact(symbol),
            "native_symbol": market["id"],
            "market": market,
            "quote": {"price": quote.price, "timestamp": _iso(quote.timestamp), "change_pct": quote.change_pct},
            "bars": bars,
            "mark_bars": mark_bars,
            "index_bars": index_bars,
            "funding": funding,
            "order_book": book,
            "trades": recent_trades,
            "liquidations": self.liquidations(symbol, limit=100),
            "quality": {"status": "READY", "source": "gate_native_rest", "closed_15m_bars": sum(1 for bar in bars["15m"] if bar.is_closed), "mark_index_aligned": [bar.timestamp for bar in mark_bars] == [bar.timestamp for bar in index_bars], "synthetic": False},
        }

    native_bootstrap = bootstrap_symbol

    def list_active_usdt_contracts(self, limit: int = 300) -> list[dict[str, Any]]:
        if self.exchange is not None:
            with self._lock:
                markets = self.exchange.load_markets()
            contracts = []
            for market in markets.values():
                if market.get("swap") and market.get("linear") and str(market.get("settle") or "").upper() == "USDT" and market.get("active") is True:
                    contracts.append({"symbol": f"{market.get('base', '')}{market.get('quote', '')}".upper(), "gate_id": market.get("id"), "ccxt_symbol": market.get("symbol"), "base": market.get("base"), "quote": market.get("quote"), "contract_size": float(market.get("contractSize", 1.0) or 1.0), "price_precision": market.get("precision", {}).get("price"), "amount_precision": market.get("precision", {}).get("amount")})
            return contracts[: max(1, min(int(limit), 1000))]
        payload = self._request("/futures/usdt/contracts")
        if not isinstance(payload, list):
            raise ProviderError("Gate contracts response is not a list", code="schema_invalid", provider="gate")
        contracts = []
        for row in payload:
            if not isinstance(row, dict) or str(row.get("status") or "").lower() not in {"trading", "online"}:
                continue
            native = str(row.get("name") or "").upper()
            if not native.endswith("_USDT"):
                continue
            contracts.append({"symbol": native.replace("_", ""), "gate_id": native, "ccxt_symbol": f"{native[:-5]}/USDT:USDT", "base": native[:-5], "quote": "USDT", "contract_size": float(row.get("quanto_multiplier") or 0), "price_precision": row.get("order_price_round"), "amount_precision": row.get("order_size_min"), "source": "gate_native_rest_contracts"})
        major_priority = {"BTCUSDT": 1, "ETHUSDT": 2, "SOLUSDT": 3, "DOGEUSDT": 4, "PEPEUSDT": 5, "SUIUSDT": 6, "XRPUSDT": 7, "NEARUSDT": 8, "AVAXUSDT": 9, "BNBUSDT": 10, "APTUSDT": 11, "LINKUSDT": 12}
        contracts.sort(key=lambda item: (major_priority.get(item["symbol"], 999), item["symbol"]))
        return contracts[: max(1, min(int(limit), 1000))]


__all__ = ["GATE_PUBLIC_API_BASE_URL", "GATE_TESTNET_PUBLIC_API_BASE_URL", "GatePublicProvider"]
