#!/usr/bin/env python3
"""Run a real-data historical one-bar paper-trade replay for NVDA and BTCUSDT."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ai import OllamaProvider, SignalPolicy, analyze_with_repair, to_signal_proposal
from core.instruments import instrument_for
from core.model_client import model_client
from core.model_routing import DEFAULT_SMART_MODEL
from core.news_engine import NewsEngine, RSSNewsProvider
from core.outcomes import settle_prediction
from core.providers import Bar, Quote
from core.providers.runtime import ProviderSnapshot, build_default_provider, fetch_market_data
from core.quant import build_quant_snapshot
from core.storage import SQLiteStore
from core.context import MarketContext
from core.time_rules import build_time_policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["NVDA", "BTCUSDT"])
    parser.add_argument("--db", default="data/phase2-real-paper.sqlite3")
    parser.add_argument("--output", default="data/phase2-real-paper.json")
    parser.add_argument("--timeframe", default="1h")
    args = parser.parse_args(argv)

    os.environ["MARKET_DATA_MODE"] = "real"
    os.environ["DISABLE_FIXTURE_FALLBACK"] = "1"
    os.environ["NEWS_MODE"] = "real"
    base_url = model_client.base_url
    model_name = DEFAULT_SMART_MODEL
    llm = OllamaProvider(base_url=base_url, model_name=model_name, timeout=180, retries=0)
    news_provider = RSSNewsProvider(timeout=15, retries=1)
    store = SQLiteStore(args.db)
    store.initialize()
    results: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []

    for symbol in args.symbols:
        try:
            instrument = instrument_for(symbol)
            provider = build_default_provider(instrument)
            bundle = fetch_market_data(provider, instrument, args.timeframe, limit=121)
            if len(bundle.bars) < 61:
                raise ValueError("real provider returned too few bars for historical replay")
            ordered_bars = tuple(sorted(bundle.bars, key=lambda item: item.timestamp))
            analysis_bars = ordered_bars[:-1]
            future_bar = ordered_bars[-1]
            anchor = analysis_bars[-1]
            previous = analysis_bars[-2]
            quote = Quote(
                instrument=instrument,
                timestamp=anchor.timestamp,
                price=anchor.close,
                volume=anchor.volume,
                change_pct=(anchor.close / previous.close - 1.0) * 100.0 if previous.close else None,
                high=anchor.high,
                low=anchor.low,
            )
            snapshot = ProviderSnapshot(
                provider=bundle.snapshot.provider,
                fetched_at=bundle.snapshot.fetched_at,
                data_as_of=anchor.timestamp,
                stale=bundle.snapshot.stale,
                error_code=bundle.snapshot.error_code,
            )
            quant = build_quant_snapshot(list(analysis_bars), args.timeframe, symbol=instrument.symbol)
            news = NewsEngine(news_provider).collect(instrument, limit=10)
            policy = build_time_policy(
                args.timeframe,
                price=quant.price,
                atr14=quant.atr14,
                market_regime=quant.market_regime,
                events=news.events,
                now=anchor.timestamp,
            )
            context = MarketContext(
                instrument=instrument,
                quote=quote,
                bars=analysis_bars,
                quant=quant,
                news=news.events,
                time_policy=policy,
                provider_snapshot=snapshot,
                market_context={"news_available": news.available, "news_provider": news.provider},
            )
            response, metadata = analyze_with_repair(llm, context, SignalPolicy.from_context(context))
            signal = to_signal_proposal(response, context, SignalPolicy.from_context(context), metadata, generated_at=anchor.timestamp)
            store.save_instrument(instrument)
            store.save_snapshot(instrument.symbol, args.timeframe, anchor.timestamp.isoformat(), context.to_dict())
            store.save_provider_snapshot(instrument.symbol, snapshot)
            store.save_news_events(instrument.symbol, news)
            store.save_prediction(signal)
            store.save_model_run(
                prediction_id=signal.prediction_id,
                model_id=metadata.model_id,
                started_at=anchor.timestamp.isoformat(),
                latency_ms=metadata.latency_ms,
                input_tokens_est=metadata.input_tokens_est,
                output_chars=metadata.output_chars,
                success=True,
            )
            outcome = None
            followed = False
            if signal.action.value != "WAIT":
                store.follow_prediction(signal.prediction_id, signal.generated_at.isoformat())
                followed = True
                outcome = settle_prediction(signal, [future_bar])
                store.save_outcome(outcome)
            results.append(
                {
                    "symbol": instrument.symbol,
                    "provider": snapshot.to_dict(),
                    "analysis_anchor": anchor.timestamp.astimezone(timezone.utc).isoformat(),
                    "future_bar": {"timestamp": future_bar.timestamp.astimezone(timezone.utc).isoformat(), "open": future_bar.open, "high": future_bar.high, "low": future_bar.low, "close": future_bar.close, "volume": future_bar.volume},
                    "quant": quant.to_dict(),
                    "news": news.to_dict(),
                    "model": metadata.to_dict(),
                    "signal": signal.to_dict(),
                    "paper_trade_followed": followed,
                    "outcome": outcome.to_dict() if outcome else None,
                }
            )
        except Exception as exc:  # pragma: no cover - network/model dependent
            failures.append({"symbol": symbol, "error": str(exc), "error_type": type(exc).__name__})

    report = {"phase": 2, "mode": "real_historical_replay", "model": model_name, "results": results, "failures": failures, "counts": store.counts()}
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    actionable_followed = any(item.get("paper_trade_followed") for item in results)
    has_outcome = any(item.get("outcome") is not None for item in results)
    return 0 if not failures and actionable_followed and has_outcome else 2


if __name__ == "__main__":
    raise SystemExit(main())
