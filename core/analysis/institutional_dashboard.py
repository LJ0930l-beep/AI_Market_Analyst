"""Read-only institutional dashboard projection.

The dashboard is deliberately a projection of durable execution facts.  It
does not call an exchange, mutate a snapshot, run a migration, or infer AI
ownership from an old prediction row.  A Gate TestNet account is scoped by
``account_type=GATE_TESTNET`` and therefore only ``TESTNET/gate`` ledger rows
are eligible; legacy ``PAPER/gate`` rows remain visibly excluded.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any

from core.model_routing import DEFAULT_SMART_MODEL, is_bonsai_model_identity
from ..trading.account_scope import resolve_account_scope
from ..trading.gate_account_truth import (
    CAPITAL_BASIS_SOURCE,
    resolve_remote_capital_basis,
)


LOCAL_LEDGER_SOURCE = "authoritative_local_execution_ledger"
REMOTE_TRUTH_MISSING_SOURCE = "gate_remote_truth_not_observed"
GATE_EQUITY_BASIS = "GATE_TESTNET_REMOTE_ACCOUNT_TRUTH"
LOCAL_EQUITY_BASIS = "realized_and_observed_fees_only"

STRATEGY_LABELS: dict[str, str] = {
    "ema_trend": "EMA 趋势跟随",
    "bollinger_squeeze": "布林带挤压",
    "liquidity_sweep": "流动性扫掠",
    "session_vwap": "时段 VWAP",
    "opening_range_breakout": "开盘区间突破",
    "funding_extreme": "资金费率极值",
}

_KNOWN_CYCLE_ACTIONS = (
    "WAIT",
    "HOLD",
    "OPEN_LONG",
    "OPEN_SHORT",
    "REDUCE_POSITION",
    "CLOSE_POSITION",
    "TIGHTEN_STOP",
)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe(value: Any, *, depth: int = 0) -> Any:
    """Return bounded UI-safe JSON without credentials or model transcripts."""
    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        forbidden = {
            "api_key",
            "api_secret",
            "secret",
            "password",
            "token",
            "raw_model_response",
            "messages",
            "prompt",
            "chain_of_thought",
            "cot",
        }
        return {
            str(key): _safe(item, depth=depth + 1)
            for key, item in value.items()
            if str(key).lower() not in forbidden
        }
    if isinstance(value, list):
        return [_safe(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, (str, int, bool)) or value is None:
        return value[:500] if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)[:500]


def _time(value: Any) -> datetime | None:
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


def _iso(value: Any) -> str | None:
    point = _time(value)
    return point.isoformat() if point else (str(value) if value else None)


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _table_exists(db: Any, name: str) -> bool:
    return bool(
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _rows(db: Any, table: str, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not _table_exists(db, table):
        return []
    return [dict(row) for row in db.execute(query, params).fetchall()]


def _in_window(value: Any, start: datetime | None, end: datetime | None) -> bool:
    point = _time(value)
    if start and (point is None or point < start):
        return False
    if end and (point is None or point > end):
        return False
    return True


def _scope_match(row: dict[str, Any], scope: dict[str, str], payload: dict[str, Any] | None = None) -> bool:
    data = payload or {}
    if str(row.get("account_id") or data.get("account_id") or "") != scope["account_id"]:
        return False
    mode = str(row.get("mode") or data.get("mode") or "").strip().upper()
    venue = str(row.get("venue") or data.get("venue") or "").strip().lower()
    # Missing scope is legacy/unverified evidence, not a dashboard fact.
    return mode == scope["mode"].upper() and venue == scope["venue"].lower()


def _public_fill(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "fill_id": row.get("fill_id"),
        "trade_id": row.get("trade_id"),
        "order_id": row.get("order_id"),
        "position_id": row.get("position_id"),
        "account_id": row.get("account_id"),
        "provider": row.get("provider") or payload.get("provider"),
        "environment": row.get("environment") or payload.get("environment"),
        "venue": row.get("venue"),
        "mode": row.get("mode"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "quantity": _number(row.get("quantity")),
        "price": _number(row.get("price")),
        "fee_amount": _number(row.get("fee_amount", row.get("fee"))),
        "fee_currency": row.get("fee_currency"),
        "status": row.get("status"),
        "event_at": _iso(row.get("event_at") or row.get("created_at")),
        "candidate_id": row.get("candidate_id") or payload.get("candidate_id"),
        "cycle_id": row.get("cycle_id") or payload.get("cycle_id"),
        "strategy_id": row.get("strategy_id") or payload.get("strategy_id"),
        "strategy_version": row.get("strategy_version") or payload.get("strategy_version"),
        "fee_evidence_status": "OBSERVED" if _number(row.get("fee_amount", row.get("fee"))) is not None else "UNKNOWN",
        "payload": _safe(payload),
    }


def _public_order(row: dict[str, Any]) -> dict[str, Any]:
    try:
        execution = _json(row.get("execution_result_json"))
    except Exception:
        execution = {}
    return {
        "intent_id": row.get("intent_id"),
        "idempotency_key": row.get("idempotency_key"),
        "account_id": row.get("account_id"),
        "provider": row.get("provider"),
        "environment": row.get("environment"),
        "venue": row.get("venue"),
        "mode": row.get("mode"),
        "instrument_id": row.get("instrument_id"),
        "side": row.get("side"),
        "order_type": row.get("order_type"),
        "final_order_type": row.get("final_order_type"),
        "order_preference": row.get("order_preference"),
        "quantity": _number(row.get("quantity")),
        "price": _number(row.get("price")),
        "limit_price": _number(row.get("limit_price")),
        "status": row.get("status"),
        "remote_order_status": row.get("remote_order_status"),
        "selection_reason_code": row.get("selection_reason_code"),
        "selection_reason": row.get("selection_reason"),
        "candidate_id": row.get("candidate_id"),
        "cycle_id": row.get("cycle_id"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "execution": _safe(execution),
    }


def _public_position(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    result = _safe(payload)
    if not isinstance(result, dict):
        result = {}
    result.update(
        {
            "position_id": row.get("position_id") or result.get("position_id"),
            "account_id": row.get("account_id") or result.get("account_id"),
            "provider": row.get("provider") or result.get("provider"),
            "environment": row.get("environment") or result.get("environment"),
            "venue": row.get("venue") or result.get("venue"),
            "mode": row.get("mode") or result.get("mode"),
            "symbol": row.get("symbol") or result.get("symbol"),
            "status": row.get("status") or result.get("status"),
            "updated_at": _iso(row.get("updated_at") or result.get("updated_at")),
            "realized_pnl": _number(result.get("realized_pnl")),
        }
    )
    return result


def _public_candidate(row: dict[str, Any]) -> dict[str, Any]:
    context = _json(row.get("context_json"))
    return {
        "candidate_id": row.get("candidate_id"),
        "account_id": row.get("account_id"),
        "provider": row.get("provider"),
        "environment": row.get("environment"),
        "symbol": row.get("symbol"),
        "strategy_id": row.get("strategy_id"),
        "strategy_name": STRATEGY_LABELS.get(str(row.get("strategy_id")), str(row.get("strategy_id") or "UNKNOWN")),
        "strategy_version": row.get("strategy_version"),
        "signal_timeframe": row.get("signal_timeframe"),
        "closed_15m_bar": row.get("closed_15m_bar"),
        "status": row.get("status"),
        "side": row.get("side"),
        "entry_price": _number(row.get("entry_price")),
        "stop_price": _number(row.get("stop_price")),
        "take_profit": _number(row.get("take_profit")),
        "rule_score": _number(row.get("rule_score")),
        "calibrated_probability": _number(row.get("calibrated_probability")),
        "calibration_sample_size": int(row.get("calibration_sample_size") or 0),
        "rationale": str(row.get("rationale") or "")[:500],
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "context": _safe(context),
    }


def _public_cycle(row: dict[str, Any]) -> dict[str, Any]:
    payload = _json(row.get("payload_json"))
    calibration = payload.get("calibration") if isinstance(payload.get("calibration"), dict) else {}
    decision_origin = str(row.get("decision_origin") or "").upper()
    system_blocked = (
        str(row.get("action") or "").upper() == "SYSTEM_BLOCKED"
        or (decision_origin and decision_origin != "MODEL")
    )
    receipt = None if system_blocked else payload.get("model_receipt")
    if not system_blocked and not isinstance(receipt, dict):
        settings = payload.get("model_inference_settings")
        settings = settings if isinstance(settings, dict) else {}
        receipt = {
            "model_id": payload.get("model_id"),
            "actual_model_id": settings.get("actual_model_id"),
            "model_identity_source": settings.get("model_identity_source"),
            "verified_manifest_model_id": settings.get("verified_manifest_model_id"),
            "model_version": payload.get("model_version"),
        }
    model_receipt = None
    if not system_blocked and isinstance(receipt, dict) and receipt.get("model_id") == DEFAULT_SMART_MODEL:
        source = receipt.get("model_identity_source")
        actual_model_id = receipt.get("actual_model_id")
        if actual_model_id is None:
            actual_model_id = receipt.get("model_version")
        manifest_model_id = receipt.get("verified_manifest_model_id")
        actual_is_bonsai = is_bonsai_model_identity(actual_model_id)
        manifest_is_bonsai = is_bonsai_model_identity(manifest_model_id)
        manifest_is_absent = manifest_model_id is None
        source_is_verified = (
            source == "completion_response"
            and (manifest_is_absent or manifest_is_bonsai)
        ) or (
            source == "request_bound_to_verified_manifest"
            and manifest_is_bonsai
        )
        if actual_is_bonsai and source_is_verified:
            model_receipt = receipt
    model_val = (model_receipt or {}).get("model_id")
    ai_analysis = None if system_blocked else payload.get("analysis")
    if not isinstance(ai_analysis, dict) or not ai_analysis:
        ai_analysis = None
    evidence_refs = None if system_blocked else payload.get("evidence_refs")
    if not system_blocked and not isinstance(evidence_refs, list):
        evidence_refs = payload.get("evidence_context_refs")
    if not isinstance(evidence_refs, list):
        evidence_refs = None
    return {
        "cycle_id": row.get("cycle_id"),
        "account_id": row.get("account_id"),
        "provider": row.get("provider") or payload.get("provider"),
        "environment": row.get("environment") or payload.get("execution_environment"),
        "session_id": row.get("session_id"),
        "generation": row.get("generation"),
        "action": row.get("action"),
        "status": row.get("status"),
        "reason": str(row.get("reason") or "")[:500],
        "scheduled_at": _iso(row.get("scheduled_at") or payload.get("scheduled_at")),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "lag_ms": _number(row.get("lag_ms")),
        "duration_ms": _number(row.get("duration_ms") or row.get("latency_ms")),
        "candidate_count": int(row.get("candidate_count") or len(payload.get("candidates") or [])),
        "calibration_state": row.get("calibration_state") or calibration.get("status"),
        "intent_id": row.get("intent_id") or row.get("order_intent_id"),
        "details": _safe(
            {
                "reason": row.get("reason"),
                "rejection_code": row.get("rejection_code"),
                "order_selection": None if system_blocked else payload.get("order_selection"),
                "evidence_refs": evidence_refs,
                "model": model_val,
                "model_receipt": model_receipt,
                "ai_analysis": ai_analysis,
            }
        ),
    }


def _weekday_label(index: int) -> str:
    return ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[index]


def _session_label(hour: int) -> str:
    if 0 <= hour < 8:
        return "亚洲 00–08"
    if 8 <= hour < 13:
        return "欧洲 08–13"
    if 13 <= hour < 21:
        return "美洲 13–21"
    return "跨时段 21–24"


def _bar_event_map(db: Any, fill_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map scoped fill identities to gross exit events without guessing scope."""
    identities: dict[str, dict[str, Any]] = {}
    for row in fill_rows:
        for key in (row.get("trade_id"), row.get("order_id"), row.get("fill_id")):
            if key:
                identities[str(key)] = row
    if not _table_exists(db, "ledger_events") or not identities:
        return {}
    result: dict[str, dict[str, Any]] = {}
    rows = db.execute(
        "SELECT event_type, amount, payload_json, occurred_at FROM ledger_events WHERE account_id=? AND event_type IN ('FILL_EXIT','REALIZED_PNL') ORDER BY occurred_at, event_id",
        (fill_rows[0].get("account_id"),),
    ).fetchall()
    for row in rows:
        payload = _json(row["payload_json"])
        identity = payload.get("trade_id") or payload.get("fill_id") or payload.get("order_id")
        if identity is None or str(identity) not in identities:
            continue
        result[str(identity)] = {
            "gross_pnl": _number(row["amount"], 0.0) or 0.0,
            "occurred_at": row["occurred_at"],
            "payload": _safe(payload),
        }
    return result


