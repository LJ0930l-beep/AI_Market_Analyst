#!/usr/bin/env python3
"""Run the offline Phase 1 vertical slice for US equities and Crypto."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.instruments import phase1_universe
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.signals import build_signal
from core.storage import SQLiteStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/market_analyst.sqlite3", help="SQLite path")
    parser.add_argument("--follow", action="store_true", help="create PaperTrade rows for actionable signals")
    args = parser.parse_args(argv)

    provider = FixtureProvider()
    store = SQLiteStore(args.db)
    store.initialize()
    output: list[dict[str, object]] = []
    for instrument in phase1_universe():
        bars = provider.get_bars(instrument, "1h", limit=120)
        quote = provider.get_quote(instrument)
        quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
        signal = build_signal(instrument, quant, timeframe="1h", generated_at=quote.timestamp)
        store.save_instrument(instrument)
        store.save_snapshot(instrument.symbol, "1h", quote.timestamp.isoformat(), quant.to_dict())
        store.save_prediction(signal)
        if args.follow and signal.action.value != "WAIT":
            store.follow_prediction(signal.prediction_id, quote.timestamp.astimezone(timezone.utc).isoformat())
        output.append(
            {
                "symbol": instrument.symbol,
                "action": signal.action.value,
                "confidence": signal.raw_confidence,
                "prediction_id": signal.prediction_id,
                "model_id": signal.model_id,
                "entry": [signal.entry_low, signal.entry_high],
                "stop": signal.stop,
                "tp1": signal.tp1,
                "tp2": signal.tp2,
            }
        )
    print(json.dumps({"phase": 1, "predictions": output, "counts": store.counts()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
