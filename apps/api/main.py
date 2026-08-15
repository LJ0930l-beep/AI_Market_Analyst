"""Optional local FastAPI product API for paper-only market research.

The stdlib core remains usable without FastAPI.  The API deliberately keeps
existing successful response shapes stable while adding a small, injectable
Phase 4 contract for the future UI.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from core.ai import OllamaProvider
from core.ai.prompts import PROMPT_VERSION
from core.analysis_service import AnalysisError, AnalysisService
from core.instruments import instrument_for, phase1_universe
from core.news_engine import NewsEngine, RSSNewsProvider
from core.outcomes import OutcomeStatus
from core.performance.metrics import build_performance_snapshot
from core.providers import FixtureNewsProvider, ProviderChain, build_default_provider
from core.signals import Action
from core.storage import SQLiteStore

_UNSET = object()
API_VERSION = "0.4.0"
API_PHASE = 4
DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
VALID_TIMEFRAMES = frozenset({"5m", "15m", "1h", "4h", "1d"})
VALID_SOURCE_TYPES = frozenset({"live", "replay"})
VALID_ACTIONS = frozenset(item.value for item in Action)
VALID_OUTCOME_STATUSES = frozenset(item.value for item in OutcomeStatus)


class APIError(Exception):
    """Stable API-facing error with an explicit HTTP status and code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        detail: Any = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        self.context = context

    def to_payload(self) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            error["detail"] = self.detail
        if self.context:
            error["context"] = self.context
        return {"error": error}


try:  # FastAPI is optional; the stdlib core must remain importable without it.
    from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - exercised only without the optional API extra
    APIRouter = None  # type: ignore[assignment,misc]
    Body = None  # type: ignore[assignment,misc]
    Depends = None  # type: ignore[assignment,misc]
    FastAPI = None  # type: ignore[assignment,misc]
    HTTPException = RuntimeError  # type: ignore[assignment,misc]
    Request = object  # type: ignore[assignment,misc]
    RequestValidationError = RuntimeError  # type: ignore[assignment,misc]
    CORSMiddleware = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]


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


def _service(
    *,
    provider: object | None = None,
    store: SQLiteStore | None = None,
    news_provider: object | None = None,
    llm_provider: object | None = _UNSET,
) -> AnalysisService:
    factory = (lambda _instrument: provider) if provider is not None else build_default_provider
    selected_llm = _llm_provider() if llm_provider is _UNSET else llm_provider
    selected_news = _news_provider() if news_provider is None else news_provider
    selected_store = store if store is not None else _store()
    return AnalysisService(
        market_provider_factory=factory,
        news_provider=selected_news,
        llm_provider=selected_llm,
        store=selected_store,
    )


def analyze_symbol(
    symbol: str,
    *,
    provider: object | None = None,
    store: SQLiteStore | None = None,
    llm_provider: object | None = None,
):
    """Backward-compatible helper returning only the SignalProposal."""

    return _service(provider=provider, store=store, llm_provider=llm_provider).analyze(instrument_for(symbol)).signal


def _analysis(symbol: str, *, timeframe: str = "1h", limit: int = 120) -> dict[str, object]:
    return _service().analyze(instrument_for(symbol), timeframe=timeframe, limit=limit).to_dict()


def get_store() -> SQLiteStore:
    """FastAPI dependency boundary for the durable SQLite store."""

    return _store()


def get_news_provider() -> object:
    """FastAPI dependency boundary for the news provider."""

    return _news_provider()


def get_llm_provider() -> OllamaProvider | None:
    """FastAPI dependency boundary for the local model provider."""

    return _llm_provider()


if FastAPI is not None:

    def get_analysis_service(
        store: SQLiteStore = Depends(get_store),
        news_provider: object = Depends(get_news_provider),
        llm_provider: object | None = Depends(get_llm_provider),
    ) -> AnalysisService:
        """Build the analysis service from overridable store/provider dependencies."""

        return _service(store=store, news_provider=news_provider, llm_provider=llm_provider)


    def get_snapshot_service(
        store: SQLiteStore = Depends(get_store),
        news_provider: object = Depends(get_news_provider),
    ) -> AnalysisService:
        """Build the legacy snapshot service without invoking a model."""

        return _service(store=store, news_provider=news_provider, llm_provider=None)

else:

    def get_analysis_service() -> AnalysisService:  # pragma: no cover - FastAPI is absent
        return _service()


    def get_snapshot_service() -> AnalysisService:  # pragma: no cover - FastAPI is absent
        return _service(llm_provider=None)


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


