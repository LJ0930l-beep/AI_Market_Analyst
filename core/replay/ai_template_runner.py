"""Continuous, isolated account replay of the production AI template contract.

Historical data and all fills remain in the supplied frozen history and replay
database.  Only inference may contact the local production Gemini model.  A
model invocation is durably claimed before it starts: interrupted claims are
never silently called again.  This is research simulation, not Gate settlement.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import urlopen

from core.model_routing import DEFAULT_SMART_MODEL, is_configured_model_identity
from core.config import DEFAULT_BASE_URL
from core.ai.transport_diagnostics import safe_transport_trace
from core.storage import SQLiteStore
from core.trading.ai_led_engine import AICycleContext, AIActionOutput, AILedDecisionEngine
from core.trading.ai_session_coordinator import AI_PROMPT_VERSION, AISessionCoordinator
from core.trading.ai_strategy_book import TEMPLATES, _PA_RESEARCH_SEED_INSTRUCTION
from core.trading.autonomous_strategy import CONTRACT, technical_context
from core.trading.execution_gateway import GatewayError, OrderStatus, TradingMode
from core.trading.order_selection import DEFAULT_LIMIT_TTL_SECONDS
from core.trading.strategy_execution import normalize_execution


SCHEMA_VERSION = "ai_template_account_replay_v2_gemini"
TEMPLATE_IDS = tuple(str(item["id"]) for item in TEMPLATES)
MAKER_FEE_RATE = 0.0002
TAKER_FEE_RATE = 0.00075
SOURCE_FILES = (
    "core/replay/ai_template_runner.py", "core/replay/ai_history.py", "core/replay/ai_simulation.py",
    "core/replay/gemini_research.py", "core/config.py",
    "core/replay/relay_policy.py", "core/ai/transport_diagnostics.py", "core/replay/settled_funding.py",
    "scripts/run_gemini_year_research.py", "scripts/verify_ai_template_replay.py",
    "scripts/run_gemini_heldout_research.py", "scripts/analyze_gemini_research.py",
    "scripts/export_gemini_response_files.py",
    "core/trading/ai_session_coordinator.py", "core/trading/autonomous_strategy.py",
    "core/trading/model_schemas.py", "core/trading/ai_led_engine.py", "core/trading/ai_strategy_book.py",
    "core/trading/price_action_structure.py",
    "core/trading/strategy_execution.py", "core/trading/execution_gateway.py", "core/trading/order_selection.py",
    "core/trading/fin_dataset_collector.py",
    "core/trading/candidate_scanner.py", "core/trading/nofx_strategy_adapter.py", "core/strategy_monitoring.py",
    "core/ai/ollama.py", "core/model_client.py", "core/model_routing.py", "core/completion_stream.py",
)


def _time(value: datetime | str) -> datetime:
    point = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if point.tzinfo is None:
        raise ValueError("REPLAY_TIMEZONE_REQUIRED")
    return point.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def frozen_source_fingerprint() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


class ReplayPaused(RuntimeError):
    """The run is resumable and must yield to the live model workload."""


class ReplayModelIdentityMismatch(ValueError):
    """Never execute or compare decisions served by unpinned weights."""


class ReplayModelUnavailable(RuntimeError):
    """A failed transport skips this scan; it never authorizes a decision."""

    def __init__(self, message: str, *, transport_trace: Any = None):
        super().__init__(message)
        self.transport_trace = safe_transport_trace(transport_trace)


def _record_health_failure(context: AICycleContext, exc: Exception, stage: str) -> None:
    """Keep probe diagnostics separate from decision inference receipts."""
    if not isinstance(exc, ReplayModelUnavailable):
        return
    settings = dict(context.model_inference_settings or {})
    record = {"stage": stage, "error_type": type(exc).__name__,
              "scope": "HEALTH_PROBE_FAILURE_NOT_DECISION_COMPLETION"}
    proof = safe_transport_trace(getattr(exc, "transport_trace", None))
    if proof is not None:
        record["transport_trace"] = proof
    settings["replay_health_failure"] = record
    context.model_inference_settings = settings


_HEALTH_TRANSPORT_ERRORS = frozenset({
    "ModelTimeoutError", "TimeoutError", "URLError", "ConnectionError", "ConnectionRefusedError",
})


def _minute_ceiling(point: datetime) -> datetime:
    rounded = point.replace(second=0, microsecond=0)
    return rounded + timedelta(minutes=1) if rounded < point else rounded


def _production_model_pin(provider: Any, *, deadline_monotonic: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        options: dict[str, Any] = {"model_name": DEFAULT_SMART_MODEL}
        if deadline_monotonic is not None:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("REPLAY_MODEL_HEALTH_DEADLINE_EXCEEDED")
            options["timeout_sec"] = min(30.0, remaining)
        health = provider.health(**options)
    except Exception as exc:
        if type(exc).__name__ in _HEALTH_TRANSPORT_ERRORS:
            raise ReplayModelUnavailable("REPLAY_MODEL_HEALTH_TRANSPORT_UNAVAILABLE:" + type(exc).__name__,
                transport_trace=getattr(exc, "transport_trace", None)) from exc
        raise ReplayModelIdentityMismatch("REPLAY_PRODUCTION_MODEL_IDENTITY_UNAVAILABLE") from exc
    if not isinstance(health, dict) or not health.get("available") or not health.get("model_available"):
        if isinstance(health, dict) and health.get("error_type") in _HEALTH_TRANSPORT_ERRORS:
            raise ReplayModelUnavailable("REPLAY_MODEL_HEALTH_TRANSPORT_UNAVAILABLE:" + str(health["error_type"]),
                transport_trace=health.get("transport_trace"))
        raise ReplayModelIdentityMismatch("REPLAY_PRODUCTION_MODEL_IDENTITY_UNAVAILABLE")
    actual = str(health.get("actual_model_id") or "").strip()
    try:
        context_length = int(health.get("context_length") or 0)
    except (TypeError, ValueError):
        context_length = 0
    if (not is_configured_model_identity(actual) or context_length <= 0
            or health.get("model_identity_source") != "completion_probe"
            or health.get("weight_digest") is not None
            or health.get("digest_status") != "REMOTE_WEIGHTS_NOT_EXPOSED"):
        raise ReplayModelIdentityMismatch("REPLAY_PRODUCTION_MODEL_IDENTITY_INCOMPLETE")
    provider.context_length = context_length
    return {"actual_model_id": actual, "model_id": DEFAULT_SMART_MODEL,
            "weight_digest": None, "digest_status": "REMOTE_WEIGHTS_NOT_EXPOSED",
            "context_length": context_length, "context_length_source": "APPLICATION_INPUT_BUDGET",
            "endpoint": DEFAULT_BASE_URL, "reasoning_effort": "high"}, health


def _model_wall_elapsed(started: float, *, production: bool) -> float:
    # Deterministic injected tests have zero simulated inference latency.
    # Real runs measure the complete prompt/schema/repair path, not merely a
    # provider's reported first HTTP request duration.
    elapsed = time.monotonic() - started
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("REPLAY_MODEL_WALL_CLOCK_INVALID")
    return elapsed if production else 0.0


def _verify_model_receipt(context: AICycleContext, pin: dict[str, Any]) -> None:
    settings = context.model_inference_settings or {}
    if (not context.model_call_completed
            or settings.get("actual_model_id") != pin["actual_model_id"]
            or settings.get("context_length") != pin["context_length"]
            or settings.get("verified_manifest_model_id") != pin["model_id"]
            or settings.get("model_identity_source") != "completion_response"
            or context.model_digest is not None):
        raise ReplayModelIdentityMismatch("REPLAY_MODEL_RECEIPT_IDENTITY_MISMATCH")


def _is_connection_refused(exc: BaseException) -> bool:
    # URLError wraps socket errors in ``reason``. Timeouts, HTTP failures,
    # malformed status and other transport errors remain fail-closed.
    cause = getattr(exc, "reason", None)
    if isinstance(cause, BaseException) and cause is not exc:
        return _is_connection_refused(cause)
    return isinstance(exc, ConnectionRefusedError) or (
        isinstance(exc, OSError) and (getattr(exc, "errno", None) == errno.ECONNREFUSED or getattr(exc, "winerror", None) == 10061)
    )


def production_model_idle(status: dict[str, Any], *, budget_seconds: float, now: datetime | None = None) -> tuple[bool, str]:
    """Observe the production scheduler; never pause or change its state."""
    observed = _time(now or datetime.now(timezone.utc))
    session = status.get("ai_session", status)
    if not isinstance(session, dict) or not session:
        return False, "PRODUCTION_MODEL_PRIORITY_STATUS_UNAVAILABLE"
    state = str(session.get("state") or session.get("status") or "").upper()
    if state in {"RUNTIME_UNAVAILABLE", "UNKNOWN", ""}:
        return False, "PRODUCTION_MODEL_PRIORITY_STATUS_UNAVAILABLE"
    if not session.get("enabled") and state in {"STOPPED", "PAUSED"}:
        return True, "PRODUCTION_AI_SESSION_IDLE"
    schedule = session.get("schedule") or {}
    try:
        started = _time(schedule["last_started_at"]) if schedule.get("last_started_at") else None
        completed = _time(schedule["last_completed_at"]) if schedule.get("last_completed_at") else None
        next_scan = _time(schedule["next_scan_at"]) if schedule.get("next_scan_at") else None
    except (ValueError, TypeError):
        return False, "PRODUCTION_SCHEDULE_INVALID"
    if started is not None and (completed is None or completed < started):
        return False, "PRODUCTION_MODEL_CYCLE_IN_PROGRESS"
    if str(session.get("calibration_state") or "").upper() in {"RUNNING", "CALIBRATING"}:
        return False, "PRODUCTION_CALIBRATION_IN_PROGRESS"
    if next_scan is None or (next_scan - observed).total_seconds() <= float(budget_seconds) + 20:
        return False, "PRODUCTION_NEXT_SCAN_HAS_PRIORITY"
    return True, "PRODUCTION_SCAN_WINDOW_IDLE"


def runtime_priority_guard(url: str, *, budget_seconds: float, allow_stopped_runtime: bool = False) -> Callable[[], None]:
    parsed = urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("REPLAY_PRIORITY_STATUS_MUST_BE_LOCAL")

    def guard() -> None:
        try:
            # The generic status endpoint may perform a bounded 30-second
            # model health probe. Allow it to return the scheduler state.
            # Still require fresh status; timeouts never imply an idle AI.
            with urlopen(url, timeout=35) as response:
                status = json.load(response)
        except Exception as exc:
            if not (allow_stopped_runtime and _is_connection_refused(exc)):
                raise ReplayPaused("PRODUCTION_MODEL_PRIORITY_STATUS_UNAVAILABLE") from exc
        else:
            idle, reason = production_model_idle(status, budget_seconds=budget_seconds)
            if not idle:
                raise ReplayPaused(reason)
        # Remote Gemini exposes no local GPU slots. Observe the application
        # scheduler only; never fabricate a provider reservation or call :8080.
    return guard


def _market_snapshot(history: Any, symbol: str, point: datetime) -> dict[str, Any]:
    from .settled_funding import settled_funding_as_of
    snapshot = deepcopy(history.market_snapshot(symbol, point))
    settled = settled_funding_as_of(history.payload.get("funding", []), [symbol], point,
        venue=history.payload.get("assumptions", {}).get("data_venue", "gate"))
    snapshot["historical_settled_funding"] = settled["rates"][symbol]
    snapshot["fee_rate"] = TAKER_FEE_RATE
    snapshot["fee_assumption"] = "COMMON_RESEARCH_FEES_NOT_PRIVATE_ACCOUNT_TIER"
    for key in ("market", "metadata", "contract_rules"):
        if isinstance(snapshot.get(key), dict):
            snapshot[key].update({"maker": MAKER_FEE_RATE, "taker": TAKER_FEE_RATE,
                                  "maker_fee_rate": MAKER_FEE_RATE, "taker_fee_rate": TAKER_FEE_RATE})
    return snapshot


def _research_account_truth(account: Any, prices: dict[str, float], point: datetime) -> dict[str, Any]:
    """Project ownership of this isolated account into the production prompt.

    The simulator is the sole creator of these IDs. Labels authorize only its
    local execution sink; source/environment continue to identify simulation.
    A dictionary managed_state prevents production's private-Gate ownership
    lookup from reclassifying these independently owned research positions.
    """
    truth = deepcopy(account.account_truth(prices, point))
    for position in truth.get("positions", []):
        position.update(ownership="VERIFIED_SYSTEM", ownership_role="MANAGED_SYSTEM_POSITION",
                        ownership_reason_code="ISOLATED_SIMULATION_POSITION_ID", ownership_scope="RESEARCH_ACCOUNT_ONLY")
    for key, role in (("pending_orders", "SYSTEM_ENTRY_ORDER"), ("owned_entry_orders", "SYSTEM_ENTRY_ORDER"),
                      ("owned_protection_orders", "MANAGED_SYSTEM_PROTECTION")):
        for order in truth.get(key, []):
            order.update(ownership="SYSTEM_ORDER_ID_MATCH", ownership_role=role,
                         ownership_reason_code="ISOLATED_SIMULATION_ORDER_ID", ownership_scope="RESEARCH_ACCOUNT_ONLY")
    positions = len(truth.get("positions") or [])
    entries = len(truth.get("owned_entry_orders") or [])
    protections = len(truth.get("owned_protection_orders") or [])
    truth["managed_state"] = {"status": "ISOLATED_SIMULATION", "source": "AI_REPLAY_SIMULATION",
        "account_id": account.account_id, "environment": "REPLAY", "venue": "simulation",
        "managed_position_count": positions, "owned_entry_order_count": entries,
        "owned_protection_order_count": protections, "owned_reduction_order_count": 0,
        "external_or_unverified_position_count": 0, "external_or_unverified_order_count": 0,
        "managed_total_count": positions + entries + protections}
    return truth


def _research_performance_context(account: Any, prices: dict[str, float], point: datetime) -> dict[str, Any]:
    """Feed only already-closed local research outcomes back to this AI."""
    summary = account.summary(prices)
    closed = [trade for trade in account.completed_trades
              if trade.get("closed_at") and _time(trade["closed_at"]) <= point]
    wins = sum(float(trade.get("net_pnl") or 0) > 0 for trade in closed)
    losses = sum(float(trade.get("net_pnl") or 0) < 0 for trade in closed)
    result = {"status": "HISTORICAL_SIMULATION", "source": "AI_REPLAY_SIMULATION", "research_only": True,
              "stance": "RESEARCH_SAMPLE_NOT_GATE_VERIFIED", "as_of": point.isoformat(),
              "closed_trade_count": len(closed), "wins": wins, "losses": losses,
              "win_rate": wins / len(closed) if closed else None,
              "net_closed_pnl_usdt": sum(float(trade.get("net_pnl") or 0) for trade in closed),
              "fees_usdt": summary["fees"], "funding_pnl_usdt": summary["funding_pnl"]}
    if closed:
        trade = closed[-1]
        result["latest_closed_trade"] = {"symbol": trade.get("instrument_id"), "closed_at": trade["closed_at"],
                                          "net_pnl_usdt": float(trade.get("net_pnl") or 0)}
    return result


class _ReplayStore:
    """History reads are causal; every write goes to the result database."""

    def __init__(self, output: SQLiteStore, history: Any):
        self.output = output
        self.history = history

    def __getattr__(self, key: str) -> Any:
        return getattr(self.output, key)

    def latest_bars(self, symbol: str, timeframe: str, *, limit: int = 240, **filters: Any) -> list[dict[str, Any]]:
        return self.history.latest_bars(symbol, timeframe, limit=limit, **filters)

    list_market_bars = latest_bars

    def build_price_action_evidence(self, rows, timeframe, as_of):
        if (self.history.payload.get("assumptions", {}).get("data_venue") == "binance"
                and self.history.payload.get("source_dataset_sha256")):
            from core.replay.gemini_research import research_price_action
            return research_price_action(rows, timeframe, as_of)
        from core.trading.price_action_structure import build_price_action_structure
        return build_price_action_structure(rows, timeframe, as_of)

    def list_event_evidence(self, *, symbol: str | None = None, as_of: str | None = None, limit: int = 8, **_: Any) -> list[dict[str, Any]]:
        point = _time(as_of) if as_of else self.history.as_of
        symbols = (symbol,) if symbol else self.history.symbols
        return self.history.news_as_of(point, symbols, limit=limit)


class _ReplayLedger:
    def __init__(self, account: Any):
        self.account = account
        self.prices: dict[str, float] = {}
        self.as_of: datetime | None = None

    def get_open_positions(self, account_id: str, **_: Any) -> list[dict[str, Any]]:
        truth = self.account.account_truth(self.prices, self.as_of)
        return list(truth.get("positions") or [])

    def get_snapshot(self, account_id: str) -> Any:
        truth = self.account.account_truth(self.prices, self.as_of)
        return SimpleNamespace(net_equity=float(truth["equity"]), available_balance=float(truth["available_margin"]),
                               used_margin=float(truth["used_margin"]))


class _ReplayGateway:
    """A deliberately small execution sink with no exchange client or imports."""

    def __init__(self, account: Any, history: Any, strategy: dict[str, Any]):
        self.account = account
        self.history = history
        self.strategy = strategy
        self.as_of: datetime | None = None
        self.current_decision: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []

    def _fresh_market_snapshot(self, symbol: str) -> dict[str, Any]:
        return _market_snapshot(self.history, symbol, self.as_of)

    def _gate_remote_position_ownership_evidence(self, **kwargs: Any) -> dict[str, Any]:
        position = kwargs.get("position") or {}
        if position.get("position_id") not in {row.get("position_id") for row in self.account.account_truth({}, self.as_of).get("positions", [])}:
            raise GatewayError("REPLAY_POSITION_NOT_OWNED", "REPLAY_POSITION_NOT_OWNED")
        return {"status": "VERIFIED_SIMULATED", "source": "HISTORICAL_SIMULATION", "position_id": position["position_id"]}

    def _apply(self, decision: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
        event = self.account.apply_decision(decision, self.as_of, market)
        self.events.append(event)
        if str(event.get("status") or "").upper() == "REJECTED":
            raise GatewayError("REPLAY_SIMULATION_REJECTED", str(event.get("reason") or "REPLAY_SIMULATION_REJECTED"))
        return event

    def submit_intent(self, intent: Any, *, market_snapshot: dict[str, Any]) -> dict[str, Any]:
        decision = deepcopy(self.current_decision)
        if intent.reduce_only:
            decision["position_id"] = intent.position_id
            decision["quantity"] = float(intent.quantity)
            decision["ttl_seconds"] = intent.ttl_seconds or DEFAULT_LIMIT_TTL_SECONDS
        else:
            decision.update({"quantity": float(intent.quantity), "requested_leverage": int(intent.leverage),
                             "entry_price": float(intent.price), "order_preference": str(intent.order_type).upper(),
                             "limit_price": intent.limit_price, "ttl_seconds": intent.ttl_seconds,
                             "stop_price": intent.protection_plan.stop_price,
                             "take_profit": intent.protection_plan.take_profit})
        event = self._apply(decision, market_snapshot)
        return {**event, "status": OrderStatus.ACKNOWLEDGED.value, "source": "HISTORICAL_SIMULATION",
                "simulation_event": event, "exchange_fill_verified": False}

    def cancel_owned_gate_order(self, *, account_id: str, instrument_id: str, remote_order_id: str) -> dict[str, Any]:
        event = self._apply({"action": "CANCEL_ORDER", "instrument_id": instrument_id, "order_id": remote_order_id},
                            self._fresh_market_snapshot(instrument_id))
        return {**event, "status": OrderStatus.CANCELED.value, "verified_reconciled": True,
                "source": "HISTORICAL_SIMULATION", "exchange_fill_verified": False}

    def update_gate_protection(self, *, account_id: str, instrument_id: str, position_id: str,
                               new_stop_price: float | None = None, new_take_profit: float | None = None) -> dict[str, Any]:
        decision = {"action": "UPDATE_PROTECTION", "instrument_id": instrument_id, "position_id": position_id}
        if new_stop_price is not None:
            decision["new_stop_price"] = new_stop_price
        if new_take_profit is not None:
            decision["new_take_profit"] = new_take_profit
        return self._apply(decision, self._fresh_market_snapshot(instrument_id))


class _ReplayEngine(AILedDecisionEngine):
    def _live_execution_quote(self, *args: Any, **kwargs: Any) -> None:
        # Production refreshes Gate here. A replay must use its frozen quote.
        return None


def frozen_templates(candidate_instructions: dict[str, str] | None = None,
                     template_ids: tuple[str, ...] | list[str] | None = None) -> list[dict[str, Any]]:
    selected = TEMPLATE_IDS if template_ids is None else tuple(template_ids)
    if (not selected or len(set(selected)) != len(selected)
            or any(identity not in TEMPLATE_IDS for identity in selected)):
        raise ValueError("RESEARCH_TEMPLATE_SELECTION_INVALID")
    if candidate_instructions is not None and (
        not isinstance(candidate_instructions, dict) or set(candidate_instructions) != set(selected)
        or any(not isinstance(text, str) or not 1 <= len(text.strip()) <= 500
               for text in candidate_instructions.values())
    ):
        raise ValueError("RESEARCH_CANDIDATE_SET_INVALID")
    result = []
    for item in TEMPLATES:
        if item["id"] not in selected:
            continue
        strategy = {"template_id": item["id"], "name": item["name"], "style": item["style"], "revision": 0,
                    "profile": deepcopy(item["profile"]), "sections": deepcopy(item["sections"]),
                    "execution": normalize_execution(deepcopy(item["execution_defaults"]))}
        if candidate_instructions is None and item["id"] == "price_action_structure":
            # Optimization only. A later candidate replaces this seed rather
            # than stacking two experimental instructions in held-out replay.
            strategy["sections"]["entry_standards"] += "\n研究优化种子：" + _PA_RESEARCH_SEED_INSTRUCTION
        if candidate_instructions is not None:
            # Research only: immutable style, cadence, ownership, margin and
            # fixed entry budget; candidate text becomes part of config hash.
            strategy["sections"]["custom_prompt"] += "\n研究候选：" + candidate_instructions[item["id"]].strip()
        strategy["config_sha256"] = _hash(strategy)
        result.append(strategy)
    return result


def _coordinator(store: Any, clock: Callable[[], datetime], provider: Any, budget_seconds: float) -> Any:
    # Starting the coordinator would bind a live session. Only its pure model
    # request/normalization path is needed, with all persistence isolated.
    obj = AISessionCoordinator.__new__(AISessionCoordinator)
    obj.store = store
    obj.clock = clock
    obj.model_provider = provider
    obj.model_budget_seconds = budget_seconds
    obj._lock = threading.RLock()
    obj._health_cache = None
    # Full prompts/outputs are already frozen in the isolated SQLite bundle.
    # Never append research or fixture cycles to the live training dataset.
    obj.sft_sample_sink = lambda **kwargs: False
    return obj


def _output_dict(output: AIActionOutput) -> dict[str, Any]:
    value = asdict(output)
    extras = value.pop("extra_fields", {})
    value.update(extras)
    value["evidence_refs"] = list(value.get("evidence_refs") or [])
    return value


def _context_dict(context: AICycleContext) -> dict[str, Any]:
    value = asdict(context)
    value["mode"] = str(getattr(context.mode, "value", context.mode))
    value["market_radar_snapshot"] = getattr(context, "market_radar_snapshot", {})
    return value


def _restore_context(value: dict[str, Any]) -> AICycleContext:
    names = {field.name for field in fields(AICycleContext)}
    context = AICycleContext(**{key: val for key, val in value.items() if key in names})
    context.mode = TradingMode(str(getattr(context.mode, "value", context.mode)))
    context.market_radar_snapshot = value.get("market_radar_snapshot", {})
    return context


def _restore_output(value: dict[str, Any]) -> AIActionOutput:
    names = {field.name for field in fields(AIActionOutput)}
    direct = {key: val for key, val in value.items() if key in names}
    extra = dict(direct.get("extra_fields") or {})
    extra.update({key: val for key, val in value.items() if key not in names})
    direct["extra_fields"] = extra
    return AIActionOutput(**direct)


def _prepare_db(store: Any) -> None:
    with store._connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS ai_template_replay_runs (
            run_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, config_hash TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL, result_json TEXT, status TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS ai_template_replay_decisions (
            run_id TEXT NOT NULL, scan_key TEXT NOT NULL, template_id TEXT NOT NULL, as_of TEXT NOT NULL,
            status TEXT NOT NULL, decision_json TEXT, context_json TEXT, response_sha256 TEXT,
            result_json TEXT, error_code TEXT, PRIMARY KEY(run_id,scan_key))""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(ai_template_replay_decisions)")}
        for name, sql_type in (("wall_elapsed_seconds", "REAL"), ("submission_at", "TEXT")):
            if name not in columns:
                db.execute(f"ALTER TABLE ai_template_replay_decisions ADD COLUMN {name} {sql_type}")
        run_columns = {row[1] for row in db.execute("PRAGMA table_info(ai_template_replay_runs)")}
        if "config_json" not in run_columns:
            db.execute("ALTER TABLE ai_template_replay_runs ADD COLUMN config_json TEXT")


def _advance_accounts(accounts: dict[str, Any], history: Any, start: datetime, through: datetime) -> None:
    """Settle minute bars and funding in time order, retaining account state."""
    for account in accounts.values():
        if getattr(account, "halted_reason", None):
            continue
        last_advanced = _time(account.last_advanced_through) if account.last_advanced_through else start
        account_start = max(start, last_advanced)
        if account_start >= through:
            continue
        _advance_account(account, history, account_start, through)


def _advance_account(account: Any, history: Any, start: datetime, through: datetime) -> None:
    by_time: dict[datetime, list[dict[str, Any]]] = {}
    for bar in history.bars_between(start, through, timeframe="1m"):
        symbol = bar.get("instrument_id") or bar.get("symbol")
        if (not isinstance(symbol, str) or not symbol.endswith("USDT")
                or not symbol[:-4].isalnum() or bar.get("symbol", symbol) != symbol):
            raise ValueError("REPLAY_EXECUTION_BAR_SYMBOL_MISMATCH")
        contracts = history.payload.get("contracts")
        contract = contracts.get(symbol) if isinstance(contracts, dict) else None
        if not isinstance(contract, dict):
            raise ValueError(f"REPLAY_EXECUTION_CONTRACT_MISSING:{symbol}")
        native = symbol[:-4] + "_USDT"
        market = contract.get("market", {})
        if (contract.get("instrument_id") != symbol
                or contract.get("symbol", symbol) != symbol
                or contract.get("native_symbol", native) != native
                or not isinstance(market, dict)
                or market.get("symbol", symbol) != symbol
                or market.get("id", native) != native):
            raise ValueError(f"REPLAY_EXECUTION_CONTRACT_SYMBOL_MISMATCH:{symbol}")
        for field in ("contract_size", "amount_step", "price_round", "min_size", "max_size", "leverage_max"):
            value = contract.get(field)
            try:
                valid = not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0
            except (TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                raise ValueError(f"REPLAY_EXECUTION_CONTRACT_INVALID:{symbol}:{field}")
        if float(contract["min_size"]) > float(contract["max_size"]):
            raise ValueError(f"REPLAY_EXECUTION_CONTRACT_INVALID:{symbol}:size_bounds")
        # Execution alone gets the frozen rules. Preserve archived OHLCV and
        # hashes; base_volume must be converted back with the same multiplier
        # used to size orders, rather than ReplayAccount's generic defaults.
        execution_bar = deepcopy(bar)
        execution_bar["market"] = deepcopy(market)
        execution_bar["contract_rules"] = deepcopy(contract)
        execution_bar["contract_rules"]["price_tick"] = contract["price_round"]
        bar_end = _time(bar["bar_end"])
        by_time.setdefault(bar_end, []).append(execution_bar)
    funding_by_time: dict[datetime, list[dict[str, Any]]] = {}
    for row in history.payload.get("funding", []):
        paid_at = _time(row["payment_time"])
        if start < paid_at <= through:
            funding_by_time.setdefault(paid_at, []).append(row)
    for point in sorted(set(by_time) | set(funding_by_time)):
        account.advance(by_time.get(point, []), through=point)
        if getattr(account, "halted_reason", None):
            return
        if funding_by_time.get(point):
            account.apply_funding(funding_by_time[point], through=point)
            if getattr(account, "halted_reason", None):
                return
    account.advance([], through=through)


def run_ai_template_replay(
    history: Any, *, db_path: str | Path, initial_equity: float = 1000.0,
    max_decisions: int | None = None, resume: bool = False, model_budget_seconds: float = 70.0,
    runtime_status_url: str | None = None, priority_guard: Callable[[], None] | None = None,
    allow_stopped_runtime: bool = False,
    model_provider: Any | None = None, model_decider: Callable[[AICycleContext], AIActionOutput] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    candidate_instructions: dict[str, str] | None = None,
    template_ids: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Replay every native scan in one contiguous frozen observation window."""
    from core.replay.ai_simulation import ReplayAccount

    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("REPLAY_INITIAL_EQUITY_INVALID")
    if max_decisions is not None and (type(max_decisions) is not int or max_decisions < 1):
        raise ValueError("REPLAY_MAX_DECISIONS_INVALID")
    if not 0 < model_budget_seconds <= 300:
        raise ValueError("REPLAY_MODEL_BUDGET_INVALID")
    points = [_time(value) for value in history.decision_points]
    if not points or any(b - a != timedelta(minutes=5) for a, b in zip(points, points[1:])):
        raise ValueError("REPLAY_CONTIGUOUS_FIVE_MINUTE_TIMELINE_REQUIRED")
    if any(point.second or point.microsecond or point.minute % 5 for point in points):
        raise ValueError("REPLAY_ALIGNED_TIMELINE_REQUIRED")
    if points[0] != history.window_start or points[-1] + timedelta(minutes=5) != history.window_end:
        raise ValueError("REPLAY_TIMELINE_MUST_COVER_COMPLETE_WINDOW")
    templates = frozen_templates(candidate_instructions, template_ids)
    model_source = "TEST_INJECTED" if model_decider else "TEST_PROVIDER" if model_provider is not None else "PRODUCTION_GEMINI"
    model_pin, model_health = None, None
    if model_decider is None:
        if priority_guard is None:
            if not runtime_status_url:
                raise ValueError("PRODUCTION_MODEL_PRIORITY_STATUS_REQUIRED")
            priority_guard = runtime_priority_guard(runtime_status_url, budget_seconds=model_budget_seconds,
                                                  allow_stopped_runtime=allow_stopped_runtime)
        if model_provider is None:
            from core.ai.ollama import OllamaProvider
            from core.trading.ai_session_coordinator import _session_context_length, DECISION_OUTPUT_TOKEN_BUDGET
            model_provider = OllamaProvider(base_url=DEFAULT_BASE_URL, model_name=DEFAULT_SMART_MODEL,
                                           context_length=_session_context_length(), max_tokens=DECISION_OUTPUT_TOKEN_BUDGET,
                                           timeout=model_budget_seconds, temperature=0, retries=0, think=False)
            model_pin, model_health = _production_model_pin(model_provider)
    config = {"schema_version": SCHEMA_VERSION, "initial_equity": initial_equity,
              "prompt_version": AI_PROMPT_VERSION, "model_id": DEFAULT_SMART_MODEL,
              "templates": templates, "model_budget_seconds": model_budget_seconds,
              "maker_fee_rate": MAKER_FEE_RATE, "taker_fee_rate": TAKER_FEE_RATE,
              "source_sha256": frozen_source_fingerprint(),
              "model_source": model_source, "model_pin": model_pin,
              "execution_clock": "CEIL_MINUTE_AFTER_COMPLETE_MODEL_WALL_ELAPSED",
              "allow_stopped_runtime": bool(allow_stopped_runtime)}
    manifest_hash = _hash({key: value for key, value in history.payload.items() if key != "manifest_sha256"})
    if history.payload.get("manifest_sha256") not in {None, manifest_hash}:
        raise ValueError("REPLAY_MANIFEST_INTEGRITY_INVALID")
    config_hash = _hash(config)
    run_id = "ai-replay-" + _hash({"manifest": manifest_hash, "config": config_hash})[:24]
    output_path = Path(db_path).resolve()
    source_path = history.payload.get("source_db_path")
    if source_path and output_path == Path(source_path).resolve():
        raise ValueError("REPLAY_OUTPUT_MUST_NOT_BE_PRODUCTION_DB")
    if output_path.exists() and not resume:
        # Do not create or migrate an arbitrary pre-existing SQLite file.
        raise ValueError("REPLAY_RESULT_DB_EXISTS_USE_RESUME")
    if output_path.exists():
        with sqlite3.connect(output_path.as_uri() + "?mode=ro", uri=True) as existing:
            marker = existing.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_template_replay_runs'").fetchone()
        if marker is None:
            raise ValueError("REPLAY_REFUSES_NON_REPLAY_DATABASE")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sql = SQLiteStore(str(output_path))
    sql.initialize()
    _prepare_db(sql)
    store = _ReplayStore(sql, history)
    with sql._connect() as db:
        saved = db.execute("SELECT * FROM ai_template_replay_runs WHERE run_id=?", (run_id,)).fetchone()
        other = db.execute("SELECT COUNT(*) FROM ai_template_replay_runs WHERE run_id<>?", (run_id,)).fetchone()[0]
    if other:
        raise ValueError("REPLAY_RESUME_CONFIG_OR_HISTORY_MISMATCH")
    if saved is not None:
        if saved["status"] == "FAILED" and saved["result_json"]:
            return json.loads(saved["result_json"])
        checkpoint = json.loads(saved["checkpoint_json"])
    else:
        accounts = {}
        for strategy in templates:
            account = ReplayAccount(initial_equity=initial_equity, account_id=f"research:{run_id}:{strategy['template_id']}",
                                    margin_cap_pct=float(strategy["execution"]["max_margin_pct"]),
                                    maker_fee_rate=MAKER_FEE_RATE, taker_fee_rate=TAKER_FEE_RATE)
            accounts[strategy["template_id"]] = account.to_dict()
        checkpoint = {"point_index": 0, "template_index": 0, "accounts": accounts, "decision_count": 0, "errors": []}
        with sql._connect() as db:
            db.execute("INSERT INTO ai_template_replay_runs(run_id,manifest_hash,config_hash,checkpoint_json,result_json,status,config_json) VALUES (?,?,?,?,NULL,?,?)",
                       (run_id, manifest_hash, config_hash, _json(checkpoint), "RUNNING", _json(config)))
    accounts = {key: ReplayAccount.from_dict(value) for key, value in checkpoint["accounts"].items()}
    current_time = points[min(checkpoint["point_index"], len(points) - 1)]
    coordinator = _coordinator(store, lambda: current_time, model_provider, model_budget_seconds)
    coordinator._health_cache = model_health
    jobs_this_call = 0
    status, pause_reason = "COMPLETED", None

    def save_checkpoint(db: Any, *, run_status: str = "RUNNING") -> None:
        checkpoint["accounts"] = {key: account.to_dict() for key, account in accounts.items()}
        db.execute("UPDATE ai_template_replay_runs SET checkpoint_json=?,status=? WHERE run_id=?",
                   (_json(checkpoint), run_status, run_id))

    while checkpoint["point_index"] < len(points):
        point_index = checkpoint["point_index"]
        current_time = points[point_index]
        history.set_as_of(current_time)
        previous = points[point_index - 1] if point_index else history.window_start
        # Matching progresses for all five accounts on every public time step,
        # including steps on which a 15m strategy has no model invocation.
        if checkpoint["template_index"] == 0:
            _advance_accounts(accounts, history, previous, current_time)
        while checkpoint["template_index"] < len(templates):
            strategy = templates[checkpoint["template_index"]]
            template_id = strategy["template_id"]
            interval = int(strategy["execution"]["scan_interval_minutes"])
            if current_time.minute % interval:
                checkpoint["template_index"] += 1
                continue
            if max_decisions is not None and jobs_this_call >= max_decisions:
                status, pause_reason = "PAUSED", "PILOT_DECISION_LIMIT"
                break
            account = accounts[template_id]
            if getattr(account, "halted_reason", None):
                scan_key = _hash({"template": template_id, "config": strategy["config_sha256"], "as_of": current_time.isoformat()})
                with sql._connect() as db:
                    db.execute("INSERT OR IGNORE INTO ai_template_replay_decisions(run_id,scan_key,template_id,as_of,status,error_code) VALUES(?,?,?,?,?,?)",
                               (run_id, scan_key, template_id, current_time.isoformat(), "ACCOUNT_HALTED", account.halted_reason))
                    checkpoint["template_index"] += 1
                    save_checkpoint(db)
                if progress:
                    progress({"run_id": run_id, "status": "ACCOUNT_HALTED_SCAN_SKIPPED", "template_id": template_id,
                              "as_of": current_time.isoformat(), "reason": account.halted_reason})
                continue
            all_prices = {symbol: float(history.market_snapshot(symbol, current_time)["price"])
                          for symbol in history.symbols if history.market_snapshot(symbol, current_time).get("price")}
            truth = _research_account_truth(account, all_prices, current_time)
            managed = list(dict.fromkeys(str(item.get("symbol") or item.get("instrument_id")) for item in truth.get("positions", [])))
            managed += [str(item.get("symbol") or item.get("instrument_id")) for item in truth.get("pending_orders", [])]
            symbols = tuple(history.select_symbols(current_time, signal_timeframe=strategy["profile"]["signal_timeframe"],
                                                   limit=3, managed_symbols=managed))
            snapshots = {symbol: _market_snapshot(history, symbol, current_time) for symbol in symbols}
            context = AICycleContext(cycle_id="replay-cycle-" + _hash({"run": run_id, "template": template_id, "as_of": current_time.isoformat()})[:28],
                                     account_id=truth.get("account_id") or f"research:{run_id}:{template_id}", generation=1,
                                     started_at=current_time.isoformat(), expires_at=(current_time + timedelta(seconds=model_budget_seconds)).isoformat(),
                                     allowed_instruments=symbols, mode=TradingMode.TESTNET, venue="gate", environment="TESTNET",
                                     execution_environment="TESTNET", provider="historical_" + history.payload.get("assumptions", {}).get("data_venue", "gate") + "_public", session_id=run_id,
                                     decision_contract=CONTRACT, strategy_instructions=strategy, account_truth=truth,
                                     positions=list(truth.get("positions") or []), market_snapshots=snapshots,
                                     news_revisions=history.news_as_of(current_time, symbols), scheduled_at=current_time.isoformat(),
                                     market_data_environment="HISTORICAL_RESEARCH", data_quality={"source": "FROZEN_HISTORY", "simulation": True},
                                     dynamic_risk={"status": "NOT_APPLICABLE", "entry_allowed": True, "reasons": [], "policy": "MARGIN_ONLY"},
                                     universe_snapshot={"status": "FROZEN_RESEARCH_UNIVERSE", "selected_symbols": list(symbols),
                                                        "selection": "AS_OF_DATA_RANK_WITH_MANAGED_INSTRUMENTS"})
            main_frame, auxiliary_frames, _ = AISessionCoordinator._strategy_scan_contract(strategy)
            context.technical_context = technical_context(store, symbols, current_time, interval=interval,
                                                         timeframes=[main_frame, *(auxiliary_frames or ())],
                                                         include_price_action=template_id == "price_action_structure")
            context.performance_context = _research_performance_context(account, all_prices, current_time)
            context.market_radar_snapshot = {"status": "UNAVAILABLE", "source": "HISTORICAL_RESEARCH_NO_ARCHIVED_RADAR", "generated_at": current_time.isoformat()}
            scan_key = _hash({"template": template_id, "config": strategy["config_sha256"], "as_of": current_time.isoformat()})
            with sql._connect() as db:
                row = db.execute("SELECT * FROM ai_template_replay_decisions WHERE run_id=? AND scan_key=?", (run_id, scan_key)).fetchone()
            if row is None:
                try:
                    if priority_guard is not None:
                        priority_guard()
                except ReplayPaused as exc:
                    status, pause_reason = "PAUSED", str(exc)
                    break
                with sql._connect() as db:
                    db.execute("INSERT INTO ai_template_replay_decisions(run_id,scan_key,template_id,as_of,status) VALUES(?,?,?,?,?)",
                               (run_id, scan_key, template_id, current_time.isoformat(), "MODEL_STARTED"))
                started_monotonic = time.monotonic()
                wall_elapsed_seconds = 0.0
                submission_at = current_time
                health_stage = "BEFORE_DECISION"
                try:
                    model_deadline = started_monotonic + model_budget_seconds
                    if model_pin is not None:
                        observed_pin, health = _production_model_pin(model_provider, deadline_monotonic=model_deadline)
                        if observed_pin != model_pin:
                            raise ReplayModelIdentityMismatch("REPLAY_MODEL_HEALTH_IDENTITY_MISMATCH")
                        coordinator._health_cache = health
                    if time.monotonic() >= model_deadline:
                        raise TimeoutError("REPLAY_COMPLETE_MODEL_DEADLINE_EXCEEDED")
                    model_output = (model_decider(context) if model_decider else
                                    coordinator._model_output(context, deadline_monotonic=model_deadline))
                    if not isinstance(model_output, AIActionOutput):
                        raise ValueError("REPLAY_MODEL_DECIDER_INVALID_OUTPUT")
                    if model_pin is not None:
                        _verify_model_receipt(context, model_pin)
                        health_stage = "AFTER_DECISION"
                        completed_pin, _ = _production_model_pin(model_provider, deadline_monotonic=model_deadline)
                        if completed_pin != model_pin:
                            raise ReplayModelIdentityMismatch("REPLAY_MODEL_COMPLETION_IDENTITY_MISMATCH")
                    # A trade becomes executable only after every identity
                    # check finishes. Include both health probes in the clock,
                    # and share the same deadline with inference and repair.
                    wall_elapsed_seconds = _model_wall_elapsed(started_monotonic, production=model_pin is not None)
                    submission_at = _minute_ceiling(current_time + timedelta(seconds=wall_elapsed_seconds))
                    if wall_elapsed_seconds > model_budget_seconds:
                        raise TimeoutError("REPLAY_COMPLETE_MODEL_DEADLINE_EXCEEDED")
                    if submission_at > history.window_end:
                        raise ValueError("REPLAY_SUBMISSION_OUTSIDE_FROZEN_WINDOW")
                    decision = _output_dict(model_output)
                    raw_response = context.model_raw_response or _json(decision)
                    response_hash = hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
                    with sql._connect() as db:
                        db.execute("UPDATE ai_template_replay_decisions SET status='MODEL_DONE',decision_json=?,context_json=?,response_sha256=?,wall_elapsed_seconds=?,submission_at=? WHERE run_id=? AND scan_key=?",
                                   (_json(decision), _json(_context_dict(context)), response_hash, wall_elapsed_seconds, submission_at.isoformat(), run_id, scan_key))
                except Exception as exc:
                    _record_health_failure(context, exc, health_stage)
                    wall_elapsed_seconds = _model_wall_elapsed(started_monotonic, production=model_pin is not None)
                    submission_at = _minute_ceiling(current_time + timedelta(seconds=wall_elapsed_seconds))
                    code = type(exc).__name__ + ":" + str(exc)[:240]
                    with sql._connect() as db:
                        db.execute("UPDATE ai_template_replay_decisions SET status='ERROR',context_json=?,error_code=?,wall_elapsed_seconds=?,submission_at=? WHERE run_id=? AND scan_key=?",
                                   (_json(_context_dict(context)), code, wall_elapsed_seconds, submission_at.isoformat(), run_id, scan_key))
                    checkpoint["errors"].append({"template_id": template_id, "as_of": current_time.isoformat(), "error": code})
                    checkpoint["template_index"] += 1
                    checkpoint["decision_count"] += 1
                    jobs_this_call += 1
                    with sql._connect() as db:
                        save_checkpoint(db)
                    if progress:
                        progress({"run_id": run_id, "status": "DECISION_ERROR", "template_id": template_id, "as_of": current_time.isoformat(), "error_code": code})
                    if isinstance(exc, ReplayModelIdentityMismatch):
                        status, pause_reason = "FAILED", str(exc)
                        break
                    continue
            else:
                if row["status"] == "MODEL_STARTED":
                    status, pause_reason = "INTERRUPTED", "MODEL_RESULT_AMBIGUOUS_DO_NOT_RECALL"
                    break
                if row["status"] == "ERROR":
                    if not any(item.get("template_id") == template_id and item.get("as_of") == current_time.isoformat()
                               for item in checkpoint["errors"]):
                        checkpoint["errors"].append({"template_id": template_id, "as_of": current_time.isoformat(), "error": row["error_code"]})
                        checkpoint["decision_count"] += 1
                    checkpoint["template_index"] += 1
                    if str(row["error_code"] or "").startswith("ReplayModelIdentityMismatch:"):
                        status, pause_reason = "FAILED", str(row["error_code"])
                        break
                    continue
                if row["status"] == "COMPLETED":
                    raise ValueError("REPLAY_CHECKPOINT_DECISION_CONFLICT")
                decision = json.loads(row["decision_json"])
                context = _restore_context(json.loads(row["context_json"]))
                model_output = _restore_output(decision)
                wall_elapsed_seconds = row["wall_elapsed_seconds"]
                submission_at = _time(row["submission_at"]) if row["submission_at"] else None
                if wall_elapsed_seconds is None or submission_at != _minute_ceiling(current_time + timedelta(seconds=wall_elapsed_seconds)):
                    raise ValueError("REPLAY_SAVED_SUBMISSION_CLOCK_INVALID")
                if model_pin is not None:
                    _verify_model_receipt(context, model_pin)
            # Existing orders and protection remain active during inference.
            # Only this account advances; the four counterfactual accounts
            # retain their own scan and completion clocks.
            _advance_accounts({template_id: account}, history, current_time, submission_at)
            execution_prices = {symbol: float(history.market_snapshot(symbol, submission_at)["price"])
                                for symbol in history.symbols if history.market_snapshot(symbol, submission_at).get("price")}
            execution_context = deepcopy(context)
            execution_context.account_truth = _research_account_truth(account, execution_prices, submission_at)
            execution_context.positions = list(execution_context.account_truth.get("positions") or [])
            execution_context.execution_market_snapshots = {symbol: _market_snapshot(history, symbol, submission_at) for symbol in context.allowed_instruments}
            # The real model deadline is enforced above. Moving a valid 65s
            # completion to its conservative 120s closed-bar boundary must
            # not introduce an artificial timeout into the research engine.
            execution_context.expires_at = _minute_ceiling(_time(context.expires_at)).isoformat()
            execution_context.data_quality = {**execution_context.data_quality,
                "research_clock_quantization": "CEIL_MINUTE_AFTER_COMPLETE_MODEL_WALL_ELAPSED"}
            ledger = _ReplayLedger(account)
            ledger.prices, ledger.as_of = execution_prices, submission_at
            gateway = _ReplayGateway(account, history, strategy)
            gateway.as_of, gateway.current_decision = submission_at, decision
            engine = _ReplayEngine(store=store, execution_gateway=gateway, risk_engine=SimpleNamespace(),
                                   ledger=ledger, guardian=SimpleNamespace(), agent_policy_id=AI_PROMPT_VERSION)
            if getattr(account, "halted_reason", None):
                # A liquidation path discovered during inference invalidates
                # execution even when the model returned a valid proposal.
                result = SimpleNamespace(status="ACCOUNT_HALTED", reason=account.halted_reason)
            else:
                result = engine.execute_cycle(execution_context, now=submission_at, model_output=model_output)
            receipt = {"status": result.status, "reason": result.reason, "action": model_output.action,
                       "events": gateway.events, "source": "HISTORICAL_SIMULATION", "private_exchange_calls": 0,
                       "model_input_as_of": current_time.isoformat(), "wall_elapsed_seconds": wall_elapsed_seconds,
                       "submission_at": submission_at.isoformat(), "execution_account_truth": execution_context.account_truth,
                       "execution_market_snapshots": execution_context.execution_market_snapshots,
                       "research_clock_quantization": "CEIL_MINUTE_AFTER_COMPLETE_MODEL_WALL_ELAPSED"}
            checkpoint["template_index"] += 1
            checkpoint["decision_count"] += 1
            jobs_this_call += 1
            with sql._connect() as db:
                db.execute("UPDATE ai_template_replay_decisions SET status='COMPLETED',result_json=? WHERE run_id=? AND scan_key=?",
                           (_json(receipt), run_id, scan_key))
                save_checkpoint(db)
            if progress:
                progress({"run_id": run_id, "status": "DECISION_COMPLETED", "template_id": template_id,
                          "as_of": current_time.isoformat(), "action": model_output.action,
                          "wall_elapsed_seconds": wall_elapsed_seconds, "submission_at": submission_at.isoformat(),
                          "execution_status": result.status, "decision_count": checkpoint["decision_count"]})
        if status != "COMPLETED":
            break
        checkpoint["point_index"] += 1
        checkpoint["template_index"] = 0
        with sql._connect() as db:
            save_checkpoint(db)
    if status == "COMPLETED":
        _advance_accounts(accounts, history, points[-1], history.window_end)
        history.set_as_of(history.window_end)
    final_time = history.window_end if status == "COMPLETED" else max(
        [current_time, *[_time(account.last_advanced_through) for account in accounts.values() if account.last_advanced_through]])
    prices = {symbol: float(history.market_snapshot(symbol, final_time)["price"])
              for symbol in history.symbols if history.market_snapshot(symbol, final_time).get("price")}
    results = []
    with sql._connect() as db:
        decision_rows = db.execute("SELECT template_id,status,decision_json,result_json,context_json FROM ai_template_replay_decisions WHERE run_id=?", (run_id,)).fetchall()
    for strategy in templates:
        own_account = accounts[strategy["template_id"]]
        account_time = _time(own_account.last_advanced_through) if own_account.last_advanced_through else current_time
        account_prices = {symbol: float(history.market_snapshot(symbol, account_time)["price"])
                          for symbol in history.symbols if history.market_snapshot(symbol, account_time).get("price")}
        summary = own_account.summary(account_prices)
        counts: dict[str, int] = {}
        execution_counts: dict[str, int] = {}
        own_rows = [row for row in decision_rows if row["template_id"] == strategy["template_id"]]
        for row in own_rows:
            action = str(json.loads(row["decision_json"] or "{}").get("action") or row["status"])
            counts[action] = counts.get(action, 0) + 1
            if row["result_json"]:
                outcome = str(json.loads(row["result_json"]).get("status") or "UNKNOWN")
                execution_counts[outcome] = execution_counts.get(outcome, 0) + 1
        skipped_halted = sum(row["status"] == "ACCOUNT_HALTED" for row in own_rows)
        results.append({"template_id": strategy["template_id"], "name": strategy["name"],
                        "config_sha256": strategy["config_sha256"], "scan_count": len(own_rows),
                        "decision_count": len(own_rows) - skipped_halted, "skipped_halted_scans": skipped_halted,
                        "complete_window": status == "COMPLETED" and not getattr(own_account, "halted_reason", None),
                        "evaluated_through": account_time.isoformat(),
                        "action_counts": counts, "execution_counts": execution_counts, **summary})
    complete_data = bool(history.payload.get("complete_data"))
    economic_eligible = all(item.get("economic_eligible") is True and not item.get("halted_reason") for item in results)
    qualified = status == "COMPLETED" and complete_data and not checkpoint["errors"] and economic_eligible and config["model_source"] == "PRODUCTION_GEMINI"
    receipts = {}
    for row in decision_rows:
        context_value = json.loads(row["context_json"] or "{}")
        if not context_value.get("model_call_completed"):
            continue
        settings = context_value.get("model_inference_settings") or {}
        receipt = {key: context_value.get(key) for key in ("model_id", "model_version", "model_digest", "model_digest_status", "model_quantization")}
        receipt.update({key: settings.get(key) for key in ("actual_model_id", "model_identity_source", "verified_manifest_model_id")})
        receipts[_hash(receipt)] = receipt
    report = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "status": status, "pause_reason": pause_reason,
              "source": "HISTORICAL_AI_ACCOUNT_SIMULATION", "formal_acceptance_eligible": False,
              "configuration_scope": "FROZEN_BUILTIN_TEMPLATES_WITH_DEFAULTS",
              "common_initial_equity_usdt": initial_equity, "user_live_account_returns": False,
              "comparison_eligible": qualified, "complete_data": complete_data, "complete_window": status == "COMPLETED",
              "decision_source": config["model_source"], "manifest_sha256": manifest_hash, "config_sha256": config_hash,
              "source_sha256": config["source_sha256"], "model_receipts": list(receipts.values()),
              "model_pin": model_pin, "execution_clock": config["execution_clock"],
              "economic_eligible": economic_eligible,
              "halted_accounts": [{"template_id": item["template_id"], "reason": item["halted_reason"],
                                   "skipped_scans": item["skipped_halted_scans"]}
                                  for item in results if item.get("halted_reason")],
              "window_start": history.window_start.isoformat(), "window_end": history.window_end.isoformat(),
              "evaluated_through": final_time.isoformat(), "decision_count": checkpoint["decision_count"],
              "errors": checkpoint["errors"], "results": results, "assumptions": history.payload.get("assumptions", []),
              "cost_assumptions": {"maker_fee_rate": MAKER_FEE_RATE, "taker_fee_rate": TAKER_FEE_RATE,
                                   "slippage_bps": 2.0, "source": "COMMON_RESEARCH_ASSUMPTION_NOT_PRIVATE_ACCOUNT_TIER"},
              "result_db": str(output_path), "private_exchange_calls": 0,
              "production_sft_writes": 0,
              "model_weights_trained": False,
              "limitations": ["Research fills do not prove Gate execution or profitability.",
                              "Mark-trigger protection is approximated with archived last-price candles.",
                              "Production priority is observed before calls; a separate process cannot reserve its future inference slot.",
                              "Cloud weights are not exposed or frozen; identical requests may produce different responses."]}
    with sql._connect() as db:
        save_checkpoint(db, run_status=status)
        db.execute("UPDATE ai_template_replay_runs SET result_json=? WHERE run_id=?", (_json(report), run_id))
    return report


__all__ = ["run_ai_template_replay", "frozen_templates", "frozen_source_fingerprint", "production_model_idle", "runtime_priority_guard", "ReplayPaused"]
