"""Executable account-scoped dynamic risk locks for autonomous entries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any
from zoneinfo import ZoneInfo


ENTRY_ACTIONS = {"OPEN_LONG", "OPEN_SHORT"}


def _utc(value: datetime | None = None) -> datetime:
    point = value or datetime.now(timezone.utc)
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _realized_exit_rows(store: Any, account_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    with store._connect() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_fills'").fetchone()
        if table is None:
            return []
        rows = db.execute(
            """SELECT fill_id, event_at, created_at, payload_json FROM trade_fills
               WHERE account_id=? AND status IN ('FILLED','CLOSED','EXECUTED')
               ORDER BY COALESCE(event_at,created_at) DESC, rowid DESC LIMIT ?""",
            (account_id, max(2, min(int(limit), 100))),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            continue
        reduce_only = payload.get("reduce_only", payload.get("reduceOnly"))
        role = str(payload.get("economic_role") or payload.get("role") or "").upper()
        if not bool(reduce_only) and role not in {"EXIT", "CLOSE", "REDUCE"}:
            continue
        pnl = _finite(payload.get("realized_pnl", payload.get("realizedPnl", payload.get("pnl"))))
        if pnl is None:
            continue
        output.append({"fill_id": row["fill_id"], "pnl": pnl, "at": row["event_at"] or row["created_at"]})
    return output


def evaluate_dynamic_risk(store: Any, account_id: str, execution: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    observed = _utc(now)
    reasons: list[str] = []
    until: datetime | None = None
    evidence: dict[str, Any] = {}

    if bool(execution.get("consecutive_loss_lock_enabled", True)):
        exits = _realized_exit_rows(store, account_id)
        evidence["recent_authoritative_exits"] = exits[:2]
        if len(exits) >= 2 and exits[0]["pnl"] < 0 and exits[1]["pnl"] < 0:
            last_exit = _time(exits[0]["at"])
            if last_exit is not None:
                loss_until = last_exit + timedelta(hours=2)
                if observed < loss_until:
                    reasons.append("CONSECUTIVE_LOSS_COOLDOWN")
                    until = loss_until

    if bool(execution.get("us_open_defense_enabled", True)):
        local = observed.astimezone(ZoneInfo("America/New_York"))
        # Weekday 09:15 through the regular-equity open at 09:30 New York time.
        in_window = local.weekday() < 5 and (local.hour, local.minute) >= (9, 15) and (local.hour, local.minute) < (9, 30)
        evidence["new_york_time"] = local.isoformat()
        if in_window:
            reasons.append("US_OPEN_DEFENSE")
            close_local = local.replace(hour=9, minute=30, second=0, microsecond=0)
            close_utc = close_local.astimezone(timezone.utc)
            until = max(filter(None, (until, close_utc)), default=close_utc)

    return {
        "status": "BLOCKED" if reasons else "READY",
        "entry_allowed": not reasons,
        "reasons": reasons,
        "blocked_until": until.isoformat() if until else None,
        "evaluated_at": observed.isoformat(),
        "atr_adaptive_sizing": {
            "enabled": bool(execution.get("atr_adaptive_sizing", True)),
            "status": "ENFORCED_BY_STOP_DISTANCE_RISK_SIZING" if execution.get("atr_adaptive_sizing", True) else "CORE_HARD_RISK_ONLY",
        },
        "evidence": evidence,
    }


__all__ = ["ENTRY_ACTIONS", "evaluate_dynamic_risk"]
