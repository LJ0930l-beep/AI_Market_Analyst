"""Production AI-led session coordinator.

This module is the only production entry point that turns the Qwen 9B
provider into :class:`AILedDecisionEngine` cycles.  It deliberately keeps
model selection, account scope, authorization, market snapshots, generation
checks and cancellation outside the model.  A missing provider, stale quote,
missing authorization or expired generation is a recorded blocked cycle, not
an implicit approval or a silent model substitution.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import threading
import time
from typing import Any, Callable, Optional

from .ai_led_engine import (
    AIActionOutput,
    AICycleContext,
    AILedDecisionEngine,
    ALLOWED_AI_ACTIONS,
)
from .authorization import AuthorizationManager
from .execution_gateway import (
    ControlMode,
    DecisionPath,
    ExecutionGateway,
    TradingMode,
)
from .ledger import AccountLedger
from .position_guardian import PositionGuardian
from .risk_engine import RiskEngine
from ..evidence import EvidenceBundle, model_weight_digest, persist_evidence_bundle
from ..model_routing import DEFAULT_SMART_MODEL
from .account_scope import resolve_account_scope
from .ai_calibration import AICalibrationService
from .candidate_scanner import CandidateScanner
from .decision_memory import memory_for_prompt, record_decision_memory
from .institutional_schema import ensure_institutional_trader_schema
from .model_schemas import AI_ACTION_SCHEMA
from .account_aliases import canonical_account_id, GATE_TESTNET_ACCOUNT_ID
from .gate_account_truth import GateAccountTruthService
from .gate_accounts import build_gate_trader
from .ai_cycle_trace import humanize_reason, infer_block_stage

logger = logging.getLogger("core.trading.ai_session_coordinator")

AI_COORDINATOR_CONTRACT_VERSION = "ai_session_coordinator_v1"
AI_PROMPT_VERSION = "ai_led_qwen9b_cycle_v1"
MIN_CYCLE_INTERVAL_SECONDS = 60.0
DEFAULT_CYCLE_INTERVAL_SECONDS = 300.0
MAX_CYCLE_SYMBOLS = 5
MODEL_BUDGET_SECONDS = 120.0
INTENT_TTL_SECONDS = 240.0
MARKET_MAX_AGE_SECONDS = 120.0
SCAN_INTERVAL_SECONDS = 300.0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _as_utc(value)
    if value:
        try:
            return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            return None
    return None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


class AISessionCoordinator:
    """Bounded, cancellable Qwen 9B cycle owner for one trading session."""

    def __init__(
        self,
        *,
        store: Any,
        service: Any,
        session_manager: Any,
        ledger: AccountLedger,
        guardian: PositionGuardian,
        execution_gateway: ExecutionGateway | None = None,
        risk_engine: RiskEngine | None = None,
        model_provider: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        cycle_interval_seconds: float = DEFAULT_CYCLE_INTERVAL_SECONDS,
        model_budget_seconds: float = MODEL_BUDGET_SECONDS,
        calibration_min_bars: int = 500,
        calibration_lookback_days: int = 30,
    ) -> None:
        self.store = store
        self.service = service
        self.session_manager = session_manager
        self.ledger = ledger
        self.guardian = guardian
        self.gateway = execution_gateway or ExecutionGateway(store, ledger=ledger)
        self.risk_engine = risk_engine or RiskEngine(ledger)
        self.model_provider = model_provider if model_provider is not None else self._provider_from_service(service)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cycle_interval_seconds = max(MIN_CYCLE_INTERVAL_SECONDS, min(float(cycle_interval_seconds), 3600.0))
        self.model_budget_seconds = max(0.1, min(float(model_budget_seconds), MODEL_BUDGET_SECONDS))
        self.calibration_min_bars = max(1, int(calibration_min_bars))
        self.calibration_lookback_days = max(1, int(calibration_lookback_days))

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aima-qwen9b")
        self._account_id: str | None = None
        self._mode: TradingMode | None = None
        self._venue = "simulated"
        self._enabled = False
        self._last_reason = "not_started"
        self._last_error: str | None = None
        self._last_cycle_id: str | None = None
        self._last_cycle_status: str | None = None
        self._last_operational_state: str | None = None
        self._last_cycle_at: str | None = None
        self._health_cache: dict[str, Any] | None = None
        self._health_checked_at: datetime | None = None
        self._active_context: AICycleContext | None = None
        self._active_future: Future[Any] | None = None
        self._calibration = AICalibrationService(store, clock=self.clock)
        self._scanner = CandidateScanner(store, clock=self.clock)
        self._calibration_state = "NOT_STARTED"
        self._calibration_run: dict[str, Any] | None = None
        self._last_scheduled_at: str | None = None
        self._last_started_at: str | None = None
        self._last_completed_at: str | None = None
        self._last_lag_ms: float | None = None
        self._last_duration_ms: float | None = None
        self._next_scan_at: str | None = None
        self._candidate_count = 0
        self._ensure_tables()

    @staticmethod
    def _provider_from_service(service: Any) -> Any | None:
        provider = getattr(service, "llm_provider", None)
        if provider is not None:
            return provider
        smart = getattr(service, "smart", None)
        return getattr(smart, "llm_provider", None) if smart is not None else None

    def _ensure_tables(self) -> None:
        try:
            with self.store._connect() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS ai_led_cycles (
                        cycle_id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        latency_ms REAL,
                        order_intent_id TEXT,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(ai_led_cycles)").fetchall()}
                for column, definition in (("session_id", "TEXT"), ("generation", "INTEGER"), ("authorization_id", "TEXT"), ("market_snapshot_hash", "TEXT")):
                    if column not in columns:
                        db.execute(f"ALTER TABLE ai_led_cycles ADD COLUMN {column} {definition}")
                ensure_institutional_trader_schema(db)
        except Exception:
            logger.exception("could not initialize AI cycle persistence")

    def _account_record(self, account_id: str) -> dict[str, Any] | None:
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["config"] = json.loads(result.get("config_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["config"] = {}
        return result

    def _configure_scope(self, account_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
        if not account_id:
            return None, "ACCOUNT_REQUIRED"
        canonical = canonical_account_id(self.store, str(account_id))
        account = self._account_record(canonical)
        if account is None:
            return None, "ACCOUNT_NOT_FOUND"
        try:
            scope = resolve_account_scope(self.store, canonical)
            mode = TradingMode(str((scope or {}).get("mode") or account.get("mode", "")).upper())
        except ValueError:
            return None, "ACCOUNT_MODE_INVALID"
        config = account.get("config") if isinstance(account.get("config"), dict) else {}
        venue = str((scope or {}).get("venue") or config.get("provider") or config.get("venue") or ("simulated" if mode is TradingMode.PAPER else "gate")).strip() or "simulated"
        if scope:
            account["execution_scope"] = scope
        with self._lock:
            self._account_id = str(canonical)
            self._mode = mode
            self._venue = venue
            self._calibration_state = "CALIBRATING"
            self._calibration_run = None
        return account, None

    def _health(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            provider = self.model_provider
            checked = self._health_checked_at
            cached = dict(self._health_cache or {})
        now = _as_utc(self.clock())
        if not force:
            if cached and checked and (now - checked).total_seconds() < 5:
                return cached
            # Status/read endpoints must not perform an unbounded provider
            # health call.  A real health result is obtained at cycle start
            # with ``force=True`` and is then persisted in the coordinator
            # status; before that point the model is explicitly unverified.
            return {
                "status": "NOT_CHECKED",
                "provider": getattr(provider, "provider_name", provider.__class__.__name__) if provider is not None else None,
                "required_model": DEFAULT_SMART_MODEL,
                "available": None,
                "model_available": None,
                "reason_code": "MODEL_HEALTH_NOT_CHECKED",
                "checked_at": None,
            }
        if provider is None or not callable(getattr(provider, "health", None)):
            result = {
                "status": "UNAVAILABLE",
                "provider": None,
                "required_model": DEFAULT_SMART_MODEL,
                "available": False,
                "model_available": False,
                "reason_code": "SMART_MODEL_UNAVAILABLE",
                "checked_at": _iso(now),
            }
        else:
            try:
                try:
                    raw = provider.health(model_name=DEFAULT_SMART_MODEL)
                except TypeError:
                    raw = provider.health()
                raw = raw if isinstance(raw, dict) else {}
                models = raw.get("models") if isinstance(raw.get("models"), list) else []
                model_available = bool(raw.get("model_available")) and (
                    str(raw.get("model_id") or DEFAULT_SMART_MODEL) == DEFAULT_SMART_MODEL
                )
                model_available = model_available or DEFAULT_SMART_MODEL in {str(item) for item in models}
                available = bool(raw.get("available"))
                status = "READY" if available and model_available else "UNAVAILABLE"
                result = {
                    **raw,
                    "status": status,
                    "provider": raw.get("provider") or getattr(provider, "provider_name", provider.__class__.__name__),
                    "required_model": DEFAULT_SMART_MODEL,
                    "available": available,
                    "model_available": model_available,
                    "reason_code": None if status == "READY" else str(raw.get("error_code") or "SMART_MODEL_UNAVAILABLE"),
                    "checked_at": _iso(now),
                }
            except Exception as exc:
                result = {
                    "status": "UNAVAILABLE",
                    "provider": getattr(provider, "provider_name", provider.__class__.__name__),
                    "required_model": DEFAULT_SMART_MODEL,
                    "available": False,
                    "model_available": False,
                    "reason_code": "SMART_MODEL_UNAVAILABLE",
                    "error": f"{type(exc).__name__}: {exc}",
                    "checked_at": _iso(now),
                }
        with self._lock:
            self._health_cache = dict(result)
            self._health_checked_at = now
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            enabled = self._enabled
            account_id = self._account_id
            mode = self._mode.value if self._mode else None
            venue = self._venue
            reason = self._last_reason
            last_error = self._last_error
            last_cycle_id = self._last_cycle_id
            last_cycle_status = self._last_cycle_status
            last_cycle_at = self._last_cycle_at
            calibration_state = self._calibration_state
            calibration_run = dict(self._calibration_run or {}) if self._calibration_run else None
            last_scheduled_at = self._last_scheduled_at
            last_started_at = self._last_started_at
            last_completed_at = self._last_completed_at
            last_lag_ms = self._last_lag_ms
            last_duration_ms = self._last_duration_ms
            next_scan_at = self._next_scan_at
            candidate_count = self._candidate_count
        session = self.session_manager.status()
        health = self._health()
        if not enabled:
            state = "STOPPED"
        elif thread is not None and thread.is_alive():
            state = "RUNNING"
        else:
            state = "PAUSED"
        scope = resolve_account_scope(self.store, account_id) if account_id else None
        return {
            "contract_version": AI_COORDINATOR_CONTRACT_VERSION,
            "state": state,
            "enabled": enabled,
            "worker_alive": bool(thread and thread.is_alive()),
            "account_id": account_id,
            "venue": venue,
            "mode": mode,
            "provider": (scope or {}).get("provider") if scope else venue,
            "environment": (scope or {}).get("environment") if scope else (mode.lower() if mode else None),
            "session_id": session.get("session_id"),
            "generation": session.get("generation"),
            "session_state": session.get("state"),
            "cycle_interval_seconds": self.cycle_interval_seconds,
            "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
            "schedule": {
                "timezone": "UTC",
                "alignment": "minute % 5 == 0",
                "catch_up": False,
                "last_scheduled_at": last_scheduled_at,
                "last_started_at": last_started_at,
                "last_completed_at": last_completed_at,
                "last_lag_ms": last_lag_ms,
                "last_duration_ms": last_duration_ms,
                "next_scan_at": next_scan_at,
            },
            "max_symbols": MAX_CYCLE_SYMBOLS,
            "candidate_count": candidate_count,
            "calibration_state": calibration_state,
            "calibration_run": calibration_run,
            "model_budget_seconds": self.model_budget_seconds,
            "model": health,
            "last_reason": reason,
            "last_error": last_error,
            "last_cycle_id": last_cycle_id,
            "last_cycle_status": last_cycle_status,
            "last_operational_state": self._last_operational_state,
            "last_cycle_at": last_cycle_at,
        }

    def _authorization(self, account_id: str, mode: TradingMode, *, as_of: datetime) -> Any | None:
        manager = AuthorizationManager(self.store)
        return manager.get_active_authorization(account_id, mode, as_of=as_of)

    def _allowed_symbols(self, authorization: Any) -> tuple[str, ...]:
        allowed = [str(item).strip().upper() for item in getattr(authorization, "allowed_instruments", []) if str(item).strip() and str(item).strip() != "*"]
        allow_all = any(str(item).strip() == "*" for item in getattr(authorization, "allowed_instruments", []))
        policies = []
        try:
            policies = [str(item.get("instrument_id") or "").strip().upper() for item in self.store.list_monitoring_policies(enabled=True)]
        except Exception:
            policies = []
        ordered = []
        # A global monitoring policy may wake the coordinator, but it cannot
        # expand the instruments granted by the account-scoped authorization.
        policy_scope = [symbol for symbol in policies if symbol and (allow_all or symbol in allowed)]
        for symbol in policy_scope + allowed:
            if symbol and symbol not in ordered:
                ordered.append(symbol)
        return tuple(ordered[:MAX_CYCLE_SYMBOLS])

    def _market_snapshots(self, symbols: tuple[str, ...], *, now: datetime) -> dict[str, dict[str, Any]]:
        snapshots: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            raw: dict[str, Any] | None = None
            try:
                raw = self.store.get_realtime_state(symbol)
            except Exception:
                raw = None
            if raw and raw.get("price") is not None:
                snapshot = dict(raw)
                snapshot.setdefault("source", snapshot.get("provider", "realtime"))
            else:
                try:
                    bars = self.store.list_market_bars(symbol, "15m", limit=1)
                except Exception:
                    bars = []
                if not bars:
                    continue
                bar = dict(bars[-1])
                snapshot = {
                    "symbol": symbol,
                    "price": bar.get("close"),
                    "data_as_of": bar.get("data_as_of") or bar.get("bar_end"),
                    "received_at": bar.get("received_at") or _iso(now),
                    "source": bar.get("provider") or "market_bars",
                    "freshness_status": "fresh",
                }
            try:
                price = float(snapshot.get("price"))
                data_as_of = _parse_time(snapshot.get("data_as_of") or snapshot.get("timestamp") or snapshot.get("bar_end"))
                received_at = _parse_time(snapshot.get("received_at") or snapshot.get("updated_at"))
            except (TypeError, ValueError):
                continue
            if price <= 0 or data_as_of is None:
                continue
            age = (now - data_as_of).total_seconds()
            freshness = str(snapshot.get("freshness_status", "fresh")).lower()
            if (
                snapshot.get("fresh") is False
                or snapshot.get("stale") is True
                or snapshot.get("executable") is False
                or age < -5
                or age > MARKET_MAX_AGE_SECONDS
                or freshness in {"stale", "degraded", "unknown", "unavailable"}
            ):
                continue
            snapshot["price"] = price
            snapshot["last"] = price
            snapshot["data_as_of"] = _iso(data_as_of)
            snapshot["received_at"] = _iso(received_at or now)
            snapshot["fresh"] = True
            # Only the local PAPER simulator has an application-owned
            # matching contract.  A remote account must provide venue rules
            # through its adapter/market snapshot; otherwise the gateway
            # cannot size new risk without inventing a contract, fee, or
            # slippage assumption.
            with self._lock:
                scoped_mode = self._mode
            if scoped_mode is TradingMode.PAPER:
                if not isinstance(snapshot.get("market"), dict):
                    snapshot["market"] = {
                        "contractSize": 1.0,
                        "precision": {"amount": 0.001, "price": 0.01},
                        "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
                        "taker": 0.0005,
                    }
                snapshot.setdefault(
                    "market_contract_evidence",
                    {
                        "status": "DEFINED_LOCAL_PAPER_CONTRACT",
                        "source": "PAPER_SIMULATION_MARKET_CONTRACT",
                        "exchange_metadata": False,
                    },
                )
            snapshots[symbol] = snapshot
        return snapshots

    def _build_context(
        self,
        *,
        cycle_id: str,
        now: datetime,
        session_id: str,
        generation: int,
        account_id: str,
        mode: TradingMode,
        venue: str,
        authorization: Any,
        snapshots: dict[str, dict[str, Any]],
        candidates: list[dict[str, Any]] | None = None,
        calibration: dict[str, Any] | None = None,
        account_truth: dict[str, Any] | None = None,
        news_revisions: list[dict[str, Any]] | None = None,
        scheduled_at: str | None = None,
    ) -> AICycleContext:
        instruments = tuple(snapshots.keys()) or tuple(self._allowed_symbols(authorization))
        limits = getattr(authorization, "limits", {}) or {}
        max_risk = float(limits.get("max_single_risk_pct", 0.0025))
        max_risk = min(max_risk, 0.0025)
        scope = resolve_account_scope(self.store, account_id) or {}
        scope_environment = str(scope.get("environment") or mode.value).lower()
        observed_environments = sorted({str(item.get("market_data_environment") or item.get("environment") or "").lower() for item in snapshots.values() if isinstance(item, dict) and (item.get("market_data_environment") or item.get("environment"))})
        market_data_environment = observed_environments[0] if len(observed_environments) == 1 else ("MIXED" if observed_environments else None)
        quality_values = [item.get("data_quality") for item in snapshots.values() if isinstance(item, dict) and isinstance(item.get("data_quality"), dict)]
        quality_statuses = {str(item.get("status") or "UNKNOWN").upper() for item in quality_values}
        data_quality = {
            "status": "BLOCKED" if not snapshots else ("DEGRADED" if any(status not in {"READY", "AVAILABLE", "FRESH"} for status in quality_statuses) else "READY"),
            "source": "gate_native_rest" if any(str(item.get("source") or "").startswith("gate_native") or str(item.get("provider") or "").startswith("gate") for item in snapshots.values() if isinstance(item, dict)) else "runtime_market_snapshot",
            "environment": market_data_environment or scope_environment,
            "synthetic": any(bool(item.get("synthetic")) for item in snapshots.values() if isinstance(item, dict)),
            "symbols": list(instruments),
        }
        candidate_rows = list(candidates or [])
        strategy_states = {str(item.get("strategy_id")): str(item.get("status") or "UNKNOWN") for item in candidate_rows if isinstance(item, dict) and item.get("strategy_id")}
        strategy_readiness = {
            "status": "READY" if snapshots and candidate_rows and all(state not in {"ERROR", "DATA_BLOCKED", "UNAVAILABLE"} for state in strategy_states.values()) else ("DATA_BLOCKED" if not snapshots else "NO_CANDIDATE_EVIDENCE"),
            "strategies": strategy_states,
            "candidate_count": len(candidate_rows),
            "calibration_status": str((calibration or {}).get("status") or "UNKNOWN"),
        }
        indicator_snapshot_id = next((str(item.get("indicator_snapshot_id")) for item in snapshots.values() if isinstance(item, dict) and item.get("indicator_snapshot_id")), None)
        remote_positions = (account_truth or {}).get("positions") if isinstance(account_truth, dict) else None
        scoped_positions = (
            [dict(item) for item in remote_positions if isinstance(item, dict)]
            if str((account_truth or {}).get("status") or "").upper() == "AVAILABLE" and isinstance(remote_positions, list)
            else self.ledger.get_open_positions(account_id, venue=venue, mode=mode.value)
        )
        return AICycleContext(
            cycle_id=cycle_id,
            account_id=account_id,
            generation=generation,
            started_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
            allowed_instruments=instruments,
            max_risk_fraction=max_risk,
            mode=mode,
            venue=venue,
            environment=mode.value,
            provider=venue,
            execution_environment=scope_environment,
            session_id=session_id,
            authorization_id=getattr(authorization, "authorization_id", None),
            authorization_version=getattr(authorization, "version", None),
            lease_holder_id=getattr(self.gateway, "runtime_lease_holder_id", None),
            fencing_token=getattr(self.gateway, "runtime_fencing_token", None),
            market_snapshots=snapshots,
            positions=scoped_positions,
            candidates=candidate_rows,
            decision_memory=memory_for_prompt(self.store, account_id),
            calibration=dict(calibration or {}),
            market_data_environment=market_data_environment,
            data_quality=data_quality,
            strategy_readiness=strategy_readiness,
            indicator_snapshot_id=indicator_snapshot_id,
            account_truth=dict(account_truth or {}),
            news_revisions=list(news_revisions or []),
            scheduled_at=scheduled_at,
        )

    def _model_output(self, context: AICycleContext) -> AIActionOutput:
        provider = self.model_provider
        if provider is None or not callable(getattr(provider, "generate_json", None)):
            raise RuntimeError("SMART_MODEL_UNAVAILABLE")
        prompt_payload = {
            "account_id": context.account_id,
            "mode": context.mode.value if isinstance(context.mode, TradingMode) else str(context.mode),
            "venue": context.venue,
            "allowed_instruments": list(context.allowed_instruments),
            "account_truth": {
                "status": context.account_truth.get("status"),
                "source": context.account_truth.get("source"),
                "observed_at": context.account_truth.get("observed_at"),
                "snapshot_id": context.account_truth.get("snapshot_id"),
                "equity": context.account_truth.get("equity"),
                "available_margin": context.account_truth.get("available_margin"),
                "used_margin": context.account_truth.get("used_margin"),
                "unrealized_pnl": context.account_truth.get("unrealized_pnl"),
                "positions": context.account_truth.get("positions", []),
                "pending_orders": context.account_truth.get("pending_orders", []),
            },
            "market_snapshots": context.market_snapshots,
            "news_revisions": context.news_revisions,
            "positions": context.positions,
            "candidates": context.candidates,
            "calibration": context.calibration,
            "market_data_environment": context.market_data_environment,
            "data_quality": context.data_quality,
            "strategy_readiness": context.strategy_readiness,
            "indicator_snapshot_id": context.indicator_snapshot_id,
            "decision_memory": context.decision_memory[:20],
            "generation": context.generation,
        }
        provider_name = str(
            getattr(provider, "provider_name", None)
            or getattr(provider, "model_id", None)
            or provider.__class__.__name__
        )
        model_id = str(getattr(provider, "model_id", None) or DEFAULT_SMART_MODEL)
        model_version = str(
            getattr(provider, "model_version", None)
            or getattr(provider, "version", None)
            or model_id
        )
        with self._lock:
            health_snapshot = dict(self._health_cache or {})
        model_digest, model_digest_status = model_weight_digest(
            provider,
            health_result=health_snapshot,
            model_name=DEFAULT_SMART_MODEL,
        )
        model_quantization = str(
            health_snapshot.get("quantization")
            or getattr(provider, "quantization", None)
            or "UNKNOWN_NOT_PROVIDED"
        )
        model_inference_settings = {
            "temperature": 0.0,
            "context_length": getattr(provider, "context_length", None),
            "max_tokens": getattr(provider, "max_tokens", None),
            "think": getattr(provider, "think", None),
            "schema": "AI_ACTION_SCHEMA",
            "schema_enforcement": "REQUESTED_NATIVE_JSON_SCHEMA",
        }
        context.model_quantization = model_quantization
        context.model_inference_settings = model_inference_settings
        evidence_refs_list = (
            [f"market_snapshot:{symbol}:{_digest(snapshot)[:16]}" for symbol, snapshot in sorted(context.market_snapshots.items())]
            + ([f"authorization:{context.authorization_id}"] if context.authorization_id else [])
            + ([f"session:{context.session_id}:{context.generation}"] if context.session_id else [])
            + ([f"account_snapshot:{context.account_truth.get('snapshot_id')}"] if context.account_truth.get("snapshot_id") else [])
            + ([f"indicator_snapshot:{context.indicator_snapshot_id}"] if context.indicator_snapshot_id else [])
        )
        for candidate in context.candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("candidate_id"):
                evidence_refs_list.append(f"candidate:{candidate['candidate_id']}")
            candidate_refs = candidate.get("evidence_refs")
            if isinstance(candidate_refs, (list, tuple)):
                evidence_refs_list.extend(str(item) for item in candidate_refs if str(item).strip())
        for revision in context.news_revisions:
            if isinstance(revision, dict) and revision.get("revision_id"):
                evidence_refs_list.append(f"news_revision:{revision['revision_id']}")
        evidence_refs = tuple(dict.fromkeys(evidence_refs_list))
        bundle = EvidenceBundle.freeze(
            dataset_id=f"ai_cycle:{context.cycle_id}",
            as_of=context.started_at,
            expires_at=context.expires_at,
            payload={
                "prompt_inputs": prompt_payload,
                "evidence_refs": list(evidence_refs),
                "model": {
                    "provider": provider_name,
                    "model_id": model_id,
                    "model_version": model_version,
                    "weight_digest": model_digest,
                    "quantization": model_quantization,
                    "inference_settings": model_inference_settings,
                    "prompt_version": AI_PROMPT_VERSION,
                    "weight_digest_status": model_digest_status,
                },
            },
            bundle_id=f"bundle_{context.cycle_id}",
            references=evidence_refs,
            missing=[] if model_digest else ["model_weight_digest"],
            frozen_at=context.started_at,
        )
        persisted_bundle = persist_evidence_bundle(self.store, bundle)
        context.evidence_bundle_id = bundle.bundle_id
        context.evidence_status = str(persisted_bundle.get("status") or "FROZEN")
        context.evidence_refs = evidence_refs
        prompt_payload["evidence_bundle_id"] = context.evidence_bundle_id
        prompt_payload["evidence_refs"] = list(evidence_refs)
        input_hash = _digest(prompt_payload)
        # The model is never allowed to choose these facts.  They are
        # stamped onto the immutable cycle record by the coordinator.
        context.model_id = model_id
        context.model_version = model_version
        context.prompt_version = AI_PROMPT_VERSION
        context.input_hash = input_hash
        context.model_digest = model_digest
        context.model_digest_status = model_digest_status
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the AI_LED decision component for a locally authorized trading session. "
                    "Return JSON only with action WAIT, HOLD, OPEN_LONG, OPEN_SHORT, REDUCE_POSITION, "
                    "CLOSE_POSITION, or TIGHTEN_STOP; instrument_id, reason, and only the numeric fields "
                    "needed for that action. For an opening action include order_preference MARKET, LIMIT, or AUTO; "
                    "LIMIT may include limit_price and ttl_seconds from 60 to 300. "
                    "Never choose account, venue, mode, authorization, or client. "
                    "Never widen stops, reverse an open position, or invent missing market data. "
                    "Write reason and other human-readable explanations in Simplified Chinese. Keep JSON keys, action enums and instrument identifiers unchanged."
                ),
            },
            {"role": "user", "content": json.dumps(prompt_payload, sort_keys=True, ensure_ascii=True)},
        ]
        def call_model(call_messages: list[dict[str, str]], prompt_version: str) -> Any:
            started = time.perf_counter()
            try:
                result = provider.generate_json(
                    call_messages,
                    model_name=DEFAULT_SMART_MODEL,
                    prompt_version=prompt_version,
                    input_hash=input_hash,
                    temperature=0.0,
                    schema=AI_ACTION_SCHEMA,
                )
            finally:
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
                context.model_latency_ms = elapsed_ms
                context.model_call_prompt_version = prompt_version
            metadata = result[2] if isinstance(result, tuple) and len(result) > 2 and isinstance(result[2], dict) else {}
            returned_model = str(metadata.get("model_id") or "").strip()
            if returned_model and returned_model != DEFAULT_SMART_MODEL:
                raise ValueError("SMART_MODEL_MISMATCH")
            context.model_latency_ms = metadata.get("latency_ms", context.model_latency_ms)
            if metadata.get("schema_enforcement"):
                context.model_inference_settings["schema_enforcement"] = metadata["schema_enforcement"]
            context.model_raw_response = str(metadata.get("raw_response"))[:12000] if metadata.get("raw_response") is not None else None
            context.model_id = str(metadata.get("model_id") or context.model_id or DEFAULT_SMART_MODEL)
            context.model_version = str(metadata.get("model_version") or context.model_version or context.model_id)
            context.prompt_version = str(metadata.get("prompt_version") or prompt_version)
            return result

        response: Any = None
        try:
            response = call_model(messages, AI_PROMPT_VERSION)
            candidate_decoded = response[0] if isinstance(response, tuple) else response
            if not isinstance(candidate_decoded, dict) or set(candidate_decoded) - set(AI_ACTION_SCHEMA["properties"]):
                raise ValueError("INVALID_ACTION_SCHEMA")
        except Exception as first_error:
            # One and only one bounded repair attempt.  The retry is still
            # schema-constrained and cannot change account, authorization or
            # market facts stamped above.
            repair_messages = [
                {"role": "system", "content": "只返回符合给定 JSON Schema 的 JSON 对象；不得增加字段，不得输出解释。"},
                {"role": "user", "content": json.dumps({"schema": AI_ACTION_SCHEMA, "validation_error": str(first_error)[:240], "previous_response": (response[1] if isinstance(response, tuple) and len(response) > 1 else None), "inputs": prompt_payload}, ensure_ascii=False, sort_keys=True)},
            ]
            response = call_model(repair_messages, AI_PROMPT_VERSION + "_repair")
        decoded = response[0] if isinstance(response, tuple) else response
        if not isinstance(decoded, dict):
            raise ValueError("INVALID_MODEL_JSON")
        action = str(decoded.get("action", "")).strip().upper()
        if action not in ALLOWED_AI_ACTIONS:
            raise ValueError("INVALID_ACTION_SCHEMA")
        instrument = str(decoded.get("instrument_id") or "").strip().upper()
        if not instrument:
            instrument = context.allowed_instruments[0] if context.allowed_instruments else ""
        reason = str(decoded.get("reason") or "").strip()
        if not reason or len(reason) > 2000:
            raise ValueError("INVALID_ACTION_REASON")
        allowed_fields = {
            "action", "instrument_id", "reason", "position_id", "entry_condition",
            "entry_price", "stop_price", "take_profit", "requested_risk_fraction",
            "requested_leverage", "new_stop_price", "reduce_fraction", "evidence_refs",
            "order_preference", "limit_price", "ttl_seconds", "candidate_id", "closed_15m_bar",
            "strategy_candidate_id", "strategy_id", "market_summary", "timeframe_analysis",
            "strategy_analysis", "news_context", "entry_zone", "take_profit_1", "take_profit_2",
            "confidence", "invalidation_condition",
        }
        if set(decoded) - allowed_fields:
            raise ValueError("INVALID_ACTION_SCHEMA: unexpected model fields")
        values: dict[str, Any] = {}
        position_id = decoded.get("position_id")
        if position_id is not None:
            if not isinstance(position_id, str) or not position_id.strip() or len(position_id) > 160:
                raise ValueError("INVALID_POSITION_ID")
            values["position_id"] = position_id.strip()
        entry_condition = decoded.get("entry_condition")
        if entry_condition is not None:
            if not isinstance(entry_condition, str) or len(entry_condition) > 500:
                raise ValueError("INVALID_ENTRY_CONDITION")
            values["entry_condition"] = entry_condition
        numeric_fields = (
            "entry_price", "stop_price", "take_profit", "requested_risk_fraction",
            "new_stop_price", "reduce_fraction",
        )
        for field in numeric_fields:
            if field not in decoded or decoded[field] is None:
                continue
            try:
                number = float(decoded[field])
            except (TypeError, ValueError):
                raise ValueError(f"INVALID_{field.upper()}")
            if not math.isfinite(number):
                raise ValueError(f"INVALID_{field.upper()}")
            values[field] = number
        if "requested_leverage" in decoded and decoded["requested_leverage"] is not None:
            try:
                leverage = int(decoded["requested_leverage"])
            except (TypeError, ValueError):
                raise ValueError("INVALID_REQUESTED_LEVERAGE")
            if leverage < 1 or leverage > 100:
                raise ValueError("INVALID_REQUESTED_LEVERAGE")
            values["requested_leverage"] = leverage
        preference = decoded.get("order_preference", "AUTO")
        if not isinstance(preference, str) or preference.upper() not in {"MARKET", "LIMIT", "AUTO"}:
            raise ValueError("INVALID_ORDER_PREFERENCE")
        values["order_preference"] = preference.upper()
        for field in ("limit_price",):
            if field in decoded and decoded[field] is not None:
                try:
                    number = float(decoded[field])
                except (TypeError, ValueError):
                    raise ValueError(f"INVALID_{field.upper()}")
                if not math.isfinite(number) or number <= 0:
                    raise ValueError(f"INVALID_{field.upper()}")
                values[field] = number
        if "ttl_seconds" in decoded and decoded["ttl_seconds"] is not None:
            try:
                ttl = int(decoded["ttl_seconds"])
            except (TypeError, ValueError):
                raise ValueError("INVALID_TTL_SECONDS")
            if ttl < 60 or ttl > 300:
                raise ValueError("INVALID_TTL_SECONDS")
            values["ttl_seconds"] = ttl
        for field in ("candidate_id", "closed_15m_bar"):
            if field in decoded and decoded[field] is not None:
                if not isinstance(decoded[field], str) or len(decoded[field]) > 200:
                    raise ValueError(f"INVALID_{field.upper()}")
                values[field] = decoded[field]
        if values.get("candidate_id") is None and decoded.get("strategy_candidate_id") is not None:
            values["candidate_id"] = decoded["strategy_candidate_id"]
        evidence_refs = decoded.get("evidence_refs", [])
        if not isinstance(evidence_refs, list) or any(not isinstance(item, str) or len(item) > 200 for item in evidence_refs):
            raise ValueError("INVALID_EVIDENCE_REFS")
        allowed_evidence_refs = set(context.evidence_refs)
        if any(item not in allowed_evidence_refs for item in evidence_refs):
            raise ValueError("INVALID_EVIDENCE_REF")
        values["evidence_refs"] = tuple(evidence_refs[:32])
        extra_fields: dict[str, Any] = {}

        def bounded_text(field: str, maximum: int) -> None:
            value = decoded.get(field)
            if value is None:
                return
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"INVALID_{field.upper()}")
            extra_fields[field] = value

        bounded_text("strategy_candidate_id", 200)
        bounded_text("strategy_id", 100)
        bounded_text("market_summary", 1000)
        bounded_text("invalidation_condition", 800)
        for field in ("take_profit_1", "take_profit_2", "confidence"):
            if field not in decoded or decoded[field] is None:
                continue
            try:
                number = float(decoded[field])
            except (TypeError, ValueError):
                raise ValueError(f"INVALID_{field.upper()}")
            if not math.isfinite(number):
                raise ValueError(f"INVALID_{field.upper()}")
            if field == "confidence" and not 0 <= number <= 100:
                raise ValueError("INVALID_CONFIDENCE")
            if field.startswith("take_profit") and number <= 0:
                raise ValueError(f"INVALID_{field.upper()}")
            extra_fields[field] = number

        timeframe_analysis = decoded.get("timeframe_analysis")
        if timeframe_analysis is not None:
            if not isinstance(timeframe_analysis, dict) or set(timeframe_analysis) - {"15m", "1h"}:
                raise ValueError("INVALID_TIMEFRAME_ANALYSIS")
            extra_fields["timeframe_analysis"] = {}
            for key, value in timeframe_analysis.items():
                if value is not None and (not isinstance(value, str) or len(value) > 800):
                    raise ValueError("INVALID_TIMEFRAME_ANALYSIS")
                extra_fields["timeframe_analysis"][key] = value

        strategy_analysis = decoded.get("strategy_analysis")
        if strategy_analysis is not None:
            if not isinstance(strategy_analysis, dict) or set(strategy_analysis) - {"strategy_id", "matched_conditions", "missing_conditions", "trigger_completion_pct"}:
                raise ValueError("INVALID_STRATEGY_ANALYSIS")
            normalized_analysis: dict[str, Any] = {}
            if strategy_analysis.get("strategy_id") is not None:
                if not isinstance(strategy_analysis["strategy_id"], str) or len(strategy_analysis["strategy_id"]) > 100:
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                normalized_analysis["strategy_id"] = strategy_analysis["strategy_id"]
            for key in ("matched_conditions", "missing_conditions"):
                values_list = strategy_analysis.get(key, [])
                if not isinstance(values_list, list) or any(not isinstance(item, str) or len(item) > 300 for item in values_list):
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                normalized_analysis[key] = values_list[:32]
            if strategy_analysis.get("trigger_completion_pct") is not None:
                try:
                    completion = float(strategy_analysis["trigger_completion_pct"])
                except (TypeError, ValueError):
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                if not math.isfinite(completion) or not 0 <= completion <= 100:
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                normalized_analysis["trigger_completion_pct"] = completion
            extra_fields["strategy_analysis"] = normalized_analysis

        news_context = decoded.get("news_context")
        if news_context is not None:
            if not isinstance(news_context, dict) or set(news_context) - {"impact", "summary"}:
                raise ValueError("INVALID_NEWS_CONTEXT")
            impact = news_context.get("impact")
            if impact is not None and impact not in {"POSITIVE", "NEGATIVE", "NEUTRAL", "UNKNOWN"}:
                raise ValueError("INVALID_NEWS_CONTEXT")
            summary = news_context.get("summary")
            if summary is not None and (not isinstance(summary, str) or len(summary) > 800):
                raise ValueError("INVALID_NEWS_CONTEXT")
            extra_fields["news_context"] = {"impact": impact, "summary": summary}

        entry_zone = decoded.get("entry_zone")
        if entry_zone is not None:
            if not isinstance(entry_zone, dict) or set(entry_zone) - {"low", "high"}:
                raise ValueError("INVALID_ENTRY_ZONE")
            normalized_zone: dict[str, float | None] = {}
            for key in ("low", "high"):
                value = entry_zone.get(key)
                if value is None:
                    normalized_zone[key] = None
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ValueError("INVALID_ENTRY_ZONE")
                if not math.isfinite(number) or number <= 0:
                    raise ValueError("INVALID_ENTRY_ZONE")
                normalized_zone[key] = number
            if normalized_zone["low"] is not None and normalized_zone["high"] is not None and normalized_zone["low"] > normalized_zone["high"]:
                raise ValueError("INVALID_ENTRY_ZONE")
            extra_fields["entry_zone"] = normalized_zone

        if extra_fields:
            values["extra_fields"] = extra_fields
        return AIActionOutput(action=action, instrument_id=instrument, reason=reason, **values)

    def _cancel_model_generation(self, context: AICycleContext) -> None:
        provider = self.model_provider
        for name in ("cancel_generation", "cancel"):
            callback = getattr(provider, name, None)
            if callable(callback):
                try:
                    callback(context.cycle_id, context.generation)
                except TypeError:
                    try:
                        callback(context.cycle_id)
                    except Exception:
                        pass
                except Exception:
                    pass
                break

    def _cancel_active_generation(self) -> None:
        """Ask the provider to stop in-flight work before a lease/session loss.

        ThreadPoolExecutor cannot kill a provider call.  The explicit cancel
        hook is therefore best effort, while the session generation and
        gateway fence remain the authoritative commit boundary.
        """
        with self._lock:
            context = self._active_context
            future = self._active_future
        if future is not None:
            future.cancel()
        if context is not None:
            self._cancel_model_generation(context)

    @staticmethod
    def _aligned_scan_at(value: datetime) -> datetime:
        """Return the current closed 5-minute boundary in UTC."""
        point = _as_utc(value).replace(second=0, microsecond=0)
        return point.replace(minute=(point.minute // 5) * 5)

    @classmethod
    def _next_aligned_scan(cls, value: datetime) -> datetime:
        return cls._aligned_scan_at(value) + timedelta(minutes=5)

    def _calibrate_scope(
        self,
        *,
        account_id: str,
        venue: str,
        mode: TradingMode,
        session_id: str | None,
        symbols: tuple[str, ...],
        now: datetime,
    ) -> dict[str, Any]:
        scope = resolve_account_scope(self.store, account_id) or {}
        environment = str(scope.get("environment") or mode.value).lower()
        active = self._calibration.active_profile(account_id, environment=environment, now=now)
        if active is not None:
            result = {
                "status": "READY",
                "profile_id": active.get("profile_id"),
                "profile": active.get("profile") or {},
                "model_digest": active.get("model_digest"),
                "digest_status": active.get("digest_status"),
                "sample_size": active.get("sample_size"),
                "expires_at": active.get("expires_at"),
            }
            with self._lock:
                self._calibration_state = "READY"
                self._calibration_run = result
            return result
        with self._lock:
            self._calibration_state = "CALIBRATING"
        result = self._calibration.run(
            account_id=account_id,
            provider=venue,
            environment=environment,
            session_id=session_id,
            symbols=symbols,
            model_provider=self.model_provider,
            now=now,
            min_bars=self.calibration_min_bars,
            lookback_days=self.calibration_lookback_days,
        )
        with self._lock:
            self._calibration_state = "READY" if result.get("status") == "READY" else "NOT_READY"
            self._calibration_run = dict(result)
        return result

    def _news_revisions(self, symbols: tuple[str, ...], *, now: datetime) -> list[dict[str, Any]]:
        """Load only point-in-time stored event revisions for the model context."""

        if not callable(getattr(self.store, "list_event_evidence", None)):
            return []
        revisions: list[dict[str, Any]] = []
        published_since = _iso(now - timedelta(hours=48))
        as_of = _iso(now)
        for symbol in symbols:
            try:
                rows = self.store.list_event_evidence(
                    symbol=symbol,
                    as_of=as_of,
                    published_since=published_since,
                    limit=20,
                )
            except Exception:
                rows = []
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                revision_id = item.get("revision_id") or item.get("event_id") or item.get("news_id")
                if not revision_id:
                    continue
                revisions.append(
                    {
                        "revision_id": str(revision_id),
                        "symbol": symbol,
                        "title": str(item.get("title") or item.get("headline") or "")[:320],
                        "published_at": item.get("published_at"),
                        "known_at": item.get("known_at"),
                        "source": str(item.get("source") or item.get("publisher") or "")[:160],
                        "impact": str(item.get("impact") or item.get("sentiment") or "UNKNOWN").upper(),
                    }
                )
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in revisions:
            identity = str(item["revision_id"])
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(item)
        return deduped[:64]

    def _blocked_cycle(self, context: AICycleContext, reason: str) -> Any:
        block_stage = infer_block_stage(reason, getattr(context, "stage_block", None))
        output = AIActionOutput(
            action="WAIT",
            instrument_id=context.allowed_instruments[0] if context.allowed_instruments else "",
            reason=reason,
            decision_origin="SYSTEM",
            extra_fields={
                "is_model_decision": False,
                "model_called": False,
                "model_result": "NOT_RUN",
                "block_stage": block_stage,
                "operational_state": "SYSTEM_BLOCKED",
                "blocked_code": str(reason).split(":", 1)[0],
                "human_message": humanize_reason(reason, stage=block_stage),
            },
        )
        engine = AILedDecisionEngine(
            store=self.store,
            execution_gateway=self.gateway,
            risk_engine=self.risk_engine,
            ledger=self.ledger,
            guardian=self.guardian,
            model_runner=None,
        )
        return engine.execute_cycle(context, now=self.clock(), model_output=output)

    def _refresh_remote_account_truth(self, account_id: str, mode: TradingMode) -> dict[str, Any]:
        """Read Gate TestNet facts before calibration or model generation.

        A TestNet cycle cannot use the local ledger as an account substitute.
        This method intentionally returns a blocked-shaped fact on every
        failure; the caller decides whether to persist a system-blocked cycle.
        """

        scope = resolve_account_scope(self.store, account_id) or {}
        if mode is not TradingMode.TESTNET or str(scope.get("account_type") or "").upper() != "GATE_TESTNET":
            return {}
        truth_service = GateAccountTruthService(self.store, clock=self.clock)
        try:
            trader = build_gate_trader(self.store, account_id)
        except Exception:
            trader = None
        if trader is None:
            return truth_service.refresh(account_id, object(), include_trades=False)
        return truth_service.refresh(account_id, trader, include_trades=False)

    def run_cycle_once(self, scheduled_at: str | datetime | None = None) -> Any:
        """Run one complete aligned cycle; manual callers may provide a schedule."""
        with self._lock:
            account_id = self._account_id
            mode = self._mode
            venue = self._venue
            enabled = self._enabled
        session = self.session_manager.status()
        now = _as_utc(self.clock())
        scheduled_point = _parse_time(scheduled_at) if scheduled_at is not None else self._aligned_scan_at(now)
        scheduled_point = scheduled_point or self._aligned_scan_at(now)
        scheduled_text = _iso(scheduled_point)
        cycle_started = now
        with self._lock:
            self._last_scheduled_at = scheduled_text
            self._last_started_at = _iso(cycle_started)
            self._next_scan_at = _iso(self._next_aligned_scan(now))
            self._last_lag_ms = max(0.0, (now - scheduled_point).total_seconds() * 1000.0)
        cycle_id = f"cycle_{now.strftime('%Y%m%dT%H%M%S%fZ')}_{threading.get_ident()}"
        if not enabled or not account_id or mode is None:
            missing_account = not account_id
            account_id = account_id or "unconfigured"
            context = AICycleContext(
                cycle_id=cycle_id,
                account_id=account_id,
                generation=int(session.get("generation") or 0),
                started_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
                allowed_instruments=(),
                mode=mode or TradingMode.PAPER,
                venue=venue,
                session_id=session.get("session_id"),
            )
            result = self._blocked_cycle(context, "ACCOUNT_REQUIRED" if missing_account else "AI_SESSION_NOT_ENABLED")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        valid, validation_reason = self.session_manager.validate_execution(str(session.get("session_id")), int(session.get("generation") or 0))
        if not valid:
            context = AICycleContext(
                cycle_id=cycle_id,
                account_id=account_id,
                generation=int(session.get("generation") or 0),
                started_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
                allowed_instruments=(),
                mode=mode,
                venue=venue,
                session_id=session.get("session_id"),
            )
            result = self._blocked_cycle(context, f"SESSION_NOT_EXECUTABLE: {validation_reason}")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        authorization = self._authorization(account_id, mode, as_of=now)
        if authorization is None:
            context = AICycleContext(
                cycle_id=cycle_id,
                account_id=account_id,
                generation=int(session.get("generation") or 0),
                started_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
                allowed_instruments=(),
                mode=mode,
                venue=venue,
                session_id=session.get("session_id"),
            )
            result = self._blocked_cycle(context, "AUTHORIZATION_REQUIRED")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        # The coordinator is an AI_LED execution owner.  A strategy-scoped
        # authorization, another venue, or another account is not a valid
        # substitute and must be recorded as a blocked cycle.
        auth_account = str(getattr(authorization, "account_id", ""))
        auth_mode = str(getattr(authorization, "mode", "")).upper()
        auth_venue = str(getattr(authorization, "venue", "")).strip().lower()
        auth_path = str(getattr(authorization, "decision_path", "")).upper()
        if (
            auth_account != account_id
            or auth_mode != mode.value
            or auth_venue != venue.lower()
            or auth_path != DecisionPath.AI_LED.value
        ):
            context = AICycleContext(
                cycle_id=cycle_id,
                account_id=account_id,
                generation=int(session.get("generation") or 0),
                started_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
                allowed_instruments=(),
                mode=mode,
                venue=venue,
                session_id=session.get("session_id"),
                authorization_id=getattr(authorization, "authorization_id", None),
                authorization_version=getattr(authorization, "version", None),
            )
            result = self._blocked_cycle(context, "AUTHORIZATION_SCOPE_MISMATCH")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        # Gate TestNet is a remote-account execution environment.  Perform
        # this read-only truth check before calibration/model calls so a
        # missing credential or degraded private response cannot be turned
        # into a model WAIT or a local 10000-unit risk budget.
        account_truth = self._refresh_remote_account_truth(account_id, mode)
        if mode is TradingMode.LIVE:
            context = AICycleContext(
                cycle_id=cycle_id,
                account_id=account_id,
                generation=int(session.get("generation") or 0),
                started_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
                allowed_instruments=(),
                mode=mode,
                venue=venue,
                session_id=session.get("session_id"),
                authorization_id=getattr(authorization, "authorization_id", None),
                authorization_version=getattr(authorization, "version", None),
                account_truth=account_truth,
                stage_block="ACCOUNT",
            )
            result = self._blocked_cycle(context, "LIVE_EXECUTION_LOCKED")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        if mode is TradingMode.TESTNET and (
            str(account_truth.get("status") or "").upper() != "AVAILABLE"
            or account_truth.get("equity") is None
            or account_truth.get("available_margin") is None
        ):
            context = self._build_context(
                cycle_id=cycle_id,
                now=now,
                session_id=str(session.get("session_id")),
                generation=int(session.get("generation") or 0),
                account_id=account_id,
                mode=mode,
                venue=venue,
                authorization=authorization,
                snapshots={},
                candidates=[],
                calibration={},
                account_truth=account_truth,
                scheduled_at=scheduled_text,
            )
            reason = str(account_truth.get("error_code") or "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE")
            result = self._blocked_cycle(context, reason)
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        # Probe the required Smart model before calibration.  This prevents a
        # provider whose normal default is qwen3.5:4b from making any
        # calibration call when qwen3.5:9b is unavailable.
        health = self._health(force=True)
        if health.get("status") != "READY":
            context = self._build_context(
                cycle_id=cycle_id,
                now=now,
                session_id=str(session.get("session_id")),
                generation=int(session.get("generation") or 0),
                account_id=account_id,
                mode=mode,
                venue=venue,
                authorization=authorization,
                snapshots={},
                candidates=[],
                calibration={},
                scheduled_at=scheduled_text,
                account_truth=account_truth,
            )
            result = self._blocked_cycle(context, str(health.get("reason_code") or "SMART_MODEL_UNAVAILABLE"))
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        symbols = self._allowed_symbols(authorization)
        calibration = self._calibrate_scope(
            account_id=account_id,
            venue=venue,
            mode=mode,
            session_id=str(session.get("session_id") or "") or None,
            symbols=symbols,
            now=now,
        )
        if calibration.get("status") != "READY":
            context = self._build_context(
                cycle_id=cycle_id,
                now=now,
                session_id=str(session.get("session_id")),
                generation=int(session.get("generation") or 0),
                account_id=account_id,
                mode=mode,
                venue=venue,
                authorization=authorization,
                snapshots={},
                calibration=calibration,
                scheduled_at=scheduled_text,
                account_truth=account_truth,
            )
            result = self._blocked_cycle(context, str(calibration.get("error_code") or "CALIBRATION_NOT_READY"))
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        candidates = self._scanner.scan(
            account_id=account_id,
            provider=venue,
            environment=str((resolve_account_scope(self.store, account_id) or {}).get("environment") or mode.value).lower(),
            symbols=symbols,
            now=now,
            calibration_profile=calibration,
        )
        with self._lock:
            self._candidate_count = len(candidates)
        snapshots = self._market_snapshots(symbols, now=now)
        news_revisions = self._news_revisions(symbols, now=now)
        context = self._build_context(
            cycle_id=cycle_id,
            now=now,
            session_id=str(session.get("session_id")),
            generation=int(session.get("generation") or 0),
            account_id=account_id,
            mode=mode,
            venue=venue,
            authorization=authorization,
            snapshots=snapshots,
            candidates=candidates,
            calibration=calibration,
            account_truth=account_truth,
            news_revisions=news_revisions,
            scheduled_at=scheduled_text,
        )
        if not snapshots:
            result = self._blocked_cycle(context, "MARKET_DATA_UNAVAILABLE")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        health = self._health(force=True)
        if health.get("status") != "READY":
            result = self._blocked_cycle(context, str(health.get("reason_code") or "SMART_MODEL_UNAVAILABLE"))
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        future: Future[Any] = self._executor.submit(self._model_output, context)
        with self._lock:
            self._active_context = context
            self._active_future = future
        try:
            output = future.result(timeout=min(self.model_budget_seconds, INTENT_TTL_SECONDS))
        except FutureTimeout:
            future.cancel()
            self._cancel_model_generation(context)
            result = self._blocked_cycle(context, "MODEL_TIMEOUT_DISCARDED")
            result.status = "TIMEOUT_DISCARDED"
            self._correct_persisted_outcome(result)
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        except Exception as exc:
            future.cancel()
            result = self._blocked_cycle(context, f"SMART_MODEL_UNAVAILABLE: {type(exc).__name__}: {exc}")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        finally:
            with self._lock:
                if self._active_future is future:
                    self._active_future = None
                    self._active_context = None

        valid, validation_reason = self.session_manager.validate_execution(context.session_id or "", context.generation)
        if not valid:
            result = self._blocked_cycle(context, f"STALE_GENERATION_DISCARDED: {validation_reason}")
            result.status = "TIMEOUT_DISCARDED"
            self._correct_persisted_outcome(result)
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        if self._authorization(account_id, mode, as_of=_as_utc(self.clock())) is None:
            result = self._blocked_cycle(context, "AUTHORIZATION_REVOKED_BEFORE_EXECUTION")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        engine = AILedDecisionEngine(
            store=self.store,
            execution_gateway=self.gateway,
            risk_engine=self.risk_engine,
            ledger=self.ledger,
            guardian=self.guardian,
            model_runner=None,
        )
        result = engine.execute_cycle(context, now=_as_utc(self.clock()), model_output=output)
        self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
        return result

    def _record_result(
        self,
        result: Any,
        *,
        context: AICycleContext | None = None,
        scheduled_at: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        completed = _as_utc(self.clock())
        started = started_at or (_parse_time(context.started_at) if context else completed) or completed
        duration_ms = max(0.0, (completed - started).total_seconds() * 1000.0)
        with self._lock:
            self._last_cycle_id = getattr(result, "cycle_id", None)
            self._last_cycle_status = getattr(result, "status", None)
            self._last_operational_state = getattr(result, "operational_state", None)
            self._last_cycle_at = _iso(completed)
            self._last_completed_at = _iso(completed)
            self._last_duration_ms = duration_ms
            self._last_scheduled_at = scheduled_at or self._last_scheduled_at
            self._last_reason = str(getattr(result, "reason", "cycle_complete"))
            if getattr(result, "status", "") in {"BLOCKED", "REJECTED", "TIMEOUT_DISCARDED"}:
                self._last_error = self._last_reason
            else:
                self._last_error = None
            account_id = self._account_id
            mode = self._mode.value if self._mode else None
            venue = self._venue
        if context is None or not account_id or account_id == "unconfigured":
            return
        scope = resolve_account_scope(self.store, account_id) or {}
        environment = str(scope.get("environment") or context.execution_environment or mode or "unknown").lower()
        output = getattr(result, "action_output", None)
        intent = getattr(result, "order_intent", None)
        candidate_id = getattr(intent, "candidate_id", None) if intent is not None else getattr(output, "candidate_id", None)
        symbol = getattr(output, "instrument_id", None)
        memory = None
        if str(getattr(result, "decision_origin", "MODEL")).upper() == "MODEL":
            try:
                memory = record_decision_memory(
                    self.store,
                    account_id=account_id,
                    provider=str(scope.get("provider") or context.provider or venue),
                    environment=environment,
                    cycle_id=str(getattr(result, "cycle_id", "")),
                    session_id=context.session_id,
                    candidate_id=candidate_id,
                    symbol=symbol,
                    action=str(getattr(output, "action", "WAIT")),
                    cycle_status=str(getattr(result, "status", "UNKNOWN")),
                    decision_at=completed,
                    reason=str(getattr(result, "reason", "")),
                    payload={
                        "authorization_id": context.authorization_id,
                        "model_id": context.model_id,
                        "model_digest_status": context.model_digest_status,
                        "execution_intent_id": getattr(intent, "intent_id", None),
                        "order_preference": getattr(output, "order_preference", "AUTO"),
                    },
                    decision_origin="MODEL",
                )
            except Exception:
                logger.warning("Failed to persist AI decision memory", exc_info=True)
        else:
            try:
                self.store.record_runtime_diagnostic(
                    state=str(getattr(result, "operational_state", "PRECHECK_BLOCKED")),
                    code=str(getattr(result, "reason", "PRECHECK_BLOCKED")).split(":", 1)[0],
                    account_id=account_id,
                    session_id=context.session_id,
                    cycle_id=str(getattr(result, "cycle_id", "")),
                    payload={"reason": str(getattr(result, "reason", "")), "decision_origin": getattr(result, "decision_origin", None)},
                    occurred_at=completed,
                )
            except Exception:
                logger.warning("Failed to persist AI runtime diagnostic", exc_info=True)
        if hasattr(self.store, "_connect") and getattr(result, "cycle_id", None):
            try:
                with self.store._connect() as db:
                    db.execute(
                        """UPDATE ai_led_cycles SET completed_at=?, duration_ms=?, lag_ms=?,
                           scheduled_at=?, next_scan_at=?, decision_memory_id=? WHERE cycle_id=?""",
                        (_iso(completed), duration_ms, self._last_lag_ms, scheduled_at or self._last_scheduled_at, self._next_scan_at, memory.get("memory_id") if isinstance(memory, dict) else None, str(result.cycle_id)),
                    )
            except Exception:
                logger.warning("Failed to persist AI cycle timing", exc_info=True)

    def _correct_persisted_outcome(self, result: Any) -> None:
        """Keep the durable cycle row aligned with a coordinator discard.

        ``AILedDecisionEngine`` persists a safe WAIT before this coordinator
        knows whether the model future timed out or became stale.  The final
        lifecycle outcome is still a durable fact and must not remain
        misleadingly recorded as ``WAITING``.
        """
        cycle_id = getattr(result, "cycle_id", None)
        if not cycle_id or not hasattr(self.store, "_connect"):
            return
        try:
            with self.store._connect() as db:
                db.execute(
                    "UPDATE ai_led_cycles SET status=?, reason=? WHERE cycle_id=?",
                    (
                        str(getattr(result, "status", "UNKNOWN")),
                        str(getattr(result, "reason", "")),
                        str(cycle_id),
                    ),
                )
        except Exception:
            logger.warning("Failed to correct persisted AI cycle outcome", exc_info=True)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            now = _as_utc(self.clock())
            next_boundary = self._next_aligned_scan(now)
            with self._lock:
                self._next_scan_at = _iso(next_boundary)
            # The scheduler has no catch-up mode: if startup misses a close,
            # wait for the next 5-minute UTC boundary instead of replaying a
            # burst of stale cycles.
            wait_seconds = max(0.0, (next_boundary - now).total_seconds())
            if self._wake_event.wait(wait_seconds):
                self._wake_event.clear()
                continue
            if self._stop_event.is_set():
                break
            try:
                self.run_cycle_once(scheduled_at=next_boundary)
            except Exception as exc:
                logger.exception("AI cycle failed")
                with self._lock:
                    self._last_reason = "cycle_failed"
                    self._last_error = f"{type(exc).__name__}: {exc}"

    def start(self, *, account_id: str | None = None) -> dict[str, Any]:
        account, error = self._configure_scope(account_id or self._account_id)
        if error:
            with self._lock:
                self._enabled = False
                self._last_reason = error
                self._last_error = error
            return self.status()
        del account
        with self._lock:
            session = self.session_manager.status()
            self._enabled = True
            self._stop_event.clear()
            self._wake_event.clear()
            if self._thread is not None and self._thread.is_alive():
                self._last_reason = "already_active"
                return self.status()
            self._last_reason = "explicit_start"
            self._last_error = None
            self._thread = threading.Thread(target=self._loop, name="aima-ai-led-coordinator", daemon=True)
            self._thread.start()
            return self.status()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
            self._enabled = False
            self._last_reason = "explicit_pause"
        self._cancel_active_generation()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
            self._enabled = False
            self._last_reason = "explicit_terminate"
        self._cancel_active_generation()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return self.status()

    def close(self) -> None:
        self.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["AISessionCoordinator", "AI_COORDINATOR_CONTRACT_VERSION"]
