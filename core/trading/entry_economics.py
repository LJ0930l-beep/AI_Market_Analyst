"""Shared, fail-closed economics for autonomous Gate entries and replay.

All calculations use Decimal values and the same adverse price/fee assumptions
at model preflight, the execution gateway, and the historical simulation sink.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any


class EntryEconomicsError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


PRICE_ACTION_MIN_NET_RR = Decimal("2.0")
_UNSET = object()


def effective_min_net_rr(template_id: Any, configured_min_net_rr: Any) -> Decimal:
    """Keep the PA strategy's registered 2.0 net-RR floor immutable.

    Stronger configured thresholds remain valid. Other strategies continue to
    use their own active policy value.
    """
    configured = _decimal("min_net_rr", configured_min_net_rr, positive=True)
    if str(template_id or "").strip().lower() == "price_action_structure":
        return max(configured, PRICE_ACTION_MIN_NET_RR)
    return configured


@dataclass(frozen=True)
class GateEntryEconomics:
    side: str
    order_type: str
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    quantity: Decimal
    contract_size: Decimal
    notional_usdt: Decimal
    estimated_stop_risk_usdt: Decimal
    max_stop_risk_usdt: Decimal | None
    estimated_target_net_usdt: Decimal
    net_reward_risk: Decimal
    taker_fee_rate: Decimal
    slippage_rate: Decimal
    price_tick: Decimal
    amount_step: Decimal

    def audit_dict(self) -> dict[str, str]:
        return {
            "side": self.side,
            "order_type": self.order_type,
            "entry_price": str(self.entry_price),
            "stop_price": str(self.stop_price),
            "target_price": str(self.target_price),
            "quantity": str(self.quantity),
            "contract_size": str(self.contract_size),
            "notional_usdt": str(self.notional_usdt),
            "estimated_stop_risk_usdt": str(self.estimated_stop_risk_usdt),
            "max_stop_risk_usdt": str(self.max_stop_risk_usdt) if self.max_stop_risk_usdt is not None else "DEFERRED_TO_ATOMIC_REMOTE_EQUITY_CHECK",
            "estimated_target_net_usdt": str(self.estimated_target_net_usdt),
            "net_reward_risk": str(self.net_reward_risk),
            "taker_fee_rate": str(self.taker_fee_rate),
            "slippage_rate": str(self.slippage_rate),
            "price_tick": str(self.price_tick),
            "amount_step": str(self.amount_step),
            "cost_basis": "TAKER_FEE_BOTH_LEGS; MARKET_ENTRY_AND_EXIT_SLIPPAGE; LIMIT_ENTRY_PRICE_CAPPED_AND_EXIT_SLIPPAGE",
        }


def _decimal(name: str, value: Any, *, positive: bool = False, non_negative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID", f"{name} is missing or invalid") from exc
    if not result.is_finite() or (positive and result <= 0) or (non_negative and result < 0):
        raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID", f"{name} is outside its allowed range")
    return result


def market_price_tick(market_snapshot: dict[str, Any], market: dict[str, Any] | None = None) -> Decimal:
    market = market or market_snapshot.get("market") or market_snapshot.get("metadata") or {}
    limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
    price_limits = limits.get("price") if isinstance(limits.get("price"), dict) else {}
    precision = market.get("precision") if isinstance(market.get("precision"), dict) else {}
    raw = (
        price_limits.get("step")
        or price_limits.get("tick")
        or market.get("price_tick")
        or market_snapshot.get("price_tick")
        or precision.get("price")
    )
    return _decimal("price_tick", raw, positive=True)


def market_amount_step(market_snapshot: dict[str, Any], market: dict[str, Any] | None = None) -> Decimal:
    market = market or market_snapshot.get("market") or market_snapshot.get("metadata") or {}
    limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
    amount_limits = limits.get("amount") if isinstance(limits.get("amount"), dict) else {}
    precision = market.get("precision") if isinstance(market.get("precision"), dict) else {}
    return _decimal("amount_step", precision.get("amount") or amount_limits.get("step"), positive=True)


def quantize_gate_prices(
    *, side: str, order_type: str, quote: Any, entry_price: Any, stop_price: Any,
    target_price: Any, price_tick: Any,
) -> tuple[Decimal, Decimal, Decimal]:
    """Validate model prices; only a market quote may be rounded adversely.

    Limit entry, stop, and target are model-authored decision values. Silently
    changing one would make the accepted order differ from the proposal, so
    they must already match Gate's tick. A market quote is an observed venue
    value rather than a model instruction and is rounded outward for risk.
    """
    clean_side = str(side).upper()
    clean_type = str(order_type).lower()
    if clean_side not in {"LONG", "SHORT"} or clean_type not in {"limit", "market"}:
        raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID", "unsupported side or order type")
    tick = _decimal("price_tick", price_tick, positive=True)
    quote_dec = _decimal("quote", quote, positive=True)
    raw_entry = _decimal("entry_price", entry_price, positive=True)
    raw_stop = _decimal("stop_price", stop_price, positive=True)
    raw_target = _decimal("target_price", target_price, positive=True)
    sign = 1 if clean_side == "LONG" else -1

    if clean_type == "market":
        raw_entry = quote_dec
        entry_rounding = ROUND_CEILING if sign > 0 else ROUND_FLOOR
    else:
        entry_rounding = ROUND_FLOOR

    def rounded(value: Decimal, rounding: str) -> Decimal:
        return (value / tick).to_integral_value(rounding=rounding) * tick

    if clean_type == "limit":
        for label, value in (("entry", raw_entry), ("stop", raw_stop), ("target", raw_target)):
            if value % tick != 0:
                raise EntryEconomicsError("GATE_PRICE_TICK_MISMATCH", f"{label} is not aligned to the Gate price tick")
        entry, stop, target = raw_entry, raw_stop, raw_target
    else:
        for label, value in (("stop", raw_stop), ("target", raw_target)):
            if value % tick != 0:
                raise EntryEconomicsError("GATE_PRICE_TICK_MISMATCH", f"{label} is not aligned to the Gate price tick")
        entry = rounded(raw_entry, entry_rounding)
        stop, target = raw_stop, raw_target
    if min(entry, stop, target) <= 0:
        raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID", "price precision rounded a price to zero")
    if (sign > 0 and not stop < entry < target) or (sign < 0 and not target < entry < stop):
        raise EntryEconomicsError("NOFX_PROTECTION_GEOMETRY_INVALID")
    return entry, stop, target


def quantity_for_notional(*, notional_usdt: Any, entry_price: Any, order_type: str,
                          side: str, contract_size: Any, amount_step: Any,
                          slippage_rate: Any) -> Decimal:
    """Floor only at the venue amount step; never reduce for margin/risk caps."""
    notional = _decimal("notional_usdt", notional_usdt, positive=True)
    entry = _decimal("entry_price", entry_price, positive=True)
    size = _decimal("contract_size", contract_size, positive=True)
    step = _decimal("amount_step", amount_step, positive=True)
    slippage = _decimal("slippage_rate", slippage_rate, non_negative=True)
    clean_type = str(order_type).lower()
    sign = 1 if str(side).upper() == "LONG" else -1 if str(side).upper() == "SHORT" else 0
    if sign == 0 or clean_type not in {"limit", "market"} or slippage >= 1:
        raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID")
    effective_entry = entry * (Decimal(1) + Decimal(sign) * (slippage if clean_type == "market" else Decimal(0)))
    raw_quantity = notional / (effective_entry * size)
    return (raw_quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


def calculate_gate_entry_economics(
    *, side: str, order_type: str, quote: Any, entry_price: Any, stop_price: Any,
    target_price: Any, quantity: Any, contract_size: Any, price_tick: Any,
    amount_step: Any, taker_fee_rate: Any, slippage_rate: Any,
    equity: Any | None, risk_per_trade_pct: Any, min_net_rr: Any = _UNSET,
) -> GateEntryEconomics:
    """Calculate Gate entry economics without applying acceptance thresholds.

    ``risk_per_trade_pct`` is percentage points (0.15 means 0.15% of equity).
    This is the shared calculation primitive for both enforcement and read-only
    diagnostics. Missing prices, precision, costs, or policy inputs fail closed.
    """
    clean_side = str(side).upper()
    clean_type = str(order_type).lower()
    if clean_side not in {"LONG", "SHORT"} or clean_type not in {"limit", "market"}:
        raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID")
    sign = Decimal(1) if clean_side == "LONG" else Decimal(-1)
    fee = _decimal("taker_fee_rate", taker_fee_rate, non_negative=True)
    slippage = _decimal("slippage_rate", slippage_rate, non_negative=True)
    if fee >= 1 or slippage >= 1:
        raise EntryEconomicsError("GATE_ENTRY_COSTS_UNAVAILABLE")
    tick = _decimal("price_tick", price_tick, positive=True)
    step = _decimal("amount_step", amount_step, positive=True)
    contract = _decimal("contract_size", contract_size, positive=True)
    risk_pct = _decimal("risk_per_trade_pct", risk_per_trade_pct, positive=True)
    if risk_pct > Decimal(100):
        raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID", "risk_per_trade_pct must be percentage points")
    equity_dec = _decimal("equity", equity, positive=True) if equity is not None else None
    if min_net_rr is not _UNSET:
        _decimal("min_net_rr", min_net_rr, positive=True)
    qty = _decimal("quantity", quantity, positive=True)
    if (qty / step) != (qty / step).to_integral_value():
        raise EntryEconomicsError("GATE_AMOUNT_STEP_MISMATCH")

    entry, stop, target = quantize_gate_prices(
        side=clean_side, order_type=clean_type, quote=quote,
        entry_price=entry_price, stop_price=stop_price, target_price=target_price,
        price_tick=tick,
    )
    entry_slippage = slippage if clean_type == "market" else Decimal(0)
    effective_entry = entry * (Decimal(1) + sign * entry_slippage)
    effective_stop = stop * (Decimal(1) - sign * slippage)
    effective_target = target * (Decimal(1) - sign * slippage)
    multiplier = qty * contract
    stop_distance = sign * (effective_entry - effective_stop)
    target_distance = sign * (effective_target - effective_entry)
    if stop_distance <= 0 or target_distance <= 0:
        raise EntryEconomicsError("NOFX_PROTECTION_GEOMETRY_INVALID")

    entry_fee = multiplier * effective_entry * fee
    stop_fee = multiplier * effective_stop * fee
    target_fee = multiplier * effective_target * fee
    stop_risk = multiplier * stop_distance + entry_fee + stop_fee
    target_net = multiplier * target_distance - entry_fee - target_fee
    if stop_risk <= 0:
        raise EntryEconomicsError("GATE_ENTRY_ECONOMICS_INVALID")
    net_rr = target_net / stop_risk
    max_risk = equity_dec * risk_pct / Decimal(100) if equity_dec is not None else None
    result = GateEntryEconomics(
        side=clean_side, order_type=clean_type, entry_price=entry, stop_price=stop,
        target_price=target, quantity=qty, contract_size=contract,
        notional_usdt=multiplier * effective_entry,
        estimated_stop_risk_usdt=stop_risk, max_stop_risk_usdt=max_risk,
        estimated_target_net_usdt=target_net, net_reward_risk=net_rr,
        taker_fee_rate=fee, slippage_rate=slippage, price_tick=tick, amount_step=step,
    )
    return result


def evaluate_gate_entry_economics(
    *, side: str, order_type: str, quote: Any, entry_price: Any, stop_price: Any,
    target_price: Any, quantity: Any, contract_size: Any, price_tick: Any,
    amount_step: Any, taker_fee_rate: Any, slippage_rate: Any,
    equity: Any | None, risk_per_trade_pct: Any, min_net_rr: Any,
) -> GateEntryEconomics:
    """Calculate and enforce the Gate net-RR and stop-risk acceptance rules."""
    result = calculate_gate_entry_economics(
        side=side, order_type=order_type, quote=quote,
        entry_price=entry_price, stop_price=stop_price, target_price=target_price,
        quantity=quantity, contract_size=contract_size, price_tick=price_tick,
        amount_step=amount_step, taker_fee_rate=taker_fee_rate,
        slippage_rate=slippage_rate, equity=equity,
        risk_per_trade_pct=risk_per_trade_pct, min_net_rr=min_net_rr,
    )
    rr_floor = _decimal("min_net_rr", min_net_rr, positive=True)
    if result.net_reward_risk < rr_floor:
        raise EntryEconomicsError("AI_NET_REWARD_RISK_TOO_LOW")
    if (result.max_stop_risk_usdt is not None
            and result.estimated_stop_risk_usdt > result.max_stop_risk_usdt):
        raise EntryEconomicsError("STOP_RISK_LIMIT_EXCEEDED")
    return result
