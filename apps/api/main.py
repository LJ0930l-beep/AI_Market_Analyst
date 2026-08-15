"""Optional Phase 2 API for local, paper-only market research."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.analysis_service import AnalysisError, AnalysisService
from core.instruments import instrument_for, phase1_universe
from core.news_engine import NewsEngine, RSSNewsProvider
from core.providers import FixtureNewsProvider, FixtureProvider, ProviderChain, build_default_provider
from core.storage import SQLiteStore
from core.ai import OllamaProvider
from core.performance.metrics import aggregate_performance, build_performance_snapshot

_UNSET = object()

try:  # FastAPI is optional; the stdlib core remains usable without it.
    from fastapi import Body, FastAPI, HTTPException
except ImportError:  # pragma: no cover - exercised only without the optional extra
    FastAPI = None  # type: ignore[assignment,misc]
    Body = None  # type: ignore[assignment,misc]
    HTTPException = RuntimeError  # type: ignore[assignment,misc]


def _store() -> SQLiteStore:
    store = SQLiteStore(os.environ.get("DATABASE_PATH", "data/market_analyst.sqlite3"))
    store.initialize()
    return store


def _news_provider() -> object:
    return FixtureNewsProvider() if os.environ.get("NEWS_MODE", "real").lower() == "fixture" else RSSNewsProvider()


def _llm_provider() -> OllamaProvider | None:
    if os.environ.get("LLM_MODE", "ollama").lower() in {"disabled", "off", "none"}:
        return None
    return OllamaProvider()


def _service(*, provider: object | None = None, store: SQLiteStore | None = None, llm_provider: object | None = _UNSET) -> AnalysisService:
    factory = (lambda _instrument: provider) if provider is not None else build_default_provider
    selected_llm = _llm_provider() if llm_provider is _UNSET else llm_provider
    return AnalysisService(
        market_provider_factory=factory,
        news_provider=_news_provider(),
        llm_provider=selected_llm,
        store=store or _store(),
    )


def analyze_symbol(symbol: str, *, provider: object | None = None, store: SQLiteStore | None = None, llm_provider: object | None = None):
    """Backward-compatible helper returning only the SignalProposal."""

    return _service(provider=provider, store=store, llm_provider=llm_provider).analyze(instrument_for(symbol)).signal


def _analysis(symbol: str, *, timeframe: str = "1h", limit: int = 120) -> dict[str, object]:
    return _service().analyze(instrument_for(symbol), timeframe=timeframe, limit=limit).to_dict()


if FastAPI is not None:
    app = FastAPI(
        title="AI Market Analyst - Phase 2",
        version="0.2.0",
        description="Local-first market context, structured Qwen analysis, and paper-only tracking.",
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "phase": 2, "real_orders": False, "private_keys": False}

    @app.get("/health/providers")
    def health_providers() -> dict[str, object]:
        routes: list[dict[str, object]] = []
        for item in phase1_universe():
            provider = build_default_provider(item)
            names = [str(getattr(candidate, "provider_name", candidate.__class__.__name__.lower())) for candidate in provider.providers] if isinstance(provider, ProviderChain) else [str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))]
            routes.append({"symbol": item.symbol, "asset_type": item.asset_type.value, "providers": names, "mode": os.environ.get("MARKET_DATA_MODE", "real")})
        news_provider = _news_provider()
        return {
            "available": True,
            "routes": routes,
            "news": {
                "provider": str(getattr(news_provider, "provider_name", news_provider.__class__.__name__.lower())),
                "configured": True,
                "probe": "deferred_until_symbol_request",
            },
            "offline_fixture": "fixture",
        }

    @app.get("/health/model")
    def health_model() -> dict[str, object]:
        if os.environ.get("LLM_MODE", "ollama").lower() in {"disabled", "off", "none"}:
            return {"provider": "none", "available": False, "error_code": "MODEL_NOT_CONFIGURED"}
        return OllamaProvider(timeout=float(os.environ.get("OLLAMA_HEALTH_TIMEOUT_SEC", "2")), retries=0).health()

    @app.get("/instruments")
    def instruments() -> list[dict[str, object]]:
        return [item_payload(item) for item in phase1_universe()]

    @app.get("/instruments/{symbol}/snapshot")
    def snapshot(symbol: str) -> dict[str, object]:
        try:
            result = _service(llm_provider=None).analyze(instrument_for(symbol))
        except (ValueError, AnalysisError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "symbol": result.instrument.symbol,
            "response_time": result.response_time.isoformat(),
            "data_as_of": result.data_as_of.isoformat(),
            "provider_snapshot": result.bundle.snapshot.to_dict(),
            "quote": result.to_dict()["quote"],
            "quant": result.context.quant.to_dict(),
            "time_policy": result.context.time_policy.to_dict() if result.context.time_policy else None,
        }

    @app.get("/instruments/{symbol}/news")
    def news(symbol: str) -> dict[str, object]:
        try:
            instrument = instrument_for(symbol)
            result = NewsEngine(_news_provider()).collect(instrument, limit=10)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.to_dict()

    @app.post("/analysis/{symbol}")
    def analysis(symbol: str, body: dict[str, Any] | None = Body(default=None)) -> dict[str, object]:
        body = body or {}
        timeframe = str(body.get("timeframe", "1h"))
        try:
            limit = max(60, min(500, int(body.get("limit", 120))))
            return _analysis(symbol, timeframe=timeframe, limit=limit)
        except (ValueError, AnalysisError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/analyze/{symbol}")
    def analyze_legacy(symbol: str) -> dict[str, object]:
        return _analysis(symbol)

    @app.get("/predictions")
    def predictions(limit: int = 50) -> list[dict[str, Any]]:
        return _store().list_prediction_payloads(limit)

    @app.post("/predictions/{prediction_id}/follow")
    def follow_prediction(prediction_id: str, body: dict[str, Any] | None = Body(default=None)) -> dict[str, object]:
        status = str((body or {}).get("status", "OPEN"))
        try:
            _store().follow_prediction(prediction_id, datetime.now(timezone.utc).isoformat(), status=status)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"prediction_id": prediction_id, "status": status, "real_order": False}

    @app.post("/paper-trades/{prediction_id}")
    def follow_legacy(prediction_id: str) -> dict[str, object]:
        return follow_prediction(prediction_id, None)

    @app.get("/stats")
    def stats() -> dict[str, int]:
        return _store().counts()

    @app.get("/performance/summary")
    def performance_summary(
        source_type: str = "live",
        symbol: str | None = None,
        timeframe: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        replay_run_id: str | None = None,
    ) -> dict[str, object]:
        if source_type not in {"live", "replay"}:
            raise HTTPException(status_code=400, detail="source_type must be live or replay")
        records = _store().list_prediction_records(
            source_type=source_type,
            replay_run_id=replay_run_id,
            symbol=symbol,
            timeframe=timeframe,
            model_id=model_id,
            prompt_version=prompt_version,
        )
        scope = {"source_type": source_type}
        for key, value in (("symbol", symbol), ("timeframe", timeframe), ("model_id", model_id), ("prompt_version", prompt_version)):
            if value:
                scope[key] = value
        return build_performance_snapshot(records, scope=scope)

    @app.get("/performance/by-symbol/{symbol}")
    def performance_by_symbol(
        symbol: str,
        source_type: str = "live",
        timeframe: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        replay_run_id: str | None = None,
    ) -> dict[str, object]:
        return performance_summary(source_type, symbol.upper(), timeframe, model_id, prompt_version, replay_run_id)

    @app.get("/performance/buckets")
    def performance_buckets(
        source_type: str = "live",
        model_id: str | None = None,
        prompt_version: str | None = None,
        replay_run_id: str | None = None,
    ) -> dict[str, object]:
        summary = performance_summary(source_type, None, None, model_id, prompt_version, replay_run_id)
        metrics = summary.get("metrics") if isinstance(summary, dict) else {}
        return {"scope": summary.get("scope", {}), "confidence_buckets": (metrics or {}).get("confidence_buckets", [])}

    @app.get("/calibration/current")
    def calibration_current() -> dict[str, object]:
        results = _store().list_calibration_results(limit=1)
        return results[0] if results else {"status": "INSUFFICIENT_SAMPLE", "sample_count": 0, "buckets": []}

    @app.post("/replay/runs")
    def create_replay_run_request(body: dict[str, Any] | None = Body(default=None)) -> dict[str, object]:
        body = body or {}
        symbols = [str(item).upper() for item in body.get("symbols", [item.symbol for item in phase1_universe()])]
        timeframes = [str(item).lower() for item in body.get("timeframes", ["1h", "4h"])]
        if not symbols or not timeframes:
            raise HTTPException(status_code=400, detail="symbols and timeframes are required")
        run_id = f"api-{uuid4().hex[:16]}"
        store = _store()
        store.create_replay_run(
            run_id=run_id,
            model_id=str(body.get("model_id", os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"))),
            prompt_version=str(body.get("prompt_version", "phase2-json-v6")),
            symbols=symbols,
            timeframes=timeframes,
            sampling_policy={"samples": int(body.get("samples", 300)), "resume": True, "execution": "cli"},
            manifest_hash=f"pending:{run_id}",
            config={"requested_via": "api", "execution": "scripts/run_phase3_replay.py"},
            status="PENDING",
        )
        return store.get_replay_run(run_id) or {"run_id": run_id, "status": "PENDING"}

    @app.get("/replay/runs/{run_id}")
    def replay_run_status(run_id: str) -> dict[str, object]:
        run = _store().get_replay_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"replay run not found: {run_id}")
        run["samples"] = _store().list_replay_samples(run_id)
        return run

    @app.get("/predictions/{prediction_id}/calibration")
    def prediction_calibration(prediction_id: str) -> dict[str, object]:
        records = _store().list_prediction_records(limit=100000)
        for record in records:
            prediction = record["prediction"]
            if prediction.get("prediction_id") == prediction_id:
                return {
                    "prediction_id": prediction_id,
                    "raw_confidence": prediction.get("raw_confidence"),
                    "calibrated_confidence": prediction.get("calibrated_confidence"),
                    "calibration_version": prediction.get("calibration_version"),
                    "calibration_scope": prediction.get("calibration_scope"),
                    "calibration_sample_size": prediction.get("calibration_sample_size"),
                    "calibration_fallback": prediction.get("calibration_fallback"),
                    "source_type": prediction.get("source_type", "live"),
                }
        raise HTTPException(status_code=404, detail=f"prediction not found: {prediction_id}")
else:
    app = None


def item_payload(item) -> dict[str, object]:
    return {
        "symbol": item.symbol,
        "asset_type": item.asset_type.value,
        "exchange": item.exchange,
        "currency": item.currency,
        "quote_currency": item.quote_currency,
        "timezone": item.timezone,
        "trading_hours": item.trading_hours.value,
        "sector": item.sector,
    }
