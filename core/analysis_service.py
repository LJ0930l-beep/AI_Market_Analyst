"""Phase 2 orchestration: Provider -> News -> Quant -> Context -> Model -> Prediction."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .ai import OllamaProvider, SignalPolicy, analyze_with_repair, to_signal_proposal
from .ai.contracts import LLMError
from .benchmarks import BenchmarkContext, BenchmarkContextService
from .context import MarketContext
from .events import EventIntelligenceResult, EventIntelligenceService, NewsEventProviderAdapter
from .instruments import Instrument
from .memory import MarketMemoryContext, MarketMemoryService
from .news_engine import NewsFetchResult, RSSNewsProvider
from .providers import FixtureNewsProvider, ProviderError, ProviderChain, Quote
from .providers.runtime import MarketDataBundle, ProviderSnapshot, build_default_provider, fetch_market_data
from .quant import QuantSnapshot, build_quant_snapshot
from .signals import Action, SignalProposal, build_signal
from .storage import SQLiteStore
from .time_rules import build_time_policy


class AnalysisError(RuntimeError):
    def __init__(self, message: str, *, code: str, provider: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.provider = provider


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    instrument: Instrument
    timeframe: str
    response_time: datetime
    bundle: MarketDataBundle
    news: NewsFetchResult
    context: MarketContext
    signal: SignalProposal
    model_status: dict[str, object]
    benchmark_context: BenchmarkContext | None = None
    event_context: EventIntelligenceResult | None = None
    memory_context: MarketMemoryContext | None = None

    @property
    def data_as_of(self) -> datetime:
        return self.bundle.data_as_of

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument": {
                "symbol": self.instrument.symbol,
                "asset_type": self.instrument.asset_type.value,
                "exchange": self.instrument.exchange,
                "quote_currency": self.instrument.quote_currency,
            },
            "timeframe": self.timeframe,
            "response_time": self.response_time.astimezone(timezone.utc).isoformat(),
            "data_as_of": self.data_as_of.astimezone(timezone.utc).isoformat(),
            "provider_snapshot": self.bundle.snapshot.to_dict(),
            "quote": {
                "timestamp": self.bundle.quote.timestamp.astimezone(timezone.utc).isoformat(),
                "price": self.bundle.quote.price,
                "change_pct": self.bundle.quote.change_pct,
                "high": self.bundle.quote.high,
                "low": self.bundle.quote.low,
            },
            "quant": self.context.quant.to_dict(),
            "news": self.news.to_dict(),
            "benchmark_context": self.benchmark_context.to_dict() if self.benchmark_context else None,
            "events": self.event_context.to_dict() if self.event_context else None,
            "market_memory": self.memory_context.to_dict() if self.memory_context else None,
            "time_policy": self.context.time_policy.to_dict() if self.context.time_policy else None,
            "model": self.model_status,
            "signal": self.signal.to_dict(),
            "input_hash": self.context.input_hash(),
        }


@dataclass(frozen=True, slots=True)
class MarketSnapshotResult:
    """Read-only provider and deterministic quant output for Asset Detail."""

    instrument: Instrument
    timeframe: str
    response_time: datetime
    bundle: MarketDataBundle
    quant: QuantSnapshot

    @property
    def data_as_of(self) -> datetime:
        return self.bundle.data_as_of

    @staticmethod
    def _utc_iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, object]:
        quote = self.bundle.quote
        return {
            "symbol": self.instrument.symbol,
            "timeframe": self.timeframe,
            "response_time": self._utc_iso(self.response_time),
            "data_as_of": self._utc_iso(self.data_as_of),
            "provider_snapshot": self.bundle.snapshot.to_dict(),
            "quote": {
                "timestamp": self._utc_iso(quote.timestamp),
                "price": quote.price,
                "change_pct": quote.change_pct,
                "high": quote.high,
                "low": quote.low,
            },
            "quant": self.quant.to_dict(),
            "time_policy": None,
            "bars": [
                {
                    "timestamp": self._utc_iso(bar.timestamp),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                }
                for bar in self.bundle.bars
            ],
        }


@dataclass(frozen=True, slots=True)
class MarketContextSnapshotResult:
    """Read-only deterministic Phase 6 context for Asset Detail and API reads."""

    instrument: Instrument
    timeframe: str
    response_time: datetime
    bundle: MarketDataBundle
    quant: QuantSnapshot
    benchmark_context: BenchmarkContext
    event_context: EventIntelligenceResult
    memory_context: MarketMemoryContext
    time_policy: object

    def to_dict(self) -> dict[str, object]:
        quote = self.bundle.quote
        return {
            "symbol": self.instrument.symbol,
            "timeframe": self.timeframe,
            "response_time": self.response_time.astimezone(timezone.utc).isoformat(),
            "data_as_of": self.bundle.data_as_of.astimezone(timezone.utc).isoformat(),
            "provider_snapshot": self.bundle.snapshot.to_dict(),
            "quote": {
                "timestamp": quote.timestamp.astimezone(timezone.utc).isoformat(),
                "price": quote.price,
                "change_pct": quote.change_pct,
                "high": quote.high,
                "low": quote.low,
            },
            "quant": self.quant.to_dict(),
            "benchmark_context": self.benchmark_context.to_dict(),
            "events": self.event_context.to_dict(),
            "market_memory": self.memory_context.to_dict(),
            "time_policy": self.time_policy.to_dict() if hasattr(self.time_policy, "to_dict") else None,
            "provenance": {
                "computed_by": "python_deterministic",
                "read_only": True,
                "prediction_created": False,
                "outcome_created": False,
                "paper_trade_created": False,
                "alert_created": False,
                "memory_materialized": False,
            },
        }


class AnalysisService:
    """One bounded analysis run with explicit provider/model state."""

    def __init__(
        self,
        *,
        market_provider_factory: Callable[[Instrument], object] | None = None,
        news_provider: object | None = None,
        event_provider: object | None = None,
        llm_provider: object | None = None,
        store: SQLiteStore | None = None,
        benchmark_provider_factory: Callable[[Instrument], object] | None = None,
    ) -> None:
        self.market_provider_factory = market_provider_factory or build_default_provider
        if news_provider is None:
            news_provider = FixtureNewsProvider() if os.environ.get("NEWS_MODE", "real").lower() == "fixture" else RSSNewsProvider()
        self.news_provider = news_provider
        self.event_provider = event_provider or NewsEventProviderAdapter(news_provider)
        self.llm_provider = llm_provider
        self.store = store
        if self.store is not None:
            self.store.initialize()
        self.event_service = EventIntelligenceService(self.event_provider)
        self.benchmark_service = BenchmarkContextService(provider_factory=benchmark_provider_factory or self.market_provider_factory, store=store)
        self.memory_service = MarketMemoryService(store) if store is not None else MarketMemoryService(None)

    def _bundle(self, instrument: Instrument, timeframe: str, limit: int) -> MarketDataBundle:
        provider = self.market_provider_factory(instrument)
        try:
            if isinstance(provider, ProviderChain) or hasattr(provider, "get_bundle"):
                return provider.get_bundle(instrument, timeframe, limit)  # type: ignore[attr-defined]
            return fetch_market_data(provider, instrument, timeframe, limit)  # type: ignore[arg-type]
        except ProviderError as exc:
            raise AnalysisError(str(exc), code=exc.code, provider=exc.provider) from exc
        except Exception as exc:
            provider_name = str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))
            raise AnalysisError(str(exc), code="provider_error", provider=provider_name) from exc

    def market_snapshot(
        self,
        instrument: Instrument,
        *,
        timeframe: str = "1h",
        limit: int = 120,
        snapshot_time: datetime | None = None,
    ) -> MarketSnapshotResult:
        """Fetch market data and compute quant without invoking or persisting analysis."""

        response_time = (snapshot_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        bundle = self._bundle(instrument, timeframe, limit)
        ordered_bars = tuple(sorted(bundle.bars, key=lambda bar: bar.timestamp))[-limit:]
        snapshot_bundle = MarketDataBundle(
            quote=bundle.quote,
            bars=ordered_bars,
            snapshot=bundle.snapshot,
        )
        try:
            quant = build_quant_snapshot(list(ordered_bars), timeframe, symbol=instrument.symbol)
        except ValueError as exc:
            raise AnalysisError(
                str(exc),
                code="quant_error",
                provider=bundle.snapshot.provider,
            ) from exc
        return MarketSnapshotResult(instrument, timeframe, response_time, snapshot_bundle, quant)

    def _phase6_context(
        self,
        instrument: Instrument,
        *,
        timeframe: str,
        bundle: MarketDataBundle,
        quant: QuantSnapshot,
        response_time: datetime,
        persist_mapping: bool,
        context_as_of: datetime | None = None,
    ) -> tuple[EventIntelligenceResult, NewsFetchResult, BenchmarkContext, MarketMemoryContext, object]:
        cutoff = (context_as_of or bundle.data_as_of).astimezone(timezone.utc)
        event_context = self.event_service.collect(instrument, as_of=cutoff, limit=20)
        news = event_context.to_news_result()
        benchmark_context = self.benchmark_service.build(
            instrument,
            target_bars=bundle.bars,
            target_quant=quant,
            timeframe=timeframe,
            as_of=cutoff,
            target_provider=bundle.snapshot.provider,
            persist_mapping=persist_mapping,
        )
        query_context = {
            "quant": quant.to_dict(),
            "market_context": {
                "benchmark_context": benchmark_context.to_dict(),
                "event_intelligence": event_context.to_dict(),
            },
            "risk_events": [event.to_dict() for event in event_context.events if event.importance >= 70],
        }
        query_prediction = {
            "symbol": instrument.symbol,
            "instrument": {
                "symbol": instrument.symbol,
                "asset_type": instrument.asset_type.value,
            },
            "analysis_timeframe": timeframe,
            "action": "WAIT",
            "context_json": json.dumps(query_context, sort_keys=True),
        }
        memory_context = self.memory_service.query(
            symbol=instrument.symbol,
            timeframe=timeframe,
            as_of=cutoff,
            query_prediction=query_prediction,
            top_k=5,
        )
        policy = build_time_policy(
            timeframe,
            price=quant.price,
            atr14=quant.atr14,
            market_regime=quant.market_regime,
            events=news.events,
            event_evidence=event_context.events,
            now=response_time,
        )
        return event_context, news, benchmark_context, memory_context, policy

    def market_context_snapshot(
        self,
        instrument: Instrument,
        *,
        timeframe: str = "1h",
        limit: int = 120,
        snapshot_time: datetime | None = None,
        as_of: datetime | None = None,
    ) -> MarketContextSnapshotResult:
        """Read-only Phase 6 context; never persists evidence or invokes a model."""

        response_time = (snapshot_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        source_bundle = self._bundle(instrument, timeframe, limit)
        cutoff = (as_of or source_bundle.data_as_of).astimezone(timezone.utc)
        bounded_bars = tuple(sorted((bar for bar in source_bundle.bars if bar.timestamp.astimezone(timezone.utc) <= cutoff), key=lambda bar: bar.timestamp))
        if len(bounded_bars) < 60:
            raise AnalysisError("not enough bars known by requested as_of", code="context_history_unavailable", provider=source_bundle.snapshot.provider)
        snapshot = source_bundle.snapshot
        if bounded_bars[-1].timestamp != source_bundle.data_as_of:
            from .providers.runtime import ProviderSnapshot

            snapshot = ProviderSnapshot(
                provider=snapshot.provider,
                fetched_at=snapshot.fetched_at,
                data_as_of=bounded_bars[-1].timestamp,
                stale=snapshot.stale,
                error_code=snapshot.error_code,
            )
        quote = source_bundle.quote
        if quote.timestamp.astimezone(timezone.utc) > cutoff:
            last = bounded_bars[-1]
            previous = bounded_bars[-2].close if len(bounded_bars) > 1 else None
            quote = Quote(
                instrument=instrument,
                timestamp=last.timestamp,
                price=last.close,
                volume=last.volume,
                change_pct=((last.close / previous) - 1.0) * 100.0 if previous else None,
                high=last.high,
                low=last.low,
            )
        bundle = MarketDataBundle(quote=quote, bars=bounded_bars, snapshot=snapshot)
        quant = build_quant_snapshot(list(bundle.bars), timeframe, symbol=instrument.symbol)
        event_context, _news, benchmark_context, memory_context, policy = self._phase6_context(
            instrument,
            timeframe=timeframe,
            bundle=bundle,
            quant=quant,
            response_time=response_time,
            persist_mapping=False,
            context_as_of=cutoff,
        )
        return MarketContextSnapshotResult(
            instrument,
            timeframe,
            response_time,
            bundle,
            quant,
            benchmark_context,
            event_context,
            memory_context,
            policy,
        )

    @staticmethod
    def _risk_events(news: NewsFetchResult) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "event_id": event.event_id,
                "title": event.title,
                "importance": event.importance,
                "sentiment": event.sentiment,
                "impact_horizon": event.impact_horizon,
            }
            for event in news.events
            if event.importance >= 70
        )

    @staticmethod
    def _wait_signal(
        instrument: Instrument,
        quant,
        *,
        timeframe: str,
        policy,
        response_time: datetime,
        model_id: str,
        parse_status: str,
        input_hash: str,
        data_as_of: datetime,
        context_json: str,
        raw_model_response: str | None,
        reason: str,
        confidence_cap: float = 1.0,
        source_type: str = "live",
        replay_run_id: str | None = None,
    ) -> SignalProposal:
        return build_signal(
            instrument,
            quant,
            timeframe=timeframe,
            generated_at=response_time,
            time_policy=policy,
            model_id=model_id,
            parse_status=parse_status,
            input_hash=input_hash,
            data_as_of=data_as_of,
            context_json=context_json,
            raw_model_response=raw_model_response,
            force_action=Action.WAIT,
            reason_codes=(parse_status, reason),
            summary=f"WAIT: {reason}.",
            confidence_cap=confidence_cap,
            source_type=source_type,
            replay_run_id=replay_run_id,
        )

    def persist(self, result: AnalysisResult) -> None:
        """Persist one result; replay can call this after calibration metadata is attached."""

        if self.store is None:
            return
        signal = result.signal
        model_status = result.model_status
        self.store.save_instrument(result.instrument)
        self.store.save_snapshot(result.instrument.symbol, result.timeframe, result.response_time.isoformat(), result.context.to_dict())
        self.store.save_provider_snapshot(result.instrument.symbol, result.bundle.snapshot)
        self.store.save_news_events(result.instrument.symbol, result.news)
        if result.benchmark_context is not None:
            self.store.save_benchmark_metadata(result.benchmark_context.benchmark.to_dict())
        if result.event_context is not None:
            self.store.save_event_context(result.event_context.to_dict())
        self.store.save_prediction(signal)
        self.store.save_model_run(
            prediction_id=signal.prediction_id,
            model_id=str(model_status.get("model_id") or signal.model_id),
            started_at=result.response_time.isoformat(),
            latency_ms=float(model_status["latency_ms"]) if model_status.get("latency_ms") is not None else None,
            input_tokens_est=int(model_status["input_tokens_est"]) if model_status.get("input_tokens_est") is not None else None,
            output_chars=int(model_status["output_chars"]) if model_status.get("output_chars") is not None else None,
            success=bool(model_status.get("available")) and not bool(model_status.get("error_code")),
            error_code=str(model_status["error_code"]) if model_status.get("error_code") else None,
        )

    def analyze(
        self,
        instrument: Instrument,
        *,
        timeframe: str = "1h",
        limit: int = 120,
        analysis_time: datetime | None = None,
        source_type: str = "live",
        replay_run_id: str | None = None,
        context_capabilities: dict[str, object] | None = None,
    ) -> AnalysisResult:
        response_time = (analysis_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if source_type == "replay" and not replay_run_id:
            raise ValueError("replay analysis requires replay_run_id")
        bundle = self._bundle(instrument, timeframe, limit)
        quant = build_quant_snapshot(list(bundle.bars), timeframe, symbol=instrument.symbol)
        event_context, news, benchmark_context, memory_context, policy = self._phase6_context(
            instrument,
            timeframe=timeframe,
            bundle=bundle,
            quant=quant,
            response_time=response_time,
            persist_mapping=True,
        )
        market_context: dict[str, object] = {
            "news_available": news.available,
            "news_provider": news.provider,
            "news_error_code": news.error_code,
            "news_clusters": [cluster.to_dict() for cluster in news.clusters],
            "source_type": source_type,
            "benchmark_context": benchmark_context.to_dict(),
            "event_intelligence": event_context.to_dict(),
            "market_memory": memory_context.to_dict(),
        }
        if replay_run_id:
            market_context["replay_run_id"] = replay_run_id
        if context_capabilities:
            market_context["context_capabilities"] = dict(context_capabilities)
        context = MarketContext(
            instrument=instrument,
            quote=bundle.quote,
            bars=bundle.bars,
            quant=quant,
            news=news.events,
            time_policy=policy,
            provider_snapshot=bundle.snapshot,
            market_context=market_context,
            risk_events=self._risk_events(news) + tuple(event.to_dict() for event in event_context.events if event.importance >= 70),
        )
        context_json = json.dumps(context.to_prompt_payload(max_news=8), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        fallback_confidence_cap = 1.0
        if bundle.snapshot.stale:
            fallback_confidence_cap = min(fallback_confidence_cap, 0.65)
        if not news.available:
            fallback_confidence_cap = min(fallback_confidence_cap, 0.60)
        model_status: dict[str, object]
        if self.llm_provider is None:
            model_status = {"provider": "none", "available": False, "error_code": "MODEL_NOT_CONFIGURED"}
            signal = self._wait_signal(
                instrument,
                quant,
                timeframe=timeframe,
                policy=policy,
                response_time=response_time,
                model_id="none",
                parse_status="MODEL_NOT_CONFIGURED",
                input_hash=context.input_hash(),
                data_as_of=bundle.data_as_of,
                context_json=context_json,
                raw_model_response=None,
                reason="local model is not configured",
                confidence_cap=fallback_confidence_cap,
                source_type=source_type,
                replay_run_id=replay_run_id,
            )
        else:
            provider_name = str(getattr(self.llm_provider, "provider_name", self.llm_provider.__class__.__name__.lower()))
            model_id = str(getattr(self.llm_provider, "model_name", getattr(self.llm_provider, "model_id", provider_name)))
            try:
                model_policy = SignalPolicy.from_context(context)
                response, metadata = analyze_with_repair(self.llm_provider, context, model_policy)  # type: ignore[arg-type]
                signal = to_signal_proposal(
                    response,
                    context,
                    model_policy,
                    metadata,
                    generated_at=response_time,
                    source_type=source_type,
                    replay_run_id=replay_run_id,
                )
                model_status = {"provider": provider_name, "available": True} | metadata.to_dict()
                if getattr(self.llm_provider, "quantization", None):
                    model_status["quantization"] = getattr(self.llm_provider, "quantization")
                if getattr(self.llm_provider, "context_length", None):
                    model_status["context_length"] = getattr(self.llm_provider, "context_length")
                if hasattr(self.llm_provider, "think"):
                    model_status["think"] = bool(getattr(self.llm_provider, "think"))
            except LLMError as exc:
                model_status = {
                    "provider": provider_name,
                    "model_id": model_id,
                    "available": False,
                    "error_code": exc.code,
                    "raw_response": exc.raw_response,
                }
                if getattr(self.llm_provider, "quantization", None):
                    model_status["quantization"] = getattr(self.llm_provider, "quantization")
                if hasattr(self.llm_provider, "think"):
                    model_status["think"] = bool(getattr(self.llm_provider, "think"))
                signal = self._wait_signal(
                    instrument,
                    quant,
                    timeframe=timeframe,
                    policy=policy,
                    response_time=response_time,
                    model_id=model_id,
                    parse_status=exc.code,
                    input_hash=context.input_hash(),
                    data_as_of=bundle.data_as_of,
                    context_json=context_json,
                    raw_model_response=exc.raw_response,
                    reason=f"model unavailable or output invalid ({exc.code})",
                    confidence_cap=fallback_confidence_cap,
                    source_type=source_type,
                    replay_run_id=replay_run_id,
                )
        result = AnalysisResult(
            instrument,
            timeframe,
            response_time,
            bundle,
            news,
            context,
            signal,
            model_status,
            benchmark_context,
            event_context,
            memory_context,
        )
        self.persist(result)
        return result
