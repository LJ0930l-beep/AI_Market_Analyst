"""Phase 2 orchestration: Provider -> News -> Quant -> Context -> Model -> Prediction."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .ai import OllamaProvider, SignalPolicy, analyze_with_repair, to_signal_proposal
from .ai.contracts import LLMError
from .context import MarketContext
from .instruments import Instrument
from .news_engine import NewsEngine, NewsFetchResult, RSSNewsProvider
from .providers import FixtureNewsProvider, ProviderError, ProviderChain
from .providers.runtime import MarketDataBundle, ProviderSnapshot, build_default_provider, fetch_market_data
from .quant import build_quant_snapshot
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
            "time_policy": self.context.time_policy.to_dict() if self.context.time_policy else None,
            "model": self.model_status,
            "signal": self.signal.to_dict(),
            "input_hash": self.context.input_hash(),
        }


class AnalysisService:
    """One bounded analysis run with explicit provider/model state."""

    def __init__(
        self,
        *,
        market_provider_factory: Callable[[Instrument], object] | None = None,
        news_provider: object | None = None,
        llm_provider: object | None = None,
        store: SQLiteStore | None = None,
    ) -> None:
        self.market_provider_factory = market_provider_factory or build_default_provider
        if news_provider is None:
            news_provider = FixtureNewsProvider() if os.environ.get("NEWS_MODE", "real").lower() == "fixture" else RSSNewsProvider()
        self.news_provider = news_provider
        self.llm_provider = llm_provider
        self.store = store
        if self.store is not None:
            self.store.initialize()

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
        news = NewsEngine(self.news_provider).collect(instrument, limit=10)  # type: ignore[arg-type]
        policy = build_time_policy(
            timeframe,
            price=quant.price,
            atr14=quant.atr14,
            market_regime=quant.market_regime,
            events=news.events,
            now=response_time,
        )
        market_context: dict[str, object] = {
            "news_available": news.available,
            "news_provider": news.provider,
            "news_error_code": news.error_code,
            "news_clusters": [cluster.to_dict() for cluster in news.clusters],
            "source_type": source_type,
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
            risk_events=self._risk_events(news),
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
        result = AnalysisResult(instrument, timeframe, response_time, bundle, news, context, signal, model_status)
        self.persist(result)
        return result
