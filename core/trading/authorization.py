"""Trading Authorization and Scope Lifecycle Management (N09, AT36, AT38, AT40).

Strictly fail-closed authorization engine:
1. Prevents AI from self-authorizing (AT40).
2. Enforces explicit validity periods and blocks silent auto-renewal (AT36).
3. Revocation blocks new risk immediately while preserving existing protection stop orders (AT36, AT37).
4. Strictly confines orders to authorized instruments, directions, and risk caps.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import threading
from typing import Any, Dict, List, Optional
import uuid


class AuthorizationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    SUSPENDED = "SUSPENDED"


class EmergencyPolicy(str, Enum):
    MAINTAIN_PROTECTIONS_WAIT_MANUAL = "MAINTAIN_PROTECTIONS_WAIT_MANUAL"
    CANCEL_OPEN_AND_EXIT = "CANCEL_OPEN_AND_EXIT"


class ConfirmationSource(str, Enum):
    LOCAL_USER_WIZARD = "LOCAL_USER_WIZARD"
    MODEL_LLM_OUTPUT = "MODEL_LLM_OUTPUT"
    EXTERNAL_API = "EXTERNAL_API"


class AuthorizationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 403):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class AISelfConfirmationError(AuthorizationError):
    def __init__(
        self,
        message: str = "AI_SELF_CONFIRMATION_REJECTED: Model cannot self-authorize trading permissions.",
    ):
        super().__init__("AI_SELF_CONFIRMATION_REJECTED", message, status_code=403)


class AuthorizationExpiredError(AuthorizationError):
    def __init__(
        self,
        message: str = "403 AUTHORIZATION_EXPIRED: Scope has expired. Silent auto-renewal is forbidden.",
    ):
        super().__init__("AUTHORIZATION_EXPIRED", message, status_code=403)


class AuthorizationRevokedError(AuthorizationError):
    def __init__(
        self,
        message: str = "403 AUTHORIZATION_REVOKED: Authorization has been revoked.",
    ):
        super().__init__("AUTHORIZATION_REVOKED", message, status_code=403)


class InstrumentUnauthorizedError(AuthorizationError):
    def __init__(
        self,
        message: str = "403 INSTRUMENT_NOT_ALLOWED: Instrument not in authorized scope.",
    ):
        super().__init__("INSTRUMENT_NOT_AUTHORIZED", message, status_code=403)


class DirectionUnauthorizedError(AuthorizationError):
    def __init__(
        self,
        message: str = "403 DIRECTION_NOT_ALLOWED: Side not in authorized scope.",
    ):
        super().__init__("DIRECTION_NOT_ALLOWED", message, status_code=403)


class RiskLimitExceededError(AuthorizationError):
    def __init__(
        self,
        message: str = "403 RISK_LIMIT_EXCEEDED: Requested parameters exceed authorized caps.",
    ):
        super().__init__("LEVERAGE_EXCEEDS_AUTHORIZED_MAX", message, status_code=403)


@dataclass
class TradingAuthorization:
    authorization_id: str
    account_id: str
    venue: str
    mode: str  # PAPER, TESTNET, LIVE
    decision_path: str  # STRATEGY_DRIVEN, AI_LED
    model_digest: str
    agent_policy_version: str
    allowed_instruments: List[str]
    allowed_directions: List[str]
    limits: Dict[str, Any]  # max_single_risk_pct, max_portfolio_risk_pct, max_daily_loss_pct, max_leverage
    valid_from: str
    expires_at: str
    emergency_policy: str = EmergencyPolicy.MAINTAIN_PROTECTIONS_WAIT_MANUAL.value
    confirmed_by: str = "LOCAL_USER_WIZARD"
    confirmation_token: Optional[str] = None
    status: str = AuthorizationStatus.ACTIVE.value
    version: int = 1
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def is_active(self, as_of: Optional[datetime] = None) -> bool:
        if self.status != AuthorizationStatus.ACTIVE.value:
            return False
        now_dt = as_of or datetime.now(timezone.utc)
        from_dt = datetime.fromisoformat(self.valid_from)
        to_dt = datetime.fromisoformat(self.expires_at)
        return from_dt <= now_dt <= to_dt

    def is_valid(self, as_of: Optional[datetime] = None) -> bool:
        return self.is_active(as_of=as_of)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TradingAuthorization:
        return cls(**data)


class AuthorizationManager:
    def __init__(self, store: Any):
        self.store = store
        # All managers for one store share the same short execution fence.
        # Revocation and the final pre-adapter authorization read therefore
        # cannot interleave within this process.
        self._execution_lock = getattr(store, "_authorization_execution_lock", None)
        if self._execution_lock is None:
            self._execution_lock = threading.RLock()
            try:
                setattr(store, "_authorization_execution_lock", self._execution_lock)
            except Exception:
                pass
        if hasattr(self.store, "_connect"):
            self._init_db()
        else:
            if not hasattr(self.store, "_auths"):
                self.store._auths = {}

    def execution_boundary(self):
        """Serialize revoke with the final authorization-to-adapter fence."""
        return self._execution_lock

    def _init_db(self) -> None:
        with self.store._connect() as db:
            db.execute("""
            CREATE TABLE IF NOT EXISTS trading_authorizations (
                authorization_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 1,
                account_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                mode TEXT NOT NULL,
                decision_path TEXT NOT NULL,
                model_digest TEXT NOT NULL,
                agent_policy_version TEXT NOT NULL,
                allowed_instruments_json TEXT NOT NULL,
                allowed_directions_json TEXT NOT NULL,
                limits_json TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                emergency_policy TEXT NOT NULL,
                confirmed_by TEXT NOT NULL,
                confirmation_token TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

    def grant_authorization(
        self,
        account_id: str,
        venue: str = "gate",
        mode: Any = "TESTNET",
        decision_path: Any = "AI_LED",
        allowed_instruments: Optional[List[str]] = None,
        allowed_sides: Optional[List[str]] = None,
        allowed_directions: Optional[List[str]] = None,
        max_risk_fraction: Optional[Any] = None,
        max_leverage: Optional[int] = None,
        daily_loss_limit_fraction: Optional[Any] = None,
        max_portfolio_risk_fraction: Optional[Any] = None,
        max_cluster_risk_fraction: Optional[Any] = None,
        duration_seconds: int = 3600,
        confirmed_by: Any = ConfirmationSource.LOCAL_USER_WIZARD,
        confirmation_token: Optional[str] = None,
        model_digest: str = "qwen3.5:9b",
        agent_policy_version: str = "v2.1",
        emergency_policy: Any = EmergencyPolicy.MAINTAIN_PROTECTIONS_WAIT_MANUAL.value,
    ) -> TradingAuthorization:
        """Helper adapter supporting AT36 & AT40 test signatures."""
        now_dt = datetime.now(timezone.utc)
        valid_from = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=duration_seconds)).isoformat()

        mode_str = mode.value if hasattr(mode, "value") else str(mode)
        decision_str = decision_path.value if hasattr(decision_path, "value") else str(decision_path)
        confirmed_str = confirmed_by.value if hasattr(confirmed_by, "value") else str(confirmed_by)
        em_policy_str = emergency_policy.value if hasattr(emergency_policy, "value") else str(emergency_policy)

        directions = allowed_directions or allowed_sides or ["LONG", "SHORT"]
        instruments = allowed_instruments or ["*"]

        limits: Dict[str, Any] = {
            "max_single_risk_pct": float(max_risk_fraction) if max_risk_fraction is not None else 0.01,
            "max_portfolio_risk_pct": float(max_portfolio_risk_fraction) if max_portfolio_risk_fraction is not None else 0.01,
            "max_cluster_risk_pct": float(max_cluster_risk_fraction) if max_cluster_risk_fraction is not None else 0.005,
            "max_leverage": int(max_leverage) if max_leverage is not None else 3,
            "max_daily_loss_pct": float(daily_loss_limit_fraction) if daily_loss_limit_fraction is not None else 0.03,
        }

        return self.create_authorization(
            account_id=account_id,
            venue=venue,
            mode=mode_str,
            decision_path=decision_str,
            allowed_instruments=instruments,
            allowed_directions=directions,
            limits=limits,
            valid_from=valid_from,
            expires_at=expires_at,
            model_digest=model_digest,
            agent_policy_version=agent_policy_version,
            emergency_policy=em_policy_str,
            confirmed_by=confirmed_str,
            confirmation_token=confirmation_token,
        )

    def create_authorization(
        self,
        account_id: str,
        venue: str,
        mode: str,
        decision_path: str,
        allowed_instruments: List[str],
        allowed_directions: List[str],
        limits: Dict[str, Any],
        valid_from: str,
        expires_at: str,
        model_digest: str = "qwen3.5:9b",
        agent_policy_version: str = "v2.1",
        emergency_policy: str = EmergencyPolicy.MAINTAIN_PROTECTIONS_WAIT_MANUAL.value,
        confirmed_by: str = "LOCAL_USER_WIZARD",
        confirmation_token: Optional[str] = None,
    ) -> TradingAuthorization:
        # AT40: AI cannot self-grant or update limits!
        invalid_confirmers = {"AI", "AI_AGENT", "MODEL", "QWEN", "LLM", "SYSTEM_AUTO", "MODEL_LLM_OUTPUT"}
        conf_upper = str(confirmed_by).upper()
        if conf_upper in invalid_confirmers or "LLM" in conf_upper or "MODEL" in conf_upper:
            raise AISelfConfirmationError(
                "AI_SELF_CONFIRMATION_REJECTED: Model cannot self-authorize trading permissions. Explicit local user confirmation required."
            )
        if conf_upper != ConfirmationSource.LOCAL_USER_WIZARD.value:
            raise AuthorizationError(
                "LOCAL_CONFIRMATION_REQUIRED",
                "Only the protected local-user wizard can create a trading authorization.",
                status_code=403,
            )

        if not allowed_instruments:
            raise ValueError("allowed_instruments cannot be empty")
        if not allowed_directions:
            raise ValueError("allowed_directions cannot be empty")
        if not limits.get("max_single_risk_pct") or float(limits["max_single_risk_pct"]) <= 0:
            raise ValueError("limits.max_single_risk_pct must be greater than zero")

        now_iso = datetime.now(timezone.utc).isoformat()
        auth_id = f"auth_{uuid.uuid4().hex[:12]}"
        token = confirmation_token or f"tok_{uuid.uuid4().hex[:16]}"

        auth = TradingAuthorization(
            authorization_id=auth_id,
            account_id=account_id,
            venue=venue,
            mode=mode,
            decision_path=decision_path,
            model_digest=model_digest,
            agent_policy_version=agent_policy_version,
            allowed_instruments=allowed_instruments,
            allowed_directions=allowed_directions,
            limits=limits,
            valid_from=valid_from,
            expires_at=expires_at,
            emergency_policy=emergency_policy,
            confirmed_by=confirmed_by,
            confirmation_token=token,
            status=AuthorizationStatus.ACTIVE.value,
            version=1,
            created_at=now_iso,
            updated_at=now_iso,
        )

        if hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                db.execute("""
                INSERT INTO trading_authorizations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    auth.authorization_id,
                    auth.version,
                    auth.account_id,
                    auth.venue,
                    auth.mode,
                    auth.decision_path,
                    auth.model_digest,
                    auth.agent_policy_version,
                    json.dumps(auth.allowed_instruments),
                    json.dumps(auth.allowed_directions),
                    json.dumps(auth.limits),
                    auth.valid_from,
                    auth.expires_at,
                    auth.emergency_policy,
                    auth.confirmed_by,
                    auth.confirmation_token,
                    auth.status,
                    auth.created_at,
                    auth.updated_at,
                ))
        elif hasattr(self.store, "save_trading_authorization"):
            self.store.save_trading_authorization(auth.to_dict())
        else:
            self.store._auths[auth.authorization_id] = auth.to_dict()

        return auth

    def get_authorization(self, auth_id: str) -> Optional[TradingAuthorization]:
        if hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                row = db.execute(
                    "SELECT * FROM trading_authorizations WHERE authorization_id = ?",
                    (auth_id,),
                ).fetchone()
                if not row:
                    return None
                return self._row_to_auth(row)
        elif hasattr(self.store, "get_trading_authorization"):
            data = self.store.get_trading_authorization(auth_id)
            return TradingAuthorization.from_dict(data) if data else None
        else:
            data = getattr(self.store, "_auths", {}).get(auth_id)
            return TradingAuthorization.from_dict(data) if data else None

    def list_authorizations(self, account_id: str) -> List[TradingAuthorization]:
        if hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                rows = db.execute(
                    "SELECT * FROM trading_authorizations WHERE account_id = ? ORDER BY created_at DESC",
                    (account_id,),
                ).fetchall()
                return [self._row_to_auth(r) for r in rows]
        elif hasattr(self.store, "list_trading_authorizations"):
            items = self.store.list_trading_authorizations(account_id)
            return [TradingAuthorization.from_dict(d) for d in items]
        else:
            items = [d for d in getattr(self.store, "_auths", {}).values() if d.get("account_id") == account_id]
            return [TradingAuthorization.from_dict(d) for d in items]

    def get_active_authorization(
        self,
        account_id: str,
        mode: Optional[Any] = None,
        as_of: Optional[datetime] = None,
    ) -> Optional[TradingAuthorization]:
        now_dt = as_of or datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        mode_val = mode.value if hasattr(mode, "value") else (str(mode) if mode is not None else None)

        all_auths = self.list_authorizations(account_id)
        for auth in all_auths:
            if mode_val and auth.mode != mode_val:
                continue
            if auth.status == AuthorizationStatus.ACTIVE.value:
                to_dt = datetime.fromisoformat(auth.expires_at)
                if now_dt > to_dt:
                    # Expired
                    auth.status = AuthorizationStatus.EXPIRED.value
                    auth.updated_at = now_iso
                    self._save_auth_status(auth.authorization_id, AuthorizationStatus.EXPIRED.value, now_iso)
                    continue
                from_dt = datetime.fromisoformat(auth.valid_from)
                if from_dt <= now_dt <= to_dt:
                    return auth
        return None

    def _save_auth_status(self, auth_id: str, status: str, updated_at: str) -> None:
        if hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                db.execute(
                    "UPDATE trading_authorizations SET status = ?, updated_at = ?, version = version + 1 WHERE authorization_id = ? AND status = 'ACTIVE'",
                    (status, updated_at, auth_id),
                )
        elif hasattr(self.store, "get_trading_authorization"):
            data = self.store.get_trading_authorization(auth_id)
            if data:
                data["status"] = status
                data["updated_at"] = updated_at
                self.store.save_trading_authorization(data)
        elif hasattr(self.store, "_auths") and auth_id in self.store._auths:
            self.store._auths[auth_id]["status"] = status
            self.store._auths[auth_id]["updated_at"] = updated_at

    def revoke_authorization(self, auth_id: str, reason: str = "USER_REVOKED") -> TradingAuthorization:
        del reason  # The durable authorization schema stores status/version; caller retains the audit reason.
        with self.execution_boundary():
            now_iso = datetime.now(timezone.utc).isoformat()
            auth = self.get_authorization(auth_id)
            if not auth:
                raise AuthorizationError("AUTH_NOT_FOUND", f"Authorization {auth_id} not found", status_code=404)

            auth.status = AuthorizationStatus.REVOKED.value
            auth.updated_at = now_iso
            self._save_auth_status(auth_id, AuthorizationStatus.REVOKED.value, now_iso)
            return auth

    def validate_intent(
        self,
        intent: Any,
        auth: Optional[TradingAuthorization] = None,
        as_of: Optional[datetime] = None,
    ) -> None:
        """Enforces all authorization bounds on an OrderIntent (AT36)."""
        now_dt = as_of or datetime.now(timezone.utc)
        mode_str = intent.mode.value if hasattr(intent.mode, "value") else str(intent.mode)
        account_id = getattr(intent, "account_id", "default_account")

        target_auth = auth
        if not target_auth:
            target_auth = self.get_active_authorization(account_id, mode_str, as_of=now_dt)
        elif hasattr(self.store, "_connect"):
            # Re-read the authoritative row immediately before accepting an
            # intent. A revoke racing with a model response must win over a
            # previously cached ACTIVE object.
            current = self.get_authorization(target_auth.authorization_id)
            if current is None:
                raise AuthorizationError("AUTHORIZATION_REQUIRED", "Authorization no longer exists.", 403)
            target_auth = current

        if not target_auth:
            raise AuthorizationError(
                "AUTHORIZATION_REQUIRED",
                f"No active trading authorization found for account {account_id} and mode {mode_str}.",
                status_code=403,
            )

        if target_auth.status == AuthorizationStatus.REVOKED.value:
            raise AuthorizationRevokedError("403 AUTHORIZATION_REVOKED: Authorization has been revoked.")

        from_dt = datetime.fromisoformat(target_auth.valid_from)
        to_dt = datetime.fromisoformat(target_auth.expires_at)
        if now_dt < from_dt:
            raise AuthorizationError(
                "AUTHORIZATION_NOT_YET_VALID",
                "Authorization validity window has not started.",
                403,
            )
        if now_dt > to_dt or target_auth.status == AuthorizationStatus.EXPIRED.value:
            raise AuthorizationExpiredError("AUTHORIZATION_EXPIRED")

        if target_auth.account_id != account_id:
            raise AuthorizationError("AUTHORIZATION_ACCOUNT_MISMATCH", "Authorization belongs to another account.", 403)
        if str(target_auth.mode).upper() != mode_str.upper():
            raise AuthorizationError("AUTHORIZATION_MODE_MISMATCH", "Authorization mode does not match the order environment.", 403)
        intent_auth_id = getattr(intent, "authorization_id", None)
        if intent_auth_id and str(intent_auth_id) != str(target_auth.authorization_id):
            raise AuthorizationError("AUTHORIZATION_ID_MISMATCH", "Intent authorization does not match the active authorization.", 403)
        intent_auth_version = getattr(intent, "authorization_version", None)
        if intent_auth_version is not None and int(intent_auth_version) != int(target_auth.version):
            raise AuthorizationError("AUTHORIZATION_VERSION_MISMATCH", "Intent authorization version is stale.", 403)
        intent_venue = str(getattr(intent, "venue", "simulated") or "simulated").lower()
        auth_venue = str(target_auth.venue or "").lower()
        if not auth_venue or auth_venue != intent_venue:
            raise AuthorizationError("AUTHORIZATION_VENUE_MISMATCH", "Authorization venue does not match the order venue.", 403)
        intent_path = getattr(intent, "decision_path", None)
        intent_path = intent_path.value if hasattr(intent_path, "value") else str(intent_path or "")
        if target_auth.decision_path and intent_path and target_auth.decision_path != intent_path:
            raise AuthorizationError("AUTHORIZATION_DECISION_PATH_MISMATCH", "Authorization decision path does not match the order.", 403)

        # Instrument check
        clean_inst = intent.instrument_id.upper()
        allowed_insts = [i.upper() for i in target_auth.allowed_instruments]
        if clean_inst not in allowed_insts and "*" not in allowed_insts:
            raise InstrumentUnauthorizedError(
                f"403 INSTRUMENT_NOT_ALLOWED: Instrument '{clean_inst}' is outside authorized list {allowed_insts}."
            )

        # Direction check
        clean_side = intent.side.upper()
        if clean_side in ("BUY",):
            clean_side = "LONG"
        elif clean_side in ("SELL",):
            clean_side = "SHORT"
        allowed_dirs = [d.upper() for d in target_auth.allowed_directions]
        if clean_side not in allowed_dirs and not intent.reduce_only:
            raise DirectionUnauthorizedError(
                f"403 DIRECTION_NOT_ALLOWED: Side '{clean_side}' is outside authorized directions {allowed_dirs}."
            )

        # Leverage check
        if intent.leverage is not None:
            max_lev = target_auth.limits.get("max_leverage")
            if max_lev and intent.leverage > max_lev:
                raise RiskLimitExceededError(
                    f"403 RISK_LIMIT_EXCEEDED: Leverage {intent.leverage}x exceeds authorized maximum {max_lev}x."
                )

    def _row_to_auth(self, row: Any) -> TradingAuthorization:
        data = dict(row)
        data["allowed_instruments"] = json.loads(data["allowed_instruments_json"])
        data["allowed_directions"] = json.loads(data["allowed_directions_json"])
        data["limits"] = json.loads(data["limits_json"])
        del data["allowed_instruments_json"]
        del data["allowed_directions_json"]
        del data["limits_json"]
        return TradingAuthorization.from_dict(data)
