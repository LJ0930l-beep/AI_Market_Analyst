"""Remote Gate TestNet account truth and local mirror projection.

The execution ledger is intentionally local, but it is not an authority for a
managed Gate account.  This service records every read from Gate and projects
only the observed account/position facts into the local read model.  It never
creates a trade fill, assumes an opening balance, or turns an unavailable
remote response into an empty account.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from typing import Any, Callable, Dict, Optional

from .account_aliases import GATE_TESTNET_ACCOUNT_ID, canonical_account_id


def _iso(value: Any, fallback: datetime | None = None) -> str:
    point = value if isinstance(value, datetime) else None
    if point is None and value:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            point = None
    point = point or fallback or datetime.now(timezone.utc)
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    else:
        point = point.astimezone(timezone.utc)
    return point.isoformat()


def _json_safe(value: Any) -> Any:
    """Make an observed provider payload serializable without inventing values."""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _json_text(value: Any, default: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) if value is not None else json.dumps(default, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class GateAccountTruthService:
    """Persist and project the latest remote Gate account facts."""

    def __init__(self, store: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ensure_tables()

    def ensure_tables(self) -> None:
        with self.store._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS gate_remote_account_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    equity TEXT,
                    available_margin TEXT,
                    used_margin TEXT,
                    unrealized_pnl TEXT,
                    realized_pnl TEXT,
                    balance_json TEXT NOT NULL DEFAULT '{}',
                    positions_json TEXT NOT NULL DEFAULT '[]',
                    pending_orders_json TEXT NOT NULL DEFAULT '[]',
                    fills_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    endpoint TEXT,
                    raw_hash TEXT NOT NULL,
                    error_code TEXT,
                    message_zh TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gate_remote_account_snapshots_scope
                    ON gate_remote_account_snapshots(account_id, observed_at DESC, snapshot_id DESC);
                """
            )
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(gate_remote_account_snapshots)").fetchall()
            }
            if "balance_json" not in columns:
                # Existing installations are upgraded in place; no snapshot
                # or audit row is deleted or rewritten.
                db.execute(
                    "ALTER TABLE gate_remote_account_snapshots ADD COLUMN balance_json TEXT NOT NULL DEFAULT '{}'"
                )

    @staticmethod
    def _failure(account_id: str, code: str, message: str, *, observed_at: str) -> Dict[str, Any]:
        return {
            "status": "UNAVAILABLE",
            "source": "Gate.io private API",
            "api_environment": "TESTNET",
            "endpoint": None,
            "observed_at": observed_at,
            "equity": None,
            "available_margin": None,
            "used_margin": None,
            "unrealized_pnl": None,
            "realized_pnl": None,
            "balance": {"data_status": "UNAVAILABLE"},
            "positions": [],
            "pending_orders": [],
            "fills": [],
            "positions_status": "UNAVAILABLE",
            "pending_orders_status": "UNAVAILABLE",
            "fills_status": "NOT_REQUESTED",
            "error_code": code,
            "message_zh": message,
            "remote_truth": True,
            "account_id": account_id,
        }

    def _record(self, account_id: str, truth: Dict[str, Any], *, created_at: str) -> Dict[str, Any]:
        observed_at = _iso(truth.get("observed_at"), self.clock())
        positions = truth.get("positions") if isinstance(truth.get("positions"), list) else []
        pending_orders = truth.get("pending_orders") if isinstance(truth.get("pending_orders"), list) else []
        fills = truth.get("fills") if isinstance(truth.get("fills"), list) else []
        balance = truth.get("balance") if isinstance(truth.get("balance"), dict) else {}
        raw_payload = {
            "account_id": account_id,
            "status": truth.get("status"),
            "observed_at": observed_at,
            "equity": truth.get("equity"),
            "available_margin": truth.get("available_margin"),
            "used_margin": truth.get("used_margin"),
            "unrealized_pnl": truth.get("unrealized_pnl"),
            "realized_pnl": truth.get("realized_pnl"),
            "balance": balance,
            "positions": positions,
            "pending_orders": pending_orders,
            "fills": fills,
            "error_code": truth.get("error_code"),
        }
        raw_hash = hashlib.sha256(_json_text(raw_payload, {}).encode("utf-8")).hexdigest()
        snapshot_id = f"gate_remote_{account_id}_{raw_hash[:20]}_{hashlib.sha256(created_at.encode()).hexdigest()[:8]}"
        source = str(truth.get("source") or "Gate.io private API")[:200]
        endpoint = str(truth.get("endpoint") or "")[:300] or None
        with self.store._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO gate_remote_account_snapshots(
                    snapshot_id, account_id, provider, environment, observed_at,
                    status, equity, available_margin, used_margin, unrealized_pnl,
                    realized_pnl, balance_json, positions_json, pending_orders_json, fills_json,
                    source, endpoint, raw_hash, error_code, message_zh, created_at
                ) VALUES (?, ?, 'gate', 'testnet', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    account_id,
                    observed_at,
                    str(truth.get("status") or "UNAVAILABLE").upper(),
                    str(truth.get("equity")) if truth.get("equity") is not None else None,
                    str(truth.get("available_margin")) if truth.get("available_margin") is not None else None,
                    str(truth.get("used_margin")) if truth.get("used_margin") is not None else None,
                    str(truth.get("unrealized_pnl")) if truth.get("unrealized_pnl") is not None else None,
                    str(truth.get("realized_pnl")) if truth.get("realized_pnl") is not None else None,
                    _json_text(balance, {}),
                    _json_text(positions, []),
                    _json_text(pending_orders, []),
                    _json_text(fills, []),
                    source,
                    endpoint,
                    raw_hash,
                    str(truth.get("error_code") or "")[:120] or None,
                    str(truth.get("message_zh") or "")[:500] or None,
                    created_at,
                ),
            )
        return {
            "snapshot_id": snapshot_id,
            "account_id": account_id,
            "provider": "gate",
            "environment": "testnet",
            "observed_at": observed_at,
            "created_at": created_at,
            "raw_hash": raw_hash,
            **{key: _json_safe(value) for key, value in truth.items()},
        }

    def refresh(self, account_id: str, trader: Any, *, include_trades: bool = False) -> Dict[str, Any]:
        """Read and persist Gate facts; never create a local position mirror."""

        canonical = canonical_account_id(self.store, str(account_id or "").strip()) or GATE_TESTNET_ACCOUNT_ID
        now = _iso(self.clock())
        getter = getattr(trader, "get_account_truth", None)
        if not callable(getter):
            truth = self._failure(canonical, "REMOTE_ACCOUNT_TRUTH_UNSUPPORTED", "当前 Gate 适配器未提供远端账户事实读取能力。", observed_at=now)
        else:
            try:
                raw = getter(include_trades=include_trades)
                truth = dict(raw) if isinstance(raw, dict) else self._failure(canonical, "REMOTE_ACCOUNT_TRUTH_SCHEMA_INVALID", "Gate 账户事实响应格式异常，未使用本地账本替代。", observed_at=now)
            except Exception as exc:
                truth = self._failure(canonical, "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE", f"Gate 账户事实读取失败：{type(exc).__name__}。", observed_at=now)
        truth["account_id"] = canonical
        truth.setdefault("remote_truth", True)
        return self._record(canonical, truth, created_at=now)

    def latest(self, account_id: str, *, require_available: bool = False) -> Dict[str, Any] | None:
        canonical = canonical_account_id(self.store, str(account_id or "").strip())
        with self.store._connect() as db:
            row = db.execute(
                "SELECT * FROM gate_remote_account_snapshots WHERE account_id=? ORDER BY observed_at DESC, created_at DESC, snapshot_id DESC LIMIT 1",
                (canonical,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("balance_json", "positions_json", "pending_orders_json", "fills_json"):
            target = key[:-5]
            try:
                default = {} if key == "balance_json" else []
                result[target] = json.loads(result.pop(key) or json.dumps(default))
            except (TypeError, ValueError, json.JSONDecodeError):
                result[target] = {} if key == "balance_json" else []
        if require_available and str(result.get("status") or "").upper() != "AVAILABLE":
            return None
        return result


__all__ = ["GateAccountTruthService"]
