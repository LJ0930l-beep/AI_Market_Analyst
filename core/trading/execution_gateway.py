"""Unified Execution Gateway, Capability Service, and Order Lifecycle Engine.

Fulfills R01, R02, R03 requirements:
1. Strict environment isolation (RESEARCH, PAPER, TESTNET, LIVE) and control modes.
2. Account-scoped credentials and the immutable order/risk lifecycle apply to
   both TestNet and Live.  There is no separate local release or authorization
   lock: a correctly scoped Gate credential is the execution identity.
3. Unified 11-state order lifecycle.
4. Idempotency guarantees: same key + same payload -> idempotent return; same key + different payload -> 409 Conflict.
5. OrderIntent must carry ProtectionPlan with stop_price and failure handling.
6. Finite Decimal parameter validation (no NaN, no Infinity, no negative price/size).
7. Timeout and unknown state reconciliation.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict, replace
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import enum
import hashlib
import json
import logging
import math
import sqlite3
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

logger = logging.getLogger("core.trading.execution_gateway")


def intent_mode_value(mode: Any) -> str:
    return str(mode.value if hasattr(mode, "value") else mode).upper()


def _decimal_text(value: Any, *, market: bool = False) -> str:
    if value is None:
        return "MARKET" if market else ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


class TradingMode(str, enum.Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class ControlMode(str, enum.Enum):
    ASSISTED = "ASSISTED"
    AUTONOMOUS = "AUTONOMOUS"


class DecisionPath(str, enum.Enum):
    STRATEGY_DRIVEN = "STRATEGY_DRIVEN"
    AI_LED = "AI_LED"


class LivePermissionState(str, enum.Enum):
    LOCKED = "LOCKED"
    ELIGIBLE = "ELIGIBLE"
    AUTHORIZED = "AUTHORIZED"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ProtectionStatus(str, enum.Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    DEGRADED = "DEGRADED"


# PAPER is an explicitly local execution environment.  Its matching contract
# is therefore part of the application configuration, not a claim about an
# exchange's current instrument metadata.  Older local fixtures and imported
# PAPER bars predate the durable market-contract fields; filling these values
# only for PAPER keeps that compatibility path deterministic while the quote
# itself still has to pass the freshness gate.  TESTNET/LIVE never use this
# fallback: their venue adapter remains the authority for venue metadata.
PAPER_SIMULATION_MARKET_CONTRACT = {
    "contractSize": 1.0,
    "precision": {"amount": 0.001, "price": 0.01},
    "limits": {
        "amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001},
    },
    "taker": 0.0005,
}

# A resting order intent whose own TTL elapsed this long ago is no longer
# wanted.  The window lets the reconcile pass cancel it remotely rather than
# treating a transient quote hiccup as abandonment.
STALE_INTENT_GRACE = timedelta(minutes=5)


class CapabilityStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    LOCKED = "LOCKED"


# Custom Exceptions
class GatewayError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class IdempotencyConflictError(GatewayError):
    def __init__(self, message: str = "Duplicate idempotency key with conflicting payload."):
        super().__init__("IDEMPOTENCY_CONFLICT", message, status_code=409)


class ParameterValidationError(GatewayError):
    def __init__(self, message: str):
        super().__init__("INVALID_ORDER_PARAMETER", message, status_code=422)


class ProtectionOrderError(GatewayError):
    def __init__(self, message: str):
        super().__init__("PROTECTION_PLAN_FAILED", message, status_code=500)


@dataclass
class ProtectionPlan:
    stop_price: float
    take_profit: Optional[float] = None
    trigger_type: str = "mark"
    reduce_only: bool = True
    status: ProtectionStatus = ProtectionStatus.PENDING
    failure_action: str = "BLOCK_NEW_RISK_AND_ALERT"
    # Optional durable position-exit contract.  These fields do not create a
    # second execution path; they are carried through the same gateway fill
    # into the account-scoped PositionGuardian.
    trade_plan_id: Optional[str] = None
    time_exit_at: Optional[str] = None
    partial_take_profits: List[Dict[str, Any]] = field(default_factory=list)
    trailing_protection: Optional[Dict[str, Any]] = None
    event_invalidation: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stop_price": self.stop_price,
            "take_profit": self.take_profit,
            "trigger_type": self.trigger_type,
            "reduce_only": self.reduce_only,
            "status": self.status.value if isinstance(self.status, ProtectionStatus) else self.status,
            "failure_action": self.failure_action,
            "trade_plan_id": self.trade_plan_id,
            "time_exit_at": self.time_exit_at,
            "partial_take_profits": list(self.partial_take_profits),
            "trailing_protection": dict(self.trailing_protection) if isinstance(self.trailing_protection, dict) else self.trailing_protection,
            "event_invalidation": list(self.event_invalidation),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProtectionPlan:
        return cls(
            stop_price=float(data["stop_price"]),
            take_profit=float(data["take_profit"]) if data.get("take_profit") is not None else None,
            trigger_type=str(data.get("trigger_type", "mark")),
            reduce_only=bool(data.get("reduce_only", True)),
            status=ProtectionStatus(data.get("status", "PENDING")),
            failure_action=str(data.get("failure_action", "BLOCK_NEW_RISK_AND_ALERT")),
            trade_plan_id=data.get("trade_plan_id"),
            time_exit_at=data.get("time_exit_at"),
            partial_take_profits=list(data.get("partial_take_profits") or []),
            trailing_protection=dict(data["trailing_protection"]) if isinstance(data.get("trailing_protection"), dict) else None,
            event_invalidation=list(data.get("event_invalidation") or []),
        )


@dataclass
class OrderIntent:
    intent_id: str
    idempotency_key: str
    account_id: str
    mode: TradingMode
    instrument_id: str
    side: str  # "LONG" | "SHORT" | "BUY" | "SELL"
    order_type: str  # "market" | "limit"
    quantity: float
    price: Optional[float] = None
    leverage: Optional[int] = None
    protection_plan: Optional[ProtectionPlan] = None
    reduce_only: bool = False
    control_mode: ControlMode = ControlMode.ASSISTED
    decision_path: DecisionPath = DecisionPath.STRATEGY_DRIVEN
    session_id: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    signal_at: Optional[str] = None
    # AI-led entry selection is persisted as an execution fact.  The model
    # may express a preference, but the deterministic policy stamps the final
    # order type and evidence before the adapter is called.
    candidate_id: Optional[str] = None
    closed_15m_bar: Optional[str] = None
    order_preference: str = "AUTO"
    final_order_type: Optional[str] = None
    selection_reason_code: Optional[str] = None
    selection_reason: Optional[str] = None
    selection_evidence: Dict[str, Any] = field(default_factory=dict)
    limit_price: Optional[float] = None
    ttl_seconds: Optional[int] = None
    selection_policy_version: Optional[str] = None
    status: OrderStatus = OrderStatus.CREATED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    venue: str = "simulated"
    environment: Optional[str] = None
    position_id: Optional[str] = None
    cycle_id: Optional[str] = None
    generation: Optional[int] = None
    authorization_id: Optional[str] = None
    authorization_version: Optional[int] = None
    expires_at: Optional[str] = None
    lease_holder_id: Optional[str] = None
    fencing_token: Optional[int] = None
    runtime_lease_name: str = "monitoring_runtime"

    def compute_payload_hash(self) -> str:
        """Compute deterministic SHA256 of order parameters to detect 409 conflict."""
        protection = self.protection_plan.to_dict() if self.protection_plan else None
        # ``status`` is a mutable observation written after a fill.  Exclude
        # it from the idempotency payload so retrying the same intent object
        # after PAPER protection becomes ACTIVE does not manufacture a
        # payload conflict.  Every durable behaviour field is included.
        if isinstance(protection, dict):
            protection = {key: value for key, value in protection.items() if key != "status"}
        content = {
            "account_id": self.account_id,
            "venue": self.venue,
            "environment": self.environment or (self.mode.value if isinstance(self.mode, TradingMode) else self.mode),
            "mode": self.mode.value if isinstance(self.mode, TradingMode) else self.mode,
            "instrument_id": self.instrument_id,
            "side": self.side.upper(),
            "order_type": self.order_type.lower(),
            "quantity": _decimal_text(self.quantity),
            "price": _decimal_text(self.price, market=True),
            "leverage": self.leverage,
            "reduce_only": self.reduce_only,
            "position_id": self.position_id,
            "cycle_id": self.cycle_id,
            "generation": self.generation,
            "authorization_id": self.authorization_id,
            "authorization_version": self.authorization_version,
            "expires_at": self.expires_at,
            "lease_holder_id": self.lease_holder_id,
            "fencing_token": self.fencing_token,
            "runtime_lease_name": self.runtime_lease_name,
            "decision_path": self.decision_path.value if isinstance(self.decision_path, DecisionPath) else self.decision_path,
            "control_mode": self.control_mode.value if isinstance(self.control_mode, ControlMode) else self.control_mode,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "session_id": self.session_id,
            "signal_at": self.signal_at,
            "candidate_id": self.candidate_id,
            "closed_15m_bar": self.closed_15m_bar,
            "order_preference": str(self.order_preference or "AUTO").upper(),
            "final_order_type": self.final_order_type,
            "selection_reason_code": self.selection_reason_code,
            "selection_reason": self.selection_reason,
            "selection_evidence": self.selection_evidence,
            "limit_price": _decimal_text(self.limit_price, market=True),
            "ttl_seconds": self.ttl_seconds,
            "selection_policy_version": self.selection_policy_version,
            "protection_plan": protection,
        }
        encoded = json.dumps(content, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "account_id": self.account_id,
            "venue": self.venue,
            "environment": self.environment or (self.mode.value if isinstance(self.mode, TradingMode) else self.mode),
            "mode": self.mode.value if isinstance(self.mode, TradingMode) else self.mode,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "price": self.price,
            "leverage": self.leverage,
            "protection_plan": self.protection_plan.to_dict() if self.protection_plan else None,
            "reduce_only": self.reduce_only,
            "position_id": self.position_id,
            "cycle_id": self.cycle_id,
            "generation": self.generation,
            "authorization_id": self.authorization_id,
            "authorization_version": self.authorization_version,
            "expires_at": self.expires_at,
            "lease_holder_id": self.lease_holder_id,
            "fencing_token": self.fencing_token,
            "runtime_lease_name": self.runtime_lease_name,
            "control_mode": self.control_mode.value if isinstance(self.control_mode, ControlMode) else self.control_mode,
            "decision_path": self.decision_path.value if isinstance(self.decision_path, DecisionPath) else self.decision_path,
            "session_id": self.session_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "signal_at": self.signal_at,
            "candidate_id": self.candidate_id,
            "closed_15m_bar": self.closed_15m_bar,
            "order_preference": str(self.order_preference or "AUTO").upper(),
            "final_order_type": self.final_order_type,
            "selection_reason_code": self.selection_reason_code,
            "selection_reason": self.selection_reason,
            "selection_evidence": dict(self.selection_evidence or {}),
            "limit_price": self.limit_price,
            "ttl_seconds": self.ttl_seconds,
            "selection_policy_version": self.selection_policy_version,
            "status": self.status.value if isinstance(self.status, OrderStatus) else self.status,
            "created_at": self.created_at,
        }


class CapabilityService:
    """Service to evaluate genuine backend operational capabilities."""

    @staticmethod
    def get_capabilities(store) -> Dict[str, Any]:
        from core.security.credentials import CredentialVault
        cred_meta = CredentialVault.get_metadata(store)
        is_configured = cred_meta["configured"]
        migration_required = cred_meta["migration_status"] == "MIGRATION_REQUIRED"

        # Check live permission state in DB or default to LOCKED
        live_state = LivePermissionState.LOCKED
        if hasattr(store, "_connect"):
            with store._connect() as db:
                db.execute("""
                CREATE TABLE IF NOT EXISTS live_authorizations (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    permission_state TEXT NOT NULL DEFAULT 'LOCKED',
                    authorization_id TEXT,
                    confirmed_at TEXT,
                    expires_at TEXT
                );
                """)
                row = db.execute("SELECT permission_state FROM live_authorizations WHERE id = 1").fetchone()
                if row:
                    try:
                        live_state = LivePermissionState(row["permission_state"])
                    except ValueError:
                        live_state = LivePermissionState.LOCKED

        modes = {
            "RESEARCH": {"status": CapabilityStatus.AVAILABLE.value, "reason_code": "READY"},
            "PAPER": {"status": CapabilityStatus.AVAILABLE.value, "reason_code": "READY"},
            "TESTNET": {
                "status": (
                    CapabilityStatus.AVAILABLE.value
                    if (is_configured and not migration_required)
                    else (CapabilityStatus.DEGRADED.value if migration_required else CapabilityStatus.NOT_CONFIGURED.value)
                ),
                "reason_code": "MIGRATION_REQUIRED" if migration_required else ("CONFIGURED" if is_configured else "CREDENTIALS_MISSING"),
            },
            "LIVE": {
                # In M0-M3 milestone, LIVE release policy is LOCKED by default
                "status": CapabilityStatus.LOCKED.value,
                "reason_code": "RELEASE_POLICY_LOCK_M0_TO_M3",
                "permission_state": live_state.value,
            },
        }

        return {
            "schema_version": "1.1.0",
            "modes": modes,
            "credentials": {
                "configured": is_configured,
                "api_key_masked": cred_meta["api_key_masked"],
                "migration_status": cred_meta["migration_status"],
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


class ExecutionGateway:
    """Unified execution gateway enforcing R01, R02, R03 contracts."""

    def __init__(
        self,
        store,
        trader_client: Optional[Any] = None,
        ledger: Optional[Any] = None,
        runtime_fence_validator: Optional[Callable[[OrderIntent], Any]] = None,
    ):
        self.store = store
        self.trader_client = trader_client
        self.runtime_fence_validator = runtime_fence_validator
        self._lock = threading.RLock()
        if ledger is not None:
            self.ledger = ledger
        elif store is not None and hasattr(store, "_connect"):
            from .ledger import AccountLedger
            self.ledger = AccountLedger(store)
        else:
            self.ledger = None
        if self.ledger is not None:
            from .risk_engine import RiskEngine
            self.risk_engine = RiskEngine(self.ledger)
        else:
            self.risk_engine = None
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        if hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                db.execute("""
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    account_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL,
                    leverage INTEGER,
                    protection_plan_json TEXT,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    execution_result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    venue TEXT NOT NULL DEFAULT 'simulated',
                    environment TEXT,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    position_id TEXT,
                    reservation_id TEXT,
                    risk_decision_json TEXT,
                    cycle_id TEXT,
                    generation INTEGER,
                    authorization_id TEXT,
                    authorization_version INTEGER,
                    expires_at TEXT
                    ,strategy_id TEXT
                    ,strategy_version TEXT
                    ,decision_path TEXT
                    ,control_mode TEXT
                    ,session_id TEXT
                    ,signal_at TEXT
                );
                """)
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(order_intents)").fetchall()}
                for column, definition in (
                    ("venue", "TEXT NOT NULL DEFAULT 'simulated'"),
                    ("environment", "TEXT"),
                    ("reduce_only", "INTEGER NOT NULL DEFAULT 0"),
                    ("position_id", "TEXT"),
                    ("reservation_id", "TEXT"),
                    ("risk_decision_json", "TEXT"),
                    ("cycle_id", "TEXT"),
                    ("generation", "INTEGER"),
                    ("authorization_id", "TEXT"),
                    ("authorization_version", "INTEGER"),
                    ("expires_at", "TEXT"),
                    ("lease_holder_id", "TEXT"),
                    ("fencing_token", "INTEGER"),
                    ("runtime_lease_name", "TEXT NOT NULL DEFAULT 'monitoring_runtime'"),
                    ("strategy_id", "TEXT"),
                    ("strategy_version", "TEXT"),
                    ("decision_path", "TEXT"),
                    ("control_mode", "TEXT"),
                    ("session_id", "TEXT"),
                    ("signal_at", "TEXT"),
                ):
                    if column not in columns:
                        db.execute(f"ALTER TABLE order_intents ADD COLUMN {column} {definition}")
                from .institutional_schema import ensure_institutional_trader_schema
                ensure_institutional_trader_schema(db)
        else:
            if not hasattr(self.store, "_orders"):
                self.store._orders = {}

    def _resolve_scoped_trader_client(
        self,
        intent: OrderIntent,
        trader_client: Optional[Any],
    ) -> Optional[Any]:
        """Resolve a remote adapter without crossing account credential scopes.

        The API and AI/strategy paths share one gateway. A previously supplied
        global adapter must not be reused for a managed Gate account because
        the account row is the authority for both environment and credentials.
        LIVE and TestNet are both resolved from their own persisted credential
        slot.  No caller can substitute a global adapter for a managed account.
        """
        mode = intent_mode_value(intent.mode)
        venue = str(intent.venue or "").strip().lower()
        if trader_client is not None:
            # A caller-provided/global adapter is not authoritative for a
            # managed Gate account.  Resolve that account's encrypted slot
            # and persisted environment again here, so API, AI-led, trade
            # plan, and recovery callers cannot cross credentials by passing
            # a stale client object.  Non-managed/in-memory compatibility
            # callers retain their explicit test adapter.
            if (
                hasattr(self.store, "_connect")
                and mode in {TradingMode.TESTNET.value, TradingMode.LIVE.value}
                and venue == "gate"
            ):
                try:
                    from .gate_accounts import build_gate_trader, is_managed_gate_account

                    if is_managed_gate_account(self.store, intent.account_id):
                        return build_gate_trader(self.store, intent.account_id)
                except (ValueError, TypeError):
                    return None
            return trader_client

        if (
            hasattr(self.store, "_connect")
            and mode in {TradingMode.TESTNET.value, TradingMode.LIVE.value}
            and venue == "gate"
        ):
            try:
                from .gate_accounts import build_gate_trader, is_managed_gate_account

                if is_managed_gate_account(self.store, intent.account_id):
                    return build_gate_trader(self.store, intent.account_id)
            except (ValueError, TypeError):
                # Account/mode/credential mismatches are surfaced by the
                # authoritative account and adapter gates below; never fall
                # through to another account's global client.
                return None

        # Compatibility path for legacy, explicitly constructed adapters and
        # in-memory test doubles. It is unreachable for managed Gate scopes.
        return self.trader_client or getattr(self.store, "_trader_client", None) or getattr(self.store, "trader_client", None)

    def _intent_from_order_row(self, row: Dict[str, Any], *, fallback_mode: str = "TESTNET") -> OrderIntent:
        """Rehydrate the immutable scope needed by cancel/reconciliation.

        Recovery used to call ``self.trader_client`` directly, which could
        reuse another account's adapter.  Building the same scoped intent
        shape here lets the account registry resolve the correct TestNet slot
        for every background operation.
        """
        try:
            plan_data = json.loads(row.get("protection_plan_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            plan_data = {}
        plan = ProtectionPlan.from_dict(plan_data) if isinstance(plan_data, dict) and plan_data.get("stop_price") is not None else None
        try:
            row_mode = TradingMode(str(row.get("mode") or fallback_mode).upper())
        except ValueError:
            row_mode = TradingMode.TESTNET
        return OrderIntent(
            intent_id=str(row.get("intent_id") or ""),
            idempotency_key=str(row.get("idempotency_key") or row.get("intent_id") or ""),
            account_id=str(row.get("account_id") or ""),
            mode=row_mode,
            instrument_id=str(row.get("instrument_id") or ""),
            side=str(row.get("side") or ""),
            order_type=str(row.get("order_type") or "market"),
            quantity=float(row.get("quantity") or 0),
            price=float(row["price"]) if row.get("price") is not None else None,
            leverage=int(row["leverage"]) if row.get("leverage") is not None else None,
            protection_plan=plan,
            reduce_only=bool(row.get("reduce_only") or 0),
            venue=str(row.get("venue") or "gate"),
            environment=str(row.get("environment") or row.get("mode") or fallback_mode),
            position_id=row.get("position_id"),
            cycle_id=row.get("cycle_id"),
            generation=row.get("generation"),
            authorization_id=row.get("authorization_id"),
            authorization_version=row.get("authorization_version"),
            control_mode=row.get("control_mode") or ControlMode.ASSISTED.value,
            decision_path=row.get("decision_path") or DecisionPath.STRATEGY_DRIVEN.value,
            session_id=row.get("session_id"),
            strategy_id=row.get("strategy_id"),
            strategy_version=row.get("strategy_version"),
            signal_at=row.get("signal_at"),
            candidate_id=row.get("candidate_id"),
            closed_15m_bar=row.get("closed_15m_bar"),
            order_preference=str(row.get("order_preference") or "AUTO"),
            final_order_type=row.get("final_order_type"),
            selection_reason_code=row.get("selection_reason_code"),
            selection_reason=row.get("selection_reason"),
            selection_evidence=json.loads(row.get("selection_evidence_json") or "{}") if isinstance(row.get("selection_evidence_json"), str) else {},
            limit_price=float(row["limit_price"]) if row.get("limit_price") is not None else None,
            ttl_seconds=int(row["ttl_seconds"]) if row.get("ttl_seconds") is not None else None,
            selection_policy_version=row.get("selection_policy_version"),
            expires_at=row.get("expires_at"),
        )

    def _require_runtime_fence(self, intent: OrderIntent) -> None:
        """Reject a stale runtime-owned opening before any side effect."""
        validator = self.runtime_fence_validator
        if not callable(validator) or intent.reduce_only:
            return
        try:
            verdict = validator(intent)
        except Exception as exc:
            raise GatewayError("RUNTIME_LEASE_FENCED", f"Runtime lease validation failed: {exc}", 409) from exc
        if isinstance(verdict, tuple):
            valid = bool(verdict[0])
            reason = str(verdict[1] if len(verdict) > 1 else "RUNTIME_LEASE_FENCED")
        else:
            valid = bool(verdict)
            reason = "OK" if valid else "RUNTIME_LEASE_FENCED"
        if not valid:
            raise GatewayError("RUNTIME_LEASE_FENCED", f"New risk is fenced: {reason}", 409)

    @staticmethod
    def validate_finite_decimal(name: str, value: Any, min_val: Optional[float] = None, allow_none: bool = False) -> Optional[Decimal]:
        """Strict validation rejecting NaN, Infinity, negative values."""
        if value is None:
            if allow_none:
                return None
            raise ParameterValidationError(f"Parameter '{name}' cannot be None.")
        try:
            val_str = str(value).strip()
            d = Decimal(val_str)
        except (InvalidOperation, TypeError, ValueError):
            raise ParameterValidationError(f"Parameter '{name}' contains non-numeric value: {value}")

        if d.is_nan() or d.is_infinite():
            raise ParameterValidationError(f"Parameter '{name}' cannot be NaN or Infinity.")

        if min_val is not None and float(d) < min_val:
            raise ParameterValidationError(f"Parameter '{name}' ({d}) is below minimum allowed ({min_val}).")

        return d

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime]:
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

    def _fresh_market_snapshot(
        self,
        symbol: str,
        supplied: Optional[Dict[str, Any]] = None,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return a recent executable quote or fail closed.

        ``OrderIntent.price`` is an order parameter, never a market-data
        source.  Explicit callers must supply ``data_as_of``/``received_at``;
        persisted bars and realtime state carry the same timestamps.
        """
        candidates: list[Dict[str, Any]] = []
        if supplied is not None:
            candidates.append(dict(supplied))
        elif hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                has_state = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_realtime_state'").fetchone()
                if has_state:
                    row = db.execute("SELECT * FROM market_realtime_state WHERE symbol=?", (symbol,)).fetchone()
                    if row and row["price"] is not None:
                        item = dict(row)
                        try:
                            item.update(json.loads(row["payload_json"] or "{}"))
                        except Exception:
                            pass
                        item["price"] = row["price"]
                        item["source"] = "market_realtime_state"
                        candidates.append(item)
                has_bars = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_bars'").fetchone()
                if has_bars:
                    row = db.execute(
                        "SELECT * FROM market_bars WHERE symbol=? ORDER BY data_as_of DESC, bar_start DESC LIMIT 1",
                        (symbol,),
                    ).fetchone()
                    if row:
                        item = dict(row)
                        item["price"] = row["close"]
                        item["source"] = "market_bars"
                        candidates.append(item)

        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        else:
            clock = clock.astimezone(timezone.utc)
        for candidate in candidates:
            candidate_symbol = candidate.get("symbol") or candidate.get("instrument_id")
            if candidate_symbol and str(candidate_symbol).strip().upper() != str(symbol).strip().upper():
                continue
            if supplied is not None and candidate.get("fresh") is not True:
                # An explicitly supplied quote must carry an affirmative
                # freshness assertion; an empty/malformed override may not
                # fall back to a persisted quote or become fresh by default.
                continue
            if candidate.get("fresh") is False or candidate.get("stale") is True or candidate.get("executable") is False:
                continue
            try:
                price = Decimal(str(candidate.get("price", candidate.get("last"))))
                if not price.is_finite() or price <= 0:
                    continue
            except Exception:
                continue
            data_as_of = self._parse_timestamp(candidate.get("data_as_of") or candidate.get("timestamp") or candidate.get("bar_end"))
            received_at = self._parse_timestamp(candidate.get("received_at") or candidate.get("updated_at"))
            reference = data_as_of or received_at
            if reference is None:
                continue
            age = (clock - reference).total_seconds()
            try:
                max_age_value = candidate["stale_after_seconds"] if "stale_after_seconds" in candidate else 120
                max_age = float(max_age_value)
            except (TypeError, ValueError):
                continue
            # An explicit non-positive/invalid freshness budget is a failed
            # quote contract, not permission to widen it to a default.
            if not math.isfinite(max_age) or max_age <= 0:
                continue
            max_age = min(max_age, 120.0)
            if age < -5.0 or age > max_age:
                continue
            if str(candidate.get("freshness_status", "")).upper() in {"STALE", "DEGRADED", "UNKNOWN"}:
                continue
            nested_market = dict(candidate.get("market") or candidate.get("metadata") or {})
            result = {**candidate, "price": float(price), "data_as_of": (data_as_of or reference).isoformat(), "received_at": (received_at or clock).isoformat(), "fresh": True}
            if nested_market:
                result["market"] = nested_market
            return result
        raise GatewayError(
            "MARKET_DATA_UNAVAILABLE",
            f"422 MARKET_DATA_UNAVAILABLE: No fresh executable market price available for {symbol}.",
            status_code=422,
        )

    def _validate_reduce_only(self, intent: OrderIntent) -> None:
        side = intent.side.upper()
        target_side = "LONG" if side in ("SELL", "CLOSE") else "SHORT" if side == "BUY" else None
        if target_side is None:
            raise GatewayError("REDUCE_ONLY_DIRECTION_INVALID", "Reduce-only orders must use SELL for LONG or BUY for SHORT.", 422)
        positions = self.ledger.get_open_positions(intent.account_id) if self.ledger is not None else []
        matching = [
            position for position in positions
            if str(position.get("symbol", position.get("instrument_id", ""))).upper() == intent.instrument_id.upper()
            and str(position.get("venue", "simulated")).lower() == str(intent.venue or "simulated").lower()
            and str(position.get("mode", "PAPER")).upper() == str(intent_mode_value(intent.mode)).upper()
            and str(position.get("side", "")).upper() == target_side
            and (not intent.position_id or position.get("position_id") == intent.position_id)
        ]
        available = sum((Decimal(str(item.get("remaining_contracts", item.get("quantity", 0)))) for item in matching), Decimal("0"))
        requested = Decimal(str(intent.quantity))
        if not matching:
            raise GatewayError("REDUCE_ONLY_POSITION_NOT_FOUND", "No matching scoped position exists for this reduce-only order.", 422)
        if not intent.position_id and len(matching) > 1:
            raise GatewayError(
                "REDUCE_ONLY_POSITION_ID_REQUIRED",
                "A reduce-only order must name position_id when more than one scoped position matches.",
                422,
            )
        if requested > available:
            raise GatewayError("REDUCE_ONLY_EXCEEDS_POSITION", "Reduce-only quantity exceeds the scoped position remainder.", 422)

    @staticmethod
    def _gate_symbol_key(value: Any) -> str:
        """Compare Gate symbols without confusing CCXT and native spellings."""

        return "".join(character for character in str(value or "").upper() if character.isalnum())

    @classmethod
    def _gate_remote_positions_for_risk(
        cls,
        truth: Dict[str, Any],
        account_id: str,
    ) -> list[Dict[str, Any]]:
        """Project observed Gate positions into the risk engine's read shape.

        These records are an in-memory projection of the latest private API
        response.  They are deliberately never written to ``simulated_positions``.
        Missing entry/stop facts remain missing (represented as zero for the
        existing risk arithmetic), rather than being filled from a local quote.
        """

        positions = truth.get("positions") if isinstance(truth.get("positions"), list) else []
        pending_orders = truth.get("pending_orders") if isinstance(truth.get("pending_orders"), list) else []
        result: list[Dict[str, Any]] = []

        def positive(value: Any, default: Decimal = Decimal("0")) -> Decimal:
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return default
            return parsed if parsed.is_finite() and parsed > 0 else default

        for raw in positions:
            if not isinstance(raw, dict):
                continue
            contracts = positive(raw.get("contracts", raw.get("size")))
            side = str(raw.get("side") or "").upper()
            if contracts <= 0 or side not in {"LONG", "SHORT"}:
                continue
            symbol = str(raw.get("symbol") or "").replace("/", "").replace(":USDT", "").upper()
            close_side = "SELL" if side == "LONG" else "BUY"
            protected = any(
                isinstance(order, dict)
                and bool(order.get("reduce_only"))
                and str(order.get("side") or "").upper() == close_side
                and positive(order.get("stop_price")) > 0
                and cls._gate_symbol_key(order.get("symbol")) == cls._gate_symbol_key(symbol)
                for order in pending_orders
            )
            entry = positive(raw.get("entry_price", raw.get("entry")))
            mark = positive(raw.get("mark_price", raw.get("mark")), entry)
            contract_size = positive(raw.get("contract_size"), Decimal("1"))
            leverage = positive(raw.get("leverage"), Decimal("1"))
            stop = positive(raw.get("stop_price", raw.get("stop_loss", raw.get("stop"))))
            result.append(
                {
                    "account_id": account_id,
                    "venue": "gate",
                    "mode": "TESTNET",
                    "symbol": symbol,
                    "instrument_id": symbol,
                    "side": side,
                    "contracts": str(contracts),
                    "remaining_contracts": str(contracts),
                    "entry": str(entry),
                    "entry_price": str(entry),
                    "mark_price": str(mark),
                    "stop": str(stop),
                    "stop_loss": str(stop),
                    "contract_size": str(contract_size),
                    "leverage": str(leverage),
                    "position_id": raw.get("position_id"),
                    "protection_status": "ACTIVE" if protected else "UNKNOWN",
                    "legacy_unverified": 0,
                    "local_mirror": False,
                    "source": "GATE_REMOTE_PRIVATE_API",
                }
            )
        return result

    def _validate_gate_remote_reduce_only(self, intent: OrderIntent, truth: Dict[str, Any]) -> None:
        """Validate a Gate TestNet reduction against current remote positions."""

        side = str(intent.side or "").upper()
        target_side = "LONG" if side in {"SELL", "CLOSE"} else "SHORT" if side == "BUY" else None
        if target_side is None:
            raise GatewayError("REDUCE_ONLY_DIRECTION_INVALID", "Reduce-only orders must use SELL for LONG or BUY for SHORT.", 422)
        if str(truth.get("status") or "").upper() != "AVAILABLE":
            raise GatewayError(
                "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE",
                "Gate TestNet 远端持仓事实不可用，未提交平仓单。",
                422,
            )
        positions = self._gate_remote_positions_for_risk(truth, intent.account_id)
        symbol_key = self._gate_symbol_key(intent.instrument_id)
        matching = [
            position
            for position in positions
            if self._gate_symbol_key(position.get("symbol")) == symbol_key
            and str(position.get("side") or "").upper() == target_side
            and (
                not intent.position_id
                or str(position.get("position_id") or "") == str(intent.position_id)
            )
        ]
        available = sum(
            (Decimal(str(item.get("remaining_contracts", item.get("contracts", 0)))) for item in matching),
            Decimal("0"),
        )
        requested = Decimal(str(intent.quantity))
        if not matching:
            raise GatewayError("REDUCE_ONLY_POSITION_NOT_FOUND", "Gate 远端没有匹配的账户/标的/方向仓位。", 422)
        if not intent.position_id and len(matching) > 1:
            raise GatewayError(
                "REDUCE_ONLY_POSITION_ID_REQUIRED",
                "Gate 远端同一标的存在多个匹配仓位时，平仓单必须带 position_id。",
                422,
            )
        if requested > available:
            raise GatewayError("REDUCE_ONLY_EXCEEDS_POSITION", "平仓张数超过 Gate 远端仓位剩余张数。", 422)

    @staticmethod
    def _market_amount_rules(market_snapshot: Dict[str, Any]) -> tuple[Decimal, Decimal, Optional[Decimal]]:
        """Resolve executable amount rules without silently rounding an order.

        A service may round a planned reduction down before it reaches this
        boundary.  The gateway still validates the final intent because a
        direct caller, AI path, or guardian adapter must not bypass venue
        minimums or create an amount the venue cannot represent.
        """
        market = dict(market_snapshot.get("market") or market_snapshot.get("metadata") or {})
        limits = dict((market.get("limits") or {}).get("amount") or {})
        precision = dict(market.get("precision") or {})
        raw_step = limits.get("step")
        if raw_step is None:
            raw_step = precision.get("amount")
        raw_minimum = limits.get("min")
        if raw_minimum is None:
            raw_minimum = market_snapshot.get("min_quantity")
        raw_maximum = limits.get("max")
        if raw_maximum is None:
            raw_maximum = market_snapshot.get("max_quantity")
        try:
            step = Decimal(str(raw_step))
            minimum = Decimal(str(raw_minimum if raw_minimum is not None else step))
            maximum = Decimal(str(raw_maximum)) if raw_maximum is not None else None
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise GatewayError("MARKET_RULES_UNAVAILABLE", "Fresh market data does not contain valid amount step/minimum rules.", 422) from exc
        if not step.is_finite() or step <= 0 or not minimum.is_finite() or minimum <= 0:
            raise GatewayError("MARKET_RULES_UNAVAILABLE", "Fresh market data does not contain positive amount step/minimum rules.", 422)
        if maximum is not None and (not maximum.is_finite() or maximum < minimum):
            raise GatewayError("MARKET_RULES_UNAVAILABLE", "Fresh market data contains an invalid amount maximum.", 422)
        return step, minimum, maximum

    def _validate_intent_market_rules(self, intent: OrderIntent, market_snapshot: Dict[str, Any]) -> None:
        step, minimum, maximum = self._market_amount_rules(market_snapshot)
        quantity = Decimal(str(intent.quantity))
        if quantity < minimum:
            raise GatewayError("QUANTITY_BELOW_MARKET_MINIMUM", "Order quantity is below the fresh market minimum.", 422)
        if maximum is not None and quantity > maximum:
            raise GatewayError("QUANTITY_ABOVE_MARKET_MAXIMUM", "Order quantity exceeds the fresh market maximum.", 422)
        units = quantity / step
        if units != units.to_integral_value():
            raise GatewayError("QUANTITY_STEP_INVALID", "Order quantity is not aligned to the fresh market amount step.", 422)

        if intent.price is not None:
            market = dict(market_snapshot.get("market") or market_snapshot.get("metadata") or {})
            precision = dict(market.get("precision") or {})
            limits = dict((market.get("limits") or {}).get("price") or {})
            raw_tick = limits.get("step") or limits.get("tick") or precision.get("price") or market_snapshot.get("price_tick")
            if raw_tick is not None:
                try:
                    tick = Decimal(str(raw_tick))
                    price = Decimal(str(intent.price))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise GatewayError("MARKET_RULES_UNAVAILABLE", "Fresh market data contains invalid price step rules.", 422) from exc
                if not tick.is_finite() or tick <= 0 or price / tick != (price / tick).to_integral_value():
                    raise GatewayError("PRICE_STEP_INVALID", "Explicit limit price is not aligned to the fresh market price step.", 422)

    @staticmethod
    def _validate_remote_market_economics(market_snapshot: Dict[str, Any]) -> None:
        """Require venue-supplied sizing/cost facts before remote risk sizing.

        ``RiskEngine`` retains compatibility defaults for old PAPER callers,
        but those defaults are not exchange evidence.  A TESTNET/LIVE opening
        must therefore carry an explicit contract size, amount precision and
        limits, fee, and slippage before the gateway can reserve risk.  A
        protective reduction is handled by the remote adapter and does not
        use these facts to calculate new exposure.
        """
        market = dict(market_snapshot.get("market") or market_snapshot.get("metadata") or {})
        precision = dict(market.get("precision") or {})
        amount_limits = dict((market.get("limits") or {}).get("amount") or {})

        def finite(value: Any, *, non_negative: bool = False) -> bool:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return False
            return math.isfinite(parsed) and (parsed >= 0 if non_negative else parsed > 0)

        contract_size = market.get("contractSize", market.get("contract_size", market_snapshot.get("contractSize", market_snapshot.get("contract_size"))))
        amount_step = precision.get("amount", amount_limits.get("step"))
        amount_min = amount_limits.get("min")
        amount_max = amount_limits.get("max")
        fee_rate = market.get("taker")
        if fee_rate is None:
            fee_rate = market_snapshot.get("fee_rate", market_snapshot.get("taker_fee"))
        slippage = market_snapshot.get("slippage")
        if slippage is None:
            slippage = 0.001
        if not (
            finite(contract_size)
            and finite(amount_step)
            and finite(amount_min)
            and finite(amount_max)
            and finite(fee_rate, non_negative=True)
            and finite(slippage, non_negative=True)
        ):
            raise GatewayError(
                "MARKET_RULES_UNAVAILABLE",
                "Remote opening requires explicit venue contract size, amount limits, fee, and slippage evidence.",
                422,
            )
        if float(amount_max) < float(amount_min):
            raise GatewayError("MARKET_RULES_UNAVAILABLE", "Remote venue amount maximum is below its minimum.", 422)

    @staticmethod
    def _apply_paper_contract_defaults(
        intent: OrderIntent,
        market_snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Complete the explicit local PAPER matching contract when needed.

        ``market_bars`` written by the pre-v1.3 compatibility path contain a
        quote and timestamps but no venue contract metadata.  A local PAPER
        order is still executable because its contract is defined by the
        application's simulator.  Preserve every supplied value and annotate
        the fallback so it cannot be mistaken for exchange metadata in an
        evidence report.  Remote modes must fail/route through their adapter
        instead of using these values.
        """
        if intent_mode_value(intent.mode) != TradingMode.PAPER.value:
            return market_snapshot
        result = dict(market_snapshot)
        current = dict(result.get("market") or result.get("metadata") or {})
        amount_limits = dict((current.get("limits") or {}).get("amount") or {})
        defaults = PAPER_SIMULATION_MARKET_CONTRACT
        used_fallback = False

        if current.get("contractSize") is None and current.get("contract_size") is None and result.get("contractSize") is None:
            current["contractSize"] = defaults["contractSize"]
            used_fallback = True
        precision = dict(current.get("precision") or {})
        if precision.get("amount") is None:
            precision["amount"] = defaults["precision"]["amount"]
            used_fallback = True
        if precision.get("price") is None:
            precision["price"] = defaults["precision"]["price"]
            used_fallback = True
        current["precision"] = precision
        for key, value in defaults["limits"]["amount"].items():
            if amount_limits.get(key) is None:
                amount_limits[key] = value
                used_fallback = True
        current["limits"] = {**dict(current.get("limits") or {}), "amount": amount_limits}
        if current.get("taker") is None and result.get("taker_fee") is None and result.get("fee_rate") is None:
            current["taker"] = defaults["taker"]
            used_fallback = True
        result["market"] = current
        if used_fallback:
            result["market_contract_evidence"] = {
                "status": "DEFINED_LOCAL_PAPER_CONTRACT",
                "source": "PAPER_SIMULATION_MARKET_CONTRACT",
                "exchange_metadata": False,
                "contract": current,
            }
        return result

    def tighten_stop(
        self,
        *,
        account_id: str,
        instrument_id: str,
        position_id: str,
        new_stop: Any,
        venue: str = "simulated",
        mode: Any = TradingMode.PAPER,
    ) -> Dict[str, Any]:
        """Apply a risk-reducing stop change through the execution boundary.

        Stop management does not create a fill, but it still changes the
        protected position.  Keeping it behind the gateway makes the same
        account/venue/mode/position identity and compare-and-swap rules apply
        to AI-led stop changes as to close orders.
        """
        if self.ledger is None:
            raise GatewayError("LEDGER_UNAVAILABLE", "Position stop management requires the account ledger.", 503)
        if not account_id or not instrument_id or not position_id:
            raise GatewayError("POSITION_SCOPE_REQUIRED", "account_id, instrument_id, and position_id are required.", 422)
        stop_dec = self.validate_finite_decimal("new_stop", new_stop, min_val=1e-8)
        mode_clean = intent_mode_value(mode)
        scoped = self.ledger.get_open_positions(account_id, venue=venue, mode=mode_clean)
        target = next(
            (
                position
                for position in scoped
                if str(position.get("position_id") or "") == str(position_id)
                and str(position.get("instrument_id", position.get("symbol", ""))).upper() == str(instrument_id).upper()
            ),
            None,
        )
        if target is None:
            raise GatewayError("POSITION_NOT_FOUND", "No matching scoped open position exists for stop management.", 404)

        current_stop = Decimal(str(target.get("stop") or target.get("stop_loss") or 0))
        side = str(target.get("side") or "").upper()
        if side == "LONG" and stop_dec <= current_stop:
            raise GatewayError("STOP_WIDENING_FORBIDDEN", "A LONG stop can only move upward.", 422)
        if side == "SHORT" and (current_stop <= 0 or stop_dec >= current_stop):
            raise GatewayError("STOP_WIDENING_FORBIDDEN", "A SHORT stop can only move downward.", 422)
        if side not in {"LONG", "SHORT"}:
            raise GatewayError("POSITION_SIDE_INVALID", "Only LONG and SHORT positions can be protected.", 422)

        if not self.ledger.update_position_stop(
            account_id,
            instrument_id,
            float(stop_dec),
            position_id,
            venue=venue,
            mode=mode_clean,
            expected_version=int(target.get("position_version", 0)),
        ):
            raise GatewayError("POSITION_UPDATE_CONFLICT", "The position changed before the stop update was committed.", 409)
        return {
            "status": "STOP_UPDATED",
            "account_id": account_id,
            "venue": venue,
            "mode": mode_clean,
            "instrument_id": instrument_id,
            "position_id": position_id,
            "previous_stop": float(current_stop),
            "new_stop": float(stop_dec),
            "evidence": {
                "source": "execution_gateway",
                "position_scope_verified": True,
                "compare_and_swap": True,
            },
        }

    def _update_order(self, intent_id: str, status: str, result: Dict[str, Any], *, reservation_id: Optional[str] = None, risk_decision: Optional[Dict[str, Any]] = None) -> None:
        if hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                if reservation_id is None and risk_decision is None:
                    db.execute(
                        "UPDATE order_intents SET status=?, execution_result_json=?, updated_at=? WHERE intent_id=?",
                        (status, json.dumps(result, allow_nan=False), datetime.now(timezone.utc).isoformat(), intent_id),
                    )
                else:
                    db.execute(
                        """UPDATE order_intents SET status=?, execution_result_json=?, reservation_id=?, risk_decision_json=?, updated_at=?
                           WHERE intent_id=?""",
                        (status, json.dumps(result, allow_nan=False), reservation_id, json.dumps(risk_decision or {}, allow_nan=False), datetime.now(timezone.utc).isoformat(), intent_id),
                    )
        elif hasattr(self.store, "_orders") and intent_id in self.store._orders:
            self.store._orders[intent_id]["status"] = status
            self.store._orders[intent_id]["execution_result"] = result

    def _reserve_unknown(self, intent: OrderIntent) -> Optional[str]:
        if self.ledger is None or intent.reduce_only:
            return None
        try:
            snapshot = self.ledger.get_snapshot(intent.account_id)
            amount = max(Decimal("0.01"), snapshot.net_equity * Decimal("0.0025"))
            reservation_id = f"res_unknown_{intent.intent_id}"
            if self.ledger.reserve_risk(
                intent.account_id,
                reservation_id,
                amount,
                amount,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                instrument_id=intent.instrument_id,
                venue=str(intent.venue or "gate"),
                mode=intent_mode_value(intent.mode),
                max_single_risk_fraction=Decimal("0.0025"),
                max_portfolio_risk_fraction=Decimal("0.01"),
                max_cluster_risk_fraction=Decimal("0.005"),
            ):
                return reservation_id
        except Exception:
            return None
        return None

    def submit_intent(
        self,
        intent: OrderIntent,
        trader_client: Optional[Any] = None,
        market_snapshot: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Process an OrderIntent with strict idempotency, gates, and lifecycle tracking."""
        # Retain an explicitly supplied adapter on the shared gateway.  A
        # later PositionGuardian protection cycle must use the same authorized
        # adapter instead of becoming falsely degraded after an opening order.
        # The unified path still rechecks authorization and the runtime fence.
        if trader_client is not None:
            with self._lock:
                self.trader_client = trader_client
        try:
            return self._submit_intent_v12(
                intent,
                trader_client=trader_client,
                market_snapshot=market_snapshot,
                now=now,
            )
        except GatewayError as exc:
            self._update_order(
                intent.intent_id,
                OrderStatus.REJECTED.value,
                {"intent_id": intent.intent_id, "status": OrderStatus.REJECTED.value, "error_code": exc.code, "error": exc.message, "timestamp": datetime.now(timezone.utc).isoformat()},
            )
            raise
        except Exception as exc:
            self._update_order(
                intent.intent_id,
                OrderStatus.REJECTED.value,
                {"intent_id": intent.intent_id, "status": OrderStatus.REJECTED.value, "error_code": "EXECUTION_FAILED", "error": str(exc), "timestamp": datetime.now(timezone.utc).isoformat()},
            )
            raise GatewayError("EXECUTION_FAILED", str(exc))

    def _submit_intent_v12(
        self,
        intent: OrderIntent,
        *,
        trader_client: Optional[Any] = None,
        market_snapshot: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Single production submission path for every executable mode."""
        with self._lock:
            side_clean = str(intent.side).upper().strip()
            if side_clean not in ("LONG", "SHORT", "BUY", "SELL"):
                raise ParameterValidationError(f"Invalid side: '{intent.side}'. Must be LONG/SHORT or BUY/SELL.")
            self.validate_finite_decimal("quantity", intent.quantity, min_val=1e-8)
            if intent.price is not None:
                self.validate_finite_decimal("price", intent.price, min_val=1e-8)
            if not intent.account_id or not str(intent.instrument_id).strip():
                raise ParameterValidationError("account_id and instrument_id are required.")
            if hasattr(self.store, "_connect"):
                try:
                    from .account_aliases import canonical_account_id
                    intent.account_id = canonical_account_id(self.store, intent.account_id)
                except Exception:
                    pass
            mode = intent_mode_value(intent.mode)
            if mode not in {item.value for item in TradingMode}:
                raise ParameterValidationError(f"Unsupported trading mode: {mode}")
            # The in-memory compatibility adapter has no registered account
            # row, so preserve its explicit name-based environment mismatch
            # guard before the release lock.  SQLite-backed production
            # accounts are checked by the authoritative registry below; a
            # locked LIVE request there still fails at the outer release
            # boundary before any account or adapter access.
            if (
                not hasattr(self.store, "_connect")
                and mode == "LIVE"
                and any(value in str(intent.account_id).lower() for value in ("testnet", "sandbox", "demo", "paper"))
            ):
                raise GatewayError(
                    "ENVIRONMENT_ACCOUNT_MISMATCH",
                    f"Account '{intent.account_id}' is testnet/sandbox, but intent requested mode LIVE.",
                    422,
                )
            if mode == "RESEARCH":
                raise GatewayError("MODE_NOT_EXECUTABLE", "RESEARCH mode is non-executable", status_code=403)

            # Resolve only after the account registry and LIVE release lock
            # above. This is the shared account-routing point for API, AI-led,
            # strategy, trade-plan, and recovery submissions.
            trader_client = self._resolve_scoped_trader_client(intent, trader_client)

            if not intent.reduce_only:
                if not intent.protection_plan:
                    raise ParameterValidationError("OrderIntent must carry a valid ProtectionPlan with stop_price.")
                self.validate_finite_decimal("stop_price", intent.protection_plan.stop_price, min_val=1e-8)
                if intent.protection_plan.take_profit is not None:
                    self.validate_finite_decimal("take_profit", intent.protection_plan.take_profit, min_val=1e-8)
            elif intent.protection_plan:
                self.validate_finite_decimal("stop_price", intent.protection_plan.stop_price, min_val=1e-8)
                if intent.protection_plan.take_profit is not None:
                    self.validate_finite_decimal("take_profit", intent.protection_plan.take_profit, min_val=1e-8)

            inst_id_lower = intent.instrument_id.lower()
            if ("equity" in inst_id_lower or "nasdaq" in inst_id_lower or "nyse" in inst_id_lower) and side_clean == "SHORT" and not intent.reduce_only:
                raise ParameterValidationError("EQUITY_SHORT_UNAUTHORIZED: Equity shorting requires borrow capability and is disabled by default.")
            if ":spot:" in inst_id_lower and side_clean == "SHORT" and not intent.reduce_only:
                raise ParameterValidationError("SPOT_SHORTING_UNSUPPORTED: Cannot open SHORT on a spot instrument.")

            # The registered account row is authoritative.  A missing row is
            # never silently created or inferred from the account name.
            managed_gate_testnet = False
            if hasattr(self.store, "_connect"):
                with self.store._connect() as db:
                    acct = db.execute("SELECT account_id, mode, config_json FROM accounts WHERE account_id=?", (intent.account_id,)).fetchone()
                if acct is None:
                    raise GatewayError(
                        "ACCOUNT_NOT_FOUND",
                        f"422 ACCOUNT_NOT_FOUND: Account '{intent.account_id}' is not registered.",
                        422,
                    )
                account_mode = str(acct["mode"]).upper()
                try:
                    account_config = json.loads(acct["config_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    account_config = {}
                # A managed gate_paper row may retain normalized mode PAPER
                # for compatibility, while account_type=GATE_TESTNET makes
                # TESTNET its authoritative execution environment.  This
                # prevents the API from entering the local PAPER engine.
                expected_mode = str(account_config.get("execution_mode") or account_mode).upper()
                if account_config.get("account_type") == "GATE_TESTNET":
                    expected_mode = "TESTNET"
                    managed_gate_testnet = True
                if expected_mode != mode:
                    raise GatewayError("ENVIRONMENT_ACCOUNT_MISMATCH", f"Account '{intent.account_id}' is in execution mode {expected_mode}, but order intent requested mode {mode}.", 422)
                if intent.environment and str(intent.environment).upper() != mode:
                    raise GatewayError("ENVIRONMENT_ACCOUNT_MISMATCH", f"Intent environment {intent.environment} does not match account execution mode {mode}.", 422)
                expected_venue = str(account_config.get("provider") or account_config.get("venue") or ("simulated" if expected_mode == "PAPER" else "gate")).strip().lower()
                intent_venue = str(intent.venue or "").strip().lower()
                if intent_venue != expected_venue:
                    raise GatewayError(
                        "ACCOUNT_VENUE_MISMATCH",
                        f"422 ACCOUNT_VENUE_MISMATCH: Account '{intent.account_id}' is bound to venue '{expected_venue}', not '{intent.venue}'.",
                        422,
                    )
            else:
                # The in-memory test double has no account registry.  Keep
                # its explicit legacy-name mismatch guard, but never use it
                # for SQLite-backed production accounts.
                aid_lower = intent.account_id.lower()
                if mode in ("PAPER", "TESTNET", "RESEARCH") and "live" in aid_lower and "not_live" not in aid_lower:
                    raise GatewayError("ENVIRONMENT_ACCOUNT_MISMATCH", f"Account '{intent.account_id}' is live, but intent requested mode {mode}.", 422)
                if mode == "LIVE" and any(value in aid_lower for value in ("testnet", "sandbox", "demo", "paper")):
                    raise GatewayError("ENVIRONMENT_ACCOUNT_MISMATCH", f"Account '{intent.account_id}' is testnet/sandbox, but intent requested mode LIVE.", 422)
            if intent.environment and str(intent.environment).upper() != mode:
                raise GatewayError("ENVIRONMENT_ACCOUNT_MISMATCH", f"Intent environment {intent.environment} does not match account mode {mode}.", 422)

            # This check occurs before inserting an intent or reserving risk.
            # Reduce-only protection is deliberately exempt so a lease loss
            # cannot turn into an unprotected position; all new exposure is
            # fenced by the current durable runtime epoch.
            self._require_runtime_fence(intent)
            payload_hash = intent.compute_payload_hash()
            # Idempotency is checked before authorization so a replay of an
            # already terminal request returns its original receipt even if
            # the scope has since expired or been revoked.
            if hasattr(self.store, "_connect"):
                with self.store._connect() as db:
                    existing = db.execute("SELECT * FROM order_intents WHERE idempotency_key=?", (intent.idempotency_key,)).fetchone()
                    if existing:
                        if existing["payload_hash"] != payload_hash:
                            raise IdempotencyConflictError(f"409 Conflict: idempotency_key '{intent.idempotency_key}' was previously submitted with a different payload.")
                        if existing["execution_result_json"]:
                            try:
                                stored_result = json.loads(existing["execution_result_json"])
                            except (TypeError, ValueError, json.JSONDecodeError):
                                stored_result = None
                            if isinstance(stored_result, dict) and stored_result:
                                return stored_result
                        return {
                            "intent_id": existing["intent_id"],
                            "idempotency_key": intent.idempotency_key,
                            "status": existing["status"],
                            "reconciled": False,
                            "message": "Order is still in flight; no duplicate execution was attempted.",
                        }
                    now_iso = datetime.now(timezone.utc).isoformat()
                    try:
                        db.execute(
                            """INSERT INTO order_intents
                               (intent_id, idempotency_key, account_id, mode, instrument_id, side, order_type, quantity, price, leverage,
                                protection_plan_json, payload_hash, status, execution_result_json, created_at, updated_at,
                               venue, environment, reduce_only, position_id, cycle_id, generation, authorization_id,
                               authorization_version, expires_at, lease_holder_id, fencing_token, runtime_lease_name,
                               strategy_id, strategy_version, decision_path, control_mode, session_id, signal_at,
                               provider, candidate_id, closed_15m_bar, order_preference, final_order_type,
                               selection_reason_code, selection_reason, selection_evidence_json, limit_price,
                               ttl_seconds, selection_policy_version, remote_order_status)
                               VALUES (
                                   ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                               )""",
                            (
                                intent.intent_id, intent.idempotency_key, intent.account_id, mode, intent.instrument_id,
                                side_clean, intent.order_type.lower(), float(intent.quantity), float(intent.price) if intent.price is not None else None,
                                intent.leverage, json.dumps(intent.protection_plan.to_dict() if intent.protection_plan else {}, allow_nan=False),
                                payload_hash, OrderStatus.CREATED.value, None, now_iso, now_iso, str(intent.venue or "simulated"),
                                intent.environment or mode, int(bool(intent.reduce_only)), intent.position_id,
                                intent.cycle_id, intent.generation, intent.authorization_id,
                                intent.authorization_version, intent.expires_at, intent.lease_holder_id,
                                intent.fencing_token, intent.runtime_lease_name, intent.strategy_id,
                                intent.strategy_version,
                                 intent.decision_path.value if isinstance(intent.decision_path, DecisionPath) else intent.decision_path,
                                 intent.control_mode.value if isinstance(intent.control_mode, ControlMode) else intent.control_mode,
                                 intent.session_id, intent.signal_at,
                                 str(intent.venue or "simulated").strip().lower(), intent.candidate_id,
                                 intent.closed_15m_bar, str(intent.order_preference or "AUTO").upper(),
                                 intent.final_order_type or intent.order_type.lower(), intent.selection_reason_code,
                                 intent.selection_reason, json.dumps(intent.selection_evidence or {}, allow_nan=False),
                                 intent.limit_price, intent.ttl_seconds, intent.selection_policy_version, None,
                             ),
                        )
                    except sqlite3.IntegrityError:
                        existing = db.execute("SELECT * FROM order_intents WHERE idempotency_key=?", (intent.idempotency_key,)).fetchone()
                        if existing and existing["payload_hash"] == payload_hash:
                            if existing["execution_result_json"]:
                                try:
                                    stored_result = json.loads(existing["execution_result_json"])
                                except (TypeError, ValueError, json.JSONDecodeError):
                                    stored_result = None
                                if isinstance(stored_result, dict) and stored_result:
                                    return stored_result
                            return {"intent_id": existing["intent_id"], "status": existing["status"], "reconciled": False}
                        raise IdempotencyConflictError(f"409 Conflict: idempotency_key '{intent.idempotency_key}' was previously submitted with a different payload.")
            else:
                orders = getattr(self.store, "_orders", {})
                for existing in orders.values():
                    if existing.get("idempotency_key") == intent.idempotency_key:
                        if existing.get("payload_hash") != payload_hash:
                            raise IdempotencyConflictError(f"409 Conflict: idempotency_key '{intent.idempotency_key}' was previously submitted with a different payload.")
                        stored_result = existing.get("execution_result")
                        if isinstance(stored_result, dict) and stored_result:
                            return stored_result
                        return {"intent_id": existing.get("intent_id"), "status": existing.get("status"), "reconciled": False}
                orders[intent.intent_id] = {"intent_id": intent.intent_id, "idempotency_key": intent.idempotency_key, "account_id": intent.account_id, "mode": mode, "instrument_id": intent.instrument_id, "side": side_clean, "payload_hash": payload_hash, "status": OrderStatus.CREATED.value}
                self.store._orders = orders

            clock = now or datetime.now(timezone.utc)
            if clock.tzinfo is None:
                clock = clock.replace(tzinfo=timezone.utc)
            else:
                clock = clock.astimezone(timezone.utc)
            if intent.expires_at:
                expiry = self._parse_timestamp(intent.expires_at)
                if expiry is None:
                    raise GatewayError("INTENT_EXPIRY_INVALID", "Order intent expires_at is not a valid timestamp.", 422)
                if clock > expiry:
                    raise GatewayError("INTENT_EXPIRED", "Order intent expired before execution.", 422)

            # Credential verification, account/environment routing, runtime
            # fencing (when a caller elects to use it), idempotency and the
            # RiskEngine are the executable boundary.  TradingAuthorization
            # was a duplicate local permit and deliberately is not consulted.
            active_auth = None

            if mode == "RESEARCH":
                raise GatewayError("MODE_NOT_EXECUTABLE", "RESEARCH mode is non-executable", 403)
            if mode in ("TESTNET", "LIVE") and trader_client is None:
                raise GatewayError("EXECUTION_ADAPTER_UNAVAILABLE", f"422 EXECUTION_ADAPTER_UNAVAILABLE: No execution adapter connected for {mode} account '{intent.account_id}'.", 422)

            fresh_market = self._fresh_market_snapshot(intent.instrument_id, market_snapshot, now=clock)
            fresh_market = self._apply_paper_contract_defaults(intent, fresh_market)
            # This is deliberately after freshness/account checks and before
            # risk reservation or any adapter/PAPER effect.  It applies to
            # opening and reduce-only intents alike.
            try:
                if intent_mode_value(intent.mode) in {TradingMode.TESTNET.value, TradingMode.LIVE.value} and not intent.reduce_only:
                    self._validate_remote_market_economics(fresh_market)
                self._validate_intent_market_rules(intent, fresh_market)
            except GatewayError as exc:
                # For a remote venue, an adapter may be connected while its
                # market metadata is not present in the local public-bar
                # snapshot.  Do not invent a local fill or claim the rules
                # were verified; allow the adapter to apply its own venue
                # contract and carry the absence in the execution evidence.
                # PAPER never reaches this branch because its explicit local
                # simulator contract was applied above.
                if (
                    intent_mode_value(intent.mode) in {TradingMode.TESTNET.value, TradingMode.LIVE.value}
                    and trader_client is not None
                    and exc.code == "MARKET_RULES_UNAVAILABLE"
                ):
                    if not intent.reduce_only:
                        # Do not let RiskEngine's compatibility defaults size a
                        # remote opening when the venue contract was not
                        # supplied.  The old adapter-timeout compatibility
                        # path records an explicit UNKNOWN/NOT_SUBMITTED
                        # receipt, with no reservation and no adapter call;
                        # callers must retry with a new idempotency key after
                        # the adapter provides contract evidence.
                        result = {
                            "intent_id": intent.intent_id,
                            "idempotency_key": intent.idempotency_key,
                            "status": OrderStatus.UNKNOWN.value,
                            "mode": intent_mode_value(intent.mode),
                            "venue": str(intent.venue or "gate"),
                            "symbol": intent.instrument_id,
                            "side": intent.side,
                            "quantity": intent.quantity,
                            "reconciled": False,
                            "risk_reservation_status": "NOT_CREATED",
                            "error_code": "MARKET_RULES_UNAVAILABLE",
                            "error": "Remote opening was not submitted because venue contract rules were not provided to the gateway.",
                            "market_contract_evidence": {
                                "status": "NOT_PROVIDED_TO_GATEWAY",
                                "source": "REMOTE_ADAPTER_AUTHORITY",
                                "exchange_metadata": False,
                                "reconciliation_required": True,
                            },
                            "execution_evidence": {
                                "source": "execution_gateway_preflight",
                                "status": "NOT_SUBMITTED",
                                "reason": "REMOTE_MARKET_RULES_REQUIRED_FOR_RISK_SIZING",
                                "observed_at": datetime.now(timezone.utc).isoformat(),
                            },
                        }
                        self._update_order(intent.intent_id, OrderStatus.UNKNOWN.value, result)
                        return result
                    # Protective remote reductions may be handed to the
                    # venue adapter when the local public quote has no
                    # contract metadata; the adapter remains responsible for
                    # rejecting an unrepresentable amount and for returning
                    # concrete fill/fee evidence.  Openings are different:
                    # RiskEngine sizing must never fall back to synthetic
                    # steps/fees for TESTNET/LIVE.
                    fresh_market["market_contract_evidence"] = {
                        "status": "NOT_PROVIDED_TO_GATEWAY",
                        "source": "REMOTE_ADAPTER_AUTHORITY",
                        "exchange_metadata": False,
                        "reconciliation_required": True,
                    }
                else:
                    raise
            # Managed Gate TestNet orders use one remote-account read for both
            # opening risk sizing and reduce-only position validation.  The
            # local simulated ledger is never a substitute for this fact.  A
            # degraded private endpoint therefore blocks a normal close with
            # an explicit error instead of falsely claiming that no position
            # exists; the dedicated recovery path remains available for an
            # already-known remote order when this gateway is unavailable.
            remote_account_truth: Dict[str, Any] | None = None
            if managed_gate_testnet and mode == TradingMode.TESTNET:
                from .gate_account_truth import GateAccountTruthService

                if trader_client is None:
                    raise GatewayError(
                        "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE",
                        "Gate TestNet 远端账户事实不可用，未创建新的风险敞口。",
                        422,
                    )
                remote_account_truth = GateAccountTruthService(self.store).refresh(
                    intent.account_id,
                    trader_client,
                    include_trades=False,
                )
                if str(remote_account_truth.get("status") or "").upper() != "AVAILABLE":
                    raise GatewayError(
                        "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE",
                        "Gate TestNet 账户/持仓事实未完整取得，未提交订单。",
                        422,
                    )
                if intent.reduce_only:
                    self._validate_gate_remote_reduce_only(intent, remote_account_truth)
                elif any(remote_account_truth.get(field) is None for field in ("equity", "available_margin")):
                    raise GatewayError(
                        "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE",
                        "Gate TestNet 账户余额/可用保证金未取得完整远端事实，未提交订单。",
                        422,
                    )
            elif intent.reduce_only:
                self._validate_reduce_only(intent)

            risk_decision = None
            reservation_id = None
            if not intent.reduce_only:
                if self.risk_engine is None:
                    raise GatewayError("RISK_ENGINE_UNAVAILABLE", "Unified RiskEngine is unavailable.", 503)
                if mode != "PAPER":
                    from .strategy_execution import venue_leverage_limit

                    market_meta = fresh_market.get("market") or fresh_market.get("metadata") or {}
                    if venue_leverage_limit(market_meta) is None:
                        raise GatewayError(
                            "VENUE_LEVERAGE_LIMIT_UNAVAILABLE",
                            "Gate contract leverage ceiling is unavailable; opening order was not submitted.",
                            422,
                        )
                auth_risk_limit = None
                auth_portfolio_limit = None
                auth_cluster_limit = None
                auth_daily_limit = None
                if active_auth is not None:
                    try:
                        auth_risk_limit = active_auth.limits.get("max_single_risk_pct")
                        auth_portfolio_limit = active_auth.limits.get("max_portfolio_risk_pct")
                        auth_cluster_limit = active_auth.limits.get("max_cluster_risk_pct")
                        auth_daily_limit = active_auth.limits.get("max_daily_loss_pct")
                    except AttributeError:
                        auth_risk_limit = auth_portfolio_limit = auth_cluster_limit = auth_daily_limit = None
                risk_decision = self.risk_engine.evaluate_intent(
                    intent,
                    fresh_market,
                    now=clock,
                    open_positions=(
                        self._gate_remote_positions_for_risk(remote_account_truth, intent.account_id)
                        if remote_account_truth is not None
                        else None
                    ),
                    max_single_risk_fraction=auth_risk_limit,
                    max_portfolio_risk_fraction=auth_portfolio_limit,
                    max_cluster_risk_fraction=auth_cluster_limit,
                    max_daily_loss_fraction=auth_daily_limit,
                )
                if not risk_decision.approved:
                    raise GatewayError(risk_decision.reason_code, f"RiskEngine rejected opening order: {risk_decision.reason_code}.", 422)
                # RiskEngine is authoritative for the actual leverage. Persist
                # the resolved value before any paper/exchange side effect so
                # the ledger, Gate set_leverage call and audit row agree.
                effective_leverage = int(risk_decision.leverage)
                if hasattr(self.store, "_connect"):
                    with self.store._connect() as db:
                        db.execute(
                            "UPDATE order_intents SET leverage=?, updated_at=? WHERE intent_id=?",
                            (effective_leverage, datetime.now(timezone.utc).isoformat(), intent.intent_id),
                        )
                # Preserve the caller's immutable/idempotent request payload;
                # execute with a derived copy carrying the authoritative
                # leverage instead of mutating the original OrderIntent.
                intent = replace(intent, leverage=effective_leverage)
                reservation_id = risk_decision.reservation_id
                self._update_order(intent.intent_id, OrderStatus.RISK_APPROVED.value, {"intent_id": intent.intent_id, "status": OrderStatus.RISK_APPROVED.value, "risk_decision": risk_decision.to_dict()}, reservation_id=reservation_id, risk_decision=risk_decision.to_dict())

            try:
                # Recheck the optional runtime fence immediately before the
                # adapter or PAPER side effect.  A manually submitted order
                # uses an unfenced gateway and is not forced to start a
                # separate AI/session authorization first.
                self._require_runtime_fence(intent)
                if mode == "PAPER":
                    exec_result = self._execute_paper(intent, fresh_market, risk_decision, now=clock)
                else:
                    exec_result = self._execute_exchange(intent, trader_client, fresh_market)
                if remote_account_truth and isinstance(exec_result, dict):
                    exec_result["remote_account_truth"] = {
                        "snapshot_id": remote_account_truth.get("snapshot_id"),
                        "observed_at": remote_account_truth.get("observed_at"),
                        "status": remote_account_truth.get("status"),
                        "source": remote_account_truth.get("source"),
                    }
            except Exception:
                if reservation_id:
                    self.ledger.release_risk(intent.account_id, reservation_id)
                raise

            final_status = str(exec_result.get("status", OrderStatus.REJECTED.value))
            if reservation_id:
                protection_status = str(exec_result.get("protection_status") or "").upper()
                protection_verified = intent.reduce_only or protection_status == ProtectionStatus.ACTIVE.value
                # A filled exchange order whose conditional protection is not
                # verified still carries open risk.  Keep the reservation
                # pending until reconciliation proves protection; never turn
                # a PENDING/FAILED position into free budget merely because a
                # venue reported a fill.
                if final_status == OrderStatus.FILLED.value and protection_verified:
                    self.ledger.commit_risk(intent.account_id, reservation_id)
                elif final_status == OrderStatus.FILLED.value and not protection_verified:
                    exec_result.setdefault("risk_reservation_status", "PENDING")
                elif final_status == OrderStatus.PARTIALLY_FILLED.value:
                    exec_result.setdefault("risk_reservation_status", "PENDING")
                elif final_status in (OrderStatus.REJECTED.value, OrderStatus.CANCELED.value, OrderStatus.EXPIRED.value):
                    self.ledger.release_risk(intent.account_id, reservation_id)
            self._update_order(intent.intent_id, final_status, exec_result, reservation_id=reservation_id, risk_decision=risk_decision.to_dict() if risk_decision else None)
            return exec_result

        # This branch is outside the lock only for exceptions raised above;
        # normalize and persist the explicit rejection receipt.

    def _execute_paper(
        self,
        intent: OrderIntent,
        market_snapshot: Optional[Dict[str, Any]] = None,
        risk_decision: Any = None,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Deterministic Paper Simulation Execution using a fresh quote."""
        if not market_snapshot or not market_snapshot.get("fresh"):
            raise GatewayError("MARKET_DATA_UNAVAILABLE", "Paper execution requires a fresh executable market snapshot.", 422)
        quote = Decimal(str(market_snapshot.get("price")))
        market = dict(market_snapshot.get("market") or market_snapshot.get("metadata") or {})
        contract_size = Decimal(str(market.get("contractSize", market.get("contract_size", market_snapshot.get("contractSize", 1)))))
        fee_rate = Decimal(str(market_snapshot.get("fee_rate", market.get("taker", market_snapshot.get("taker_fee", 0.0005)))))
        slippage = Decimal(str(market_snapshot.get("slippage", 0.001)))
        if not quote.is_finite() or quote <= 0 or not contract_size.is_finite() or contract_size <= 0:
            raise GatewayError("MARKET_DATA_UNAVAILABLE", "Paper execution received an invalid executable quote or contract size.", 422)
        if not fee_rate.is_finite() or fee_rate < 0 or not slippage.is_finite() or slippage < 0:
            raise GatewayError("MARKET_DATA_UNAVAILABLE", "Paper execution received invalid fee or slippage metadata.", 422)
        side = intent.side.upper()
        direction_sign = Decimal("1") if side in ("LONG", "BUY") else Decimal("-1")
        is_buy = direction_sign > 0

        def _market_price(key: str) -> Optional[Decimal]:
            value = market_snapshot.get(key)
            if value is None:
                value = market.get(key)
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return None
            return parsed if parsed.is_finite() and parsed > 0 else None

        executable_quote = _market_price("ask" if is_buy else "bid")
        max_adverse = quote * (Decimal("1") + (slippage if is_buy else -slippage))
        if executable_quote is None:
            executable_quote = max_adverse
            price_source = "quote_plus_configured_slippage"
        else:
            if (is_buy and executable_quote > max_adverse) or (not is_buy and executable_quote < max_adverse):
                raise GatewayError(
                    "SLIPPAGE_CONSTRAINT_EXCEEDED",
                    "Paper execution quote exceeds the configured adverse-slippage bound.",
                    422,
                )
            price_source = "order_book_ask" if is_buy else "order_book_bid"
        fill_price = executable_quote
        ord_id = f"ord_paper_{intent.intent_id}"
        fill_id = f"fill_paper_{intent.intent_id}"
        if intent.order_type.lower() == "limit":
            if intent.price is None:
                raise GatewayError("LIMIT_PRICE_REQUIRED", "Limit orders require an explicit limit price.", 422)
            limit_price = Decimal(str(intent.price))
            if (is_buy and limit_price < executable_quote) or (not is_buy and limit_price > executable_quote):
                return {
                    "intent_id": intent.intent_id,
                    "order_id": ord_id,
                    "status": OrderStatus.ACKNOWLEDGED.value,
                    "mode": intent_mode_value(intent.mode),
                    "symbol": intent.instrument_id,
                    "side": intent.side,
                    "filled_quantity": 0,
                    "average_price": None,
                    "fee": 0,
                    "fills": [],
                    "protection": intent.protection_plan.to_dict() if intent.protection_plan else None,
                    "market_data_as_of": market_snapshot.get("data_as_of"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "message": "Limit order remains open because the fresh quote does not cross the limit.",
                    "execution_evidence": {
                        "source": "paper_matching_engine",
                        "status": "RESTING_LIMIT",
                        "market_data_as_of": market_snapshot.get("data_as_of"),
                    },
                }
            fill_price = min(limit_price, executable_quote) if is_buy else max(limit_price, executable_quote)
            price_source = "limit_crossed_ask" if is_buy else "limit_crossed_bid"

        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        else:
            clock = clock.astimezone(timezone.utc)
        now_iso = clock.isoformat()
        fee_dec = (Decimal(str(intent.quantity)) * fill_price * contract_size * fee_rate).quantize(Decimal("0.00000001"))
        slippage_cost_dec = (abs(fill_price - quote) * Decimal(str(intent.quantity)) * contract_size).quantize(Decimal("0.00000001"))
        recorded_fill: Dict[str, Any] = {}
        protection_status = ProtectionStatus.UNKNOWN.value if intent.reduce_only else ProtectionStatus.FAILED.value
        if self.ledger is not None:
            recorded_fill = self.ledger.record_trade_fill(
                account_id=intent.account_id,
                instrument_id=intent.instrument_id,
                side=intent.side,
                quantity=Decimal(str(intent.quantity)),
                price=fill_price,
                fee=fee_dec,
                mode=intent_mode_value(intent.mode),
                venue=str(intent.venue or "simulated"),
                order_id=ord_id,
                event_id=f"evt_{fill_id}",
                trade_id=fill_id,
                position_id=intent.position_id,
                reduce_only=bool(intent.reduce_only),
                contract_size=contract_size,
                leverage=Decimal(str(intent.leverage or 1)),
                protection_status="PENDING" if not intent.reduce_only else "UNKNOWN",
                stop_price=float(intent.protection_plan.stop_price) if intent.protection_plan and intent.protection_plan.stop_price else None,
                take_profit=float(intent.protection_plan.take_profit) if intent.protection_plan and intent.protection_plan.take_profit else None,
                fee_currency=str(market_snapshot.get("fee_currency") or "USDT"),
                fx_rate=market_snapshot.get("fx_rate"),
                event_at=datetime.fromisoformat(now_iso),
                protection_contract=intent.protection_plan.to_dict() if intent.protection_plan else None,
                trade_plan_id=intent.protection_plan.trade_plan_id if intent.protection_plan else None,
                slippage_cost=slippage_cost_dec,
                provider=str(intent.venue or "simulated"),
                environment=str(intent.environment or intent_mode_value(intent.mode)).lower(),
                candidate_id=intent.candidate_id,
                cycle_id=intent.cycle_id,
                strategy_id=intent.strategy_id,
                strategy_version=intent.strategy_version,
                fee_source="PAPER_MATCHING_ENGINE",
            )
            generated_position_id = intent.position_id or recorded_fill.get("position_id")
            if not intent.reduce_only and generated_position_id:
                protected = self.ledger.mark_protection(
                    intent.account_id,
                    generated_position_id,
                    ProtectionStatus.ACTIVE.value,
                    venue=str(intent.venue or "simulated"),
                    mode=intent_mode_value(intent.mode),
                    evidence={"mode": "PAPER", "venue": "simulated", "order_id": ord_id, "verified_at": now_iso},
                )
                if protected and intent.protection_plan:
                    intent.protection_plan.status = ProtectionStatus.ACTIVE
                    protection_status = ProtectionStatus.ACTIVE.value
                else:
                    # The fill is durable, so it must not be hidden behind a
                    # rejected order or release its budget.  Persist FAILED
                    # protection and return a filled-but-quarantined receipt;
                    # the pending reservation blocks further opening risk.
                    self.ledger.mark_protection(
                        intent.account_id,
                        generated_position_id,
                        ProtectionStatus.FAILED.value,
                        venue=str(intent.venue or "simulated"),
                        mode=intent_mode_value(intent.mode),
                        evidence={
                            "mode": "PAPER",
                            "venue": "simulated",
                            "order_id": ord_id,
                            "verified_at": now_iso,
                            "failure": "PROTECTION_REGISTRATION_UNVERIFIED",
                        },
                    )
                    if intent.protection_plan:
                        intent.protection_plan.status = ProtectionStatus.FAILED
            elif not intent.reduce_only:
                # A PAPER fill without a ledger/position is not executable
                # evidence.  Keep it explicitly quarantined rather than
                # claiming ACTIVE protection.
                protection_status = ProtectionStatus.FAILED.value
                if intent.protection_plan:
                    intent.protection_plan.status = ProtectionStatus.FAILED

        return {
            "intent_id": intent.intent_id,
            "order_id": ord_id,
            "status": OrderStatus.FILLED.value,
            "mode": intent_mode_value(intent.mode),
            "venue": str(intent.venue or "simulated"),
            "symbol": intent.instrument_id,
            "side": intent.side,
            "reduce_only": bool(intent.reduce_only),
            "position_id": intent.position_id or recorded_fill.get("position_id"),
            "quantity": intent.quantity,
            "filled_quantity": intent.quantity,
            "average_price": float(fill_price),
            "fee": float(fee_dec),
            "fee_currency": "USDT",
            "contract_size": float(contract_size),
            "fee_rate": float(fee_rate),
            "slippage": float(slippage),
            "slippage_cost": float(slippage_cost_dec),
            "price_source": price_source,
            "protection_status": protection_status,
            "risk_reservation_status": "COMMITTABLE" if protection_status == ProtectionStatus.ACTIVE.value or intent.reduce_only else "PENDING",
            "market_data_as_of": market_snapshot.get("data_as_of"),
            "fills": [{"fill_id": fill_id, "price": float(fill_price), "quantity": intent.quantity, "time": now_iso}],
            "protection": intent.protection_plan.to_dict() if intent.protection_plan else None,
            "execution_evidence": {
                "source": "paper_matching_engine",
                "simulated": True,
                "status": "FILLED",
                "market_data_as_of": market_snapshot.get("data_as_of"),
                "order_id": ord_id,
                "fill_id": fill_id,
            },
            "created_at": now_iso,
        }

    def _record_exchange_fill_report(
        self,
        intent: OrderIntent,
        response: Dict[str, Any],
        market_snapshot: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Reconcile concrete adapter fills into the scoped local ledger.

        Adapter ``filled`` fields are usually cumulative.  Explicit fill
        records are preferred; otherwise only the delta above the already
        recorded quantity/fee is applied.  A venue status alone is never
        treated as a local fill or as verified protection.
        """
        if self.ledger is None:
            return {"recorded": False, "protection_status": ProtectionStatus.PENDING.value}
        remote_order_id = str(response.get("order_id") or response.get("id") or intent.intent_id)
        mode = intent_mode_value(intent.mode)
        venue = str(intent.venue or "gate")
        raw_fills = response.get("fills") if isinstance(response.get("fills"), list) else []
        fill_reports: list[tuple[str, Decimal, Decimal, Decimal, str, Optional[Decimal], Decimal, Optional[datetime]]] = []
        known_fill = False

        def parse_decimal(value: Any) -> Optional[Decimal]:
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return None
            return parsed if parsed.is_finite() and parsed > 0 else None

        def parse_fee(value: Any) -> Optional[Decimal]:
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return None
            return parsed if parsed.is_finite() and parsed >= 0 else None

        def parse_event_at(value: Any) -> Optional[datetime]:
            return self._parse_timestamp(value)

        raw_response_fee = response.get("fee")
        if raw_response_fee is None:
            raw_response_fee = response.get("fee_cost")
        response_fee = parse_fee(raw_response_fee)
        fee_evidence_observed = response_fee is not None and raw_response_fee is not None
        fee_currency = str(response.get("fee_currency") or response.get("feeCurrency") or "USDT").strip().upper() or "USDT"
        fx_value = response.get("fx_rate", response.get("fxRate"))
        try:
            response_fx_rate = Decimal(str(fx_value)) if fx_value is not None else None
            if response_fx_rate is not None and (not response_fx_rate.is_finite() or response_fx_rate <= 0):
                response_fx_rate = None
        except (InvalidOperation, TypeError, ValueError):
            response_fx_rate = None
        raw_response_contract = response.get("contract_size")
        if raw_response_contract is None:
            raw_response_contract = response.get("contractSize")
        response_contract = parse_decimal(raw_response_contract)
        contract_evidence_source = "ADAPTER_RESPONSE" if response_contract is not None else None
        if response_contract is None and market_snapshot:
            market_meta = market_snapshot.get("market") or market_snapshot.get("metadata") or {}
            response_contract = parse_decimal(
                market_meta.get("contractSize", market_meta.get("contract_size", market_snapshot.get("contractSize")))
            )
            if response_contract is not None:
                contract_evidence_source = "MARKET_SNAPSHOT"
        contract_evidence_observed = response_contract is not None
        response_contract = response_contract or Decimal("1")
        response_event_at = parse_event_at(response.get("event_at") or response.get("timestamp") or response.get("created_at"))
        remote_authoritative = venue.strip().lower() == "gate" and mode == "TESTNET"

        if raw_fills:
            parsed_items: list[tuple[str, Decimal, Decimal, Decimal | None, Decimal, str, Optional[Decimal], Optional[datetime]]] = []
            for index, item in enumerate(raw_fills):
                if not isinstance(item, dict):
                    continue
                qty = parse_decimal(item.get("quantity", item.get("amount", item.get("filled"))))
                price = parse_decimal(item.get("price", item.get("average_price", item.get("average"))))
                if qty is None or price is None:
                    continue
                fill_id = str(item.get("trade_id") or item.get("fill_id") or item.get("id") or f"{remote_order_id}:fill:{index}")
                raw_item_fee = item.get("fee")
                if raw_item_fee is None:
                    raw_item_fee = item.get("fee_cost")
                item_fee = parse_fee(raw_item_fee)
                if item_fee is not None and raw_item_fee is not None:
                    fee_evidence_observed = True
                item_contract = parse_decimal(item.get("contract_size", item.get("contractSize"))) or response_contract
                item_currency = str(item.get("fee_currency") or item.get("feeCurrency") or fee_currency).strip().upper() or fee_currency
                item_fx = item.get("fx_rate", item.get("fxRate"))
                try:
                    item_fx_rate = Decimal(str(item_fx)) if item_fx is not None else response_fx_rate
                    if item_fx_rate is not None and (not item_fx_rate.is_finite() or item_fx_rate <= 0):
                        item_fx_rate = None
                except (InvalidOperation, TypeError, ValueError):
                    item_fx_rate = response_fx_rate
                item_event_at = parse_event_at(item.get("event_at") or item.get("timestamp") or item.get("created_at")) or response_event_at
                parsed_items.append((fill_id, qty, price, item_fee, item_contract, item_currency, item_fx_rate, item_event_at))
            previous_qty, previous_fee = self.ledger.get_recorded_fill_totals(
                intent.account_id,
                remote_order_id,
                venue=venue,
                mode=mode,
            ) if hasattr(self.ledger, "get_recorded_fill_totals") else (Decimal("0"), Decimal("0"))
            known_fill = previous_qty > 0
            new_items = [
                item for item in parsed_items
                if not (
                    hasattr(self.ledger, "has_recorded_fill")
                    and self.ledger.has_recorded_fill(
                        intent.account_id,
                        remote_order_id,
                        item[0],
                        venue=venue,
                        mode=mode,
                    )
                )
            ]
            fee_delta = max(Decimal("0"), (response_fee or Decimal("0")) - previous_fee)
            new_qty = sum((item[1] for item in new_items), Decimal("0"))
            for fill_id, qty, price, item_fee, item_contract, item_currency, item_fx_rate, item_event_at in new_items:
                if item_fee is None and fee_delta > 0 and new_qty > 0:
                    item_fee = fee_delta * qty / new_qty
                fill_reports.append((fill_id, qty, price, item_fee or Decimal("0"), item_currency, item_fx_rate, item_contract, item_event_at))
        else:
            quantity_present = any(key in response for key in ("filled_quantity", "filled"))
            total_qty = parse_decimal(response.get("filled_quantity", response.get("filled"))) if quantity_present else None
            price = parse_decimal(response.get("average_price", response.get("average", response.get("price"))))
            if total_qty is not None and price is not None:
                if hasattr(self.ledger, "get_recorded_fill_totals"):
                    previous_qty, previous_fee = self.ledger.get_recorded_fill_totals(
                        intent.account_id,
                        remote_order_id,
                        venue=venue,
                        mode=mode,
                    )
                else:
                    previous_qty, previous_fee = Decimal("0"), Decimal("0")
                known_fill = previous_qty > 0
                delta_qty = total_qty - previous_qty
                total_fee = response_fee or Decimal("0")
                delta_fee = max(Decimal("0"), total_fee - previous_fee)
                if delta_qty > 0:
                    fill_reports.append(
                        (
                            f"{remote_order_id}:aggregate:{_decimal_text(total_qty)}",
                            delta_qty,
                            price,
                            delta_fee,
                            fee_currency,
                            response_fx_rate,
                            response_contract,
                            response_event_at,
                        )
                    )

        last_record: Dict[str, Any] | None = None
        for fill_id, quantity, price, fee, fill_fee_currency, fill_fx_rate, fill_contract_size, fill_event_at in fill_reports:
            last_record = self.ledger.record_trade_fill(
                account_id=intent.account_id,
                instrument_id=intent.instrument_id,
                side=intent.side,
                quantity=quantity,
                price=price,
                fee=fee,
                mode=mode,
                venue=venue,
                order_id=remote_order_id,
                trade_id=fill_id,
                reduce_only=bool(intent.reduce_only),
                position_id=intent.position_id,
                stop_price=float(intent.protection_plan.stop_price) if intent.protection_plan else None,
                take_profit=float(intent.protection_plan.take_profit) if intent.protection_plan and intent.protection_plan.take_profit else None,
                protection_status=(
                    ProtectionStatus.ACTIVE.value
                    if response.get("protection_verified") and not intent.reduce_only
                    else ProtectionStatus.UNKNOWN.value
                    if intent.reduce_only
                    else ProtectionStatus.PENDING.value
                ),
                fee_currency=fill_fee_currency,
                fx_rate=fill_fx_rate,
                contract_size=fill_contract_size,
                leverage=Decimal(str(intent.leverage or 1)),
                event_at=fill_event_at or response_event_at,
                protection_contract=intent.protection_plan.to_dict() if intent.protection_plan else None,
                trade_plan_id=intent.protection_plan.trade_plan_id if intent.protection_plan else None,
                provider=str(intent.venue or "gate"),
                environment=str(intent.environment or mode).lower(),
                candidate_id=intent.candidate_id,
                cycle_id=intent.cycle_id,
                strategy_id=intent.strategy_id,
                strategy_version=intent.strategy_version,
                fee_source="REMOTE_ADAPTER" if fill_fee_currency else "REMOTE_ADAPTER_FEE_UNKNOWN",
            )
            known_fill = True

        if remote_authoritative and known_fill and last_record is None:
            # A cumulative exchange retry may contain no new trade row.  It is
            # still a concrete, already-recorded remote fill and must not be
            # downgraded to UNKNOWN merely because there is no local position
            # row to replay.
            last_record = {
                "status": "RECORDED",
                "position_id": intent.position_id,
                "remote_order_id": remote_order_id,
                "replayed": True,
                "local_mirror": False,
                "source": "GATE_REMOTE_PRIVATE_API_AUDIT",
            }

        protection_status = ProtectionStatus.UNKNOWN.value if intent.reduce_only else (
            ProtectionStatus.ACTIVE.value
            if remote_authoritative and response.get("protection_verified")
            else ProtectionStatus.PENDING.value
        )
        protection_evidence = None
        if response.get("protection_verified") and not intent.reduce_only and not remote_authoritative and last_record is None and known_fill:
            # A later reconciliation may repeat the same cumulative fill with
            # newly verified conditional protection.  No new trade row is
            # expected in that case, but the existing position must still be
            # upgraded from PENDING to ACTIVE.
            scoped_positions = self.ledger.get_open_positions(
                intent.account_id,
                venue=venue,
                mode=mode,
            )
            existing_position = next(
                (
                    position
                    for position in scoped_positions
                    if (
                        (intent.position_id and str(position.get("position_id")) == str(intent.position_id))
                        or str(position.get("order_id") or "") == remote_order_id
                    )
                    and str(position.get("instrument_id", position.get("symbol", ""))).upper() == str(intent.instrument_id).upper()
                ),
                None,
            )
            if existing_position is not None:
                last_record = {
                    "status": "RECORDED",
                    "position_id": existing_position.get("position_id"),
                    "replayed": True,
                }
        if response.get("protection_verified") and not intent.reduce_only and remote_authoritative and last_record:
            protection_evidence = {
                "source": "execution_adapter_response",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "remote_order_id": response.get("order_id") or response.get("id"),
                "protection_verified": True,
                "local_mirror": False,
            }
        elif response.get("protection_verified") and not intent.reduce_only and last_record and last_record.get("position_id"):
            protection_evidence = {
                "source": "execution_adapter_response",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "remote_order_id": response.get("order_id") or response.get("id"),
                "protection_verified": True,
            }
            if self.ledger.mark_protection(
                intent.account_id,
                last_record["position_id"],
                ProtectionStatus.ACTIVE.value,
                venue=venue,
                mode=mode,
                evidence=protection_evidence,
            ):
                protection_status = ProtectionStatus.ACTIVE.value
            else:
                protection_status = ProtectionStatus.FAILED.value
        elif response.get("protection_verified") and not intent.reduce_only and not last_record:
            protection_status = ProtectionStatus.PENDING.value

        economic_status = "VERIFIED" if fee_evidence_observed and contract_evidence_observed else "UNVERIFIED"
        economic_evidence = {
            "status": economic_status,
            "reconciliation_required": economic_status != "VERIFIED",
            "fee": {
                "status": "OBSERVED_ADAPTER" if fee_evidence_observed else "UNKNOWN_NOT_PROVIDED",
                "source": "execution_adapter_response" if fee_evidence_observed else None,
                "currency": fee_currency,
            },
            "contract_size": {
                "status": "OBSERVED" if contract_evidence_observed else "UNKNOWN_NOT_PROVIDED",
                "source": contract_evidence_source,
                "value": _decimal_text(response_contract),
            },
            "mode": mode,
            "venue": venue,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "note": "A remote fill is not economically verified when the adapter omits fee or contract evidence; compatibility ledger values are not proof.",
        }

        return {
            "recorded": bool(last_record),
            "ledger_record": last_record,
            "protection_status": protection_status,
            "protection_evidence": protection_evidence,
            "economic_evidence": economic_evidence,
        }

    def _execute_exchange(self, intent: OrderIntent, trader_client: Any, market_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Exchange execution with conservative fill/protection reconciliation."""
        try:
            res = trader_client.place_order(
                symbol=intent.instrument_id,
                side=intent.side,
                amount=intent.quantity,
                price=intent.price if intent.order_type.lower() == "limit" else None,
                order_type=intent.order_type,
                stop_loss=intent.protection_plan.stop_price if intent.protection_plan else None,
                take_profit=intent.protection_plan.take_profit if intent.protection_plan else None,
                leverage=intent.leverage,
                reduce_only=intent.reduce_only,
                client_order_id=intent.intent_id,
            )
            if not isinstance(res, dict):
                raise GatewayError("ADAPTER_INVALID_RESPONSE", "Execution adapter returned a non-object response.", 502)
            status_raw = str(res.get("status", "")).lower()
            def _positive_decimal(value: Any) -> Optional[Decimal]:
                try:
                    parsed = Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError):
                    return None
                return parsed if parsed.is_finite() and parsed > 0 else None

            reported_filled = _positive_decimal(res.get("filled_quantity", res.get("filled"))) or Decimal("0")
            reported_amount = _positive_decimal(res.get("amount", res.get("quantity")))
            if status_raw in ("open", "new", "accepted", "ack", "acknowledged", "submitted", "pending", "dry_run_acknowledged"):
                if reported_filled > 0:
                    res["status"] = OrderStatus.FILLED.value if reported_amount and reported_filled >= reported_amount else OrderStatus.PARTIALLY_FILLED.value
                else:
                    res["status"] = OrderStatus.ACKNOWLEDGED.value
            elif status_raw in ("filled", "closed"):
                # A terminal venue label without a concrete quantity is not
                # local fill evidence and must remain reconciliable.
                if reported_filled <= 0:
                    res["status"] = OrderStatus.UNKNOWN.value
                else:
                    res["status"] = OrderStatus.FILLED.value if not reported_amount or reported_filled >= reported_amount else OrderStatus.PARTIALLY_FILLED.value
            elif status_raw in ("partially_filled", "partial"):
                res["status"] = OrderStatus.PARTIALLY_FILLED.value
            elif status_raw in ("cancelled", "canceled"):
                res["status"] = OrderStatus.CANCELED.value
            elif status_raw in ("rejected", "failed", "failure", "error", "expired", "execution_failed"):
                res["status"] = OrderStatus.REJECTED.value
            else:
                res["status"] = OrderStatus.UNKNOWN.value
            res.setdefault("intent_id", intent.intent_id)
            res.setdefault("mode", intent_mode_value(intent.mode))
            res.setdefault("venue", str(intent.venue or "simulated"))
            # A remote fill is only entered into the local ledger when the
            # adapter returned a concrete fill/quantity.  Protection remains
            # PENDING until the adapter explicitly proves its conditional
            # orders were accepted; no synthetic ACTIVE claim is allowed.
            res["execution_evidence"] = {
                "source": "execution_adapter_response",
                "simulated": False,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "remote_order_id": res.get("order_id") or res.get("id"),
                "raw_status": status_raw,
                "venue": str(intent.venue or "gate"),
                "mode": intent_mode_value(intent.mode),
            }
            if res["status"] in (OrderStatus.FILLED.value, OrderStatus.PARTIALLY_FILLED.value):
                reconciliation = self._record_exchange_fill_report(intent, res, market_snapshot)
                if reconciliation.get("recorded"):
                    res["ledger_record"] = reconciliation.get("ledger_record")
                res["protection_status"] = reconciliation.get("protection_status")
                if reconciliation.get("protection_evidence"):
                    res["protection_evidence"] = reconciliation["protection_evidence"]
                if reconciliation.get("economic_evidence"):
                    res["economic_reconciliation"] = reconciliation["economic_evidence"]
                if not reconciliation.get("recorded"):
                    res["status"] = OrderStatus.UNKNOWN.value
                    res["reconciled"] = False
                    res["fill_reconciliation"] = "CONCRETE_FILL_REQUIRED"
            return res
        except Exception as exc:
            err_msg = str(exc).lower()
            if "timeout" in err_msg or "timed out" in err_msg or "deadline" in err_msg:
                return {
                    "intent_id": intent.intent_id,
                    "order_id": f"ord_unknown_{intent.intent_id}",
                    "status": OrderStatus.UNKNOWN.value,
                    "mode": intent.mode.value if hasattr(intent.mode, "value") else str(intent.mode),
                    "symbol": intent.instrument_id,
                    "side": intent.side,
                    "quantity": intent.quantity,
                    "error_code": "ADAPTER_TIMEOUT_UNKNOWN",
                    "reconciled": False,
                    "error": str(exc),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            raise

    def cancel_intent(self, intent_id: str, reason: str = "USER_REQUESTED") -> Dict[str, Any]:
        """Cancel an OrderIntent with in-flight state reconciliation (AT37)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        if hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                row = db.execute("SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)).fetchone()
                if not row:
                    raise GatewayError("INTENT_NOT_FOUND", f"Order intent '{intent_id}' not found", status_code=404)

                status = row["status"]
                if status in (OrderStatus.FILLED.value, OrderStatus.CANCELED.value, OrderStatus.REJECTED.value):
                    return {"intent_id": intent_id, "status": status, "message": "Order already in terminal state"}
                reservation_id = row["reservation_id"] if "reservation_id" in row.keys() else None
                row_dict = dict(row)
                row_mode = str(row_dict.get("mode") or TradingMode.PAPER.value).upper()
                row_venue = str(row_dict.get("venue") or "simulated").lower()
                try:
                    from .account_scope import resolve_account_scope
                    scope = resolve_account_scope(self.store, str(row_dict.get("account_id") or ""))
                except Exception:
                    scope = None
                if scope:
                    row_mode = scope["mode"]
                    row_venue = scope["venue"]
                # Only an explicitly simulated account uses the local
                # matching engine.  The managed Gate TestNet compatibility
                # account has a legacy PAPER row mode, but its explicit
                # account_type routes cancellation to the remote TestNet.
                if row_mode == TradingMode.PAPER.value and row_venue == "simulated":
                    db.execute(
                        "UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=? AND status NOT IN (?, ?, ?)",
                        (OrderStatus.CANCELED.value, now_iso, intent_id, OrderStatus.FILLED.value, OrderStatus.CANCELED.value, OrderStatus.REJECTED.value),
                    )
                    db.commit()
                    if reservation_id and self.ledger is not None:
                        self.ledger.release_risk(row["account_id"], reservation_id)
                    return {"intent_id": intent_id, "status": OrderStatus.CANCELED.value, "verified_reconciled": True, "reconciled": True, "reason": reason}
                scoped_row = dict(row_dict)
                scoped_row["mode"] = row_mode
                scoped_row["venue"] = row_venue
                scoped_row["environment"] = str(scope.get("environment") if scope else row_mode).upper()
                scoped_intent = self._intent_from_order_row(scoped_row, fallback_mode=row_mode)
                adapter = self._resolve_scoped_trader_client(scoped_intent, None)
                if adapter is not None and hasattr(adapter, "cancel_order"):
                    try:
                        remote_order_id = row["intent_id"]
                        try:
                            previous_receipt = json.loads(row["execution_result_json"] or "{}")
                            remote_order_id = previous_receipt.get("order_id") or previous_receipt.get("id") or remote_order_id
                        except (TypeError, ValueError, json.JSONDecodeError):
                            pass
                        # Gate adapters follow the exchange convention
                        # cancel_order(order_id, symbol); the old call passed
                        # those arguments in reverse order and could report a
                        # local CANCEL_PENDING while leaving the remote order
                        # open.
                        result = adapter.cancel_order(remote_order_id, row["instrument_id"])
                        remote_status = str((result or {}).get("status", "")).lower()
                        if remote_status in {"cancelled", "canceled", "closed"}:
                            db.execute("UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?", (OrderStatus.CANCELED.value, now_iso, intent_id))
                            db.commit()
                            if reservation_id and self.ledger is not None:
                                self.ledger.release_risk(row["account_id"], reservation_id)
                            return {"intent_id": intent_id, "status": OrderStatus.CANCELED.value, "verified_reconciled": True, "reason": reason}
                    except Exception:
                        pass
                # Without a confirmed adapter response, an UNKNOWN or
                # submitting order remains in-flight and its reservation stays
                # held.  Cancellation intent is observable but not asserted as
                # a completed exchange action.
                db.execute("UPDATE order_intents SET status=?, updated_at=? WHERE intent_id=?", (OrderStatus.CANCEL_PENDING.value, now_iso, intent_id))
                return {"intent_id": intent_id, "status": OrderStatus.CANCEL_PENDING.value, "verified_reconciled": False, "reconciled": False, "reason": reason}
        else:
            orders = getattr(self.store, "_orders", {})
            if intent_id not in orders:
                raise GatewayError("INTENT_NOT_FOUND", f"Order intent '{intent_id}' not found", status_code=404)
            orders[intent_id]["status"] = "CANCEL_PENDING"
            return {
                "intent_id": intent_id,
                "status": "CANCEL_PENDING",
                "verified_reconciled": False,
                "reconciled": False,
                "reason": reason,
            }

    def cancel_order(self, intent_id: str, reason: str = "USER_REQUESTED") -> Dict[str, Any]:
        """Convenience alias for cancel_intent."""
        return self.cancel_intent(intent_id, reason=reason)

    @staticmethod
    def _resting_intent_is_stale(row: Dict[str, Any], fallback_mode: str) -> tuple[bool, str]:
        """Report whether a resting order intent has outlived its TTL.

        Returns ``(stale, reason)``.  The order-level TTL is authoritative;
        the row timestamp is only a fallback for rows written before the TTL
        column was populated.
        """

        expires_raw = row.get("expires_at")
        now = datetime.now(timezone.utc)
        if expires_raw:
            try:
                parsed = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if now >= parsed + STALE_INTENT_GRACE:
                    return True, "STALE_TTL_EXPIRED"
                return False, ""
            except ValueError:
                pass
        ttl_seconds = row.get("ttl_seconds")
        created_raw = row.get("created_at")
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError):
            ttl = 0
        if ttl > 0 and created_raw:
            try:
                created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if now >= created + timedelta(seconds=ttl) + STALE_INTENT_GRACE:
                    return True, "STALE_TTL_ELAPSED"
            except ValueError:
                pass
        return False, ""

    def reconcile_in_flight_orders(self, account_id: str, mode: TradingMode) -> List[Dict[str, Any]]:
        """Reconcile in-flight orders without blind resubmission.

        UNKNOWN is deliberately queried rather than treated as terminal.  A
        concrete remote fill is first applied through the same account-scoped
        ledger path as initial exchange execution; a venue status without a
        quantity and execution price remains UNKNOWN with its reservation
        held.
        """
        mode_val = mode.value if hasattr(mode, "value") else str(mode)
        now_iso = datetime.now(timezone.utc).isoformat()
        reconciled: list[Dict[str, Any]] = []
        if not hasattr(self.store, "_connect"):
            return reconciled
        # Runtime recovery may still pass the legacy PAPER row mode for the
        # Gate TestNet compatibility account.  Query the effective execution
        # mode so remote TestNet orders are not skipped by a stale display
        # value.
        try:
            from .account_scope import resolve_account_scope
            account_scope = resolve_account_scope(self.store, account_id)
        except Exception:
            account_scope = None
        query_mode = account_scope["mode"] if account_scope else str(mode_val).upper()
        with self.store._connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM order_intents WHERE account_id = ? AND mode = ? AND status IN (?, ?, ?, ?, ?)",
                (account_id, query_mode, OrderStatus.SUBMITTING.value, OrderStatus.ACKNOWLEDGED.value, OrderStatus.PARTIALLY_FILLED.value, OrderStatus.UNKNOWN.value, OrderStatus.CANCEL_PENDING.value),
            ).fetchall()]

        for row in rows:
            iid = row["intent_id"]
            previous_status = row["status"]
            reservation_id = row.get("reservation_id")
            row_mode_value = str(row.get("mode") or mode_val).upper()

            # PAPER has no remote venue to query.  Re-run the local matching
            # engine against a newly-fetched fresh quote so a resting limit
            # order can fill after submission without introducing a second
            # ledger path.  The deterministic paper fill id makes concurrent
            # reconciliation and process retries idempotent at the ledger.
            if row_mode_value == TradingMode.PAPER.value:
                try:
                    plan_data = json.loads(row.get("protection_plan_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    plan_data = {}
                plan = ProtectionPlan.from_dict(plan_data) if isinstance(plan_data, dict) and plan_data.get("stop_price") is not None else None
                intent = OrderIntent(
                    intent_id=iid,
                    idempotency_key=str(row.get("idempotency_key") or iid),
                    account_id=str(row["account_id"]),
                    mode=TradingMode.PAPER,
                    instrument_id=str(row["instrument_id"]),
                    side=str(row["side"]),
                    order_type=str(row.get("order_type") or "market"),
                    quantity=float(row.get("quantity") or 0),
                    price=float(row["price"]) if row.get("price") is not None else None,
                    leverage=int(row["leverage"]) if row.get("leverage") is not None else None,
                    protection_plan=plan,
                    reduce_only=bool(row.get("reduce_only") or 0),
                    venue=str(row.get("venue") or "simulated"),
                    environment=str(row.get("environment") or TradingMode.PAPER.value),
                    position_id=row.get("position_id"),
                    cycle_id=row.get("cycle_id"),
                    generation=row.get("generation"),
                    authorization_id=row.get("authorization_id"),
                    authorization_version=row.get("authorization_version"),
                    control_mode=row.get("control_mode") or ControlMode.ASSISTED.value,
                    decision_path=row.get("decision_path") or DecisionPath.STRATEGY_DRIVEN.value,
                    session_id=row.get("session_id"),
                    strategy_id=row.get("strategy_id"),
                    strategy_version=row.get("strategy_version"),
                    signal_at=row.get("signal_at"),
                )
                try:
                    fresh_market = self._fresh_market_snapshot(intent.instrument_id)
                    paper_result = self._execute_paper(intent, fresh_market)
                    final_status = str(paper_result.get("status") or OrderStatus.REJECTED.value)
                    protection_status = str(paper_result.get("protection_status") or "").upper()
                    protection_verified = intent.reduce_only or protection_status == ProtectionStatus.ACTIVE.value
                    if reservation_id and self.ledger is not None and final_status == OrderStatus.FILLED.value and protection_verified:
                        self.ledger.commit_risk(account_id, reservation_id)
                        paper_result.setdefault("risk_reservation_status", "COMMITTED")
                    elif reservation_id and final_status == OrderStatus.FILLED.value and not protection_verified:
                        paper_result.setdefault("risk_reservation_status", "PENDING")
                    paper_result["reconciled"] = final_status == OrderStatus.FILLED.value
                    paper_result.setdefault("execution_evidence", {})
                    paper_result["execution_evidence"].update({"reconciled_at": now_iso, "reconciliation_source": "paper_matching_engine"})
                    with self.store._connect() as db:
                        db.execute(
                            "UPDATE order_intents SET status=?, execution_result_json=?, updated_at=? WHERE intent_id=?",
                            (final_status, json.dumps(paper_result, allow_nan=False), now_iso, iid),
                        )
                    reconciled.append({
                        "intent_id": iid,
                        "previous_status": previous_status,
                        "reconciled_status": final_status,
                        "reconciled": True,
                        "reservation_held": not (final_status == OrderStatus.FILLED.value and protection_verified),
                    })
                except GatewayError as exc:
                    # A stale/missing quote is not a rejection of the resting
                    # order.  Keep ACKNOWLEDGED and its risk reservation until
                    # a fresh matching attempt or explicit cancellation.
                    reconciled.append({
                        "intent_id": iid,
                        "previous_status": previous_status,
                        "reconciled_status": previous_status,
                        "reconciled": False,
                        "reservation_held": True,
                        "reason": exc.code,
                    })
                except Exception as exc:
                    logger.warning("PAPER order reconciliation failed for %s: %s", iid, exc)
                    reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True, "reason": "PAPER_RECONCILIATION_FAILED"})
                continue

            scoped_row = dict(row)
            scoped_row["mode"] = row_mode_value
            if account_scope:
                scoped_row["venue"] = account_scope["venue"]
                scoped_row["environment"] = account_scope["environment"].upper()
            scoped_intent = self._intent_from_order_row(scoped_row, fallback_mode=query_mode)
            adapter = self._resolve_scoped_trader_client(scoped_intent, None)
            if adapter is None or not hasattr(adapter, "fetch_order"):
                reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True})
                continue

            try:
                remote_order_id = iid
                try:
                    previous_receipt = json.loads(row.get("execution_result_json") or "{}")
                    remote_order_id = previous_receipt.get("order_id") or previous_receipt.get("id") or remote_order_id
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                remote = adapter.fetch_order(remote_order_id, row["instrument_id"])
                if not isinstance(remote, dict):
                    raise ValueError("ADAPTER_INVALID_RECONCILIATION_RESPONSE")
                remote_status = str(remote.get("status", "")).lower()
                filled_value = remote.get("filled_quantity", remote.get("filled"))
                try:
                    filled_quantity = Decimal(str(filled_value)) if filled_value is not None else Decimal("0")
                except (InvalidOperation, TypeError, ValueError):
                    filled_quantity = Decimal("0")
                is_fill_state = remote_status in {"closed", "filled", "partially_filled", "partial"} or filled_quantity > 0
                if is_fill_state:
                    try:
                        plan_data = json.loads(row.get("protection_plan_json") or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        plan_data = {}
                    plan = ProtectionPlan.from_dict(plan_data) if isinstance(plan_data, dict) and plan_data.get("stop_price") is not None else None
                    try:
                        row_mode = TradingMode(str(row.get("mode") or mode_val).upper())
                    except ValueError:
                        row_mode = mode
                    intent = OrderIntent(
                        intent_id=iid,
                        idempotency_key=str(row.get("idempotency_key") or iid),
                        account_id=str(row["account_id"]),
                        mode=row_mode,
                        instrument_id=str(row["instrument_id"]),
                        side=str(row["side"]),
                        order_type=str(row.get("order_type") or "market"),
                        quantity=float(row.get("quantity") or 0),
                        price=float(row["price"]) if row.get("price") is not None else None,
                        leverage=int(row["leverage"]) if row.get("leverage") is not None else None,
                        protection_plan=plan,
                        reduce_only=bool(row.get("reduce_only") or 0),
                        venue=str(row.get("venue") or "gate"),
                        environment=str(row.get("environment") or row.get("mode") or mode_val),
                        position_id=row.get("position_id"),
                        cycle_id=row.get("cycle_id"),
                        generation=row.get("generation"),
                        authorization_id=row.get("authorization_id"),
                        authorization_version=row.get("authorization_version"),
                        control_mode=row.get("control_mode") or ControlMode.ASSISTED.value,
                        decision_path=row.get("decision_path") or DecisionPath.STRATEGY_DRIVEN.value,
                        session_id=row.get("session_id"),
                        strategy_id=row.get("strategy_id"),
                        strategy_version=row.get("strategy_version"),
                        signal_at=row.get("signal_at"),
                    )
                    if intent.protection_plan and not intent.reduce_only and hasattr(adapter, "place_protection_orders"):
                        prev_prot_orders = previous_receipt.get("protection_orders") or []
                        if not prev_prot_orders and (intent.protection_plan.stop_price is not None or intent.protection_plan.take_profit is not None):
                            try:
                                prot_legs = adapter.place_protection_orders(
                                    symbol=intent.instrument_id,
                                    side=intent.side,
                                    amount=float(filled_quantity),
                                    stop_loss=float(intent.protection_plan.stop_price) if intent.protection_plan.stop_price is not None else None,
                                    take_profit=float(intent.protection_plan.take_profit) if intent.protection_plan.take_profit is not None else None,
                                    client_order_id=intent.intent_id,
                                )
                                remote["protection_orders"] = prot_legs
                                remote["protection_status"] = "PROTECTED"
                                remote["protection_verified"] = True
                            except Exception as prot_err:
                                logger.error("Failed to place protection orders during reconciliation for %s: %s", iid, prot_err)
                                remote["protection_status"] = "PROTECTION_FAILED"

                    reconciliation = self._record_exchange_fill_report(intent, remote, None)
                    local_quantity = Decimal("0")
                    if self.ledger is not None and hasattr(self.ledger, "get_recorded_fill_totals"):
                        local_quantity, _ = self.ledger.get_recorded_fill_totals(
                            account_id,
                            str(remote.get("order_id") or remote.get("id") or remote_order_id),
                            venue=intent.venue,
                            mode=mode_val,
                        )
                    has_concrete_local_fill = bool(reconciliation.get("recorded") or local_quantity > 0)
                    if not has_concrete_local_fill:
                        reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True, "reason": "CONCRETE_FILL_REQUIRED"})
                        continue
                    final_status = OrderStatus.PARTIALLY_FILLED.value
                    amount_value = remote.get("amount", remote.get("quantity"))
                    try:
                        amount = Decimal(str(amount_value)) if amount_value is not None else Decimal("0")
                    except (InvalidOperation, TypeError, ValueError):
                        amount = Decimal("0")
                    if remote_status in {"closed", "filled"} or (amount > 0 and filled_quantity >= amount):
                        final_status = OrderStatus.FILLED.value
                    receipt = dict(remote)
                    receipt.update({"intent_id": iid, "reconciled": True, "execution_evidence": {"source": "execution_adapter_reconciliation", "observed_at": now_iso, "remote_order_id": remote.get("order_id") or remote.get("id") or remote_order_id}})
                    if reconciliation.get("ledger_record"):
                        receipt["ledger_record"] = reconciliation["ledger_record"]
                    receipt["protection_status"] = reconciliation.get("protection_status") or remote.get("protection_status")
                    if remote.get("protection_orders"):
                        receipt["protection_orders"] = remote.get("protection_orders")
                    if reconciliation.get("economic_evidence"):
                        receipt["economic_reconciliation"] = reconciliation["economic_evidence"]
                    with self.store._connect() as db:
                        db.execute("UPDATE order_intents SET status=?, execution_result_json=?, updated_at=? WHERE intent_id=?", (final_status, json.dumps(receipt, allow_nan=False), now_iso, iid))
                    protection_verified = bool(intent.reduce_only or reconciliation.get("protection_status") == ProtectionStatus.ACTIVE.value)
                    if reservation_id and self.ledger is not None and final_status == OrderStatus.FILLED.value and protection_verified:
                        self.ledger.commit_risk(account_id, reservation_id)
                    reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": final_status, "reconciled": True, "reservation_held": not (final_status == OrderStatus.FILLED.value and protection_verified)})
                    continue

                if remote_status in {"cancelled", "canceled"}:
                    with self.store._connect() as db:
                        db.execute("UPDATE order_intents SET status=?, execution_result_json=?, updated_at=? WHERE intent_id=?", (OrderStatus.CANCELED.value, json.dumps({"intent_id": iid, "reconciled": True, "status": OrderStatus.CANCELED.value, "execution_evidence": {"source": "execution_adapter_reconciliation", "observed_at": now_iso, "remote_order_id": remote.get("order_id") or remote.get("id") or remote_order_id}}, allow_nan=False), now_iso, iid))
                    if reservation_id and self.ledger is not None:
                        self.ledger.release_risk(account_id, reservation_id)
                    reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": OrderStatus.CANCELED.value, "reconciled": True, "reservation_held": False})
                    continue

                # A resting (``open``) remote order has no terminal state yet.
                # Left alone it pins the account: the intent never leaves the
                # active set and its risk reservation stays PENDING forever,
                # so every later opening is refused.  Once the intent's own
                # TTL has elapsed the order is no longer wanted, so cancel it
                # remotely and only then release its budget.
                if remote_status == "open":
                    stale, stale_reason = self._resting_intent_is_stale(row, mode_val)
                    if not stale:
                        reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True, "reason": "RESTING_WITHIN_TTL"})
                        continue
                    if adapter is None or not hasattr(adapter, "cancel_order"):
                        reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True, "reason": "CANCEL_UNSUPPORTED"})
                        continue
                    try:
                        cancel_result = adapter.cancel_order(remote_order_id, row["instrument_id"])
                    except Exception as exc:
                        logger.warning("Stale resting order cancel failed for %s: %s", iid, exc)
                        reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True, "reason": "CANCEL_FAILED"})
                        continue
                    # Confirm the terminal state before touching local facts:
                    # a cancel response alone is not proof of the venue state.
                    try:
                        after = adapter.fetch_order(remote_order_id, row["instrument_id"])
                    except Exception as exc:
                        logger.warning("Stale resting order verify failed for %s: %s", iid, exc)
                        reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True, "reason": "CANCEL_UNVERIFIED"})
                        continue
                    final_remote_status = str((after or {}).get("status", "")).lower()
                    filled_after = (after or {}).get("filled_quantity", (after or {}).get("filled"))
                    try:
                        filled_after_dec = Decimal(str(filled_after)) if filled_after is not None else Decimal("0")
                    except (InvalidOperation, TypeError, ValueError):
                        filled_after_dec = Decimal("0")
                    if filled_after_dec > 0 or final_remote_status in {"closed", "filled", "partially_filled", "partial"}:
                        # The order filled while we were cancelling it.  Do
                        # not report a clean cancellation; let the next pass
                        # handle it through the fill path.
                        reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True, "reason": "FILLED_DURING_CANCEL"})
                        continue
                    if final_remote_status not in {"cancelled", "canceled", "expired", "rejected"}:
                        reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True, "reason": "CANCEL_NOT_TERMINAL"})
                        continue
                    cancel_receipt = {
                        "intent_id": iid,
                        "status": OrderStatus.CANCELED.value,
                        "reconciled": True,
                        "reason": stale_reason,
                        "cancel_result": cancel_result if isinstance(cancel_result, dict) else str(cancel_result),
                        "execution_evidence": {
                            "source": "execution_adapter_reconciliation",
                            "observed_at": now_iso,
                            "remote_order_id": (after or {}).get("order_id") or (after or {}).get("id") or remote_order_id,
                            "remote_status": final_remote_status,
                        },
                    }
                    with self.store._connect() as db:
                        db.execute("UPDATE order_intents SET status=?, execution_result_json=?, updated_at=? WHERE intent_id=?", (OrderStatus.CANCELED.value, json.dumps(cancel_receipt, allow_nan=False), now_iso, iid))
                    if reservation_id and self.ledger is not None:
                        self.ledger.release_risk(account_id, reservation_id)
                    reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": OrderStatus.CANCELED.value, "reconciled": True, "reservation_held": False, "reason": stale_reason})
                    continue
            except Exception as exc:
                logger.warning("Order reconciliation failed for %s: %s", iid, exc)
            reconciled.append({"intent_id": iid, "previous_status": previous_status, "reconciled_status": previous_status, "reconciled": False, "reservation_held": True})
        return reconciled

    submit_order_intent = submit_intent
