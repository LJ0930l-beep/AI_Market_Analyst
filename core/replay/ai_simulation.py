"""Causal, account-isolated execution simulator for historical AI replay.

This module is deliberately a bar-based research simulator.  Its fills are
synthetic and must never be presented as Gate execution evidence.  In
particular, OHLCV cannot establish queue position or the intrabar path; the
assumptions recorded on every event make those limits visible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_UP
import copy
import math
import uuid
from typing import Any, Iterable


_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_SIM_ASSUMPTIONS = (
    "SYNTHETIC_OHLCV_MATCHING",
    "NO_HISTORICAL_ORDER_BOOK_OR_QUEUE_POSITION",
    "PARTICIPATION_LIMIT_USES_BAR_VOLUME",
    "LIMIT_REQUIRES_STRICT_PRICE_CROSSING",
    "MARKET_ORDERS_FILL_AT_NEXT_ELIGIBLE_BAR_OPEN_WITH_ADVERSE_SLIPPAGE",
    "INTRABAR_PROTECTION_USES_CONSERVATIVE_OHLC_RULES",
    "ONE_WAY_POSITION_MODE_WITH_FIFO_ENTRY_LOT_NETTING",
    "LIMIT_MARKETABILITY_USES_SUBMISSION_LAST_PRICE_PROXY_NOT_ORDER_BOOK",
    "RESTING_LIMIT_MAKER_FEES_ARE_A_RESEARCH_PROXY",
    "DRAWDOWN_USES_OBSERVED_BAR_CLOSES_NOT_INTRABAR_EQUITY",
    "CURRENT_PUBLIC_MAINTENANCE_RATE_IS_A_PROXY_NOT_HISTORICAL_LIQUIDATION_ENGINE",
)


def _decimal(value: Any, *, name: str = "value", allow_none: bool = False) -> Decimal | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name.upper()}_INVALID")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name.upper()}_INVALID") from exc
    if not number.is_finite():
        raise ValueError(f"{name.upper()}_NOT_FINITE")
    return number


def _time(value: Any, *, name: str = "time") -> datetime:
    if isinstance(value, datetime):
        point = value
    elif isinstance(value, str):
        try:
            point = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name.upper()}_INVALID") from exc
    else:
        raise ValueError(f"{name.upper()}_INVALID")
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _time(value).isoformat()


def _float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _json_state(value: Any) -> Any:
    """Encode Decimal values explicitly so checkpoints restore without loss."""
    if isinstance(value, Decimal):
        return {"__decimal__": format(value, "f")}
    if isinstance(value, dict):
        return {str(key): _json_state(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_state(item) for item in value]
    return value


def _restore_state(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__decimal__"}:
            return Decimal(value["__decimal__"])
        return {key: _restore_state(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_state(item) for item in value]
    return value


def _round_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        raise ValueError("CONTRACT_STEP_INVALID")
    return (value / step).to_integral_value(rounding=rounding) * step


def _positive(value: Decimal | None) -> bool:
    return value is not None and value.is_finite() and value > 0


class ReplayAccount:
    """One continuously-compounded, isolated paper account for one replay.

    Orders are only evaluated from closed bars passed to :meth:`advance`.
    Account state and event outputs are intentionally marked as simulated.
    """

    CHECKPOINT_VERSION = 1

    def __init__(
        self,
        initial_equity: float = 1000.0,
        maker_fee_rate: float = 0.0002,
        taker_fee_rate: float = 0.0005,
        slippage_bps: float = 2.0,
        participation_rate: float = 0.01,
        margin_cap_pct: float = 20.0,
        account_id: str = "replay-default",
    ) -> None:
        self.initial_equity = _decimal(initial_equity, name="initial_equity")
        self.maker_fee_rate = _decimal(maker_fee_rate, name="maker_fee_rate")
        self.taker_fee_rate = _decimal(taker_fee_rate, name="taker_fee_rate")
        self.slippage_bps = _decimal(slippage_bps, name="slippage_bps")
        self.participation_rate = _decimal(participation_rate, name="participation_rate")
        self.margin_cap_pct = _decimal(margin_cap_pct, name="margin_cap_pct")
        self.account_id = str(account_id or "").strip()
        if not self.account_id:
            raise ValueError("ACCOUNT_ID_REQUIRED")
        if self.initial_equity <= 0:
            raise ValueError("INITIAL_EQUITY_MUST_BE_POSITIVE")
        if self.maker_fee_rate < 0 or self.taker_fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError("COST_PARAMETERS_MUST_NOT_BE_NEGATIVE")
        if not _ZERO < self.participation_rate <= _ONE:
            raise ValueError("PARTICIPATION_RATE_OUT_OF_RANGE")
        if not _ZERO < self.margin_cap_pct <= _HUNDRED:
            raise ValueError("MARGIN_CAP_PCT_OUT_OF_RANGE")

        self.realized_gross_pnl = _ZERO
        self.total_fees = _ZERO
        self.funding_pnl = _ZERO
        self.orders: dict[str, dict[str, Any]] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        self.completed_trades: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.last_prices: dict[str, Decimal] = {}
        self.processed_bars: set[str] = set()
        self.processed_funding: set[str] = set()
        self.last_bar_end: dict[str, str] = {}
        self.last_advanced_through: str | None = None
        self.equity_history: list[dict[str, Any]] = [
            {"at": None, "equity": self.initial_equity}
        ]
        self.filled_order_ids: set[str] = set()
        self.partial_order_ids: set[str] = set()
        self._counter = 0
        self.halted_reason: str | None = None
        self.halted_at: str | None = None
        self.halted_diagnostic: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReplayAccount":
        if not isinstance(value, dict) or value.get("checkpoint_version") != cls.CHECKPOINT_VERSION:
            raise ValueError("REPLAY_CHECKPOINT_VERSION_UNSUPPORTED")
        config = _restore_state(value.get("config") or {})
        account = cls(**config)
        state = _restore_state(value.get("state") or {})
        for key in (
            "realized_gross_pnl", "total_fees", "funding_pnl", "orders", "positions",
            "completed_trades", "events", "last_prices", "last_bar_end", "equity_history",
            "last_advanced_through", "_counter", "halted_reason", "halted_at", "halted_diagnostic",
        ):
            if key in state:
                setattr(account, key, state[key])
        account.processed_bars = set(state.get("processed_bars") or [])
        account.processed_funding = set(state.get("processed_funding") or [])
        account.filled_order_ids = set(state.get("filled_order_ids") or [])
        account.partial_order_ids = set(state.get("partial_order_ids") or [])
        account._assert_state()
        return account

    def to_dict(self) -> dict[str, Any]:
        config = {
            "initial_equity": self.initial_equity,
            "maker_fee_rate": self.maker_fee_rate,
            "taker_fee_rate": self.taker_fee_rate,
            "slippage_bps": self.slippage_bps,
            "participation_rate": self.participation_rate,
            "margin_cap_pct": self.margin_cap_pct,
            "account_id": self.account_id,
        }
        state = {
            "realized_gross_pnl": self.realized_gross_pnl,
            "total_fees": self.total_fees,
            "funding_pnl": self.funding_pnl,
            "orders": self.orders,
            "positions": self.positions,
            "completed_trades": self.completed_trades,
            "events": self.events,
            "last_prices": self.last_prices,
            "processed_bars": sorted(self.processed_bars),
            "processed_funding": sorted(self.processed_funding),
            "last_bar_end": self.last_bar_end,
            "last_advanced_through": self.last_advanced_through,
            "equity_history": self.equity_history,
            "filled_order_ids": sorted(self.filled_order_ids),
            "partial_order_ids": sorted(self.partial_order_ids),
            "_counter": self._counter,
            "halted_reason": self.halted_reason,
            "halted_at": self.halted_at,
            "halted_diagnostic": self.halted_diagnostic,
        }
        return {
            "checkpoint_version": self.CHECKPOINT_VERSION,
            "config": _json_state(config),
            "state": _json_state(state),
        }

    def _assert_state(self) -> None:
        if self.initial_equity <= 0 or self.total_fees < 0:
            raise ValueError("REPLAY_CHECKPOINT_STATE_INVALID")
        for order in self.orders.values():
            if order.get("remaining_qty", _ZERO) < 0:
                raise ValueError("REPLAY_CHECKPOINT_ORDER_INVALID")
        for position in self.positions.values():
            for lot in position.get("lots", []):
                if lot.get("remaining_qty", _ZERO) < 0 or lot.get("quantity", _ZERO) <= 0:
                    raise ValueError("REPLAY_CHECKPOINT_POSITION_INVALID")

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self.account_id}_{self._counter:08d}"

    @staticmethod
    def _rules(market: dict[str, Any]) -> dict[str, Any]:
        nested = market.get("market") if isinstance(market.get("market"), dict) else {}
        metadata = market.get("metadata") if isinstance(market.get("metadata"), dict) else {}
        candidate = market.get("contract_rules") if isinstance(market.get("contract_rules"), dict) else {}
        merged: dict[str, Any] = {}
        for source in (metadata, nested, market, candidate):
            merged.update(source)
        return merged

    @staticmethod
    def _step_value(raw: Any, *, default: Decimal, precision_digits: bool = False) -> Decimal:
        if raw is None:
            return default
        step = _decimal(raw, name="contract_step")
        # CCXT precision metadata may be a count of decimal places; Gate
        # metadata commonly supplies the actual amount/price increment.
        if precision_digits and step >= 1 and step == step.to_integral_value():
            return Decimal("1").scaleb(-int(step))
        return step

    def _contract(self, market: dict[str, Any]) -> dict[str, Decimal | str | None]:
        rules = self._rules(market)
        precision = rules.get("precision") if isinstance(rules.get("precision"), dict) else {}
        limits = rules.get("limits") if isinstance(rules.get("limits"), dict) else {}
        amount_limits = limits.get("amount") if isinstance(limits.get("amount"), dict) else {}
        price_limits = limits.get("price") if isinstance(limits.get("price"), dict) else {}
        leverage_limits = limits.get("leverage") if isinstance(limits.get("leverage"), dict) else {}
        contract_size = _decimal(
            rules.get("contract_size", rules.get("contractSize", rules.get("quanto_multiplier", 1))),
            name="contract_size",
        )
        amount_raw = rules.get("amount_step", amount_limits.get("step"))
        amount_is_precision = amount_raw is None and precision.get("amount") is not None
        amount_step = self._step_value(amount_raw if amount_raw is not None else precision.get("amount"), default=Decimal("0.001"), precision_digits=amount_is_precision)
        price_raw = rules.get("price_tick", rules.get("price_round", price_limits.get("step")))
        price_is_precision = price_raw is None and precision.get("price") is not None
        price_tick = self._step_value(price_raw if price_raw is not None else precision.get("price"), default=Decimal("0.01"), precision_digits=price_is_precision)
        min_amount = _decimal(rules.get("min_size", amount_limits.get("min", amount_step)), name="min_amount")
        max_amount = _decimal(rules.get("max_size", amount_limits.get("max", "1000000000")), name="max_amount")
        max_leverage_raw = rules.get("leverage_max", leverage_limits.get("max"))
        max_leverage = _decimal(max_leverage_raw, name="max_leverage", allow_none=True)
        min_notional = _decimal(rules.get("min_notional"), name="min_notional", allow_none=True)
        maintenance_rate = _decimal(rules.get("maintenance_rate"), name="maintenance_rate", allow_none=True)
        if maintenance_rate is not None and not _ZERO <= maintenance_rate < _ONE:
            raise ValueError("MAINTENANCE_RATE_INVALID")
        return {
            "contract_size": contract_size,
            "amount_step": amount_step,
            "price_tick": price_tick,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "max_leverage": max_leverage,
            "min_notional": min_notional,
            "maintenance_rate": maintenance_rate,
            "volume_unit": str(rules.get("bar_volume_unit") or rules.get("volume_unit") or "contracts").lower(),
        }

    def _market_price(self, market: dict[str, Any]) -> Decimal | None:
        for key in ("price", "mark_price", "last_price", "current_price"):
            if market.get(key) is not None:
                try:
                    value = _decimal(market[key], name="market_price")
                    if value > 0:
                        return value
                except ValueError:
                    return None
        return None

    def _equity(self, prices: dict[str, Any] | None = None, *, leverage_overrides: dict[str, Decimal] | None = None) -> tuple[Decimal, Decimal, Decimal]:
        marks = dict(self.last_prices)
        if isinstance(prices, dict):
            for instrument, raw in prices.items():
                if isinstance(raw, dict):
                    raw = raw.get("price", raw.get("mark_price", raw.get("close")))
                try:
                    number = _decimal(raw, name="mark_price")
                except ValueError:
                    continue
                if number > 0:
                    marks[str(instrument)] = number
        unrealized = _ZERO
        used_margin = _ZERO
        for position in self.positions.values():
            mark = marks.get(position["instrument_id"])
            if mark is None or mark <= 0:
                mark = position.get("last_mark") or position["reference_price"]
            sign = _ONE if position["side"] == "LONG" else -_ONE
            for lot in position["lots"]:
                qty = lot["remaining_qty"]
                if qty <= 0:
                    continue
                contract_size = lot["contract_size"]
                unrealized += (mark - lot["entry_price"]) * qty * contract_size * sign
                leverage = (leverage_overrides or {}).get(position["instrument_id"], lot["leverage"])
                if leverage > 0:
                    used_margin += mark * qty * contract_size / leverage
        equity = self.initial_equity + self.realized_gross_pnl - self.total_fees + self.funding_pnl + unrealized
        return equity, unrealized, used_margin

    def _order_reserve(self, order: dict[str, Any], *, leverage_override: Decimal | None = None) -> Decimal:
        if order.get("kind") != "ENTRY":
            return _ZERO
        qty = order["remaining_qty"]
        if qty <= 0:
            return _ZERO
        # In one-way mode the offset portion reduces existing exposure and
        # cannot reserve a second opening margin against the same contracts.
        offset = sum((lot["remaining_qty"] for position in self.positions.values()
            if position["instrument_id"] == order["instrument_id"] and position["side"] != order["side"]
            for lot in position["lots"] if lot["remaining_qty"] > 0), _ZERO)
        opening_qty = max(_ZERO, qty - offset)
        notional = opening_qty * order["reserve_price"] * order["contract_size"]
        fee_buffer = order["reserve_fee_rate"] + self.taker_fee_rate
        return notional / (leverage_override or order["leverage"]) + notional * fee_buffer

    def _budget(self, prices: dict[str, Any] | None = None, *, leverage_overrides: dict[str, Decimal] | None = None) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        equity, unrealized, used_margin = self._equity(prices, leverage_overrides=leverage_overrides)
        cap = max(_ZERO, equity * self.margin_cap_pct / _HUNDRED)
        reserved = sum((self._order_reserve(order, leverage_override=(leverage_overrides or {}).get(order["instrument_id"])) for order in self.orders.values()), _ZERO)
        available = max(_ZERO, cap - used_margin - reserved)
        return cap, used_margin, reserved, available

    def _mark_history(self, at: datetime, prices: dict[str, Any] | None = None) -> None:
        if self.halted_reason:
            return
        equity = self._equity(prices)[0]
        when = _iso(at)
        if self.equity_history and self.equity_history[-1].get("at") == when:
            self.equity_history[-1] = {"at": when, "equity": equity}
        else:
            self.equity_history.append({"at": when, "equity": equity})

    def _economic_guard(self, at: datetime, prices: dict[str, Any] | None = None, *, cause: str) -> bool:
        """Stop at an unsupported liquidation boundary; never invent settlement.

        Public base maintenance rates cannot reproduce Gate's historical risk
        tiers, mark-price liquidation, insurance fund or bankruptcy settlement.
        They identify a possible unsupported path, rather than an exact loss.
        """
        if self.halted_reason:
            return False
        marks = dict(self.last_prices)
        if isinstance(prices, dict):
            marks.update({str(key): _decimal(value, name="guard_mark") for key, value in prices.items()})
        equity, unrealized, used = self._equity(marks)
        maintenance = _ZERO
        active = False
        missing_rates = False
        for position in self.positions.values():
            mark = _decimal(marks.get(position["instrument_id"], position["reference_price"]), name="guard_mark")
            for lot in position["lots"]:
                if lot["remaining_qty"] <= 0:
                    continue
                active = True
                rate = lot.get("maintenance_rate")
                if rate is None:
                    missing_rates = True
                else:
                    maintenance += mark * lot["remaining_qty"] * lot["contract_size"] * rate
        breached = equity <= 0 or (active and maintenance > 0 and equity <= maintenance)
        if not breached:
            return True
        for instrument, mark in marks.items():
            if _positive(mark):
                self.last_prices[instrument] = mark
        self._mark_history(at)
        self.halted_reason = "UNSUPPORTED_LIQUIDATION_PATH"
        self.halted_at = _iso(at)
        self.last_advanced_through = self.halted_at
        self.halted_diagnostic = {
            "at": self.halted_at, "equity": equity, "unrealized_pnl": unrealized,
            "used_margin": used, "maintenance_margin_proxy": maintenance,
            "missing_maintenance_rates": missing_rates, "cause": cause,
            "valuation": "DIAGNOSTIC_ONLY_NOT_GATE_LIQUIDATION_SETTLEMENT",
        }
        self._emit(at=at, status="HALTED", action="ECONOMIC_PATH_UNSUPPORTED",
                   reason=self.halted_reason, diagnostic_equity=_float(equity),
                   maintenance_margin_proxy=_float(maintenance), cause=cause,
                   economic_eligible=False)
        return False

    def _emit(self, *, at: datetime, status: str, action: str, **fields: Any) -> dict[str, Any]:
        event = {
            "event_id": self._next_id("replay_event"),
            "account_id": self.account_id,
            "status": status,
            "action": action,
            "event_time": _iso(at),
            "simulation": True,
            "fill_source": "SIMULATED_BAR" if "fill_price" in fields else None,
            "assumptions": list(_SIM_ASSUMPTIONS),
            **fields,
        }
        self.events.append(event)
        return event

    def apply_decision(self, decision: dict[str, Any], as_of: datetime, market: dict[str, Any]) -> dict[str, Any]:
        """Validate and stage one AI decision; only ``advance`` may fill it."""
        now = _time(as_of, name="as_of")
        if self.halted_reason:
            return {"status": "HALTED", "action": "ACCOUNT_HALTED", "reason": self.halted_reason,
                    "event_time": self.halted_at, "simulation": True, "economic_eligible": False}
        if self.last_advanced_through and now < _time(self.last_advanced_through):
            return self._emit(at=now, status="REJECTED", action="UNKNOWN", reason="DECISION_BEFORE_REPLAY_CLOCK")
        if not isinstance(decision, dict) or not isinstance(market, dict):
            return self._emit(at=now, status="REJECTED", action="UNKNOWN", reason="DECISION_OR_MARKET_INVALID")
        action = str(decision.get("action") or decision.get("action_type") or "").strip().upper()
        aliases = {
            "CLOSE": "CLOSE_POSITION", "REDUCE": "REDUCE_POSITION", "TIGHTEN_STOP": "UPDATE_PROTECTION",
            "NO_ACTION": "WAIT",
        }
        action = aliases.get(action, action)
        instrument = str(decision.get("instrument_id") or decision.get("symbol") or market.get("instrument_id") or "").strip()
        if action in {"WAIT", "HOLD"}:
            return self._emit(at=now, status="ACCEPTED", action=action, reason=str(decision.get("reason") or "NO_ORDER"))
        if action == "CANCEL_ORDER":
            return self._cancel_order(decision, now, instrument)
        if action == "UPDATE_PROTECTION":
            return self._update_protection(decision, now, instrument, market)
        if action in {"CLOSE_POSITION", "REDUCE_POSITION"}:
            return self._submit_exit(decision, now, instrument, market, action)
        if action not in {"OPEN_LONG", "OPEN_SHORT"}:
            return self._emit(at=now, status="REJECTED", action=action or "UNKNOWN", reason="ACTION_UNSUPPORTED")
        return self._submit_entry(decision, now, market, instrument, action)

    def _reject(self, now: datetime, action: str, reason: str, **fields: Any) -> dict[str, Any]:
        return self._emit(at=now, status="REJECTED", action=action, reason=reason, **fields)

    def _submit_entry(self, decision: dict[str, Any], now: datetime, market: dict[str, Any], instrument: str, action: str) -> dict[str, Any]:
        if not instrument:
            return self._reject(now, action, "INSTRUMENT_REQUIRED")
        try:
            contract = self._contract(market)
            contract_size = contract["contract_size"]
            step = contract["amount_step"]
            tick = contract["price_tick"]
            min_amount, max_amount = contract["min_amount"], contract["max_amount"]
            if not all(_positive(value) for value in (contract_size, step, tick, min_amount, max_amount)):
                raise ValueError("CONTRACT_RULES_INVALID")
            leverage = _decimal(decision.get("requested_leverage", decision.get("leverage", 1)), name="leverage")
            if leverage != leverage.to_integral_value() or leverage < 1:
                raise ValueError("LEVERAGE_INVALID")
            max_leverage = contract["max_leverage"]
            if max_leverage is not None and leverage > max_leverage:
                raise ValueError("LEVERAGE_EXCEEDS_CONTRACT_MAX")
            reference = self._market_price(market)
            if reference is None:
                raise ValueError("MARKET_REFERENCE_PRICE_REQUIRED")
            order_preference = str(decision.get("order_preference") or decision.get("order_type") or "MARKET").upper()
            if order_preference in {"LIMIT", "LIMIT_ORDER"}:
                order_type = "LIMIT"
                raw_price = decision.get("limit_price", decision.get("entry_price"))
                price = _decimal(raw_price, name="limit_price")
                side_round = ROUND_DOWN if action == "OPEN_LONG" else ROUND_UP
                price = _round_step(price, tick, side_round)
            elif order_preference in {"MARKET", "MARKET_ORDER"}:
                order_type = "MARKET"
                price = reference
            else:
                raise ValueError("ORDER_PREFERENCE_INVALID")
            if not _positive(price):
                raise ValueError("ENTRY_PRICE_INVALID")

            stop_raw = decision.get("stop_price")
            protection = decision.get("protection_plan") if isinstance(decision.get("protection_plan"), dict) else {}
            if stop_raw is None:
                stop_raw = protection.get("stop_price")
            stop = _decimal(stop_raw, name="stop_price")
            stop_round = ROUND_DOWN if action == "OPEN_LONG" else ROUND_UP
            stop = _round_step(stop, tick, stop_round)
            target_raw = decision.get("take_profit", decision.get("take_profit_price", protection.get("take_profit")))
            target = _decimal(target_raw, name="take_profit", allow_none=True)
            if target is None:
                raise ValueError("TAKE_PROFIT_REQUIRED")
            target_round = ROUND_DOWN if action == "OPEN_LONG" else ROUND_UP
            target = _round_step(target, tick, target_round)
            if action == "OPEN_LONG" and (stop >= price or (target is not None and target <= price)):
                raise ValueError("PROTECTION_GEOMETRY_INVALID")
            if action == "OPEN_SHORT" and (stop <= price or (target is not None and target >= price)):
                raise ValueError("PROTECTION_GEOMETRY_INVALID")

            qty_raw = decision.get("quantity", decision.get("contracts", decision.get("amount")))
            notional_raw = decision.get("position_size_usdt", decision.get("notional_usdt", decision.get("requested_notional_usdt")))
            if qty_raw is not None:
                requested_qty = _decimal(qty_raw, name="quantity")
            elif notional_raw is not None:
                notional = _decimal(notional_raw, name="position_size_usdt")
                requested_qty = notional / (price * contract_size)
            else:
                raise ValueError("POSITION_SIZE_REQUIRED")
            if requested_qty <= 0:
                raise ValueError("QUANTITY_MUST_BE_POSITIVE")
            requested_qty = _round_step(requested_qty, step, ROUND_DOWN)
            if requested_qty < min_amount:
                raise ValueError("QUANTITY_BELOW_CONTRACT_MIN")

            # Gate one-way mode: an opposite entry first reduces the existing
            # net position; only an excess amount opens the new direction.
            side = "LONG" if action == "OPEN_LONG" else "SHORT"

            # Apply amount and margin limits to the requested amount.  The
            # resulting order is rounded down, never over budget or contract.
            requested_qty = min(requested_qty, max_amount)
            cap, used, reserved, available = self._budget(leverage_overrides={instrument: leverage})
            marketable_limit = order_type == "LIMIT" and (price >= reference if side == "LONG" else price <= reference)
            fee_type = "TAKER" if order_type == "MARKET" or marketable_limit else "MAKER"
            fee_rate = self.taker_fee_rate if fee_type == "TAKER" else self.maker_fee_rate
            reserve_price = price
            unit_cost = reserve_price * contract_size * (1 / leverage + fee_rate + self.taker_fee_rate)
            opposite_qty = sum((lot["remaining_qty"] for existing in self.positions.values()
                if existing["instrument_id"] == instrument and existing["side"] != side
                for lot in existing["lots"] if lot["remaining_qty"] > 0), _ZERO)
            if unit_cost <= 0 or (available <= 0 and opposite_qty <= 0):
                raise ValueError("MARGIN_BUDGET_EXHAUSTED")
            max_affordable = opposite_qty + _round_step(available / unit_cost, step, ROUND_DOWN)
            quantity = min(requested_qty, max_affordable)
            quantity = _round_step(quantity, step, ROUND_DOWN)
            if quantity < min_amount:
                raise ValueError("MARGIN_BUDGET_BELOW_CONTRACT_MIN")
            notional = quantity * price * contract_size
            if contract["min_notional"] is not None and notional < contract["min_notional"]:
                raise ValueError("NOTIONAL_BELOW_CONTRACT_MIN")
            expires_raw = decision.get("expires_at")
            ttl_raw = decision.get("ttl_seconds", 900)
            ttl = _decimal(ttl_raw, name="ttl_seconds")
            if ttl <= 0:
                raise ValueError("TTL_MUST_BE_POSITIVE")
            expires = _time(expires_raw, name="expires_at") if expires_raw else now + timedelta(seconds=float(ttl))
        except (ValueError, OverflowError, TypeError, ArithmeticError) as exc:
            return self._reject(now, action, str(exc) or "ORDER_ECONOMICS_INVALID", instrument_id=instrument)

        order_id = str(decision.get("order_id") or decision.get("intent_id") or self._next_id("sim_order"))
        position_id = str(decision.get("position_id") or decision.get("trade_id") or self._next_id("sim_position"))
        if order_id in self.orders or order_id in self.filled_order_ids:
            return self._reject(now, action, "DUPLICATE_ORDER_ID", order_id=order_id, instrument_id=instrument)
        requested_position = self.positions.get(position_id)
        if requested_position and (requested_position["instrument_id"] != instrument or requested_position["side"] != side):
            return self._reject(now, action, "POSITION_ID_SCOPE_CONFLICT", position_id=position_id)
        same_side = next((item for item in self.positions.values()
            if item["instrument_id"] == instrument and item["side"] == side and not item.get("completed")
            and (any(lot["remaining_qty"] > 0 for lot in item["lots"])
                 or any(row["kind"] == "ENTRY" and row["position_id"] == item["position_id"] for row in self.orders.values()))), None)
        if same_side is not None:
            position_id = same_side["position_id"]
        position = self.positions.get(position_id)
        if position is None:
            position = {
                "position_id": position_id,
                "instrument_id": instrument,
                "side": side,
                "leverage": leverage,
                "created_at": _iso(now),
                "reference_price": price,
                "last_mark": reference,
                "entry_order_ids": [],
                "lots": [],
                "completed": False,
                "position_mode": "ONE_WAY",
            }
            self.positions[position_id] = position
        elif position["instrument_id"] != instrument or position["side"] != side:
            return self._reject(now, action, "POSITION_ID_SCOPE_CONFLICT", position_id=position_id)
        elif position.get("completed"):
            return self._reject(now, action, "COMPLETED_POSITION_ID_CANNOT_BE_REUSED", position_id=position_id)
        # Gate one-way cross leverage is a setting for the instrument, rather
        # than an immutable value attached to each historical entry lot.
        for existing in self.positions.values():
            if existing["instrument_id"] != instrument:
                continue
            existing["leverage"] = leverage
            for lot in existing["lots"]:
                if lot["remaining_qty"] > 0:
                    lot.setdefault("entry_requested_leverage", lot["leverage"])
                    lot["leverage"] = leverage
        for pending in self.orders.values():
            if pending["instrument_id"] == instrument and pending["kind"] == "ENTRY":
                pending["leverage"] = leverage
        position["entry_order_ids"].append(order_id)
        order = {
            "order_id": order_id,
            "position_id": position_id,
            "kind": "ENTRY",
            "instrument_id": instrument,
            "side": side,
            "order_type": order_type,
            "requested_qty": quantity,
            "remaining_qty": quantity,
            "submitted_at": _iso(now),
            "expires_at": _iso(expires),
            "reference_price": reference,
            "reserve_price": reserve_price,
            "limit_price": price if order_type == "LIMIT" else None,
            "leverage": leverage,
            "stop_price": stop,
            "take_profit": target,
            "contract_size": contract_size,
            "amount_step": step,
            "price_tick": tick,
            "fee_rate": fee_rate,
            "fee_type": fee_type,
            "marketable_limit": marketable_limit,
            "maintenance_rate": contract["maintenance_rate"],
            "reserve_fee_rate": fee_rate,
            "synthetic": bool(market.get("synthetic", False)),
            "market_rules": _json_state(contract),
            "requested_qty_before_cap": requested_qty,
            "margin_cap_pct": self.margin_cap_pct,
        }
        self.orders[order_id] = order
        event = self._emit(
            at=now,
            status="ACCEPTED",
            action=action,
            order_id=order_id,
            position_id=position_id,
            instrument_id=instrument,
            side=side,
            order_type=order_type,
            quantity=_float(quantity),
            limit_price=_float(order["limit_price"]),
            stop_price=_float(stop),
            take_profit=_float(target),
            requested_quantity=_float(requested_qty),
            quantity_capped=_float(quantity) < _float(requested_qty),
            leverage=_float(leverage),
            expires_at=_iso(expires),
            used_margin=_float(used),
            reserved_margin=_float(reserved + self._order_reserve(order)),
            available_margin=_float(max(_ZERO, available - self._order_reserve(order))),
            instrument_rules_assumption="DEFAULT_CONTRACT_RULES_USED" if not market.get("contract_rules") and not market.get("market") and not market.get("metadata") else "MARKET_CONTRACT_RULES",
            synthetic=bool(market.get("synthetic", False)),
            fee_type=fee_type,
            fee_rate=_float(fee_rate),
            marketability_assumption="SUBMISSION_LAST_PRICE_PROXY" if marketable_limit else ("RESTING_LIMIT_PROXY" if order_type == "LIMIT" else "MARKET_ORDER"),
        )
        self._mark_history(now)
        return event

    def _cancel_order(self, decision: dict[str, Any], now: datetime, instrument: str) -> dict[str, Any]:
        order_id = str(decision.get("order_id") or decision.get("intent_id") or "")
        candidates = [
            order for order in self.orders.values()
            if (not instrument or order["instrument_id"] == instrument)
            and (not order_id or order["order_id"] == order_id)
        ]
        if len(candidates) != 1:
            return self._reject(now, "CANCEL_ORDER", "PENDING_ORDER_NOT_UNIQUE", instrument_id=instrument or None)
        order = candidates[0]
        self.orders.pop(order["order_id"], None)
        self._maybe_complete(order["position_id"], now)
        return self._emit(at=now, status="ACCEPTED", action="CANCEL_ORDER", order_id=order["order_id"], instrument_id=order["instrument_id"], canceled_quantity=_float(order["remaining_qty"]), released_reserve=_float(self._order_reserve(order)))

    def _update_protection(self, decision: dict[str, Any], now: datetime, instrument: str, market: dict[str, Any]) -> dict[str, Any]:
        position_id = str(decision.get("position_id") or "")
        positions = [
            position for position in self.positions.values()
            if position["instrument_id"] == instrument
            and (not position_id or position["position_id"] == position_id)
            and any(lot["remaining_qty"] > 0 for lot in position["lots"])
        ]
        if len(positions) != 1:
            return self._reject(now, "UPDATE_PROTECTION", "POSITION_NOT_UNIQUE", instrument_id=instrument or None)
        position = positions[0]
        contract = self._contract(market)
        tick = contract["price_tick"]
        if not _positive(tick):
            return self._reject(now, "UPDATE_PROTECTION", "PRICE_TICK_INVALID", position_id=position["position_id"])
        try:
            stop_supplied = "new_stop_price" in decision or "stop_price" in decision
            stop_raw = decision.get("new_stop_price", decision.get("stop_price"))
            stop = _decimal(stop_raw, name="new_stop_price", allow_none=True) if stop_supplied else None
            target_supplied = "new_take_profit" in decision or "take_profit" in decision
            target_raw = decision.get("new_take_profit", decision.get("take_profit"))
            target = _decimal(target_raw, name="new_take_profit", allow_none=True) if target_supplied else None
            if not stop_supplied and not target_supplied:
                raise ValueError("PROTECTION_UPDATE_EMPTY")
            if stop_supplied and (stop is None or stop <= 0):
                raise ValueError("PROTECTION_PRICE_INVALID")
            if target_supplied and (target is None or target <= 0):
                raise ValueError("PROTECTION_PRICE_INVALID")
            if stop is not None:
                stop = _round_step(stop, tick, ROUND_DOWN if position["side"] == "LONG" else ROUND_UP)
            if target is not None:
                target = _round_step(target, tick, ROUND_DOWN if position["side"] == "LONG" else ROUND_UP)
            if (stop is not None and stop <= 0) or (target is not None and target <= 0):
                raise ValueError("PROTECTION_PRICE_INVALID")
            # Match the production update contract: AI may tighten or widen
            # protection, including stops above/below entry to protect profit.
            # A crossed level exits on a later eligible bar; entry geometry is
            # not an additional strategy veto here.
        except (ValueError, ArithmeticError) as exc:
            return self._reject(now, "UPDATE_PROTECTION", str(exc) or "PROTECTION_INVALID", position_id=position["position_id"])
        updated = 0
        for lot in position["lots"]:
            if lot["remaining_qty"] <= 0:
                continue
            if stop_supplied:
                lot["stop_price"] = stop
            if target_supplied:
                lot["take_profit"] = target
            lot["protection_active_after"] = _iso(now)
            updated += 1
        return self._emit(at=now, status="ACCEPTED", action="UPDATE_PROTECTION", position_id=position["position_id"], instrument_id=instrument, stop_price=_float(stop), take_profit=_float(target) if target_supplied else None, take_profit_preserved=not target_supplied, lots_updated=updated)

    def _submit_exit(self, decision: dict[str, Any], now: datetime, instrument: str, market: dict[str, Any], action: str) -> dict[str, Any]:
        if not instrument:
            return self._reject(now, action, "INSTRUMENT_REQUIRED")
        position_id = str(decision.get("position_id") or "")
        positions = [
            position for position in self.positions.values()
            if position["instrument_id"] == instrument
            and (not position_id or position["position_id"] == position_id)
            and any(lot["remaining_qty"] > 0 for lot in position["lots"])
        ]
        if len(positions) != 1:
            return self._reject(now, action, "POSITION_NOT_UNIQUE", instrument_id=instrument)
        position = positions[0]
        contract = self._contract(market)
        step = contract["amount_step"]
        total = sum((lot["remaining_qty"] for lot in position["lots"]), _ZERO)
        try:
            if action == "CLOSE_POSITION":
                quantity = total
            elif decision.get("quantity", decision.get("contracts")) is not None:
                quantity = _decimal(decision.get("quantity", decision.get("contracts")), name="quantity")
            elif decision.get("reduce_fraction") is not None:
                fraction = _decimal(decision["reduce_fraction"], name="reduce_fraction")
                if not _ZERO < fraction <= _ONE:
                    raise ValueError("REDUCE_FRACTION_OUT_OF_RANGE")
                quantity = total * fraction
            else:
                notional_raw = decision.get("position_size_usdt", decision.get("notional_usdt"))
                if notional_raw:
                    price = self._market_price(market) or position["reference_price"]
                    quantity = _decimal(notional_raw, name="position_size_usdt") / (price * contract["contract_size"])
                else:
                    quantity = total
            quantity = _round_step(quantity, step, ROUND_DOWN)
            quantity = min(quantity, total)
            if quantity <= 0:
                raise ValueError("REDUCE_QUANTITY_INVALID")
            expires_raw = decision.get("expires_at")
            ttl = _decimal(decision.get("ttl_seconds", 900), name="ttl_seconds")
            if ttl <= 0:
                raise ValueError("TTL_MUST_BE_POSITIVE")
            expires = _time(expires_raw, name="expires_at") if expires_raw else now + timedelta(seconds=float(ttl))
        except (ValueError, ArithmeticError) as exc:
            return self._reject(now, action, str(exc) or "EXIT_ORDER_INVALID", position_id=position["position_id"])
        order_id = str(decision.get("order_id") or decision.get("intent_id") or self._next_id("sim_exit"))
        if order_id in self.orders or order_id in self.filled_order_ids:
            return self._reject(now, action, "DUPLICATE_ORDER_ID", order_id=order_id)
        order = {
            "order_id": order_id,
            "position_id": position["position_id"],
            "target_position_id": position["position_id"],
            "kind": "EXIT",
            "instrument_id": instrument,
            "side": "SHORT" if position["side"] == "LONG" else "LONG",
            "position_side": position["side"],
            "order_type": "MARKET",
            "requested_qty": quantity,
            "remaining_qty": quantity,
            "submitted_at": _iso(now),
            "expires_at": _iso(expires),
            "reference_price": self._market_price(market) or position["reference_price"],
            "contract_size": contract["contract_size"],
            "amount_step": step,
            "price_tick": contract["price_tick"],
            "synthetic": bool(market.get("synthetic", False)),
        }
        self.orders[order_id] = order
        return self._emit(at=now, status="ACCEPTED", action=action, order_id=order_id, position_id=position["position_id"], instrument_id=instrument, order_type="MARKET", quantity=_float(quantity), expires_at=_iso(expires), reduce_only=True)

    def _bar_capacity(self, bar: dict[str, Any], market: dict[str, Any]) -> Decimal:
        volume_raw = bar.get("contracts_volume", bar.get("volume"))
        volume = _decimal(volume_raw, name="bar_volume")
        if volume < 0:
            return _ZERO
        contract = self._contract(market)
        unit = str(bar.get("volume_unit") or contract["volume_unit"] or "contracts").lower()
        if bar.get("base_volume") is not None:
            volume = _decimal(bar["base_volume"], name="bar_base_volume") / contract["contract_size"]
        elif unit in {"base", "base_asset", "coin"}:
            volume = volume / contract["contract_size"]
        elif unit in {"quote", "quote_asset", "usdt"}:
            volume = volume / (bar["close"] * contract["contract_size"])
        return max(_ZERO, _round_step(volume * self.participation_rate, contract["amount_step"], ROUND_DOWN))

    def advance(self, bars: list[dict[str, Any]], through: datetime) -> list[dict[str, Any]]:
        """Apply only new, valid, fully closed bars ending no later than through."""
        cutoff = _time(through, name="through")
        if self.halted_reason:
            return []
        if self.last_advanced_through and cutoff < _time(self.last_advanced_through):
            return []
        if not isinstance(bars, list):
            return []
        normalized: list[tuple[datetime, datetime, dict[str, Any]]] = []
        for raw in bars:
            if not isinstance(raw, dict):
                continue
            try:
                start = _time(raw.get("bar_start"), name="bar_start")
                end = _time(raw.get("bar_end"), name="bar_end")
                if not start < end or end > cutoff:
                    continue
                instrument = str(raw.get("instrument_id") or raw.get("symbol") or "").strip()
                if not instrument:
                    continue
                candle = dict(raw)
                candle["instrument_id"] = instrument
                for field in ("open", "high", "low", "close", "volume"):
                    if field == "volume" and candle.get("contracts_volume") is not None:
                        continue
                    if candle.get(field) is None:
                        raise ValueError("BAR_FIELD_MISSING")
                    candle[field] = _decimal(candle[field], name=f"bar_{field}")
                if not all(candle[field] > 0 for field in ("open", "high", "low", "close")) or candle["volume"] < 0:
                    continue
                if candle["high"] < max(candle["open"], candle["close"], candle["low"]) or candle["low"] > min(candle["open"], candle["close"], candle["high"]):
                    continue
                normalized.append((start, end, candle))
            except (ValueError, TypeError):
                continue

        normalized.sort(key=lambda item: (item[0], item[1], item[2]["instrument_id"]))
        output: list[dict[str, Any]] = []
        for start, end, bar in normalized:
            instrument = bar["instrument_id"]
            key = f"{instrument}|{_iso(start)}|{_iso(end)}"
            if key in self.processed_bars:
                continue
            prior_end = self.last_bar_end.get(instrument)
            if prior_end and start < _time(prior_end):
                continue
            market = dict(bar.get("market")) if isinstance(bar.get("market"), dict) else {}
            market.update(bar.get("contract_rules") if isinstance(bar.get("contract_rules"), dict) else {})
            for key in ("contract_size", "contractSize", "quanto_multiplier", "amount_step", "price_tick", "min_size", "max_size", "leverage_max", "min_notional", "maintenance_rate", "precision", "limits", "bar_volume_unit", "volume_unit"):
                if key in bar and key not in market:
                    market[key] = bar[key]
            market.setdefault("instrument_id", instrument)
            market.setdefault("synthetic", bool(bar.get("synthetic", False)))
            try:
                capacity = self._bar_capacity(bar, market)
            except ValueError:
                capacity = _ZERO
            bar_output_start = len(self.events)
            if not self._economic_guard(start, {instrument: bar["open"]}, cause="BAR_OPEN_GAP"):
                output.extend(self.events[bar_output_start:])
                break

            # Funding belongs to the event timestamp, not the later account
            # read time.  Explicit records can also be supplied through the
            # public apply_funding method.
            funding_record = None
            if bar.get("funding_rate") is not None:
                payment_raw = bar.get("funding_time") or bar.get("funding_timestamp") or _iso(end)
                try:
                    payment = _time(payment_raw, name="bar_funding_time")
                    funding_mark = bar.get("funding_mark_price")
                    if funding_mark is None:
                        funding_mark = bar["close"] if payment == end else self.last_prices.get(instrument, bar["open"])
                    funding_record = {
                        "instrument_id": instrument,
                        "payment_time": payment,
                        "rate": bar.get("funding_rate"),
                        "mark_price": funding_mark,
                        "source": bar.get("funding_source") or "BAR_FUNDING_FIELD",
                    }
                except ValueError:
                    funding_record = None
            # If a stop and target are both inside one OHLC bar, choose the
            # adverse stop.  There is no defensible intrabar ordering data.
            capacity = self._process_orders(bar, start, end, market, capacity, order_type="MARKET")
            if not self.halted_reason:
                capacity = self._process_protection(bar, start, end, market, capacity)
            if funding_record is not None:
                # The event is timestamped at bar end.  Process it after
                # market-at-open and existing protective exits, but before
                # ambiguous intrabar limit fills.
                self.apply_funding([funding_record], through=end)
            if not self.halted_reason:
                capacity = self._process_orders(bar, start, end, market, capacity, order_type="LIMIT")
            # LIMIT crossing and protective levels are ordered along the
            # price path for a continuously traded contract: if a long limit
            # is crossed while descending, it precedes a lower stop.  OHLC
            # cannot show the queue or exact timestamp, so fills remain
            # synthetic and stop wins any same-bar stop/target tie.
            if not self.halted_reason:
                capacity = self._process_protection(bar, start, end, market, capacity)

            # Remaining exposure may have crossed bankruptcy/maintenance in
            # the bar even when its close subsequently recovered. Protective
            # exits above run first; an unfilled remainder is still exposed.
            sides = {position["side"] for position in self.positions.values()
                     if position["instrument_id"] == instrument
                     and any(lot["remaining_qty"] > 0 for lot in position["lots"])}
            if sides and not self.halted_reason:
                adverse = bar["low"] if "LONG" in sides else bar["high"]
                self._economic_guard(end, {instrument: adverse}, cause="INTRABAR_REMAINING_EXPOSURE_PROXY")
            if self.halted_reason:
                output.extend(self.events[bar_output_start:])
                break

            self.last_prices[instrument] = bar["close"]
            for position in self.positions.values():
                if position["instrument_id"] == instrument:
                    position["last_mark"] = bar["close"]

            self.processed_bars.add(key)
            self.last_bar_end[instrument] = _iso(end)
            self._mark_history(end)
            self._economic_guard(end, cause="BAR_CLOSE_EQUITY")
            output.extend(self.events[bar_output_start:])
            if self.halted_reason:
                break
        if self.halted_reason:
            return output
        # TTL is wall-clock state and must progress even across a data gap.
        for order in list(self.orders.values()):
            expiry = _time(order["expires_at"])
            if expiry > cutoff:
                continue
            self.orders.pop(order["order_id"], None)
            if order["kind"] == "ENTRY":
                self._maybe_complete(order["position_id"], expiry)
            output.append(self._emit(at=expiry, status="EXPIRED", action="TTL_CANCEL", order_id=order["order_id"], position_id=order["position_id"], instrument_id=order["instrument_id"], unfilled_quantity=_float(order["remaining_qty"]), released_reserve=_float(self._order_reserve(order)), reason="TTL_ELAPSED_WITHOUT_ELIGIBLE_FILL"))
        self.last_advanced_through = _iso(cutoff)
        return output

    def _process_protection(self, bar: dict[str, Any], start: datetime, end: datetime, market: dict[str, Any], capacity: Decimal) -> Decimal:
        instrument = bar["instrument_id"]
        bar_key = f"{instrument}|{_iso(start)}|{_iso(end)}"
        for position in list(self.positions.values()):
            if self.halted_reason:
                break
            if position["instrument_id"] != instrument:
                continue
            for lot in list(position["lots"]):
                if self.halted_reason:
                    break
                if lot["remaining_qty"] <= 0 or _time(lot["protection_active_after"]) > start or lot.get("last_protection_bar") == bar_key:
                    continue
                lot["last_protection_bar"] = bar_key
                side = position["side"]
                gap_stop = False
                if lot.get("triggered_exit"):
                    reason = lot["triggered_exit"]
                    price = bar["open"]
                    stop = lot["stop_price"]
                    gap_stop = bar["open"] <= stop if side == "LONG" else bar["open"] >= stop
                    fill = self._adverse_price(price, "SHORT" if side == "LONG" else "LONG", lot["price_tick"])
                else:
                    stop = lot["stop_price"]
                    target = lot["take_profit"]
                    stop_hit = bar["low"] <= stop if side == "LONG" else bar["high"] >= stop
                    target_hit = target is not None and (bar["high"] >= target if side == "LONG" else bar["low"] <= target)
                    if not stop_hit and not target_hit:
                        continue
                    if stop_hit:
                        reason = "STOP_LOSS"
                        gap_stop = bar["open"] <= stop if side == "LONG" else bar["open"] >= stop
                        price = bar["open"] if gap_stop else stop
                        fill = self._adverse_price(price, "SHORT" if side == "LONG" else "LONG", lot["price_tick"])
                    else:
                        reason = "TAKE_PROFIT"
                        # OHLC cannot prove that a target was reached before
                        # a liquidation boundary in the same bar. Do not let
                        # a profitable target erase an earlier possible
                        # bankruptcy/maintenance breach. The stop-first path
                        # above remains valid when its guard price is safe.
                        adverse = bar["low"] if side == "LONG" else bar["high"]
                        if not self._economic_guard(end, {instrument: adverse}, cause="TAKE_PROFIT_LIQUIDATION_ORDERING_AMBIGUOUS"):
                            break
                        # Native protection exits are market orders. A gap
                        # does not justify filling at a past trigger price.
                        price = max(bar["open"], target) if side == "LONG" else min(bar["open"], target)
                        fill = self._adverse_price(price, "SHORT" if side == "LONG" else "LONG", lot["price_tick"])
                        gap_stop = False
                if not lot.get("triggered_exit"):
                    lot["triggered_exit"] = reason
                    lot["triggered_order_id"] = self._next_id("sim_protection")
                if not lot.get("triggered_order_id"):
                    lot["triggered_order_id"] = self._next_id("sim_protection")
                remaining_before = lot["remaining_qty"]
                requested = min(lot["remaining_qty"], capacity)
                qty = _round_step(requested, lot["amount_step"], ROUND_DOWN)
                if qty <= 0:
                    self._emit(at=end, status="PROTECTION_REMAINDER_PENDING", action=reason, position_id=position["position_id"], instrument_id=instrument, lot_id=lot["lot_id"], remaining_quantity=_float(lot["remaining_qty"]), synthetic=bool(bar.get("synthetic", False)), note="Protective exit triggered; bar participation capacity is exhausted.")
                    continue
                closed = self._close_lot(
                    position, lot, qty, fill, end,
                    order_id=lot["triggered_order_id"],
                    reason=reason, synthetic=bool(bar.get("synthetic", False)),
                    gap_stop=gap_stop,
                )
                if closed < remaining_before:
                    self.partial_order_ids.add(lot["triggered_order_id"])
                capacity = max(_ZERO, capacity - closed)
                if lot["remaining_qty"] <= 0:
                    lot["triggered_exit"] = None
                elif closed > 0:
                    self._emit(at=end, status="PROTECTION_REMAINDER_PENDING", action=reason, position_id=position["position_id"], instrument_id=instrument, lot_id=lot["lot_id"], remaining_quantity=_float(lot["remaining_qty"]), note="Remaining triggered quantity exits at a later eligible bar open, subject to volume participation.")
        return capacity

    def _process_orders(self, bar: dict[str, Any], start: datetime, end: datetime, market: dict[str, Any], capacity: Decimal, *, order_type: str) -> Decimal:
        instrument = bar["instrument_id"]
        orders = [order for order in list(self.orders.values()) if order["instrument_id"] == instrument]
        for order in orders:
            if self.halted_reason:
                break
            if order["order_id"] not in self.orders:
                continue
            submitted = _time(order["submitted_at"])
            expires = _time(order["expires_at"])
            if start < submitted:
                continue
            expired_before_execution = expires <= start or (order["order_type"] == "LIMIT" and expires <= end)
            if expired_before_execution:
                self.orders.pop(order["order_id"], None)
                if order["kind"] == "ENTRY":
                    self._maybe_complete(order["position_id"], end)
                self._emit(at=end, status="EXPIRED", action="TTL_CANCEL", order_id=order["order_id"], position_id=order["position_id"], instrument_id=instrument, unfilled_quantity=_float(order["remaining_qty"]), released_reserve=_float(self._order_reserve(order)), synthetic=bool(bar.get("synthetic", False)))
                continue
            if order["remaining_qty"] <= 0:
                self.orders.pop(order["order_id"], None)
                continue
            if order["kind"] == "ENTRY":
                if order["order_type"] != order_type:
                    continue
                if order["order_type"] == "MARKET":
                    fill_price = self._adverse_price(bar["open"], order["side"], order["price_tick"])
                else:
                    if order["side"] == "LONG" and not bar["low"] < order["limit_price"]:
                        continue
                    if order["side"] == "SHORT" and not bar["high"] > order["limit_price"]:
                        continue
                    if order.get("marketable_limit"):
                        open_executable = bar["open"] <= order["limit_price"] if order["side"] == "LONG" else bar["open"] >= order["limit_price"]
                        available_price = bar["open"] if open_executable else order["limit_price"]
                        slipped = self._adverse_price(available_price, order["side"], order["price_tick"])
                        fill_price = min(slipped, order["limit_price"]) if order["side"] == "LONG" else max(slipped, order["limit_price"])
                    else:
                        fill_price = order["limit_price"]
                qty = _round_step(min(order["remaining_qty"], capacity), order["amount_step"], ROUND_DOWN)
                # Price gaps can invalidate the original reservation.  Recheck
                # against equity known before this bar and cap quantity down.
                if qty > 0:
                    _, _, _, current_available = self._budget()
                    original_reserve = self._order_reserve(order)
                    available_for_order = current_available + original_reserve
                    unit_cost = fill_price * order["contract_size"] * (1 / order["leverage"] + order["fee_rate"] + self.taker_fee_rate)
                    offset_qty = sum((lot["remaining_qty"] for existing in self.positions.values()
                        if existing["instrument_id"] == instrument and existing["side"] != order["side"]
                        for lot in existing["lots"] if lot["remaining_qty"] > 0), _ZERO)
                    affordable = offset_qty + (_round_step(available_for_order / unit_cost, order["amount_step"], ROUND_DOWN) if unit_cost > 0 else _ZERO)
                    qty = min(qty, affordable)
                if qty <= 0:
                    continue
                position = self.positions[order["position_id"]]
                if (order["side"] == "LONG" and (order["stop_price"] >= fill_price or (order["take_profit"] is not None and order["take_profit"] <= fill_price))) or (order["side"] == "SHORT" and (order["stop_price"] <= fill_price or (order["take_profit"] is not None and order["take_profit"] >= fill_price))):
                    self.orders.pop(order["order_id"], None)
                    self._emit(at=end, status="REJECTED", action="ENTRY_FILL", reason="PROTECTION_GEOMETRY_INVALID_AT_FILL", order_id=order["order_id"], position_id=position["position_id"], fill_price=_float(fill_price), synthetic=bool(bar.get("synthetic", False)))
                    self._maybe_complete(position["position_id"], end)
                    continue
                # Match one-way Gate semantics before creating any new lot.
                # Each netting exit pays the incoming order's actual assumed
                # maker/taker rate; the leftover opens in the new direction.
                netted = _ZERO
                netting_fees_before = self.total_fees
                for opposite in list(self.positions.values()):
                    if opposite["instrument_id"] != instrument or opposite["side"] == order["side"]:
                        continue
                    for existing_lot in opposite["lots"]:
                        if netted >= qty or self.halted_reason:
                            break
                        if existing_lot["remaining_qty"] <= 0:
                            continue
                        netted += self._close_lot(opposite, existing_lot, min(qty - netted, existing_lot["remaining_qty"]), fill_price, end,
                            order_id=order["order_id"], reason="ONE_WAY_NETTING", synthetic=bool(bar.get("synthetic", False)),
                            fee_rate=order["fee_rate"], fee_type=order.get("fee_type", "MAKER" if order["order_type"] == "LIMIT" else "TAKER"))
                if self.halted_reason:
                    break
                opened_qty = qty - netted
                fee = opened_qty * fill_price * order["contract_size"] * order["fee_rate"]
                lot = {
                    "lot_id": self._next_id("sim_lot"),
                    "order_id": order["order_id"],
                    "quantity": opened_qty,
                    "remaining_qty": opened_qty,
                    "entry_price": fill_price,
                    "contract_size": order["contract_size"],
                    "leverage": order["leverage"],
                    "entry_requested_leverage": order["leverage"],
                    "maintenance_rate": order.get("maintenance_rate"),
                    "stop_price": order["stop_price"],
                    "take_profit": order["take_profit"],
                    "entry_fee": fee,
                    "exit_fees": _ZERO,
                    "funding_pnl": _ZERO,
                    "realized_gross": _ZERO,
                    "opened_at": _iso(start),
                    "protection_active_after": _iso(start),
                    "amount_step": order["amount_step"],
                    "price_tick": order["price_tick"],
                    "last_mark": fill_price,
                    "triggered_exit": None,
                    "exits": [],
                    "funding_entries": [],
                    "synthetic": bool(bar.get("synthetic", False) or order.get("synthetic", False)),
                }
                if opened_qty > 0:
                    position["lots"].append(lot)
                position["last_mark"] = fill_price
                position["reference_price"] = fill_price
                order["remaining_qty"] -= qty
                self.total_fees += fee
                capacity -= qty
                self.filled_order_ids.add(order["order_id"])
                partial = order["remaining_qty"] > 0
                if partial:
                    self.partial_order_ids.add(order["order_id"])
                else:
                    self.orders.pop(order["order_id"], None)
                self._emit(
                    at=end,
                    status="PARTIAL_FILL" if partial else "FILLED",
                    action="OPEN_LONG" if order["side"] == "LONG" else "OPEN_SHORT",
                    order_id=order["order_id"],
                    position_id=position["position_id"],
                    lot_id=lot["lot_id"],
                    instrument_id=instrument,
                    side=order["side"],
                    quantity=_float(qty),
                    fill_price=_float(fill_price),
                    fee=_float(fee),
                    fee_rate=_float(order["fee_rate"]),
                    fee_type=order.get("fee_type", "MAKER" if order["order_type"] == "LIMIT" else "TAKER"),
                    netted_quantity=_float(netted),
                    opened_quantity=_float(opened_qty),
                    fee_paid_total=_float(self.total_fees - netting_fees_before),
                    marketability_assumption="SUBMISSION_LAST_PRICE_PROXY" if order.get("marketable_limit") else ("RESTING_LIMIT_PROXY" if order["order_type"] == "LIMIT" else "MARKET_ORDER"),
                    fill_time=_iso(start),
                    observed_at=_iso(end),
                    stop_price=_float(lot["stop_price"]),
                    take_profit=_float(lot["take_profit"]),
                    protection_active_after=_iso(start),
                    synthetic=lot["synthetic"],
                )
                if partial:
                    self._emit(at=end, status="PARTIALLY_FILLED", action="ORDER_STATE", order_id=order["order_id"], position_id=position["position_id"], remaining_quantity=_float(order["remaining_qty"]), synthetic=lot["synthetic"])
                self._economic_guard(start, {instrument: fill_price}, cause="ENTRY_FEE_AND_EXPOSURE")
            else:
                if order_type != "MARKET":
                    continue
                position = self.positions.get(order["target_position_id"])
                if position is None:
                    self.orders.pop(order["order_id"], None)
                    continue
                fill_price = self._adverse_price(bar["open"], order["side"], order["price_tick"])
                qty = _round_step(min(order["remaining_qty"], capacity), order["amount_step"], ROUND_DOWN)
                if qty <= 0:
                    continue
                left = qty
                for lot in position["lots"]:
                    if left <= 0:
                        break
                    available_lot = lot["remaining_qty"]
                    if available_lot <= 0:
                        continue
                    take = min(available_lot, left)
                    done = self._close_lot(position, lot, take, fill_price, end, order_id=order["order_id"], reason="AI_REDUCE", synthetic=bool(bar.get("synthetic", False)))
                    left -= done
                filled = qty - left
                if filled <= 0:
                    self.orders.pop(order["order_id"], None)
                    continue
                order["remaining_qty"] -= filled
                capacity -= filled
                self.filled_order_ids.add(order["order_id"])
                partial = order["remaining_qty"] > 0
                if partial:
                    self.partial_order_ids.add(order["order_id"])
                else:
                    self.orders.pop(order["order_id"], None)
                self._emit(at=end, status="ORDER_PARTIAL" if partial else "ORDER_FILLED", action="REDUCE_POSITION", order_id=order["order_id"], position_id=position["position_id"], instrument_id=instrument, quantity=_float(filled), fill_time=_iso(start), observed_at=_iso(end), remaining_quantity=_float(order["remaining_qty"]), synthetic=bool(bar.get("synthetic", False)))
        return capacity

    def _adverse_price(self, price: Decimal, side: str, tick: Decimal) -> Decimal:
        slip = self.slippage_bps / Decimal("10000")
        multiplier = _ONE + slip if side == "LONG" else _ONE - slip
        raw = price * multiplier
        return _round_step(raw, tick, ROUND_UP if side == "LONG" else ROUND_DOWN)

    def _close_lot(
        self,
        position: dict[str, Any],
        lot: dict[str, Any],
        quantity: Decimal,
        price: Decimal,
        at: datetime,
        *,
        order_id: str,
        reason: str,
        synthetic: bool,
        gap_stop: bool = False,
        fee_rate: Decimal | None = None,
        fee_type: str = "TAKER",
    ) -> Decimal:
        qty = min(quantity, lot["remaining_qty"])
        qty = _round_step(qty, lot["amount_step"], ROUND_DOWN)
        if qty <= 0:
            return _ZERO
        if not self._economic_guard(at, {position["instrument_id"]: price}, cause="EXIT_PRICE_BEYOND_MAINTENANCE_PROXY"):
            return _ZERO
        rate = self.taker_fee_rate if fee_rate is None else fee_rate
        exit_fee = qty * price * lot["contract_size"] * rate
        sign = _ONE if position["side"] == "LONG" else -_ONE
        gross = (price - lot["entry_price"]) * qty * lot["contract_size"] * sign
        lot["remaining_qty"] -= qty
        lot["exit_fees"] += exit_fee
        lot["realized_gross"] += gross
        lot["exits"].append({"at": _iso(at), "quantity": qty, "price": price, "gross_pnl": gross, "fee": exit_fee, "order_id": order_id, "reason": reason})
        self.realized_gross_pnl += gross
        self.total_fees += exit_fee
        self.filled_order_ids.add(order_id)
        self._emit(
            at=at,
            status="FILLED",
            action=reason,
            order_id=order_id,
            position_id=position["position_id"],
            lot_id=lot["lot_id"],
            instrument_id=position["instrument_id"],
            side="SHORT" if position["side"] == "LONG" else "LONG",
            quantity=_float(qty),
            fill_price=_float(price),
            gross_pnl=_float(gross),
            fee=_float(exit_fee),
            fee_rate=_float(rate),
            fee_type=fee_type,
            gap_stop=gap_stop,
            fill_time=_iso(at),
            observed_at=_iso(at),
            synthetic=bool(synthetic or lot.get("synthetic", False)),
        )
        if lot["remaining_qty"] <= 0:
            lot["closed_at"] = _iso(at)
        if self._economic_guard(at, {position["instrument_id"]: price}, cause="EXIT_FEES_AND_REALIZED_EQUITY"):
            self._maybe_complete(position["position_id"], at)
        return qty

    def _maybe_complete(self, position_id: str, at: datetime) -> None:
        position = self.positions.get(position_id)
        if not position or position.get("completed"):
            return
        if any(lot["remaining_qty"] > 0 for lot in position["lots"]):
            return
        if any(order.get("kind") == "ENTRY" and order.get("position_id") == position_id for order in self.orders.values()):
            return
        if not position["lots"]:
            return
        net = sum((lot["realized_gross"] - lot["entry_fee"] - lot["exit_fees"] + lot["funding_pnl"] for lot in position["lots"]), _ZERO)
        trade = {
            "position_id": position_id,
            "instrument_id": position["instrument_id"],
            "side": position["side"],
            "opened_at": min(lot["opened_at"] for lot in position["lots"]),
            "closed_at": _iso(at),
            "quantity": sum((lot["quantity"] for lot in position["lots"]), _ZERO),
            "realized_gross_pnl": sum((lot["realized_gross"] for lot in position["lots"]), _ZERO),
            "fees": sum((lot["entry_fee"] + lot["exit_fees"] for lot in position["lots"]), _ZERO),
            "funding_pnl": sum((lot["funding_pnl"] for lot in position["lots"]), _ZERO),
            "net_pnl": net,
            "synthetic": any(lot.get("synthetic", False) for lot in position["lots"]),
        }
        self.completed_trades.append(trade)
        position["completed"] = True
        self._emit(at=at, status="CLOSED", action="TRADE_COMPLETE", position_id=position_id, instrument_id=position["instrument_id"], net_pnl=_float(net), win=net > 0, synthetic=trade["synthetic"])

    def apply_funding(self, records: list[dict[str, Any]], through: datetime) -> list[dict[str, Any]]:
        """Apply timestamped historical funding without exposing future rows.

        Funding is charged to the amount open at ``payment_time``.  If a
        provider supplies ``available_at``/``known_at``, that timestamp is
        also enforced; absent availability metadata is conservatively treated
        as available at the payment time, never before it.
        """
        cutoff = _time(through, name="through")
        if self.halted_reason:
            return []
        if not isinstance(records, list):
            return []
        output = []
        normalized = []
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                payment = _time(record.get("payment_time", record.get("timestamp")), name="payment_time")
                known_raw = record.get("available_at", record.get("known_at"))
                known = _time(known_raw, name="funding_available_at") if known_raw else payment
                if payment > cutoff or known > cutoff:
                    continue
                instrument = str(record.get("instrument_id") or record.get("symbol") or "").strip()
                if not instrument:
                    continue
                rate = _decimal(record.get("rate", record.get("funding_rate")), name="funding_rate")
                mark_raw = record.get("mark_price", record.get("price"))
                if mark_raw is None:
                    mark_raw = self.last_prices.get(instrument)
                if mark_raw is None:
                    continue
                mark = _decimal(mark_raw, name="funding_mark_price")
                if mark <= 0:
                    continue
                normalized.append((payment, known, instrument, rate, mark, record))
            except ValueError:
                continue
        normalized.sort(key=lambda item: (item[0], item[2]))
        for payment, known, instrument, rate, mark, record in normalized:
            if self.halted_reason:
                break
            key = f"{instrument}|{_iso(payment)}"
            if key in self.processed_funding:
                continue
            applied = _ZERO
            for position in self.positions.values():
                if position["instrument_id"] != instrument:
                    continue
                sign = _ONE if position["side"] == "LONG" else -_ONE
                for lot in position["lots"]:
                    opened = _time(lot["opened_at"])
                    if opened > payment:
                        continue
                    remaining_at_payment = lot["quantity"] - sum(
                        (exit_row["quantity"] for exit_row in lot.get("exits", []) if _time(exit_row["at"]) <= payment),
                        _ZERO,
                    )
                    remaining_at_payment = max(_ZERO, remaining_at_payment)
                    if remaining_at_payment <= 0:
                        continue
                    funding = -sign * remaining_at_payment * lot["contract_size"] * mark * rate
                    lot["funding_pnl"] += funding
                    lot.setdefault("funding_entries", []).append({"key": key, "at": _iso(payment), "available_at": _iso(known), "quantity": remaining_at_payment, "rate": rate, "mark_price": mark, "funding_pnl": funding, "source": str(record.get("source") or "HISTORICAL_FUNDING")})
                    applied += funding
            self.funding_pnl += applied
            self.processed_funding.add(key)
            for trade in self.completed_trades:
                if trade["instrument_id"] == instrument:
                    position = self.positions.get(trade["position_id"])
                    if position is None:
                        continue
                    trade["funding_pnl"] = sum((lot["funding_pnl"] for lot in position["lots"]), _ZERO)
                    trade["net_pnl"] = trade["realized_gross_pnl"] - trade["fees"] + trade["funding_pnl"]
                    for event in self.events:
                        if event.get("action") == "TRADE_COMPLETE" and event.get("position_id") == trade["position_id"]:
                            event["net_pnl"] = _float(trade["net_pnl"])
                            event["win"] = trade["net_pnl"] > 0
            output.append(self._emit(at=payment, status="APPLIED" if applied != 0 else "NO_EXPOSURE", action="FUNDING", instrument_id=instrument, funding_pnl=_float(applied), rate=_float(rate), mark_price=_float(mark), payment_time=_iso(payment), available_at=_iso(known), source=str(record.get("source") or "HISTORICAL_FUNDING"), simulation=True))
            self._economic_guard(payment, {instrument: mark}, cause="FUNDING_EQUITY")
        return output

    def account_truth(self, prices: dict[str, Any], as_of: datetime) -> dict[str, Any]:
        """Return a production-shaped but explicitly synthetic account view."""
        now = _time(as_of, name="as_of")
        if self.halted_reason:
            return {"status": "UNAVAILABLE", "account_id": self.account_id,
                "provider": "ai_replay_simulation", "source": "AI_REPLAY_SIMULATION",
                "environment": "REPLAY", "api_environment": "REPLAY", "observed_at": self.halted_at,
                "error_code": self.halted_reason, "halted_reason": self.halted_reason,
                "economic_eligible": False, "equity": None, "available_margin": None,
                "positions": [], "pending_orders": [], "managed_state": "HALTED",
                "positions_status": "UNAVAILABLE", "pending_orders_status": "UNAVAILABLE",
                "simulation": True, "remote_truth": False}
        if self.last_advanced_through and now < _time(self.last_advanced_through):
            return {
                "status": "UNAVAILABLE",
                "account_id": self.account_id,
                "provider": "ai_replay_simulation",
                "source": "AI isolated historical simulation",
                "environment": "REPLAY",
                "api_environment": "REPLAY",
                "observed_at": _iso(now),
                "error_code": "REPLAY_ASOF_BEFORE_STATE",
                "equity": None,
                "available_margin": None,
                "used_margin": None,
                "unrealized_pnl": None,
                "positions": [],
                "pending_orders": [],
                "owned_entry_orders": [],
                "owned_protection_orders": [],
                "managed_state": "UNAVAILABLE",
                "positions_status": "UNAVAILABLE",
                "pending_orders_status": "UNAVAILABLE",
                "simulation": True,
                "remote_truth": False,
            }
        if isinstance(prices, dict) and not self.halted_reason:
            for instrument, raw in prices.items():
                if isinstance(raw, dict):
                    timestamp = raw.get("as_of", raw.get("timestamp", raw.get("observed_at", raw.get("bar_end"))))
                    if timestamp is not None:
                        try:
                            if _time(timestamp, name="price_timestamp") > now:
                                continue
                        except ValueError:
                            continue
                    raw = raw.get("price", raw.get("mark_price", raw.get("close")))
                try:
                    price = _decimal(raw, name="mark_price")
                    if price > 0:
                        self.last_prices[str(instrument)] = price
                except ValueError:
                    continue
        equity, unrealized, used = self._equity()
        cap, _, reserved, available = self._budget()
        positions = []
        owned_protection = []
        for position in self.positions.values():
            active = [lot for lot in position["lots"] if lot["remaining_qty"] > 0]
            if not active:
                continue
            qty = sum((lot["remaining_qty"] for lot in active), _ZERO)
            contract_size = active[0]["contract_size"]
            weighted_entry = sum((lot["entry_price"] * lot["remaining_qty"] for lot in active), _ZERO) / qty
            stops = {lot["stop_price"] for lot in active}
            targets = {lot["take_profit"] for lot in active}
            mark = self.last_prices.get(position["instrument_id"], position.get("last_mark", weighted_entry))
            sign = _ONE if position["side"] == "LONG" else -_ONE
            unreal = (mark - weighted_entry) * qty * contract_size * sign
            row = {
                "position_id": position["position_id"],
                "instrument_id": position["instrument_id"],
                "symbol": position["instrument_id"],
                "side": position["side"],
                "contracts": _float(qty),
                "quantity": _float(qty),
                "contract_size": _float(contract_size),
                "entry_price": _float(weighted_entry),
                "mark_price": _float(mark),
                "stop_price": _float(next(iter(stops))) if len(stops) == 1 else None,
                "take_profit": _float(next(iter(targets))) if len(targets) == 1 else None,
                "opened_at": min(lot["opened_at"] for lot in active),
                "leverage": _float(position["leverage"]),
                "unrealized_pnl": _float(unreal),
                "used_margin": _float(sum((lot["remaining_qty"] * mark * contract_size / lot["leverage"] for lot in active), _ZERO)),
                "ownership_status": "VERIFIED_SYSTEM",
                "ownership_role": "MANAGED_SYSTEM_POSITION",
                "source": "AI_REPLAY_SIMULATION",
                "synthetic": any(lot.get("synthetic", False) for lot in active),
            }
            positions.append(row)
            for lot in active:
                owned_protection.append({
                    "position_id": position["position_id"],
                    "lot_id": lot["lot_id"],
                    "instrument_id": position["instrument_id"],
                    "quantity": _float(lot["remaining_qty"]),
                    "stop_price": _float(lot["stop_price"]),
                    "take_profit": _float(lot["take_profit"]),
                    "status": "ACTIVE",
                    "ownership_status": "VERIFIED_SYSTEM",
                    "ownership_role": "MANAGED_SYSTEM_PROTECTION",
                    "source": "AI_REPLAY_SIMULATION",
                })
        pending = [
            {
                "order_id": order["order_id"],
                "position_id": order["position_id"],
                "instrument_id": order["instrument_id"],
                "symbol": order["instrument_id"],
                "side": order["side"],
                "order_type": order["order_type"],
                "kind": order["kind"],
                "quantity": _float(order["requested_qty"]),
                "remaining_quantity": _float(order["remaining_qty"]),
                "limit_price": _float(order.get("limit_price")),
                "submitted_at": order["submitted_at"],
                "expires_at": order["expires_at"],
                "status": "OPEN",
                "ownership_status": "SYSTEM_ORDER_ID_MATCH",
                "ownership_role": "MANAGED_SYSTEM_ENTRY" if order["kind"] == "ENTRY" else "MANAGED_SYSTEM_EXIT",
                "reduce_only": order["kind"] == "EXIT",
                "source": "AI_REPLAY_SIMULATION",
            }
            for order in self.orders.values()
        ]
        owned_entries = [item for item in pending if item["kind"] == "ENTRY"]
        fills = [
            {
                "event_id": event.get("event_id"),
                "order_id": event.get("order_id"),
                "position_id": event.get("position_id"),
                "instrument_id": event.get("instrument_id"),
                "side": event.get("side"),
                "quantity": event.get("quantity"),
                "price": event.get("fill_price"),
                "fee": event.get("fee"),
                "timestamp": event.get("fill_time", event.get("event_time")),
                "source": "AI_REPLAY_SIMULATION",
                "synthetic": event.get("synthetic", False),
            }
            for event in self.events if event.get("fill_price") is not None
        ][-100:]
        self._mark_history(now)
        return {
            "status": "AVAILABLE",
            "account_id": self.account_id,
            "provider": "ai_replay_simulation",
            "source": "AI isolated historical simulation",
            "environment": "REPLAY",
            "api_environment": "REPLAY",
            "observed_at": _iso(now),
            "equity": _float(equity),
            "available_margin": _float(available),
            "used_margin": _float(used),
            "reserved_margin": _float(reserved),
            "margin_cap": _float(cap),
            "unrealized_pnl": _float(unrealized),
            "realized_pnl": _float(self.realized_gross_pnl),
            "fees": _float(self.total_fees),
            "funding_pnl": _float(self.funding_pnl),
            "balance": {"equity": _float(equity), "data_status": "AVAILABLE", "source": "AI_REPLAY_SIMULATION"},
            "positions": positions,
            "positions_status": "AVAILABLE",
            "pending_orders": pending,
            "pending_orders_status": "AVAILABLE",
            "owned_entry_orders": owned_entries,
            "owned_protection_orders": owned_protection,
            "managed_state": "MANAGED_SYSTEM_POSITIONS" if positions else ("PENDING_SYSTEM_ORDERS" if pending else "FLAT"),
            "fills": fills,
            "fills_status": "AVAILABLE",
            "remote_truth": False,
            "simulation": True,
            "synthetic": any(item.get("synthetic", False) for item in positions),
        }

    def summary(self, prices: dict[str, Any]) -> dict[str, Any]:
        """Return economic performance, counts, and the simulator assumptions."""
        if isinstance(prices, dict) and not self.halted_reason:
            for instrument, raw in prices.items():
                if isinstance(raw, dict):
                    raw = raw.get("price", raw.get("mark_price", raw.get("close")))
                try:
                    mark = _decimal(raw, name="summary_mark")
                    if mark > 0:
                        self.last_prices[str(instrument)] = mark
                except ValueError:
                    continue
        equity, unrealized, used = self._equity()
        _, _, reserved, available = self._budget()
        if self.equity_history and not self.halted_reason:
            previous_at = self.equity_history[-1].get("at")
            if previous_at is None:
                self.equity_history[-1] = {"at": None, "equity": equity}
            else:
                self._mark_history(_time(previous_at))
        peak = self.initial_equity
        max_dd = _ZERO
        max_dd_amount = _ZERO
        for row in self.equity_history:
            value = row["equity"]
            peak = max(peak, value)
            drawdown_amount = max(_ZERO, peak - value)
            drawdown = drawdown_amount / peak if peak > 0 else _ZERO
            max_dd = max(max_dd, drawdown)
            max_dd_amount = max(max_dd_amount, drawdown_amount)
        closed_count = len(self.completed_trades)
        wins = sum(1 for trade in self.completed_trades if trade["net_pnl"] > 0)
        losses = sum(1 for trade in self.completed_trades if trade["net_pnl"] < 0)
        net_winners = sum((trade["net_pnl"] for trade in self.completed_trades if trade["net_pnl"] > 0), _ZERO)
        net_losers = abs(sum((trade["net_pnl"] for trade in self.completed_trades if trade["net_pnl"] < 0), _ZERO))
        profit_factor = net_winners / net_losers if net_losers > 0 else None
        fees_by_type = {
            "maker": sum((_decimal(event.get("fee", 0), name="event_fee") for event in self.events if event.get("fee_type") == "MAKER"), _ZERO),
            "taker": sum((_decimal(event.get("fee", 0), name="event_fee") for event in self.events if event.get("fee_type") == "TAKER"), _ZERO),
        }
        count = lambda status, action=None: sum(
            1 for event in self.events
            if event.get("status") == status and (action is None or event.get("action") == action)
        )
        return {
            "account_id": self.account_id,
            "simulation": True,
            "source": "AI isolated historical simulation; not Gate fill evidence",
            "initial_equity": _float(self.initial_equity),
            "ending_equity": _float(equity) if not self.halted_reason else None,
            "net_equity": _float(equity) if not self.halted_reason else None,
            "roi": _float(equity / self.initial_equity - _ONE) if not self.halted_reason else None,
            "economic_eligible": not self.halted_reason,
            "halted_reason": self.halted_reason,
            "halted_at": self.halted_at,
            "diagnostic_equity": _float(equity) if self.halted_reason else None,
            "halted_diagnostic": {key: (_float(value) if isinstance(value, Decimal) else value) for key, value in (self.halted_diagnostic or {}).items()},
            "position_mode": "ONE_WAY",
            "realized_gross_pnl": _float(self.realized_gross_pnl),
            "unrealized_pnl": _float(unrealized),
            "fees": _float(self.total_fees),
            "maker_fees": _float(fees_by_type["maker"]),
            "taker_fees": _float(fees_by_type["taker"]),
            "funding_pnl": _float(self.funding_pnl),
            "used_margin": _float(used),
            "reserved_margin": _float(reserved),
            "available_margin": _float(available),
            "closed_trade_count": closed_count,
            "settled_trades": closed_count,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / closed_count if closed_count else None,
            "profit_factor": _float(profit_factor),
            "profit_factor_status": "DEFINED" if profit_factor is not None else ("NO_LOSING_TRADES" if closed_count > 0 else "NO_CLOSED_TRADES"),
            "max_drawdown": _float(max_dd),
            "max_drawdown_amount": _float(max_dd_amount),
            "drawdown_time_basis": "OBSERVED_ONE_MINUTE_BAR_CLOSES_WITH_HALT_DIAGNOSTIC_MARK",
            "fills": count("FILLED") + count("PARTIAL_FILL"),
            "filled_order_count": len(self.filled_order_ids),
            "accepted_entry_order_count": len({event.get("order_id") for event in self.events
                if event.get("status") == "ACCEPTED" and event.get("action") in {"OPEN_LONG", "OPEN_SHORT"}}),
            "filled_entry_order_count": len({event.get("order_id") for event in self.events
                if event.get("status") in {"FILLED", "PARTIAL_FILL"} and event.get("action") in {"OPEN_LONG", "OPEN_SHORT"}}),
            "pending_order_count": len(self.orders),
            "pending_orders": len(self.orders),
            "rejected_orders": count("REJECTED"),
            "expired_orders": count("EXPIRED"),
            "partial_fill_orders": len(self.partial_order_ids),
            "completed_trades": [
                {key: (_float(value) if isinstance(value, Decimal) else value) for key, value in trade.items()}
                for trade in self.completed_trades
            ],
            "simulation_assumptions": list(_SIM_ASSUMPTIONS),
            "known_limitations": [
            "No historical order book, queue position, cancellation race, or intrabar path is available.",
                "If stop and target both cross one bar, stop is chosen; a limit entry crossing before a farther stop is a conservative path assumption, not an observed tick sequence.",
                "Limit fills require strict OHLC crossing but cannot prove a real exchange fill.",
                "Funding is included only when timestamped historical funding records are provided.",
                "Marketable limits use submission last-price and subsequent bar-price proxies; maker/taker status cannot be proven without historical order-book data.",
                "Cross-margin liquidation and historical risk tiers are unavailable. A nonpositive equity or public maintenance-proxy breach halts the account and invalidates ROI rather than inventing a settlement.",
            ],
        }
