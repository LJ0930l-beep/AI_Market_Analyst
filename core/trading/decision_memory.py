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
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
    ).fetchone()
    intents = db.execute(
        """SELECT position_id, execution_result_json FROM order_intents
           WHERE account_id=? AND cycle_id=? ORDER BY created_at ASC""",
        (account_id, cycle_id),
    ).fetchall() if exists is not None else []
    for row in intents:
        if row["position_id"]:
            return str(row["position_id"])
        try:
            receipt = json.loads(row["execution_result_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        ledger_record = receipt.get("ledger_record") if isinstance(receipt, dict) else None
        if isinstance(ledger_record, dict) and ledger_record.get("position_id"):
            return str(ledger_record["position_id"])
        order_id = str(receipt.get("order_id") or "") if isinstance(receipt, dict) else ""
        if order_id and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_fills'"
        ).fetchone():
            fill = db.execute(
                """SELECT position_id FROM trade_fills WHERE account_id=? AND order_id=?
                   AND position_id IS NOT NULL AND position_id<>'' LIMIT 1""",
                (account_id, order_id),
            ).fetchone()
            if fill is not None:
                return str(fill["position_id"])
    # The current ledger migration adds cycle_id to trade_fills.  Direct
    # cycle attribution remains the strongest fallback when an execution
    # receipt was lost or never carried a position_id.
    columns = {str(item[1]) for item in db.execute("PRAGMA table_info(trade_fills)").fetchall()}
    if "cycle_id" in columns:
        fill = db.execute(
            """SELECT position_id FROM trade_fills WHERE account_id=? AND cycle_id=?
               AND position_id IS NOT NULL AND position_id<>'' LIMIT 1""",
            (account_id, cycle_id),
        ).fetchone()
        if fill is not None:
            return str(fill["position_id"])
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
        """SELECT side, quantity, price, contract_size, fee_amount, fee_source FROM trade_fills
           WHERE account_id=? AND position_id=?""",
        (account_id, position_id),
    ).fetchall()
    if not rows:
        return None
    bought = sold = 0.0
    buy_notional = sell_notional = 0.0
    fees = 0.0
    for row in rows:
        fee_source = str(row["fee_source"] or "").upper()
        if not fee_source or "UNKNOWN" in fee_source:
            # A compatibility fee of zero is not an observed trading cost.
            # Wait for an exchange fee readback before teaching the model a
            # potentially false net outcome.
            return None
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
                 AND lower(environment) NOT IN ('live', 'testnet')
               ORDER BY decision_at ASC, memory_id ASC""",
            (account_id, ENTRY_ACTIONS[0], ENTRY_ACTIONS[1]),
        ).fetchall()
        for row in rows:
            if len(settlements) >= max(1, int(limit)):
                break
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
                summary_zh=excluded.summary_zh,
                lesson_zh=COALESCE(excluded.lesson_zh, ai_decision_memory.lesson_zh),
                outcome_status=COALESCE(excluded.outcome_status, ai_decision_memory.outcome_status),
                outcome_pnl=COALESCE(excluded.outcome_pnl, ai_decision_memory.outcome_pnl),
                payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (
                memory_id, account_id, str(provider or "unknown"), str(environment or "unknown").lower(),
                session_id, cycle_id, candidate_id, symbol, str(action or "WAIT").upper(),
                str(cycle_status or "UNKNOWN").upper(), decision_time, summary, lesson_zh,
                outcome_status, outcome_pnl, json.dumps(safe_payload, ensure_ascii=False, allow_nan=False), now, now, "MODEL",
            ),
        )
        # Prompt retrieval is capped in list_decision_memory, but the durable
        # outcome ledger must retain older decisions: a trade may settle many
        # cycles later, and deleting its entry destroys the profit/lesson link.
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
    recent_rows = list_decision_memory(store, account_id, limit=20)
    verified_rows: list[dict[str, Any]] = []
    try:
        with store._connect() as db:
            required_tables = {
                "gate_episode_memory_links",
                "gate_accounting_episodes",
                "gate_episode_settlements",
                "ai_decision_memory",
            }
            existing_tables = {
                str(row[0]) for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)",
                    tuple(sorted(required_tables)),
                ).fetchall()
            }
            if existing_tables == required_tables:
                rows = db.execute(
                    """SELECT m.*,e.environment AS episode_environment,e.contract,e.entry_order_id,
                              e.entry_intent_id,e.side,e.strategy_id AS episode_strategy_id,
                              e.strategy_version AS episode_strategy_version,
                              s.settlement_json,s.settled_at,l.basis AS link_basis,l.evidence_json AS link_evidence_json
                       FROM gate_episode_memory_links l
                       JOIN gate_accounting_episodes e ON e.episode_id=l.episode_id
                       JOIN gate_episode_settlements s ON s.episode_id=e.episode_id
                       JOIN ai_decision_memory m ON m.memory_id=l.memory_id AND m.account_id=l.account_id
                       WHERE l.account_id=? AND s.status='SETTLED_FULL_COST'
                       ORDER BY s.settled_at DESC,e.episode_id LIMIT 50""",
                    (str(account_id),),
                ).fetchall()
                for raw in rows:
                    item = dict(raw)
                    try:
                        payload = json.loads(item.get("payload_json") or "{}")
                        link_evidence = json.loads(item.get("link_evidence_json") or "{}")
                        settlement = json.loads(item.get("settlement_json") or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict) or not isinstance(link_evidence, dict) or not isinstance(settlement, dict):
                        continue
                    environment = str(item.get("episode_environment") or "").upper()
                    episode_id = str(link_evidence.get("accounting_episode_id") or "")
                    order_id = str(item.get("entry_order_id") or "")
                    intent_id = str(item.get("entry_intent_id") or "")
                    try:
                        evidence_pnl = float(settlement.get("total_pnl"))
                        memory_pnl = float(item.get("outcome_pnl"))
                    except (TypeError, ValueError):
                        continue
                    payload_evidence = payload.get("outcome_evidence") if isinstance(payload.get("outcome_evidence"), dict) else {}
                    if (
                        str(item.get("link_basis") or "") != "GATE_NATIVE_SETTLEMENT"
                        or link_evidence.get("basis") != "GATE_NATIVE_TRADE_AND_POSITION_CLOSE_LIFECYCLE"
                        or not episode_id
                        or str(settlement.get("accounting_episode_id") or "") != episode_id
                        or str(settlement.get("account_id") or "") != str(account_id)
                        or str(settlement.get("environment") or "").upper() != environment
                        or str(settlement.get("entry_order_id") or "") != order_id
                        or str(settlement.get("entry_intent_id") or "") != intent_id
                        or str(item.get("environment") or "").lower() != environment.lower()
                        or str(payload_evidence.get("accounting_episode_id") or "") != episode_id
                        or str(item.get("outcome_status") or "").upper() not in {"WIN", "LOSS", "FLAT"}
                        or abs(evidence_pnl - memory_pnl) > 1e-10
                    ):
                        continue
                    selected_evidence = {
                        "basis": "GATE_NATIVE_TRADE_AND_POSITION_CLOSE_LIFECYCLE",
                        "accounting_episode_id": episode_id,
                        "entry_intent_id": intent_id,
                        "entry_order_id": order_id,
                        "environment": environment,
                        "contract": str(item.get("contract") or ""),
                        "side": str(item.get("side") or "").upper(),
                        "total_pnl": settlement.get("total_pnl"),
                        "settlement_currency": settlement.get("settlement_currency"),
                        "fee_status": settlement.get("fee_status"),
                        "fee_effect": settlement.get("fee_effect"),
                        "funding_status": settlement.get("funding_status"),
                        "funding_effect": settlement.get("funding_effect"),
                        "exit_trade_ids": settlement.get("exit_trade_ids") if isinstance(settlement.get("exit_trade_ids"), list) else [],
                        "position_close_evidence_id": settlement.get("position_close_evidence_id"),
                        "closed_at_ms": settlement.get("closed_at_ms"),
                        "pnl_source": settlement.get("pnl_source"),
                    }
                    verified_rows.append({
                        "memory_id": str(item.get("memory_id") or ""),
                        "cycle_id": item.get("cycle_id"),
                        "decision_at": item.get("decision_at"),
                        "action": item.get("action"),
                        "status": item.get("cycle_status"),
                        "symbol": item.get("symbol"),
                        "summary_zh": item.get("summary_zh"),
                        "lesson_zh": item.get("lesson_zh"),
                        "outcome_status": item.get("outcome_status"),
                        "outcome_pnl": item.get("outcome_pnl"),
                        "environment": item.get("environment"),
                        "strategy_template_id": payload.get("strategy_template_id"),
                        "strategy_id": item.get("episode_strategy_id") or payload.get("strategy_id"),
                        "strategy_version": item.get("episode_strategy_version") or payload.get("strategy_version"),
                        "memory_role": "VERIFIED_SETTLED_LESSON",
                        "outcome_evidence": selected_evidence,
                        "_closed_at_ms": settlement.get("closed_at_ms"),
                        "_settled_at": item.get("settled_at"),
                    })
    except Exception:
        # Recent decisions remain usable if the optional settlement ledger is
        # unavailable; verified historical lessons are simply not claimed.
        verified_rows = []

    def project(item: dict[str, Any]) -> dict[str, Any]:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        row = {
            "decision_at": item.get("decision_at"),
            "action": item.get("action"),
            "status": item.get("cycle_status") or item.get("status"),
            "symbol": item.get("symbol"),
            "summary_zh": item.get("summary_zh"),
            "lesson_zh": item.get("lesson_zh"),
            "outcome_status": item.get("outcome_status"),
            "outcome_pnl": item.get("outcome_pnl"),
            "strategy_template_id": payload.get("strategy_template_id"),
        }
        evidence = payload.get("outcome_evidence")
        if isinstance(evidence, dict):
            row["outcome_evidence"] = evidence
        return {key: value for key, value in row.items() if value is not None}

    output_by_identity: dict[str, dict[str, Any]] = {}
    recent_identities: list[str] = []
    for item in recent_rows:
        projected = project(item)
        identity = str(item.get("memory_id") or item.get("cycle_id") or "")
        if identity:
            output_by_identity[identity] = projected
            recent_identities.append(identity)
    for item in sorted(
        verified_rows,
        key=lambda row: (
            int(row.get("_closed_at_ms") or 0),
            str(row.get("_settled_at") or ""),
        ),
        reverse=True,
    )[:5]:
        projected = {key: value for key, value in item.items() if not key.startswith("_")}
        identity = str(projected.get("memory_id") or projected.get("cycle_id") or "")
        if identity:
            projected["memory_role"] = "VERIFIED_SETTLED_LESSON"
            output_by_identity[identity] = projected

    output = [output_by_identity[key] for key in recent_identities if key in output_by_identity]
    output.extend(
        item for key, item in output_by_identity.items()
        if key not in set(recent_identities)
    )
    return output


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