def _gate_capital_account(
    capital: dict[str, Any],
    *,
    local_execution_net_pnl: float | None,
) -> dict[str, Any]:
    """Account capital block for a managed Gate account.

    Every capital field is an observed remote snapshot fact.  The local
    compatibility seed (``accounts.initial_deposit``, historically ``10000``)
    is never published here, so an account that has not been synchronised
    renders an explicit missing value instead of a fabricated balance.
    """

    observed = str(capital.get("status") or "").upper() == "AVAILABLE"
    if not observed:
        return {
            "initial_capital_usdt": None,
            "initial_capital_basis": "NOT_OBSERVED",
            "initial_capital_observed_at": None,
            "current_equity_usdt": None,
            "available_margin_usdt": None,
            "used_margin_usdt": None,
            "realized_pnl_usdt": None,
            "unrealized_pnl_usdt": None,
            "net_pnl_usdt": None,
            "cumulative_fees_usdt": None,
            "cumulative_fees_basis": "NOT_OBSERVED",
            "total_roi_pct": None,
            "max_drawdown_pct": None,
            "local_execution_net_pnl_usdt": local_execution_net_pnl,
            "equity_basis": "NOT_OBSERVED",
            "capital_source": None,
            "provider_source": capital.get("provider_source"),
            "truth_observed_at": capital.get("observed_at"),
            "truth_age_seconds": capital.get("age_seconds"),
            "truth_stale": True,
            "truth_error_code": str(capital.get("error_code") or "REMOTE_ACCOUNT_TRUTH_NOT_OBSERVED"),
            "truth_message_zh": capital.get("message_zh"),
        }
    return {
        "initial_capital_usdt": capital.get("baseline_equity"),
        "initial_capital_basis": capital.get("baseline_basis"),
        "initial_capital_observed_at": capital.get("baseline_observed_at"),
        "current_equity_usdt": capital.get("current_equity"),
        "available_margin_usdt": capital.get("available_margin"),
        "used_margin_usdt": capital.get("used_margin"),
        "realized_pnl_usdt": capital.get("realized_pnl"),
        "unrealized_pnl_usdt": capital.get("unrealized_pnl"),
        "net_pnl_usdt": capital.get("net_pnl"),
        "cumulative_fees_usdt": capital.get("cumulative_fees"),
        "cumulative_fees_basis": capital.get("fee_evidence"),
        "total_roi_pct": capital.get("roi_pct"),
        "max_drawdown_pct": capital.get("max_drawdown_pct"),
        "local_execution_net_pnl_usdt": local_execution_net_pnl,
        "equity_basis": GATE_EQUITY_BASIS,
        "capital_source": capital.get("source") or CAPITAL_BASIS_SOURCE,
        "provider_source": capital.get("provider_source"),
        "truth_observed_at": capital.get("observed_at"),
        "truth_age_seconds": capital.get("age_seconds"),
        "truth_stale": bool(capital.get("stale")),
        "truth_error_code": None,
        "truth_message_zh": None,
    }


