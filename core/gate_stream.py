"""One public Gate socket for the currently enabled subscription set."""

import json
import threading
import time
from datetime import datetime, timezone

from .providers.base import Bar
from .providers.gateio_provider import GatePublicProvider
from .realtime import RealtimeConnectionState


class GateRealtimeStream:
    provider_name = "gate_public_ws"

    def __init__(self, symbols, provider=None):
        self.symbols = tuple(dict.fromkeys(symbols))
        if not 1 <= len(self.symbols) <= 50:
            raise ValueError("1..50 symbols required")
        self.provider = provider or GatePublicProvider()
        self._stop = threading.Event()
        self._socket = None
        self.subscription_ack_count = 0

    def stop(self):
        self._stop.set()
        if self._socket:
            self._socket.close()

    def run_forever(self, *, on_bar, on_state=None):
        import websocket

        retries = 0
        while not self._stop.is_set():
            try:
                self.subscription_ack_count = 0
                mapping = {self.provider.market(s)["id"]: s for s in self.symbols}
                if self._stop.is_set():
                    break
                self._socket = websocket.create_connection(
                    "wss://fx-ws.gateio.ws/v4/ws/usdt",
                    timeout=10,
                    header=["X-Gate-Size-Decimal: 1"],
                )
                for contract in mapping:
                    self._socket.send(
                        json.dumps(
                            {
                                "time": int(time.time()),
                                "channel": "futures.candlesticks",
                                "event": "subscribe",
                                "payload": ["15m", contract],
                            }
                        )
                    )
                if on_state:
                    on_state(
                        RealtimeConnectionState(
                            "connecting",
                            self.provider_name,
                            self.symbols,
                            "15m",
                            retries,
                            None,
                        )
                    )
                while not self._stop.is_set():
                    raw = self._socket.recv()
                    if self._stop.is_set():
                        break
                    if not raw:
                        raise ConnectionError("public socket closed")
                    data = json.loads(raw)
                    if data.get("error"):
                        raise ValueError("Gate subscription error")
                    if (
                        data.get("event") == "subscribe"
                        and data.get("result", {}).get("status") == "success"
                    ):
                        self.subscription_ack_count += 1
                        if (
                            self.subscription_ack_count == len(self.symbols)
                            and on_state
                        ):
                            on_state(
                                RealtimeConnectionState(
                                    "connected",
                                    self.provider_name,
                                    self.symbols,
                                    "15m",
                                    retries,
                                    datetime.now(timezone.utc),
                                )
                            )
                    if data.get("event") != "update":
                        continue
                    for row in data.get("result", []):
                        contract = row.get("n", "").removeprefix("15m_")
                        if contract not in mapping:
                            continue
                        bar = Bar(
                            datetime.fromtimestamp(int(row["t"]), timezone.utc),
                            *[float(row[k]) for k in ("o", "h", "l", "c", "v")],
                        )
                        on_bar(mapping[contract], bar, row.get("w") is True)
            except Exception as exc:
                if self._stop.is_set():
                    break
                retries += 1
                if on_state:
                    on_state(
                        RealtimeConnectionState(
                            "degraded",
                            self.provider_name,
                            self.symbols,
                            "15m",
                            retries,
                            None,
                            type(exc).__name__,
                        )
                    )
                if self._stop.wait(min(60, 2 ** min(retries, 6))):
                    break
            finally:
                if self._socket:
                    self._socket.close()
                self._socket = None
