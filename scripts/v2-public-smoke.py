"""Real public-source evidence. No fixture, account, order, or installed-UI claims."""

import json
import threading
import time
from datetime import datetime, timezone
from core.providers.gateio_provider import GatePublicProvider
from core.gate_stream import GateRealtimeStream


def main():
    provider = GatePublicProvider()
    provider.market("BTCUSDT")
    markets = [
        m
        for m in provider.exchange.markets.values()
        if m.get("swap")
        and m.get("active") is True
        and m.get("linear")
        and m.get("settle") == "USDT"
    ]
    symbols = list(
        dict.fromkeys(
            ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
            + [m["base"] + m["quote"] for m in markets]
        )
    )
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "source": "Gate public WS via validated CCXT metadata",
        "mode": "read-only live",
        "resource": [],
    }
    for count in (20, 50):
        seen = set()
        messages = []
        states = []
        stream = GateRealtimeStream(symbols[:count], provider)

        def bar(symbol, bar, closed):
            seen.add(symbol)
            if len(messages) < 3:
                messages.append(
                    {
                        "symbol": symbol,
                        "bar_at": bar.timestamp.isoformat(),
                        "closed": closed,
                        "close": bar.close,
                    }
                )

        worker = threading.Thread(
            target=stream.run_forever,
            kwargs={
                "on_bar": bar,
                "on_state": lambda state: states.append(state.to_dict()),
            },
            daemon=True,
        )
        started = time.monotonic()
        worker.start()
        deadline = started + 25
        while time.monotonic() < deadline and len(seen) < count:
            time.sleep(0.25)
        stream.stop()
        worker.join(12)
        report["resource"].append(
            {
                "requested_symbols": count,
                "acknowledged_subscriptions": stream.subscription_ack_count,
                "observed_symbols": sorted(seen),
                "observed_count": len(seen),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "owned_stream_threads": 1,
                "stopped": not worker.is_alive(),
                "state": states[-1] if states else None,
                "sample": messages,
            }
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
