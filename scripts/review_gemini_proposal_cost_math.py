"""Read-only optimization proposal cost scenarios; never an execution veto."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path


def number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("NUMERIC_VALUE_REQUIRED")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError("NUMERIC_VALUE_REQUIRED") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("FINITE_NONNEGATIVE_VALUE_REQUIRED")
    return result


def scenario(side, entry, stop, target, fee_rate, exit_slippage):
    """One entry and one full exit at adverse slipped prices, per underlying unit.

    The supplied visible fee is assumed for each leg. No Maker guarantee, funding,
    partial exits, later protection changes, or future fill predictions are implied.
    """
    if side not in ("LONG", "SHORT"):
        raise ValueError("SIDE_REQUIRED")
    e, s, t = [number(x, positive=True) for x in (entry, stop, target)]
    fee, slip = [number(x) for x in (fee_rate, exit_slippage)]
    if fee >= 1 or slip >= 1:
        raise ValueError("FRACTION_OUT_OF_RANGE")
    direction = Decimal(1 if side == "LONG" else -1)
    gross_loss, gross_reward = direction * (e - s), direction * (t - e)
    if gross_loss <= 0 or gross_reward <= 0:
        raise ValueError("DIRECTIONAL_PROTECTION_REQUIRED")
    with localcontext() as context:
        context.prec = 40
        slipped_stop = s * (1 - direction * slip)
        slipped_target = t * (1 - direction * slip)
        loss = direction * (e - slipped_stop) + fee * (e + slipped_stop)
        reward = direction * (slipped_target - e) - fee * (e + slipped_target)
        return {"gross_reward_to_loss": str(gross_reward / gross_loss),
                "cost_adjusted_reward_to_loss": str(reward / loss),
                "reward_per_underlying_unit": str(reward),
                "loss_per_underlying_unit": str(loss),
                "entry_fee_per_unit": str(e * fee),
                "stop_exit_fee_per_unit": str(slipped_stop * fee),
                "target_exit_fee_per_unit": str(slipped_target * fee),
                "assumed_stop_execution_price": str(slipped_stop),
                "assumed_target_execution_price": str(slipped_target),
                "fee_rate_per_leg": str(fee), "adverse_exit_slippage_fraction": str(slip)}


def review(directory):
    directory = Path(directory).resolve()
    registration = json.loads((directory / "research-plan.json").read_text(encoding="utf-8"))
    raw = (directory / "trade-casebook.json").read_bytes()
    book = json.loads(raw)
    plan = registration["plan"]
    expected = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if expected != registration["plan_sha256"] or book["plan_sha256"] != expected:
        raise ValueError("CASEBOOK_PLAN_BINDING_REQUIRED")
    windows = plan.get("pilot_windows", [])
    if not windows or any(w.get("partition") != "optimization" for w in windows):
        raise ValueError("OPTIMIZATION_ONLY")
    if book["case_count"] != len(book["cases"]):
        raise ValueError("CASE_COUNT_MISMATCH")
    rows = []
    for case in book["cases"]:
        for order_id, proposal in case["entry_model_proposals"].items():
            decision = proposal["decision"]
            snapshot = proposal["effective_model_visible_entry_facts"]["market_snapshot"]
            row = {"case_id": case["case_id"], "instrument_id": case["instrument_id"],
                   "side": case["side"], "order_id": order_id,
                   "effective_model_request_hash": proposal["effective_model_request_hash"],
                   "model_reason": decision.get("reason"),
                   "actual_net_pnl_usdt": case["net_pnl_usdt"],
                   "actual_gross_pnl_usdt": case["gross_pnl_usdt"],
                   "actual_fees_usdt": case["fees_usdt"],
                   "actual_initial_fee_only_scenario": case.get("initial_plan_scenario"),
                   "gross_claim_is_not_an_explicit_net_claim": True}
            try:
                row["visible_cost_scenario"] = scenario(case["side"],
                    decision.get("entry_price") or decision.get("limit_price"),
                    decision["stop_price"], decision["take_profit"],
                    snapshot["fee_rate"], snapshot["slippage"])
                row["status"] = "AVAILABLE_DESCRIPTIVE_SCENARIO"
            except (KeyError, ValueError) as exc:
                row.update(status="UNAVAILABLE", error=str(exc))
            rows.append(row)
    return {"scope": "ALL_OPTIMIZATION_CLOSED_ENTRY_PROPOSALS_COST_MATH_NOT_CAUSAL_OR_ACCEPTANCE",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "plan_sha256": registration["plan_sha256"],
            "casebook_sha256": hashlib.sha256(raw).hexdigest(),
            "case_count": book["case_count"], "proposal_count": len(rows), "rows": rows,
            "scenario_assumptions": ["Entry at the AI proposed price; adverse slippage on full exit only",
                "Visible fee_rate applied to both legs at scenario execution notionals",
                "Funding and later management excluded; LIMIT does not guarantee Maker",
                "Actual ledger outcomes remain separate from hypothetical protection scenarios"],
            "causal_conclusion": "NONE: cost-space evidence does not explain stop-hit probability",
            "strategy_writes": 0, "private_exchange_calls": 0, "profit_acceptance_passed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    result = review(args.directory)
    output = Path(args.directory) / "proposal-cost-math-review.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"case_count": result["case_count"], "proposal_count": result["proposal_count"],
                      "status_counts": {status: sum(r["status"] == status for r in result["rows"])
                       for status in {r["status"] for r in result["rows"]}}, "output": str(output)}))


if __name__ == "__main__":
    main()
