"""Remote Gate TestNet account truth and local mirror projection.

The execution ledger is intentionally local, but it is not an authority for a
managed Gate account.  This service records every read from Gate and projects
only the observed account/position facts into the local read model.  It never
creates a trade fill, assumes an opening balance, or turns an unavailable
remote response into an empty account.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from typing import Any, Callable, Dict, Iterable, Optional

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


def _finite_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


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


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper().replace("/", "").replace(":USDT", "").replace("_", "").replace("-", "")


def _side(value: Any) -> str:
    clean = str(value or "").strip().upper()
    if clean in {"BUY", "LONG", "1"}:
        return "LONG"
    if clean in {"SELL", "SHORT", "-1"}:
        return "SHORT"
    return clean or "UNKNOWN"


def _stable_position_id(account_id: str, symbol: str, side: str) -> str:
    identity = f"{account_id}|gate|TESTNET|{symbol}|{side}".encode("utf-8")
    return f"gate_remote_pos_{hashlib.sha256(identity).hexdigest()[:24]}"


def _protection_from_orders(symbol: str, pending_orders: Iterable[Dict[str, Any]]) -> str:
    """Return ACTIVE only when Gate explicitly exposes a reduce-only trigger."""

    target = _symbol(symbol)
    for order in pending_orders:
        if not isinstance(order, dict) or not bool(order.get("reduce_only")):
            continue
        if _symbol(order.get("symbol")) not in {"", target}:
            continue
        if _finite_decimal(order.get("stop_price")) is not None or str(order.get("type") or "").lower() in {"stop", "stop_market", "take_profit", "trigger"}:
            return "ACTIVE"
    return "UNKNOWN"


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
        raw_payload = {
            "account_id": account_id,
            "status": truth.get("status"),
            "observed_at": observed_at,
            "equity": truth.get("equity"),
            "available_margin": truth.get("available_margin"),
            "used_margin": truth.get("used_margin"),
            "unrealized_pnl": truth.get("unrealized_pnl"),
            "realized_pnl": truth.get("realized_pnl"),
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
                    realized_pnl, positions_json, pending_orders_json, fills_json,
                    source, endpoint, raw_hash, error_code, message_zh, created_at
                ) VALUES (?, ?, 'gate', 'testnet', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

    def _mirror_positions(self, account_id: str, positions: list[Dict[str, Any]], pending_orders: list[Dict[str, Any]], observed_at: str) -> None:
        """Replace the Gate position read model without writing economic fills."""

        with self.store._connect() as db:
            current_ids: list[str] = []
            for position in positions:
                if not isinstance(position, dict):
                    continue
                contracts = _finite_decimal(position.get("contracts", position.get("size")))
                symbol = _symbol(position.get("symbol"))
                side = _side(position.get("side"))
                if contracts is None or contracts <= 0 or not symbol or side not in {"LONG", "SHORT"}:
                    continue
                position_id = _stable_position_id(account_id, symbol, side)
                current_ids.append(position_id)
                existing = db.execute(
                    """SELECT position_id, payload_json, protection_status, position_version
                       FROM simulated_positions
                       WHERE account_id=? AND venue='gate' AND mode='TESTNET'
                         AND status IN ('OPEN','PARTIALLY_CLOSED')
                         AND json_extract(payload_json, '$.symbol')=?
                         AND UPPER(json_extract(payload_json, '$.side'))=?
                       ORDER BY updated_at DESC LIMIT 1""",
                    (account_id, symbol, side),
                ).fetchone()
                if existing is not None and str(existing["position_id"]) != position_id:
                    # Gateway-reconciled remote fills may already have a local
                    # position id.  Reuse it to avoid double counting in the
                    # local risk projection while still replacing its facts.
                    position_id = str(existing["position_id"])
                    current_ids[-1] = position_id
                try:
                    old_payload = json.loads(existing["payload_json"] or "{}") if existing else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    old_payload = {}
                contract_size = _finite_decimal(position.get("contract_size", position.get("contractSize")))
                leverage = _finite_decimal(position.get("leverage"))
                entry_price = _finite_decimal(position.get("entry_price", position.get("entryPrice")))
                mark_price = _finite_decimal(position.get("mark_price", position.get("markPrice")))
                unrealized = _finite_decimal(position.get("unrealized_pnl", position.get("unrealizedPnl")))
                protection_status = str(old_payload.get("protection_status") or (existing["protection_status"] if existing else "UNKNOWN")).upper()
                if protection_status != "ACTIVE":
                    protection_status = _protection_from_orders(symbol, pending_orders)
                payload = {
                    **old_payload,
                    "position_id": position_id,
                    "account_id": account_id,
                    "venue": "gate",
                    "mode": "TESTNET",
                    "environment": "testnet",
                    "provider": "gate",
                    "symbol": symbol,
                    "instrument_id": symbol,
                    "side": side,
                    "contracts": str(contracts),
                    "remaining_contracts": str(contracts),
                    "quantity": str(contracts),
                    "entry": str(entry_price) if entry_price is not None else None,
                    "entry_price": str(entry_price) if entry_price is not None else None,
                    "mark_price": str(mark_price) if mark_price is not None else None,
                    "unrealized_pnl": str(unrealized) if unrealized is not None else None,
                    "contract_size": str(contract_size) if contract_size is not None else None,
                    "leverage": str(leverage) if leverage is not None else None,
                    "liquidation_price": position.get("liquidation_price"),
                    "remote_position_id": position.get("position_id"),
                    "remote_observed_at": observed_at,
                    "local_mirror": True,
                    "source": "GATE_TESTNET_PRIVATE_API",
                    "protection_status": protection_status,
                    "protected": protection_status == "ACTIVE",
                    "contract_size_status": "OBSERVED_REMOTE" if contract_size is not None else "UNKNOWN_NOT_PROVIDED",
                }
                version = int(existing["position_version"] or 0) + 1 if existing else 0
                db.execute(
                    """INSERT INTO simulated_positions(
                        position_id, symbol, status, payload_json, updated_at,
                        account_id, venue, mode, position_version, protection_status,
                        legacy_unverified, provider, environment
                    ) VALUES (?, ?, 'OPEN', ?, ?, ?, 'gate', 'TESTNET', ?, ?, 0, 'gate', 'testnet')
                    ON CONFLICT(position_id) DO UPDATE SET
                        symbol=excluded.symbol, status='OPEN', payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at, account_id=excluded.account_id,
                        venue='gate', mode='TESTNET', position_version=excluded.position_version,
                        protection_status=excluded.protection_status, legacy_unverified=0,
                        provider='gate', environment='testnet'""",
                    (position_id, symbol, _json_text(payload, {}), observed_at, account_id, version, protection_status),
                )
            if current_ids:
                placeholders = ",".join("?" for _ in current_ids)
                db.execute(
                    f"""UPDATE simulated_positions
                        SET status='CLOSED', updated_at=?
                        WHERE account_id=? AND venue='gate' AND mode='TESTNET'
                          AND status IN ('OPEN','PARTIALLY_CLOSED')
                          AND json_extract(payload_json, '$.local_mirror')=1
                          AND position_id NOT IN ({placeholders})""",
                    (observed_at, account_id, *current_ids),
                )
            else:
                db.execute(
                    """UPDATE simulated_positions
                       SET status='CLOSED', updated_at=?
                       WHERE account_id=? AND venue='gate' AND mode='TESTNET'
                         AND status IN ('OPEN','PARTIALLY_CLOSED')
                         AND json_extract(payload_json, '$.local_mirror')=1""",
                    (observed_at, account_id),
                )

    def refresh(self, account_id: str, trader: Any, *, include_trades: bool = False) -> Dict[str, Any]:
        """Read Gate facts, persist them, and update the local mirror."""

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
        recorded = self._record(canonical, truth, created_at=now)
        if str(truth.get("status") or "").upper() == "AVAILABLE":
            positions = truth.get("positions") if isinstance(truth.get("positions"), list) else []
            pending = truth.get("pending_orders") if isinstance(truth.get("pending_orders"), list) else []
            self._mirror_positions(canonical, positions, pending, recorded["observed_at"])
        return recorded

    def latest(self, account_id: str, *, require_available: bool = False) -> Dict[str, Any] | None:
        canonical = canonical_account_id(self.store, str(account_id or "").strip())
        with self.store._connect() as db:
            row = db.execute(
                "SELECT * FROM gate_remote_account_snapshots WHERE account_id=? ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1",
                (canonical,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("positions_json", "pending_orders_json", "fills_json"):
            target = key[:-5]
            try:
                result[target] = json.loads(result.pop(key) or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                result[target] = []
        if require_available and str(result.get("status") or "").upper() != "AVAILABLE":
            return None
        return result


__all__ = ["GateAccountTruthService"]