def _clamp_limit(value: int, *, maximum: int = 500) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise APIError(400, "INVALID_LIMIT", "limit must be an integer", detail=str(value)) from exc
    return max(1, min(parsed, maximum))


def _clamp_offset(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise APIError(400, "INVALID_OFFSET", "offset must be an integer", detail=str(value)) from exc
    return max(0, min(parsed, 100_000))


def _enum_filter(
    value: str | None,
    *,
    field: str,
    allowed: frozenset[str],
    upper: bool,
) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper() if upper else str(value).strip().lower()
    if normalized not in allowed:
        raise APIError(
            400,
            f"INVALID_{field.upper()}",
            f"invalid {field} filter",
            detail=f"expected one of: {', '.join(sorted(allowed))}",
            context={"field": field, "value": value, "allowed": sorted(allowed)},
        )
    return normalized


def _normalize_timeframe(value: str) -> str:
    normalized = _enum_filter(value, field="timeframe", allowed=VALID_TIMEFRAMES, upper=False)
    assert normalized is not None
    return normalized


def _normalize_symbol(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return instrument_for(value).symbol
    except ValueError as exc:
        raise APIError(400, "INVALID_SYMBOL", "unsupported instrument symbol", detail=str(exc), context={"symbol": value}) from exc


def _normalize_paper_status(value: object) -> str:
    if value is None:
        raise APIError(400, "INVALID_PAPER_TRADE_STATUS", "paper trade status must be a non-empty string")
    normalized = str(value).strip().upper()
    if not normalized:
        raise APIError(400, "INVALID_PAPER_TRADE_STATUS", "paper trade status must be a non-empty string")
    return normalized


def _parse_utc_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prediction_valid_until(prediction: dict[str, Any]) -> datetime | None:
    explicit = _parse_utc_timestamp(prediction.get("signal_valid_until"))
    if explicit is not None:
        return explicit
    generated_at = _parse_utc_timestamp(prediction.get("generated_at"))
    try:
        validity_minutes = int(prediction.get("signal_validity_minutes"))
    except (TypeError, ValueError):
        return None
    return generated_at + timedelta(minutes=validity_minutes) if generated_at else None


def _read_filters(
    *,
    symbol: str | None,
    timeframe: str | None,
    action: str | None,
    source_type: str | None,
    outcome_status: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    return (
        _normalize_symbol(symbol),
        _normalize_timeframe(timeframe) if timeframe is not None else None,
        _enum_filter(action, field="action", allowed=VALID_ACTIONS, upper=True),
        _enum_filter(source_type, field="source_type", allowed=VALID_SOURCE_TYPES, upper=False),
        _enum_filter(outcome_status, field="outcome_status", allowed=VALID_OUTCOME_STATUSES, upper=True),
    )


def _analysis_result(
    service: AnalysisService,
    symbol: str,
    *,
    timeframe: str = "1h",
    limit: int = 120,
) -> dict[str, object]:
    try:
        instrument = instrument_for(symbol)
    except ValueError as exc:
        raise APIError(400, "INVALID_SYMBOL", "unsupported instrument symbol", detail=str(exc), context={"symbol": symbol}) from exc
    try:
        return service.analyze(instrument, timeframe=timeframe, limit=limit).to_dict()
    except ValueError as exc:
        raise APIError(400, "INVALID_ANALYSIS_REQUEST", "analysis request is invalid", detail=str(exc)) from exc
    except AnalysisError as exc:
        raise APIError(
            502,
            "ANALYSIS_PROVIDER_ERROR",
            "analysis provider failed",
            detail=str(exc),
            context={"provider": exc.provider, "provider_error_code": exc.code},
        ) from exc


if FastAPI is not None:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "phase": API_PHASE,
            "api_version": API_VERSION,
            "product": "AI Market Analyst",
            "real_orders": False,
            "private_keys": False,
        }

    @router.get("/health/providers")
    def health_providers() -> dict[str, object]:
        routes: list[dict[str, object]] = []
        for item in phase1_universe():
            provider = build_default_provider(item)
            names = (
                [str(getattr(candidate, "provider_name", candidate.__class__.__name__.lower())) for candidate in provider.providers]
                if isinstance(provider, ProviderChain)
                else [str(getattr(provider, "provider_name", provider.__class__.__name__.lower()))]
            )
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

    @router.get("/health/model")
    def health_model(llm_provider: object | None = Depends(get_llm_provider)) -> dict[str, object]:
        if llm_provider is None:
            return {"provider": "none", "available": False, "error_code": "MODEL_NOT_CONFIGURED"}
        health_method = getattr(llm_provider, "health", None)
        if not callable(health_method):
            return {
                "provider": str(getattr(llm_provider, "provider_name", llm_provider.__class__.__name__.lower())),
                "available": False,
                "error_code": "MODEL_HEALTH_UNSUPPORTED",
            }
        try:
            if isinstance(llm_provider, OllamaProvider):
                health = OllamaProvider(
                    timeout=float(os.environ.get("OLLAMA_HEALTH_TIMEOUT_SEC", "2")),
                    retries=0,
                ).health()
            else:
                health = health_method()
            return dict(health)
        except Exception as exc:  # pragma: no cover - depends on the local model process
            return {
                "provider": str(getattr(llm_provider, "provider_name", llm_provider.__class__.__name__.lower())),
                "available": False,
                "error_code": "MODEL_HEALTH_ERROR",
                "detail": str(exc),
            }

    @router.get("/instruments")
    def instruments() -> list[dict[str, object]]:
        return [item_payload(item) for item in phase1_universe()]

    @router.get("/instruments/{symbol}/snapshot")
    def snapshot(symbol: str, service: AnalysisService = Depends(get_snapshot_service)) -> dict[str, object]:
        try:
            result = service.analyze(instrument_for(symbol))
        except ValueError as exc:
            raise APIError(400, "INVALID_SYMBOL", "unsupported instrument symbol", detail=str(exc), context={"symbol": symbol}) from exc
        except AnalysisError as exc:
            raise APIError(
                502,
                "SNAPSHOT_PROVIDER_ERROR",
                "instrument snapshot provider failed",
                detail=str(exc),
                context={"provider": exc.provider, "provider_error_code": exc.code},
            ) from exc
        return {
            "symbol": result.instrument.symbol,
            "response_time": result.response_time.isoformat(),
            "data_as_of": result.data_as_of.isoformat(),
            "provider_snapshot": result.bundle.snapshot.to_dict(),
            "quote": result.to_dict()["quote"],
            "quant": result.context.quant.to_dict(),
            "time_policy": result.context.time_policy.to_dict() if result.context.time_policy else None,
        }

    @router.get("/instruments/{symbol}/news")
    def news(symbol: str, news_provider: object = Depends(get_news_provider)) -> dict[str, object]:
        try:
            instrument = instrument_for(symbol)
        except ValueError as exc:
            raise APIError(400, "INVALID_SYMBOL", "unsupported instrument symbol", detail=str(exc), context={"symbol": symbol}) from exc
        return NewsEngine(news_provider).collect(instrument, limit=10).to_dict()

    @router.post("/analysis/{symbol}")
    def analysis(
        symbol: str,
        body: dict[str, Any] | None = Body(default=None),
        service: AnalysisService = Depends(get_analysis_service),
    ) -> dict[str, object]:
        body = body or {}
        timeframe = _normalize_timeframe(str(body.get("timeframe", "1h")))
        try:
            limit = max(60, min(500, int(body.get("limit", 120))))
        except (TypeError, ValueError) as exc:
            raise APIError(400, "INVALID_LIMIT", "analysis limit must be an integer", detail=str(body.get("limit"))) from exc
        return _analysis_result(service, symbol, timeframe=timeframe, limit=limit)

    @router.post("/analyze/{symbol}")
    def analyze_legacy(symbol: str, service: AnalysisService = Depends(get_analysis_service)) -> dict[str, object]:
        return _analysis_result(service, symbol)

    @router.get("/predictions")
    def predictions(
        limit: int = 50,
        offset: int = 0,
        symbol: str | None = None,
        timeframe: str | None = None,
        action: str | None = None,
        source_type: str | None = None,
        replay_run_id: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        outcome_status: str | None = None,
        has_outcome: bool | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        symbol, timeframe, action, source_type, outcome_status = _read_filters(
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            source_type=source_type,
            outcome_status=outcome_status,
        )
        return store.list_prediction_payloads(
            limit=_clamp_limit(limit),
            offset=_clamp_offset(offset),
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            source_type=source_type,
            replay_run_id=replay_run_id,
            model_id=model_id,
            prompt_version=prompt_version,
            outcome_status=outcome_status,
            has_outcome=has_outcome,
        )

    @router.get("/predictions/{prediction_id}")
    def prediction_detail(prediction_id: str, store: SQLiteStore = Depends(get_store)) -> dict[str, Any]:
        record = store.get_prediction_record(prediction_id)
        if record is None:
            raise APIError(404, "PREDICTION_NOT_FOUND", "prediction not found", context={"prediction_id": prediction_id})
        payload = dict(record["prediction"])
        payload["paper_trade"] = record["paper_trade"]
        payload["outcome"] = record["outcome"]
        payload["outcome_status"] = record["outcome_status"]
        return payload

    def _follow_prediction(
        prediction_id: str,
        body: dict[str, Any] | None,
        store: SQLiteStore,
    ) -> dict[str, object]:
        raw_body = body or {}
        record = store.get_prediction_record(prediction_id)
        if record is None:
            raise APIError(404, "PREDICTION_NOT_FOUND", "prediction not found", context={"prediction_id": prediction_id})
        existing = record["paper_trade"]
        prediction = record["prediction"]
        if existing is None:
            action = str(prediction.get("action", "")).upper()
            if action not in {Action.LONG.value, Action.SHORT.value}:
                raise APIError(
                    409,
                    "PREDICTION_NOT_ACTIONABLE",
                    "only LONG and SHORT predictions can be followed",
                    context={"prediction_id": prediction_id, "action": action or None},
                )
            valid_until = _prediction_valid_until(prediction)
            if valid_until is not None and datetime.now(timezone.utc) >= valid_until:
                raise APIError(
                    409,
                    "SIGNAL_EXPIRED",
                    "prediction signal validity has expired",
                    context={"prediction_id": prediction_id, "signal_valid_until": valid_until.isoformat()},
                )
        if "status" in raw_body:
            status = _normalize_paper_status(raw_body["status"])
        elif existing is not None:
            status = str(existing["status"])
        else:
            status = "OPEN"
        followed_at = str(existing["followed_at"]) if existing is not None else datetime.now(timezone.utc).isoformat()
        try:
            store.follow_prediction(prediction_id, followed_at, status=status)
        except KeyError as exc:
            raise APIError(404, "PREDICTION_NOT_FOUND", "prediction not found", detail=str(exc), context={"prediction_id": prediction_id}) from exc
        return {"prediction_id": prediction_id, "status": status, "real_order": False}

    @router.post("/predictions/{prediction_id}/follow")
    def follow_prediction(
        prediction_id: str,
        body: dict[str, Any] | None = Body(default=None),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        return _follow_prediction(prediction_id, body, store)

    @router.get("/paper-trades")
    def paper_trades(
        limit: int = 50,
        offset: int = 0,
        symbol: str | None = None,
        timeframe: str | None = None,
        action: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
        outcome_status: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        symbol, timeframe, action, source_type, outcome_status = _read_filters(
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            source_type=source_type,
            outcome_status=outcome_status,
        )
        return store.list_paper_trade_records(
            limit=_clamp_limit(limit),
            offset=_clamp_offset(offset),
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            source_type=source_type,
            status=_normalize_paper_status(status) if status is not None else None,
            outcome_status=outcome_status,
        )

    @router.get("/paper-trades/{prediction_id}")
    def paper_trade_detail(prediction_id: str, store: SQLiteStore = Depends(get_store)) -> dict[str, Any]:
        record = store.get_paper_trade_record(prediction_id)
        if record is None:
            raise APIError(404, "PAPER_TRADE_NOT_FOUND", "paper trade not found", context={"prediction_id": prediction_id})
        return record

    @router.post("/paper-trades/{prediction_id}")
    def follow_legacy(prediction_id: str, store: SQLiteStore = Depends(get_store)) -> dict[str, object]:
        return _follow_prediction(prediction_id, None, store)

    @router.get("/outcomes")
    def outcomes(
        limit: int = 50,
        offset: int = 0,
        symbol: str | None = None,
        timeframe: str | None = None,
        action: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        symbol, timeframe, action, source_type, _ = _read_filters(
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            source_type=source_type,
            outcome_status=status,
        )
        return store.list_outcome_records(
            limit=_clamp_limit(limit),
            offset=_clamp_offset(offset),
            symbol=symbol,
            timeframe=timeframe,
            action=action,
            source_type=source_type,
            status=_enum_filter(status, field="status", allowed=VALID_OUTCOME_STATUSES, upper=True),
        )

    @router.get("/outcomes/{prediction_id}")
    def outcome_detail(prediction_id: str, store: SQLiteStore = Depends(get_store)) -> dict[str, Any]:
        record = store.get_outcome_record(prediction_id)
        if record is None:
            raise APIError(404, "OUTCOME_NOT_FOUND", "outcome not found", context={"prediction_id": prediction_id})
        return record

    @router.get("/stats")
    def stats(store: SQLiteStore = Depends(get_store)) -> dict[str, int]:
        return store.counts()

    def _performance_summary(
        store: SQLiteStore,
        *,
        source_type: str,
        symbol: str | None,
        timeframe: str | None,
        model_id: str | None,
        prompt_version: str | None,
        replay_run_id: str | None,
    ) -> dict[str, object]:
        source_type = _enum_filter(source_type, field="source_type", allowed=VALID_SOURCE_TYPES, upper=False) or "live"
        timeframe = _normalize_timeframe(timeframe) if timeframe else None
        symbol = _normalize_symbol(symbol) if symbol else None
        records = store.list_prediction_records(
            source_type=source_type,
            replay_run_id=replay_run_id,
            symbol=symbol,
            timeframe=timeframe,
            model_id=model_id,
            prompt_version=prompt_version,
        )
        scope: dict[str, object] = {"source_type": source_type}
        for key, value in (("symbol", symbol), ("timeframe", timeframe), ("model_id", model_id), ("prompt_version", prompt_version)):
            if value:
                scope[key] = value
        return build_performance_snapshot(records, scope=scope)

    @router.get("/performance/summary")
    def performance_summary(
        source_type: str = "live",
        symbol: str | None = None,
        timeframe: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        replay_run_id: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        return _performance_summary(
            store,
            source_type=source_type,
            symbol=symbol,
            timeframe=timeframe,
            model_id=model_id,
            prompt_version=prompt_version,
            replay_run_id=replay_run_id,
        )

    @router.get("/performance/by-symbol/{symbol}")
    def performance_by_symbol(
        symbol: str,
        source_type: str = "live",
        timeframe: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        replay_run_id: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        return _performance_summary(
            store,
            source_type=source_type,
            symbol=symbol.upper(),
            timeframe=timeframe,
            model_id=model_id,
            prompt_version=prompt_version,
            replay_run_id=replay_run_id,
        )

    @router.get("/performance/buckets")
    def performance_buckets(
        source_type: str = "live",
        model_id: str | None = None,
        prompt_version: str | None = None,
        replay_run_id: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        summary = _performance_summary(
            store,
            source_type=source_type,
            symbol=None,
            timeframe=None,
            model_id=model_id,
            prompt_version=prompt_version,
            replay_run_id=replay_run_id,
        )
        metrics = summary.get("metrics") if isinstance(summary, dict) else {}
        return {"scope": summary.get("scope", {}), "confidence_buckets": (metrics or {}).get("confidence_buckets", [])}

    @router.get("/calibration/current")
    def calibration_current(store: SQLiteStore = Depends(get_store)) -> dict[str, object]:
        results = store.list_calibration_results(limit=1)
        return results[0] if results else {"status": "INSUFFICIENT_SAMPLE", "sample_count": 0, "buckets": []}

    @router.post("/replay/runs")
    def create_replay_run_request(
        body: dict[str, Any] | None = Body(default=None),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        body = body or {}
        raw_symbols = body.get("symbols", [item.symbol for item in phase1_universe()])
        raw_timeframes = body.get("timeframes", ["1h", "4h"])
        if not isinstance(raw_symbols, list) or not raw_symbols:
            raise APIError(400, "INVALID_SYMBOLS", "symbols must be a non-empty list")
        if not isinstance(raw_timeframes, list) or not raw_timeframes:
            raise APIError(400, "INVALID_TIMEFRAMES", "timeframes must be a non-empty list")
        symbols = [_normalize_symbol(str(item)) for item in raw_symbols]
        timeframes = [_normalize_timeframe(str(item)) for item in raw_timeframes]
        assert all(item is not None for item in symbols)
        run_id = f"api-{uuid4().hex[:16]}"
        try:
            samples = int(body.get("samples", 300))
        except (TypeError, ValueError) as exc:
            raise APIError(400, "INVALID_SAMPLES", "samples must be an integer", detail=str(body.get("samples"))) from exc
        store.create_replay_run(
            run_id=run_id,
            model_id=str(body.get("model_id", os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"))),
            prompt_version=str(body.get("prompt_version", PROMPT_VERSION)),
            symbols=[item for item in symbols if item is not None],
            timeframes=timeframes,
            sampling_policy={"samples": max(1, min(samples, 10_000)), "resume": True, "execution": "cli"},
            manifest_hash=f"pending:{run_id}",
            config={"requested_via": "api", "execution": "scripts/run_phase3_replay.py"},
            status="PENDING",
        )
        return store.get_replay_run(run_id) or {"run_id": run_id, "status": "PENDING"}

    @router.get("/replay/runs")
    def replay_runs(
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        valid_statuses = frozenset({"PENDING", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"})
        normalized_status = _enum_filter(status, field="replay_status", allowed=valid_statuses, upper=True)
        return store.list_replay_runs(
            limit=_clamp_limit(limit),
            offset=_clamp_offset(offset),
            status=normalized_status,
        )

    @router.get("/replay/runs/{run_id}")
    def replay_run_status(run_id: str, store: SQLiteStore = Depends(get_store)) -> dict[str, object]:
        run = store.get_replay_run(run_id)
        if run is None:
            raise APIError(404, "REPLAY_RUN_NOT_FOUND", "replay run not found", context={"run_id": run_id})
        run["samples"] = store.list_replay_samples(run_id)
        return run

    @router.get("/predictions/{prediction_id}/calibration")
    def prediction_calibration(prediction_id: str, store: SQLiteStore = Depends(get_store)) -> dict[str, object]:
        record = store.get_prediction_record(prediction_id)
        if record is None:
            raise APIError(404, "PREDICTION_NOT_FOUND", "prediction not found", context={"prediction_id": prediction_id})
        prediction = record["prediction"]
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

else:
    router = None


def _cors_origins() -> list[str]:
    configured = os.environ.get("API_CORS_ORIGINS")
    if configured is None:
        configured = os.environ.get("CORS_ORIGINS")
    if configured is None:
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app(
    *,
    store: SQLiteStore | None = None,
    analysis_service: AnalysisService | None = None,
    news_provider: object | None = None,
    llm_provider: object | None = _UNSET,
):
    """Create an API app with optional service injections for isolated tests."""

    if FastAPI is None:  # pragma: no cover - API tests install the optional extra
        return None
    origins = _cors_origins()
    allow_credentials = _env_bool("API_CORS_ALLOW_CREDENTIALS", _env_bool("CORS_ALLOW_CREDENTIALS", False))
    if "*" in origins and allow_credentials:
        raise ValueError("wildcard CORS origins cannot be combined with credentials")
    app = FastAPI(
        title="AI Market Analyst",
        version=API_VERSION,
        description="Local-first Phase 4 market research API with structured paper-only tracking.",
        openapi_extra={"x-phase": API_PHASE, "x-product-baseline": "phase4"},
    )
    app.state.api_phase = API_PHASE
    app.state.api_version = API_VERSION
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(APIError)
    async def api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(HTTPException)
    async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            error = detail
        else:
            code = {
                400: "BAD_REQUEST",
                401: "UNAUTHORIZED",
                403: "FORBIDDEN",
                404: "NOT_FOUND",
                405: "METHOD_NOT_ALLOWED",
                422: "VALIDATION_ERROR",
            }.get(exc.status_code, "HTTP_ERROR")
            error = {"code": code, "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": error})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details: list[dict[str, object]] = []
        for item in exc.errors():
            details.append(
                {
                    "loc": list(item.get("loc", ())),
                    "msg": str(item.get("msg", "invalid value")),
                    "type": str(item.get("type", "validation_error")),
                }
            )
        error = APIError(422, "VALIDATION_ERROR", "request validation failed", detail=details)
        return JSONResponse(status_code=error.status_code, content=error.to_payload())

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        detail = str(exc) if _env_bool("API_DEBUG_ERRORS", False) else None
        error = APIError(500, "INTERNAL_ERROR", "internal API error", detail=detail)
        return JSONResponse(status_code=error.status_code, content=error.to_payload())

    assert router is not None
    app.include_router(router)

    if store is not None:
        store.initialize()
        app.dependency_overrides[get_store] = lambda: store
    if news_provider is not None:
        app.dependency_overrides[get_news_provider] = lambda: news_provider
    if llm_provider is not _UNSET:
        app.dependency_overrides[get_llm_provider] = lambda: llm_provider
    if analysis_service is not None:
        app.dependency_overrides[get_analysis_service] = lambda: analysis_service
    return app


app = create_app()
