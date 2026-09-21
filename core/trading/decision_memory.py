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

# Only a decision that actually opened exposure can have a market outcome.
ENTRY_ACTIONS = ("OPEN_LONG", "OPEN_SHORT")
# A settled position whose net result sits inside this band is booked FLAT.
# The band exists so fee and rounding noise never reads as a win or a loss.
FLAT_BAND_USDT = 0.01
# Closed quantity must match opened quantity to this relative tolerance before
# an outcome is considered final.  A partially closed position is still open.
QUANTITY_MATCH_TOLERANCE = 0.005


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


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _position_id_for_cycle(db: Any, account_id: str, cycle_id: str) -> str | None:
    """Find the position opened by one AI cycle.

    ``order_intents`` carries the position id assigned at placement time;
    ``trade_fills`` carries it once the exchange acknowledges the fill.  Both
    are checked because a rejected placement writes neither, and a LIMIT entry
    can fill in a later cycle than the one that decided it.
    """
    for table in ("order_intents", "trade_fills"):
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            continue
        row = db.execute(
            f"""SELECT position_id FROM {table}
                WHERE account_id=? AND cycle_id=?
                  AND position_id IS NOT NULL AND position_id<>''
                LIMIT 1""",
            (account_id, cycle_id),
        ).fetchone()
        if row is not None and row["position_id"]:
            return str(row["position_id"])
    return None


def _position_settlement(db: Any, account_id: str, position_id: str) -> dict[str, Any] | None:
    """Net realised result of one position, or ``None`` while it is still open.

    ``gross = (sell notional) - (buy notional)`` is direction agnostic: a long
    buys then sells, a short sells then buys, so the same expression yields the
    correct sign for both.  Fees are subtracted to give the net figure the
    prompt should quote.
    """
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_fills'"
    ).fetchone()
    if exists is None:
        return None
    rows = db.execute(
        """SELECT side, quantity, price, contract_size, fee_amount FROM trade_fills
           WHERE account_id=? AND position_id=?""",
        (account_id, position_id),
    ).fetchall()
    if not rows:
        return None
    bought = sold = 0.0
    buy_notional = sell_notional = 0.0
    fees = 0.0
    for row in rows:
        quantity = _number(row["quantity"]) or 0.0
        price = _number(row["price"]) or 0.0
        contract = _number(row["contract_size"]) or 1.0
        fees += abs(_number(row["fee_amount"]) or 0.0)
        notional = quantity * price * contract
        side = str(row["side"] or "").upper()
        if side in {"BUY", "LONG"}:
            bought += quantity
            buy_notional += notional
        elif side in {"SELL", "SHORT"}:
            sold += quantity
            sell_notional += notional
    if bought <= 0 or sold <= 0:
        return None
    if abs(bought - sold) > max(bought, sold) * QUANTITY_MATCH_TOLERANCE:
        # Still open, or only partially closed: the result is not final yet.
        return None
    gross = sell_notional - buy_notional
    return {
        "gross_realized": round(gross, 8),
        "fees": round(fees, 8),
        "net_realized": round(gross - fees, 8),
        "matched_quantity": round(min(bought, sold), 8),
        "fill_count": len(rows),
    }


