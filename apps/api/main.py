"""Optional local FastAPI product API for paper-only market research.

The stdlib core remains usable without FastAPI.  The API deliberately keeps
existing successful response shapes stable while exposing Phase 6 deterministic
context evidence over the accepted Phase 0-5 paper-only core.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from core.ai import OllamaProvider
from core.ai.prompts import PROMPT_VERSION
from core.benchmarks import benchmark_capabilities
from core.alerts import (
    ALERT_PAGE_LIMIT,
    ALERT_SEVERITIES,
    ALERT_SOURCES,
    ALERT_STATUSES,
    AlertReconciler,
    alert_capabilities,
    alert_policy,
)
from core.analysis_service import AnalysisError, AnalysisService
from core.config import API_PHASE, APP_VERSION, ConfigurationError, database_path_from_env, env_bool, redact_path, runtime_capabilities
from core.consult import (
    CONSULT_CONTRACT_VERSION,
    CONSULT_STREAM_MEDIA_TYPE,
    ConsultServiceError,
    ConsultValidationError,
    QwenConsultService,
    ndjson_line,
    parse_consult_request,
)
from core.events import NewsEventProviderAdapter, event_capabilities
from core.instruments import (
    CandidateParseError,
    Instrument,
    InstrumentCandidate,
    instrument_for,
    parse_instrument_candidate,
    phase1_universe,
)
from core.memory import MarketMemoryService, memory_capabilities
from core.market_intelligence import MARKET_INTELLIGENCE_VERSION, brief_source_evidence, build_market_intelligence
from core.model_routing import DEFAULT_FAST_MODEL, ModelRoutingConfig
from core.news_engine import NewsEngine, RSSNewsProvider
from core.outcomes import OutcomeStatus
from core.performance.metrics import build_performance_snapshot
from core.radar import RADAR_CATEGORIES, build_radar
from core.scheduler import (
    DefaultScanAnalysisExecutor,
    LocalResourceProbe,
    LocalSchedulerRuntime,
    ResourceProbe,
    ScanAnalysisExecutor,
    SchedulerRuntimeError,
)
from core.settlement import SettlementService
from core.providers import (
    FixtureNewsProvider,
    InstrumentValidationError,
    InstrumentValidator,
    ProviderChain,
    PublicInstrumentValidator,
    build_default_provider,
)
from core.signals import Action
from core.storage import APP_SETTING_DEFINITIONS, SQLiteStore, validate_app_setting_value

_UNSET = object()
API_VERSION = APP_VERSION
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
    from fastapi.responses import JSONResponse, StreamingResponse
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
    StreamingResponse = None  # type: ignore[assignment,misc]


def _store() -> SQLiteStore:
    try:
        database_path = database_path_from_env()
    except ConfigurationError as exc:
        raise RuntimeError("invalid local database configuration") from exc
    store = SQLiteStore(database_path)
    store.initialize()
    return store


def _news_provider() -> object:
    return FixtureNewsProvider() if os.environ.get("NEWS_MODE", "real").lower() == "fixture" else RSSNewsProvider()


def _llm_provider() -> OllamaProvider | None:
    if os.environ.get("LLM_MODE", "ollama").lower() in {"disabled", "off", "none"}:
        return None
    return OllamaProvider()


def _consult_service() -> QwenConsultService:
    """Build the bounded local consultation service without probing the model."""

    return QwenConsultService()


def _service(
    *,
    provider: object | None = None,
    store: SQLiteStore | None = None,
    news_provider: object | None = None,
    event_provider: object | None = None,
    llm_provider: object | None = _UNSET,
) -> AnalysisService:
    factory = (lambda _instrument: provider) if provider is not None else build_default_provider
    selected_llm = _llm_provider() if llm_provider is _UNSET else llm_provider
    selected_news = _news_provider() if news_provider is None else news_provider
    selected_store = store if store is not None else _store()
    return AnalysisService(
        market_provider_factory=factory,
        news_provider=selected_news,
        event_provider=event_provider,
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


def get_event_provider() -> object:
    """FastAPI dependency boundary for typed event evidence."""

    return NewsEventProviderAdapter(_news_provider())


def get_llm_provider() -> OllamaProvider | None:
    """FastAPI dependency boundary for the local model provider."""

    return _llm_provider()


def get_consult_service(request: Request) -> QwenConsultService:
    """Return the app-scoped serial consultation service."""

    service = getattr(request.app.state, "consult_service", None)
    if not isinstance(service, QwenConsultService):
        raise RuntimeError("Qwen consultation service is not configured")
    return service


def get_scheduler(request: Request) -> LocalSchedulerRuntime:
    """Return the app-scoped scheduler without starting it implicitly."""

    runtime = getattr(request.app.state, "scheduler_runtime", None)
    if runtime is None:
        factory = getattr(request.app.state, "scheduler_factory", None)
        if not callable(factory):
            raise RuntimeError("scheduler runtime is not configured")
        runtime = factory()
        request.app.state.scheduler_runtime = runtime
    return runtime


def get_instrument_validator() -> InstrumentValidator:
    """FastAPI dependency boundary for explicit public instrument validation."""

    return PublicInstrumentValidator()


def _read_only_snapshot_service() -> AnalysisService:
    """Build a snapshot service with no persistence, news, or model boundary."""

    return AnalysisService(
        market_provider_factory=build_default_provider,
        news_provider=FixtureNewsProvider(),
        llm_provider=None,
        store=None,
    )


if FastAPI is not None:

    def get_analysis_service(
        store: SQLiteStore = Depends(get_store),
        news_provider: object = Depends(get_news_provider),
        event_provider: object = Depends(get_event_provider),
        llm_provider: object | None = Depends(get_llm_provider),
    ) -> AnalysisService:
        """Build the analysis service from overridable store/provider dependencies."""

        return _service(store=store, news_provider=news_provider, event_provider=event_provider, llm_provider=llm_provider)


    def get_snapshot_service(
    ) -> AnalysisService:
        """Build the read-only market snapshot service without persistence or enrichment."""

        return _read_only_snapshot_service()

else:

    def get_analysis_service() -> AnalysisService:  # pragma: no cover - FastAPI is absent
        return _service()


    def get_snapshot_service() -> AnalysisService:  # pragma: no cover - FastAPI is absent
        return _read_only_snapshot_service()


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


def _analysis_limit(value: object, *, label: str = "analysis") -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise APIError(400, "INVALID_LIMIT", f"{label} limit must be an integer", detail=str(value)) from exc
    return max(60, min(500, parsed))


def _resolve_instrument(store: SQLiteStore | None, value: str) -> Instrument:
    try:
        return instrument_for(value)
    except ValueError as canonical_error:
        if store is not None:
            try:
                return store.resolve_instrument(value)
            except (TypeError, ValueError):
                pass
        raise canonical_error


def _normalize_symbol(value: str | None, store: SQLiteStore | None = None) -> str | None:
    if value is None:
        return None
    try:
        return _resolve_instrument(store, value).symbol
    except ValueError as exc:
        raise APIError(400, "INVALID_SYMBOL", "unsupported instrument symbol", detail=str(exc), context={"symbol": value}) from exc


def _instrument_payload(item: Instrument, *, record: dict[str, object] | None = None) -> dict[str, object]:
    registry_source = str(record.get("registry_source", "canonical")) if record else "canonical"
    metadata_status = str(record.get("metadata_status", "canonical")) if record else "canonical"
    labels = record.get("metadata_labels", {}) if record else {}
    metadata_labels = labels if isinstance(labels, dict) else {}
    return {
        "symbol": item.symbol,
        "asset_type": item.asset_type.value,
        "exchange": item.exchange,
        "currency": item.currency,
        "quote_currency": item.quote_currency,
        "timezone": item.timezone,
        "trading_hours": item.trading_hours.value,
        "sector": item.sector,
        "registry_source": registry_source,
        "metadata_status": metadata_status,
        "metadata_labels": metadata_labels,
        "validation_provider": record.get("validation_provider") if record else None,
        "validated_at": record.get("validated_at") if record else None,
    }


def _watchlist_entry_payload(entry: dict[str, str], store: SQLiteStore) -> dict[str, object]:
    instrument = _resolve_instrument(store, entry["symbol"])
    return {
        **entry,
        "instrument": _instrument_payload(
            instrument,
            record=store.get_instrument_record(instrument.symbol),
        ),
    }


def _watchlist_body_symbol(
    body: dict[str, Any] | None,
    *,
    required: bool,
    store: SQLiteStore | None = None,
) -> str | None:
    if body is None:
        if required:
            raise APIError(400, "INVALID_WATCHLIST_PAYLOAD", "watchlist payload must contain only a symbol")
        return None
    unknown = set(body) - {"symbol"}
    if unknown:
        raise APIError(
            400,
            "INVALID_WATCHLIST_PAYLOAD",
            "watchlist payload may contain only symbol",
            context={"unknown_fields": sorted(unknown)},
        )
    if "symbol" not in body:
        if required:
            raise APIError(400, "INVALID_WATCHLIST_PAYLOAD", "watchlist payload must contain symbol")
        return None
    raw_symbol = body["symbol"]
    if not isinstance(raw_symbol, str) or not raw_symbol.strip():
        raise APIError(400, "INVALID_WATCHLIST_PAYLOAD", "watchlist symbol must be a non-empty string")
    return _normalize_symbol(raw_symbol, store)


def _normalize_setting_key(value: str) -> str:
    normalized = value.strip()
    if normalized not in APP_SETTING_DEFINITIONS:
        raise APIError(
            404,
            "APP_SETTING_NOT_FOUND",
            "unsupported app setting key",
            context={"key": value, "allowed_keys": sorted(APP_SETTING_DEFINITIONS)},
        )
    return normalized


def _normalize_radar_asset_type(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    normalized = value.strip().lower()
    if normalized not in {"equity", "crypto"}:
        raise APIError(
            400,
            "INVALID_RADAR_ASSET_TYPE",
            "radar asset_type must be equity or crypto",
            context={"asset_type": value, "allowed": ["equity", "crypto"]},
        )
    return normalized


def _normalize_radar_category(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    normalized = value.strip().upper().replace(" ", "_")
    if normalized not in RADAR_CATEGORIES:
        raise APIError(
            400,
            "INVALID_RADAR_CATEGORY",
            "radar category is unsupported",
            context={"category": value, "allowed": sorted(RADAR_CATEGORIES)},
        )
    return normalized


def _normalize_alert_source(value: str | None) -> str | None:
    return _enum_filter(value, field="alert_source", allowed=ALERT_SOURCES, upper=False)


def _normalize_alert_severity(value: str | None) -> str | None:
    return _enum_filter(value, field="alert_severity", allowed=ALERT_SEVERITIES, upper=True)


def _normalize_alert_status(value: str | None) -> str | None:
    return _enum_filter(value, field="alert_status", allowed=ALERT_STATUSES, upper=True)


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


def _required_utc_timestamp(value: object, *, field: str = "as_of") -> datetime:
    parsed = _parse_utc_timestamp(value)
    if parsed is None:
        raise APIError(400, "INVALID_AS_OF", f"{field} must be an ISO timestamp with timezone", context={"field": field})
    return parsed


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
    store: SQLiteStore | None = None,
    symbol: str | None,
    timeframe: str | None,
    action: str | None,
    source_type: str | None,
    outcome_status: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    return (
        _normalize_symbol(symbol, store),
        _normalize_timeframe(timeframe) if timeframe is not None else None,
        _enum_filter(action, field="action", allowed=VALID_ACTIONS, upper=True),
        _enum_filter(source_type, field="source_type", allowed=VALID_SOURCE_TYPES, upper=False),
        _enum_filter(outcome_status, field="outcome_status", allowed=VALID_OUTCOME_STATUSES, upper=True),
    )


def _analysis_result(
    service: AnalysisService,
    symbol: str,
    *,
    store: SQLiteStore | None = None,
    timeframe: str = "1h",
    limit: int = 120,
) -> dict[str, object]:
    try:
        instrument = _resolve_instrument(store, symbol)
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


def _instrument_registration_candidate(body: dict[str, Any] | None) -> InstrumentCandidate:
    if body is None or set(body) != {"symbol", "asset_type"}:
        raise APIError(
            400,
            "INVALID_INSTRUMENT_REGISTRATION_PAYLOAD",
            "instrument registration payload must contain only symbol and asset_type",
        )
    try:
        return parse_instrument_candidate(body["symbol"], body["asset_type"])
    except CandidateParseError as exc:
        raise APIError(
            400,
            "INVALID_INSTRUMENT_CANDIDATE",
            "instrument candidate failed strict validation",
            detail=str(exc),
        ) from exc


def _instrument_catalog(store: SQLiteStore) -> list[dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for instrument in phase1_universe():
        records[instrument.symbol] = {"instrument": instrument}
    for record in store.list_instrument_records():
        instrument = record["instrument"]
        source = str(record.get("registry_source", "canonical"))
        if source in {"canonical", "registered"} and isinstance(instrument, Instrument):
            records.setdefault(instrument.symbol, record)
    return [
        _instrument_payload(record["instrument"], record=record if "registry_source" in record else None)
        for record in records.values()
    ]


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
    def health_model(
        llm_provider: object | None = Depends(get_llm_provider),
        consult_service: QwenConsultService = Depends(get_consult_service),
    ) -> dict[str, object]:
        consult = consult_service.capability()
        if llm_provider is None:
            consult["available"] = False
            return {"provider": "none", "available": False, "error_code": "MODEL_NOT_CONFIGURED", "consult": consult}
        health_method = getattr(llm_provider, "health", None)
        if not callable(health_method):
            consult["available"] = False
            return {
                "provider": str(getattr(llm_provider, "provider_name", llm_provider.__class__.__name__.lower())),
                "available": False,
                "error_code": "MODEL_HEALTH_UNSUPPORTED",
                "consult": consult,
            }
        try:
            if isinstance(llm_provider, OllamaProvider):
                health = OllamaProvider(
                    timeout=float(os.environ.get("OLLAMA_HEALTH_TIMEOUT_SEC", "2")),
                    retries=0,
                ).health()
            else:
                health = health_method()
            safe_health = dict(health)
            # Provider exception text can contain local paths or request data;
            # the health contract exposes only stable capability/error fields.
            safe_health.pop("detail", None)
            installed = {str(name) for name in safe_health.get("models", []) if isinstance(name, str)} if isinstance(safe_health.get("models"), list) else set()
            configured_models = consult.get("models") if isinstance(consult.get("models"), dict) else {}
            fast_model = str(configured_models.get("fast", ""))
            smart_model = str(configured_models.get("smart", ""))
            consult["models_status"] = {
                "fast": {"model_id": fast_model, "available": fast_model in installed},
                "smart": {"model_id": smart_model, "available": smart_model in installed},
            }
            consult["available"] = bool(safe_health.get("available")) and bool(installed & {fast_model, smart_model})
            safe_health["consult"] = consult
            return safe_health
        except Exception:  # pragma: no cover - depends on the local model process
            consult["available"] = False
            return {
                "provider": str(getattr(llm_provider, "provider_name", llm_provider.__class__.__name__.lower())),
                "available": False,
                "error_code": "MODEL_HEALTH_ERROR",
                "consult": consult,
            }

    @router.get("/health/release")
    def health_release(
        store: SQLiteStore = Depends(get_store),
        consult_service: QwenConsultService = Depends(get_consult_service),
    ) -> dict[str, object]:
        """Expose bounded local capability evidence without revealing paths."""

        try:
            database = {
                "available": True,
                "schema_version": store.schema_version(),
                "path": redact_path(store.path),
            }
        except Exception:  # pragma: no cover - local filesystem dependent
            database = {"available": False, "error_code": "DATABASE_UNAVAILABLE"}
        capabilities = runtime_capabilities()
        capabilities["qwen_consult"] = consult_service.capability()
        capabilities["model_routing"] = ModelRoutingConfig.from_env().capability()
        capabilities["market_intelligence"] = {
            "version": MARKET_INTELLIGENCE_VERSION,
            "get_is_read_only": True,
            "live_fetch_on_get": False,
            "daily_brief_generation": "explicit_post_only",
        }
        return {
            "status": "ok" if database.get("available") else "degraded",
            "phase": API_PHASE,
            "api_version": API_VERSION,
            "database": database,
            "backup": {
                "available": True,
                "format_version": "phase7_backup_v1",
                "mechanism": "sqlite_backup_api",
                "restore_requires_explicit_command": True,
            },
            "capabilities": capabilities,
        }

    @router.get("/health/context")
    def health_context() -> dict[str, object]:
        return {
            "phase": API_PHASE,
            "api_version": API_VERSION,
            "benchmark": benchmark_capabilities(),
            "events": event_capabilities(),
            "memory": memory_capabilities(),
            "time_policy": {
                "owner": "python",
                "major_event_effects": "validity_cap_and_re_evaluation",
                "llm_override": False,
            },
            "read_only_get": True,
            "cloud_required": False,
        }

    @router.get("/market-intelligence")
    def market_intelligence(
        as_of: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        cutoff = _required_utc_timestamp(as_of) if as_of is not None else datetime.now(timezone.utc)
        return build_market_intelligence(store, as_of=cutoff)

    @router.get("/daily-brief")
    def daily_brief(
        language: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        if language is not None and language not in {"en", "zh-CN"}:
            raise APIError(400, "INVALID_BRIEF_LANGUAGE", "daily brief language must be en or zh-CN")
        brief = store.latest_daily_brief(language=language)
        return {
            "contract_version": "daily_brief_v1",
            "status": "available" if brief else "unavailable",
            "brief": brief,
            "generation": "explicit_post_only",
            "read_only": True,
        }

    @router.post("/daily-brief/generate")
    async def generate_daily_brief(
        body: dict[str, Any] = Body(default_factory=dict),
        store: SQLiteStore = Depends(get_store),
        service: QwenConsultService = Depends(get_consult_service),
    ) -> dict[str, object]:
        if set(body) - {"language", "model_preference"}:
            raise APIError(400, "INVALID_BRIEF_PAYLOAD", "daily brief accepts only language and model_preference")
        language = body.get("language", "en")
        preference = body.get("model_preference", "auto")
        if language not in {"en", "zh-CN"}:
            raise APIError(400, "INVALID_BRIEF_LANGUAGE", "daily brief language must be en or zh-CN")
        view = build_market_intelligence(store)
        source_hash, sources, missing = brief_source_evidence(view)
        evidence = json.dumps(
            {key: view[key] for key in ("as_of", "pulse", "calendar", "watchlist", "heatmap", "news")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        instruction = (
            "请仅依据以下本地已保存证据生成简洁的每日市场简报，分为隔夜变化、今日事件、热门领域、关注资产和风险；缺失项要明确说明。"
            if language == "zh-CN"
            else "Using only the following locally saved evidence, write a concise daily market brief covering overnight changes, today's events, hot areas, focus assets, and risks. State missing evidence explicitly."
        )
        evidence_budget = max(512, service.config.max_message_chars - len(instruction) - 96)
        evidence = evidence[:evidence_budget]
        try:
            request = parse_consult_request(
                {
                    "language": language,
                    "model_preference": preference,
                    "task": "daily_brief",
                    "messages": [{"role": "user", "content": f"{instruction}\nBEGIN_SAVED_EVIDENCE\n{evidence}\nEND_SAVED_EVIDENCE"}],
                },
                service.config,
            )
            session = await service.open(request, store=store)
        except ConsultValidationError as exc:
            raise APIError(exc.status_code, exc.code, exc.message) from exc
        except ConsultServiceError as exc:
            raise APIError(exc.status_code, exc.code, exc.message) from exc
        content: list[str] = []
        metadata: dict[str, object] = {}
        stream_error: dict[str, object] | None = None
        async for event in session.events():
            if event.get("type") == "meta":
                metadata = event
            elif event.get("type") == "delta" and isinstance(event.get("content"), str):
                content.append(str(event["content"]))
            elif event.get("type") == "error":
                stream_error = event.get("error") if isinstance(event.get("error"), dict) else {"code": "QWEN_STREAM_FAILED"}
        if stream_error is not None:
            raise APIError(503, str(stream_error.get("code", "QWEN_STREAM_FAILED")), str(stream_error.get("message", "Daily brief generation failed.")))
        rendered = "".join(content).strip()
        route = metadata.get("model_route") if isinstance(metadata.get("model_route"), dict) else {}
        if not rendered:
            raise APIError(503, "QWEN_EMPTY_RESPONSE", "Daily brief generation returned no content")
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "brief_id": str(uuid4()),
            "generated_at": now,
            "as_of": view["as_of"],
            "language": language,
            "model_id": metadata.get("model_id", "unknown"),
            "model_tier": metadata.get("model_tier", "unknown"),
            "route_reason": route.get("reason", "unknown"),
            "source_hash": source_hash,
            "sources": sources,
            "missing": missing,
            "content": rendered,
            "capability": {"provider": metadata.get("provider"), "contract_version": "daily_brief_v1", "explicit_generation": True},
        }
        store.save_daily_brief(payload)
        return {"contract_version": "daily_brief_v1", "status": "available", "brief": payload}

    async def _consult_payload(request: Request, service: QwenConsultService) -> object:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise APIError(400, "INVALID_CONTENT_LENGTH", "request content length is invalid") from exc
            if declared_length < 0 or declared_length > service.config.max_body_bytes:
                raise APIError(413, "CONSULT_BODY_LIMIT", "consultation request body exceeds the configured limit")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise APIError(415, "CONSULT_JSON_REQUIRED", "consultation request must use application/json")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > service.config.max_body_bytes:
                raise APIError(413, "CONSULT_BODY_LIMIT", "consultation request body exceeds the configured limit")
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(400, "INVALID_CONSULT_JSON", "consultation request body is not valid JSON") from exc

    @router.post("/consult/stream")
    async def consult_stream(
        request: Request,
        store: SQLiteStore = Depends(get_store),
        service: QwenConsultService = Depends(get_consult_service),
    ):
        """Stream local Qwen text as versioned NDJSON without domain writes."""

        payload = await _consult_payload(request, service)
        try:
            consult_request = parse_consult_request(payload, service.config)
            session = await service.open(consult_request, store=store)
        except ConsultValidationError as exc:
            raise APIError(exc.status_code, exc.code, exc.message) from exc
        except ConsultServiceError as exc:
            raise APIError(exc.status_code, exc.code, exc.message) from exc

        async def stream_events():
            try:
                async for event in session.events():
                    if await request.is_disconnected():
                        break
                    yield ndjson_line(event)
            finally:
                await session.close()

        return StreamingResponse(
            stream_events(),
            media_type=CONSULT_STREAM_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Qwen-Consult-Contract": CONSULT_CONTRACT_VERSION,
            },
        )

    @router.get("/instruments")
    def instruments(store: SQLiteStore = Depends(get_store)) -> list[dict[str, object]]:
        return _instrument_catalog(store)

    @router.post("/instruments/register")
    def register_instrument(
        body: dict[str, Any] | None = Body(default=None),
        store: SQLiteStore = Depends(get_store),
        validator: InstrumentValidator = Depends(get_instrument_validator),
    ) -> dict[str, object]:
        candidate = _instrument_registration_candidate(body)
        symbol = candidate.instrument.symbol
        existing = store.get_instrument_record(symbol)
        was_existing = existing is not None
        if existing is not None and str(existing.get("registry_source")) == "registered":
            return {
                "instrument": _instrument_payload(existing["instrument"], record=existing),
                "registered": True,
                "idempotent": True,
                "validation": {"status": "already_registered"},
            }
        if candidate.is_canonical:
            if existing is None:
                store.save_instrument(
                    candidate.instrument,
                    registry_source="canonical",
                    metadata_status=candidate.metadata_status,
                    metadata_labels=candidate.metadata_labels,
                )
                existing = store.get_instrument_record(symbol)
            assert existing is not None
            return {
                "instrument": _instrument_payload(existing["instrument"], record=existing),
                "registered": True,
                "idempotent": was_existing,
                "validation": {"status": "canonical_known"},
            }
        try:
            validation = validator.validate(candidate)
        except InstrumentValidationError as exc:
            status_code = 422 if exc.code == "INSTRUMENT_UNSUPPORTED" else 503
            raise APIError(
                status_code,
                exc.code,
                "instrument provider validation failed",
                detail=str(exc),
                context={"provider": exc.provider},
            ) from exc
        store.save_instrument(
            candidate.instrument,
            registry_source="registered",
            metadata_status=candidate.metadata_status,
            metadata_labels=candidate.metadata_labels,
            validation_provider=validation.provider,
            validated_at=validation.validated_at.astimezone(timezone.utc).isoformat(),
        )
        # Registration is the explicit write boundary for the deterministic
        # benchmark mapping; context GETs remain read-only.
        from core.benchmarks import benchmark_metadata_for

        store.save_benchmark_metadata(benchmark_metadata_for(candidate.instrument).to_dict())
        stored = store.get_instrument_record(symbol)
        assert stored is not None
        return {
            "instrument": _instrument_payload(stored["instrument"], record=stored),
            "registered": True,
            "idempotent": False,
            "validation": validation.to_dict(),
        }

    @router.get("/watchlist")
    def watchlist(store: SQLiteStore = Depends(get_store)) -> list[dict[str, object]]:
        return [_watchlist_entry_payload(entry, store) for entry in store.list_watchlist_entries()]

    @router.post("/watchlist")
    def add_watchlist(
        body: dict[str, Any] | None = Body(default=None),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        symbol = _watchlist_body_symbol(body, required=True, store=store)
        assert symbol is not None
        return _watchlist_entry_payload(store.upsert_watchlist_entry(symbol), store)

    @router.put("/watchlist/{symbol}")
    def upsert_watchlist(
        symbol: str,
        body: dict[str, Any] | None = Body(default=None),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        canonical_symbol = _normalize_symbol(symbol, store)
        assert canonical_symbol is not None
        body_symbol = _watchlist_body_symbol(body, required=False, store=store)
        if body_symbol is not None and body_symbol != canonical_symbol:
            raise APIError(
                400,
                "WATCHLIST_SYMBOL_MISMATCH",
                "watchlist body symbol must match the path symbol",
                context={"path_symbol": canonical_symbol, "body_symbol": body_symbol},
            )
        return _watchlist_entry_payload(store.upsert_watchlist_entry(canonical_symbol), store)

    @router.delete("/watchlist/{symbol}")
    def delete_watchlist(symbol: str, store: SQLiteStore = Depends(get_store)) -> dict[str, object]:
        canonical_symbol = _normalize_symbol(symbol, store)
        assert canonical_symbol is not None
        return {"symbol": canonical_symbol, "deleted": store.delete_watchlist_entry(canonical_symbol)}

    @router.get("/scheduler/status")
    def scheduler_status(scheduler: LocalSchedulerRuntime = Depends(get_scheduler)) -> dict[str, object]:
        return scheduler.status()

    @router.get("/scheduler/history")
    def scheduler_history(
        limit: int = 20,
        scheduler: LocalSchedulerRuntime = Depends(get_scheduler),
    ) -> dict[str, object]:
        return scheduler.history(limit=max(1, min(int(limit), 100)))

    @router.post("/scheduler/start")
    def scheduler_start(scheduler: LocalSchedulerRuntime = Depends(get_scheduler)) -> dict[str, object]:
        try:
            return scheduler.start()
        except SchedulerRuntimeError as exc:
            raise APIError(409, exc.code, exc.message, context=exc.context) from exc

    @router.post("/scheduler/stop")
    def scheduler_stop(scheduler: LocalSchedulerRuntime = Depends(get_scheduler)) -> dict[str, object]:
        try:
            return scheduler.stop()
        except SchedulerRuntimeError as exc:
            raise APIError(409, exc.code, exc.message, context=exc.context) from exc

    @router.post("/scheduler/run-once")
    def scheduler_run_once(scheduler: LocalSchedulerRuntime = Depends(get_scheduler)) -> dict[str, object]:
        try:
            return scheduler.run_once()
        except SchedulerRuntimeError as exc:
            raise APIError(409, exc.code, exc.message, context=exc.context) from exc

    @router.get("/alerts")
    def alerts(
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        normalized_source = _normalize_alert_source(source)
        normalized_severity = _normalize_alert_severity(severity)
        normalized_status = _normalize_alert_status(status)
        bounded_limit = min(_clamp_limit(limit, maximum=ALERT_PAGE_LIMIT), ALERT_PAGE_LIMIT)
        bounded_offset = _clamp_offset(offset)
        filters = {
            "source": normalized_source,
            "severity": normalized_severity,
            "status": normalized_status,
        }
        return {
            "policy": alert_policy(),
            "capabilities": alert_capabilities(),
            "alerts": store.list_alerts(limit=bounded_limit, offset=bounded_offset, **filters),
            "counts": store.alert_counts(),
            "pagination": {
                "limit": bounded_limit,
                "offset": bounded_offset,
                "total": store.count_alerts(**filters),
                "filters": {key: value for key, value in filters.items() if value is not None},
            },
        }

    @router.get("/alerts/count")
    def alert_count(store: SQLiteStore = Depends(get_store)) -> dict[str, object]:
        return {"policy": alert_policy(), "counts": store.alert_counts()}

    @router.get("/alerts/status")
    def alert_status(store: SQLiteStore = Depends(get_store)) -> dict[str, object]:
        return {
            "policy": alert_policy(),
            "capabilities": alert_capabilities(),
            "counts": store.alert_counts(),
            "last_reconciliation": store.get_scheduler_state("last_alert_reconciliation"),
        }

    @router.post("/alerts/{alert_id}/acknowledge")
    def acknowledge_alert(
        alert_id: str,
        body: dict[str, Any] | None = Body(default=None),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        if body not in (None, {}):
            raise APIError(
                400,
                "INVALID_ALERT_ACK_PAYLOAD",
                "alert acknowledgement payload must be empty",
            )
        alert = store.acknowledge_alert(
            alert_id,
            acknowledged_at=datetime.now(timezone.utc).isoformat(),
        )
        if alert is None:
            raise APIError(404, "ALERT_NOT_FOUND", "alert not found", context={"alert_id": alert_id})
        return alert

    @router.get("/settings")
    def settings(store: SQLiteStore = Depends(get_store)) -> list[dict[str, object]]:
        return store.list_app_settings()

    @router.get("/settings/{key}")
    def app_setting(key: str, store: SQLiteStore = Depends(get_store)) -> dict[str, object]:
        return store.get_app_setting(_normalize_setting_key(key))

    @router.put("/settings/{key}")
    def update_app_setting(
        key: str,
        body: dict[str, Any] | None = Body(default=None),
        store: SQLiteStore = Depends(get_store),
        scheduler: LocalSchedulerRuntime = Depends(get_scheduler),
    ) -> dict[str, object]:
        normalized_key = _normalize_setting_key(key)
        if body is None or set(body) != {"value"}:
            raise APIError(
                400,
                "INVALID_APP_SETTING_PAYLOAD",
                "app setting payload must contain only value",
            )
        try:
            validate_app_setting_value(normalized_key, body["value"])
            setting = store.upsert_app_setting(normalized_key, body["value"])
            if normalized_key == "scheduler.enabled" and body["value"] is False:
                try:
                    scheduler.stop()
                except SchedulerRuntimeError as exc:
                    raise APIError(409, exc.code, exc.message, context=exc.context) from exc
            return setting
        except ValueError as exc:
            raise APIError(
                400,
                "INVALID_APP_SETTING_VALUE",
                "app setting value failed validation",
                detail=str(exc),
                context={"key": normalized_key},
            ) from exc

    @router.delete("/settings/{key}")
    def reset_app_setting(
        key: str,
        store: SQLiteStore = Depends(get_store),
        scheduler: LocalSchedulerRuntime = Depends(get_scheduler),
    ) -> dict[str, object]:
        normalized_key = _normalize_setting_key(key)
        deleted = store.delete_app_setting(normalized_key)
        if normalized_key == "scheduler.enabled":
            try:
                scheduler.stop()
            except SchedulerRuntimeError as exc:
                raise APIError(409, exc.code, exc.message, context=exc.context) from exc
        return {"key": normalized_key, "deleted": deleted, "setting": store.get_app_setting(normalized_key)}

    @router.get("/instruments/{symbol}/snapshot")
    def snapshot(
        symbol: str,
        timeframe: str = "1h",
        limit: str = "120",
        service: AnalysisService = Depends(get_snapshot_service),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        timeframe = _normalize_timeframe(timeframe)
        bounded_limit = _analysis_limit(limit, label="snapshot")
        try:
            instrument = _resolve_instrument(store, symbol)
        except ValueError as exc:
            raise APIError(400, "INVALID_SYMBOL", "unsupported instrument symbol", detail=str(exc), context={"symbol": symbol}) from exc
        try:
            result = service.market_snapshot(instrument, timeframe=timeframe, limit=bounded_limit)
        except AnalysisError as exc:
            if exc.code == "quant_error":
                raise APIError(
                    502,
                    "SNAPSHOT_QUANT_ERROR",
                    "instrument snapshot quant calculation failed",
                    detail=str(exc),
                    context={"provider": exc.provider, "provider_error_code": exc.code},
                ) from exc
            raise APIError(
                502,
                "SNAPSHOT_PROVIDER_ERROR",
                "instrument snapshot provider failed",
                detail=str(exc),
                context={"provider": exc.provider, "provider_error_code": exc.code},
            ) from exc
        return result.to_dict()

    @router.get("/instruments/{symbol}/news")
    def news(
        symbol: str,
        news_provider: object = Depends(get_news_provider),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        try:
            instrument = _resolve_instrument(store, symbol)
        except ValueError as exc:
            raise APIError(400, "INVALID_SYMBOL", "unsupported instrument symbol", detail=str(exc), context={"symbol": symbol}) from exc
        return NewsEngine(news_provider).collect(instrument, limit=10).to_dict()

    @router.get("/instruments/{symbol}/context")
    def instrument_context(
        symbol: str,
        timeframe: str = "1h",
        limit: str = "120",
        as_of: str | None = None,
        service: AnalysisService = Depends(get_analysis_service),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        timeframe = _normalize_timeframe(timeframe)
        bounded_limit = _analysis_limit(limit, label="context")
        cutoff = _required_utc_timestamp(as_of) if as_of is not None else None
        try:
            instrument = _resolve_instrument(store, symbol)
            result = service.market_context_snapshot(
                instrument,
                timeframe=timeframe,
                limit=bounded_limit,
                as_of=cutoff,
            )
            return result.to_dict()
        except ValueError as exc:
            raise APIError(400, "INVALID_CONTEXT_REQUEST", "context request is invalid", detail=str(exc)) from exc
        except AnalysisError as exc:
            raise APIError(
                503,
                "CONTEXT_PROVIDER_ERROR",
                "deterministic context provider failed",
                detail=str(exc),
                context={"provider": exc.provider, "provider_error_code": exc.code},
            ) from exc

    @router.get("/instruments/{symbol}/events")
    def instrument_events(
        symbol: str,
        timeframe: str = "1h",
        limit: str = "120",
        as_of: str | None = None,
        service: AnalysisService = Depends(get_analysis_service),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        context = instrument_context(symbol, timeframe, limit, as_of, service, store)
        return {
            "symbol": context.get("symbol"),
            "as_of": context.get("data_as_of"),
            "events": context.get("events"),
            "time_policy": context.get("time_policy"),
            "provenance": context.get("provenance"),
        }

    @router.get("/instruments/{symbol}/memory")
    def instrument_memory(
        symbol: str,
        timeframe: str = "1h",
        limit: str = "120",
        as_of: str | None = None,
        service: AnalysisService = Depends(get_analysis_service),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        context = instrument_context(symbol, timeframe, limit, as_of, service, store)
        return {
            "symbol": context.get("symbol"),
            "as_of": context.get("data_as_of"),
            "market_memory": context.get("market_memory"),
            "provenance": context.get("provenance"),
        }

    @router.post("/memory/materialize")
    def materialize_memory(
        body: dict[str, Any] | None = Body(default=None),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        body = body or {}
        if set(body) - {"as_of", "source_type", "limit"} or "as_of" not in body:
            raise APIError(400, "INVALID_MEMORY_MATERIALIZATION_PAYLOAD", "memory materialization requires only as_of, source_type and limit")
        source_type = str(body.get("source_type", "live"))
        if source_type not in VALID_SOURCE_TYPES:
            raise APIError(400, "INVALID_SOURCE_TYPE", "memory source_type must be live or replay")
        try:
            limit = _clamp_limit(int(body.get("limit", 500)), maximum=1_000)
        except (TypeError, ValueError) as exc:
            raise APIError(400, "INVALID_LIMIT", "memory materialization limit must be an integer", detail=str(body.get("limit"))) from exc
        service = MarketMemoryService(store)
        return service.materialize(as_of=_required_utc_timestamp(body["as_of"]), source_type=source_type, limit=limit)

    @router.post("/analysis/{symbol}")
    def analysis(
        symbol: str,
        body: dict[str, Any] | None = Body(default=None),
        service: AnalysisService = Depends(get_analysis_service),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        body = body or {}
        timeframe = _normalize_timeframe(str(body.get("timeframe", "1h")))
        limit = _analysis_limit(body.get("limit", 120), label="analysis")
        return _analysis_result(service, symbol, store=store, timeframe=timeframe, limit=limit)

    @router.post("/analyze/{symbol}")
    def analyze_legacy(
        symbol: str,
        service: AnalysisService = Depends(get_analysis_service),
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        return _analysis_result(service, symbol, store=store)

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
            store=store,
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
            store=store,
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
            store=store,
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

    @router.get("/radar")
    def radar(
        asset_type: str | None = None,
        category: str | None = None,
        store: SQLiteStore = Depends(get_store),
    ) -> dict[str, object]:
        normalized_asset_type = _normalize_radar_asset_type(asset_type)
        normalized_category = _normalize_radar_category(category)
        watchlist_entries = store.list_watchlist_entries()
        symbols = [entry["symbol"] for entry in watchlist_entries]
        instruments = {symbol: store.resolve_instrument(symbol) for symbol in symbols}
        predictions = store.list_latest_prediction_records(symbols, source_type="live")
        calibrations = store.list_calibration_results(limit=1)
        return build_radar(
            watchlist_entries,
            instruments=instruments,
            predictions=predictions,
            calibration=calibrations[0] if calibrations else None,
            asset_type=normalized_asset_type,
            category=normalized_category,
        )

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
        symbol = _normalize_symbol(symbol, store) if symbol else None
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
            symbol=symbol,
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
        symbols = [_normalize_symbol(str(item), store) for item in raw_symbols]
        timeframes = [_normalize_timeframe(str(item)) for item in raw_timeframes]
        assert all(item is not None for item in symbols)
        run_id = f"api-{uuid4().hex[:16]}"
        try:
            samples = int(body.get("samples", 300))
        except (TypeError, ValueError) as exc:
            raise APIError(400, "INVALID_SAMPLES", "samples must be an integer", detail=str(body.get("samples"))) from exc
        store.create_replay_run(
            run_id=run_id,
            model_id=str(body.get("model_id", os.environ.get("OLLAMA_MODEL", DEFAULT_FAST_MODEL))),
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
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if len(origins) > 16:
        raise ValueError("too many CORS origins")
    return origins


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        return env_bool(name, default)
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def create_app(
    *,
    store: SQLiteStore | None = None,
    analysis_service: AnalysisService | None = None,
    snapshot_service: AnalysisService | None = None,
    news_provider: object | None = None,
    event_provider: object | None = None,
    llm_provider: object | None = _UNSET,
    consult_service: QwenConsultService | object = _UNSET,
    instrument_validator: InstrumentValidator | None = None,
    scheduler_runtime: LocalSchedulerRuntime | None = None,
    scheduler_executor: ScanAnalysisExecutor | None = None,
    scheduler_resource_probe: ResourceProbe | Callable[[], object] | None = None,
    scheduler_clock: Callable[[], datetime] | None = None,
    settlement_service: SettlementService | None = None,
    alert_reconciler: AlertReconciler | None = None,
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
        description="Local-first V1.1 market research API with deterministic point-in-time evidence, explicit local scheduling, paper-only tracking, read-only market intelligence and dual-tier local Qwen assistance.",
        openapi_extra={"x-phase": API_PHASE, "x-product-baseline": "v1.1"},
    )
    app.state.api_phase = API_PHASE
    app.state.api_version = API_VERSION
    app.state.consult_service = _consult_service() if consult_service is _UNSET else consult_service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Content-Type"],
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
        app.dependency_overrides[get_event_provider] = lambda: event_provider or NewsEventProviderAdapter(news_provider)
    elif event_provider is not None:
        app.dependency_overrides[get_event_provider] = lambda: event_provider
    if llm_provider is not _UNSET:
        app.dependency_overrides[get_llm_provider] = lambda: llm_provider
    if instrument_validator is not None:
        app.dependency_overrides[get_instrument_validator] = lambda: instrument_validator
    if analysis_service is not None:
        app.dependency_overrides[get_analysis_service] = lambda: analysis_service
    if snapshot_service is not None:
        app.dependency_overrides[get_snapshot_service] = lambda: snapshot_service
    elif analysis_service is not None:
        app.dependency_overrides[get_snapshot_service] = lambda: analysis_service

    def scheduler_factory() -> LocalSchedulerRuntime:
        scheduler_store = store if store is not None else _store()
        if scheduler_executor is not None:
            executor = scheduler_executor
        else:
            service_or_factory: AnalysisService | Callable[[], AnalysisService]
            if analysis_service is not None:
                service_or_factory = analysis_service
            else:
                service_or_factory = lambda: _service(
                    store=scheduler_store,
                    news_provider=news_provider,
                    event_provider=event_provider,
                    llm_provider=llm_provider,
                )
            executor = DefaultScanAnalysisExecutor(service_or_factory, scheduler_store)
        return LocalSchedulerRuntime(
            store=scheduler_store,
            analysis_executor=executor,
            resource_probe=scheduler_resource_probe or LocalResourceProbe(),
            clock=scheduler_clock or (lambda: datetime.now(timezone.utc)),
            settlement_service=settlement_service,
            alert_reconciler=alert_reconciler,
        )

    app.state.scheduler_factory = scheduler_factory
    app.state.scheduler_runtime = scheduler_runtime

    async def shutdown_scheduler() -> None:
        runtime = getattr(app.state, "scheduler_runtime", None)
        if runtime is not None:
            try:
                runtime.stop()
            except SchedulerRuntimeError:
                pass

    app.router.on_shutdown.append(shutdown_scheduler)
    return app


app = create_app()
