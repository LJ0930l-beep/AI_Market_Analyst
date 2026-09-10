"""Strategy Effectiveness Evaluation & Counterfactual Comparison (N06, N07, AT24, AT25).

Implements rigorous quantitative metrics:
1. AT24: Full trade intent accounting (partial TP1/TP2 counts as ONE trade, not multiple wins; fees included).
2. EVIDENCE_INSUFFICIENT gate (< 100 samples or < 30 days).
3. Risk-adjusted metrics: Net Expectancy, Normalized R, Mark-to-Market Max Drawdown, Tail Loss (95% CVaR), MAE, MFE, Cost Ratio.
4. AT25: 3-branch counterfactual comparison (STRATEGY_BASELINE vs AI_FILTERED vs AI_LED) with opportunity cost and 2x fee/slippage stress testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
import math
from typing import Any, Dict, List, Optional, Tuple, Union


def _finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _finite_value(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


@dataclass
class TradeOutcome:
    trade_id: str
    strategy_id: str
    symbol: str
    side: str  # LONG, SHORT
    entry_price: float
    initial_stop: float
    target_price: float
    exit_fills: List[Dict[str, Any]]  # [{price, quantity, fee, type}]
    opened_at: str
    closed_at: str
    total_quantity: float
    fees_paid: float
    funding_fees_paid: float = 0.0
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    status: str = "CLOSED"  # CLOSED or OPEN


class StrategyEvaluation(dict):
    """Evaluation metrics dictionary supporting both dict indexing and attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'StrategyEvaluation' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def to_dict(self) -> Dict[str, Any]:
        return dict(self)


