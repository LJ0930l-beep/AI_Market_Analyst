"""AI-Led Autonomous Decision & Execution Engine.

Fulfills N08 & AT31–AT35 requirements:
- Dual Decision Paths: AI_LED operates independently of S1–S6 rules (AT31).
- Strict Action Schema: WAIT, HOLD, OPEN_LONG, OPEN_SHORT, REDUCE_POSITION, CLOSE_POSITION, TIGHTEN_STOP.
- Autonomous Execution: Valid actions auto-executed through RiskEngine and ExecutionGateway across consecutive cycles without per-trade confirmation (AT32).
- Hard Risk Enforcement: Over-budget, unauthorized instruments, or self-modifying limits rejected (AT33).
- Guardian Priority: Guardian stop execution takes precedence over AI actions; no reverse opening without closing; cannot widen stops (AT34).
- Timeout & Stale Discard: Actions arriving after cycle expiration discarded; no batch replay of stale actions (AT35).
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import enum
import hashlib
import json
import logging
import math
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.trading.execution_gateway import (
    ExecutionGateway,
    TradingMode,
    ControlMode,
    DecisionPath,
    OrderIntent,
    ProtectionPlan,
    ProtectionStatus,
    OrderStatus,
    GatewayError,
)
from core.trading.ledger import AccountLedger
from core.trading.risk_engine import RiskEngine, RiskLimits
from core.trading.position_guardian import PositionGuardian
from core.trading.order_selection import (
    DEFAULT_LIMIT_TTL_SECONDS,
    OrderSelectionInput,
    OrderSelectionPolicy,
)
from .ai_cycle_trace import (
    build_stage_trace,
    humanize_reason,
    infer_block_stage,
    persist_stage_trace,
)
from core.model_routing import (
    is_verified_bonsai_receipt as is_verified_bonsai_inference_receipt,
    DEFAULT_SMART_MODEL,
)

logger = logging.getLogger("core.trading.ai_led_engine")


class AIActionType(str, enum.Enum):
    WAIT = "WAIT"
    HOLD = "HOLD"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    REDUCE_POSITION = "REDUCE_POSITION"
    CLOSE_POSITION = "CLOSE_POSITION"
    TIGHTEN_STOP = "TIGHTEN_STOP"


ALLOWED_AI_ACTIONS = {a.value for a in AIActionType}


@dataclass
class AICycleContext:
    cycle_id: str
    account_id: str
    generation: int
    started_at: str
    expires_at: str
    allowed_instruments: tuple[str, ...]
    max_risk_fraction: float = 0.0025  # 0.25% standard risk limit
    mode: TradingMode = TradingMode.PAPER
    venue: str = "simulated"
    environment: Optional[str] = None
    provider: Optional[str] = None
    execution_environment: Optional[str] = None
    session_id: Optional[str] = None
    authorization_id: Optional[str] = None
    authorization_version: Optional[int] = None
    lease_holder_id: Optional[str] = None
    fencing_token: Optional[int] = None
    # Model provenance is carried with the cycle rather than reconstructed
    # from a later UI query.  A scorecard must be able to distinguish model,
    # prompt, and input changes from execution outcomes.
    model_id: Optional[str] = None
    model_version: Optional[str] = None
    prompt_version: Optional[str] = None
    input_hash: Optional[str] = None
    model_digest: Optional[str] = None
    model_digest_status: str = "UNKNOWN_NOT_PROVIDED"
    model_quantization: Optional[str] = None
    model_inference_settings: Dict[str, Any] = field(default_factory=dict)
    model_latency_ms: Optional[float] = None
    model_call_attempted: bool = False
    model_call_completed: bool = False
    model_raw_response: Optional[str] = None
    model_call_prompt_version: Optional[str] = None
    evidence_bundle_id: Optional[str] = None
    evidence_status: str = "NOT_FROZEN"
    evidence_refs: tuple[str, ...] = ()
    market_snapshots: Dict[str, Any] = field(default_factory=dict)
    news_revisions: List[Any] = field(default_factory=list)
    positions: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    decision_memory: List[Dict[str, Any]] = field(default_factory=list)
    calibration: Dict[str, Any] = field(default_factory=dict)
    # Explicit data/execution facts are carried through the immutable cycle
    # context so the UI and audit projection never infer them from an action
    # such as WAIT or from a legacy account label.
    market_data_environment: Optional[str] = None
    data_quality: Dict[str, Any] = field(default_factory=dict)
    strategy_readiness: Dict[str, Any] = field(default_factory=dict)
    indicator_snapshot_id: Optional[str] = None
    scheduled_at: Optional[str] = None
    # Managed Gate TestNet cycles carry the exact remote account snapshot used
    # for economics.  A local ledger projection is never substituted here.
    account_truth: Dict[str, Any] = field(default_factory=dict)
    stage_block: Optional[str] = None
    decision_contract: Optional[str] = None
    technical_context: Dict[str, Any] = field(default_factory=dict)
    strategy_instructions: Dict[str, Any] = field(default_factory=dict)
    universe_snapshot: Dict[str, Any] = field(default_factory=dict)
    dynamic_risk: Optional[Dict[str, Any]] = None
    performance_context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AIActionOutput:
    action: str
    instrument_id: str
    reason: str
    decision_id: str = field(default_factory=lambda: f"dec_{uuid.uuid4().hex[:12]}")
    position_id: Optional[str] = None
    entry_condition: Optional[str] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    requested_risk_fraction: Optional[float] = None
    position_size_usdt: Optional[float] = None
    requested_leverage: Optional[int] = None
    new_stop_price: Optional[float] = None
    reduce_fraction: Optional[float] = None
    order_preference: str = "AUTO"
    limit_price: Optional[float] = None
    ttl_seconds: Optional[int] = None
    candidate_id: Optional[str] = None
    closed_15m_bar: Optional[str] = None
    evidence_refs: tuple[str, ...] = ()
    extra_fields: Dict[str, Any] = field(default_factory=dict)
    # MODEL is reserved for a schema-valid Qwen output.  PRECHECK/SYSTEM
    # outputs are lifecycle facts and must never be projected as model WAIT.
    decision_origin: str = "MODEL"


@dataclass
class AICycleResult:
    cycle_id: str
    action_output: AIActionOutput
    status: str  # "EXECUTED", "WAITING", "BLOCKED", "TIMEOUT_DISCARDED", "REJECTED"
    reason: str
    order_intent: Optional[OrderIntent] = None
    execution_result: Optional[Dict[str, Any]] = None
    decision_origin: str = "MODEL"
    operational_state: str = "MODEL_DECISION"


class AILedDecisionEngine:
    """Autonomous trading engine driven by Qwen 9B."""

    def __init__(
        self,
        store,
        execution_gateway: ExecutionGateway,
        risk_engine: RiskEngine,
        ledger: AccountLedger,
        guardian: PositionGuardian,
        model_runner: Optional[Callable[[AICycleContext], AIActionOutput]] = None,
        agent_policy_id: str = "ai_led_qwen9b",
    ):
        self.store = store
        self.gateway = execution_gateway
        self.risk_engine = risk_engine
        self.ledger = ledger
        self.guardian = guardian
        self.model_runner = model_runner
        self.agent_policy_id = agent_policy_id
        self._ensure_tables()

    def _get_conn(self):
        if hasattr(self.ledger, "_get_conn"):
            return self.ledger._get_conn()
        if hasattr(self.store, "_connect"):
            return self.store._connect()
        return None

    def _ensure_tables(self) -> None:
        try:
            conn = self.ledger._get_conn() if hasattr(self.ledger, "_get_conn") else None
            if conn:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_led_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    latency_ms REAL,
                    order_intent_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """)
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(ai_led_cycles)").fetchall()}
                for column, definition in (
                    ("session_id", "TEXT"),
                    ("generation", "INTEGER"),
                    ("authorization_id", "TEXT"),
                    ("market_snapshot_hash", "TEXT"),
                ):
                    if column not in columns:
                        conn.execute(f"ALTER TABLE ai_led_cycles ADD COLUMN {column} {definition}")
                from .institutional_schema import ensure_institutional_trader_schema
                ensure_institutional_trader_schema(conn)
                conn.commit()
            elif hasattr(self.store, "_connect"):
                with self.store._connect() as db:
                    db.execute("""
                    CREATE TABLE IF NOT EXISTS ai_led_cycles (
                        cycle_id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        latency_ms REAL,
                        order_intent_id TEXT,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """)
                    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(ai_led_cycles)").fetchall()}
                    for column, definition in (
                        ("session_id", "TEXT"),
                        ("generation", "INTEGER"),
                        ("authorization_id", "TEXT"),
                        ("market_snapshot_hash", "TEXT"),
                    ):
                        if column not in columns:
                            db.execute(f"ALTER TABLE ai_led_cycles ADD COLUMN {column} {definition}")
                    from .institutional_schema import ensure_institutional_trader_schema
                    ensure_institutional_trader_schema(db)
        except Exception:
            pass

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            point = value
        elif value:
            try:
                point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        else:
            return None
        return point.replace(tzinfo=timezone.utc) if point.tzinfo is None else point.astimezone(timezone.utc)

    def _cycle_latency_ms(self, context: AICycleContext) -> float:
        started = self._parse_time(context.started_at)
        if started is None:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds() * 1000.0)

    def _persist_cycle(self, result: AICycleResult, context: AICycleContext) -> None:
        output = result.action_output
        origin = str(result.decision_origin or getattr(output, "decision_origin", "SYSTEM") or "SYSTEM").upper()
        extra = getattr(output, "extra_fields", {}) or {}
        if origin == "MODEL" and extra.get("is_model_decision") is False:
            origin = "SYSTEM"
        model_called = context.model_call_attempted or (bool(extra.get("model_called", origin == "MODEL")) if origin == "MODEL" else False)
        model_result = str(
            extra.get("model_result")
            or (output.action if origin == "MODEL" else "INVALID_OR_DISCARDED" if model_called else "NOT_RUN")
        )
        block_stage = infer_block_stage(result.reason, extra.get("block_stage") or getattr(context, "stage_block", None)) if origin != "MODEL" else None
        human_message = str(extra.get("human_message") or humanize_reason(result.reason, stage=block_stage))
        persisted_action = output.action if origin == "MODEL" else "SYSTEM_BLOCKED"
        stage_trace = build_stage_trace(
            context,
            result_status=result.status,
            result_reason=result.reason,
            decision_origin=origin,
            model_called=model_called,
            model_result=model_result,
            completed_at=datetime.now(timezone.utc),
            execution_result=result.execution_result,
            order_intent=result.order_intent,
        )
        is_model = (origin == "MODEL")
        payload = {
            "cycle_id": result.cycle_id,
            "action": persisted_action,
            "model_action": output.action if not is_model else None,
            "instrument_id": output.instrument_id,
            "reason": output.reason,
            "decision_id": output.decision_id,
            "model_output": asdict(output),
            "analysis": dict(output.extra_fields) if is_model else None,
            "strategy_plan": output.extra_fields.get("strategy_plan") if is_model else None,
            "strategy_instructions": getattr(context, "strategy_instructions", None),
            "performance_context": getattr(context, "performance_context", None),
            "account_id": context.account_id,
            "venue": context.venue,
            "mode": context.mode.value if isinstance(context.mode, TradingMode) else str(context.mode),
            "environment": context.execution_environment or context.environment,
            "provider": context.provider or context.venue,
            "generation": context.generation,
            "session_id": context.session_id,
            "authorization_id": context.authorization_id,
            "authorization_version": context.authorization_version,
            "lease_holder_id": context.lease_holder_id,
            "fencing_token": context.fencing_token,
            "model": (context.model_id or DEFAULT_SMART_MODEL) if is_model else None,
            "model_id": context.model_id if is_model else None,
            "model_receipt": context.model_inference_settings if is_model else None,
            "model_version": context.model_version if is_model else None,
            "prompt_version": context.prompt_version if is_model else None,
            "input_hash": context.input_hash if is_model else None,
            "model_digest": context.model_digest if is_model else None,
            "model_digest_status": context.model_digest_status if is_model else "UNKNOWN_NOT_PROVIDED",
            "model_quantization": context.model_quantization if is_model else None,
            "model_inference_settings": context.model_inference_settings,
            "model_latency_ms": context.model_latency_ms,
            "model_raw_response": context.model_raw_response,
            "model_call_prompt_version": context.model_call_prompt_version,
            "evidence_bundle_id": context.evidence_bundle_id,
            "evidence_status": context.evidence_status,
            "evidence_context_refs": list(context.evidence_refs),
            "market_snapshot_hash": hashlib.sha256(
                json.dumps(context.market_snapshots, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            "started_at": context.started_at,
            "expires_at": context.expires_at,
            "scheduled_at": context.scheduled_at,
            "candidate_count": len(context.candidates),
            "candidates": [
                {
                    "candidate_id": item.get("candidate_id"),
                    "symbol": item.get("symbol"),
                    "strategy_id": item.get("strategy_id"),
                    "status": item.get("status"),
                    "closed_15m_bar": item.get("closed_15m_bar"),
                }
                for item in context.candidates[:32]
            ],
            "calibration": context.calibration,
            "market_data_environment": context.market_data_environment,
            "data_quality": context.data_quality,
            "strategy_readiness": context.strategy_readiness,
            "decision_contract": context.decision_contract,
            "technical_context": context.technical_context,
            "news_revisions": context.news_revisions,
            "indicator_snapshot_id": context.indicator_snapshot_id,
            "decision_memory": context.decision_memory[:20],
            "evidence_refs": list(output.evidence_refs),
            "order_selection": (
                {
                    "preference": output.order_preference,
                    "limit_price": output.limit_price,
                    "ttl_seconds": output.ttl_seconds,
                    "candidate_id": output.candidate_id,
                    "closed_15m_bar": output.closed_15m_bar,
                }
                if is_model
                else None
            ),
            "order_intent": result.order_intent.to_dict() if result.order_intent else None,
            "execution_result": result.execution_result,
            "decision_origin": origin,
            "operational_state": result.operational_state,
            "is_model_decision": origin == "MODEL" and model_called,
            "model_called": model_called,
            "model_result": model_result,
            "block_stage": block_stage,
            "human_message": human_message,
            "stage_trace": stage_trace,
        }
        iid = result.order_intent.intent_id if result.order_intent else None
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            conn = self.ledger._get_conn() if hasattr(self.ledger, "_get_conn") else None
            if conn:
                conn.execute(
                    """INSERT OR REPLACE INTO ai_led_cycles
                       (cycle_id, account_id, action, status, reason, latency_ms, order_intent_id, payload_json, created_at,
                        session_id, generation, authorization_id, market_snapshot_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (result.cycle_id, context.account_id, persisted_action, result.status, result.reason, self._cycle_latency_ms(context), iid, json.dumps(payload, allow_nan=False), now_iso,
                     context.session_id, context.generation, context.authorization_id, payload["market_snapshot_hash"]),
                )
                conn.execute(
                    """UPDATE ai_led_cycles SET started_at=?, scheduled_at=?,
                       calibration_state=?, candidate_count=?, provider=?, environment=?,
                       operational_state=?, decision_origin=?, model_call_status=?,
                       model_called=?, model_result=?, block_stage=?, human_message=?,
                       stage_trace_json=?
                       WHERE cycle_id=?""",
                    (context.started_at, context.scheduled_at, (context.calibration or {}).get("status"), len(context.candidates), context.provider or context.venue, context.execution_environment or context.environment, result.operational_state, origin, "MODEL_DECISION" if model_called else "NOT_CALLED", int(model_called), model_result, block_stage, human_message, json.dumps(stage_trace, ensure_ascii=False, allow_nan=False), result.cycle_id),
                )
                conn.commit()
                persist_stage_trace(self.store, cycle_id=result.cycle_id, account_id=context.account_id, trace=stage_trace)
            elif hasattr(self.store, "_connect"):
                with self.store._connect() as db:
                    db.execute(
                        """INSERT OR REPLACE INTO ai_led_cycles
                           (cycle_id, account_id, action, status, reason, latency_ms, order_intent_id, payload_json, created_at,
                            session_id, generation, authorization_id, market_snapshot_hash)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (result.cycle_id, context.account_id, persisted_action, result.status, result.reason, self._cycle_latency_ms(context), iid, json.dumps(payload, allow_nan=False), now_iso,
                         context.session_id, context.generation, context.authorization_id, payload["market_snapshot_hash"]),
                    )
                    db.execute(
                        """UPDATE ai_led_cycles SET started_at=?, scheduled_at=?,
                           calibration_state=?, candidate_count=?, provider=?, environment=?,
                           operational_state=?, decision_origin=?, model_call_status=?,
                           model_called=?, model_result=?, block_stage=?, human_message=?,
                           stage_trace_json=?
                           WHERE cycle_id=?""",
                        (context.started_at, context.scheduled_at, (context.calibration or {}).get("status"), len(context.candidates), context.provider or context.venue, context.execution_environment or context.environment, result.operational_state, origin, "MODEL_DECISION" if model_called else "NOT_CALLED", int(model_called), model_result, block_stage, human_message, json.dumps(stage_trace, ensure_ascii=False, allow_nan=False), result.cycle_id),
                    )
                    persist_stage_trace(self.store, cycle_id=result.cycle_id, account_id=context.account_id, trace=stage_trace)
        except Exception as e:
            logger.warning("Failed to persist ai_led_cycle: %s", e)

    def _compute_market_readiness_score(self, context: AICycleContext) -> float:
        """从已收盘的 15m 技术指标动态评估当前全市场机会就绪度（0~100%）。"""
        scores = []
        for symbol in context.allowed_instruments:
            tc = (context.technical_context or {}).get(symbol, {})
            frame = (tc.get("timeframes") or {}).get("15m") or {}
            ind = frame.get("indicators") or {}
            support = ind.get("support20")
            resistance = ind.get("resistance20")
            rsi = ind.get("rsi14_simple")
            vol_ratio = ind.get("volume_ratio20")
            quote = ((context.market_snapshots or {}).get(symbol) or {}).get("price") or (frame.get("last_close"))
            if not all(isinstance(v, (int, float)) and v > 0 for v in (support, resistance, quote)):
                continue
            span = max(1e-6, resistance - support)
            dist_to_key = min(abs(quote - support), abs(quote - resistance))
            proximity_score = max(0.0, min(40.0, 40.0 * (1.0 - (dist_to_key / span))))
            rsi_score = 20.0
            if isinstance(rsi, (int, float)):
                if 40 <= rsi <= 60:
                    rsi_score = 25.0
                elif (30 <= rsi < 40) or (60 < rsi <= 70):
                    rsi_score = 35.0
                elif rsi < 30 or rsi > 70:
                    rsi_score = 15.0
            vol_score = 15.0
            if isinstance(vol_ratio, (int, float)) and vol_ratio > 0:
                vol_score = max(5.0, min(25.0, vol_ratio * 15.0))
            symbol_score = round(proximity_score + rsi_score + vol_score, 1)
            scores.append(symbol_score)
        if scores:
            return max(scores)
        return 50.0

    def _live_execution_quote(
        self,
        symbol: str,
        *,
        environment: Any = None,
        venue: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Read the venue's live last price right before order dispatch."""
        raw_mode = (
            environment
            or getattr(self, "execution_environment", "")
            or getattr(self, "mode", "")
            or getattr(getattr(self, "gateway", None), "mode", "")
            or ""
        )
        mode = str(getattr(raw_mode, "value", raw_mode)).upper()
        if mode not in {"TESTNET", "LIVE"}:
            return None
        target_venue = str(venue or getattr(self, "venue", "") or "gate").lower()
        if target_venue != "gate":
            return None
        try:
            from core.providers import gateio_provider
            GatePublicProvider = getattr(gateio_provider, "GatePublicProvider", None)
            if GatePublicProvider is None:
                from core.providers.gateio_provider import GatePublicProvider
            is_testnet = (mode == "TESTNET")
            provider = getattr(self, "_live_quote_provider", None)
            if provider is None or getattr(provider, "testnet", None) != is_testnet:
                provider = GatePublicProvider(testnet=is_testnet)
                self._live_quote_provider = provider
            ticker = provider._native_ticker(symbol)
        except Exception:
            return None
        if not isinstance(ticker, dict):
            return None
        raw_price = ticker.get("last") or ticker.get("last_price") or ticker.get("mark_price")
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None
        wall_now = datetime.now(timezone.utc)
        stamp = wall_now if now is None or wall_now >= now else now
        return {
            "price": price,
            "data_as_of": stamp.isoformat(),
            "received_at": stamp.isoformat(),
            "source": "gate_native_ticker",
            "environment": mode,
            "venue": target_venue,
        }

    def execute_cycle(
        self,
        context: AICycleContext,
        now: Optional[datetime] = None,
        model_output: Optional[AIActionOutput] = None,
    ) -> AICycleResult:
        """Validate and execute exactly one scoped AI cycle.

        The coordinator supplies timestamped market snapshots.  Direct unit
        callers may provide an unstamped snapshot; it is stamped at call time
        only when its context is current, so an old context still fails the
        gateway freshness check.  An intent price is never used as market data.
        """
        now = now or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        default_symbol = context.allowed_instruments[0] if context.allowed_instruments else "BTCUSDT"

        def finish(
            output: AIActionOutput,
            status: str,
            reason: str,
            *,
            intent: Optional[OrderIntent] = None,
            execution_result: Optional[Dict[str, Any]] = None,
            operational_state: str | None = None,
        ) -> AICycleResult:
            origin = str(getattr(output, "decision_origin", "MODEL") or "MODEL").upper()
            result = AICycleResult(
                cycle_id=context.cycle_id,
                action_output=output,
                status=status,
                reason=reason,
                order_intent=intent,
                execution_result=execution_result,
                decision_origin=origin,
                operational_state=operational_state or ("MODEL_DECISION" if origin == "MODEL" else "SYSTEM_BLOCKED"),
            )
            self._persist_cycle(result, context)
            return result

        def system_output(reason: str, *, block_stage: str | None = None) -> AIActionOutput:
            """Represent a guard/availability fact without a fake model WAIT."""

            stage = infer_block_stage(reason, block_stage)
            return AIActionOutput(
                action="WAIT",  # compatibility for callers; persistence/API use SYSTEM_BLOCKED
                instrument_id=default_symbol,
                reason=reason,
                decision_origin="SYSTEM",
                extra_fields={
                    "is_model_decision": False,
                    "model_called": False,
                    "model_result": "NOT_RUN",
                    "block_stage": stage,
                    "operational_state": "SYSTEM_BLOCKED",
                    "blocked_code": str(reason).split(":", 1)[0],
                    "human_message": humanize_reason(reason, stage=stage),
                },
            )

        def market_for(symbol: str) -> Dict[str, Any]:
            exec_snaps = getattr(context, "execution_market_snapshots", None)
            if isinstance(exec_snaps, dict) and symbol in exec_snaps:
                raw = exec_snaps.get(symbol) or {}
            else:
                raw = context.market_snapshots.get(symbol) or {}
            market = dict(raw) if isinstance(raw, dict) else {}
            if market.get("price") is None:
                market["price"] = market.get("last", market.get("close"))
            if market.get("price") is None:
                try:
                    persisted = self.gateway._fresh_market_snapshot(symbol)
                except GatewayError:
                    persisted = None
                if persisted:
                    return persisted
            live = self._live_execution_quote(
                symbol,
                environment=context.execution_environment or context.mode,
                venue=context.venue,
                now=now,
            )
            if live:
                market["price"] = live["price"]
                market["data_as_of"] = live["data_as_of"]
                market["received_at"] = live["received_at"]
                market["source"] = live["source"]
                market["freshness_status"] = "FRESH"
                market["fresh"] = True
                return market
            wall_now = datetime.now(timezone.utc)
            if not any(market.get(key) for key in ("data_as_of", "timestamp", "bar_end")):
                started = self._parse_time(context.started_at)
                reference = started if started and started <= wall_now + timedelta(seconds=5) else wall_now
                market["data_as_of"] = reference.isoformat()
            market.setdefault("received_at", wall_now.isoformat())
            market.setdefault("source", "ai_cycle_snapshot")
            market.setdefault("freshness_status", "FRESH")
            market.setdefault("fresh", True)
            return market

        expires_at_dt = self._parse_time(context.expires_at)
        if expires_at_dt is None:
            output = system_output("INVALID_CYCLE_EXPIRATION", block_stage="ACCOUNT")
            return finish(output, "REJECTED", "INVALID_CYCLE_EXPIRATION")
        if now > expires_at_dt:
            output = system_output("MODEL_TIMEOUT_DISCARDED", block_stage="AI_MODEL")
            return finish(
                output,
                "TIMEOUT_DISCARDED",
                f"Cycle timeout: current time {now_iso} > expiration {context.expires_at}",
            )

        if model_output is None:
            if self.model_runner is None:
                output = system_output("SMART_MODEL_UNAVAILABLE", block_stage="AI_MODEL")
                return finish(output, "BLOCKED", "SMART_MODEL_UNAVAILABLE")
            try:
                output = self.model_runner(context)
            except Exception as exc:
                output = system_output(f"SMART_MODEL_UNAVAILABLE: {type(exc).__name__}", block_stage="AI_MODEL")
                return finish(output, "BLOCKED", "SMART_MODEL_UNAVAILABLE")
        else:
            output = model_output

        if not isinstance(output, AIActionOutput):
            output = system_output("INVALID_MODEL_OUTPUT_SCHEMA", block_stage="AI_MODEL")
            return finish(output, "REJECTED", "INVALID_MODEL_OUTPUT_SCHEMA")

        if str(getattr(output, "decision_origin", "MODEL") or "MODEL").upper() != "MODEL" or (getattr(output, "extra_fields", {}) or {}).get("is_model_decision") is False:
            return finish(
                output,
                "BLOCKED",
                output.reason or "系统前置条件未满足",
                operational_state=str((getattr(output, "extra_fields", {}) or {}).get("operational_state") or "SYSTEM_BLOCKED"),
            )

        if output.action not in ALLOWED_AI_ACTIONS:
            return finish(output, "REJECTED", f"INVALID_ACTION_SCHEMA: Unknown action '{output.action}'")

        if output.action in ("WAIT", "HOLD"):
            extra = dict(getattr(output, "extra_fields", None) or {})
            strat_analysis = dict(extra.get("strategy_analysis") or {})
            if "trigger_completion_pct" not in strat_analysis or strat_analysis.get("trigger_completion_pct") is None:
                dynamic_score = self._compute_market_readiness_score(context)
                strat_analysis["trigger_completion_pct"] = dynamic_score
                strat_analysis["market_readiness"] = dynamic_score
                extra["strategy_analysis"] = strat_analysis
                extra["market_readiness"] = dynamic_score
                output.extra_fields = extra
            return finish(
                output,
                "WAITING" if output.action == "WAIT" else "HOLDING",
                output.reason or f"AI opted to {output.action}",
            )

        if output.instrument_id not in context.allowed_instruments:
            return finish(
                output,
                "REJECTED",
                f"INSTRUMENT_NOT_AUTHORIZED: {output.instrument_id} not in allowed pool {context.allowed_instruments}",
            )

        mode_scope = context.mode.value if isinstance(context.mode, TradingMode) else str(context.mode)
        if (
            str(mode_scope).upper() == TradingMode.TESTNET.value
            and str(context.venue or "").lower() == "gate"
            and isinstance(context.account_truth, dict)
            and context.account_truth
        ):
            open_positions = ExecutionGateway._gate_remote_positions_for_risk(
                context.account_truth,
                context.account_id,
            )
        else:
            open_positions = self.ledger.get_open_positions(
                context.account_id,
                venue=context.venue,
                mode=mode_scope,
            )
        existing_positions = [
            position
            for position in open_positions
            if str(position.get("instrument_id", position.get("symbol", ""))).upper() == output.instrument_id.upper()
            and str(position.get("venue", context.venue)).lower() == str(context.venue).lower()
            and str(position.get("mode", mode_scope)).upper() == mode_scope.upper()
        ]
        if output.action in ("CLOSE_POSITION", "REDUCE_POSITION") and output.position_id is None and len(existing_positions) != 1:
            return finish(output, "REJECTED", "POSITION_ID_REQUIRED: Multiple or zero scoped positions require an explicit position_id")
        existing_pos = next(
            (position for position in existing_positions if not output.position_id or position.get("position_id") == output.position_id),
            None,
        )

        if output.action in ("OPEN_LONG", "OPEN_SHORT"):
            from .autonomous_strategy import CONTRACT, validate_entry
            if context.decision_contract == CONTRACT:
                settings = context.model_inference_settings if isinstance(context.model_inference_settings, dict) else {}
                receipt = {
                    "model_id": context.model_id or DEFAULT_SMART_MODEL,
                    "actual_model_id": settings.get("actual_model_id"),
                    "model_identity_source": settings.get("model_identity_source"),
                    "verified_manifest_model_id": settings.get("verified_manifest_model_id"),
                    "model_version": context.model_version,
                }
                if (
                    not context.model_call_attempted
                    or not getattr(context, "model_call_completed", False)
                    or not is_verified_bonsai_inference_receipt(receipt)
                ):
                    output = system_output("BONSAI_INFERENCE_RECEIPT_UNVERIFIED", block_stage="AI_MODEL")
                    return finish(output, "BLOCKED", "BONSAI_INFERENCE_RECEIPT_UNVERIFIED")
                rejection = validate_entry(context, output, now)
                if rejection:
                    return finish(output, "BLOCKED", rejection)

            strategy_instructions = getattr(context, "strategy_instructions", None) or {}
            strategy_exec = strategy_instructions.get("execution") if isinstance(strategy_instructions, dict) else {}
            max_positions = strategy_exec.get("max_positions") if isinstance(strategy_exec, dict) else None
            if max_positions is not None:
                try:
                    max_pos_int = int(max_positions)
                    truth = context.account_truth if isinstance(context.account_truth, dict) else {}
                    pending = truth.get("pending_orders") if isinstance(truth.get("pending_orders"), list) else []
                    working_slots = len([
                        o for o in pending
                        if not o.get("reduce_only") and str(o.get("status", "")).upper() in ("OPEN", "NEW", "ACCEPTED")
                    ])
                    total_occupied = len(open_positions) + working_slots
                    if total_occupied >= max_pos_int:
                        return finish(output, "BLOCKED", f"MAX_POSITIONS_REACHED: {total_occupied} >= {max_pos_int}")
                except (TypeError, ValueError):
                    pass
            if existing_positions:
                target_side = "LONG" if output.action == "OPEN_LONG" else "SHORT"
                existing_side = str(existing_positions[0].get("side", "")).upper()
                if existing_side != target_side:
                    return finish(output, "REJECTED", f"REVERSE_OPENING_FORBIDDEN: Must close existing {existing_side} position before opening {target_side}")
                return finish(output, "REJECTED", "DOUBLE_ENTRY_FORBIDDEN: Position already open on this instrument")

            # NOFX Autopilot: Re-entry cooldown gate prevents immediate whipsaws after close
            strategy_instructions = getattr(context, "strategy_instructions", None) or {}
            throttle_config = strategy_instructions.get("trade_throttle") or strategy_instructions.get("throttle") or (strategy_instructions.get("execution") or {}).get("throttle")
            throttle_active = bool(
                strategy_instructions.get("throttle_enabled")
                or (isinstance(throttle_config, dict) and throttle_config.get("enabled", True))
                or (strategy_instructions.get("nofx_runtime") is not None)
                or (strategy_instructions.get("profile") is not None)
            )
            if throttle_active and hasattr(self.store, "_connect") and context.account_id:
                from .order_selection import TradeThrottlePolicy
                strategy_exec = strategy_instructions.get("execution") if isinstance(strategy_instructions, dict) else {}
                strategy_profile = strategy_instructions.get("profile") if isinstance(strategy_instructions, dict) else {}
                cooldown_sec = float((strategy_exec or {}).get("cooldown_minutes", (strategy_profile or {}).get("cooldown_minutes", 60))) * 60.0
                try:
                    with self.store._connect() as db:
                        last_close = db.execute(
                            """SELECT created_at FROM order_intents
                               WHERE account_id=? AND instrument_id=? AND reduce_only=1
                                 AND status IN ('FILLED', 'ACKNOWLEDGED')
                               ORDER BY created_at DESC LIMIT 1""",
                            (context.account_id, output.instrument_id),
                        ).fetchone()
                        if last_close and last_close[0]:
                            try:
                                last_close_dt = datetime.fromisoformat(str(last_close[0]).replace("Z", "+00:00"))
                                open_ok, open_code, open_reason = TradeThrottlePolicy.check_open_throttle(
                                    output.instrument_id,
                                    has_open_position=False,
                                    last_closed_at=last_close_dt,
                                    now=now,
                                    cooldown_seconds=cooldown_sec,
                                )
                                if not open_ok:
                                    return finish(output, "BLOCKED", f"{open_code}: {open_reason}")
                            except Exception:
                                pass
                except Exception:
                    pass

            req_risk = output.requested_risk_fraction if output.requested_risk_fraction is not None else 0.0025
            try:
                req_risk_value = float(req_risk)
                max_risk_value = float(context.max_risk_fraction)
            except (TypeError, ValueError):
                return finish(output, "REJECTED", "INVALID_RISK_FRACTION")
            if req_risk_value <= 0 or req_risk_value > max_risk_value:
                return finish(output, "REJECTED", f"RISK_LIMIT_EXCEEDED: Requested risk {req_risk_value:.4f} > limit {max_risk_value:.4f}")

            market_snap = market_for(output.instrument_id)
            try:
                current_price = float(market_snap.get("price"))
            except (TypeError, ValueError):
                current_price = 0.0
            if current_price <= 0:
                return finish(output, "REJECTED", "MARKET_DATA_UNAVAILABLE")

            mode_val = context.mode if isinstance(context.mode, TradingMode) else TradingMode(str(context.mode).upper())
            if mode_val is TradingMode.PAPER and market_snap.get("slippage") is None:
                # The local PAPER contract owns a deterministic 10 bps
                # adverse-slippage bound.  Supplying it here lets AUTO select
                # MARKET only after the same bound is represented in the
                # selection evidence; remote TestNet/Live paths must still
                # provide an observed venue value and are never defaulted.
                market_snap["slippage"] = 0.001
            candidate = next(
                (
                    item for item in context.candidates
                    if str(item.get("symbol") or "").upper() == output.instrument_id.upper()
                    and str(item.get("status") or "").upper() == "PROPOSAL"
                    and output.candidate_id and str(item.get("candidate_id")) == str(output.candidate_id)
                ),
                None,
            )
            if output.candidate_id and candidate is None:
                return finish(output, "REJECTED", "CANDIDATE_SCOPE_MISMATCH")
            candidate_id = str((candidate or {}).get("candidate_id") or output.candidate_id or "") or None
            closed_15m_bar = str((candidate or {}).get("closed_15m_bar") or output.closed_15m_bar or "") or None
            candidate_proposal = (candidate or {}).get("proposal") if isinstance((candidate or {}).get("proposal"), dict) else {}
            entry_reference = output.entry_price or candidate_proposal.get("entry") or current_price
            try:
                entry_reference = float(entry_reference)
            except (TypeError, ValueError):
                entry_reference = current_price
            market_info_for_selection = market_snap.get("market") or market_snap.get("metadata") or {}
            precision_for_selection = market_info_for_selection.get("precision") if isinstance(market_info_for_selection, dict) else {}
            slippage_value = market_snap.get("slippage")
            if slippage_value is None:
                slippage_value = market_snap.get("slippage_rate")
            try:
                slippage_bps = float(slippage_value) * 10_000 if slippage_value is not None else None
            except (TypeError, ValueError):
                slippage_bps = None
            signal_at = closed_15m_bar or output.closed_15m_bar or output.entry_condition or context.started_at
            selection = OrderSelectionPolicy.select(
                OrderSelectionInput(
                    side="LONG" if output.action == "OPEN_LONG" else "SHORT",
                    preference=output.order_preference or "AUTO",
                    quote=current_price,
                    bid=market_snap.get("bid"),
                    ask=market_snap.get("ask"),
                    limit_price=output.limit_price or output.entry_price or candidate_proposal.get("entry"),
                    entry_zone_low=(output.extra_fields.get("entry_zone") or {}).get("low") or market_snap.get("entry_zone_low") or (entry_reference * 0.995),
                    entry_zone_high=(output.extra_fields.get("entry_zone") or {}).get("high") or market_snap.get("entry_zone_high") or (entry_reference * 1.005),
                    signal_at=signal_at,
                    now=now,
                    slippage_bps=slippage_bps,
                    liquidity_ok=market_snap.get("liquidity_ok"),
                    tick_size=(precision_for_selection or {}).get("price") if isinstance(precision_for_selection, dict) else None,
                    ttl_seconds=output.ttl_seconds or DEFAULT_LIMIT_TTL_SECONDS,
                    pending_candidate=bool(candidate_id and any(
                        str(item.get("candidate_id") or "") == candidate_id
                        for item in context.candidates
                        if bool(item.get("pending_order"))
                    )),
                    market_is_remote=mode_val in (TradingMode.TESTNET, TradingMode.LIVE),
                    evidence_refs=tuple(output.evidence_refs),
                )
            )
            if not selection.allowed:
                return finish(output, "BLOCKED", selection.reason_code)
            output.order_preference = selection.preference
            output.limit_price = selection.limit_price
            output.ttl_seconds = selection.ttl_seconds
            output.candidate_id = candidate_id
            output.closed_15m_bar = closed_15m_bar

            stop_price = output.stop_price
            if stop_price is None or stop_price <= 0:
                return finish(output, "REJECTED", "INVALID_STOP_PRICE: Stop price must be positive")
            side = "LONG" if output.action == "OPEN_LONG" else "SHORT"
            if (side == "LONG" and stop_price >= current_price) or (side == "SHORT" and stop_price <= current_price):
                return finish(output, "REJECTED", f"INVALID_STOP_DIRECTION: Stop {stop_price} is invalid for {side} at executable quote {current_price}")

            market_info = market_snap.get("market") or market_snap.get("metadata") or {}
            if mode_val in (TradingMode.TESTNET, TradingMode.LIVE):
                amount_precision = (market_info.get("precision") or {}).get("amount")
                amount_limits = (market_info.get("limits") or {}).get("amount") or {}
                has_contract_rules = all(
                    value is not None
                    for value in (
                        market_info.get("contractSize", market_snap.get("contractSize")),
                        amount_precision or amount_limits.get("step"),
                        amount_limits.get("min"),
                        amount_limits.get("max"),
                    )
                )
                has_cost_rules = (
                    market_snap.get("fee_rate") is not None
                    or market_info.get("taker") is not None
                    or market_snap.get("taker_fee") is not None
                ) and market_snap.get("slippage") is not None
                if not has_contract_rules or not has_cost_rules:
                    return finish(
                        output,
                        "BLOCKED",
                        "MARKET_RULES_UNAVAILABLE_REMOTE: AI-led opening requires adapter-provided contract, fee, and slippage evidence",
                    )

            try:
                account_snapshot = self.ledger.get_snapshot(context.account_id)
                contract_size = float(market_info.get("contractSize", market_info.get("contract_size", market_snap.get("contractSize", 1.0))))
                fee_rate = float(market_snap.get("fee_rate", market_info.get("taker", market_snap.get("taker_fee", 0.0005))))
                slippage = float(market_snap.get("slippage", 0.001))
                precision = market_info.get("precision", {}) or {}
                step_size = float(precision.get("amount", market_snap.get("step", 0.001)))
                if contract_size <= 0 or fee_rate < 0 or slippage < 0 or step_size <= 0:
                    raise ValueError("invalid market metadata")
                # Match the gateway's adverse executable-entry contract before
                # requesting a quantity.  Sizing from the informational quote
                # would round a request just above the authoritative budget
                # once PAPER slippage is applied by the gateway.
                sign = 1.0 if side == "LONG" else -1.0
                executable_entry = (
                    float(selection.limit_price)
                    if selection.order_type == "limit" and selection.limit_price is not None
                    else current_price * (1.0 + sign * slippage)
                )
                unit_risk = (abs(executable_entry - stop_price) + stop_price * slippage + (executable_entry + stop_price) * fee_rate) * contract_size
                strategy_exec = (getattr(context, "strategy_instructions", None) or {}).get("execution")
                if strategy_exec and isinstance(strategy_exec, dict):
                    from .strategy_execution import (
                        normalize_execution,
                        resolve_execution_leverage,
                        size_position,
                    )
                    from .risk_engine import compute_rule_based_leverage_score
                    exec_config = normalize_execution(strategy_exec)
                    equity = float(account_snapshot.net_equity)
                    avail = float(getattr(account_snapshot, "available_balance", equity))
                    used_m = float(getattr(account_snapshot, "used_margin", 0.0))
                    confidence = float((output.extra_fields or {}).get("confidence") or 0.8)
                    strategy_profile = strategy_instructions.get("profile") if isinstance(strategy_instructions, dict) else {}
                    rule_leverage, rule_reason = compute_rule_based_leverage_score(
                        str((strategy_profile or {}).get("strategy_id") or "ai_led"),
                        confidence / 100.0 if confidence > 1 else confidence,
                        executable_entry,
                        stop_price,
                    )
                    leverage_resolution = resolve_execution_leverage(
                        exec_config,
                        market=market_info,
                        rule_leverage=rule_leverage,
                        requested_leverage=output.requested_leverage,
                        require_venue_limit=(mode_val is not TradingMode.PAPER),
                    )
                    lev = int(leverage_resolution["leverage"])
                    leverage_resolution["risk_rule_reason"] = rule_reason
                    output.extra_fields["leverage_resolution"] = leverage_resolution
                    sizing_res = size_position(
                        exec_config,
                        equity=equity,
                        available=avail,
                        used_margin=used_m,
                        entry=executable_entry,
                        unit_risk=unit_risk,
                        contract_size=contract_size,
                        step=step_size,
                        risk_fraction=req_risk_value,
                        leverage=lev,
                        fee_rate=fee_rate,
                        requested_notional=getattr(output, "position_size_usdt", None),
                    )
                    output.extra_fields["position_sizing"] = sizing_res
                    qty = sizing_res["quantity"]
                    raw_qty = qty
                else:
                    raw_qty = float(account_snapshot.net_equity) * req_risk_value / unit_risk
                    qty = float((Decimal(str(raw_qty)) / Decimal(str(step_size))).quantize(Decimal("1"), rounding="ROUND_DOWN") * Decimal(str(step_size)))
            except (TypeError, ValueError, ArithmeticError) as exc:
                return finish(output, "REJECTED", f"RISK_SIZING_UNAVAILABLE: {exc}")
            if qty <= 0:
                return finish(output, "REJECTED", f"ORDER_QTY_BELOW_MINIMUM: Computed quantity {raw_qty:.12g} below market step {step_size}")
            if context.decision_contract == CONTRACT and mode_val is not TradingMode.PAPER:
                from .autonomous_strategy import number
                depth = number((market_snap.get("depth_contracts") or {}).get("asks" if side == "LONG" else "bids"))
                if depth is None or qty > depth:
                    return finish(output, "BLOCKED", "AI_ORDER_EXCEEDS_OBSERVED_DEPTH")

            initial_risk_dist = abs(float(executable_entry) - float(stop_price))
            trailing_config = None
            if initial_risk_dist > 0:
                activation_p = (
                    float(executable_entry) + 1.5 * initial_risk_dist
                    if side == "LONG"
                    else float(executable_entry) - 1.5 * initial_risk_dist
                )
                trailing_config = {
                    "type": "DISTANCE",
                    "value": float(round(initial_risk_dist, 6)),
                    "activation_price": float(round(activation_p, 6)),
                }

            protection = ProtectionPlan(
                stop_price=float(stop_price),
                take_profit=float(output.take_profit) if output.take_profit else None,
                trigger_type="mark",
                reduce_only=True,
                status=ProtectionStatus.PENDING,
                trailing_protection=trailing_config,
            )
            intent = OrderIntent(
                intent_id=f"intent_ai_{uuid.uuid4().hex[:12]}",
                idempotency_key=f"idemp_{context.account_id}_{context.cycle_id}_{output.instrument_id}_{side}",
                account_id=context.account_id,
                mode=mode_val,
                instrument_id=output.instrument_id,
                side=side,
                order_type=selection.order_type,
                quantity=qty,
                # Informational only; gateway uses market_snap's executable quote.
                price=selection.limit_price if selection.order_type == "limit" else current_price,
                leverage=(output.requested_leverage or 1) if context.decision_contract == CONTRACT else output.requested_leverage,
                protection_plan=protection,
                reduce_only=False,
                control_mode=ControlMode.AUTONOMOUS,
                decision_path=DecisionPath.AI_LED,
                session_id=context.session_id or context.cycle_id,
                strategy_id="ai_led",
                strategy_version=self.agent_policy_id,
                signal_at=now_iso,
                status=OrderStatus.CREATED,
                created_at=now_iso,
                venue=context.venue,
                environment=context.environment or mode_val.value,
                cycle_id=context.cycle_id,
                generation=context.generation,
                authorization_id=context.authorization_id,
                authorization_version=context.authorization_version,
                lease_holder_id=context.lease_holder_id,
                fencing_token=context.fencing_token,
                expires_at=selection.expires_at or context.expires_at,
                candidate_id=candidate_id,
                closed_15m_bar=closed_15m_bar,
                order_preference=selection.preference,
                final_order_type=selection.order_type,
                selection_reason_code=selection.reason_code,
                selection_reason=selection.reason,
                selection_evidence=selection.evidence,
                limit_price=selection.limit_price,
                ttl_seconds=selection.ttl_seconds,
                selection_policy_version=selection.to_dict().get("policy_version"),
            )
            try:
                execution = self.gateway.submit_intent(intent, market_snapshot=market_snap)
            except GatewayError as exc:
                return finish(output, "REJECTED", f"GATEWAY_REJECTED: {exc.message}", intent=intent)
            final_status = str(execution.get("status", ""))
            if final_status not in {OrderStatus.FILLED.value, OrderStatus.PARTIALLY_FILLED.value, OrderStatus.ACKNOWLEDGED.value}:
                return finish(output, "REJECTED", f"GATEWAY_NOT_FILLED: {final_status or 'UNKNOWN'}", intent=intent, execution_result=execution)
            return finish(output, "EXECUTED", "Autonomous AI-led order submitted through the unified gateway", intent=intent, execution_result=execution)

        if output.action in ("CLOSE_POSITION", "REDUCE_POSITION"):
            if not existing_pos:
                return finish(output, "REJECTED", f"NO_OPEN_POSITION_TO_CLOSE: Instrument {output.instrument_id} has no scoped open position")
            pos_qty = float(existing_pos.get("remaining_contracts", existing_pos.get("quantity", 0.0)))
            if pos_qty <= 0:
                return finish(output, "REJECTED", "NO_REMAINING_POSITION_TO_CLOSE")
            if output.action == "CLOSE_POSITION":
                close_qty = pos_qty
            else:
                if output.reduce_fraction is None:
                    return finish(output, "REJECTED", "REDUCE_FRACTION_REQUIRED: AI must provide an explicit reduction fraction")
                fraction = output.reduce_fraction
                if not 0 < float(fraction) <= 1:
                    return finish(output, "REJECTED", "INVALID_REDUCE_FRACTION")
                close_qty = pos_qty * float(fraction)
                if close_qty >= pos_qty:
                    return finish(output, "REJECTED", "USE_CLOSE_POSITION_FOR_FULL_REDUCTION")
            exit_side = "SELL" if str(existing_pos.get("side", "")).upper() == "LONG" else "BUY"
            market_snap = market_for(output.instrument_id)
            try:
                current_price = float(market_snap.get("price"))
            except (TypeError, ValueError):
                current_price = 0.0
            if current_price <= 0:
                return finish(output, "REJECTED", "MARKET_DATA_UNAVAILABLE")

            # NOFX Autopilot: Guard against premature noise close within min hold window
            strategy_instructions = getattr(context, "strategy_instructions", None) or {}
            throttle_config = strategy_instructions.get("trade_throttle") or strategy_instructions.get("throttle") or (strategy_instructions.get("execution") or {}).get("throttle")
            throttle_active = bool(
                strategy_instructions.get("throttle_enabled")
                or (isinstance(throttle_config, dict) and throttle_config.get("enabled", True))
                or (strategy_instructions.get("nofx_runtime") is not None)
                or (strategy_instructions.get("profile") is not None)
            )
            if throttle_active:
                from .order_selection import TradeThrottlePolicy
                entry_p = float(existing_pos.get("entry_price") or current_price)
                if entry_p > 0:
                    side_mult = 1.0 if str(existing_pos.get("side", "")).upper() == "LONG" else -1.0
                    price_pnl_pct = (current_price - entry_p) / entry_p * 100.0 * side_mult
                else:
                    price_pnl_pct = 0.0
                raw_opened = existing_pos.get("opened_at") or existing_pos.get("created_at") or existing_pos.get("timestamp")
                entry_dt = None
                if raw_opened:
                    try:
                        if isinstance(raw_opened, (int, float)):
                            entry_dt = datetime.fromtimestamp(raw_opened / 1000.0 if raw_opened > 1e11 else raw_opened, tz=timezone.utc)
                        else:
                            entry_dt = datetime.fromisoformat(str(raw_opened).replace("Z", "+00:00"))
                    except Exception:
                        entry_dt = None
                throttle_allowed, throttle_code, throttle_reason = TradeThrottlePolicy.check_close_throttle(
                    output.instrument_id,
                    entry_time=entry_dt,
                    price_pnl_pct=price_pnl_pct,
                    now=datetime.now(timezone.utc),
                )
                if not throttle_allowed:
                    return finish(output, "BLOCKED", f"{throttle_code}: {throttle_reason}")

            mode_val = context.mode if isinstance(context.mode, TradingMode) else TradingMode(str(context.mode).upper())
            intent = OrderIntent(
                intent_id=f"intent_close_{uuid.uuid4().hex[:12]}",
                idempotency_key=f"idemp_close_{context.account_id}_{context.cycle_id}_{output.instrument_id}_{existing_pos.get('position_id', '')}",
                account_id=context.account_id,
                mode=mode_val,
                instrument_id=output.instrument_id,
                side=exit_side,
                order_type="market",
                quantity=close_qty,
                price=current_price,
                reduce_only=True,
                control_mode=ControlMode.AUTONOMOUS,
                decision_path=DecisionPath.AI_LED,
                session_id=context.session_id or context.cycle_id,
                strategy_id="ai_led",
                strategy_version=self.agent_policy_id,
                signal_at=now_iso,
                status=OrderStatus.CREATED,
                created_at=now_iso,
                venue=str(existing_pos.get("venue", context.venue)),
                environment=context.environment or mode_val.value,
                position_id=existing_pos.get("position_id"),
                cycle_id=context.cycle_id,
                generation=context.generation,
                authorization_id=context.authorization_id,
                authorization_version=context.authorization_version,
                lease_holder_id=context.lease_holder_id,
                fencing_token=context.fencing_token,
                expires_at=context.expires_at,
            )
            try:
                execution = self.gateway.submit_intent(intent, market_snapshot=market_snap)
            except GatewayError as exc:
                return finish(output, "REJECTED", f"GATEWAY_REJECTED: {exc.message}", intent=intent)
            return finish(output, "EXECUTED", f"Position {output.action} executed through the scoped gateway", intent=intent, execution_result=execution)

        if output.action == "TIGHTEN_STOP":
            if not existing_pos:
                return finish(output, "REJECTED", "NO_OPEN_POSITION_TO_TIGHTEN")
            new_stop = output.new_stop_price
            if not new_stop or new_stop <= 0:
                return finish(output, "REJECTED", "INVALID_TIGHTEN_STOP_PRICE")
            current_stop = float(existing_pos.get("stop") or existing_pos.get("stop_loss") or 0.0)
            side = str(existing_pos.get("side", "")).upper()
            if (side == "LONG" and new_stop <= current_stop) or (side == "SHORT" and current_stop > 0 and new_stop >= current_stop):
                return finish(output, "REJECTED", f"STOP_WIDENING_FORBIDDEN: New stop {new_stop} is not tighter than current stop {current_stop} for {side}")
            try:
                self.gateway.tighten_stop(
                    account_id=context.account_id,
                    instrument_id=output.instrument_id,
                    position_id=str(existing_pos.get("position_id") or ""),
                    new_stop=new_stop,
                    venue=str(existing_pos.get("venue", context.venue)),
                    mode=str(existing_pos.get("mode", mode_scope)),
                )
            except GatewayError as exc:
                return finish(output, "REJECTED", f"GATEWAY_REJECTED: {exc.message}")
            return finish(output, "EXECUTED", f"Stop tightened from {current_stop} to {new_stop}")

        return finish(output, "REJECTED", "UNHANDLED_ACTION")