def reconcile_decision_outcomes(store: Any, account_id: str, *, limit: int = 20) -> dict[str, Any]:
    """Backfill the realised outcome of entry decisions that have since closed.

    The strategy book tells the model what it *intends* to do; this is what
    tells it what *happened*.  Before this ran, ``memory_for_prompt`` returned a
    permanently null ``outcome_status``/``outcome_pnl``, so the model could see
    its own past decisions but never whether they made or lost money.

    Only entry decisions with no recorded outcome are considered.  A decision
    whose position is still open, or whose placement never produced a position,
    is left untouched — this function never invents a number.
    """
    considered = 0
    pending = 0
    resolved: list[dict[str, Any]] = []
    settlements: list[dict[str, Any]] = []
    with store._connect() as db:
        ensure_institutional_trader_schema(db)
        rows = db.execute(
            """SELECT memory_id, cycle_id, symbol, action, decision_at
               FROM ai_decision_memory
               WHERE account_id=? AND outcome_status IS NULL
                 AND action IN (?, ?)
               ORDER BY decision_at ASC, memory_id ASC LIMIT ?""",
            (account_id, ENTRY_ACTIONS[0], ENTRY_ACTIONS[1], max(1, min(int(limit), 20))),
        ).fetchall()
        for row in rows:
            considered += 1
            position_id = _position_id_for_cycle(db, account_id, str(row["cycle_id"]))
            if not position_id:
                pending += 1
                continue
            settlement = _position_settlement(db, account_id, position_id)
            if settlement is None:
                pending += 1
                continue
            net = float(settlement["net_realized"])
            status = "WIN" if net > FLAT_BAND_USDT else ("LOSS" if net < -FLAT_BAND_USDT else "FLAT")
            symbol = str(row["symbol"] or "UNKNOWN")
            settlements.append({
                "memory_id": str(row["memory_id"]),
                "symbol": symbol,
                "position_id": position_id,
                "outcome_status": status,
                "outcome_pnl": net,
                "settlement": settlement,
                "lesson_zh": (
                    f"{symbol} 已平仓：净盈亏 {net:+.2f} USDT"
                    f"（毛 {settlement['gross_realized']:+.2f} / 费用 {settlement['fees']:.2f}）。"
                ),
            })
    for item in settlements:
        written = update_memory_outcome(
            store,
            item["memory_id"],
            outcome_status=item["outcome_status"],
            outcome_pnl=item["outcome_pnl"],
            lesson_zh=item["lesson_zh"],
            evidence={
                "basis": "LOCAL_FILL_MIRROR_NET_OF_FEES",
                "position_id": item["position_id"],
                "symbol": item["symbol"],
                **item["settlement"],
            },
        )
        if written:
            resolved.append({
                "memory_id": item["memory_id"],
                "symbol": item["symbol"],
                "position_id": item["position_id"],
                "outcome_status": item["outcome_status"],
                "outcome_pnl": item["outcome_pnl"],
            })
    return {"considered": considered, "pending": pending, "resolved": resolved}


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


def update_memory_outcome(
    store: Any,
    memory_id: str,
    *,
    outcome_status: str,
    outcome_pnl: float | None = None,
    lesson_zh: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> bool:
    """Record the observed result of a decision.

    ``evidence`` is merged into the existing ``payload_json`` under
    ``outcome_evidence`` so the arithmetic behind ``outcome_pnl`` stays
    auditable without widening the table.  Callers that only know the verdict
    can omit it.
    """

    with store._connect() as db:
        if evidence:
            row = db.execute(
                "SELECT payload_json FROM ai_decision_memory WHERE memory_id=?", (memory_id,)
            ).fetchone()
            if row is not None:
                try:
                    merged = json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    merged = {}
                if not isinstance(merged, dict):
                    merged = {}
                merged["outcome_evidence"] = dict(evidence)
                db.execute(
                    "UPDATE ai_decision_memory SET payload_json=? WHERE memory_id=?",
                    (json.dumps(merged, ensure_ascii=False, allow_nan=False), memory_id),
                )
        result = db.execute(
            "UPDATE ai_decision_memory SET outcome_status=?, outcome_pnl=?, lesson_zh=?, updated_at=? WHERE memory_id=?",
            (str(outcome_status or "UNKNOWN").upper(), outcome_pnl, lesson_zh, _iso(), memory_id),
        )
        return result.rowcount == 1


__all__ = [
    "ENTRY_ACTIONS",
    "list_decision_memory",
    "memory_for_prompt",
    "reconcile_decision_outcomes",
    "record_decision_memory",
    "update_memory_outcome",
]