def evaluate_strategy_effectiveness(
    trades: Optional[Any] = None,
    initial_capital: float = 1000.0,
    min_samples: int = 100,
    strategy_id: str = "all",
    group_dimensions: Optional[Dict[str, Any]] = None,
    market_context: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> StrategyEvaluation:
    """Evaluates strategy effectiveness under strict AT24 / N06 requirements.
    
    1. Closed full-trade intent win accounting:
       A position with partial TPs is ONE trade. If net realized PnL > 0, it's 1 win; else 1 loss.
    2. Fees and funding are included in net PnL.
    3. If sample_count < min_samples (100), status is EVIDENCE_INSUFFICIENT.
    """
    raw_trades = trades if trades is not None else kwargs.get("trades", [])
    if isinstance(raw_trades, (list, tuple)):
        trade_items = list(raw_trades)
    else:
        trade_items = []

    # 1. Parse into standardized aggregated closed trades
    aggregated_trades: List[Dict[str, Any]] = []

    if trade_items and isinstance(trade_items[0], dict) and any("position_id" in x for x in trade_items):
        # List of individual order fills grouped by position_id (AT24 scenario)
        positions: Dict[str, List[Dict[str, Any]]] = {}
        for item in trade_items:
            pid = item.get("position_id", item.get("order_id", "default"))
            positions.setdefault(pid, []).append(item)

        for pid, orders in positions.items():
            entries = [o for o in orders if not o.get("is_exit", False)]
            exits = [o for o in orders if o.get("is_exit", False)]
            if not exits:
                continue  # Open position, not counted in closed trades

            entry_qty = sum(float(o.get("filled_quantity", o.get("quantity", 0.0))) for o in entries)
            entry_val = sum(
                float(o.get("executed_price", o.get("price", 0.0)))
                * float(o.get("filled_quantity", o.get("quantity", 0.0)))
                for o in entries
            )
            entry_price = (entry_val / entry_qty) if entry_qty > 0 else (
                float(entries[0].get("executed_price", entries[0].get("price", 0.0))) if entries else 0.0
            )
            side = entries[0].get("side", "BUY").upper() if entries else "BUY"
            sign = 1.0 if side in ("BUY", "LONG") else -1.0

            exit_qty = sum(float(o.get("filled_quantity", o.get("quantity", 0.0))) for o in exits)
            # A position remains open until the complete entry quantity has
            # been exited.  Partial TP fills are lifecycle events, not closed
            # trades and must not inflate the denominator or win rate.
            if entry_qty <= 0 or exit_qty + 1e-12 < entry_qty:
                continue
            exit_val = sum(
                float(o.get("executed_price", o.get("price", 0.0)))
                * float(o.get("filled_quantity", o.get("quantity", 0.0)))
                for o in exits
            )

            if sign == 1.0:
                gross_pnl = exit_val - (entry_price * exit_qty)
            else:
                gross_pnl = (entry_price * exit_qty) - exit_val

            observed_fees = [_finite_value(order.get("fee")) for order in orders]
            fees_complete = all(value is not None for value in observed_fees)
            total_order_fees = sum(value for value in observed_fees if value is not None) if fees_complete else None
            net_pnl = gross_pnl - total_order_fees if total_order_fees is not None else None

            stop_price = float(
                entries[0].get("stop_loss", entries[0].get("stop", entry_price * 0.98 if sign == 1 else entry_price * 1.02))
            ) if entries else 0.0
            risk = abs(entry_price - stop_price) * entry_qty

            observed_mae = next((_finite_value(order.get("mae")) for order in orders if _finite_value(order.get("mae")) is not None), None)
            observed_mfe = next((_finite_value(order.get("mfe")) for order in orders if _finite_value(order.get("mfe")) is not None), None)
            mae = abs(observed_mae) if observed_mae is not None else None
            mfe = abs(observed_mfe) if observed_mfe is not None else None

            aggregated_trades.append({
                "trade_id": pid,
                "gross_pnl": gross_pnl,
                "fee": total_order_fees,
                "net_pnl": net_pnl,
                "entry_val": entry_val,
                "risk": risk,
                "mae": mae,
                "mfe": mfe,
            })

    elif trade_items and isinstance(trade_items[0], TradeOutcome):
        for t in trade_items:
            if t.status != "CLOSED":
                continue
            exit_val = sum(f["price"] * f["quantity"] for f in t.exit_fills)
            exit_qty = sum(f["quantity"] for f in t.exit_fills)
            if t.total_quantity <= 0 or exit_qty + 1e-12 < t.total_quantity:
                continue
            entry_val = t.entry_price * exit_qty
            sign = 1.0 if t.side.upper() in ("LONG", "BUY") else -1.0
            raw_pnl = (exit_val - entry_val) if sign == 1.0 else (entry_val - exit_val)
            fees = _finite_value(t.fees_paid)
            funding_fees = _finite_value(t.funding_fees_paid)
            total_fees_for_trade = fees + funding_fees if fees is not None and funding_fees is not None else None
            net_pnl = raw_pnl - total_fees_for_trade if total_fees_for_trade is not None else None
            risk = abs(t.entry_price - t.initial_stop) * t.total_quantity
            if t.entry_price > 0 and t.highest_price is not None and t.lowest_price is not None:
                if sign > 0:
                    mae = (t.entry_price - t.lowest_price) / t.entry_price * 100.0
                    mfe = (t.highest_price - t.entry_price) / t.entry_price * 100.0
                else:
                    # For shorts, adverse excursion is the high and favorable
                    # excursion is the low.  The old implementation silently
                    # inverted these two metrics.
                    mae = (t.highest_price - t.entry_price) / t.entry_price * 100.0
                    mfe = (t.entry_price - t.lowest_price) / t.entry_price * 100.0
            else:
                mae = mfe = None
            aggregated_trades.append({
                "trade_id": t.trade_id,
                "gross_pnl": raw_pnl,
                "fee": total_fees_for_trade,
                "net_pnl": net_pnl,
                "entry_val": entry_val,
                "risk": risk,
                "mae": max(0.0, mae),
                "mfe": max(0.0, mfe),
            })

    else:
        for t in trade_items:
            net_pnl = _finite_value(t.get("pnl", t.get("net_pnl")))
            fee = _finite_value(t.get("fee", t.get("fees")))
            gross_pnl = net_pnl + fee if net_pnl is not None and fee is not None else None
            risk = _finite_value(t.get("risk"))
            mae_value = _finite_value(t.get("mae"))
            mfe_value = _finite_value(t.get("mfe"))
            mae = abs(mae_value) if mae_value is not None else None
            mfe = abs(mfe_value) if mfe_value is not None else None
            aggregated_trades.append({
                "trade_id": t.get("trade_id", str(id(t))),
                "gross_pnl": gross_pnl,
                "fee": fee,
                "net_pnl": net_pnl,
                "entry_val": _finite_value(t.get("entry_val")),
                "risk": risk,
                "mae": mae,
                "mfe": mfe,
            })

    sample_count = len(aggregated_trades)
    if sample_count == 0:
        return StrategyEvaluation({
            "strategy_id": strategy_id,
            "sample_count": 0,
            "total_closed_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "win_rate_pct": 0.0,
            "total_fees": 0.0,
            "evidence_status": "EVIDENCE_INSUFFICIENT",
            "status": "EVIDENCE_INSUFFICIENT",
            "total_net_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "net_expectancy": 0.0,
            "normalized_r_expectancy": 0.0,
            "max_drawdown_pct": 0.0,
            "tail_loss_cvar95": 0.0,
            "cvar_95": 0.0,
            "turnover": 0.0,
            "cost_ratio_pct": 0.0,
            "avg_mae_pct": None,
            "mae_average": None,
            "avg_mfe_pct": None,
            "mfe_average": None,
            "group_dimensions": group_dimensions or market_context or {},
        })

    valid_trades = [
        trade for trade in aggregated_trades
        if _finite_value(trade.get("net_pnl")) is not None
    ]
    complete_net_pnl = len(valid_trades) == sample_count
    complete_fees = all(_finite_value(trade.get("fee")) is not None for trade in aggregated_trades)
    winning_trades = sum(1 for t in valid_trades if t["net_pnl"] > 0)
    losing_trades = sum(1 for t in valid_trades if t["net_pnl"] <= 0)
    total_fees = sum(float(t["fee"]) for t in aggregated_trades) if complete_fees else None
    total_net_pnl = sum(float(t["net_pnl"]) for t in valid_trades) if complete_net_pnl else None
    gross_profit = sum(t["net_pnl"] for t in valid_trades if t["net_pnl"] > 0)
    gross_loss = sum(abs(t["net_pnl"]) for t in valid_trades if t["net_pnl"] <= 0)

    win_rate = (winning_trades / len(valid_trades)) if complete_net_pnl and valid_trades else None
    win_rate_pct = win_rate * 100.0 if win_rate is not None else None
    net_expectancy = total_net_pnl / sample_count if total_net_pnl is not None and sample_count > 0 else None

    r_multiples = [
        (t["net_pnl"] / t["risk"])
        for t in valid_trades
        if _finite_value(t.get("risk")) is not None and t["risk"] > 0
    ]
    norm_r_expectancy = sum(r_multiples) / len(r_multiples) if complete_net_pnl and r_multiples else None

    maes = [value for t in aggregated_trades for value in (_finite_value(t.get("mae")),) if value is not None]
    mfes = [value for t in aggregated_trades for value in (_finite_value(t.get("mfe")),) if value is not None]
    avg_mae = sum(maes) / len(maes) if maes else None
    avg_mfe = sum(mfes) / len(mfes) if mfes else None

    # Drawdown must use mark-to-market equity when the caller has it.  A
    # realized-only curve is retained as a compatibility fallback but is
    # explicitly labelled so consumers cannot mistake it for MTM evidence.
    supplied_curve = (market_context or {}).get("equity_curve") if market_context else None
    if supplied_curve:
        equity_curve = [float(value) for value in supplied_curve if _finite_number(value)]
    elif complete_net_pnl:
        equity_curve = [initial_capital]
        curr = initial_capital
        for t in valid_trades:
            curr += t["net_pnl"]
            equity_curve.append(curr)
    else:
        equity_curve = []

    if equity_curve:
        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        max_drawdown_pct = max_dd * 100.0
    else:
        max_drawdown_pct = None

    # Tail loss: 95% expected shortfall of losses only.  A profitable sample
    # has zero tail loss; treating its best trades as a positive loss is a
    # particularly misleading risk report.
    losses = sorted(t["net_pnl"] for t in valid_trades if t["net_pnl"] < 0)
    if losses:
        tail_cutoff = max(1, math.ceil(len(losses) * 0.05))
        tail_trades = losses[:tail_cutoff]
        tail_loss_cvar = abs(sum(tail_trades) / len(tail_trades))
    else:
        tail_loss_cvar = 0.0

    known_entry_values = [t["entry_val"] for t in valid_trades if _finite_value(t.get("entry_val")) is not None]
    turnover = sum(known_entry_values) / initial_capital if initial_capital > 0 and len(known_entry_values) == len(valid_trades) else None
    cost_ratio = (total_fees / gross_profit * 100.0) if total_fees is not None and gross_profit > 0 else (100.0 if total_fees is not None and total_fees > 0 else None)

    evidence_status = "EVIDENCE_INSUFFICIENT" if sample_count < min_samples or not complete_net_pnl or not complete_fees else "EVALUATED"

    return StrategyEvaluation({
        "strategy_id": strategy_id,
        "sample_count": sample_count,
        "total_closed_trades": sample_count,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "win_rate_pct": round(win_rate_pct, 2) if win_rate_pct is not None else None,
        "total_fees": round(total_fees, 4) if total_fees is not None else None,
        "evidence_status": evidence_status,
        "status": evidence_status,
        "total_net_pnl": round(total_net_pnl, 2) if total_net_pnl is not None else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_expectancy": round(net_expectancy, 4) if net_expectancy is not None else None,
        "normalized_r_expectancy": round(norm_r_expectancy, 4) if norm_r_expectancy is not None else None,
        "max_drawdown_pct": round(max_drawdown_pct, 2) if max_drawdown_pct is not None else None,
        "tail_loss_cvar95": round(tail_loss_cvar, 2),
        "cvar_95": round(tail_loss_cvar, 2),
        "turnover": round(turnover, 2) if turnover is not None else None,
        "cost_ratio_pct": round(cost_ratio, 2) if cost_ratio is not None else None,
        "avg_mae_pct": round(avg_mae, 2) if avg_mae is not None else None,
        "mae_average": round(avg_mae, 2) if avg_mae is not None else None,
        "avg_mfe_pct": round(avg_mfe, 2) if avg_mfe is not None else None,
        "mfe_average": round(avg_mfe, 2) if avg_mfe is not None else None,
        "valid_trade_count": len(valid_trades),
        "incomplete_trade_count": sample_count - len(valid_trades),
        "cost_data_complete": complete_fees,
        "group_dimensions": group_dimensions or market_context or {},
        "drawdown_basis": "MARK_TO_MARKET" if supplied_curve else "REALIZED_ONLY_FALLBACK",
    })


def run_counterfactual_comparison(
    proposals: Optional[List[Dict[str, Any]]] = None,
    actual_market_outcomes: Optional[Dict[str, float]] = None,
    ai_verdicts: Optional[Dict[str, str]] = None,
    ai_led_actions: Optional[List[Dict[str, Any]]] = None,
    fee_rate: float = 0.0005,
    slippage_bps: float = 2.0,
    compute_cost_per_decision: float = 0.02,
    stress_scenarios: bool = True,
    *,
    baseline_trades: Optional[List[Dict[str, Any]]] = None,
    ai_reviews: Optional[Union[List[Dict[str, Any]], Dict[str, str]]] = None,
    ai_led_trades: Optional[List[Dict[str, Any]]] = None,
    initial_equity: Optional[Any] = None,
    **kwargs,
) -> Dict[str, Any]:
    """Runs 3-way counterfactual comparison (AT25, N07).

    Supports both (baseline_trades, ai_reviews, ai_led_trades) and
    (proposals, actual_market_outcomes, ai_verdicts, ai_led_actions).
    """
    # 1. AT25 / API v2 calling convention
    if baseline_trades is not None:
        review_map: Dict[str, str] = {}
        if isinstance(ai_reviews, list):
            for r in ai_reviews:
                tid = r.get("trade_id", r.get("id"))
                if tid:
                    review_map[tid] = r.get("verdict", "REVIEW_NOT_AVAILABLE")
        elif isinstance(ai_reviews, dict):
            review_map = ai_reviews

        base_pnl = sum(float(t.get("pnl", t.get("net_pnl", 0.0))) for t in baseline_trades)

        ai_filtered = []
        rejected = []
        for t in baseline_trades:
            tid = t.get("trade_id", t.get("id"))
            # A missing review is an unresolved decision, never an implicit
            # approval.  Otherwise the comparison silently converts a
            # research gap into an AI benefit.
            v = review_map.get(tid, "REVIEW_NOT_AVAILABLE")
            if v == "REVIEW_PASS":
                ai_filtered.append(t)
            else:
                rejected.append(t)

        ai_filt_pnl = sum(float(t.get("pnl", t.get("net_pnl", 0.0))) for t in ai_filtered)
        opp_cost = sum(
            float(t.get("pnl", t.get("net_pnl", 0.0)))
            for t in rejected
            if float(t.get("pnl", t.get("net_pnl", 0.0))) > 0
        )
        tail_loss = sum(
            abs(float(t.get("pnl", t.get("net_pnl", 0.0))))
            for t in rejected
            if float(t.get("pnl", t.get("net_pnl", 0.0))) < 0
        )

        ai_led_pnl = sum(float(t.get("pnl", t.get("net_pnl", 0.0))) for t in (ai_led_trades or []))

        def _observed_cost(trade: Dict[str, Any], kind: str) -> float:
            if kind == "fee":
                if "fee" in trade or "fees" in trade:
                    return float(trade.get("fee", trade.get("fees", 0.0)) or 0.0)
                return float(trade.get("notional", 0.0) or 0.0) * max(0.0, float(fee_rate)) * 2.0
            if "slippage_cost" in trade or "slippage" in trade:
                return float(trade.get("slippage_cost", trade.get("slippage", 0.0)) or 0.0)
            if "slippage_bps" in trade:
                return float(trade.get("notional", 0.0) or 0.0) * max(0.0, float(trade["slippage_bps"])) / 10000.0
            return 0.0

        def _recompute(trade: Dict[str, Any], fee_multiplier: float = 1.0, slippage_multiplier: float = 1.0) -> float:
            observed_pnl = float(trade.get("pnl", trade.get("net_pnl", 0.0)) or 0.0)
            fee = _observed_cost(trade, "fee")
            slippage = _observed_cost(trade, "slippage")
            # PnL already includes the observed costs.  Stress applies only
            # the incremental cost; scaling the whole PnL was economically
            # invalid and could make a loss look better.
            return observed_pnl - fee * (fee_multiplier - 1.0) - slippage * (slippage_multiplier - 1.0)

        def _branch_stress(trades: List[Dict[str, Any]], fee_multiplier: float = 1.0, slippage_multiplier: float = 1.0) -> float:
            return sum(_recompute(trade, fee_multiplier, slippage_multiplier) for trade in trades)

        stress = {
            "baseline": {
                "strategy_baseline_pnl": round(_branch_stress(baseline_trades), 2),
                "ai_filtered_pnl": round(_branch_stress(ai_filtered), 2),
                "ai_delta": round(_branch_stress(ai_filtered) - _branch_stress(baseline_trades), 2),
            },
            "double_fees": {
                "strategy_baseline_pnl": round(_branch_stress(baseline_trades, fee_multiplier=2.0), 2),
                "ai_filtered_pnl": round(_branch_stress(ai_filtered, fee_multiplier=2.0), 2),
                "ai_delta": round(_branch_stress(ai_filtered, fee_multiplier=2.0) - _branch_stress(baseline_trades, fee_multiplier=2.0), 2),
            },
            "double_slippage": {
                "strategy_baseline_pnl": round(_branch_stress(baseline_trades, slippage_multiplier=2.0), 2),
                "ai_filtered_pnl": round(_branch_stress(ai_filtered, slippage_multiplier=2.0), 2),
                "ai_delta": round(_branch_stress(ai_filtered, slippage_multiplier=2.0) - _branch_stress(baseline_trades, slippage_multiplier=2.0), 2),
            },
        }
        stress["2x_fee"] = stress["double_fees"]
        stress["2x_slippage"] = stress["double_slippage"]

        return {
            "branches": {
                "STRATEGY_BASELINE": {"net_pnl": round(base_pnl, 2), "trade_count": len(baseline_trades)},
                "AI_FILTERED": {"net_pnl": round(ai_filt_pnl, 2), "trade_count": len(ai_filtered)},
                "AI_LED": {"net_pnl": round(ai_led_pnl, 2), "trade_count": len(ai_led_trades or [])},
            },
            "opportunity_cost_rejected_winners": round(opp_cost, 2),
            "tail_loss_prevented": round(tail_loss, 2),
            "stress_tests": stress,
            "dataset_summary": {
                "total_proposals": len(baseline_trades),
                "ai_accepted_count": len(ai_filtered),
                "ai_rejected_count": len(rejected),
                "ai_led_actions_count": len(ai_led_trades or []),
            },
            "comparison": {
                "strategy_baseline_net_pnl": round(base_pnl, 2),
                "ai_filtered_net_pnl": round(ai_filt_pnl, 2),
                "ai_led_net_pnl": round(ai_led_pnl, 2),
                "opportunity_cost_usdt": round(opp_cost, 2),
                "tail_loss_reduction_usdt": round(tail_loss, 2),
                "net_ai_benefit_usdt": round(tail_loss - opp_cost, 2),
            },
            "review_status": "REVIEW_COMPLETE" if len(review_map) >= len(baseline_trades) else "REVIEW_INCOMPLETE",
        }

    # 2. Proposals / outcomes calling convention
    props = proposals or []
    outcomes = actual_market_outcomes or {}
    verdicts = ai_verdicts or {}

    baseline_trades_list = []
    for p in props:
        pid = p["proposal_id"]
        raw_pnl = outcomes.get(pid, 0.0)
        notional = float(p.get("quantity", 1.0)) * float(p.get("entry", 100.0))
        fee = notional * fee_rate * 2
        slippage = notional * (slippage_bps / 10000.0)
        net_pnl = raw_pnl - fee - slippage
        baseline_trades_list.append({"proposal_id": pid, "net_pnl": net_pnl, "raw_pnl": raw_pnl})

    ai_filtered_trades = []
    rejected_proposals = []
    for p in props:
        pid = p["proposal_id"]
        verdict = verdicts.get(pid, "REVIEW_NOT_AVAILABLE")
        raw_pnl = outcomes.get(pid, 0.0)
        notional = float(p.get("quantity", 1.0)) * float(p.get("entry", 100.0))
        fee = notional * fee_rate * 2
        latency_slip = notional * ((slippage_bps + 1.0) / 10000.0)
        net_pnl = raw_pnl - fee - latency_slip

        if verdict == "REVIEW_PASS":
            ai_filtered_trades.append({"proposal_id": pid, "net_pnl": net_pnl, "raw_pnl": raw_pnl})
        else:
            rejected_proposals.append({"proposal_id": pid, "net_pnl": net_pnl, "raw_pnl": raw_pnl, "reason": verdict})

    opp_cost = sum(r["net_pnl"] for r in rejected_proposals if r["net_pnl"] > 0)
    tail_loss_reduction = sum(abs(r["net_pnl"]) for r in rejected_proposals if r["net_pnl"] < 0)

    total_compute_cost = len(props) * compute_cost_per_decision
    baseline_pnl = sum(t["net_pnl"] for t in baseline_trades_list)
    ai_filtered_pnl = sum(t["net_pnl"] for t in ai_filtered_trades) - total_compute_cost
    net_benefit = tail_loss_reduction - opp_cost - total_compute_cost

    ai_led_pnl = 0.0
    ai_led_count = 0
    if ai_led_actions:
        ai_led_count = len(ai_led_actions)
        for act in ai_led_actions:
            aid = act.get("action_id", "")
            raw = outcomes.get(aid, act.get("simulated_pnl", 0.0))
            ai_led_pnl += raw - (compute_cost_per_decision * 1.5)

    scenarios = {}
    if stress_scenarios:
        for scen_name, (fee_mult, slip_mult) in [
            ("baseline", (1.0, 1.0)),
            ("double_fees", (2.0, 1.0)),
            ("double_slippage", (1.0, 2.0)),
            ("2x_fee", (2.0, 1.0)),
            ("2x_slippage", (1.0, 2.0)),
        ]:
            s_base_pnl = 0.0
            s_filtered_pnl = 0.0
            for p in props:
                pid = p["proposal_id"]
                raw_pnl = outcomes.get(pid, 0.0)
                notional = float(p.get("quantity", 1.0)) * float(p.get("entry", 100.0))
                c_fee = notional * (fee_rate * fee_mult) * 2
                c_slip = notional * ((slippage_bps * slip_mult) / 10000.0)
                net = raw_pnl - c_fee - c_slip
                s_base_pnl += net

                if verdicts.get(pid) == "REVIEW_PASS":
                    s_filtered_pnl += net
            s_filtered_pnl -= total_compute_cost
            scenarios[scen_name] = {
                "strategy_baseline_pnl": round(s_base_pnl, 2),
                "ai_filtered_pnl": round(s_filtered_pnl, 2),
                "ai_delta": round(s_filtered_pnl - s_base_pnl, 2),
            }

    return {
        "branches": {
            "STRATEGY_BASELINE": {"net_pnl": round(baseline_pnl, 2), "trade_count": len(baseline_trades_list)},
            "AI_FILTERED": {"net_pnl": round(ai_filtered_pnl, 2), "trade_count": len(ai_filtered_trades)},
            "AI_LED": {"net_pnl": round(ai_led_pnl, 2), "trade_count": ai_led_count},
        },
        "opportunity_cost_rejected_winners": round(opp_cost, 2),
        "tail_loss_prevented": round(tail_loss_reduction, 2),
        "stress_tests": scenarios,
        "dataset_summary": {
            "total_proposals": len(props),
            "ai_accepted_count": len(ai_filtered_trades),
            "ai_rejected_count": len(rejected_proposals),
            "ai_led_actions_count": ai_led_count,
        },
        "comparison": {
            "strategy_baseline_net_pnl": round(baseline_pnl, 2),
            "ai_filtered_net_pnl": round(ai_filtered_pnl, 2),
            "ai_led_net_pnl": round(ai_led_pnl, 2),
            "opportunity_cost_usdt": round(opp_cost, 2),
            "tail_loss_reduction_usdt": round(tail_loss_reduction, 2),
            "total_compute_cost_usdt": round(total_compute_cost, 2),
            "net_ai_benefit_usdt": round(net_benefit, 2),
        },
    }
