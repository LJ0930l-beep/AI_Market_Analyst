"""Bounded Binance public WebSocket ingestion with reconnect and REST backfill."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .providers.base import Bar, ProviderError
from .providers.binance import BinancePublicProvider


@dataclass(frozen=True, slots=True)
class RealtimeConnectionState:
    status: str
    provider: str
    symbols: tuple[str, ...]
    timeframe: str
    reconnect_count: int
    last_message_at: datetime | None
    last_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "reconnect_count": self.reconnect_count,
            "last_message_at": self.last_message_at.astimezone(timezone.utc).isoformat() if self.last_message_at else None,
            "last_error": self.last_error,
            "public_only": True,
        }


class BinanceRealtimeStream:
    """A caller-owned WebSocket loop; construction and connect are explicit."""

    provider_name = "binance_public_ws"

    def __init__(
        self,
        *,
        symbols: Iterable[str],
        timeframe: str = "15m",
        base_url: str | None = None,
        max_symbols: int = 50,
        websocket_factory: Callable[[str, float], object] | None = None,
        rest_provider: BinancePublicProvider | None = None,
        backfill_limit: int = 240,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized = tuple(dict.fromkeys(str(symbol).strip().lower() for symbol in symbols if str(symbol).strip()))
        if not normalized or len(normalized) > max(1, min(int(max_symbols), 50)):
            raise ValueError("WebSocket symbols must contain between 1 and 50 symbols")
        if timeframe not in {"15m", "1h"}:
            raise ValueError("WebSocket timeframe must be 15m or 1h")
        self.symbols = normalized
        self.timeframe = timeframe
        self.base_url = (base_url or os.environ.get("BINANCE_WS_URL", "wss://data-stream.binance.vision")).rstrip("/")
        self.websocket_factory = websocket_factory or self._default_websocket
        self.rest_provider = rest_provider
        self.backfill_limit = max(1, min(int(backfill_limit), 1000))
        self.sleep = sleep
        self.stop_event = threading.Event()
        self._socket_lock = threading.Lock()
        self._active_socket: object | None = None
        self.reconnect_count = 0
        self.last_message_at: datetime | None = None
        self.last_error: str | None = None

    @staticmethod
    def _default_websocket(url: str, timeout: float) -> object:
        try:
            import websocket  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dependency is optional in stdlib-only mode
            raise ProviderError("websocket-client is not installed", code="websocket_dependency_missing", provider="binance_public_ws") from exc
        return websocket.create_connection(url, timeout=timeout, origin="https://www.binance.com")

    @property
    def url(self) -> str:
        streams = "/".join(f"{symbol}@kline_{self.timeframe}" for symbol in self.symbols)
        # Binance's combined stream endpoint is public and contains no account
        # or credential material.
        return f"{self.base_url}/stream?streams={streams}"

    def state(self, status: str = "idle") -> RealtimeConnectionState:
        return RealtimeConnectionState(status, self.provider_name, tuple(symbol.upper() for symbol in self.symbols), self.timeframe, self.reconnect_count, self.last_message_at, self.last_error)

    def stop(self) -> None:
        self.stop_event.set()
        with self._socket_lock:
            websocket = self._active_socket
        if websocket is not None:
            try:
                websocket.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    def backfill(self, provider: BinancePublicProvider | None = None, *, limit: int = 240) -> dict[str, list[Bar]]:
        """Fetch REST history after a disconnect so the next close is not lost."""

        selected = provider or self.rest_provider or BinancePublicProvider()
        from .instruments import instrument_for

        result: dict[str, list[Bar]] = {}
        for symbol in self.symbols:
            instrument = instrument_for(symbol)
            result[instrument.symbol] = selected.get_bars(instrument, self.timeframe, limit=limit)
        return result

    @staticmethod
    def _bar_from_message(message: dict[str, Any]) -> tuple[str, Bar, bool] | None:
        payload = message.get("data") if isinstance(message.get("data"), dict) else message
        kline = payload.get("k") if isinstance(payload, dict) else None
        if not isinstance(kline, dict):
            return None
        symbol = str(kline.get("s") or "").upper()
        try:
            closed = kline.get("x") in (True, 1, "1", "true", "TRUE")
            timestamp = datetime.fromtimestamp(float(kline["t"]) / 1000.0, tz=timezone.utc)
            bar_end = datetime.fromtimestamp(float(kline["T"]) / 1000.0, tz=timezone.utc) if kline.get("T") is not None else None
            event_at = datetime.fromtimestamp(float(message.get("E")) / 1000.0, tz=timezone.utc) if message.get("E") is not None else datetime.now(timezone.utc)
            bar = Bar(
                timestamp,
                float(kline["o"]),
                float(kline["h"]),
                float(kline["l"]),
                float(kline["c"]),
                float(kline["v"]),
                bar_end=bar_end,
                event_at=event_at,
                sequence=kline.get("n") or message.get("E"),
                is_closed=closed,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Binance WebSocket returned a malformed kline", code="websocket_invalid_payload", provider="binance_public_ws") from exc
        return symbol, bar, closed

    def run_forever(
        self,
        *,
        on_bar: Callable[[str, Bar, bool], None],
        on_state: Callable[[RealtimeConnectionState], None] | None = None,
        timeout: float = 30.0,
        max_reconnect_delay: float = 30.0,
    ) -> None:
        """Run until ``stop`` is called; reconnects are bounded and observable."""

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
                delay = 1.0
                connected = True
                if on_state:
                    on_state(self.state("connected"))
                while not self.stop_event.is_set():
                    raw = websocket.recv()  # type: ignore[attr-defined]
                    if raw is None:
                        raise ProviderError("Binance WebSocket closed", code="websocket_closed", provider=self.provider_name)
                    message = json.loads(raw) if isinstance(raw, str) else json.loads(bytes(raw).decode("utf-8"))
                    parsed = self._bar_from_message(message)
                    if parsed is None:
                        continue
                    symbol, bar, is_closed = parsed
                    self.last_message_at = datetime.now(timezone.utc)
                    self.last_error = None
                    on_bar(symbol, bar, is_closed)
            except Exception as exc:
                self.last_error = getattr(exc, "code", str(exc))
                self.reconnect_count += 1
                if on_state:
                    on_state(self.state("degraded"))
                if self.stop_event.is_set():
                    break
                if connected:
                    if on_state:
                        on_state(self.state("backfilling"))
                    try:
                        for symbol, bars in self.backfill(limit=self.backfill_limit).items():
                            for bar in bars:
                                if self.stop_event.is_set():
                                    break
                                # REST backfill rows are closed history. The
                                # downstream ledger/store remains idempotent
                                # if a row was also received before disconnect.
                                on_bar(symbol, bar, True)
                    except Exception as backfill_error:
                        self.last_error = getattr(backfill_error, "code", str(backfill_error))
                        if on_state:
                            on_state(self.state("degraded"))
                self.sleep(min(delay, max_reconnect_delay))
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


__all__ = ["BinanceRealtimeStream", "RealtimeConnectionState"]
