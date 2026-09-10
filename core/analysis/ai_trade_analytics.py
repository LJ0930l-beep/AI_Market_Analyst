"""AI Trading Analytics & Style Profiler.

Tracks simulated positions from an initial equity of 1,000 USDT with dynamic AI-selected
leverage (5x - 100x), evaluates strategy metrics, and profiles trader behavioral DNA.
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import math
from typing import Any

from ..trading.account_scope import resolve_account_scope


INITIAL_SIMULATED_CAPITAL = 1000.0
MAX_LEVERAGE = 100.0
MIN_LEVERAGE = 5.0

_KNOWN_EXECUTION_MODES = {"RESEARCH", "PAPER", "TESTNET", "LIVE"}
_EXECUTION_ATTRIBUTIONS = {
    "STRATEGY_DRIVEN",
    "AI_FILTERED",
    "AI_LED",
    "MANUAL",
    "LEGACY_UNCONFIRMED",
}
_CONCRETE_ORDER_STATUSES = {"FILLED", "PARTIALLY_FILLED"}
_NON_FILLED_ORDER_STATUSES = {
    "CREATED",
    "RISK_APPROVED",
    "SUBMITTING",
    "ACKNOWLEDGED",
    "CANCEL_PENDING",
    "CANCELED",
    "EXPIRED",
    "REJECTED",
    "UNKNOWN",
}


def _projection_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if not isinstance(value, dict) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _projection_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    elif value:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if point.tzinfo is None:
        return point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _projection_iso(value: Any) -> str | None:
    point = _projection_time(value)
    return point.isoformat() if point else (str(value) if value else None)


def _projection_number(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _projection_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _projection_table_exists(db: Any, table: str) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _projection_path(*values: Any) -> str | None:
    """Return an explicit attribution, never infer AI ownership from PnL."""
    for value in values:
        if value is None:
            continue
        candidate = str(value).strip().upper().replace("-", "_")
        if candidate in {"AI_FILTER", "AI_FILTERED", "FILTERED_BY_AI"}:
            return "AI_FILTERED"
        if candidate in {"AI_LED", "AUTONOMOUS", "AUTONOMOUS_AI"}:
            return "AI_LED"
        if candidate in {"STRATEGY_DRIVEN", "STRATEGY", "QUANT", "RULE"}:
            return "STRATEGY_DRIVEN"
        if candidate in {"MANUAL", "TRADER", "USER"}:
            return "MANUAL"
    return None


def _projection_scope_status(
    *,
    account_id: str,
    record_account: Any,
    record_mode: Any,
    record_venue: Any,
    expected_mode: str | None,
    expected_venue: str | None,
    legacy: bool = False,
) -> str | None:
    """Validate account/environment without assigning incomplete legacy rows."""
    if str(record_account or "") != account_id:
        return None
    mode = str(record_mode or "").strip().upper()
    venue = str(record_venue or "").strip().lower()
    if expected_mode and mode and mode != expected_mode:
        return None
    if expected_venue and venue and venue != expected_venue:
        return None
    if legacy or not mode or not venue:
        return "LEGACY_UNCONFIRMED"
    return "SCOPED"


def build_execution_ledger_projection(
    store: Any,
    account_id: str | None,
    *,
    venue: str | None = None,
    mode: str | None = None,
    from_at: datetime | str | None = None,
    to_at: datetime | str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    """Build a read-only, account-scoped view over authoritative execution facts.

    This intentionally does not use the old ``agent_trade_decisions`` table as
    the source of truth.  Decisions are metadata only; concrete fills come
    from ``trade_fills`` and order state comes from ``order_intents``.  The
    helper performs no writes and never turns an order acknowledgement into a
    fill.
    """
    if page < 1 or page_size < 1 or page_size > 500:
        raise ValueError("EXECUTION_PROJECTION_PAGINATION_INVALID")
    start = _projection_time(from_at)
    end = _projection_time(to_at)
    if from_at is not None and start is None or to_at is not None and end is None:
        raise ValueError("EXECUTION_PROJECTION_TIME_INVALID")
    if start and end and end < start:
        raise ValueError("EXECUTION_PROJECTION_TIME_RANGE_INVALID")

    requested_mode = str(mode).strip().upper() if mode else None
    requested_venue = str(venue).strip().lower() if venue else None
    if requested_mode and requested_mode not in _KNOWN_EXECUTION_MODES:
        raise ValueError("EXECUTION_PROJECTION_MODE_INVALID")
    if requested_venue is not None and not requested_venue:
        raise ValueError("EXECUTION_PROJECTION_VENUE_INVALID")

    account_id = str(account_id).strip() if account_id else None
    resolved_scope = resolve_account_scope(store, account_id) if account_id else None
    with store._connect() as db:
        account_row = None
        account_count = 0
        if _projection_table_exists(db, "accounts"):
            account_count = int(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            if account_id:
                account_row = db.execute(
                    "SELECT account_id, mode, config_json, initial_deposit FROM accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
            elif account_count:
                raise ValueError("ACCOUNT_REQUIRED")
        if account_row is None and account_count:
            raise ValueError(f"ACCOUNT_NOT_FOUND: Account '{account_id}' is not registered.")

        expected_mode = str((resolved_scope or {}).get("mode") or account_row["mode"]).upper() if account_row else requested_mode
        account_config = _projection_json(account_row["config_json"]) if account_row else {}
        expected_venue = str(
            (resolved_scope or {}).get("venue")
            or account_config.get("venue")
            or ("simulated" if expected_mode == "PAPER" else "gate")
        ).strip().lower() if expected_mode else requested_venue
        if requested_mode and expected_mode and requested_mode != expected_mode:
            raise ValueError("ACCOUNT_MODE_SCOPE_MISMATCH")
        if requested_venue and expected_venue and requested_venue != expected_venue:
            raise ValueError("ACCOUNT_VENUE_SCOPE_MISMATCH")
        scope_mode = expected_mode or requested_mode
        scope_venue = expected_venue or requested_venue
        scope_status = "SCOPED" if account_row and account_id else "EMPTY_UNREGISTERED_SCOPE"

        def rows_for(table: str, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            if not _projection_table_exists(db, table):
                return []
            return [dict(row) for row in db.execute(query, params).fetchall()]

        fill_rows = rows_for(
            "trade_fills",
            "SELECT rowid AS _projection_rowid, * FROM trade_fills WHERE account_id=? ORDER BY COALESCE(event_at, created_at), rowid",
            (account_id or "",),
        ) if account_id else []
        order_rows = rows_for(
            "order_intents",
            "SELECT * FROM order_intents WHERE account_id=? ORDER BY created_at, intent_id",
            (account_id or "",),
        ) if account_id else []
        plan_rows = rows_for(
            "trader_trade_plans",
            "SELECT * FROM trader_trade_plans WHERE account_id=? ORDER BY created_at, plan_id",
            (account_id or "",),
        ) if account_id else []
        position_rows = rows_for(
            "simulated_positions",
            "SELECT * FROM simulated_positions ORDER BY updated_at, position_id",
        )
        decision_rows = rows_for(
            "agent_trade_decisions",
            "SELECT decision_id, symbol, status, payload_json, created_at FROM agent_trade_decisions ORDER BY created_at, decision_id",
        )
        cycle_rows = rows_for(
            "ai_led_cycles",
            "SELECT * FROM ai_led_cycles WHERE account_id=? ORDER BY created_at, cycle_id",
            (account_id or "",),
        ) if account_id else []

    def in_window(value: Any) -> bool:
        point = _projection_time(value)
        if start and (point is None or point < start):
            return False
        if end and (point is None or point > end):
            return False
        return True

    # Preserve explicit account scope on positions even when a legacy row has
    # the account only inside its payload.  It remains visibly unverified.
    positions: list[dict[str, Any]] = []
    unscoped_legacy: list[dict[str, Any]] = []
    position_by_id: dict[str, dict[str, Any]] = {}
    for row in position_rows:
        payload = _projection_json(row.get("payload_json"))
        raw_account = row.get("account_id") or payload.get("account_id")
        raw_mode = row.get("mode") or payload.get("mode")
        raw_venue = row.get("venue") or payload.get("venue")
        is_legacy = _projection_bool(row.get("legacy_unverified"))
        if is_legacy and not raw_account:
            unscoped_legacy.append({
                "position_id": row.get("position_id") or payload.get("position_id"),
                "symbol": row.get("symbol") or payload.get("symbol"),
                "reason": "LEGACY_POSITION_ACCOUNT_UNVERIFIED",
            })
            continue
        scope_for_row = _projection_scope_status(
            account_id=account_id or "",
            record_account=raw_account,
            record_mode=raw_mode,
            record_venue=raw_venue,
            expected_mode=scope_mode,
            expected_venue=scope_venue,
            legacy=is_legacy,
        ) if account_id else None
        if scope_for_row is None:
            continue
        position_id = str(row.get("position_id") or payload.get("position_id") or "")
        if not position_id:
            continue
        item = {
            "position_id": position_id,
            "account_id": raw_account,
            "venue": str(raw_venue).lower() if raw_venue else "UNKNOWN",
            "mode": str(raw_mode).upper() if raw_mode else "UNKNOWN",
            "symbol": row.get("symbol") or payload.get("symbol"),
            "side": payload.get("side") or "UNKNOWN",
            "status": row.get("status") or payload.get("status") or "UNKNOWN",
            "position_version": row.get("position_version"),
            "protection_status": row.get("protection_status") or payload.get("protection_status") or "UNKNOWN",
            "remaining_contracts": payload.get("remaining_contracts", payload.get("quantity")),
            "quantity": payload.get("quantity", payload.get("contracts")),
            "entry_price": payload.get("entry_price", payload.get("entry")),
            "stop_loss": payload.get("stop_loss", payload.get("stop")),
            "realized_pnl": payload.get("realized_pnl", 0.0),
            "trade_plan_id": payload.get("trade_plan_id"),
            "order_id": payload.get("order_id"),
            "legacy_unverified": is_legacy,
            "scope_status": scope_for_row,
            "updated_at": row.get("updated_at"),
            "created_at": payload.get("created_at"),
        }
        position_by_id[position_id] = item
        # The position snapshot is a display/window concern.  Keep the
        # complete scoped lifecycle in ``position_by_id`` so a fill inside
        # the window can still inherit attribution from an entry outside it.
        if in_window(row.get("updated_at") or payload.get("updated_at")):
            positions.append(item)

    plans: dict[str, dict[str, Any]] = {}
    for row in plan_rows:
        row_scope = _projection_scope_status(
            account_id=account_id or "",
            record_account=row.get("account_id"),
            record_mode=row.get("mode"),
            record_venue=row.get("venue"),
            expected_mode=scope_mode,
            expected_venue=scope_venue,
        ) if account_id else None
        if row_scope is None:
            continue
        plan_id = str(row.get("plan_id") or "")
        if plan_id:
            plans[plan_id] = {"row": row, "payload": _projection_json(row.get("payload_json")), "scope_status": row_scope}

    decisions: dict[str, dict[str, Any]] = {}
    for row in decision_rows:
        payload = _projection_json(row.get("payload_json"))
        proposal = _projection_json(payload.get("proposal"))
        decision_account = payload.get("account_id") or proposal.get("account_id")
        if account_id and decision_account != account_id:
            continue
        decision_id = str(row.get("decision_id") or payload.get("decision_id") or "")
        if decision_id:
            decisions[decision_id] = {"row": row, "payload": payload, "proposal": proposal}

    cycles_by_id: dict[str, dict[str, Any]] = {}
    cycles_by_intent: dict[str, dict[str, Any]] = {}
    for row in cycle_rows:
        payload = _projection_json(row.get("payload_json"))
        cycle = {"row": row, "payload": payload}
        cycle_id = str(row.get("cycle_id") or payload.get("cycle_id") or "")
        if cycle_id:
            cycles_by_id[cycle_id] = cycle
        for candidate in (
            row.get("order_intent_id"),
            payload.get("order_intent_id"),
            _projection_json(payload.get("order_intent")).get("intent_id"),
        ):
            if candidate:
                cycles_by_intent[str(candidate)] = cycle

    # An intent can be addressed by its durable ID, its remote/local order ID,
    # or the ID echoed in a receipt.  All aliases are still tied to the same
    # account-scoped order row.
    orders_by_alias: dict[str, dict[str, Any]] = {}
    all_scoped_orders: list[dict[str, Any]] = []
    scoped_orders: list[dict[str, Any]] = []
    for row in order_rows:
        row_scope = _projection_scope_status(
            account_id=account_id or "",
            record_account=row.get("account_id"),
            record_mode=row.get("mode") or row.get("environment"),
            record_venue=row.get("venue"),
            expected_mode=scope_mode,
            expected_venue=scope_venue,
        ) if account_id else None
        if row_scope is None:
            continue
        execution = _projection_json(row.get("execution_result_json"))
        protection = _projection_json(row.get("protection_plan_json"))
        aliases = {
            str(row.get("intent_id") or ""),
            str(execution.get("order_id") or ""),
            str(execution.get("id") or ""),
        }
        for alias in aliases:
            if alias:
                orders_by_alias[alias] = row
        scoped_order = {"row": row, "execution": execution, "protection": protection, "scope_status": row_scope}
        all_scoped_orders.append(scoped_order)
        if in_window(row.get("created_at")):
            scoped_orders.append(scoped_order)

    all_fill_contexts: list[dict[str, Any]] = []
    fill_contexts: list[dict[str, Any]] = []
    seen_fill_ids: set[str] = set()
    for row in fill_rows:
        fill_id = str(row.get("fill_id") or "")
        if not fill_id or fill_id in seen_fill_ids:
            continue
        fill_scope = _projection_scope_status(
            account_id=account_id or "",
            record_account=row.get("account_id"),
            record_mode=row.get("mode") or row.get("environment"),
            record_venue=row.get("venue"),
            expected_mode=scope_mode,
            expected_venue=scope_venue,
        ) if account_id else None
        if fill_scope is None:
            # A row may share the account id while carrying a different
            # venue/mode due to a bad import or a cross-environment write.
            # It is not safe to show it in this scoped economic view.
            continue
        seen_fill_ids.add(fill_id)
        payload = _projection_json(row.get("payload_json"))
        order = orders_by_alias.get(str(row.get("order_id") or ""))
        if order is None:
            order = orders_by_alias.get(str(payload.get("intent_id") or payload.get("order_intent_id") or ""))
        if order is None and row.get("position_id"):
            for candidate in all_scoped_orders:
                if str(candidate["row"].get("position_id") or "") == str(row["position_id"]):
                    order = candidate["row"]
                    break
        order_execution = _projection_json(order.get("execution_result_json")) if order else {}
        order_protection = _projection_json(order.get("protection_plan_json")) if order else {}
        intent_id = str(order.get("intent_id") or "") if order else str(payload.get("intent_id") or payload.get("order_intent_id") or "")
        position_key = (
            row.get("position_id")
            or payload.get("position_id")
            or (order or {}).get("position_id")
        )
        position = position_by_id.get(str(position_key or ""))
        plan_id = (
            payload.get("trade_plan_id")
            or (position or {}).get("trade_plan_id")
            or order_protection.get("trade_plan_id")
            or order_execution.get("plan_id")
        )
        plan = plans.get(str(plan_id)) if plan_id else None
        plan_payload = plan.get("payload", {}) if plan else {}
        decision = decisions.get(intent_id)
        decision_payload = decision.get("payload", {}) if decision else {}
        proposal = decision.get("proposal", {}) if decision else {}
        cycle_id = (
            (order or {}).get("cycle_id")
            or payload.get("cycle_id")
            or decision_payload.get("cycle_id")
            or order_execution.get("cycle_id")
        )
        cycle = cycles_by_id.get(str(cycle_id)) if cycle_id else None
        if cycle is None and intent_id:
            cycle = cycles_by_intent.get(intent_id)
        cycle_payload = cycle.get("payload", {}) if cycle else {}
        cycle_order = _projection_json(cycle_payload.get("order_intent"))
        explicit_path = _projection_path(
            payload.get("decision_path"),
            (order or {}).get("decision_path"),
            order_execution.get("decision_path"),
            cycle_order.get("decision_path"),
            cycle_payload.get("decision_path"),
        )
        strategy_id = (
            (order or {}).get("strategy_id")
            or payload.get("strategy_id")
            or plan_payload.get("strategy_id")
            or proposal.get("strategy_id")
            or cycle_order.get("strategy_id")
        )
        strategy_version = (
            (order or {}).get("strategy_version")
            or payload.get("strategy_version")
            or plan_payload.get("strategy_version")
            or proposal.get("strategy_version")
            or cycle_order.get("strategy_version")
        )
        path = explicit_path
        if path is None and (plan or decision or strategy_id):
            path = "STRATEGY_DRIVEN"
        if path is None and position and position.get("scope_status") == "LEGACY_UNCONFIRMED":
            path = "LEGACY_UNCONFIRMED"
        if path is None:
            path = "LEGACY_UNCONFIRMED"
        context = {
            "row": row,
            "payload": payload,
            "order": order,
            "position": position,
            "scope_status": fill_scope,
            "plan_id": str(plan_id) if plan_id else None,
            "intent_id": intent_id or None,
            "cycle_id": str(cycle_id) if cycle_id else None,
            "strategy_id": str(strategy_id) if strategy_id else "UNKNOWN",
            "strategy_version": str(strategy_version) if strategy_version else "UNKNOWN",
            "path": path,
            "path_explicit": explicit_path is not None,
            "position_id": str(position_key) if position_key else None,
            "in_window": in_window(row.get("event_at") or row.get("created_at")),
        }
        all_fill_contexts.append(context)
        if context["in_window"]:
            fill_contexts.append(context)

    # Exit fills inherit the entry's verified attribution.  This is still an
    # observation of the existing position, not a claim that the exit was a
    # new AI decision.
    position_attribution: dict[str, dict[str, Any]] = {}
    for context in all_fill_contexts:
        row = context["row"]
        payload = context["payload"]
        reduce_only = _projection_bool(payload.get("reduce_only") or (context["order"] or {}).get("reduce_only"))
        if not reduce_only:
            position_id = str(context.get("position_id") or row.get("position_id") or payload.get("position_id") or "")
            if position_id and context["path"] != "LEGACY_UNCONFIRMED":
                position_attribution.setdefault(position_id, {
                    "attribution": context["path"],
                    "strategy_id": context["strategy_id"],
                    "strategy_version": context["strategy_version"],
                    "cycle_id": context["cycle_id"],
                    "trade_plan_id": context["plan_id"],
                })
    for context in all_fill_contexts:
        row = context["row"]
        payload = context["payload"]
        reduce_only = _projection_bool(payload.get("reduce_only") or (context["order"] or {}).get("reduce_only"))
        position_id = str(context.get("position_id") or row.get("position_id") or payload.get("position_id") or "")
        inherited = position_attribution.get(position_id)
        if reduce_only and inherited and not context["path_explicit"]:
            context["path"] = inherited["attribution"]
            context["strategy_id"] = inherited["strategy_id"]
            context["strategy_version"] = inherited["strategy_version"]
            context["cycle_id"] = context["cycle_id"] or inherited.get("cycle_id")
            context["plan_id"] = context["plan_id"] or inherited.get("trade_plan_id")

    # Build weighted entry bases from the complete scoped lifecycle before
    # applying the display window.  This keeps an exit inside a requested
    # interval attached to its real entry even when that entry happened
    # earlier, while interval PnL below still counts only visible exits.
    entry_basis: dict[str, dict[str, float | str]] = {}
    for context in all_fill_contexts:
        row = context["row"]
        payload = context["payload"]
        order = context["order"] or {}
        if _projection_bool(payload.get("reduce_only") or order.get("reduce_only")):
            continue
        position_id = context.get("position_id")
        quantity = _projection_number(row.get("quantity"), 0.0) or 0.0
        price = _projection_number(row.get("price"))
        if not position_id or quantity <= 0 or price is None:
            continue
        existing = entry_basis.setdefault(
            str(position_id),
            {"quantity": 0.0, "cost": 0.0, "side": str(row.get("side") or "").upper()},
        )
        existing["quantity"] = float(existing["quantity"]) + quantity
        existing["cost"] = float(existing["cost"]) + quantity * price

    fill_by_order: dict[str, list[dict[str, Any]]] = {}
    execution_records: list[dict[str, Any]] = []
    for context in fill_contexts:
        row = context["row"]
        payload = context["payload"]
        order = context["order"] or {}
        order_execution = _projection_json(order.get("execution_result_json")) if order else {}
        order_status = str(order.get("status") or "").upper()
        fill_order_id = row.get("order_id") or payload.get("order_id")
        if fill_order_id:
            fill_by_order.setdefault(str(fill_order_id), []).append(context)
        reduce_only = _projection_bool(payload.get("reduce_only") or order.get("reduce_only"))
        economic_role = "EXIT" if reduce_only else "ENTRY"
        if order_status in _CONCRETE_ORDER_STATUSES:
            status = order_status
        else:
            status = "FILLED"
        position = context["position"] or {}
        position_key = context.get("position_id") or row.get("position_id") or payload.get("position_id")
        quantity = _projection_number(row.get("quantity"), 0.0) or 0.0
        price = _projection_number(row.get("price"))
        # A missing fee is an evidence gap, not a zero-cost fill.  The ledger
        # schema has a historical zero default, so preserve an explicit NULL
        # here for legacy/imported rows and let the quality flags describe the
        # resulting uncertainty instead of silently improving PnL.
        fee = _projection_number(row.get("fee_amount", row.get("fee")), None)
        slippage_cost = _projection_number(
            row.get("slippage_cost", payload.get("slippage_cost", payload.get("slippage_fee"))),
            None,
        )
        realized_pnl: float | None = 0.0
        if reduce_only and position_key and price is not None:
            basis = entry_basis.get(str(position_key))
            if basis and float(basis["quantity"]) > 0:
                entry_price = float(basis["cost"]) / float(basis["quantity"])
                entry_side = str(basis.get("side") or "").upper()
            else:
                entry_price = _projection_number(
                    position.get("entry_price", position.get("entry")),
                    price,
                ) or price
                entry_side = str(position.get("side") or "LONG").upper()
            contract_size = _projection_number(
                row.get("contract_size"),
                _projection_number(position.get("contract_size"), 1.0),
            ) or 1.0
            sign = 1.0 if entry_side in {"BUY", "LONG"} else -1.0
            if fee is not None:
                realized_pnl = (price - entry_price) * quantity * contract_size * sign - fee
            else:
                realized_pnl = None
        record = {
            "record_id": str(row["fill_id"]),
            "record_type": "FILL",
            "account_id": row.get("account_id"),
            "venue": str(row.get("venue") or "UNKNOWN").lower(),
            "mode": str(row.get("mode") or "UNKNOWN").upper(),
            "fill_id": str(row["fill_id"]),
            "intent_id": context["intent_id"],
            "order_id": row.get("order_id") or order_execution.get("order_id"),
            "trade_id": row.get("trade_id") or payload.get("trade_id"),
            "position_id": position_key,
            "symbol": row.get("symbol"),
            "side": str(row.get("side") or "").upper(),
            "economic_role": economic_role,
            "status": status,
            "order_status": order_status or None,
            "quantity": quantity,
            "price": price,
            "fee": fee,
            "slippage_cost": slippage_cost,
            "cost_complete": fee is not None and bool(payload.get("cost_complete", slippage_cost is not None)),
            "fee_currency": row.get("fee_currency") or "UNKNOWN",
            "realized_pnl_usdt": round(realized_pnl, 8) if realized_pnl is not None else None,
            "strategy_id": context["strategy_id"],
            "strategy_version": context["strategy_version"],
            "decision_path": context["path"],
            "attribution": context["path"],
            "cycle_id": context["cycle_id"],
            "trade_plan_id": context["plan_id"],
            "position_status": position.get("status") or "UNKNOWN",
            "protection_status": position.get("protection_status") or "UNKNOWN",
            "scope_status": context.get("scope_status") or ("LEGACY_UNCONFIRMED" if position.get("scope_status") == "LEGACY_UNCONFIRMED" else "SCOPED"),
            "event_at": _projection_iso(row.get("event_at") or row.get("created_at")),
            "created_at": _projection_iso(row.get("created_at")),
            "concrete_economic_fill": True,
            "data_as_of": payload.get("data_as_of") or payload.get("market_data_as_of"),
        }
        execution_records.append(record)

    execution_records.sort(key=lambda item: item.get("event_at") or "", reverse=True)
    total_records = len(execution_records)
    offset = (page - 1) * page_size
    paged_records = execution_records[offset : offset + page_size]

    orders: list[dict[str, Any]] = []
    for scoped in scoped_orders:
        row = scoped["row"]
        execution = scoped["execution"]
        intent_id = str(row.get("intent_id") or "")
        aliases = {intent_id, str(execution.get("order_id") or ""), str(execution.get("id") or "")}
        related = [
            context for context in all_fill_contexts
            if str(context["row"].get("order_id") or "") in aliases
            or str(context["intent_id"] or "") == intent_id
        ]
        status = str(row.get("status") or "UNKNOWN").upper()
        if related:
            economic_status = status if status in _CONCRETE_ORDER_STATUSES else "FILLED"
        elif status == "FILLED" or status == "PARTIALLY_FILLED":
            economic_status = "RECONCILIATION_REQUIRED"
        elif status in {"ACKNOWLEDGED", "CREATED", "RISK_APPROVED", "SUBMITTING", "CANCEL_PENDING"}:
            economic_status = "NOT_FILLED_PENDING"
        else:
            economic_status = "NOT_FILLED"
        first = related[0] if related else None
        orders.append({
            "intent_id": intent_id,
            "order_id": execution.get("order_id") or execution.get("id") or (f"ord_paper_{intent_id}" if intent_id else None),
            "account_id": row.get("account_id"),
            "venue": str(row.get("venue") or "UNKNOWN").lower(),
            "mode": str(row.get("mode") or row.get("environment") or "UNKNOWN").upper(),
            "symbol": row.get("instrument_id"),
            "side": str(row.get("side") or "").upper(),
            "order_type": row.get("order_type"),
            "quantity": _projection_number(row.get("quantity"), 0.0),
            "price": _projection_number(row.get("price")),
            "reduce_only": _projection_bool(row.get("reduce_only")),
            "status": status,
            "economic_status": economic_status,
            "filled_quantity": round(sum(_projection_number(item["row"].get("quantity"), 0.0) or 0.0 for item in related), 12),
            "fill_count": len(related),
            "position_id": row.get("position_id") or (first["row"].get("position_id") if first else None),
            "strategy_id": row.get("strategy_id") or (first["strategy_id"] if first else "UNKNOWN"),
            "strategy_version": row.get("strategy_version") or (first["strategy_version"] if first else "UNKNOWN"),
            "decision_path": _projection_path(row.get("decision_path")) or (first["path"] if first else "LEGACY_UNCONFIRMED"),
            "cycle_id": row.get("cycle_id") or (first["cycle_id"] if first else None),
            "trade_plan_id": (first["plan_id"] if first else _projection_json(row.get("protection_plan_json")).get("trade_plan_id")),
            "scope_status": scoped["scope_status"],
            "created_at": _projection_iso(row.get("created_at")),
            "updated_at": _projection_iso(row.get("updated_at")),
        })

    for position in positions:
        attribution = position_attribution.get(position["position_id"])
        if attribution:
            position["attribution"] = attribution["attribution"]
            position["decision_path"] = attribution["attribution"]
            position["strategy_id"] = attribution["strategy_id"]
            position["strategy_version"] = attribution["strategy_version"]
            position["cycle_id"] = attribution.get("cycle_id")
            position["trade_plan_id"] = position.get("trade_plan_id") or attribution.get("trade_plan_id")
        else:
            position["attribution"] = "LEGACY_UNCONFIRMED" if position.get("legacy_unverified") else "LEGACY_UNCONFIRMED"
            position["decision_path"] = position["attribution"]
            position["strategy_id"] = "UNKNOWN"
            position["strategy_version"] = "UNKNOWN"

    by_attribution: dict[str, dict[str, Any]] = {
        label: {"fill_count": 0, "entry_fill_count": 0, "exit_fill_count": 0}
        for label in sorted(_EXECUTION_ATTRIBUTIONS)
    }
    for item in execution_records:
        bucket = by_attribution.setdefault(item["attribution"], {"fill_count": 0, "entry_fill_count": 0, "exit_fill_count": 0})
        bucket["fill_count"] += 1
        bucket["entry_fill_count" if item["economic_role"] == "ENTRY" else "exit_fill_count"] += 1
    performance_by_attribution: dict[str, dict[str, Any]] = {
        label: {
            "position_count": 0,
            "closed_position_count": 0,
            "fill_count": 0,
            "entry_fill_count": 0,
            "exit_fill_count": 0,
            "realized_pnl_usdt": 0.0,
        }
        for label in sorted(_EXECUTION_ATTRIBUTIONS)
    }
    for position in positions:
        label = position.get("attribution") or "LEGACY_UNCONFIRMED"
        bucket = performance_by_attribution.setdefault(label, {
            "position_count": 0,
            "closed_position_count": 0,
            "fill_count": 0,
            "entry_fill_count": 0,
            "exit_fill_count": 0,
            "realized_pnl_usdt": 0.0,
        })
        bucket["position_count"] += 1
        if str(position.get("status") or "").upper() == "CLOSED":
            bucket["closed_position_count"] += 1
    # Fill counts and interval PnL are event-window facts.  In particular,
    # do not add ``position.realized_pnl`` here: that snapshot may include
    # exits before ``from_at`` and would turn a display filter into a false
    # whole-lifecycle performance claim.
    for item in execution_records:
        label = item.get("attribution") or "LEGACY_UNCONFIRMED"
        bucket = performance_by_attribution.setdefault(label, {
            "position_count": 0,
            "closed_position_count": 0,
            "fill_count": 0,
            "entry_fill_count": 0,
            "exit_fill_count": 0,
            "realized_pnl_usdt": 0.0,
        })
        bucket["fill_count"] += 1
        if item.get("economic_role") == "ENTRY":
            bucket["entry_fill_count"] += 1
        else:
            bucket["exit_fill_count"] += 1
            bucket["realized_pnl_usdt"] = round(
                bucket["realized_pnl_usdt"] + (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0),
                8,
            )
    ai_led_performance = dict(performance_by_attribution.get("AI_LED") or {
        "position_count": 0,
        "closed_position_count": 0,
        "fill_count": 0,
        "entry_fill_count": 0,
        "exit_fill_count": 0,
        "realized_pnl_usdt": 0.0,
    })
    interval_realized_pnl = round(
        sum(_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0 for item in execution_records if item.get("economic_role") == "EXIT"),
        8,
    )
    interval_wins = sum(
        1 for item in execution_records
        if item.get("economic_role") == "EXIT" and (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0) > 0
    )
    interval_losses = sum(
        1 for item in execution_records
        if item.get("economic_role") == "EXIT" and (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0) < 0
    )
    interval_gross_profit = round(sum(
        (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0)
        for item in execution_records
        if item.get("economic_role") == "EXIT" and (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0) > 0
    ), 8)
    interval_gross_loss = round(abs(sum(
        (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0)
        for item in execution_records
        if item.get("economic_role") == "EXIT" and (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0) < 0
    )), 8)
    performance_by_strategy: dict[str, dict[str, Any]] = {}
    realized_by_position: dict[str, float] = {}
    for item in execution_records:
        strategy = str(item.get("strategy_id") or "UNKNOWN")
        metrics = performance_by_strategy.setdefault(strategy, {
            "fill_count": 0,
            "entry_fill_count": 0,
            "exit_fill_count": 0,
            "winning_exit_count": 0,
            "losing_exit_count": 0,
            "realized_pnl_usdt": 0.0,
        })
        metrics["fill_count"] += 1
        if item.get("economic_role") == "ENTRY":
            metrics["entry_fill_count"] += 1
        else:
            metrics["exit_fill_count"] += 1
            metrics["realized_pnl_usdt"] = round(
                metrics["realized_pnl_usdt"] + (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0),
                8,
            )
            if (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0) > 0:
                metrics["winning_exit_count"] += 1
            elif (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0) < 0:
                metrics["losing_exit_count"] += 1
            position_id = str(item.get("position_id") or "")
            if position_id:
                realized_by_position[position_id] = round(
                    realized_by_position.get(position_id, 0.0)
                    + (_projection_number(item.get("realized_pnl_usdt"), 0.0) or 0.0),
                    8,
                )

    return {
        "scope": {
            "account_id": account_id,
            "venue": scope_venue,
            "mode": scope_mode,
            "status": scope_status,
            "read_at": datetime.now(timezone.utc).isoformat(),
            "time_from": start.isoformat() if start else None,
            "time_to": end.isoformat() if end else None,
        },
        "execution_records": paged_records,
        "orders": orders,
        "positions": positions,
        "legacy_unscoped_positions": unscoped_legacy,
        "summary": {
            "fill_count": total_records,
            "entry_fill_count": sum(1 for item in execution_records if item["economic_role"] == "ENTRY"),
            "exit_fill_count": sum(1 for item in execution_records if item["economic_role"] == "EXIT"),
            "order_count": len(orders),
            "orders_with_concrete_fill": sum(1 for item in orders if item["fill_count"] > 0),
            "open_position_count": sum(1 for item in positions if str(item.get("status")).upper() == "OPEN"),
            "partial_position_count": sum(1 for item in positions if str(item.get("status")).upper() == "PARTIALLY_CLOSED"),
            "closed_position_count": sum(1 for item in positions if str(item.get("status")).upper() == "CLOSED"),
            "legacy_unconfirmed_count": sum(1 for item in positions if item.get("attribution") == "LEGACY_UNCONFIRMED") + len(unscoped_legacy),
            "realized_pnl_usdt": interval_realized_pnl,
            "winning_exit_count": interval_wins,
            "losing_exit_count": interval_losses,
            "gross_profit_usdt": interval_gross_profit,
            "gross_loss_usdt": interval_gross_loss,
            "by_strategy": performance_by_strategy,
            "by_position": realized_by_position,
            "pnl_window": {
                "from_at": start.isoformat() if start else None,
                "to_at": end.isoformat() if end else None,
                "basis": "windowed EXIT fills only; entry basis is resolved from the full scoped lifecycle and fees are included",
            },
            "by_attribution": by_attribution,
        },
        "performance_by_attribution": performance_by_attribution,
        "ai_led_performance": ai_led_performance,
        "data_quality": {
            "source_tables": ["order_intents", "trade_fills", "simulated_positions", "trader_trade_plans", "agent_trade_decisions", "ai_led_cycles"],
            "authoritative_fill_source": "trade_fills",
            "acknowledgement_is_not_a_fill": True,
            "attribution_basis": "complete_scoped_position_lifecycle_before_window_filter",
            "realized_pnl_basis": "window_exit_fills_net_of_recorded_exit_fees",
            "query_is_read_only": True,
        },
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_records": total_records,
            "returned_records": len(paged_records),
            "has_more": offset + len(paged_records) < total_records,
        },
    }


def compute_ai_recommended_leverage(
    strategy_id: str,
    confidence_score: float,
    entry: float,
    stop: float,
    atr_val: float = 1.0,
) -> tuple[int, str]:
    """Dynamically determine optimal leverage (5x - 100x) based on risk factors."""
    stop_distance_pct = abs(entry - stop) / entry if entry > 0 else 0.05
    # High confidence & tight stop allows higher leverage, wide stop requires lower leverage
    if stop_distance_pct <= 0.005:  # Extremely tight stop (e.g. SMC SFP wick, <=0.5%)
        base_lev = 75.0 if confidence_score >= 0.85 else 50.0
        reason = f"止损距离极窄({stop_distance_pct*100:.2f}%)且置信度高({confidence_score:.2f})，放大杠杆至高倍数捕捉极致盈亏比"
    elif stop_distance_pct <= 0.015:  # Moderate tight stop (0.5% - 1.5%, e.g. ORB breakout)
        base_lev = 50.0 if confidence_score >= 0.8 else 30.0
        reason = f"日内突破止损处于标准带宽({stop_distance_pct*100:.2f}%)，配置中高动量杠杆平衡风险"
    elif stop_distance_pct <= 0.03:  # Trend pullback stop (1.5% - 3%, e.g. EMA Trend)
        base_lev = 25.0 if confidence_score >= 0.8 else 15.0
        reason = f"趋势通道正常回调保护位({stop_distance_pct*100:.2f}%)，自适应匹配稳健波段杠杆"
    else:  # Wide stop (> 3%, e.g. VWAP Mean Reversion, Extreme Funding)
        base_lev = 10.0 if confidence_score >= 0.8 else 5.0
        reason = f"波动率较大或逆势左侧博弈，止损带宽较宽({stop_distance_pct*100:.2f}%)，保守降低杠杆严防插针"

    # Strategy-specific tuning
    if strategy_id == "liquidity_sweep" and confidence_score >= 0.85:
        base_lev = min(100.0, base_lev * 1.25)
    elif strategy_id == "funding_extreme":
        base_lev = min(15.0, base_lev)  # Hedging strategy stays conservative

    final_lev = int(max(MIN_LEVERAGE, min(MAX_LEVERAGE, round(base_lev))))
    return final_lev, reason


def analyze_ai_trading_ledger(
    store,
    account_id: str | None = "default",
    *,
    venue: str | None = None,
    mode: str | None = None,
    from_at: datetime | str | None = None,
    to_at: datetime | str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict:
    """Analyze one explicitly scoped account's decisions, positions, and events.

    The empty, unregistered database remains useful for the read-only empty
    dashboard and keeps its 1,000 USDT display baseline.  Once accounts exist,
    an unknown or omitted account is an error; this function never chooses the
    first account as a proxy for the caller's scope.
    """
    execution_projection = build_execution_ledger_projection(
        store,
        account_id,
        venue=venue,
        mode=mode,
        from_at=from_at,
        to_at=to_at,
        page=page,
        page_size=page_size,
    )
    initial_capital = INITIAL_SIMULATED_CAPITAL
    with store._connect() as db:
        has_accounts = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone()
        account_row = None
        if has_accounts:
            account_count = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if not account_id and account_count:
                raise ValueError("ACCOUNT_REQUIRED")
            if account_id:
                account_row = db.execute(
                    "SELECT initial_deposit FROM accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
            if account_row:
                initial_capital = float(account_row[0])
            elif account_count:
                raise ValueError(f"ACCOUNT_NOT_FOUND: Account '{account_id}' is not registered.")

        expected_mode = None
        expected_venue = None
        if account_row:
            account_record = db.execute(
                "SELECT mode, config_json FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if account_record:
                expected_mode = str(account_record[0]).upper()
                try:
                    account_config = json.loads(account_record[1] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    account_config = {}
                expected_venue = str(
                    account_config.get("venue")
                    or ("simulated" if expected_mode == "PAPER" else "gate")
                ).lower()

        decisions_rows = db.execute(
            "SELECT decision_id, symbol, status, payload_json, created_at FROM agent_trade_decisions ORDER BY created_at ASC"
        ).fetchall()
        positions_rows = db.execute(
            """SELECT position_id, symbol, status, payload_json, updated_at,
                      account_id, venue, mode, legacy_unverified
                 FROM simulated_positions ORDER BY updated_at ASC"""
        ).fetchall()
        events_rows = db.execute(
            "SELECT event_id, position_id, payload_json, created_at FROM simulation_events ORDER BY created_at ASC"
        ).fetchall()

    def parse_payload(value):
        try:
            item = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return item if isinstance(item, dict) else {}

    def payload_account(item):
        return item.get("account_id") or (item.get("proposal") or {}).get("account_id")

    decisions = []
    for row in decisions_rows:
        item = parse_payload(row["payload_json"])
        if account_id and payload_account(item) != account_id:
            continue
        decisions.append(item)

    positions = []
    scoped_position_ids = set()
    projection_position_ids = {
        str(item.get("position_id"))
        for item in execution_projection.get("positions", [])
        if item.get("position_id")
    }
    for row in positions_rows:
        if "legacy_unverified" in row.keys() and int(row["legacy_unverified"] or 0):
            continue
        item = parse_payload(row["payload_json"])
        scoped_account = row["account_id"] or payload_account(item)
        if account_id and scoped_account != account_id:
            continue
        row_mode = str(row["mode"] or item.get("mode") or "PAPER").upper()
        row_venue = str(row["venue"] or item.get("venue") or "simulated").lower()
        if account_id and expected_mode and (row_mode != expected_mode or row_venue != expected_venue):
            continue
        if row["position_id"] and str(row["position_id"]) not in projection_position_ids:
            # The authoritative projection applies the requested venue/mode
            # and time scope to positions.  Keep the legacy analytics metrics
            # on that same set so its KPI cards cannot silently describe a
            # different account/environment/window than the execution ledger.
            continue
        if scoped_account:
            item["account_id"] = scoped_account
            item.setdefault("mode", row_mode)
            item.setdefault("venue", row_venue)
        positions.append(item)
        if row["position_id"]:
            scoped_position_ids.add(row["position_id"])

    # Map position_id to exit events
    exit_events_by_pos = {}
    for r in events_rows:
        ev = parse_payload(r["payload_json"])
        if account_id and payload_account(ev) != account_id and r["position_id"] not in scoped_position_ids:
            continue
        pos_id = r["position_id"]
        ev_type = ev.get("type")
        if ev_type in {"STOP", "TP1", "TP2", "EMERGENCY"}:
            exit_events_by_pos.setdefault(pos_id, []).append(ev)

    # Initial metrics
    current_equity = initial_capital
    equity_curve = [{"time": "起始本金", "equity": initial_capital, "pnl": 0.0}]
    
    trades = []
    strategy_matrix = {
        "ema_trend": {"name": "EMA 动态通道动能策略", "style": "稳健型 · 趋势追踪", "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "total_rr": 0.0, "leverages": []},
        "bollinger_squeeze": {"name": "布林带/ATR 挤压突破策略", "style": "平衡型 · 波动爆发", "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "total_rr": 0.0, "leverages": []},
        "liquidity_sweep": {"name": "流动性扫荡与订单块反转", "style": "进阶型 · 机构反转", "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "total_rr": 0.0, "leverages": []},
        "session_vwap": {"name": "会话 VWAP 均值回归策略", "style": "稳健型 · 价值中枢回归", "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "total_rr": 0.0, "leverages": []},
        "opening_range_breakout": {"name": "开盘区间突破策略 (ORB)", "style": "激进型 · 动量突破", "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "total_rr": 0.0, "leverages": []},
        "funding_extreme": {"name": "资金费率/OI 极值挤压策略", "style": "对冲型 · 衍生品挤压", "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "total_rr": 0.0, "leverages": []},
    }

    total_realized_pnl = 0.0
    total_wins = 0
    total_losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    all_leverages = []
    margin_used = 0.0

    # Decision map
    decision_map = {d.get("decision_id"): d for d in decisions if d.get("decision_id")}
    projected_positions = {
        str(item.get("position_id")): item
        for item in execution_projection.get("positions", [])
        if item.get("position_id")
    }

    for p in positions:
        pos_id = p.get("position_id")
        dec = decision_map.get(pos_id, {})
        proposal = dec.get("proposal", {})
        projected = projected_positions.get(str(pos_id), {})
        strat_id = (
            projected.get("strategy_id")
            or proposal.get("strategy_id")
            or p.get("strategy_id")
            or "UNKNOWN"
        )
        matrix_strategy_id = strat_id if strat_id in strategy_matrix else None

        pnl = float(p.get("realized_pnl", 0.0))
        status = p.get("status", "OPEN")
        side = p.get("side", "LONG")
        entry = float(p.get("entry", 0.0))
        stop = float(p.get("stop", 0.0))
        targets = p.get("targets", [0.0, 0.0])
        confidence = float(proposal.get("confidence_score")) if proposal.get("confidence_score") is not None else 0.0

        # Dynamic leverage is a sizing explanation only.  It is never used as
        # evidence that the position belonged to AI_LED or that confidence is
        # a win-rate guarantee.
        lev, lev_reason = compute_ai_recommended_leverage(str(strat_id), confidence, entry, stop)
        all_leverages.append(lev)
        if matrix_strategy_id:
            strategy_matrix[matrix_strategy_id]["leverages"].append(lev)

        notional = float(p.get("notional", p.get("filled_contracts", 1) * p.get("contract_size", 0.01) * entry))
        pos_margin = notional / lev if lev > 0 else notional / 100.0

        if status == "OPEN":
            margin_used += pos_margin

        # Exit resolution
        pos_exits = exit_events_by_pos.get(pos_id, [])
        exit_price = pos_exits[-1]["price"] if pos_exits else None
        exit_time = pos_exits[-1].get("bar_at") if pos_exits else None
        exit_reason = pos_exits[-1].get("type") if pos_exits else ("RUNNING" if status == "OPEN" else "CLOSED")

        if matrix_strategy_id:
            strategy_matrix[matrix_strategy_id]["trades"] += 1
            strategy_matrix[matrix_strategy_id]["pnl"] += pnl
        total_realized_pnl += pnl

        if status == "CLOSED" or len(pos_exits) > 0:
            if pnl > 0:
                total_wins += 1
                if matrix_strategy_id:
                    strategy_matrix[matrix_strategy_id]["wins"] += 1
                gross_profit += pnl
            elif pnl < 0:
                total_losses += 1
                if matrix_strategy_id:
                    strategy_matrix[matrix_strategy_id]["losses"] += 1
                gross_loss += abs(pnl)

        # Risk-reward calculation
        risk_dist = abs(entry - stop)
        reward_dist = abs(targets[0] - entry) if targets else 0.0
        rr_ratio = round(reward_dist / risk_dist, 2) if risk_dist > 0 else 2.0
        if matrix_strategy_id:
            strategy_matrix[matrix_strategy_id]["total_rr"] += rr_ratio

        current_equity = initial_capital + total_realized_pnl
        equity_curve.append({
            "time": p.get("created_at", "")[:16].replace("T", " "),
            "equity": round(current_equity, 2),
            "pnl": round(total_realized_pnl, 2),
        })

        # Trade item
        trades.append({
            "trade_id": pos_id,
            "symbol": p.get("symbol", "BTCUSDT"),
            "strategy_id": strat_id,
            "strategy_name": strategy_matrix[matrix_strategy_id]["name"] if matrix_strategy_id else "未确认策略归属 / UNKNOWN",
            "strategy_style": strategy_matrix[matrix_strategy_id]["style"] if matrix_strategy_id else "来源不足 · 不计入策略胜率",
            "side": side,
            "leverage": lev,
            "leverage_reason": lev_reason,
            "entry_price": entry,
            "exit_price": exit_price,
            "margin_usdt": round(pos_margin, 8),
            "stop_loss": stop,
            "targets": targets,
            "pnl_usdt": round(pnl, 2),
            "roi_pct": round((pnl / (pos_margin if pos_margin > 0 else 10.0)) * 100.0, 2),
            "status": status,
            "exit_reason": exit_reason,
            "opened_at": p.get("created_at", ""),
            "closed_at": exit_time or (p.get("updated_at") if status == "CLOSED" else None),
            "ai_summary": dec.get("verdict", {}).get("summary") or proposal.get("rationale") or "证据不足；未推断模型理由或盈利保证",
            "confidence_score": confidence,
            "strategy_version": projected.get("strategy_version") or proposal.get("strategy_version") or "UNKNOWN",
            "decision_path": projected.get("decision_path") or "LEGACY_UNCONFIRMED",
            "attribution": projected.get("attribution") or "LEGACY_UNCONFIRMED",
            "account_id": projected.get("account_id") or account_id,
            "venue": projected.get("venue") or expected_venue or "UNKNOWN",
            "mode": projected.get("mode") or expected_mode or "UNKNOWN",
        })

    # When the caller requests a time window, account-level KPI cards follow
    # the same economic event window as the execution projection.  Position
    # snapshots remain useful for status/attribution, but their cumulative
    # ``realized_pnl`` is not reused as interval performance.
    if from_at is not None or to_at is not None:
        window_summary = execution_projection.get("summary") or {}
        total_realized_pnl = float(window_summary.get("realized_pnl_usdt") or 0.0)
        total_wins = int(window_summary.get("winning_exit_count") or 0)
        total_losses = int(window_summary.get("losing_exit_count") or 0)
        gross_profit = float(window_summary.get("gross_profit_usdt") or 0.0)
        gross_loss = float(window_summary.get("gross_loss_usdt") or 0.0)
        realized_by_position = window_summary.get("by_position") or {}
        for trade in trades:
            interval_pnl = float(realized_by_position.get(str(trade.get("trade_id")), 0.0) or 0.0)
            trade["pnl_usdt"] = round(interval_pnl, 2)
            margin = float(trade.get("margin_usdt") or 0.0)
            trade["roi_pct"] = round((interval_pnl / margin) * 100.0, 2) if margin > 0 else 0.0
        for data in strategy_matrix.values():
            data["pnl"] = 0.0
            data["wins"] = 0
            data["losses"] = 0
        for strategy_id, metrics in (window_summary.get("by_strategy") or {}).items():
            data = strategy_matrix.get(strategy_id)
            if data is not None:
                data["pnl"] = float(metrics.get("realized_pnl_usdt") or 0.0)
                # Fill-level counts are retained in the execution projection;
                # the legacy matrix uses position/trade counts and therefore
                # only receives outcome counts here.
                data["wins"] = int(metrics.get("winning_exit_count") or 0)
                data["losses"] = int(metrics.get("losing_exit_count") or 0)
        current_equity = initial_capital + total_realized_pnl

    # Strategy matrix summary
    per_strategy_summary = []
    for sid, sdata in strategy_matrix.items():
        cnt = sdata["trades"]
        wins = sdata["wins"]
        losses = sdata["losses"]
        closed_cnt = wins + losses
        win_rate = round((wins / closed_cnt) * 100.0, 1) if closed_cnt > 0 else 0.0
        avg_lev = round(sum(sdata["leverages"]) / len(sdata["leverages"]), 1) if sdata["leverages"] else 25.0
        avg_rr = round(sdata["total_rr"] / cnt, 2) if cnt > 0 else 2.0
        per_strategy_summary.append({
            "strategy_id": sid,
            "name": sdata["name"],
            "style": sdata["style"],
            "trades_count": cnt,
            "wins_count": wins,
            "losses_count": losses,
            "win_rate_pct": win_rate,
            "net_pnl_usdt": round(sdata["pnl"], 2),
            "avg_leverage": avg_lev,
            "avg_risk_reward": avg_rr,
        })

    # Overall metrics
    total_closed = total_wins + total_losses
    win_rate = round((total_wins / total_closed) * 100.0, 1) if total_closed > 0 else 0.0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (9.99 if gross_profit > 0 else 1.0)
    net_pnl = round(total_realized_pnl, 2)
    roi_total = round((net_pnl / initial_capital) * 100.0, 2) if initial_capital > 0 else 0.0
    avg_leverage_overall = round(sum(all_leverages) / len(all_leverages), 1) if all_leverages else 35.0

    # Max Drawdown calculation
    peak = initial_capital
    max_dd = 0.0
    for pt in equity_curve:
        eq = pt["equity"]
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    max_drawdown_pct = round(max_dd * 100.0, 2)

    # Trader DNA & Style Evaluation
    style_dna = _generate_trader_style_dna(
        win_rate=win_rate,
        avg_leverage=avg_leverage_overall,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown_pct,
        trades_count=len(trades),
        strategy_summary=per_strategy_summary,
        initial_capital=initial_capital,
    )

    return {
        "account": {
            "initial_capital_usdt": round(initial_capital, 2),
            "current_equity_usdt": round(current_equity, 2),
            "net_pnl_usdt": net_pnl,
            "total_roi_pct": roi_total,
            "margin_used_usdt": round(margin_used, 2),
            "margin_available_usdt": max(0.0, round(current_equity - margin_used, 2)),
            "win_rate_pct": win_rate,
            "total_trades": len(trades),
            "winning_trades": total_wins,
            "losing_trades": total_losses,
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_drawdown_pct,
            "avg_leverage": avg_leverage_overall,
            "leverage_range": "5x ~ 100x (规则建议评分 / 自适应)",
        },
        "style_dna": style_dna,
        "strategy_matrix": per_strategy_summary,
        "trades": sorted(trades, key=lambda x: x["opened_at"], reverse=True),
        "execution_records": execution_projection["execution_records"],
        "execution_orders": execution_projection["orders"],
        "execution_positions": execution_projection["positions"],
        "execution_summary": execution_projection["summary"],
        "performance_by_attribution": execution_projection["performance_by_attribution"],
        "ai_led_performance": execution_projection["ai_led_performance"],
        "execution_scope": execution_projection["scope"],
        "execution_data_quality": execution_projection["data_quality"],
        "execution_pagination": execution_projection["pagination"],
        "legacy_unscoped_positions": execution_projection["legacy_unscoped_positions"],
        "equity_curve": equity_curve[-30:],
        "timezone": "Asia/Hong_Kong",
        "timezone_label": "中国香港时区 (HKT UTC+8)",
    }


def _generate_trader_style_dna(
    win_rate: float,
    avg_leverage: float,
    profit_factor: float,
    max_drawdown: float,
    trades_count: int,
    strategy_summary: list,
    initial_capital: float = 1000.0,
) -> dict:
    """Evaluate trading behavioral DNA, discipline, and generate a natural language review."""
    # A configured stop is not evidence of execution discipline. Do not turn
    # architecture promises or sizing recommendations into measured behavior.
    discipline_score = None

    # Aggressiveness: based on leverage and trade count
    if avg_leverage >= 50:
        aggressiveness_label = "高杠杆建议区间（非实测交易风格）"
        risk_temperament = "偏激进"
    elif avg_leverage >= 25:
        aggressiveness_label = "中杠杆建议区间（非实测交易风格）"
        risk_temperament = "攻守兼备"
    else:
        aggressiveness_label = "低杠杆建议区间（非实测交易风格）"
        risk_temperament = "稳健防御"

    # Best strategy
    best_strat = max(strategy_summary, key=lambda s: s["net_pnl_usdt"]) if strategy_summary else None
    best_name = best_strat["name"] if best_strat and best_strat["net_pnl_usdt"] > 0 else "暂无正收益策略证据"

    # Natural language report
    report = (
        f"当前统计包含 {trades_count} 笔持仓记录，历史已结算胜率为 {win_rate:.1f}%。"
        f"策略对比结果：{best_name}。样本数量及账户、时间范围均会影响结果。"
        "尚未建立可验证的执行纪律评分和实际杠杆分布；规则建议杠杆不代表真实使用杠杆，历史收益不保证未来表现。"
    )

    return {
        "risk_temperament": risk_temperament,
        "style_label": aggressiveness_label,
        "discipline_score": discipline_score,
        "avg_leverage": None,
        "leverage_distribution": {
            "conservative_5_25x": None,
            "moderate_25_50x": None,
            "aggressive_50_100x": None,
        },
        "style_report": report,
        "best_strategy": best_name,
    }