def _empty(reason: str, scope: dict[str, str], *, status: str = "EMPTY", counts: dict[str, int] | None = None, source: str = LOCAL_LEDGER_SOURCE, account: dict[str, Any] | None = None, equity_drawdown: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "scope": scope,
        "data_quality": {
            "is_sample": False,
            "source": source,
            "status": "NO_FACTS" if status == "EMPTY" else "DEGRADED",
            "reason": reason,
            "counts": counts or {},
            "unknown_fields": ["unrealized_pnl", "model_effectiveness"] if status == "EMPTY" else [],
        },
        "empty_state": {
            "code": "NOT_RUN" if status == "EMPTY" else "DEGRADED_DATA",
            "message_zh": reason,
            "message_en": "No verified execution facts are available for this account scope." if status == "EMPTY" else "Some dashboard facts require reconciliation.",
        },
        "account": account or {
            "initial_capital_usdt": None,
            "current_equity_usdt": None,
            "realized_pnl_usdt": None,
            "net_pnl_usdt": None,
            "cumulative_fees_usdt": None,
            "total_roi_pct": None,
            "max_drawdown_pct": None,
            "equity_basis": "NOT_AVAILABLE",
        },
        "equity_drawdown": equity_drawdown or {"series": [], "max_drawdown_pct": None},
        "realized_pnl_bars": [],
        "strategy_bars": [
            {"strategy_id": key, "name": label, "trade_count": 0, "sample_size": 0, "win_count": 0, "win_rate_pct": None, "pnl_usdt": None}
            for key, label in STRATEGY_LABELS.items()
        ],
        "decision_mix": [],
        "heatmap": {"weekday": [], "session": []},
        "candles": [],
        "timeline": [],
        "details": {"fills": [], "orders": [], "positions": [], "candidates": [], "cycles": [], "memory": []},
    }


