"""Read-only diagnostics for fixed and risk-budgeted Gate entry proposals.

This module never submits or resizes an order. It calls the same Decimal cost
calculation used by the Gate gateway and historical replay, then reports which
policy or venue conditions an unchanged proposal would pass or fail.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from .entry_economics import (
    EntryEconomicsError,
    calculate_gate_entry_economics,
    quantity_for_notional,
)

FIXED_NOTIONAL = "FIXED_NOTIONAL"
RISK_BUDGETED_NOTIONAL = "RISK_BUDGETED_NOTIONAL"
FEASIBILITY_MODES = {FIXED_NOTIONAL, RISK_BUDGETED_NOTIONAL}


def _decimal(name: str, value: Any, *, positive: bool = False,
             non_negative: bool = False, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or value is None:
        raise EntryEconomicsError("TRADE_FEASIBILITY_INVALID", f"{name} is missing or invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EntryEconomicsError("TRADE_FEASIBILITY_INVALID", f"{name} is missing or invalid") from exc
    if (not number.is_finite() or (positive and number <= 0)
            or (non_negative and number < 0)):
        raise EntryEconomicsError("TRADE_FEASIBILITY_INVALID", f"{name} is outside its allowed range")
    return number


def _step_floor(quantity: Decimal, step: Decimal) -> Decimal:
    return (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _str(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _unique_append(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _budgeted_capacity(
    *, side: str, order_type: str, quote: Decimal, entry: Decimal,
    stop: Decimal, target: Decimal, contract_size: Decimal,
    amount_step: Decimal, price_tick: Decimal, fee: Decimal,
    slippage: Decimal, equity: Decimal | None, risk_pct: Decimal,
    leverage: Decimal, available_margin: Decimal | None,
    max_margin_pct: Decimal | None, min_amount: Decimal | None,
    max_amount: Decimal | None,
    max_notional: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    if equity is None:
        return None, None
    unit = calculate_gate_entry_economics(
        side=side, order_type=order_type, quote=quote, entry_price=entry,
        stop_price=stop, target_price=target, quantity=amount_step,
        contract_size=contract_size, price_tick=price_tick,
        amount_step=amount_step, taker_fee_rate=fee,
        slippage_rate=slippage, equity=equity,
        risk_per_trade_pct=risk_pct,
    )
    budget = equity * risk_pct / Decimal(100)
    if unit.estimated_stop_risk_usdt <= 0:
        raise EntryEconomicsError("TRADE_FEASIBILITY_INVALID", "unit stop risk must be positive")
    risk_steps = (budget / unit.estimated_stop_risk_usdt).to_integral_value(rounding=ROUND_FLOOR)
    quantity = risk_steps * amount_step
    if max_amount is not None:
        quantity = min(quantity, _step_floor(max_amount, amount_step))
    unit_margin = unit.notional_usdt / leverage + unit.notional_usdt * fee * Decimal(2)
    margin_caps: list[Decimal] = []
    if available_margin is not None:
        margin_caps.append(available_margin)
    if max_margin_pct is not None:
        margin_caps.append(equity * max_margin_pct / Decimal(100))
    if margin_caps:
        if unit_margin <= 0:
            raise EntryEconomicsError("TRADE_FEASIBILITY_INVALID", "unit margin must be positive")
        margin_cap = min(margin_caps)
        margin_steps = (margin_cap / unit_margin).to_integral_value(rounding=ROUND_FLOOR)
        quantity = min(quantity, margin_steps * amount_step)
    if max_notional is not None and unit.notional_usdt > 0:
        notional_steps = (max_notional / unit.notional_usdt).to_integral_value(rounding=ROUND_FLOOR)
        quantity = min(quantity, notional_steps * amount_step)
    if min_amount is not None and quantity < min_amount:
        quantity = Decimal(0)
    capacity = calculate_gate_entry_economics(
        side=side, order_type=order_type, quote=quote, entry_price=entry,
        stop_price=stop, target_price=target, quantity=quantity,
        contract_size=contract_size, price_tick=price_tick,
        amount_step=amount_step, taker_fee_rate=fee,
        slippage_rate=slippage, equity=equity,
        risk_per_trade_pct=risk_pct,
    ) if quantity > 0 else None
    return quantity, capacity.notional_usdt if capacity is not None else Decimal(0)


def diagnose_trade_proposal(
    *, mode: str, side: str, order_type: str,
    proposed_notional_usdt: Any, entry_price: Any, stop_price: Any,
    target_price: Any, requested_leverage: Any, equity: Any | None,
    risk_per_trade_pct: Any, min_net_rr: Any, taker_fee_rate: Any,
    slippage_rate: Any, contract_size: Any, amount_step: Any,
    price_tick: Any, quote: Any | None = None,
    fixed_notional_usdt: Any = "2000", available_margin: Any | None = None,
    max_margin_pct: Any | None = None, min_amount: Any | None = None,
    max_amount: Any | None = None, min_notional: Any | None = None,
    max_notional: Any | None = None, max_leverage: Any | None = None,
    quote_source: str | None = None,
) -> dict[str, Any]:
    """Return a proposal-only decision record without changing its requested size."""
    mode = str(mode or "").strip().upper()
    side = str(side or "").strip().upper()
    order_type = str(order_type or "").strip().lower()
    reasons: list[str] = []
    lifecycle = {
        "stage": "PROPOSAL_ONLY",
        "gateway_acceptance": "NOT_OBSERVED",
        "venue_fill": "NOT_OBSERVED",
        "complete_close": "NOT_OBSERVED",
    }
    result: dict[str, Any] = {
        "mode": mode,
        "side": side,
        "order_type": order_type.upper(),
        "lifecycle": lifecycle,
        "feasible": False,
        "status": "INVALID",
        "reason_codes": reasons,
        "diagnostic_only": True,
        "risk_policy_resized_proposal": False,
    }
    try:
        if mode not in FEASIBILITY_MODES:
            raise EntryEconomicsError("TRADE_FEASIBILITY_MODE_INVALID")
        if side not in {"LONG", "SHORT"} or order_type not in {"limit", "market"}:
            raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID")

        proposed = _decimal("proposed_notional_usdt", proposed_notional_usdt, positive=True)
        fixed = _decimal("fixed_notional_usdt", fixed_notional_usdt, positive=True)
        entry_raw = _decimal("entry_price", entry_price, positive=True)
        stop_raw = _decimal("stop_price", stop_price, positive=True)
        target_raw = _decimal("target_price", target_price, positive=True)
        leverage = _decimal("requested_leverage", requested_leverage, positive=True)
        risk_pct = _decimal("risk_per_trade_pct", risk_per_trade_pct, positive=True)
        rr_floor = _decimal("min_net_rr", min_net_rr, positive=True)
        fee = _decimal("taker_fee_rate", taker_fee_rate, non_negative=True)
        slippage = _decimal("slippage_rate", slippage_rate, non_negative=True)
        contract = _decimal("contract_size", contract_size, positive=True)
        step = _decimal("amount_step", amount_step, positive=True)
        tick = _decimal("price_tick", price_tick, positive=True)
        equity_dec = _decimal("equity", equity, positive=True, optional=True)
        available_dec = _decimal("available_margin", available_margin, non_negative=True, optional=True)
        max_margin = _decimal("max_margin_pct", max_margin_pct, positive=True, optional=True)
        min_qty = _decimal("min_amount", min_amount, positive=True, optional=True)
        max_qty = _decimal("max_amount", max_amount, positive=True, optional=True)
        min_value = _decimal("min_notional", min_notional, positive=True, optional=True)
        notional_cap = _decimal("max_notional", max_notional, positive=True, optional=True)
        leverage_cap = _decimal("max_leverage", max_leverage, positive=True, optional=True)
        quote_dec = _decimal("quote", quote if quote is not None else entry_raw, positive=True)
        if leverage != leverage.to_integral_value():
            raise EntryEconomicsError("TRADE_FEASIBILITY_INVALID", "requested leverage must be an integer")
        if risk_pct > Decimal(100):
            raise EntryEconomicsError("TRADE_FEASIBILITY_INVALID", "risk_per_trade_pct must be percentage points")
        if max_margin is not None and max_margin > Decimal(100):
            raise EntryEconomicsError("TRADE_FEASIBILITY_INVALID", "max_margin_pct must not exceed 100")
        if min_qty is None or max_qty is None or leverage_cap is None:
            _unique_append(reasons, "GATE_CONTRACT_LIMITS_UNAVAILABLE")

        evaluated_notional = fixed if mode == FIXED_NOTIONAL else proposed
        from .entry_economics import quantize_gate_prices
        entry, stop, target = quantize_gate_prices(
            side=side, order_type=order_type, quote=quote_dec,
            entry_price=entry_raw, stop_price=stop_raw,
            target_price=target_raw, price_tick=tick,
        )
        quantity = quantity_for_notional(
            notional_usdt=evaluated_notional, entry_price=entry,
            order_type=order_type, side=side, contract_size=contract,
            amount_step=step, slippage_rate=slippage,
        )

        result.update({
            "model_proposed_notional_usdt": _str(proposed),
            "evaluated_notional_target_usdt": _str(evaluated_notional),
            "fixed_notional_target_usdt": _str(fixed) if mode == FIXED_NOTIONAL else None,
            "fixed_target_differs_from_model_proposal": mode == FIXED_NOTIONAL and proposed != fixed,
            "requested_leverage": _str(leverage),
            "entry_price": _str(entry),
            "stop_price": _str(stop),
            "take_profit": _str(target),
            "quote_price": _str(quote_dec),
            "quote_source": quote_source or ("EXPLICIT_INPUT" if quote is not None else "ENTRY_PRICE_PROXY"),
            "contract_size": _str(contract),
            "amount_step": _str(step),
            "price_tick": _str(tick),
            "taker_fee_rate_each_leg": _str(fee),
            "slippage_rate": _str(slippage),
            "equity_usdt": _str(equity_dec),
            "risk_per_trade_pct": _str(risk_pct),
            "max_stop_risk_usdt": _str(equity_dec * risk_pct / Decimal(100)) if equity_dec is not None else None,
            "available_margin_usdt": _str(available_dec),
            "minimum_contract_amount": _str(min_qty),
            "maximum_contract_amount": _str(max_qty),
            "minimum_notional_usdt": _str(min_value),
            "maximum_notional_usdt": _str(notional_cap),
            "maximum_leverage": _str(leverage_cap),
            "amount_step_rounding_applied": True,
            "quantity": _str(quantity),
        })
        if quantity <= 0:
            _unique_append(reasons, "QUANTITY_BELOW_AMOUNT_STEP")
            result["minimum_viable_equity_usdt"] = None
            result["risk_budgeted_max_notional_usdt"] = None
        else:
            economics = calculate_gate_entry_economics(
                side=side, order_type=order_type, quote=quote_dec,
                entry_price=entry, stop_price=stop, target_price=target,
                quantity=quantity, contract_size=contract, price_tick=tick,
                amount_step=step, taker_fee_rate=fee,
                slippage_rate=slippage, equity=equity_dec,
                risk_per_trade_pct=risk_pct,
            )
            result.update({
                "executable_notional_usdt": _str(economics.notional_usdt),
                "amount_step_notional_shortfall_usdt": _str(max(Decimal(0), evaluated_notional - economics.notional_usdt)),
                "estimated_stop_risk_usdt": _str(economics.estimated_stop_risk_usdt),
                "estimated_target_net_usdt": _str(economics.estimated_target_net_usdt),
                "net_reward_risk": _str(economics.net_reward_risk),
                "net_reward_risk_pass": economics.net_reward_risk >= rr_floor,
                "stop_risk_within_budget": (
                    economics.estimated_stop_risk_usdt <= equity_dec * risk_pct / Decimal(100)
                    if equity_dec is not None else None
                ),
                "estimated_opening_margin_usdt": _str(
                    economics.notional_usdt / leverage + economics.notional_usdt * fee * Decimal(2)
                ),
                "minimum_viable_equity_usdt": _str(
                    economics.estimated_stop_risk_usdt * Decimal(100) / risk_pct
                ),
                "minimum_viable_equity_basis": (
                    "MAX_OF_STOP_RISK_BUDGET_AND_STRATEGY_MARGIN_PERCENT; "
                    "EXCLUDES_UNKNOWN_OR_INSUFFICIENT_AVAILABLE_MARGIN"
                ),
                "cost_basis": economics.audit_dict()["cost_basis"],
            })
            if economics.net_reward_risk < rr_floor:
                _unique_append(reasons, "AI_NET_REWARD_RISK_TOO_LOW")
            if equity_dec is None:
                _unique_append(reasons, "ACCOUNT_EQUITY_UNAVAILABLE")
            elif economics.estimated_stop_risk_usdt > equity_dec * risk_pct / Decimal(100):
                _unique_append(reasons, "STOP_RISK_LIMIT_EXCEEDED")
            if min_qty is not None and quantity < min_qty:
                _unique_append(reasons, "GATE_AMOUNT_BELOW_CONTRACT_MINIMUM")
            if max_qty is not None and quantity > max_qty:
                _unique_append(reasons, "GATE_AMOUNT_ABOVE_CONTRACT_MAXIMUM")
            if min_value is not None and economics.notional_usdt < min_value:
                _unique_append(reasons, "GATE_NOTIONAL_BELOW_CONTRACT_MINIMUM")
            if notional_cap is not None and economics.notional_usdt > notional_cap:
                _unique_append(reasons, "STRATEGY_NOTIONAL_CAP_EXCEEDED")
            if leverage_cap is not None and leverage > leverage_cap:
                _unique_append(reasons, "GATE_LEVERAGE_LIMIT_EXCEEDED")
            if available_dec is None:
                _unique_append(reasons, "AVAILABLE_MARGIN_UNAVAILABLE")
                result["margin_within_available"] = None
            elif economics.notional_usdt / leverage + economics.notional_usdt * fee * Decimal(2) > available_dec:
                _unique_append(reasons, "MARGIN_INSUFFICIENT")
                result["margin_within_available"] = False
            else:
                result["margin_within_available"] = True
            if (max_margin is not None and equity_dec is not None
                    and economics.notional_usdt / leverage + economics.notional_usdt * fee * Decimal(2)
                    > equity_dec * max_margin / Decimal(100)):
                _unique_append(reasons, "STRATEGY_MARGIN_CAP_EXCEEDED")
                result["strategy_margin_cap_pass"] = False
            elif max_margin is not None and equity_dec is not None:
                result["strategy_margin_cap_pass"] = True
            else:
                result["strategy_margin_cap_pass"] = None
            result["price_precision_pass"] = True
            result["amount_precision_pass"] = quantity / step == (quantity / step).to_integral_value()
            result["venue_limits_pass"] = (
                not any(code in reasons for code in (
                    "GATE_AMOUNT_BELOW_CONTRACT_MINIMUM",
                    "GATE_AMOUNT_ABOVE_CONTRACT_MAXIMUM",
                    "GATE_NOTIONAL_BELOW_CONTRACT_MINIMUM",
                    "GATE_LEVERAGE_LIMIT_EXCEEDED",
                ))
                if min_qty is not None and max_qty is not None and leverage_cap is not None else None
            )
            if (mode == RISK_BUDGETED_NOTIONAL and equity_dec is not None
                    and economics.estimated_stop_risk_usdt > equity_dec * risk_pct / Decimal(100)):
                _unique_append(reasons, "PROPOSAL_EXCEEDS_RISK_BUDGETED_CAPACITY")
            unit_margin_pct = (
                economics.notional_usdt / leverage + economics.notional_usdt * fee * Decimal(2)
            )
            if max_margin is not None and max_margin > 0:
                minimum_margin_equity = unit_margin_pct * Decimal(100) / max_margin
                minimum_risk_equity = economics.estimated_stop_risk_usdt * Decimal(100) / risk_pct
                result["minimum_viable_equity_usdt"] = _str(max(minimum_margin_equity, minimum_risk_equity))
            capacity_qty, capacity_notional = _budgeted_capacity(
                side=side, order_type=order_type, quote=quote_dec,
                entry=entry, stop=stop, target=target, contract_size=contract,
                amount_step=step, price_tick=tick, fee=fee, slippage=slippage,
                equity=equity_dec, risk_pct=risk_pct, leverage=leverage,
                available_margin=available_dec, max_margin_pct=max_margin,
                min_amount=min_qty, max_amount=max_qty, max_notional=notional_cap,
            )
            result["risk_budgeted_max_quantity"] = _str(capacity_qty)
            result["risk_budgeted_max_notional_usdt"] = _str(capacity_notional)
            result["risk_budgeted_proposal_within_capacity"] = (
                capacity_notional is not None and economics.notional_usdt <= capacity_notional
            )
        if equity_dec is None:
            _unique_append(reasons, "ACCOUNT_EQUITY_UNAVAILABLE")
        result["feasible"] = not reasons
        unknown_codes = {
            "ACCOUNT_EQUITY_UNAVAILABLE", "AVAILABLE_MARGIN_UNAVAILABLE",
            "GATE_CONTRACT_LIMITS_UNAVAILABLE",
        }
        result["economic_feasible_under_known_inputs"] = not any(
            code not in unknown_codes for code in reasons
        )
        result["status"] = "ELIGIBLE_PROPOSAL" if not reasons else (
            "BLOCKED" if all(code in unknown_codes for code in reasons) else "REJECTED"
        )
        return result
    except EntryEconomicsError as exc:
        _unique_append(reasons, exc.code)
        result["reason_codes"] = reasons
        result["status"] = "INVALID"
        result["feasible"] = False
        return result
    except (ArithmeticError, TypeError, ValueError) as exc:
        _unique_append(reasons, "TRADE_FEASIBILITY_INVALID")
        result["error_detail"] = str(exc)
        result["status"] = "INVALID"
        result["feasible"] = False
        return result
