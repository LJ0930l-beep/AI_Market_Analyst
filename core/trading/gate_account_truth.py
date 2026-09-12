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


CAPITAL_BASIS_SOURCE = "gate_testnet_remote_account_truth"
CAPITAL_BASIS_BASELINE = "GATE_TESTNET_FIRST_AVAILABLE_REMOTE_SNAPSHOT"
CAPITAL_BASIS_STALE_AFTER_SECONDS = 300.0


def _finite(value: Any) -> Optional[float]:
    """Return a finite float, or ``None``.  A bad value is never zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _unavailable_capital_basis(
    error_code: str,
    *,
    snapshot_count: int = 0,
    observed_at: Any = None,
    provider_source: Any = None,
    message_zh: Any = None,
) -> Dict[str, Any]:
    return {
        "status": "UNAVAILABLE",
        "source": None,
        "provider_source": provider_source,
        "observed_at": observed_at,
        "age_seconds": None,
        "stale": False,
        "current_equity": None,
        "available_margin": None,
        "used_margin": None,
        "unrealized_pnl": None,
        "realized_pnl": None,
        "baseline_equity": None,
        "baseline_observed_at": None,
        "baseline_basis": None,
        "net_pnl": None,
        "roi_pct": None,
        "cumulative_fees": None,
        "fee_evidence": "UNKNOWN",
        "max_drawdown_pct": None,
        "equity_series": [],
        "snapshot_count": snapshot_count,
        "error_code": error_code,
        "message_zh": message_zh,
    }


def resolve_remote_capital_basis(
    store: Any,
    account_id: str,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Project the observed Gate capital basis for one managed account.

    A managed Gate account must never publish the local
    ``accounts.initial_deposit`` compatibility seed as equity or starting
    capital: that value is a local artifact (for example ``10000``) and has no
    exchange meaning.  This function therefore reads only persisted remote
    observations and returns an explicit ``UNAVAILABLE`` when none exist, so a
    caller can render "尚未同步" instead of a fabricated balance.

    The baseline is the earliest remote snapshot whose equity is a finite,
    non-negative number.  Earlier rows are skipped rather than plotted: an
    older projection could pair a valid margin with a mangled equity.
    """

    canonical = canonical_account_id(store, str(account_id or "").strip()) or str(account_id or "").strip()
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    with store._connect() as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gate_remote_account_snapshots'"
        ).fetchone()
        if not exists:
            return _unavailable_capital_basis("REMOTE_ACCOUNT_TRUTH_NOT_OBSERVED")
        rows = [
            dict(row)
            for row in db.execute(
                """SELECT observed_at, status, equity, available_margin, used_margin,
                          unrealized_pnl, realized_pnl, source, error_code, message_zh, fills_json
                   FROM gate_remote_account_snapshots
                   WHERE account_id=?
                   ORDER BY observed_at ASC, created_at ASC, snapshot_id ASC""",
                (canonical,),
            ).fetchall()
        ]
    if not rows:
        return _unavailable_capital_basis("REMOTE_ACCOUNT_TRUTH_NOT_OBSERVED")

    available = [row for row in rows if str(row.get("status") or "").upper() == "AVAILABLE"]
    if not available:
        latest = rows[-1]
        return _unavailable_capital_basis(
            str(latest.get("error_code") or "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE"),
            snapshot_count=len(rows),
            observed_at=latest.get("observed_at"),
            provider_source=latest.get("source"),
            message_zh=latest.get("message_zh"),
        )

    plausible: list[tuple[Dict[str, Any], float]] = []
    for row in available:
        equity = _finite(row.get("equity"))
        if equity is None or equity < 0:
            continue
        plausible.append((row, equity))
    if not plausible:
        latest = available[-1]
        return _unavailable_capital_basis(
            "REMOTE_ACCOUNT_EQUITY_NOT_PLAUSIBLE",
            snapshot_count=len(rows),
            observed_at=latest.get("observed_at"),
            provider_source=latest.get("source"),
            message_zh="远端快照未包含可信权益值，未使用本地种子存款替代。",
        )

    series: list[Dict[str, Any]] = []
    peak: Optional[float] = None
    max_drawdown = 0.0
    for row, equity in plausible:
        peak = equity if peak is None or equity > peak else peak
        drawdown = ((peak - equity) / peak * 100.0) if peak and peak > 0 else None
        if drawdown is not None and drawdown > max_drawdown:
            max_drawdown = drawdown
        series.append({"time": _iso(row.get("observed_at")), "equity_usdt": equity, "drawdown_pct": drawdown})

    baseline_row, baseline_equity = plausible[0]
    current_row, current_equity = plausible[-1]
    observed_at = _iso(current_row.get("observed_at"))
    age = (moment - datetime.fromisoformat(observed_at.replace("Z", "+00:00"))).total_seconds()

    cumulative_fees: Optional[float] = None
    fee_evidence = "UNKNOWN"
    for row in reversed(rows):
        try:
            fills = json.loads(row.get("fills_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(fills, list) or not fills:
            continue
        observed_fees = [
            _finite(item.get("fee_cost"))
            for item in fills
            if isinstance(item, dict)
        ]
        observed_fees = [value for value in observed_fees if value is not None]
        if not observed_fees:
            fee_evidence = "OBSERVED_REMOTE_WITHOUT_FEE"
            break
        cumulative_fees = sum(observed_fees)
        fee_evidence = "OBSERVED_REMOTE_FILLS"
        break

    net_pnl = current_equity - baseline_equity
    roi = (net_pnl / baseline_equity * 100.0) if baseline_equity > 0 else None
    return {
        "status": "AVAILABLE",
        "source": CAPITAL_BASIS_SOURCE,
        "provider_source": current_row.get("source"),
        "observed_at": observed_at,
        "age_seconds": age,
        "stale": age > CAPITAL_BASIS_STALE_AFTER_SECONDS,
        "current_equity": current_equity,
        "available_margin": _finite(current_row.get("available_margin")),
        "used_margin": _finite(current_row.get("used_margin")),
        "unrealized_pnl": _finite(current_row.get("unrealized_pnl")),
        "realized_pnl": _finite(current_row.get("realized_pnl")),
        "baseline_equity": baseline_equity,
        "baseline_observed_at": _iso(baseline_row.get("observed_at")),
        "baseline_basis": CAPITAL_BASIS_BASELINE,
        "net_pnl": net_pnl,
        "roi_pct": roi,
        "cumulative_fees": cumulative_fees,
        "fee_evidence": fee_evidence,
        "max_drawdown_pct": max_drawdown if series else None,
        "equity_series": series,
        "snapshot_count": len(rows),
        "error_code": None,
        "message_zh": None,
    }


__all__ = ["GateAccountTruthService", "resolve_remote_capital_basis"]
