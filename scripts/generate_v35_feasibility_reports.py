"""Generate local-only V35 diagnostics from the sanitized V25 sample.

This script reads proposal-time fields only for feasibility decisions. It
copies original outcomes into the report after those decisions are complete,
without recalculating or mutating the historical ledger.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.trading.trade_feasibility import (
    FIXED_NOTIONAL,
    RISK_BUDGETED_NOTIONAL,
    diagnose_trade_proposal,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = ROOT / "docs" / "research" / "v25-price-action-sample-20261008.json"
OUTPUT_DIR = ROOT / "reports" / "v35-feasibility"
FEASIBILITY_PATH = OUTPUT_DIR / "feasibility-summary.json"
COUNTERFACTUAL_PATH = OUTPUT_DIR / "v25-counterfactual-comparison.json"

RISK_PER_TRADE_PCT = "0.15"
MIN_NET_RR = "2.0"
FIXED_NOTIONAL_USDT = "2000"
MAX_MARGIN_PCT = "12"
MAX_NOTIONAL_USDT = "2000"
TAKER_FEE_RATE = "0.00075"
EQUITY_SCENARIOS = ("1000", "5000", "10000", "20000")

# The sanitized export records one-decimal contract quantities but omits the
# raw Gate market metadata. These values are a visible research proxy only.
CONTRACT_PROXY = {
    "contract_size": "0.01",
    "amount_step": "0.1",
    "price_tick": "0.01",
    "minimum_amount": "0.1",
    "maximum_amount": None,
    "maximum_leverage": None,
    "source": "INFERRED_FROM_SANITIZED_V25_SIMULATED_ORDER_AMOUNTS; NOT_GATE_METADATA",
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _proposal_diagnostic(entry: dict[str, Any], *, mode: str, equity: str) -> dict[str, Any]:
    intended_entry = entry["intended_entry_price"]
    return diagnose_trade_proposal(
        mode=mode,
        side="LONG" if entry["action"] == "OPEN_LONG" else "SHORT",
        order_type=entry["order_type"],
        proposed_notional_usdt=entry["target_notional_usdt"],
        fixed_notional_usdt=FIXED_NOTIONAL_USDT,
        entry_price=intended_entry,
        # V25 did not export a contemporaneous quote. Use the proposal's own
        # intended entry as a labeled proxy; do not inspect later fills.
        quote=intended_entry,
        quote_source="V25_INTENDED_ENTRY_PROXY; CONTEMPORANEOUS_QUOTE_NOT_EXPORTED",
        stop_price=entry["stop_price"],
        target_price=entry["take_profit"],
        requested_leverage=entry["requested_leverage"],
        equity=equity,
        # A scenario assumes no prior positions or pending orders. It is not a
        # reconstruction of the V25 account balance or current account truth.
        available_margin=equity,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
        min_net_rr=MIN_NET_RR,
        taker_fee_rate=TAKER_FEE_RATE,
        slippage_rate="0.0002",
        contract_size=CONTRACT_PROXY["contract_size"],
        amount_step=CONTRACT_PROXY["amount_step"],
        price_tick=CONTRACT_PROXY["price_tick"],
        min_amount=CONTRACT_PROXY["minimum_amount"],
        max_amount=CONTRACT_PROXY["maximum_amount"],
        max_leverage=CONTRACT_PROXY["maximum_leverage"],
        max_margin_pct=MAX_MARGIN_PCT,
        max_notional=MAX_NOTIONAL_USDT,
    )


def _count_reason(rows: list[dict[str, Any]], reason: str) -> int:
    return sum(reason in row["reason_codes"] for row in rows)


def _trade_numbers_with_reason(rows: list[dict[str, Any]], reason: str) -> list[int]:
    return [row["closed_trade_number"] for row in rows if reason in row["reason_codes"]]


def build_reports(sample: dict[str, Any], sample_bytes: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    closed_trades = sample.get("closed_trades")
    if not isinstance(closed_trades, list) or len(closed_trades) != 10:
        raise ValueError("V25_SANITIZED_SAMPLE_MUST_CONTAIN_10_CLOSED_TRADES")

    comparison_trades: list[dict[str, Any]] = []
    scenario_rows: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for equity in EQUITY_SCENARIOS:
        scenario_rows[equity] = {FIXED_NOTIONAL: [], RISK_BUDGETED_NOTIONAL: []}

    for closed in closed_trades:
        entry = closed.get("entry_decision")
        if not isinstance(entry, dict) or entry.get("action") not in {"OPEN_LONG", "OPEN_SHORT"}:
            raise ValueError(f"V25_TRADE_{closed.get('closed_trade_number')}_HAS_NO_VALID_ENTRY_PROPOSAL")
        # Economic decisions receive only this proposal object. Exit fills,
        # realized PnL, timestamps after the proposal, and future bars are not
        # passed to the diagnostic function.
        per_scenario: dict[str, dict[str, Any]] = {}
        for equity in EQUITY_SCENARIOS:
            modes: dict[str, Any] = {}
            for mode in (FIXED_NOTIONAL, RISK_BUDGETED_NOTIONAL):
                result = _proposal_diagnostic(entry, mode=mode, equity=equity)
                result["closed_trade_number"] = closed["closed_trade_number"]
                modes[mode] = result
                scenario_rows[equity][mode].append(result)
            per_scenario[equity] = modes
        # The original outcome is copied verbatim after classification. This
        # preserves history and makes clear that counterfactual rejection does
        # not erase a simulated loss or convert it into a prevented loss.
        comparison_trades.append({
            "closed_trade_number": closed["closed_trade_number"],
            "proposal": {
                "action": entry["action"],
                "side": "LONG" if entry["action"] == "OPEN_LONG" else "SHORT",
                "order_type": entry["order_type"],
                "intended_entry_price": str(entry["intended_entry_price"]),
                "stop_price": str(entry["stop_price"]),
                "take_profit": str(entry["take_profit"]),
                "proposed_notional_usdt": str(entry["target_notional_usdt"]),
                "requested_leverage": entry["requested_leverage"],
            },
            "counterfactual_by_equity_scenario": per_scenario,
            "original_sampled_outcome_unchanged": {
                "net_outcome": closed.get("net_outcome"),
                "net_pnl_usdt": closed.get("net_pnl_usdt"),
                "outcome_source": "SANITIZED_V25_SAMPLE; NOT_RECALCULATED",
            },
        })

    scenarios: list[dict[str, Any]] = []
    for equity in EQUITY_SCENARIOS:
        fixed = scenario_rows[equity][FIXED_NOTIONAL]
        budgeted = scenario_rows[equity][RISK_BUDGETED_NOTIONAL]
        rr_then_risk_fixed = [
            row["closed_trade_number"] for row in fixed
            if row.get("net_reward_risk_pass") is True
            and row.get("stop_risk_within_budget") is False
        ]
        rr_then_risk_budgeted = [
            row["closed_trade_number"] for row in budgeted
            if row.get("net_reward_risk_pass") is True
            and row.get("stop_risk_within_budget") is False
        ]
        scenarios.append({
            "equity_usdt": equity,
            "assumptions": [
                "HYPOTHETICAL_EQUITY_ONLY; NOT_CURRENT_OR_HISTORICAL_ACCOUNT_TRUTH",
                "AVAILABLE_MARGIN_EQUALS_EQUITY; ASSUMES_NO_OPEN_POSITIONS_OR_PENDING_ORDERS",
                "CURRENT_PA_POLICY_LIMITS_APPLIED_TO_HISTORICAL_PROPOSALS",
            ],
            "risk_budget_usdt": str(Decimal(equity) * Decimal(RISK_PER_TRADE_PCT) / Decimal(100)),
            "FIXED_NOTIONAL": {
                "economic_candidates_under_known_inputs": sum(
                    row["economic_feasible_under_known_inputs"] for row in fixed
                ),
                "fully_executable_candidates_verified": sum(row["feasible"] for row in fixed),
                "rejected_for_stop_risk": _count_reason(fixed, "STOP_RISK_LIMIT_EXCEEDED"),
                "stop_risk_rejection_trade_numbers": _trade_numbers_with_reason(fixed, "STOP_RISK_LIMIT_EXCEEDED"),
                "rejected_for_net_rr": _count_reason(fixed, "AI_NET_REWARD_RISK_TOO_LOW"),
                "net_rr_rejection_trade_numbers": _trade_numbers_with_reason(fixed, "AI_NET_REWARD_RISK_TOO_LOW"),
                "net_rr_pass_but_stop_risk_over_budget_trade_numbers": rr_then_risk_fixed,
                "precision_rejection_count": sum(
                    any(code in row["reason_codes"] for code in (
                        "GATE_PRICE_TICK_MISMATCH", "GATE_AMOUNT_STEP_MISMATCH",
                        "GATE_AMOUNT_BELOW_CONTRACT_MINIMUM",
                        "GATE_AMOUNT_ABOVE_CONTRACT_MAXIMUM",
                    )) for row in fixed
                ),
                "precision_rejection_trade_numbers": [
                    row["closed_trade_number"] for row in fixed
                    if any(code in row["reason_codes"] for code in (
                        "GATE_PRICE_TICK_MISMATCH", "GATE_AMOUNT_STEP_MISMATCH",
                        "GATE_AMOUNT_BELOW_CONTRACT_MINIMUM",
                        "GATE_AMOUNT_ABOVE_CONTRACT_MAXIMUM",
                    ))
                ],
                "blocked_for_unknown_contract_limits": _count_reason(fixed, "GATE_CONTRACT_LIMITS_UNAVAILABLE"),
                "unknown_contract_limit_trade_numbers": _trade_numbers_with_reason(
                    fixed, "GATE_CONTRACT_LIMITS_UNAVAILABLE"
                ),
            },
            "RISK_BUDGETED_NOTIONAL": {
                "unchanged_historical_proposals_economic_candidates": sum(
                    row["economic_feasible_under_known_inputs"] for row in budgeted
                ),
                "unchanged_historical_proposals_fully_verified": sum(row["feasible"] for row in budgeted),
                "rejected_for_stop_risk": _count_reason(budgeted, "STOP_RISK_LIMIT_EXCEEDED"),
                "stop_risk_rejection_trade_numbers": _trade_numbers_with_reason(
                    budgeted, "STOP_RISK_LIMIT_EXCEEDED"
                ),
                "net_rr_pass_but_stop_risk_over_budget_trade_numbers": rr_then_risk_budgeted,
                "proposals_over_budgeted_capacity": _count_reason(
                    budgeted, "PROPOSAL_EXCEEDS_RISK_BUDGETED_CAPACITY"
                ),
                "positive_risk_budgeted_capacity_count": sum(
                    float(row.get("risk_budgeted_max_notional_usdt") or 0) > 0 for row in budgeted
                ),
                "capacity_is_not_a_rewritten_ai_proposal": True,
                "precision_rejection_count": sum(
                    any(code in row["reason_codes"] for code in (
                        "GATE_PRICE_TICK_MISMATCH", "GATE_AMOUNT_STEP_MISMATCH",
                        "GATE_AMOUNT_BELOW_CONTRACT_MINIMUM",
                        "GATE_AMOUNT_ABOVE_CONTRACT_MAXIMUM",
                    )) for row in budgeted
                ),
                "precision_rejection_trade_numbers": [
                    row["closed_trade_number"] for row in budgeted
                    if any(code in row["reason_codes"] for code in (
                        "GATE_PRICE_TICK_MISMATCH", "GATE_AMOUNT_STEP_MISMATCH",
                        "GATE_AMOUNT_BELOW_CONTRACT_MINIMUM",
                        "GATE_AMOUNT_ABOVE_CONTRACT_MAXIMUM",
                    ))
                ],
                "blocked_for_unknown_contract_limits": _count_reason(budgeted, "GATE_CONTRACT_LIMITS_UNAVAILABLE"),
                "unknown_contract_limit_trade_numbers": _trade_numbers_with_reason(
                    budgeted, "GATE_CONTRACT_LIMITS_UNAVAILABLE"
                ),
            },
        })

    comparison = {
        "schema_version": "v35-v25-counterfactual-v1",
        "base_commit": "8642639a78ca9aa56fbad1e88aea4b82169ea433",
        "sample_path": "docs/research/v25-price-action-sample-20261008.json",
        "sample_sha256": hashlib.sha256(sample_bytes).hexdigest(),
        "source_history_sha256": sample.get("source_history_sha256"),
        "sample_scope": "The 10 chronological fully closed V25 pilot-1 positions in the sanitized export.",
        "decision_data_boundary": {
            "used_for_feasibility": [
                "entry_decision.action", "entry_decision.order_type",
                "entry_decision.intended_entry_price", "entry_decision.stop_price",
                "entry_decision.take_profit", "entry_decision.target_notional_usdt",
                "entry_decision.requested_leverage",
            ],
            "not_used_for_feasibility": [
                "entry_fill", "exit_fills", "closed_at_utc", "net_pnl_usdt",
                "future_bars", "exchange_private_account_or_order_data",
            ],
            "quote_proxy": "The intended entry is used because the sanitized sample has no contemporaneous quote.",
            "lookahead_used": False,
        },
        "policy": {
            "risk_per_trade_pct_points": RISK_PER_TRADE_PCT,
            "min_net_rr": MIN_NET_RR,
            "fixed_notional_target_usdt": FIXED_NOTIONAL_USDT,
            "max_notional_usdt": MAX_NOTIONAL_USDT,
            "max_margin_pct": MAX_MARGIN_PCT,
            "leverage_affects_margin_only_not_stop_risk": True,
        },
        "cost_assumptions": {
            "taker_fee_rate_each_leg": TAKER_FEE_RATE,
            "market_entry_slippage_bps": 2,
            "limit_entry_slippage_bps": 0,
            "limit_entry_price_is_binding_cap": True,
            "protective_exit_slippage_bps": 2,
            "all_fees_conservatively_treated_as_taker": True,
        },
        "contract_rule_proxy": CONTRACT_PROXY,
        "historical_outcomes_modified": False,
        "fills_are_simulated_not_gate_exchange_fills": True,
        "scenarios": scenarios,
        "trades": comparison_trades,
        "limitations": [
            "The sanitized V25 export does not include actual account equity or available margin.",
            "The export omits complete Gate amount maxima and leverage limits; no proposal is marked fully verified executable.",
            "The contract size, amount step, and price tick are explicit proxies inferred from sanitized simulated quantities and prices.",
            "A rejection is a counterfactual risk-policy result; it does not erase or prevent the historical simulated outcome.",
            "Risk-budgeted capacity is informational. The historical 2,000 USDT proposal is never silently shrunk and counted as accepted.",
        ],
    }
    summary = {
        "schema_version": "v35-feasibility-summary-v1",
        "base_commit": comparison["base_commit"],
        "audit_scope": "Offline-only; no model, private Gate API, order, or current account configuration access.",
        "current_account_diagnostic": {
            "status": "NOT_QUERIED",
            "equity_usdt": None,
            "single_trade_stop_budget_usdt": None,
            "reason_code": "FRESH_ACCOUNT_EQUITY_NOT_PROVIDED",
            "private_exchange_request_count": 0,
        },
        "production_policy_observed_in_source": {
            "active_template": "price_action_structure",
            "sizing_mode": FIXED_NOTIONAL,
            "target_notional_usdt": FIXED_NOTIONAL_USDT,
            "risk_per_trade_pct_points": RISK_PER_TRADE_PCT,
            "min_net_rr": MIN_NET_RR,
            "production_default_changed": False,
        },
        "economic_calculation": {
            "implementation": "core/trading/entry_economics.py",
            "gate_ai_path": "core/trading/ai_led_engine.py",
            "fresh_equity_reservation": "core/trading/execution_gateway.py -> core/trading/ledger.py",
            "historical_replay_path": "core/replay/ai_template_runner.py -> core/trading/entry_economics.py",
            "fee_and_slippage_basis": "Conservative taker fees on both legs and adverse entry/exit slippage.",
            "proposal_fill_close_are_separate_states": True,
            "risk_budgeted_mode_is_research_only": True,
        },
        "v25_counterfactual": {
            "sample_trade_count": len(comparison_trades),
            "sample_sha256": comparison["sample_sha256"],
            "scenarios": scenarios,
            "comparison_report": "reports/v35-feasibility/v25-counterfactual-comparison.json",
            "historical_outcomes_modified": False,
        },
        "reports_git_ignored": True,
        "private_account_data_read": False,
        "real_orders_submitted": 0,
        "live_enabled": False,
    }
    return summary, comparison


def main() -> int:
    sample_bytes = SAMPLE_PATH.read_bytes()
    sample = json.loads(sample_bytes.decode("utf-8"))
    summary, comparison = build_reports(sample, sample_bytes)
    _write_json(FEASIBILITY_PATH, summary)
    _write_json(COUNTERFACTUAL_PATH, comparison)
    print(f"WROTE {FEASIBILITY_PATH.relative_to(ROOT)}")
    print(f"WROTE {COUNTERFACTUAL_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
