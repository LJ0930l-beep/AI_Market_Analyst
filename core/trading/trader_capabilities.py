"""Durable trader-assistant capabilities built on the existing ledger/gateway.

This module deliberately contains the smallest useful product layer for the
v1.3 repair.  It does not create a second trading engine: execution is still
owned by :class:`ExecutionGateway`, account truth by :class:`AccountLedger`,
and AI cycles by :class:`AISessionCoordinator`.

The service is conservative at every evidence boundary.  Missing account,
market, remote reconciliation, model, or historical-cost evidence is exposed
as ``UNKNOWN``/``EVIDENCE_INSUFFICIENT`` rather than filled with a sample
value.  All durable tables are additive and account/environment scoped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
import math
import uuid
from typing import Any

from core.analysis.strategy_evaluator import evaluate_strategy_effectiveness
from core.analysis.ai_trade_analytics import build_execution_ledger_projection
from core.analysis.strategy_replay import StrategyReplayError, run_strategy_bar_replay
from core.news_revision import NewsRevisionRegistry

from .execution_gateway import (
    ControlMode,
    DecisionPath,
    ExecutionGateway,
    GatewayError,
    OrderIntent,
    ProtectionPlan,
    TradingMode,
)
from .ledger import AccountLedger
from .account_scope import resolve_account_scope
from .risk_engine import RiskEngine
from .trade_plan_contract import (
    TRADE_PLAN_SCHEMA_VERSION,
    TradePlanContractError,
    evaluate_plan_conditions,
    normalize_plan_payload,
    round_down_quantity,
)


UNKNOWN = "UNKNOWN"
_ACTIVE_ORDER_STATUSES = {
    "CREATED",
    "RISK_APPROVED",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "UNKNOWN",
    "SUBMITTED",
    "SUBMITTING",
    "CANCEL_PENDING",
}


class TraderCapabilityError(ValueError):
    """A user-correctable, account-scoped trader capability error."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _now(clock: Any = None) -> datetime:
    value = clock() if callable(clock) else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = _parse_time(value)
        return parsed.isoformat() if parsed else value
    point = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
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
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _number(value: Any, *, positive: bool = False, non_negative: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if positive and parsed <= 0:
        return None
    if non_negative and parsed < 0:
        return None
    return parsed


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return result if result.is_finite() else Decimal(default)


class TraderCapabilityService:
    """Account-scoped risk cockpit, research, plans, news, and scorecards."""

    def __init__(
        self,
        store: Any,
        *,
        gateway: ExecutionGateway | None = None,
        runtime: Any | None = None,
        clock: Any = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.runtime = runtime
        self.clock = clock
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        if not hasattr(self.store, "_connect"):
            return
        with self.store._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS trader_trade_plans (
                    plan_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    position_id TEXT,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    execution_result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS research_evaluation_tasks (
                    task_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    window_start TEXT,
                    window_end TEXT,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS trader_news_impacts (
                    impact_id TEXT PRIMARY KEY,
                    news_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    expected_gap REAL,
                    absorbed TEXT NOT NULL,
                    invalidation_json TEXT NOT NULL,
                    verification_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )

    def _scope(
        self,
        account_id: str,
        *,
        mode: str | None = None,
        venue: str | None = None,
    ) -> dict[str, str]:
        if not account_id or not hasattr(self.store, "_connect"):
            raise TraderCapabilityError("ACCOUNT_REQUIRED", "A registered account scope is required.", 422)
        with self.store._connect() as db:
            row = db.execute(
                "SELECT account_id, mode, config_json, currency FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise TraderCapabilityError("ACCOUNT_NOT_FOUND", f"Account '{account_id}' is not registered.", 404)
        account_mode = str(row["mode"]).upper()
        config = _json_object(row["config_json"])
        resolved = resolve_account_scope(self.store, account_id) or {}
        effective_mode_value = str(resolved.get("mode") or account_mode).upper()
        account_venue = str(
            resolved.get("venue")
            or config.get("venue")
            or ("simulated" if effective_mode_value == "PAPER" else "gate")
        ).strip().lower()
        requested_mode = str(mode).upper() if mode is not None else None
        # ``gate_paper`` keeps PAPER as its legacy account-row/display label,
        # but its authoritative execution scope is Gate TestNet.  Accept that
        # one compatibility spelling at the input boundary and normalize the
        # returned scope to TESTNET so trade plans cannot enter the local
        # PAPER matcher.
        legacy_gate_paper_alias = (
            requested_mode == "PAPER"
            and effective_mode_value == "TESTNET"
            and str(resolved.get("account_type") or "").upper() == "GATE_TESTNET"
        )
        if requested_mode is not None and requested_mode != effective_mode_value and not legacy_gate_paper_alias:
            raise TraderCapabilityError(
                "ACCOUNT_MODE_MISMATCH",
                f"Account '{account_id}' is {effective_mode_value}, not {mode}.",
                422,
            )
        if venue is not None and str(venue).lower() != account_venue:
            raise TraderCapabilityError(
                "ACCOUNT_VENUE_MISMATCH",
                f"Account '{account_id}' is bound to venue '{account_venue}', not '{venue}'.",
                422,
            )
        return {
            "account_id": str(account_id),
            "mode": effective_mode_value,
            "venue": account_venue,
            "currency": str(row["currency"] or "USDT").upper(),
        }

    @staticmethod
    def _fresh_mark(store: Any, symbol: str, now: datetime) -> tuple[float | None, dict[str, Any]]:
        try:
            state = store.get_realtime_state(symbol)
        except Exception:
            state = None
        if not isinstance(state, dict):
            return None, {"status": UNKNOWN, "source": None, "data_as_of": None}
        price = _number(state.get("price"), positive=True)
        data_as_of = _parse_time(state.get("data_as_of") or state.get("timestamp") or state.get("last_trade_at"))
        received_at = _parse_time(state.get("received_at") or state.get("updated_at"))
        reference = data_as_of or received_at
        try:
            stale_after_value = state["stale_after_seconds"] if "stale_after_seconds" in state else 120.0
            stale_after = float(stale_after_value)
        except (TypeError, ValueError):
            stale_after = 0.0
        freshness = str(state.get("freshness_status") or "").upper()
        if (
            price is None
            or reference is None
            or not math.isfinite(stale_after)
            or stale_after <= 0
            or freshness in {"STALE", "DEGRADED", "UNKNOWN", "UNAVAILABLE"}
            or state.get("stale") is True
            or state.get("fresh") is False
            or (now - reference).total_seconds() > min(120.0, stale_after)
            or (now - reference).total_seconds() < -5.0
        ):
            return None, {
                "status": UNKNOWN,
                "source": state.get("provider"),
                "data_as_of": _iso(reference),
                "reason": "NO_FRESH_EXECUTABLE_MARK",
            }
        return price, {
            "status": "FRESH",
            "source": state.get("provider") or "realtime_state",
            "data_as_of": _iso(reference),
            "received_at": _iso(received_at),
        }

    def _legacy_position_counts(self, account_id: str) -> dict[str, int]:
        result = {"account_scoped": 0, "unassigned": 0}
        with self.store._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
            ).fetchone()
            if not exists:
                return result
            rows = db.execute(
                "SELECT account_id, payload_json, legacy_unverified FROM simulated_positions WHERE legacy_unverified=1"
            ).fetchall()
        for row in rows:
            payload = _json_object(row["payload_json"])
            scoped = row["account_id"] or payload.get("account_id")
            if scoped == account_id:
                result["account_scoped"] += 1
            elif not scoped:
                result["unassigned"] += 1
        return result

    def _orders(self, scope: dict[str, str], *, limit: int = 200) -> list[dict[str, Any]]:
        with self.store._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
            ).fetchone()
            if not exists:
                return []
            rows = db.execute(
                "SELECT * FROM order_intents WHERE account_id=? ORDER BY created_at DESC LIMIT ?",
                (scope["account_id"], max(1, min(int(limit), 500))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            raw_mode = row["mode"] or row["environment"]
            raw_venue = row["venue"]
            scope_complete = bool(raw_mode and raw_venue)
            row_mode = str(raw_mode).upper() if raw_mode else UNKNOWN
            row_venue = str(raw_venue).lower() if raw_venue else UNKNOWN
            if scope_complete and (row_mode != scope["mode"] or row_venue != scope["venue"]):
                continue
            execution = _json_object(row["execution_result_json"])
            protection = _json_object(row["protection_plan_json"])
            evidence = _json_object(execution.get("execution_evidence"))
            result.append(
                {
                    "intent_id": row["intent_id"],
                    "idempotency_key": row["idempotency_key"],
                    "account_id": row["account_id"],
                    "venue": row_venue,
                    "mode": row_mode,
                    "symbol": row["instrument_id"],
                    "side": row["side"],
                    "order_type": row["order_type"],
                    "quantity": row["quantity"],
                    "price": row["price"],
                    "status": row["status"],
                    "reduce_only": bool(row["reduce_only"]),
                    "position_id": row["position_id"],
                    "scope_status": "SCOPED" if scope_complete else UNKNOWN,
                    "protection": protection,
                    "protection_status": execution.get("protection_status") or (
                        "UNKNOWN" if row["status"] in _ACTIVE_ORDER_STATUSES else None
                    ),
                    "execution_result": execution,
                    "execution_evidence": evidence,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return result

    def _reservations(self, scope: dict[str, str]) -> list[dict[str, Any]]:
        with self.store._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_reservations'"
            ).fetchone()
            if not exists:
                return []
            rows = db.execute(
                """SELECT reservation_id, account_id, amount_risk, amount_margin,
                          status, expires_at, created_at, instrument_id, venue, mode
                     FROM risk_reservations
                    WHERE account_id=? AND status IN ('PENDING','COMMITTED')
                    ORDER BY created_at DESC""",
                (scope["account_id"],),
            ).fetchall()
        result = []
        for row in rows:
            scope_complete = bool(row["mode"] and row["venue"])
            if scope_complete and str(row["mode"]).upper() != scope["mode"]:
                continue
            if scope_complete and str(row["venue"]).lower() != scope["venue"]:
                continue
            result.append(
                {
                    "reservation_id": row["reservation_id"],
                    "account_id": row["account_id"],
                    "amount_risk": str(row["amount_risk"]),
                    "amount_margin": str(row["amount_margin"]),
                    "status": row["status"],
                    "expires_at": row["expires_at"],
                    "created_at": row["created_at"],
                    "instrument_id": row["instrument_id"],
                    "venue": str(row["venue"]).lower() if row["venue"] else UNKNOWN,
                    "mode": str(row["mode"]).upper() if row["mode"] else UNKNOWN,
                    "scope_status": "SCOPED" if scope_complete else UNKNOWN,
                }
            )
        return result

    def risk_snapshot(self, account_id: str, *, runtime: Any | None = None) -> dict[str, Any]:
        """Return one account/venue/mode risk cockpit snapshot.

        Numeric ledger values remain the authoritative local snapshot.  Mark
        quality and remote reconciliation are separately labelled so a stale
        or missing provider cannot be mistaken for a current equity fact.
        """

        scope = self._scope(account_id)
        now = _now(self.clock)
        ledger = AccountLedger(store=self.store)
        marks: dict[str, float] = {}
        mark_evidence: dict[str, Any] = {}
        try:
            positions = ledger.get_open_positions(
                account_id, venue=scope["venue"], mode=scope["mode"]
            )
        except Exception as exc:
            raise TraderCapabilityError("LEDGER_UNAVAILABLE", str(exc), 503) from exc
        for position in positions:
            symbol = str(position.get("symbol") or position.get("instrument_id") or "").upper()
            if not symbol:
                continue
            price, evidence = self._fresh_mark(self.store, symbol, now)
            mark_evidence[symbol] = evidence
            if price is not None:
                marks[symbol] = price
        snapshot = ledger.get_snapshot(account_id, mark_prices=marks, now=now)
        risk = RiskEngine(ledger).get_risk_summary(account_id, now=now)
        orders = self._orders(scope)
        reservations = self._reservations(scope)

        protections: list[dict[str, Any]] = []
        known_position_risk = Decimal("0")
        unknown_risk_items: list[dict[str, Any]] = []
        for position in positions:
            status = str(position.get("protection_status") or UNKNOWN).upper()
            remaining = _decimal(position.get("remaining_contracts", position.get("quantity", 0)))
            entry = _decimal(position.get("entry", position.get("entry_price", 0)))
            stop = _decimal(position.get("stop", position.get("stop_loss", 0)))
            contract = _decimal(position.get("contract_size", 1), "1")
            if status == "ACTIVE" and remaining > 0 and entry > 0 and stop > 0 and contract > 0:
                known_position_risk += abs(entry - stop) * remaining * contract
            else:
                unknown_risk_items.append(
                    {
                        "type": "POSITION_PROTECTION_OR_STOP",
                        "position_id": position.get("position_id"),
                        "symbol": position.get("symbol"),
                        "status": status,
                    }
                )
            protections.append(
                {
                    "position_id": position.get("position_id"),
                    "account_id": account_id,
                    "venue": scope["venue"],
                    "mode": scope["mode"],
                    "symbol": position.get("symbol"),
                    "side": position.get("side"),
                    "quantity": str(position.get("remaining_contracts", position.get("quantity", 0))),
                    "stop_price": position.get("stop", position.get("stop_loss")),
                    "status": status,
                    "identity": {
                        "position_id": position.get("position_id"),
                        "order_id": position.get("order_id"),
                    },
                    "evidence": position.get("protection_evidence") or {
                        "status": UNKNOWN,
                        "reason": "PROTECTION_EVIDENCE_NOT_STORED",
                    },
                }
            )
        for order in orders:
            if order.get("scope_status") == UNKNOWN:
                unknown_risk_items.append(
                    {
                        "type": "ORDER_SCOPE",
                        "intent_id": order.get("intent_id"),
                        "symbol": order.get("symbol"),
                        "status": UNKNOWN,
                        "reason": "ORDER_ENVIRONMENT_SCOPE_INCOMPLETE",
                    }
                )
            elif str(order.get("status") or "").upper() in {"UNKNOWN", "PARTIALLY_FILLED", "ACKNOWLEDGED", "CREATED", "RISK_APPROVED", "SUBMITTED"}:
                if str(order.get("status")).upper() == "UNKNOWN":
                    unknown_risk_items.append(
                        {
                            "type": "ORDER_RECONCILIATION",
                            "intent_id": order.get("intent_id"),
                            "symbol": order.get("symbol"),
                            "status": "UNKNOWN",
                        }
                    )
        for reservation in reservations:
            if reservation.get("scope_status") == UNKNOWN:
                unknown_risk_items.append(
                    {
                        "type": "RESERVATION_SCOPE",
                        "reservation_id": reservation.get("reservation_id"),
                        "status": UNKNOWN,
                        "reason": "RESERVATION_ENVIRONMENT_SCOPE_INCOMPLETE",
                    }
                )

        # Expose concentration as an evidence-bearing view, without inventing
        # correlations, event betas, or strategy ownership that the ledger
        # does not contain.  Known direction/symbol notional is calculated
        # from scoped facts; missing execution prices or group metadata stay
        # UNKNOWN and trigger the conservative group policy when they could
        # change the budget decision.
        direction_notional = {"LONG": Decimal("0"), "SHORT": Decimal("0")}
        symbol_notional: dict[str, Decimal] = {}
        exposure_symbols: set[str] = set()
        unknown_exposure_symbols: set[str] = set()
        unknown_direction = False

        def add_exposure(symbol: Any, side: Any, notional: Decimal | None) -> None:
            nonlocal unknown_direction
            normalized_symbol = str(symbol or "").strip().upper()
            normalized_side = str(side or "").strip().upper()
            direction = "LONG" if normalized_side in {"LONG", "BUY"} else "SHORT" if normalized_side in {"SHORT", "SELL"} else None
            if normalized_symbol:
                exposure_symbols.add(normalized_symbol)
            if direction is None:
                unknown_direction = True
            if not normalized_symbol or not direction or notional is None or notional <= 0:
                if normalized_symbol:
                    unknown_exposure_symbols.add(normalized_symbol)
                return
            direction_notional[direction] += notional
            symbol_notional[normalized_symbol] = symbol_notional.get(normalized_symbol, Decimal("0")) + notional

        for position in positions:
            quantity = _decimal(position.get("remaining_contracts", position.get("quantity", 0)))
            entry = _decimal(position.get("entry", position.get("entry_price", 0)))
            contract = _decimal(position.get("contract_size", 1), "1")
            add_exposure(
                position.get("symbol", position.get("instrument_id")),
                position.get("side"),
                quantity * entry * contract if quantity > 0 and entry > 0 and contract > 0 else None,
            )
        active_orders = [
            order for order in orders
            if str(order.get("status") or "").upper() in _ACTIVE_ORDER_STATUSES
        ]
        for order in active_orders:
            quantity = _decimal(order.get("quantity"))
            price = _decimal(order.get("price"))
            add_exposure(
                order.get("symbol"),
                order.get("side"),
                quantity * price if quantity > 0 and price > 0 else None,
            )
        for reservation in reservations:
            symbol = str(reservation.get("instrument_id") or "").strip().upper()
            if symbol and reservation.get("status") in {"PENDING", "COMMITTED"}:
                exposure_symbols.add(symbol)
                unknown_exposure_symbols.add(symbol)

        total_notional = sum(symbol_notional.values(), Decimal("0"))

        def concentration_bucket(value: Decimal) -> dict[str, Any]:
            return {
                "notional": str(value),
                "share": float(value / total_notional) if total_notional > 0 else None,
            }

        concentration = {
            "status": "CALCULATED_FROM_SCOPED_LEDGER" if not unknown_direction and not unknown_exposure_symbols else UNKNOWN,
            "direction": {key: concentration_bucket(value) for key, value in direction_notional.items()},
            "symbol": {key: concentration_bucket(value) for key, value in sorted(symbol_notional.items())},
            "limits": {
                "instrument_cluster_risk_fraction": str(RiskEngine.MAX_CLUSTER_RISK),
                "direction_policy": "SUM_LONG_SHORT_RISK; no netting without authoritative hedge semantics",
                "correlation_policy": "CONSERVATIVE_SINGLE_INSTRUMENT_CLUSTER_WHEN_UNKNOWN",
                "event_policy": "BLOCK_HIGH_RISK_EVENT_ACTION_WHEN_UNVERIFIED",
                "strategy_policy": "UNKNOWN_GROUP_OWNERSHIP_CANNOT_COMPETE_FOR_NEW_BUDGET",
            },
            "correlation": {
                "status": UNKNOWN,
                "source": "NO_AUTHORITATIVE_CORRELATION_SNAPSHOT",
                "group_count": len(exposure_symbols),
            },
            "event": {
                "status": UNKNOWN,
                "source": "NO_VERIFIED_EVENT_EXPOSURE_SNAPSHOT",
                "policy": "UNVERIFIED_EVENT_GAP_CANNOT_INDEPENDENTLY_TRIGGER_HIGH_RISK",
            },
            "strategy_competition": {
                "status": UNKNOWN if active_orders or reservations else "NOT_APPLICABLE",
                "source": "ORDER_AND_RESERVATION_STRATEGY_OWNERSHIP_NOT_COMPLETE",
                "active_order_count": len(active_orders),
            },
        }
        if len(exposure_symbols) > 1:
            unknown_risk_items.append(
                {
                    "type": "CORRELATION_SCOPE",
                    "status": UNKNOWN,
                    "symbols": sorted(exposure_symbols),
                    "reason": "Multiple exposed instruments without an authoritative correlation snapshot.",
                }
            )
        if unknown_exposure_symbols and any(
            str(order.get("status") or "").upper() in _ACTIVE_ORDER_STATUSES for order in active_orders
        ):
            unknown_risk_items.append(
                {
                    "type": "PORTFOLIO_NOTIONAL_SCOPE",
                    "status": UNKNOWN,
                    "symbols": sorted(unknown_exposure_symbols),
                    "reason": "Pending/UNKNOWN exposure lacks an authoritative executable notional.",
                }
            )

        legacy = self._legacy_position_counts(account_id)
        if legacy["account_scoped"] or legacy["unassigned"]:
            unknown_risk_items.append(
                {
                    "type": "LEGACY_POSITION_SCOPE",
                    "status": UNKNOWN,
                    "account_scoped_count": legacy["account_scoped"],
                    "unassigned_count": legacy["unassigned"],
                    "reason": "LEGACY_POSITION_REQUIRES_EXPLICIT_MIGRATION",
                }
            )
        runtime_obj = runtime if runtime is not None else self.runtime
        if runtime_obj is None:
            runtime_status: dict[str, Any] = {
                "state": "RUNTIME_UNAVAILABLE",
                "active": False,
                "execution_blocked": True,
            }
        else:
            try:
                runtime_status = dict(runtime_obj.status())
            except Exception as exc:
                runtime_status = {
                    "state": "RUNTIME_STATUS_UNKNOWN",
                    "active": False,
                    "execution_blocked": True,
                    "error": str(exc)[:240],
                }
            bound = runtime_status.get("account_id")
            if bound and str(bound) != account_id:
                runtime_status = {
                    "state": "RUNTIME_ACCOUNT_MISMATCH",
                    "active": False,
                    "execution_blocked": True,
                    "bound_account_id": bound,
                    "requested_account_id": account_id,
                }
        ai_status: dict[str, Any]
        coordinator = getattr(runtime_obj, "ai_coordinator", None) if runtime_obj is not None else None
        if coordinator is not None and callable(getattr(coordinator, "status", None)):
            try:
                ai_status = dict(coordinator.status())
            except Exception as exc:
                ai_status = {"status": UNKNOWN, "reason": str(exc)[:240]}
        else:
            ai_status = {
                "status": "UNAVAILABLE",
                "required_model": "qwen3.5:9b",
                "reason_code": "SMART_MODEL_UNAVAILABLE_OR_RUNTIME_UNAVAILABLE",
            }

        reconciliation_times: list[tuple[str, str]] = []
        for order in orders:
            evidence = order.get("execution_evidence") or {}
            observed = _iso(evidence.get("observed_at") or order.get("updated_at"))
            if observed and order.get("status") not in {"UNKNOWN", "CREATED", "RISK_APPROVED"}:
                reconciliation_times.append((observed, "execution_adapter_response"))
        if scope["mode"] == "PAPER":
            with self.store._connect() as db:
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_fills'"
                ).fetchone()
                if exists:
                    row = db.execute(
                        "SELECT created_at FROM trade_fills WHERE account_id=? AND venue=? AND mode=? ORDER BY created_at DESC LIMIT 1",
                        (account_id, scope["venue"], scope["mode"]),
                    ).fetchone()
                    if row:
                        reconciliation_times.append((str(row["created_at"]), "paper_ledger"))
        reconciliation_times.sort(reverse=True)
        if reconciliation_times:
            reconciliation = {
                "status": "RECONCILED",
                "last_reconciled_at": reconciliation_times[0][0],
                "source": reconciliation_times[0][1],
            }
        else:
            reconciliation = {
                "status": UNKNOWN,
                "last_reconciled_at": None,
                "source": "NO_RECONCILIATION_EVIDENCE",
            }

        max_portfolio = _decimal(risk.get("max_portfolio_risk_budget"))
        reserved = _decimal(snapshot.reserved_risk)
        unknown_capacity = bool(unknown_risk_items or snapshot.unverified_protection_count)
        if unknown_capacity:
            single_available: Any = UNKNOWN
            portfolio_available: Any = UNKNOWN
        else:
            single_available = str(
                max(Decimal("0"), snapshot.net_equity * RiskEngine.TREND_RISK_FRACTION)
            )
            portfolio_available = str(max(Decimal("0"), max_portfolio - reserved - known_position_risk))
        blocked_reasons = list(risk.get("new_risk_block_reasons") or [])
        if unknown_risk_items and "UNKNOWN_RISK" not in blocked_reasons:
            blocked_reasons.append("UNKNOWN_RISK")
        if runtime_status.get("execution_blocked") and "RUNTIME_EXECUTION_BLOCKED" not in blocked_reasons:
            blocked_reasons.append("RUNTIME_EXECUTION_BLOCKED")
        if scope["mode"] in {"TESTNET", "LIVE"} and reconciliation["status"] == UNKNOWN:
            blocked_reasons.append("REMOTE_RECONCILIATION_UNKNOWN")
        return {
            "account_id": account_id,
            "venue": scope["venue"],
            "mode": scope["mode"],
            "as_of": now.isoformat(),
            "ledger_snapshot": snapshot.to_dict(),
            "risk": {
                **risk,
                "new_risk_blocked": bool(blocked_reasons),
                "new_risk_block_reasons": list(dict.fromkeys(blocked_reasons)),
            },
            "capacity": {
                "single_trade_risk_available": single_available,
                "portfolio_risk_available": portfolio_available,
                "known_position_risk": str(known_position_risk),
                "reserved_risk": str(reserved),
                "unknown_risk_items": unknown_risk_items,
                "status": UNKNOWN if unknown_capacity else "CALCULATED_FROM_LEDGER",
                "basis": "ACCOUNT_LEDGER_AND_SCOPED_RESERVATIONS",
            },
            "concentration": concentration,
            "positions": positions,
            "orders": orders,
            "reservations": reservations,
            "protections": protections,
            "market_data": {
                "marks": marks,
                "by_symbol": mark_evidence,
                "status": "FRESH" if len(marks) == len({str(p.get("symbol") or "").upper() for p in positions if p.get("symbol")}) else UNKNOWN,
            },
            "reconciliation": reconciliation,
            "model": ai_status,
            "runtime": runtime_status,
            "legacy_data": {
                "account_scoped_unverified": legacy["account_scoped"],
                "unassigned_unverified": legacy["unassigned"],
                "management_policy": "EXCLUDED_AND_BLOCKED_UNTIL_EXPLICIT_MIGRATION",
            },
            "emergency_guidance": [
                "UNKNOWN means no authoritative external fact is available; do not infer a fill, equity, or protection state.",
                "If the application exits, PAPER protection is durable in the local ledger; remote protection must be reconciled by the venue adapter before new risk is allowed.",
                "When protection, reconciliation, lease, or model status is degraded, keep existing risk observable and use scoped reduce-only recovery through the gateway.",
            ],
        }

    # ------------------------------------------------------------------
    # Stored-data research pipeline
    # ------------------------------------------------------------------

    @staticmethod
    def _prediction_context(prediction: dict[str, Any]) -> dict[str, Any]:
        context = _json_object(prediction.get("context_json"))
        nested = _json_object(prediction.get("context"))
        return {**context, **nested}

    def _ledger_trade_records(
        self,
        scope: dict[str, str],
        *,
        strategy_id: str,
        strategy_version: str | None,
        symbol: str,
        timeframe: str,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        """Aggregate complete lifecycles from concrete fills only.

        ``predictions`` and ``outcomes`` are explanatory evidence, not an
        economic source.  This reader therefore starts at the shared ledger
        projection and excludes open/partial positions until their remaining
        quantity reaches zero.
        """

        try:
            pages: list[dict[str, Any]] = []
            page = 1
            while True:
                current = build_execution_ledger_projection(
                    self.store,
                    scope["account_id"],
                    venue=scope["venue"],
                    mode=scope["mode"],
                    page=page,
                    page_size=500,
                )
                pages.append(current)
                if not (current.get("pagination") or {}).get("has_more"):
                    break
                page += 1
                if page > 10_000:
                    raise ValueError("LEDGER_PROJECTION_PAGINATION_UNBOUNDED")
            records = [record for item in pages for record in item.get("execution_records", [])]
            positions = {
                str(item.get("position_id")): item
                for item in (pages[0].get("positions", []) if pages else [])
                if item.get("position_id")
            }
        except Exception as exc:
            return [], [f"LEDGER_PROJECTION_ERROR:{type(exc).__name__}"], {"records_read": 0, "authoritative_fill_count": 0, "source": "execution_ledger"}

        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            position_id = str(record.get("position_id") or "").strip()
            if position_id:
                grouped.setdefault(position_id, []).append(record)
        trades: list[dict[str, Any]] = []
        issues: list[str] = []
        for position_id, lifecycle in grouped.items():
            entries = [item for item in lifecycle if item.get("economic_role") == "ENTRY"]
            exits = [item for item in lifecycle if item.get("economic_role") == "EXIT"]
            if not entries or not exits:
                continue
            entry_qty = sum(float(item.get("quantity") or 0.0) for item in entries)
            exit_qty = sum(float(item.get("quantity") or 0.0) for item in exits)
            if entry_qty <= 0 or exit_qty + 1e-12 < entry_qty:
                issues.append("OPEN_OR_PARTIAL_LIFECYCLE_EXCLUDED")
                continue
            first = sorted(entries, key=lambda item: str(item.get("event_at") or item.get("created_at") or ""))[0]
            settled = sorted(exits, key=lambda item: str(item.get("event_at") or item.get("created_at") or ""))[-1]
            entry_time = _parse_time(first.get("event_at") or first.get("created_at"))
            settled_time = _parse_time(settled.get("event_at") or settled.get("created_at"))
            if entry_time is None or settled_time is None or settled_time <= entry_time:
                issues.append("INVALID_LEDGER_LIFECYCLE_TIME")
                continue
            if window_start and entry_time < window_start or window_end and entry_time > window_end:
                continue
            record_strategy = str(first.get("strategy_id") or "UNKNOWN")
            if record_strategy != strategy_id:
                continue
            record_version = str(first.get("strategy_version") or "UNKNOWN")
            if strategy_version and record_version != strategy_version:
                continue
            if str(first.get("symbol") or "").upper() != symbol.upper():
                continue
            side = str(first.get("side") or "BUY").upper()
            sign = 1.0 if side in {"BUY", "LONG"} else -1.0
            entry_cost = sum(float(item.get("price") or 0.0) * float(item.get("quantity") or 0.0) for item in entries)
            exit_value = sum(float(item.get("price") or 0.0) * float(item.get("quantity") or 0.0) for item in exits)
            gross_pnl = (exit_value - entry_cost) * sign
            fee_values = [_number(item.get("fee"), non_negative=True) for item in lifecycle]
            fees_known = all(value is not None for value in fee_values)
            fees = sum(value for value in fee_values if value is not None) if fees_known else None
            position = positions.get(position_id) or {}
            stop = _number(position.get("stop_loss"), positive=True)
            entry_price = entry_cost / entry_qty if entry_qty else None
            risk = abs(entry_price - stop) * entry_qty if entry_price is not None and stop is not None else None
            if risk is None or risk <= 0:
                issues.append("LEDGER_LIFECYCLE_RISK_MISSING")
                continue
            slippage_values = [item.get("slippage_cost") for item in lifecycle]
            slippage_known = bool(slippage_values) and all(value is not None and _number(value, non_negative=True) is not None for value in slippage_values)
            net_pnl = gross_pnl - fees if fees is not None else None
            trades.append(
                {
                    "trade_id": position_id,
                    "pnl": net_pnl,
                    "gross_pnl": gross_pnl,
                    "fee": fees,
                    "risk": risk,
                    "entry_val": entry_cost,
                    "mae": position.get("mae_pct"),
                    "mfe": position.get("mfe_pct"),
                    "generated_at": entry_time.isoformat(),
                    "settled_at": settled_time.isoformat(),
                    "data_as_of": None,
                    "strategy_id": record_strategy,
                    "strategy_version": record_version,
                    "symbol": symbol.upper(),
                    "mode": scope["mode"],
                    "venue": scope["venue"],
                    "regime": UNKNOWN,
                    "slippage_cost": sum(float(value) for value in slippage_values) if slippage_known else None,
                    "cost_complete": fees_known and slippage_known,
                    "decision_path": first.get("decision_path") or "LEGACY_UNCONFIRMED",
                    "source": "execution_ledger",
                }
            )
        return trades, sorted(set(issues)), {
            "records_read": len(records),
            "records_used": len(trades),
            "authoritative_fill_count": len(records),
            "source": "execution_ledger",
            "strategy_values": sorted({value for value in (str(record.get("strategy_id") or "UNKNOWN") for record in records)}),
        }

    def _stored_trade_records(
        self,
        scope: dict[str, str],
        *,
        strategy_id: str,
        strategy_version: str | None,
        symbol: str,
        timeframe: str,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        if not callable(getattr(self.store, "list_prediction_records", None)):
            return [], ["PREDICTION_STORE_UNAVAILABLE"], {"records_read": 0}
        try:
            linked = self.store.list_prediction_records(
                symbol=symbol,
                timeframe=timeframe,
                limit=100000,
            )
        except Exception as exc:
            return [], [f"PREDICTION_STORE_ERROR:{type(exc).__name__}"], {"records_read": 0}
        trades: list[dict[str, Any]] = []
        issues: list[str] = []
        records_read = 0
        for record in linked or []:
            records_read += 1
            prediction = _json_object(record.get("prediction"))
            outcome = _json_object(record.get("outcome"))
            context = self._prediction_context(prediction)
            # Predictions are not assumed to belong to an account merely
            # because the requested symbol matches.  Historical records with
            # no durable account scope remain useful globally only after an
            # explicitly unscoped research task; this product endpoint is
            # account-scoped, so exclude them conservatively.
            record_account = prediction.get("account_id") or context.get("account_id")
            if str(record_account or "") != scope["account_id"]:
                continue
            record_mode = str(prediction.get("mode") or context.get("mode") or "").upper()
            record_venue = str(prediction.get("venue") or context.get("venue") or "").lower()
            if record_mode != scope["mode"] or record_venue != scope["venue"]:
                continue
            record_strategy = str(
                prediction.get("strategy_id")
                or context.get("strategy_id")
                or prediction.get("strategy")
                or ""
            )
            if record_strategy != strategy_id:
                continue
            record_version = prediction.get("strategy_version") or context.get("strategy_version")
            if strategy_version and str(record_version or "") != strategy_version:
                continue
            record_symbol = str(
                prediction.get("symbol")
                or _json_object(prediction.get("instrument")).get("symbol")
                or ""
            ).upper()
            if record_symbol != symbol.upper():
                continue
            generated_at = _parse_time(prediction.get("generated_at"))
            settled_at = _parse_time(outcome.get("settled_at"))
            data_as_of = _parse_time(prediction.get("data_as_of"))
            if generated_at is None or settled_at is None:
                issues.append("MISSING_DECISION_OR_OUTCOME_TIMESTAMP")
                continue
            if window_start and generated_at < window_start:
                continue
            if window_end and generated_at > window_end:
                continue
            if settled_at <= generated_at:
                issues.append("LOOKAHEAD_OR_INVALID_OUTCOME_ORDER")
                continue
            if data_as_of and data_as_of > generated_at:
                issues.append("DECISION_DATA_AS_OF_AFTER_GENERATION")
                continue
            action = str(prediction.get("action") or "").upper()
            if action in {"WAIT", "NOT_ACTIONABLE"}:
                continue
            if not outcome:
                continue
            realized_r = _number(outcome.get("realized_r"))
            # ``net_pnl`` is an explicit persisted contract and therefore
            # already includes the recorded costs.  Realized-R and the
            # legacy ``pnl`` field are only usable as gross evidence when no
            # explicit net value exists; observed fee/slippage is then
            # deducted exactly once below.
            explicit_net_pnl = _number(outcome.get("net_pnl"))
            gross_pnl = _number(
                outcome.get("gross_pnl", outcome.get("realized_pnl"))
            )
            pnl = explicit_net_pnl
            if pnl is None:
                pnl = gross_pnl
            if pnl is None:
                pnl = _number(outcome.get("pnl"))
            entry_low = _number(prediction.get("entry_low"))
            entry_high = _number(prediction.get("entry_high"))
            entry = _number(outcome.get("entry_price"), positive=True)
            if entry is None and entry_low is not None and entry_high is not None:
                entry = (entry_low + entry_high) / 2.0
            stop = _number(prediction.get("stop"), positive=True)
            quantity = _number(
                outcome.get("quantity", outcome.get("contracts", prediction.get("quantity"))),
                positive=True,
            )
            risk = _number(outcome.get("risk"), positive=True)
            if risk is None and entry is not None and stop is not None and quantity is not None:
                # A research record without a persisted quantity has no
                # defensible risk denominator.  Do not turn it into a
                # one-contract result merely to make the evaluator run.
                risk = abs(entry - stop) * quantity
            if pnl is None and realized_r is not None and risk is not None:
                pnl = realized_r * risk
            if risk is None or risk <= 0:
                issues.append("OUTCOME_PNL_OR_RISK_MISSING")
                continue
            fee_value = outcome.get("fee", outcome.get("fees", outcome.get("fee_amount")))
            fee = _number(fee_value, non_negative=True)
            slippage = _number(
                outcome.get("slippage_cost", outcome.get("slippage_fee", outcome.get("slippage"))),
                non_negative=True,
            )
            entry_value = _number(
                outcome.get("entry_val", outcome.get("notional")), non_negative=True
            )
            if entry_value is None and entry is not None and quantity is not None:
                entry_value = entry * quantity
            if fee is None:
                issues.append("OBSERVED_FEE_MISSING")
            if slippage is None:
                issues.append("OBSERVED_SLIPPAGE_MISSING")
            if entry_value is None:
                issues.append("NOTIONAL_MISSING_FOR_COST_STRESS")
            fee_present = fee_value is not None and fee is not None
            slippage_value = outcome.get(
                "slippage_cost",
                outcome.get("slippage_fee", outcome.get("slippage")),
            )
            slippage_present = slippage_value is not None and slippage is not None
            if explicit_net_pnl is None:
                if pnl is not None and fee is not None and slippage is not None:
                    pnl = float(pnl) - float(fee) - float(slippage)
                else:
                    pnl = None
            mae_value = _number(outcome.get("mae_r"))
            mfe_value = _number(outcome.get("mfe_r"))
            regime = str(
                prediction.get("market_regime")
                or context.get("market_regime")
                or prediction.get("regime")
                or UNKNOWN
            )
            trades.append(
                {
                    "trade_id": str(prediction.get("prediction_id") or record.get("prediction_id") or uuid.uuid4().hex),
                    "pnl": float(pnl) if pnl is not None else None,
                    "gross_pnl": float(pnl) + float(fee) + float(slippage) if pnl is not None and fee is not None and slippage is not None else None,
                    "fee": float(fee) if fee is not None else None,
                    "risk": float(risk),
                    "entry_val": float(entry_value) if entry_value is not None else None,
                    "mae": abs(mae_value) if mae_value is not None else None,
                    "mfe": abs(mfe_value) if mfe_value is not None else None,
                    "generated_at": generated_at.isoformat(),
                    "settled_at": settled_at.isoformat(),
                    "data_as_of": data_as_of.isoformat() if data_as_of else None,
                    "strategy_id": record_strategy,
                    "strategy_version": str(record_version or UNKNOWN),
                    "symbol": record_symbol,
                    "mode": record_mode,
                    "venue": record_venue,
                    "regime": regime,
                    "slippage_cost": float(slippage) if slippage is not None else None,
                    "cost_complete": fee_present and slippage_present,
                    "prediction_id": prediction.get("prediction_id"),
                }
            )
        return trades, sorted(set(issues)), {
            "records_read": records_read,
            "records_used": len(trades),
        }

    @staticmethod
    def _eval(trades: list[dict[str, Any]], *, strategy_id: str, min_samples: int, group: dict[str, Any]) -> dict[str, Any]:
        return dict(
            evaluate_strategy_effectiveness(
                trades=trades,
                strategy_id=strategy_id,
                min_samples=min_samples,
                group_dimensions=group,
                market_context=group,
            )
        )

    def _persist_research_task(
        self,
        task_id: str,
        scope: dict[str, str],
        config: dict[str, Any],
        *,
        status: str,
        result: dict[str, Any] | None,
        created_at: str,
    ) -> None:
        with self.store._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO research_evaluation_tasks
                   (task_id, account_id, venue, mode, strategy_id, strategy_version,
                    symbol, timeframe, window_start, window_end, status, config_json,
                    result_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    scope["account_id"],
                    scope["venue"],
                    scope["mode"],
                    config["strategy_id"],
                    config.get("strategy_version"),
                    config["symbol"],
                    config["timeframe"],
                    config.get("window_start"),
                    config.get("window_end"),
                    status,
                    _json(config),
                    _json(result) if result is not None else None,
                    created_at,
                    _now(self.clock).isoformat(),
                ),
            )

    def _stored_market_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        window_start: datetime | None,
        window_end: datetime | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Read only durable bars inside the requested research window."""
        if not callable(getattr(self.store, "list_market_bars", None)):
            return [], ["MARKET_BAR_STORE_UNAVAILABLE"]
        try:
            rows = self.store.list_market_bars(symbol, timeframe, limit=2000)
        except Exception as exc:
            return [], [f"MARKET_BAR_STORE_ERROR:{type(exc).__name__}"]
        selected: list[dict[str, Any]] = []
        issues: list[str] = []
        for row in rows or []:
            if not isinstance(row, dict):
                issues.append("MARKET_BAR_ROW_INVALID")
                continue
            point = _parse_time(row.get("bar_start") or row.get("timestamp"))
            if point is None:
                issues.append("MARKET_BAR_TIMESTAMP_MISSING")
                continue
            if window_start and point < window_start:
                continue
            if window_end and point > window_end:
                continue
            selected.append(dict(row))
        return selected, sorted(set(issues))

    def _run_bar_replay_suite(
        self,
        *,
        symbol: str,
        timeframe: str,
        strategy_id: str,
        strategy_version: str | None,
        bars: list[dict[str, Any]],
        bar_read_issues: list[str],
        train_fraction: float,
        min_samples: int,
        parameter_perturbations: list[dict[str, Any]],
        base_params: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the frozen train/OOS, rolling, perturbation, and cost suite."""
        if not bars:
            return {
                "status": "NOT_RUN_MISSING_CLOSED_MARKET_BARS",
                "validation_type": "RETROSPECTIVE_SPLIT_ONLY",
                "reason": "A durable closed-bar input is required for causal replay; prediction/outcome rows are not substituted.",
                "bar_read_issues": bar_read_issues,
                "parameter_perturbation": {
                    "status": "NOT_RUN_REPLAY_REQUIRED" if parameter_perturbations else "NOT_REQUESTED",
                    "requested": parameter_perturbations,
                    "reason": "Parameter perturbation requires replaying stored market bars through the production strategy.",
                },
            }

        try:
            full = run_strategy_bar_replay(
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                bars=bars,
                params=base_params,
            )
        except (StrategyReplayError, ValueError, TypeError) as exc:
            return {
                "status": "NOT_RUN_REPLAY_ERROR",
                "validation_type": "FROZEN_OOS_BAR_REPLAY",
                "reason": str(exc)[:240],
                "bar_read_issues": bar_read_issues,
                "parameter_perturbation": {
                    "status": "NOT_RUN_REPLAY_ERROR" if parameter_perturbations else "NOT_REQUESTED",
                    "requested": parameter_perturbations,
                },
            }
        if full.get("status") != "EVALUATED":
            return {
                "status": full.get("status", "NOT_RUN_REPLAY_REQUIRED"),
                "validation_type": "FROZEN_OOS_BAR_REPLAY",
                "reason": full.get("reason", "closed bar replay did not run"),
                "bar_read_issues": bar_read_issues,
                "baseline": full,
                "parameter_perturbation": {
                    "status": "NOT_RUN_REPLAY_REQUIRED" if parameter_perturbations else "NOT_REQUESTED",
                    "requested": parameter_perturbations,
                },
            }

        bar_count = int((full.get("bars") or {}).get("count") or len(bars))
        warmup = int(full.get("warmup_bars") or 0)
        split_index = max(warmup, min(bar_count - 2, int(math.floor(bar_count * train_fraction))))
        purge_bars = 1
        embargo_bars = 1
        oos_start = min(bar_count - 2, split_index + purge_bars + embargo_bars)
        try:
            train_replay = run_strategy_bar_replay(
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                bars=bars,
                params=base_params,
                signal_start_index=warmup - 1,
                signal_end_index=split_index - purge_bars - 1,
            )
            oos_replay = run_strategy_bar_replay(
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                bars=bars,
                params=base_params,
                signal_start_index=oos_start,
            )
        except (StrategyReplayError, ValueError, TypeError) as exc:
            return {
                "status": "NOT_RUN_REPLAY_ERROR",
                "validation_type": "FROZEN_OOS_BAR_REPLAY",
                "reason": str(exc)[:240],
                "baseline": full,
                "bar_read_issues": bar_read_issues,
                "parameter_perturbation": {
                    "status": "NOT_RUN_REPLAY_ERROR" if parameter_perturbations else "NOT_REQUESTED",
                    "requested": parameter_perturbations,
                },
            }

        overall_group = {
            "strategy_id": strategy_id,
            "strategy_version": oos_replay.get("strategy_version", strategy_version or UNKNOWN),
            "symbol": symbol,
            "mode": "PAPER_REPLAY",
            "venue": "local_replay",
            "validation": "FROZEN_OOS_BAR_REPLAY",
        }
        train_trades_all = list(train_replay.get("trades") or []) if isinstance(train_replay, dict) else []
        # A signal close to the split may settle after the nominal train
        # window.  Verify purge/embargo against each replay trade's durable
        # settlement timestamp instead of treating an index label as proof.
        ordered_starts = sorted(
            parsed
            for parsed in (
                _parse_time(item.get("bar_start") or item.get("timestamp"))
                for item in bars
            )
            if parsed is not None
        )
        test_boundary = ordered_starts[oos_start] if oos_start < len(ordered_starts) else None
        excluded_train_trade_ids: list[str] = []
        train_trades: list[dict[str, Any]] = []
        for trade in train_trades_all:
            settled_at = _parse_time(trade.get("settled_at"))
            if test_boundary is not None and settled_at is not None and settled_at < test_boundary:
                train_trades.append(trade)
            else:
                excluded_train_trade_ids.append(str(trade.get("trade_id") or "UNKNOWN"))
        oos_trades = list(oos_replay.get("trades") or []) if isinstance(oos_replay, dict) else []

        def evaluation(items: list[dict[str, Any]]) -> dict[str, Any]:
            return self._eval(items, strategy_id=strategy_id, min_samples=min_samples, group=overall_group)

        rolling: list[dict[str, Any]] = []
        rolling_span = max(1, min(32, bar_count - oos_start))
        cursor = oos_start
        while cursor <= bar_count - 2:
            segment_end = min(bar_count - 2, cursor + rolling_span - 1)
            try:
                segment = run_strategy_bar_replay(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    bars=bars,
                    params=base_params,
                    signal_start_index=cursor,
                    signal_end_index=segment_end,
                )
            except (StrategyReplayError, ValueError, TypeError) as exc:
                rolling.append({"status": "NOT_RUN_REPLAY_ERROR", "reason": str(exc)[:240], "start_index": cursor, "end_index": segment_end})
                break
            rolling.append(
                {
                    "status": segment.get("status"),
                    "start_index": cursor,
                    "end_index": segment_end,
                    "input_hash": segment.get("input_hash"),
                    "output_hash": segment.get("output_hash"),
                    "evaluation": evaluation(list(segment.get("trades") or [])),
                }
            )
            cursor = segment_end + 1

        variants: list[dict[str, Any]] = []
        for ordinal, request in enumerate(parameter_perturbations):
            raw_parameters = request.get("parameters") if isinstance(request, dict) and "parameters" in request else request
            if not isinstance(raw_parameters, dict):
                variants.append({"ordinal": ordinal, "status": "NOT_RUN_PARAMETER_INVALID", "requested": request})
                continue
            candidate_params = dict(raw_parameters)
            try:
                variant = run_strategy_bar_replay(
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    bars=bars,
                    params=candidate_params,
                    signal_start_index=oos_start,
                )
                variants.append(
                    {
                        "ordinal": ordinal,
                        "status": "EVALUATED" if variant.get("status") == "EVALUATED" else variant.get("status"),
                        "parameters": candidate_params,
                        "parameters_hash": variant.get("parameters_hash"),
                        "recomputed": True,
                        "replay": variant,
                        "evaluation": evaluation(list(variant.get("trades") or [])) if variant.get("status") == "EVALUATED" else None,
                        "differs_from_baseline": variant.get("output_hash") != oos_replay.get("output_hash"),
                    }
                )
            except (StrategyReplayError, ValueError, TypeError) as exc:
                variants.append({"ordinal": ordinal, "status": "NOT_RUN_PARAMETER_INVALID", "parameters": candidate_params, "reason": str(exc)[:240]})
        if not parameter_perturbations:
            parameter_result = {"status": "NOT_REQUESTED", "requested": [], "variants": []}
        elif any(item.get("status") == "EVALUATED" for item in variants):
            parameter_result = {
                "status": "EVALUATED",
                "requested": parameter_perturbations,
                "baseline_output_hash": oos_replay.get("output_hash"),
                "variants": variants,
                "execution": "EACH_VARIANT_REPLAYED_THROUGH_PRODUCTION_STRATEGY_AND_NEXT_BAR_MATCHER",
            }
        else:
            parameter_result = {"status": "NOT_RUN_PARAMETER_INVALID", "requested": parameter_perturbations, "variants": variants}

        try:
            stressed = run_strategy_bar_replay(
                symbol=symbol,
                timeframe=timeframe,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                bars=bars,
                params=base_params,
                signal_start_index=oos_start,
                fee_rate=0.001,
                slippage_bps=4.0,
            )
            cost_stress = {
                "status": "EVALUATED" if stressed.get("status") == "EVALUATED" else stressed.get("status"),
                "scenario": "2X_FEE_AND_2X_SLIPPAGE",
                "fee_rate": 0.001,
                "slippage_bps": 4.0,
                "replay": stressed,
                "evaluation": evaluation(list(stressed.get("trades") or [])) if stressed.get("status") == "EVALUATED" else None,
            }
        except (StrategyReplayError, ValueError, TypeError) as exc:
            cost_stress = {"status": "NOT_RUN_REPLAY_ERROR", "scenario": "2X_FEE_AND_2X_SLIPPAGE", "reason": str(exc)[:240]}

        return {
            "status": "EVALUATED" if oos_replay.get("status") == "EVALUATED" else oos_replay.get("status", "NOT_RUN_REPLAY_REQUIRED"),
            "validation_type": "FROZEN_OOS_BAR_REPLAY",
            "bar_read_issues": bar_read_issues,
            "input": {
                "bar_count": bar_count,
                "input_hash": full.get("input_hash"),
                "closed_bars_only": True,
                "strategy_id": strategy_id,
                "strategy_version": oos_replay.get("strategy_version", strategy_version or UNKNOWN),
                "parameters": base_params,
                "parameters_hash": full.get("parameters_hash"),
                "fee_rate": full.get("costs", {}).get("fee_rate"),
                "slippage_bps": full.get("costs", {}).get("slippage_bps"),
            },
            "frozen_split": {
                "train_end_index": split_index - purge_bars - 1,
                "purge_bars": purge_bars,
                "embargo_bars": embargo_bars,
                "test_start_index": oos_start,
                "parameter_selection": "FROZEN_EXPLICIT_PARAMETERS_NO_OOS_FIT",
                "train": {
                    "replay": train_replay,
                    "evaluation": evaluation(train_trades),
                    "settlement_filter": {
                        "status": "APPLIED",
                        "boundary": _iso(test_boundary),
                        "raw_trade_count": len(train_trades_all),
                        "used_trade_count": len(train_trades),
                        "excluded_trade_ids": excluded_train_trade_ids,
                        "rule": "settled_at_before_test_boundary",
                    },
                },
                "out_of_sample": {"replay": oos_replay, "evaluation": evaluation(oos_trades)},
            },
            "rolling": rolling,
            "parameter_perturbation": parameter_result,
            "cost_stress": cost_stress,
            "future_data_invariance": oos_replay.get("future_data_invariance") or full.get("future_data_invariance"),
            "baseline": full,
        }

    def evaluate_stored_strategy(
        self,
        *,
        account_id: str,
        strategy_id: str,
        strategy_version: str | None = None,
        symbol: str,
        mode: str | None = None,
        venue: str | None = None,
        timeframe: str = "15m",
        window_start: datetime | str | None = None,
        window_end: datetime | str | None = None,
        train_fraction: float = 0.7,
        min_samples: int = 100,
        parameter_perturbations: list[dict[str, Any]] | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(account_id, mode=mode, venue=venue)
        strategy_id = str(strategy_id).strip()
        symbol = str(symbol).strip().upper()
        timeframe = str(timeframe).strip().lower()
        if not strategy_id or not symbol or not timeframe:
            raise TraderCapabilityError("RESEARCH_SCOPE_REQUIRED", "strategy_id, symbol, and timeframe are required.")
        try:
            train_fraction = float(train_fraction)
            min_samples = int(min_samples)
        except (TypeError, ValueError) as exc:
            raise TraderCapabilityError("RESEARCH_CONFIG_INVALID", "Research split and sample gate are invalid.") from exc
        if not 0.5 <= train_fraction < 1.0 or min_samples < 1 or min_samples > 100000:
            raise TraderCapabilityError("RESEARCH_CONFIG_INVALID", "train_fraction or min_samples is outside the supported bound.")
        start = _parse_time(window_start)
        end = _parse_time(window_end)
        if window_start is not None and start is None or window_end is not None and end is None:
            raise TraderCapabilityError("RESEARCH_WINDOW_INVALID", "Research window timestamps must be timezone-aware ISO values.")
        if start and end and end <= start:
            raise TraderCapabilityError("RESEARCH_WINDOW_INVALID", "window_end must be after window_start.")
        created_at = _now(self.clock).isoformat()
        task_id = f"eval_{uuid.uuid4().hex[:14]}"
        config = {
            "account_id": account_id,
            "venue": scope["venue"],
            "mode": scope["mode"],
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "symbol": symbol,
            "timeframe": timeframe,
            "window_start": _iso(start),
            "window_end": _iso(end),
            "train_fraction": train_fraction,
            "min_samples": min_samples,
            "parameter_perturbations": parameter_perturbations or [],
            "parameters": dict(parameters or {}),
            "input_type": "AUTHORITATIVE_EXECUTION_LEDGER_WITH_LEGACY_COMPATIBILITY_FALLBACK",
        }
        self._persist_research_task(task_id, scope, config, status="RUNNING", result=None, created_at=created_at)
        ledger_trades, ledger_issues, ledger_meta = self._ledger_trade_records(
            scope,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=symbol,
            timeframe=timeframe,
            window_start=start,
            window_end=end,
        )
        if ledger_meta.get("authoritative_fill_count", 0):
            trades, issues, read_meta = ledger_trades, ledger_issues, ledger_meta
            research_source = "execution_ledger"
        else:
            # V1 compatibility is intentionally explicit for historical
            # tests/data imported before the unified fill ledger existed.  A
            # v3 caller can require authoritative-only input; the legacy path
            # is never mixed with concrete fills.
            trades, issues, read_meta = self._stored_trade_records(
                scope,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                symbol=symbol,
                timeframe=timeframe,
                window_start=start,
                window_end=end,
            )
            issues = sorted(set(issues + ledger_issues))
            read_meta = {**read_meta, "authoritative_fill_count": 0, "source": "legacy_prediction_outcome_compatibility"}
            research_source = "legacy_prediction_outcome_compatibility"
        market_bars, market_bar_issues = self._stored_market_bars(
            symbol=symbol,
            timeframe=timeframe,
            window_start=start,
            window_end=end,
        )
        config["market_bar_count"] = len(market_bars)
        config["market_bar_issues"] = market_bar_issues
        trades.sort(key=lambda item: (item["generated_at"], item["trade_id"]))
        group_map: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        for trade in trades:
            key = (
                trade["strategy_id"],
                trade["strategy_version"],
                trade["symbol"],
                trade["mode"],
                trade["regime"],
            )
            group_map.setdefault(key, []).append(trade)
        group_results: list[dict[str, Any]] = []
        for key, items in group_map.items():
            group = {
                "strategy_id": key[0],
                "strategy_version": key[1],
                "symbol": key[2],
                "mode": key[3],
                "venue": scope["venue"],
                "regime": key[4],
            }
            group_results.append(
                {
                    "dimensions": group,
                    "sample_count": len(items),
                    "evaluation": self._eval(items, strategy_id=strategy_id, min_samples=min_samples, group=group),
                }
            )
        if trades:
            split = max(1, min(len(trades) - 1, int(len(trades) * train_fraction))) if len(trades) > 1 else len(trades)
            train = trades[:split]
            test = trades[split:]
        else:
            train, test = [], []
        overall_group = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version or UNKNOWN,
            "symbol": symbol,
            "mode": scope["mode"],
            "venue": scope["venue"],
            "regime": "MIXED_OR_UNKNOWN",
        }
        full_eval = self._eval(trades, strategy_id=strategy_id, min_samples=min_samples, group=overall_group)
        oos_min = max(1, min(min_samples, int(math.ceil(min_samples * 0.3))))
        oos_eval = self._eval(test, strategy_id=strategy_id, min_samples=oos_min, group={**overall_group, "split": "OUT_OF_SAMPLE"})
        rolling: list[dict[str, Any]] = []
        rolling_size = max(2, min(len(trades), max(min_samples, 20)))
        if len(trades) >= 2:
            for start_index in range(0, len(trades), rolling_size):
                window = trades[start_index : start_index + rolling_size]
                if len(window) < 2:
                    continue
                rolling.append(
                    {
                        "start": window[0]["generated_at"],
                        "end": window[-1]["settled_at"],
                        "evaluation": self._eval(
                            window,
                            strategy_id=strategy_id,
                            min_samples=min_samples,
                            group={**overall_group, "validation": "ROLLING"},
                        ),
                    }
                )
        cost_complete = bool(trades) and all(bool(item.get("cost_complete")) for item in trades)
        stress_trades = []
        for trade in trades:
            stressed = dict(trade)
            observed_fee = float(trade["fee"]) if trade.get("fee") is not None else None
            observed_slippage = float(trade["slippage_cost"]) if trade.get("slippage_cost") is not None else None
            stressed["pnl"] = (
                float(trade["pnl"]) - (observed_fee or 0.0) - (observed_slippage or 0.0)
                if trade.get("pnl") is not None else None
            )
            stressed["fee"] = observed_fee * 2.0 if observed_fee is not None else None
            stress_trades.append(stressed)
        cost_stress = {
            "status": "EVALUATED" if cost_complete else "NOT_RUN_MISSING_OBSERVED_COSTS",
            "scenario": "2X_OBSERVED_FEE_PLUS_OBSERVED_SLIPPAGE",
            "evaluation": self._eval(stress_trades, strategy_id=strategy_id, min_samples=min_samples, group={**overall_group, "validation": "COST_STRESS_2X"}) if cost_complete else None,
        }
        if parameters is not None:
            base_params = dict(parameters)
        else:
            try:
                from core.quant.strategies import STRATEGIES
                base_params = dict(STRATEGIES[strategy_id]().params)
            except (KeyError, TypeError, ValueError):
                base_params = {}
        # Persist the effective parameter set before hashing/persisting the
        # result. An empty request must not make production defaults look
        # like an unparameterized experiment.
        config["parameters"] = dict(base_params)
        bar_replay = self._run_bar_replay_suite(
            symbol=symbol,
            timeframe=timeframe,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            bars=market_bars,
            bar_read_issues=market_bar_issues,
            train_fraction=train_fraction,
            min_samples=min_samples,
            parameter_perturbations=parameter_perturbations or [],
            base_params=base_params,
        )
        parameter_result = bar_replay.get("parameter_perturbation") or {
            "status": "NOT_RUN_REPLAY_REQUIRED" if parameter_perturbations else "NOT_REQUESTED",
            "requested": parameter_perturbations or [],
        }
        has_enough_samples = len(trades) >= min_samples
        if not trades:
            overall_status = "EVIDENCE_INSUFFICIENT"
        elif not has_enough_samples or not cost_complete:
            overall_status = "EVIDENCE_INSUFFICIENT"
        else:
            overall_status = "EVALUATED"
        data_start = trades[0]["generated_at"] if trades else None
        data_end = trades[-1]["settled_at"] if trades else None
        result = {
            "task_id": task_id,
            "status": overall_status,
            "evidence_status": overall_status,
            "account_id": account_id,
            "venue": scope["venue"],
            "mode": scope["mode"],
            "strategy_id": strategy_id,
            "strategy_version": strategy_version or UNKNOWN,
            "symbol": symbol,
            "timeframe": timeframe,
            "sample_count": len(trades),
            "sample_gate": {
                "minimum": min_samples,
                "passed": has_enough_samples,
                "cost_data_complete": cost_complete,
            },
            "window": {
                "requested_start": _iso(start),
                "requested_end": _iso(end),
                "observed_start": data_start,
                "observed_end": data_end,
            },
            "full_sample": full_eval,
            "groups": group_results,
            "temporal_validation": {
                "train_fraction": train_fraction,
                "train_sample_count": len(train),
                "out_of_sample_sample_count": len(test),
                "train": self._eval(train, strategy_id=strategy_id, min_samples=min_samples, group={**overall_group, "split": "TRAIN"}),
                "out_of_sample": oos_eval,
                "no_lookahead": True,
                # Retain the historical compatibility flag used by the v1.3
                # API while making the evidentiary distinction explicit.  A
                # prediction/outcome split alone is not a causal proof.
                "no_lookahead_proven": bar_replay.get("status") == "EVALUATED" and bar_replay.get("validation_type") == "FROZEN_OOS_BAR_REPLAY",
                "validation_type": "FROZEN_OOS_BAR_REPLAY" if bar_replay.get("status") == "EVALUATED" else "RETROSPECTIVE_SPLIT",
                "retrospective_split_is_not_training_proof": True,
                "settlement_after_generation_required": True,
            },
            "rolling_validation": rolling,
            "bar_replay": bar_replay,
            "cost_after_metrics": {
                "observed_costs": {
                    "status": "AVAILABLE" if cost_complete else UNKNOWN,
                    "fee_source": "STORED_OUTCOME_FEE" if cost_complete else UNKNOWN,
                    "slippage_source": "STORED_OUTCOME_SLIPPAGE" if cost_complete else UNKNOWN,
                },
                "stress": cost_stress,
                "bar_replay_stress": bar_replay.get("cost_stress"),
            },
            "parameter_perturbation": parameter_result,
            "data_quality": {
                "issues": sorted(set(issues)),
                "records_read": read_meta.get("records_read", 0),
                "records_used": read_meta.get("records_used", 0),
                "market_bars_read": len(market_bars),
                "market_bar_issues": market_bar_issues,
                "source": research_source,
                "authoritative_fill_count": read_meta.get("authoritative_fill_count", 0),
                "legacy_compatibility_used": research_source != "execution_ledger",
            },
            "provenance": {
                "input": research_source,
                "trade_ids": [item["trade_id"] for item in trades],
                "config_hash": _hash(config),
                "generated_at_order": "ASC",
                "arbitrary_trade_array_accepted": False,
                "parameters": base_params,
                "bar_replay_input_hash": (bar_replay.get("input") or {}).get("input_hash"),
            },
        }
        self._persist_research_task(task_id, scope, config, status=overall_status, result=result, created_at=created_at)
        return result

    def get_evaluation_task(self, account_id: str, task_id: str) -> dict[str, Any]:
        scope = self._scope(account_id)
        with self.store._connect() as db:
            row = db.execute(
                "SELECT * FROM research_evaluation_tasks WHERE task_id=? AND account_id=? AND venue=? AND mode=?",
                (task_id, account_id, scope["venue"], scope["mode"]),
            ).fetchone()
        if row is None:
            raise TraderCapabilityError("RESEARCH_TASK_NOT_FOUND", "Research task is not in the requested account scope.", 404)
        return {
            "task_id": row["task_id"],
            "account_id": row["account_id"],
            "venue": row["venue"],
            "mode": row["mode"],
            "status": row["status"],
            "config": _json_object(row["config_json"]),
            "result": _json_object(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Persisted, executable trade plans
    # ------------------------------------------------------------------

    def create_trade_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(payload.get("account_id") or "")
        scope = self._scope(account_id, mode=payload.get("mode"), venue=payload.get("venue"))
        plan_id = str(payload.get("plan_id") or f"plan_{uuid.uuid4().hex[:14]}")
        now = _now(self.clock)
        canonical_input = dict(payload)
        canonical_input["plan_id"] = plan_id
        try:
            clean = normalize_plan_payload(canonical_input, scope, now=now)
        except TradePlanContractError as exc:
            raise TraderCapabilityError(exc.code, exc.message) from exc
        clean["status"] = "WAIT" if clean["action"] == "WAIT" else "ARMED"
        clean["plan_hash"] = _hash(clean)
        now_iso = clean["created_at"] or now.isoformat()
        with self.store._connect() as db:
            db.execute(
                """INSERT INTO trader_trade_plans
                   (plan_id, account_id, venue, mode, position_id, symbol, action,
                    status, plan_version, payload_json, execution_result_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    plan_id,
                    account_id,
                    scope["venue"],
                    scope["mode"],
                    clean["position_id"],
                    clean["symbol"],
                    clean["action"],
                    clean["status"],
                    TRADE_PLAN_SCHEMA_VERSION,
                    _json(clean),
                    now_iso,
                    now_iso,
                ),
            )
        return clean

    def list_trade_plans(self, account_id: str, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        scope = self._scope(account_id)
        query = "SELECT * FROM trader_trade_plans WHERE account_id=? AND venue=? AND mode=?"
        params: list[Any] = [account_id, scope["venue"], scope["mode"]]
        if status:
            query += " AND status=?"
            params.append(str(status).upper())
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.store._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [
            {
                "plan_id": row["plan_id"],
                "account_id": row["account_id"],
                "venue": row["venue"],
                "mode": row["mode"],
                "status": row["status"],
                "plan": _json_object(row["payload_json"]),
                "execution_result": _json_object(row["execution_result_json"]) if row["execution_result_json"] else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def _update_plan(self, plan_id: str, account_id: str, status: str, result: dict[str, Any] | None = None) -> None:
        with self.store._connect() as db:
            db.execute(
                "UPDATE trader_trade_plans SET status=?, execution_result_json=?, updated_at=? WHERE plan_id=? AND account_id=?",
                (status, _json(result) if result is not None else None, _now(self.clock).isoformat(), plan_id, account_id),
            )

    def _plan_event_facts(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Resolve immutable news/event references at the execution boundary."""
        facts: dict[str, Any] = {}
        registry = NewsRevisionRegistry(self.store)
        revision_ids = [str(item) for item in (plan.get("news_revision_ids") or []) if item]
        for revision_id in revision_ids:
            revision = registry.get_revision(revision_id)
            if revision is None:
                facts[revision_id] = {"status": UNKNOWN, "revision_id": revision_id}
                continue
            latest = registry.get_revisions_for_news(revision.news_id)
            latest_revision = latest[-1] if latest else revision
            if latest_revision.revision_id != revision.revision_id:
                status = "CORRECTED"
            elif str(revision.claim_status).upper() in {"RETRACTED", "DISPUTED"}:
                status = str(revision.claim_status).upper()
            else:
                status = "ACTIVE"
            fact = {
                "status": status,
                "news_id": revision.news_id,
                "revision_id": revision.revision_id,
                "latest_revision_id": latest_revision.revision_id,
                "claim_status": revision.claim_status,
                "observed_at": _now(self.clock).isoformat(),
            }
            facts[revision_id] = fact
            facts[revision.news_id] = fact
        return facts

    def _plan_news_gate(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Check that a plan still refers to its immutable news revision."""
        registry = NewsRevisionRegistry(self.store)
        for revision_id in [str(item) for item in (plan.get("news_revision_ids") or []) if item]:
            revision = registry.get_revision(revision_id)
            if revision is None:
                return {"status": "BLOCKED_DATA", "reason": "NEWS_REVISION_NOT_FOUND", "revision_id": revision_id}
            revisions = registry.get_revisions_for_news(revision.news_id)
            latest = revisions[-1] if revisions else revision
            if latest.revision_id != revision.revision_id:
                return {
                    "status": "INVALIDATED",
                    "reason": "NEWS_REVISION_SUPERSEDED",
                    "revision_id": revision.revision_id,
                    "latest_revision_id": latest.revision_id,
                }
            if str(revision.claim_status).upper() in {"RETRACTED", "DISPUTED"}:
                return {"status": "INVALIDATED", "reason": "NEWS_REVISION_RETRACTED_OR_DISPUTED", "revision_id": revision.revision_id}
            if str(revision.claim_status).upper() not in {"PRIMARY_SOURCE_VERIFIED", "CORROBORATED"}:
                return {
                    "status": "BLOCKED_DATA",
                    "reason": "NEWS_REVISION_UNVERIFIED",
                    "revision_id": revision.revision_id,
                    "claim_status": revision.claim_status,
                }
        return {"status": "VALID", "reason": "NO_SUPERSEDED_NEWS_REVISION"}

    def process_armed_trade_plans(
        self,
        account_id: str,
        symbol: str,
        market_snapshot: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Runtime entry point that consumes ARMED plans on fresh market events."""
        scope = self._scope(account_id)
        normalized_symbol = str(symbol).strip().upper()
        with self.store._connect() as db:
            rows = db.execute(
                """SELECT plan_id, payload_json FROM trader_trade_plans
                   WHERE account_id=? AND venue=? AND mode=? AND symbol=?
                     AND status IN ('ARMED','WAITING_TRIGGER','BLOCKED_DATA')
                   ORDER BY created_at ASC""",
                (account_id, scope["venue"], scope["mode"], normalized_symbol),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            plan = _json_object(row["payload_json"])
            # A blocked-data plan is re-evaluated only after a new event; it
            # never bypasses the same deterministic condition check.
            results.append(
                self.execute_trade_plan(
                    account_id,
                    str(row["plan_id"]),
                    market_snapshot=market_snapshot,
                    now=now,
                    event_facts=self._plan_event_facts(plan),
                )
            )
        return results

    def execute_trade_plan(
        self,
        account_id: str,
        plan_id: str,
        *,
        market_snapshot: dict[str, Any] | None = None,
        event_facts: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(account_id)
        with self.store._connect() as db:
            row = db.execute(
                "SELECT * FROM trader_trade_plans WHERE plan_id=? AND account_id=? AND venue=? AND mode=?",
                (plan_id, account_id, scope["venue"], scope["mode"]),
            ).fetchone()
        if row is None:
            raise TraderCapabilityError("PLAN_NOT_FOUND", "Trade plan is not in the requested account scope.", 404)
        plan = _json_object(row["payload_json"])
        action = str(plan.get("action") or "WAIT").upper()
        existing_status = str(row["status"] or "").upper()
        if existing_status in {"EXECUTED", "SUBMITTED"} and row["execution_result_json"]:
            prior = _json_object(row["execution_result_json"])
            if prior:
                return prior
        if action == "WAIT":
            result = {
                "status": "WAIT",
                "plan_id": plan_id,
                "order_created": False,
                "reason": plan.get("why_not_waiting") or "formal_wait_result",
            }
            self._update_plan(plan_id, account_id, "WAIT", result)
            return result
        if action == "HOLD":
            result = {"status": "HOLD", "plan_id": plan_id, "order_created": False}
            self._update_plan(plan_id, account_id, "HOLD", result)
            return result
        if scope["mode"] == "LIVE":
            # The trade-plan consumer must expose the same release-policy
            # decision as the direct Gate order API. It cannot turn a LIVE
            # plan into a dry-run order or depend on a runtime/credential
            # error to provide the safety boundary.
            result = {
                "status": "NOT_RUN",
                "plan_id": plan_id,
                "order_created": False,
                "reason": "LIVE_DISABLED_BY_RELEASE_POLICY",
                "release_policy": "RELEASE_POLICY_LOCK_M0_TO_M3",
                "mode": scope["mode"],
                "venue": scope["venue"],
            }
            self._update_plan(plan_id, account_id, "NOT_RUN_EXTERNAL", result)
            return result
        if scope["mode"] != "PAPER":
            result = {
                "status": "NOT_RUN",
                "plan_id": plan_id,
                "order_created": False,
                "reason": "EXTERNAL_EXECUTION_REQUIRES_SEPARATE_AUTHORIZATION_AND_ADAPTER",
                "mode": scope["mode"],
                "venue": scope["venue"],
            }
            self._update_plan(plan_id, account_id, "NOT_RUN_EXTERNAL", result)
            return result
        # News identity is a durable safety fact, not an execution-side
        # optimization.  Resolve corrections/retractions before the runtime
        # availability gate so an already-invalid plan is recorded as
        # INVALIDATED even when no runtime is currently attached; this never
        # creates an order and keeps the reason actionable for operators.
        news_gate: dict[str, Any] | None = None
        if action not in {"REDUCE_POSITION", "CLOSE_POSITION"}:
            news_gate = self._plan_news_gate(plan)
            if news_gate.get("status") != "VALID":
                result = {
                    "status": news_gate.get("status", "BLOCKED_DATA"),
                    "plan_id": plan_id,
                    "order_created": False,
                    "reason": news_gate.get("reason"),
                    "news_evidence": news_gate,
                }
                self._update_plan(plan_id, account_id, str(result["status"]), result)
                return result
        if action not in {"REDUCE_POSITION", "CLOSE_POSITION"} and self.runtime is None:
            # Supplying a compatibility gateway is not a substitute for the
            # production runtime lease/session fence.  Opening plans must
            # enter through the same lifecycle used by API and AI decisions.
            result = {
                "status": "NOT_RUN",
                "plan_id": plan_id,
                "order_created": False,
                "reason": "RUNTIME_UNAVAILABLE_NEW_RISK_BLOCKED",
            }
            self._update_plan(plan_id, account_id, "NOT_RUN_RUNTIME", result)
            return result
        gateway = self.gateway
        if gateway is None:
            # An API/service request without the production runtime must not
            # create a new opening through an unfenced compatibility gateway.
            # Direct reduce-only recovery remains available to callers that
            # explicitly construct a gateway with an account-scoped ledger.
            if action not in {"REDUCE_POSITION", "CLOSE_POSITION"}:
                result = {
                    "status": "NOT_RUN",
                    "plan_id": plan_id,
                    "order_created": False,
                    "reason": "RUNTIME_UNAVAILABLE_NEW_RISK_BLOCKED",
                }
                self._update_plan(plan_id, account_id, "NOT_RUN_RUNTIME", result)
                return result
            gateway = ExecutionGateway(self.store)
        if action not in {"REDUCE_POSITION", "CLOSE_POSITION"} and self.runtime is not None:
            try:
                runtime_status = dict(self.runtime.status())
            except Exception as exc:
                result = {"status": "NOT_RUN", "plan_id": plan_id, "order_created": False, "reason": f"RUNTIME_STATUS_UNKNOWN:{type(exc).__name__}"}
                self._update_plan(plan_id, account_id, "NOT_RUN_RUNTIME", result)
                return result
            if (
                not runtime_status.get("active")
                or runtime_status.get("execution_blocked")
                or not (runtime_status.get("lease") or {}).get("valid", False)
                or str(runtime_status.get("account_id") or "") != account_id
            ):
                result = {
                    "status": "NOT_RUN",
                    "plan_id": plan_id,
                    "order_created": False,
                    "reason": "RUNTIME_EXECUTION_BLOCKED",
                    "runtime": runtime_status,
                }
                self._update_plan(plan_id, account_id, "NOT_RUN_RUNTIME", result)
                return result
        try:
            # ``None`` means "load the authoritative current quote".  An
            # explicitly supplied empty/malformed snapshot must not be
            # replaced by a hidden fallback, otherwise a caller could turn a
            # missing market fact into an executable plan by accident.
            market = market_snapshot if market_snapshot is not None else gateway._fresh_market_snapshot(str(plan["symbol"]))
            evaluated_at = now or _now(self.clock)
            if action not in {"REDUCE_POSITION", "CLOSE_POSITION"}:
                conditions_result = evaluate_plan_conditions(
                    plan,
                    market,
                    now=evaluated_at,
                    event_facts=event_facts if event_facts is not None else self._plan_event_facts(plan),
                )
                if conditions_result.get("status") != "TRIGGERED":
                    status = str(conditions_result.get("status") or "BLOCKED_DATA")
                    result = {
                        "status": status,
                        "plan_id": plan_id,
                        "order_created": False,
                        "reason": conditions_result.get("reason"),
                        "condition_evaluation": conditions_result,
                    }
                    self._update_plan(plan_id, account_id, status, result)
                    return result
            entry = _number(plan.get("entry_price"), positive=True)
            if entry is None:
                entry = _number(market.get("price"), positive=True)
            stop = _number(plan.get("stop_loss"), positive=True)
            ledger = AccountLedger(store=self.store)
            risk_engine = RiskEngine(ledger)
            reduce_only = action in {"REDUCE_POSITION", "CLOSE_POSITION"}
            if not reduce_only and (entry is None or stop is None):
                raise TraderCapabilityError("PLAN_EXECUTION_PRICE_UNKNOWN", "Fresh entry and stop facts are required to execute an opening plan.")
            position_id = plan.get("position_id")
            quantity: Decimal
            side: str
            leverage_value = _number(plan.get("leverage"), positive=True)
            leverage = int(leverage_value) if leverage_value is not None else 1
            quantity_source = "RISK_ENGINE" if not reduce_only else "EXPLICIT_PLAN_REDUCTION"
            target: dict[str, Any] | None = None
            if reduce_only:
                positions = ledger.get_open_positions(account_id, venue=scope["venue"], mode=scope["mode"])
                candidates = [
                    item for item in positions
                    if str(item.get("symbol") or "").upper() == str(plan["symbol"]).upper()
                    and (not position_id or str(item.get("position_id")) == str(position_id))
                ]
                if position_id is None and len(candidates) != 1:
                    raise TraderCapabilityError("PLAN_POSITION_ID_REQUIRED", "A plan must specify position_id when zero or multiple scoped positions match.")
                target = candidates[0] if len(candidates) == 1 else None
                if target is None:
                    raise TraderCapabilityError("PLAN_POSITION_NOT_FOUND", "Reduce plan has no matching scoped open position.")
                position_id = target.get("position_id")
                remaining = _decimal(target.get("remaining_contracts", target.get("quantity", 0)))
                if remaining <= 0:
                    raise TraderCapabilityError("PLAN_REDUCE_QUANTITY_ZERO", "The scoped position has no remaining quantity.")
                if action == "REDUCE_POSITION":
                    if plan.get("reduce_fraction") is None and plan.get("reduce_quantity") is None:
                        raise TraderCapabilityError("PLAN_REDUCE_FRACTION_REQUIRED", "A legacy REDUCE_POSITION plan needs explicit re-confirmation; no full-close default is allowed.")
                    if plan.get("reduce_fraction") is not None:
                        fraction = _number(plan.get("reduce_fraction"), positive=True)
                        if fraction is None or fraction > 1:
                            raise TraderCapabilityError("PLAN_REDUCE_FRACTION_INVALID", "reduce_fraction must be finite and in (0, 1].")
                        quantity = remaining * Decimal(str(fraction))
                    else:
                        quantity = _decimal(plan.get("reduce_quantity"))
                    if quantity <= 0 or quantity > remaining:
                        raise TraderCapabilityError("PLAN_REDUCE_QUANTITY_INVALID", "Requested reduction must be positive and no greater than the scoped remainder.")
                else:
                    # Full size is only legal because the action explicitly
                    # says CLOSE_POSITION; REDUCE_POSITION never falls into
                    # this branch by accident.
                    quantity = remaining
                    quantity_source = "EXPLICIT_CLOSE_POSITION"
                spec = dict(market.get("market") or market.get("metadata") or {})
                limits = dict(spec.get("limits") or {}).get("amount") or {}
                step = _decimal(limits.get("step", 0.001), "0.001")
                minimum = _decimal(limits.get("min", step), str(step))
                try:
                    quantity = round_down_quantity(quantity, step=step, minimum=minimum)
                except Exception as exc:
                    if isinstance(exc, TraderCapabilityError):
                        raise
                    code = getattr(exc, "code", "PLAN_REDUCE_QUANTITY_BELOW_MIN")
                    raise TraderCapabilityError(code, str(exc)) from exc
                if quantity >= remaining and action == "REDUCE_POSITION":
                    raise TraderCapabilityError("PLAN_USE_CLOSE_FOR_FULL_REDUCTION", "A reduction rounded to the full remainder; use explicit CLOSE_POSITION instead.")
                side = "SELL" if str(target.get("side")).upper() == "LONG" else "BUY"
                target_stop = _number(target.get("stop", target.get("stop_loss")), positive=True)
                if target_stop is not None:
                    stop = target_stop
                target_leverage = _number(target.get("leverage"), positive=True)
                if leverage_value is None and target_leverage is not None:
                    leverage = int(target_leverage)
            else:
                spec = dict(market.get("market") or market.get("metadata") or {})
                spec.setdefault("contractSize", market.get("contractSize", 1))
                spec.setdefault("limits", {"amount": {"min": 0.001, "max": 1_000_000, "step": 0.001}})
                equity = ledger.get_snapshot(account_id).net_equity
                budget = _decimal(plan.get("worst_loss_budget"))
                hard_budget = equity * RiskEngine.TREND_RISK_FRACTION
                budget = min(budget, hard_budget)
                risk_fraction = budget / equity if equity > 0 else Decimal("0")
                quantity, _, _ = risk_engine.calculate_position_size(
                    account_id,
                    entry,
                    stop,
                    spec,
                    risk_budget_pct=risk_fraction,
                    leverage=leverage,
                )
                if quantity <= 0:
                    raise TraderCapabilityError("PLAN_QUANTITY_ZERO", "Programmatic risk sizing produced no executable quantity.")
                side = "LONG" if action == "OPEN_LONG" else "SHORT"
            protection = ProtectionPlan(
                stop_price=stop,
                take_profit=_number(plan.get("take_profit"), positive=True),
                reduce_only=reduce_only,
                trade_plan_id=plan_id,
                time_exit_at=plan.get("time_exit_at"),
                partial_take_profits=list(plan.get("partial_take_profits") or []),
                trailing_protection=plan.get("trailing_protection"),
                event_invalidation=list(plan.get("event_invalidation") or []),
            ) if stop is not None else None
            intent = OrderIntent(
                intent_id=f"intent_plan_{plan_id}_{uuid.uuid4().hex[:8]}",
                idempotency_key=f"plan:{plan_id}",
                account_id=account_id,
                mode=TradingMode(scope["mode"]),
                instrument_id=str(plan["symbol"]),
                side=side,
                order_type="market",
                quantity=float(quantity),
                price=None,
                leverage=leverage,
                protection_plan=protection,
                reduce_only=reduce_only,
                control_mode=ControlMode.ASSISTED,
                decision_path=DecisionPath.STRATEGY_DRIVEN,
                strategy_id=plan.get("strategy_id"),
                strategy_version=plan.get("strategy_version"),
                venue=scope["venue"],
                environment=scope["mode"],
                position_id=position_id,
            )
            result = gateway.submit_intent(intent, market_snapshot=market)
            result = dict(result)
            result.setdefault("plan_execution", {})
            result["plan_execution"].update({
                "plan_id": plan_id,
                "schema_version": plan.get("schema_version", TRADE_PLAN_SCHEMA_VERSION),
                "quantity_source": quantity_source,
                "requested_reduce_fraction": plan.get("reduce_fraction"),
                "requested_reduce_quantity": plan.get("reduce_quantity"),
                "leverage": leverage,
                "leverage_source": "PLAN" if leverage_value is not None else "POSITION_OR_CONSERVATIVE_POLICY",
            })
        except TraderCapabilityError as exc:
            result = {"status": "BLOCKED", "plan_id": plan_id, "error_code": exc.code, "error": exc.message}
            self._update_plan(plan_id, account_id, "NEEDS_RECONFIRMATION" if exc.code in {"PLAN_REDUCE_FRACTION_REQUIRED", "PLAN_USE_CLOSE_FOR_FULL_REDUCTION"} else "BLOCKED", result)
            return result
        except GatewayError as exc:
            result = {"status": "REJECTED", "plan_id": plan_id, "error_code": exc.code, "error": exc.message}
            self._update_plan(plan_id, account_id, "REJECTED", result)
            return result
        except Exception as exc:
            result = {"status": "REJECTED", "plan_id": plan_id, "error_code": "PLAN_EXECUTION_FAILED", "error": str(exc)}
            self._update_plan(plan_id, account_id, "REJECTED", result)
            return result
        result["plan_id"] = plan_id
        order_status = str(result.get("status") or "UNKNOWN").upper()
        plan_status = "EXECUTED" if order_status in {"FILLED", "PARTIALLY_FILLED"} else "SUBMITTED" if order_status in {"ACKNOWLEDGED", "CREATED"} else "UNKNOWN" if order_status == "UNKNOWN" else "REJECTED"
        self._update_plan(plan_id, account_id, plan_status, result)
        return result

    # ------------------------------------------------------------------
    # News research and AI scorecard
    # ------------------------------------------------------------------

    def record_news_impact(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(payload.get("account_id") or "")
        scope = self._scope(account_id, mode=payload.get("mode"), venue=payload.get("venue"))
        news_id = str(payload.get("news_id") or "").strip()
        if not news_id:
            raise TraderCapabilityError("NEWS_ID_REQUIRED", "news_id is required.")
        registry = NewsRevisionRegistry(self.store)
        revision_id = str(payload.get("revision_id") or "")
        revision = registry.get_revision(revision_id) if revision_id else None
        if revision is None:
            revisions = registry.get_revisions_for_news(news_id)
            revision = revisions[-1] if revisions else None
        if revision is None or revision.news_id != news_id:
            raise TraderCapabilityError("NEWS_REVISION_NOT_FOUND", "The impact must reference an immutable stored news revision.", 404)
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise TraderCapabilityError("NEWS_SYMBOL_REQUIRED", "symbol is required.")
        direction = str(payload.get("direction") or UNKNOWN).upper()
        if direction not in {"LONG", "SHORT", "NEUTRAL", UNKNOWN}:
            raise TraderCapabilityError("NEWS_DIRECTION_INVALID", "direction must be LONG, SHORT, NEUTRAL, or UNKNOWN.")
        # The impact endpoint may receive analyst observations, but numeric
        # values alone are not evidence of a consensus surprise or market
        # absorption.  Keep them UNKNOWN until a versioned provenance bundle
        # supplies all three independent legs of the inference.
        inference_evidence = payload.get("inference_evidence")
        inference_supported = False
        inference_reason = "INFERENCE_EVIDENCE_MISSING"
        if isinstance(inference_evidence, dict):
            legs = ("consensus", "observation", "price_alignment")
            value_keys = {
                "consensus": ("value", "expected", "consensus"),
                "observation": ("value", "observed", "actual", "outcome"),
                "price_alignment": ("value", "price", "price_before", "price_after", "reference_price", "mark"),
            }

            def finite_observation(leg: dict[str, Any], keys: tuple[str, ...]) -> bool:
                for key in keys:
                    value = leg.get(key)
                    if value is None or isinstance(value, bool) or value == "":
                        continue
                    try:
                        return math.isfinite(float(value))
                    except (TypeError, ValueError, OverflowError):
                        continue
                return False

            legs_complete = all(
                isinstance(inference_evidence.get(leg), dict)
                and str(inference_evidence[leg].get("source") or "").strip()
                and _parse_time(inference_evidence[leg].get("as_of")) is not None
                and finite_observation(inference_evidence[leg], value_keys[leg])
                for leg in legs
            )
            expected_gap_candidate = _number(inference_evidence.get("expected_gap"))
            absorbed_candidate = inference_evidence.get("absorbed")
            absorbed_complete = isinstance(absorbed_candidate, bool) or str(absorbed_candidate or "").upper() in {"YES", "NO"}
            inference_supported = bool(legs_complete and expected_gap_candidate is not None and absorbed_complete)
            if not legs_complete:
                inference_reason = "CONSENSUS_OBSERVATION_OR_PRICE_ALIGNMENT_EVIDENCE_INCOMPLETE"
            elif expected_gap_candidate is None:
                inference_reason = "EXPECTED_GAP_EVIDENCE_MISSING_OR_INVALID"
            elif not absorbed_complete:
                inference_reason = "ABSORPTION_EVIDENCE_MISSING_OR_INVALID"
            expected_gap = expected_gap_candidate if inference_supported else None
            absorbed_raw = absorbed_candidate if inference_supported else None
        else:
            expected_gap = None
            absorbed_raw = None
        if isinstance(absorbed_raw, bool):
            absorbed = "YES" if absorbed_raw else "NO"
        elif str(absorbed_raw or "").upper() in {"YES", "NO"} and inference_supported:
            absorbed = str(absorbed_raw).upper()
        else:
            absorbed = UNKNOWN
        claim = str(revision.claim_status or "UNVERIFIED").upper()
        verification = "VERIFIED" if claim in {"PRIMARY_SOURCE_VERIFIED", "CORROBORATED"} else "UNVERIFIED"
        if expected_gap is None:
            expected_gap_value: Any = UNKNOWN
        else:
            expected_gap_value = expected_gap
        # This is deliberately a research qualification, never a trading
        # permission.  Risk limits and authorization remain independent.
        high_risk_allowed = False
        impact_id = str(payload.get("impact_id") or f"impact_{uuid.uuid4().hex[:14]}")
        effective_direction = direction if inference_supported else UNKNOWN
        effective_horizon = str(payload.get("horizon") or UNKNOWN) if inference_supported else UNKNOWN
        clean = {
            "impact_id": impact_id,
            "news_id": news_id,
            "revision_id": revision.revision_id,
            "account_id": account_id,
            "venue": scope["venue"],
            "mode": scope["mode"],
            "symbol": symbol,
            "direction": effective_direction,
            "horizon": effective_horizon,
            "expected_gap": expected_gap_value,
            "absorbed": absorbed,
            "invalidation": payload.get("invalidation") or [],
            "verification_status": verification,
            "claim_status": claim,
            "high_risk_trade_trigger_allowed": high_risk_allowed,
            "evidence_level": "INFERENCE_SUPPORTED" if inference_supported else ("SOURCE_VERIFIED" if verification == "VERIFIED" else "UNVERIFIED"),
            "inference_status": "INFERENCE_SUPPORTED" if inference_supported else UNKNOWN,
            "inference_reason": "EVIDENCE_BUNDLE_COMPLETE" if inference_supported else inference_reason,
            "inference_evidence": inference_evidence if isinstance(inference_evidence, dict) else {},
            "risk_permission": "NOT_GRANTED_BY_NEWS",
            "source": {
                "source_url": revision.source_url,
                "publisher": revision.publisher,
                "published_at": revision.published_at,
                "first_seen_at": revision.first_seen_at,
                "fetched_at": revision.fetched_at,
                "content_hash": revision.content_hash,
            },
            "facts_vs_inference": {
                "fact": {
                    "headline": revision.headline,
                    "claim_status": claim,
                },
                "inference": {
                    "direction": effective_direction,
                    "horizon": effective_horizon,
                    "expected_gap": expected_gap_value,
                    "absorbed": absorbed,
                    "status": "INFERENCE_SUPPORTED" if inference_supported else UNKNOWN,
                    "evidence": inference_evidence if isinstance(inference_evidence, dict) else {},
                },
            },
            "conflict_status": str(payload.get("conflict_status") or UNKNOWN).upper(),
            "created_at": _now(self.clock).isoformat(),
        }
        with self.store._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO trader_news_impacts
                   (impact_id, news_id, revision_id, account_id, venue, mode, symbol,
                    direction, horizon, expected_gap, absorbed, invalidation_json,
                    verification_status, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    impact_id,
                    news_id,
                    revision.revision_id,
                    account_id,
                    scope["venue"],
                    scope["mode"],
                    symbol,
                    clean["direction"],
                    clean["horizon"],
                    expected_gap,
                    absorbed,
                    _json(clean["invalidation"]),
                    verification,
                    _json(clean),
                    clean["created_at"],
                ),
            )
        return clean

    def get_news_research(self, account_id: str, news_id: str) -> dict[str, Any]:
        scope = self._scope(account_id)
        registry = NewsRevisionRegistry(self.store)
        revisions = registry.get_revisions_for_news(news_id)
        with self.store._connect() as db:
            rows = db.execute(
                """SELECT * FROM trader_news_impacts
                    WHERE account_id=? AND news_id=? AND venue=? AND mode=?
                    ORDER BY created_at ASC""",
                (account_id, news_id, scope["venue"], scope["mode"]),
            ).fetchall()
        return {
            "news_id": news_id,
            "account_id": account_id,
            "venue": scope["venue"],
            "mode": scope["mode"],
            "revisions": [revision.to_dict() for revision in revisions],
            "impacts": [_json_object(row["payload_json"]) for row in rows],
            "status": "AVAILABLE" if revisions else UNKNOWN,
        }

    def ai_scorecard(self, account_id: str, *, runtime: Any | None = None) -> dict[str, Any]:
        scope = self._scope(account_id)
        with self.store._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_led_cycles'"
            ).fetchone()
            rows = db.execute(
                "SELECT * FROM ai_led_cycles WHERE account_id=? ORDER BY created_at ASC LIMIT 1000",
                (account_id,),
            ).fetchall() if exists else []
            task_rows = db.execute(
                """SELECT * FROM research_evaluation_tasks
                    WHERE account_id=? AND venue=? AND mode=?
                    ORDER BY updated_at DESC LIMIT 100""",
                (account_id, scope["venue"], scope["mode"]),
            ).fetchall()
        decisions: list[dict[str, Any]] = []
        model_digests: set[str] = set()
        prompt_versions: set[str] = set()
        authorization_versions: set[str] = set()
        decision_errors = 0
        execution_errors = 0
        data_errors = 0
        for row in rows:
            payload = _json_object(row["payload_json"])
            execution = _json_object(payload.get("execution_result"))
            model_digest = payload.get("model_digest") or UNKNOWN
            prompt_version = payload.get("prompt_version") or UNKNOWN
            auth_version = payload.get("authorization_version")
            row_keys = set(row.keys()) if hasattr(row, "keys") else set()
            authorization_id = payload.get("authorization_id")
            if not authorization_id and "authorization_id" in row_keys:
                authorization_id = row["authorization_id"]
            model_digests.add(str(model_digest))
            prompt_versions.add(str(prompt_version))
            if auth_version is not None:
                authorization_versions.add(str(auth_version))
            action = str(payload.get("action") or row["action"] or UNKNOWN)
            status = str(row["status"] or UNKNOWN)
            reason = str(row["reason"] or payload.get("reason") or "")
            if status in {"REJECTED", "BLOCKED"} or "INVALID" in reason or "MODEL" in reason:
                decision_errors += 1
            if execution.get("error") or execution.get("error_code"):
                execution_errors += 1
            if "MARKET" in reason or "DATA" in reason or "STALE" in reason:
                data_errors += 1
            decisions.append(
                {
                    "cycle_id": row["cycle_id"],
                    "created_at": row["created_at"],
                    "action": action,
                    "status": status,
                    "reason": reason,
                    "input": {
                        "market_snapshot_hash": payload.get("market_snapshot_hash") or UNKNOWN,
                        "input_hash": payload.get("input_hash") or UNKNOWN,
                        "strategy_version": payload.get("strategy_version") or UNKNOWN,
                        "model_id": payload.get("model_id") or UNKNOWN,
                        "model_version": payload.get("model_version") or UNKNOWN,
                        "model_digest": model_digest,
                        "prompt_version": prompt_version,
                        "authorization_id": authorization_id or UNKNOWN,
                        "authorization_version": auth_version if auth_version is not None else UNKNOWN,
                        "fencing_token": payload.get("fencing_token") or UNKNOWN,
                    },
                    "execution": execution or {"status": UNKNOWN},
                }
            )
        evaluations = []
        for row in task_rows:
            result = _json_object(row["result_json"])
            evaluations.append(
                {
                    "task_id": row["task_id"],
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"] or UNKNOWN,
                    "status": row["status"],
                    "evidence_status": result.get("evidence_status") or UNKNOWN,
                    "sample_count": result.get("sample_count", 0),
                    "config_hash": _hash(_json_object(row["config_json"])),
                }
            )
        coordinator = getattr(runtime if runtime is not None else self.runtime, "ai_coordinator", None)
        if coordinator is not None and callable(getattr(coordinator, "status", None)):
            try:
                model_status = dict(coordinator.status().get("model") or {})
            except Exception as exc:
                model_status = {"status": UNKNOWN, "error": str(exc)[:240]}
        else:
            model_status = {"status": "UNAVAILABLE", "required_model": "qwen3.5:9b"}
        validated_digests = {
            str(item.get("model_digest"))
            for item in evaluations
            if item.get("status") == "EVALUATED" and item.get("model_digest")
        }
        revalidation_required = bool(
            model_digests
            and (UNKNOWN in model_digests or len(model_digests) > 1 or not validated_digests)
        )
        if len(model_digests) > 1 or len(prompt_versions) > 1:
            revalidation_reason = "MODEL_OR_PROMPT_VERSION_CHANGED"
        elif not evaluations:
            revalidation_reason = "NO_STORED_VALIDATION_FOR_AI_LED"
        else:
            revalidation_reason = "CURRENT_MODEL_DIGEST_NOT_LINKED_TO_EVALUATION"
        branch_status = {
            "STRATEGY_BASELINE": next((item for item in evaluations if item["strategy_id"] != "ai_led"), {"status": "EVIDENCE_INSUFFICIENT", "reason": "NO_STORED_STRATEGY_EVALUATION"}),
            "AI_FILTERED": {"status": "EVIDENCE_INSUFFICIENT", "reason": "NO_SEPARATE_STORED_AI_FILTER_EVALUATION"},
            "AI_LED": {
                "status": "EVIDENCE_INSUFFICIENT" if not decisions else "EVIDENCE_INSUFFICIENT_NO_SETTLED_OUTCOME_LINK",
                "sample_count": len(decisions),
                "reason": "AI cycle receipts alone are not profitable outcome evidence.",
            },
        }
        return {
            "account_id": account_id,
            "venue": scope["venue"],
            "mode": scope["mode"],
            "status": "AVAILABLE" if decisions else UNKNOWN,
            "model_status": model_status,
            "decision_inputs": decisions,
            "model_digests": sorted(model_digests),
            "prompt_versions": sorted(prompt_versions),
            "authorization_versions": sorted(authorization_versions),
            "research_evaluations": evaluations,
            "same_data_interval_cost_comparison": branch_status,
            "revalidation": {
                "required": revalidation_required,
                "reason": revalidation_reason,
                "confidence_is_not_win_rate": True,
            },
            "error_attribution": {
                "decision_error_count": decision_errors,
                "execution_slippage_or_adapter_error_count": execution_errors,
                "data_error_count": data_errors,
                "counts_are_receipt_classifications_not_profit_claims": True,
            },
            "limitations": [
                "AI confidence is not converted into a win rate or profit guarantee.",
                "A cycle with no durable settled outcome remains evidence-insufficient.",
                "A model or prompt digest change starts a new validation boundary; prior authorization and scorecard evidence are not silently inherited.",
            ],
        }
