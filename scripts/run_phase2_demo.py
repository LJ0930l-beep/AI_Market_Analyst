#!/usr/bin/env python3
"""Run the Phase 2 vertical slice with fixture, real, or auto data routing."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ai import MockLLMProvider, OllamaProvider
from core.analysis_service import AnalysisError, AnalysisService
from core.instruments import instrument_for, phase1_universe
from core.news_engine import RSSNewsProvider
from core.outcomes import settle_prediction
from core.providers import Bar, FixtureNewsProvider
from core.storage import SQLiteStore


def _future_bar(result):
    signal = result.signal
    price = result.bundle.quote.price
    if signal.action.value == "LONG" and signal.tp1 is not None and signal.stop is not None:
        high = signal.tp1 * 1.001
        low = max(signal.stop * 1.001, price * 0.995)
    elif signal.action.value == "SHORT" and signal.tp1 is not None and signal.stop is not None:
        high = min(signal.stop * 0.999, price * 1.005)
        low = signal.tp1 * 0.999
    else:
        high = price * 1.002
        low = price * 0.998
    return Bar(signal.generated_at + timedelta(hours=1), price, high, low, price, 0.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "real", "auto"), default="fixture")
    parser.add_argument("--llm", choices=("mock", "ollama", "off"), default="mock")
    parser.add_argument("--news", choices=("fixture", "rss"), default="fixture")
    parser.add_argument("--timeframe", choices=("5m", "15m", "1h", "4h", "1d"), default="1h")
    parser.add_argument("--symbols", nargs="+", default=[item.symbol for item in phase1_universe()])
    parser.add_argument("--db", default="data/phase2-demo.sqlite3")
    parser.add_argument("--follow", action="store_true", help="disabled: demo signals cannot create PaperTrade rows")
    parser.add_argument("--settle", action="store_true", help="disabled: demo runs cannot create synthetic Outcome rows")
    args = parser.parse_args(argv)

    if args.follow or args.settle:
        parser.error("Phase 2 is analysis-only; --follow and --settle are disabled so demo rows cannot create PaperTrade or Outcome history")

    os.environ["MARKET_DATA_MODE"] = args.mode
    os.environ["NEWS_MODE"] = "fixture" if args.news == "fixture" else "real"
    llm = None if args.llm == "off" else MockLLMProvider() if args.llm == "mock" else OllamaProvider()
    news = FixtureNewsProvider() if args.news == "fixture" else RSSNewsProvider()
    store = SQLiteStore(args.db)
    service = AnalysisService(news_provider=news, llm_provider=llm, store=store)
    output: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for symbol in args.symbols:
        try:
            result = service.analyze(instrument_for(symbol), timeframe=args.timeframe, limit=120, source_type="demo")
        except (ValueError, AnalysisError) as exc:
            failures.append({"symbol": symbol, "error_code": getattr(exc, "code", "analysis_error"), "error": str(exc)})
            continue
        signal = result.signal
        followed = False
        outcome = None
        if args.follow and signal.action.value != "WAIT":
            store.follow_prediction(signal.prediction_id, signal.generated_at.isoformat())
            followed = True
        if args.settle:
            if signal.action.value == "WAIT":
                outcome = settle_prediction(signal, [])
            else:
                outcome = settle_prediction(signal, [_future_bar(result)])
            store.save_outcome(outcome)
        output.append(
            {
                "symbol": symbol,
                "provider": result.bundle.snapshot.provider,
                "stale": result.bundle.snapshot.stale,
                "data_as_of": result.data_as_of.isoformat(),
                "response_time": result.response_time.isoformat(),
                "quant": result.context.quant.to_dict(),
                "news_count": len(result.news.events),
                "news_available": result.news.available,
                "model": result.model_status,
                "action": signal.action.value,
                "confidence": signal.raw_confidence,
                "prediction_id": signal.prediction_id,
                "entry": [signal.entry_low, signal.entry_high],
                "stop": signal.stop,
                "tp1": signal.tp1,
                "tp2": signal.tp2,
                "signal_valid_until": signal.signal_valid_until.isoformat(),
                "max_hold_until": signal.max_hold_until.isoformat(),
                "followed": followed,
                "outcome": outcome.to_dict() if outcome else None,
            }
        )
    model_provenance = {
        "mock": "mock_simulation",
        "ollama": "local_provider_demo",
        "off": "model_disabled",
    }[args.llm]
    print(json.dumps({
        "phase": 2,
        "mode": args.mode,
        "llm": args.llm,
        "model_provenance": model_provenance,
        "source_type": "demo",
        "formal_acceptance_eligible": False,
        "results": output,
        "failures": failures,
        "counts": store.counts(),
    }, indent=2))
    return 0 if not failures or args.mode == "fixture" else 2


if __name__ == "__main__":
    raise SystemExit(main())