def build_institutional_dashboard(
    store: Any,
    account_id: str,
    *,
    from_at: datetime | str | None = None,
    to_at: datetime | str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Build one account-scoped, server-side dashboard projection."""
    account_id = str(account_id or "").strip()
    if not account_id:
        raise ValueError("ACCOUNT_REQUIRED")
    scope = resolve_account_scope(store, account_id)
    if scope is None:
        raise ValueError(f"ACCOUNT_NOT_FOUND: Account '{account_id}' is not registered")
    start = _time(from_at)
    end = _time(to_at)
    if from_at is not None and start is None or to_at is not None and end is None:
        raise ValueError("DASHBOARD_TIME_INVALID")
    if start and end and end < start:
        raise ValueError("DASHBOARD_TIME_RANGE_INVALID")
    bounded = max(1, min(int(limit), 500))

    # A managed Gate account derives its capital, equity, drawdown and fees from
    # observed remote facts only.  The local compatibility seed is not read
    # here at all, so it can never be published as TestNet equity.
    is_gate_scope = str(scope.get("provider") or "").strip().lower() == "gate"
    capital = resolve_remote_capital_basis(store, account_id) if is_gate_scope else {}
    remote_capital_ready = is_gate_scope and str(capital.get("status") or "").upper() == "AVAILABLE"

    with store._connect() as db:
        account_rows = _rows(db, "accounts", "SELECT account_id, mode, currency, initial_deposit, config_json, created_at FROM accounts WHERE account_id=?", (account_id,))
        if not account_rows:
            raise ValueError(f"ACCOUNT_NOT_FOUND: Account '{account_id}' is not registered")
        account_row = account_rows[0]
        account_config = _json(account_row.get("config_json"))
        is_gate_testnet = str(account_config.get("account_type") or "").upper() == "GATE_TESTNET"
        fill_source_rows = _rows(db, "trade_fills", "SELECT * FROM trade_fills WHERE account_id=? ORDER BY COALESCE(event_at, created_at), fill_id", (account_id,))
        order_source_rows = _rows(db, "order_intents", "SELECT * FROM order_intents WHERE account_id=? ORDER BY created_at, intent_id", (account_id,))
        # Read legacy rows with a plain nullable-account predicate first.  A
        # malformed legacy payload must become an excluded/unknown row below,
        # never a SQLite JSON1 exception that turns a read-only dashboard GET
        # into HTTP 500.
        position_source_rows = [] if is_gate_testnet else _rows(db, "simulated_positions", "SELECT * FROM simulated_positions WHERE account_id=? OR account_id IS NULL ORDER BY updated_at, position_id", (account_id,))
        candidate_source_rows = _rows(db, "ai_strategy_candidates", "SELECT * FROM ai_strategy_candidates WHERE account_id=? ORDER BY closed_15m_bar, candidate_id", (account_id,))
        cycle_source_rows = _rows(db, "ai_led_cycles", "SELECT * FROM ai_led_cycles WHERE account_id=? ORDER BY COALESCE(scheduled_at, created_at), cycle_id", (account_id,))
        memory_source_rows = _rows(db, "ai_decision_memory", "SELECT * FROM ai_decision_memory WHERE account_id=? ORDER BY decision_at, memory_id", (account_id,))

        fill_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in fill_source_rows:
            payload = _json(row.get("payload_json"))
            if _scope_match(row, scope, payload) and _in_window(row.get("event_at") or row.get("created_at"), start, end):
                fill_rows.append((row, payload))
        order_rows = [row for row in order_source_rows if _scope_match(row, scope) and _in_window(row.get("created_at"), start, end)]
        position_rows = []
        for row in position_source_rows:
            payload = _json(row.get("payload_json"))
            if _scope_match(row, scope, payload) and _in_window(row.get("updated_at"), start, end):
                position_rows.append((row, payload))
        candidate_rows = [row for row in candidate_source_rows if _scope_match(row, scope) and _in_window(row.get("closed_15m_bar") or row.get("updated_at"), start, end)]
        cycle_rows = [row for row in cycle_source_rows if _scope_match(row, scope, _json(row.get("payload_json"))) and _in_window(row.get("scheduled_at") or row.get("created_at"), start, end)]
        memory_rows = [row for row in memory_source_rows if str(row.get("environment") or "").lower() == scope["environment"].lower() and _in_window(row.get("decision_at"), start, end)]
        exit_events = _bar_event_map(db, [row for row, _ in fill_rows])

        symbols = sorted(
            {
                str(row.get("symbol") or "").upper()
                for row, _ in fill_rows
                if row.get("symbol")
            }
            | {
                str(row.get("symbol") or "").upper()
                for row, _ in position_rows
                if row.get("symbol")
            }
            | {
                str(row.get("symbol") or "").upper()
                for row in candidate_rows
                if row.get("symbol")
            }
        )
        candle_rows: list[dict[str, Any]] = []
        if symbols and _table_exists(db, "market_bar_versions"):
            placeholders = ",".join("?" for _ in symbols)
            params: list[Any] = [*symbols, "15m"]
            conditions = [f"symbol IN ({placeholders})", "timeframe=?", "is_closed=1", "quality_status != 'LEGACY_UNVERIFIED'"]
            if start:
                conditions.append("bar_start >= ?")
                params.append(start.isoformat())
            if end:
                conditions.append("bar_start <= ?")
                params.append(end.isoformat())
            bar_rows = [dict(row) for row in db.execute(
                f"SELECT * FROM market_bar_versions WHERE {' AND '.join(conditions)} ORDER BY bar_start, available_at, revision_id LIMIT 2000",
                tuple(params),
            ).fetchall()]
            latest: dict[tuple[str, str], dict[str, Any]] = {}
            for row in bar_rows:
                key = (str(row.get("instrument_key")), str(row.get("bar_start")))
                previous = latest.get(key)
                if previous is None or (str(row.get("available_at") or ""), str(row.get("revision_id") or "")) >= (str(previous.get("available_at") or ""), str(previous.get("revision_id") or "")):
                    latest[key] = row
            candle_rows = list(latest.values())[-500:]

    public_fills = [_public_fill(row, payload) for row, payload in fill_rows][-bounded:]
    public_orders = [_public_order(row) for row in order_rows][-bounded:]
    public_positions = [_public_position(row, payload) for row, payload in position_rows][-bounded:]
    public_candidates = [_public_candidate(row) for row in candidate_rows][-bounded:]
    public_cycles = [_public_cycle(row) for row in cycle_rows][-bounded:]
    public_memory: list[dict[str, Any]] = []
    for row in memory_rows[-20:]:
        public_memory.append(
            {
                "memory_id": row.get("memory_id"),
                "account_id": row.get("account_id"),
                "provider": row.get("provider"),
                "environment": row.get("environment"),
                "cycle_id": row.get("cycle_id"),
                "candidate_id": row.get("candidate_id"),
                "symbol": row.get("symbol"),
                "action": row.get("action"),
                "cycle_status": row.get("cycle_status"),
                "decision_at": _iso(row.get("decision_at")),
                "summary_zh": str(row.get("summary_zh") or "")[:500],
                "lesson_zh": str(row.get("lesson_zh") or "")[:500] or None,
                "outcome_status": row.get("outcome_status"),
                "outcome_pnl": _number(row.get("outcome_pnl")),
            }
        )

    counts = {
        "fills": len(public_fills),
        "orders": len(public_orders),
        "positions": len(public_positions),
        "candidates": len(public_candidates),
        "cycles": len(public_cycles),
        "memory": len(public_memory),
        "qualified_candles": len(candle_rows),
    }
    if not any(counts.values()):
        gate_account = _gate_capital_account(capital, local_execution_net_pnl=None) if is_gate_scope else None
        if is_gate_scope and not remote_capital_ready:
            reason = "当前 Gate 模拟盘账户尚无远端账户事实；本地种子存款不作为资金口径，请先在「AI 做单」中同步远端账户。"
        else:
            reason = "当前账户作用域尚无可验证的执行账本事实。"
        result = _empty(
            reason,
            scope,
            counts=counts,
            source=CAPITAL_BASIS_SOURCE if remote_capital_ready else (REMOTE_TRUTH_MISSING_SOURCE if is_gate_scope else LOCAL_LEDGER_SOURCE),
            account=gate_account,
            equity_drawdown=(
                {"series": capital.get("equity_series") or [], "max_drawdown_pct": capital.get("max_drawdown_pct")}
                if remote_capital_ready
                else None
            ),
        )
        result["scope"]["currency"] = account_row.get("currency") or "USDT"
        return result

    gross_exit_by_identity: dict[str, dict[str, Any]] = exit_events
    gross_realized = 0.0
    fill_fees = 0.0
    fee_unknown = 0
    pnl_by_day: dict[str, float] = defaultdict(float)
    pnl_known_by_day: set[str] = set()
    equity_events: list[tuple[datetime, float, str]] = []
    strategy_exit_pnl: dict[str, float] = defaultdict(float)
    strategy_exit_count: dict[str, int] = defaultdict(int)
    strategy_wins: dict[str, int] = defaultdict(int)
    heat_weekday: dict[int, dict[str, Any]] = defaultdict(lambda: {"count": 0, "pnl_usdt": 0.0, "pnl_known": False})
    heat_session: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "pnl_usdt": 0.0, "pnl_known": False})
    for row, payload in fill_rows:
        point = _time(row.get("event_at") or row.get("created_at"))
        if point is None:
            continue
        fee = _number(row.get("fee_amount", row.get("fee")))
        if fee is None:
            fee_unknown += 1
        else:
            fill_fees += fee
        delta = -(fee or 0.0)
        identity_candidates = [row.get("trade_id"), row.get("order_id"), row.get("fill_id")]
        exit_event = next((gross_realized_by for key in identity_candidates if key and (gross_realized_by := gross_exit_by_identity.get(str(key)))), None)
        if exit_event:
            gross = float(exit_event.get("gross_pnl") or 0.0)
            delta += gross
            gross_realized += gross
            day = point.date().isoformat()
            pnl_by_day[day] += delta
            pnl_known_by_day.add(day)
            strategy_id = str(row.get("strategy_id") or payload.get("strategy_id") or "unattributed")
            strategy_exit_pnl[strategy_id] += delta
            strategy_exit_count[strategy_id] += 1
            if delta > 0:
                strategy_wins[strategy_id] += 1
        weekday = point.weekday()
        heat_weekday[weekday]["count"] += 1
        heat_session[_session_label(point.hour)]["count"] += 1
        if exit_event:
            heat_weekday[weekday]["pnl_usdt"] += delta
            heat_weekday[weekday]["pnl_known"] = True
            heat_session[_session_label(point.hour)]["pnl_usdt"] += delta
            heat_session[_session_label(point.hour)]["pnl_known"] = True
        equity_events.append((point, delta, "fill"))

    # If a legacy-compatible remote mirror has no event rows, use its explicit
    # position realized_pnl only as a visible fallback, never as a fabricated
    # per-trade curve.
    if not gross_exit_by_identity:
        fallback_realized = sum((_number(payload.get("realized_pnl"), 0.0) or 0.0) for _, payload in position_rows)
        gross_realized = fallback_realized + fill_fees

    local_execution_net_pnl = gross_realized - fill_fees if not fee_unknown else None
    equity_series: list[dict[str, Any]] = []
    max_drawdown = 0.0
    if remote_capital_ready:
        # The remote snapshot history is the account's real equity curve.  The
        # local ledger delta curve is intentionally not built for this account:
        # it would be anchored to the local seed balance.
        equity_series = [dict(point) for point in capital.get("equity_series") or []]
        max_drawdown = float(capital.get("max_drawdown_pct") or 0.0)
        initial = capital.get("baseline_equity")
        net_pnl = capital.get("net_pnl")
        current_equity = capital.get("current_equity")
    else:
        initial = _number(account_row.get("initial_deposit"))
        net_pnl = local_execution_net_pnl
        current_equity = initial + net_pnl if initial is not None and net_pnl is not None else None
        peak: float | None = initial
        running = initial
        if initial is not None:
            equity_series.append({"time": _iso(_time(account_row.get("created_at")) or datetime.now(timezone.utc)), "equity_usdt": initial, "drawdown_pct": 0.0})
        for point, delta, _kind in sorted(equity_events, key=lambda item: item[0]):
            if running is None:
                break
            running += delta
            if peak is None or running > peak:
                peak = running
            drawdown = ((peak - running) / peak * 100.0) if peak and peak > 0 else None
            if drawdown is not None:
                max_drawdown = max(max_drawdown, drawdown)
            equity_series.append({"time": point.isoformat(), "equity_usdt": running, "drawdown_pct": drawdown})

    strategy_sample: dict[str, int] = defaultdict(int)
    for row in candidate_rows:
        strategy_sample[str(row.get("strategy_id") or "unattributed")] += 1
    strategy_bars = []
    for strategy_id, label in STRATEGY_LABELS.items():
        trades = strategy_exit_count.get(strategy_id, 0)
        pnl = strategy_exit_pnl.get(strategy_id)
        strategy_bars.append(
            {
                "strategy_id": strategy_id,
                "name": label,
                "trade_count": trades,
                "sample_size": strategy_sample.get(strategy_id, 0),
                "win_count": strategy_wins.get(strategy_id, 0),
                "win_rate_pct": (strategy_wins[strategy_id] / trades * 100.0) if trades else None,
                "pnl_usdt": pnl if trades else None,
            }
        )

    decision_counts: dict[str, int] = defaultdict(int)
    for row in cycle_rows:
        decision_counts[str(row.get("action") or "UNKNOWN").upper()] += 1
    decision_mix = [
        {"label": action, "count": decision_counts[action]}
        for action in _KNOWN_CYCLE_ACTIONS
        if decision_counts.get(action)
    ]
    decision_mix.extend(
        {"label": action, "count": count}
        for action, count in sorted(decision_counts.items())
        if action not in _KNOWN_CYCLE_ACTIONS
    )

    marker_by_bar: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidate_rows:
        marker_time = _time(row.get("closed_15m_bar"))
        if marker_time:
            marker_by_bar[(str(row.get("symbol")), marker_time.isoformat())].append({"type": "CANDIDATE", "candidate_id": row.get("candidate_id"), "price": _number(row.get("entry_price"))})
    for row, _payload in fill_rows:
        marker_time = _time(row.get("event_at") or row.get("created_at"))
        if marker_time:
            marker_by_bar[(str(row.get("symbol")), marker_time.isoformat())].append({"type": "FILL", "trade_id": row.get("trade_id"), "price": _number(row.get("price"))})
    for row in order_rows:
        marker_time = _time(row.get("created_at"))
        if marker_time:
            marker_by_bar[(str(row.get("instrument_id")), marker_time.isoformat())].append({"type": "ORDER", "intent_id": row.get("intent_id"), "price": _number(row.get("limit_price") or row.get("price"))})
    candles = []
    for row in candle_rows:
        bar_time = _time(row.get("bar_start"))
        if bar_time is None:
            continue
        markers: list[dict[str, Any]] = []
        bar_end = _time(row.get("bar_end")) or bar_time + timedelta(minutes=15)
        for key, items in marker_by_bar.items():
            symbol, marker_at = key
            marker_point = _time(marker_at)
            if symbol == str(row.get("symbol")) and marker_point and bar_time <= marker_point <= bar_end:
                markers.extend(items)
        candles.append(
            {
                "time": bar_time.isoformat(),
                "symbol": row.get("symbol"),
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": _number(row.get("close")),
                "volume": _number(row.get("volume")),
                "source": row.get("source"),
                "markers": markers,
            }
        )

    status = "AVAILABLE" if fill_rows or order_rows or position_rows or candidate_rows or cycle_rows else "EMPTY"
    if fee_unknown or any(not _scope_match(row, scope, _json(row.get("payload_json"))) for row in fill_source_rows):
        status = "DEGRADED"
    # An empty scope keeps its EMPTY status contract: the missing remote
    # capital basis is reported through data_quality and empty_state instead of
    # reclassifying an account that genuinely has no execution facts yet.
    if remote_capital_ready:
        capital_quality_source = CAPITAL_BASIS_SOURCE
    elif is_gate_scope:
        capital_quality_source = REMOTE_TRUTH_MISSING_SOURCE
    else:
        capital_quality_source = LOCAL_LEDGER_SOURCE
    account_block = (
        _gate_capital_account(capital, local_execution_net_pnl=local_execution_net_pnl)
        if is_gate_scope
        else {
            "initial_capital_usdt": initial,
            "current_equity_usdt": current_equity,
            "realized_pnl_usdt": gross_realized if gross_exit_by_identity or position_rows else None,
            "net_pnl_usdt": net_pnl,
            "cumulative_fees_usdt": None if fee_unknown else fill_fees,
            "total_roi_pct": (net_pnl / initial * 100.0) if net_pnl is not None and initial and initial > 0 else None,
            "max_drawdown_pct": max_drawdown if equity_series else None,
            "local_execution_net_pnl_usdt": local_execution_net_pnl,
            "equity_basis": LOCAL_EQUITY_BASIS,
            "capital_source": LOCAL_LEDGER_SOURCE,
        }
    )
    if is_gate_scope:
        gate_note = (
            "账户资金、权益、回撤与费用来自 Gate TestNet 私有 API 的远端账户事实；成交、决策与保护单来自账户作用域执行账本。"
            if remote_capital_ready
            else "未观测到 Gate 远端账户事实：账户资金与权益显示为空，本地种子存款不作为资金口径。请同步远端账户。"
        )
        quality_note = gate_note
    else:
        quality_note = "成交、费用和盈亏只来自账户作用域账本。"
    result = {
        "status": status,
        "scope": {**scope, "currency": account_row.get("currency") or "USDT"},
        "data_quality": {
            "is_sample": False,
            "source": capital_quality_source,
            "status": "DEGRADED" if status == "DEGRADED" else "AVAILABLE",
            "counts": counts,
            "fee_unknown_count": fee_unknown,
            "unknown_fields": (["fee_amount"] if fee_unknown else []) + (["unrealized_pnl"] if position_rows else []),
            "capital_basis_status": str(capital.get("status") or "UNAVAILABLE").upper() if is_gate_scope else "LOCAL_PAPER",
            "capital_observed_at": capital.get("observed_at") if remote_capital_ready else None,
            "capital_stale": bool(capital.get("stale")) if remote_capital_ready else None,
            "note_zh": quality_note,
        },
        "empty_state": None if status != "EMPTY" else {"code": "NOT_RUN", "message_zh": "当前账户尚无可验证的执行账本事实。", "message_en": "No verified execution facts are available for this account scope."},
        "account": account_block,
        "equity_drawdown": {"series": equity_series, "max_drawdown_pct": max_drawdown if equity_series else None},
        "realized_pnl_bars": [{"time": day, "pnl_usdt": value} for day, value in sorted(pnl_by_day.items()) if day in pnl_known_by_day],
        "strategy_bars": strategy_bars,
        "decision_mix": decision_mix,
        "heatmap": {
            "weekday": [{"key": index, "label": _weekday_label(index), "count": heat_weekday[index]["count"], "pnl_usdt": heat_weekday[index]["pnl_usdt"] if heat_weekday[index]["pnl_known"] else None} for index in range(7)],
            "session": [{"key": label, "label": label, "count": heat_session[label]["count"], "pnl_usdt": heat_session[label]["pnl_usdt"] if heat_session[label]["pnl_known"] else None} for label in ("亚洲 00–08", "欧洲 08–13", "美洲 13–21", "跨时段 21–24")],
        },
        "candles": candles,
        "timeline": list(reversed(public_cycles[-bounded:])),
        "details": {
            "fills": list(reversed(public_fills)),
            "orders": list(reversed(public_orders)),
            "positions": list(reversed(public_positions)),
            "candidates": list(reversed(public_candidates)),
            "cycles": list(reversed(public_cycles)),
            "memory": list(reversed(public_memory)),
        },
    }
    return result


__all__ = ["build_institutional_dashboard"]
