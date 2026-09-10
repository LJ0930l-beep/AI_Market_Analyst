"""Account-scoped, auditable AI decision memory.

Only compact decision summaries and observed outcomes are persisted.  The
model's hidden chain-of-thought is never stored or returned.  Rollover is
intentionally exact: the 21st row removes the oldest ten rows and leaves the
newest eleven; later rows grow back to a hard maximum of twenty.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid
from typing import Any

from .institutional_schema import ensure_institutional_trader_schema


def _iso(value: Any = None) -> str:
    point = value if isinstance(value, datetime) else datetime.now(timezone.utc)
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc).isoformat()


def _safe_summary(action: str, status: str, reason: str, symbol: str | None = None) -> str:
    # Reasons come from the model or provider boundary.  Keep the UI fact
    # structured and bounded; no prompt or internal reasoning transcript is
    # copied into durable memory.
    clean_reason = " ".join(str(reason or "").split())[:320]
    parts = [f"动作={str(action or 'WAIT').upper()}", f"结果={str(status or 'UNKNOWN').upper()}"]
    if symbol:
        parts.append(f"标的={str(symbol).upper()[:80]}")
    if clean_reason:
        parts.append(f"原因={clean_reason}")
    return "；".join(parts)


def record_decision_memory(
    store: Any,
    *,
    account_id: str,
    provider: str,
    environment: str,
    cycle_id: str,
    session_id: str | None,
    candidate_id: str | None,
    symbol: str | None,
    action: str,
    cycle_status: str,
    decision_at: Any,
    reason: str,
    payload: dict[str, Any] | None = None,
    lesson_zh: str | None = None,
    outcome_status: str | None = None,
    outcome_pnl: float | None = None,
    decision_origin: str = "MODEL",
) -> dict[str, Any]:
    """Upsert one cycle row and apply the exact account-local rollover."""

    if str(decision_origin or "MODEL").upper() != "MODEL":
        # Operational/precheck states are diagnostics, not model decisions.
        # Refusing them here keeps every memory row attributable to a real,
        # schema-valid model output even if a caller bypasses the coordinator.
        return {"skipped": True, "decision_origin": str(decision_origin or "UNKNOWN").upper()}

    now = _iso()
    decision_time = _iso(decision_at)
    memory_id = f"memory_{uuid.uuid4().hex[:16]}"
    summary = _safe_summary(action, cycle_status, reason, symbol)
    safe_payload = dict(payload or {})
    # Explicitly discard fields that could carry raw prompts or model traces.
    for key in ("prompt", "messages", "raw_model_response", "chain_of_thought", "cot", "secret", "api_secret"):
        safe_payload.pop(key, None)
    with store._connect() as db:
        ensure_institutional_trader_schema(db)
        existing = db.execute(
            "SELECT memory_id FROM ai_decision_memory WHERE account_id=? AND cycle_id=?",
            (account_id, cycle_id),
        ).fetchone()
        if existing:
            memory_id = str(existing["memory_id"])
        db.execute(
            """INSERT INTO ai_decision_memory(
                memory_id, account_id, provider, environment, session_id,
                cycle_id, candidate_id, symbol, action, cycle_status,
                decision_at, summary_zh, lesson_zh, outcome_status, outcome_pnl,
                payload_json, created_at, updated_at, decision_origin
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, cycle_id) DO UPDATE SET
                provider=excluded.provider, environment=excluded.environment,
                session_id=excluded.session_id, candidate_id=excluded.candidate_id,
                symbol=excluded.symbol, action=excluded.action,
                cycle_status=excluded.cycle_status, decision_at=excluded.decision_at,
                summary_zh=excluded.summary_zh, lesson_zh=excluded.lesson_zh,
                outcome_status=excluded.outcome_status, outcome_pnl=excluded.outcome_pnl,
                payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (
                memory_id, account_id, str(provider or "unknown"), str(environment or "unknown").lower(),
                session_id, cycle_id, candidate_id, symbol, str(action or "WAIT").upper(),
                str(cycle_status or "UNKNOWN").upper(), decision_time, summary, lesson_zh,
                outcome_status, outcome_pnl, json.dumps(safe_payload, ensure_ascii=False, allow_nan=False), now, now, "MODEL",
            ),
        )
        count = int(db.execute("SELECT COUNT(*) FROM ai_decision_memory WHERE account_id=?", (account_id,)).fetchone()[0])
        if count == 21:
            db.execute(
                """DELETE FROM ai_decision_memory
                   WHERE account_id=? AND memory_id IN (
                       SELECT memory_id FROM ai_decision_memory
                       WHERE account_id=? ORDER BY decision_at ASC, memory_id ASC LIMIT 10
                   )""",
                (account_id, account_id),
            )
        elif count > 21:
            # Repair an overfull legacy account without deleting authoritative
            # execution rows.  Keep the same newest-20 invariant.
            db.execute(
                """DELETE FROM ai_decision_memory
                   WHERE account_id=? AND memory_id NOT IN (
                       SELECT memory_id FROM ai_decision_memory
                       WHERE account_id=? ORDER BY decision_at DESC, memory_id DESC LIMIT 20
                   )""",
                (account_id, account_id),
            )
        row = db.execute("SELECT * FROM ai_decision_memory WHERE memory_id=?", (memory_id,)).fetchone()
    return dict(row) if row is not None else {"memory_id": memory_id, "account_id": account_id, "cycle_id": cycle_id}


def list_decision_memory(store: Any, account_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), 20))
    with store._connect() as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_decision_memory'"
        ).fetchone()
        if table is None:
            return []
        rows = db.execute(
            """SELECT * FROM ai_decision_memory
               WHERE account_id=? ORDER BY decision_at DESC, memory_id DESC LIMIT ?""",
            (account_id, bounded),
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["payload"] = {}
            item.pop("payload_json", None)
        output.append(item)
    return output


def memory_for_prompt(store: Any, account_id: str) -> list[dict[str, Any]]:
    return [
        {
            "decision_at": item.get("decision_at"),
            "action": item.get("action"),
            "status": item.get("cycle_status"),
            "symbol": item.get("symbol"),
            "summary_zh": item.get("summary_zh"),
            "outcome_status": item.get("outcome_status"),
            "outcome_pnl": item.get("outcome_pnl"),
        }
        for item in list_decision_memory(store, account_id, limit=20)
    ]


def update_memory_outcome(store: Any, memory_id: str, *, outcome_status: str, outcome_pnl: float | None = None, lesson_zh: str | None = None) -> bool:
    with store._connect() as db:
        result = db.execute(
            "UPDATE ai_decision_memory SET outcome_status=?, outcome_pnl=?, lesson_zh=?, updated_at=? WHERE memory_id=?",
            (str(outcome_status or "UNKNOWN").upper(), outcome_pnl, lesson_zh, _iso(), memory_id),
        )
        return result.rowcount == 1


__all__ = ["list_decision_memory", "memory_for_prompt", "record_decision_memory", "update_memory_outcome"]
