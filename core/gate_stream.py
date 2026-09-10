"""Bounded Gate native public WebSocket ingestion.

The stream carries closed 5m/15m/1h candles plus trades, liquidations and
order-book deltas.  Order-book state is never considered trusted across a
sequence gap; the stream requests a fresh Gate REST snapshot and emits the
gap as an auditable event.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .providers.base import Bar, ProviderError
from .providers.gateio_provider import GatePublicProvider
from .realtime import RealtimeConnectionState


class GateNativeRealtimeStream:
    provider_name = "gate_native_public_ws"
    url = "wss://fx-ws.gateio.ws/v4/ws/usdt"

    def __init__(
        self,
        symbols: Iterable[str],
        provider: GatePublicProvider | None = None,
        *,
        timeframes: tuple[str, ...] = ("5m", "15m", "1h"),
        websocket_factory: Callable[[str, float], object] | None = None,
        max_symbols: int = 50,
    ) -> None:
        normalized = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
        if not normalized or len(normalized) > max(1, min(int(max_symbols), 50)):
            raise ValueError("Gate WebSocket symbols must contain between 1 and 50 symbols")
        selected = tuple(dict.fromkeys(str(item).lower() for item in timeframes))
        if not selected or any(item not in {"5m", "15m", "1h"} for item in selected):
            raise ValueError("Gate WebSocket timeframes must be 5m, 15m or 1h")
        self.symbols = normalized
        self.timeframes = selected
        self.provider = provider or GatePublicProvider()
        self.websocket_factory = websocket_factory or self._default_websocket
        self.stop_event = threading.Event()
        self._socket_lock = threading.Lock()
        self._active_socket: object | None = None
        self.reconnect_count = 0
        self.last_message_at: datetime | None = None
        self.last_error: str | None = None
        self._book_sequence: dict[str, int] = {}
        self._book_trusted: dict[str, bool] = {}

    @staticmethod
    def _default_websocket(url: str, timeout: float) -> object:
        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("websocket-client is not installed", code="websocket_dependency_missing", provider="gate_native_public_ws") from exc
        return websocket.create_connection(url, timeout=timeout, header=["X-Gate-Size-Decimal: 1"])

    def state(self, status: str = "idle") -> RealtimeConnectionState:
        return RealtimeConnectionState(status, self.provider_name, self.symbols, ",".join(self.timeframes), self.reconnect_count, self.last_message_at, self.last_error)

    def stop(self) -> None:
        self.stop_event.set()
        with self._socket_lock:
            websocket = self._active_socket
        if websocket is not None:
            try:
                websocket.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    @staticmethod
    def _contract(provider: GatePublicProvider, symbol: str) -> str:
        market = provider.market(symbol)
        return str(market.get("id") or GatePublicProvider._contract_id(symbol)).upper()

    @staticmethod
    def _bar_from_update(result: dict[str, Any], timeframe: str) -> tuple[str, Bar, bool] | None:
        name = str(result.get("n") or "")
        contract = name.removeprefix(f"{timeframe}_").upper()
        if not contract:
            contract = str(result.get("contract") or result.get("s") or "").upper()
        try:
            start = datetime.fromtimestamp(float(result["t"]), timezone.utc)
            end = datetime.fromtimestamp(float(result["t"]), timezone.utc)
            seconds = {"5m": 300, "15m": 900, "1h": 3600}[timeframe]
            end = end + timedelta(seconds=seconds)
            closed = result.get("w") in (True, 1, "1", "true", "TRUE")
            bar = Bar(
                start,
                float(result["o"]), float(result["h"]), float(result["l"]), float(result["c"]), float(result.get("v", 0)),
                bar_end=end,
                event_at=datetime.now(timezone.utc),
                event_time=start,
                sequence=result.get("id") or result.get("t"),
                is_closed=closed,
                first_received_at=datetime.now(timezone.utc),
                available_at=datetime.now(timezone.utc),
                fetched_at=datetime.now(timezone.utc),
                source="gate_native_ws",
                volume_unit="contracts",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Gate WebSocket returned a malformed candlestick", code="websocket_invalid_payload", provider="gate_native_public_ws") from exc
        return contract, bar, closed

    @staticmethod
    def _sequence_values(result: dict[str, Any]) -> tuple[int | None, int | None]:
        def integer(*names: str) -> int | None:
            for name in names:
                value = result.get(name)
                if value is None:
                    continue
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
            return None
        return integer("U", "first", "first_sequence"), integer("u", "last", "last_sequence", "id")

    def _handle_order_book(self, contract: str, result: dict[str, Any], *, on_event: Callable[[str, dict[str, Any]], None]) -> None:
        first, last = self._sequence_values(result)
        previous = self._book_sequence.get(contract)
        if first is None or last is None:
            status = "UNKNOWN_SEQUENCE"
            trusted = False
        elif previous is None:
            status = "SNAPSHOT_REQUIRED"
            trusted = False
        elif first > previous + 1:
            status = "GAP_DETECTED"
            trusted = False
        elif last <= previous:
            status = "OUT_OF_ORDER"
            trusted = False
        else:
            status = "CONTIGUOUS"
            trusted = True
        payload = {"provider": "gate", "environment": "LIVE_PUBLIC", "native_symbol": contract, "first_sequence": first, "last_sequence": last, "sequence_status": status, "event_at": result.get("time") or result.get("t"), "bids": result.get("bids") or result.get("b") or [], "asks": result.get("asks") or result.get("a") or [], "raw": result}
        self._book_trusted[contract] = trusted
        if trusted:
            self._book_sequence[contract] = int(last)
            on_event("order_book_delta", payload)
            return
        on_event("order_book_untrusted", payload)
        try:
            snapshot = self.provider.order_book(contract, limit=20)
            sequence = snapshot.get("sequence")
            if sequence is not None:
                self._book_sequence[contract] = int(float(sequence))
                self._book_trusted[contract] = True
            on_event("order_book_snapshot", snapshot)
        except Exception as exc:
            self._book_trusted[contract] = False
            on_event("order_book_snapshot_failed", {"native_symbol": contract, "error_code": getattr(exc, "code", type(exc).__name__), "sequence_status": status})

    def _subscribe(self, websocket: object, channel: str, payload: list[str]) -> None:
        websocket.send(json.dumps({"time": int(time.time()), "channel": channel, "event": "subscribe", "payload": payload}))  # type: ignore[attr-defined]

    def run_forever(
        self,
        *,
        on_bar: Callable[[str, Bar, bool], None],
        on_state: Callable[[RealtimeConnectionState], None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        timeout: float = 30.0,
        max_reconnect_delay: float = 30.0,
    ) -> None:
        emit = on_event or (lambda _kind, _payload: None)
        mapping = {self._contract(self.provider, symbol): symbol for symbol in self.symbols}
        delay = 1.0
        while not self.stop_event.is_set():
            websocket = None
            connected = False
            try:
                if on_state:
                    on_state(self.state("connecting"))
                websocket = self.websocket_factory(self.url, timeout)
                with self._socket_lock:
                    self._active_socket = websocket
                for contract in mapping:
                    for timeframe in self.timeframes:
                        self._subscribe(websocket, "futures.candlesticks", [timeframe, contract])
                    self._subscribe(websocket, "futures.order_book_update", [contract, "1000ms", "20"])
                    self._subscribe(websocket, "futures.trades", [contract])
                    self._subscribe(websocket, "futures.public_liquidates", [contract])
                    try:
                        snapshot = self.provider.order_book(contract, limit=20)
                        emit("order_book_snapshot", snapshot)
                        current = snapshot.get("sequence")
                        if current is not None:
                            self._book_sequence[contract] = int(float(current))
                            self._book_trusted[contract] = True
                    except Exception as exc:
                        self._book_trusted[contract] = False
                        emit("order_book_snapshot_failed", {"native_symbol": contract, "error_code": getattr(exc, "code", type(exc).__name__)})
                delay = 1.0
                connected = True
                if on_state:
                    on_state(self.state("connected"))
                while not self.stop_event.is_set():
                    raw = websocket.recv()  # type: ignore[attr-defined]
                    if raw is None:
                        raise ProviderError("Gate WebSocket closed", code="websocket_closed", provider=self.provider_name)
                    message = json.loads(raw if isinstance(raw, str) else bytes(raw).decode("utf-8"))
                    if not isinstance(message, dict):
                        continue
                    if message.get("error"):
                        raise ProviderError("Gate subscription error", code="subscription_error", provider=self.provider_name)
                    self.last_message_at = datetime.now(timezone.utc)
                    channel = str(message.get("channel") or "")
                    result = message.get("result")
                    if message.get("event") == "subscribe":
                        continue
                    if channel == "futures.candlesticks" and isinstance(result, list):
                        # Gate returns the interval in ``n`` (e.g. 15m_BTC_USDT).
                        for row in result:
                            if not isinstance(row, dict):
                                continue
                            name = str(row.get("n") or "")
                            tf = next((candidate for candidate in self.timeframes if name.startswith(f"{candidate}_")), None)
                            if tf is None:
                                continue
                            parsed = self._bar_from_update(row, tf)
                            if parsed and parsed[0] in mapping:
                                if tf != "15m":
                                    emit("candle", {"symbol": mapping[parsed[0]], "native_symbol": parsed[0], "timeframe": tf, "bar": parsed[1], "closed": parsed[2], "environment": "LIVE_PUBLIC"})
                                # The legacy callback owns the signal 15m
                                # path.  Other bars are delivered through
                                # on_event so they cannot be accidentally
                                # persisted as 15m data.
                                if tf == "15m":
                                    on_bar(mapping[parsed[0]], parsed[1], parsed[2])
                    elif channel == "futures.order_book_update":
                        rows = result if isinstance(result, list) else [result]
                        for row in rows:
                            if isinstance(row, dict):
                                contract = str(row.get("s") or row.get("contract") or row.get("symbol") or "").upper()
                                if contract in mapping:
                                    self._handle_order_book(contract, row, on_event=emit)
                    elif channel == "futures.trades":
                        rows = result if isinstance(result, list) else [result]
                        for row in rows:
                            if isinstance(row, dict):
                                contract = str(row.get("contract") or row.get("s") or "").upper()
                                if contract in mapping:
                                    emit("trade", {"symbol": mapping[contract], "provider": "gate", "environment": "LIVE_PUBLIC", "native_symbol": contract, "trade_id": row.get("id"), "event_at": row.get("create_time_ms") or row.get("create_time"), "price": row.get("price"), "size": row.get("size"), "side": "BUY" if float(row.get("size") or 0) > 0 else "SELL", "raw": row})
                    elif channel == "futures.public_liquidates":
                        rows = result if isinstance(result, list) else [result]
                        for row in rows:
                            if isinstance(row, dict):
                                event_contract = str(row.get("contract") or row.get("s") or row.get("symbol") or "").upper()
                                if event_contract not in mapping:
                                    continue
                                emit("liquidation", {"provider": "gate", "environment": "LIVE_PUBLIC", "native_symbol": event_contract, "symbol": mapping[event_contract], "event_at": row.get("time") or row.get("create_time"), "raw": row})
            except Exception as exc:
                self.last_error = getattr(exc, "code", type(exc).__name__)
                self.reconnect_count += 1
                if on_state:
                    on_state(self.state("degraded"))
                if self.stop_event.is_set():
                    break
                time.sleep(min(delay, max_reconnect_delay))
                delay = min(max_reconnect_delay, delay * 2.0)
            finally:
                if websocket is not None:
                    try:
                        websocket.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                with self._socket_lock:
                    if self._active_socket is websocket:
                        self._active_socket = None
        if on_state:
            on_state(self.state("stopped"))


class GateRealtimeStream(GateNativeRealtimeStream):
    """Compatibility name used by the existing monitoring runtime."""


__all__ = ["GateNativeRealtimeStream", "GateRealtimeStream"]
