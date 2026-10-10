"""Create a read-only V36 review or a V25 coverage-only pilot report."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from core.replay.pa_decision_quality_v36.review import summarize


def _load(path: Path) -> tuple[Any, str]:
    content = path.read_bytes()
    return json.loads(content.decode("utf-8")), hashlib.sha256(content).hexdigest()


def _write_new_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def build_v25_coverage_report(sample: dict[str, Any], sample_sha256: str) -> dict[str, Any]:
    scans = sample.get("scans")
    trades = sample.get("closed_trades")
    if not isinstance(scans, list) or not isinstance(trades, list):
        raise TypeError("V25_SAMPLE_SHAPE_INVALID")
    actions = Counter(str(row.get("action") or "UNKNOWN") for row in scans if isinstance(row, dict))
    if len(scans) != sample.get("scan_count") or dict(actions) != sample.get("action_counts"):
        raise ValueError("V25_SCAN_AGGREGATE_MISMATCH")
    simulated_open_events = [
        event
        for scan in scans if isinstance(scan, dict)
        for event in (scan.get("simulation_events") or [])
        if isinstance(event, dict)
        and event.get("action") in {"OPEN_LONG", "OPEN_SHORT"}
        and event.get("status") == "ACCEPTED"
    ]
    wins = sum((row.get("net_outcome") == "WIN") for row in trades if isinstance(row, dict))
    losses = sum((row.get("net_outcome") == "LOSS") for row in trades if isinstance(row, dict))
    try:
        net = sum((Decimal(str(row["net_pnl_usdt"])) for row in trades), Decimal(0))
        source_net = Decimal(str(sample["closed_net_pnl_usdt"]))
    except (InvalidOperation, KeyError, TypeError) as exc:
        raise ValueError("V25_CLOSED_PNL_INVALID") from exc
    if net != source_net or len(trades) != sample.get("closed_trade_count"):
        raise ValueError("V25_CLOSED_TRADE_AGGREGATE_MISMATCH")
    count = len(simulated_open_events)
    return {
        "schema_version": "pa-decision-quality-v36/v25-coverage-1",
        "status": "PILOT_NOT_RUN_INPUT_COVERAGE_ONLY",
        "source": {
            "sample_sha256": sample_sha256,
            "source_history_sha256": sample.get("source_history_sha256"),
            "source_experiment": sample.get("source_experiment"),
            "source_window": sample.get("source_window"),
            "source_commit": sample.get("source_commit"),
        },
        "preservation": {
            "source_sample_modified": False,
            "raw_prompts_or_responses_read": False,
            "original_ledger_modified": False,
            "historical_results_reinterpreted_as_real_fills": False,
        },
        "historical_descriptive_counts": {
            "completed_scans": len(scans),
            "scan_action_counts": dict(sorted(actions.items())),
            "accepted_simulated_open_events": count,
            "complete_closed_trade_records": len(trades),
            "closed_wins": wins,
            "closed_losses": losses,
            "closed_net_pnl_usdt": str(net),
            "simulation_is_not_private_venue_fill_proof": True,
        },
        "prior_v35_reference": {
            "source": "docs/audits/V35-acceptance-report.md",
            "proposal_count": 10,
            "net_reward_risk_below_2_0_under_v35_proxy_assumptions": 9,
            "complete_current_gate_contract_feasibility_verified": False,
            "this_is_not_a_v36_price_action_quality_result": True,
            "source_sample_and_results_recomputed_or_modified": False,
        },
        "input_coverage": {
            "decision_time_ohlcv_by_scan": "MISSING",
            "original_v35_prompt_and_response_cache": "MISSING",
            "point_in_time_account_equity_and_margin": "MISSING",
            "point_in_time_complete_contract_rules": "MISSING",
            "per_decision_market_quote_and_cost_snapshot": "MISSING",
        },
        "experiment_status": {
            "A0": {"status": "NOT_RUN", "reason": "NO_EXACT_ORIGINAL_PROMPT_INPUT_STATE_MODEL_CACHE"},
            "A1": {"status": "NOT_RUN", "reason": "NO_CAUSAL_15M_5M_1H_4H_OHLCV_OR_STATE"},
            "A2": {"status": "NOT_RUN", "reason": "NO_CAUSAL_BARS_TARGET_EVIDENCE_OR_STATE"},
            "A3": {"status": "NOT_RUN", "reason": "NO_CAUSAL_OHLCV_FOR_FROZEN_RULE"},
        },
        "counterfactual_feasibility": {
            "observed_proposal_denominator": count,
            "FIXED_NOTIONAL": {
                "target_notional_usdt": "2000",
                "eligible_count": None,
                "rejected_count": None,
                "status": "NOT_EVALUABLE",
                "reason": "MISSING_EQUITY_MARGIN_CONTRACT_LIMIT_AND_PER_DECISION_COST_SNAPSHOTS",
            },
            "RISK_BUDGETED_NOTIONAL": {
                "maximum_notional_per_proposal": None,
                "eligible_count": None,
                "rejected_count": None,
                "status": "NOT_EVALUABLE",
                "reason": "MISSING_EQUITY_STOP_DISTANCE_MARGIN_CONTRACT_LIMIT_AND_COST_SNAPSHOTS",
            },
            "proposal_sizes_were_resized": False,
            "risk_rejections_do_not_erase_historical_losses": True,
            "counterfactual_eligibility_is_not_a_fill": True,
        },
        "limitations": [
            "The 100 scan sample and the 10 complete closes are separate populations; closed positions may open outside the scan interval.",
            "Accepted simulated open events are not real Gate acceptance, fills, or completed venue orders.",
            "No mode candidate is marked feasible or rejected because point-in-time equity, margin, contract, and cost inputs are absent.",
            "No V36 decision-quality or performance effect is estimated from this coverage-only pilot.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--decisions", type=Path,
                      help="JSON array or object containing decision_records")
    mode.add_argument("--v25-sample", type=Path,
                      help="Sanitized V25 sample; emits a coverage-only, no-counterfactual report")
    parser.add_argument("--outcomes", type=Path, help="Optional explicit experiment-keyed outcomes JSON")
    parser.add_argument("--reviews", type=Path, help="Optional explicit experiment-keyed reviews JSON")
    parser.add_argument("--output", type=Path, required=True,
                        help="New report path; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        if args.v25_sample:
            sample, sample_hash = _load(args.v25_sample)
            if not isinstance(sample, dict):
                raise ValueError("V25_SAMPLE_NOT_OBJECT")
            report = build_v25_coverage_report(sample, sample_hash)
        else:
            decisions, decisions_hash = _load(args.decisions)
            records = decisions.get("decision_records") if isinstance(decisions, dict) else decisions
            if not isinstance(records, list):
                raise ValueError("DECISIONS_REQUIRE_LIST")
            outcomes, outcomes_hash = _load(args.outcomes) if args.outcomes else (None, None)
            reviews, reviews_hash = _load(args.reviews) if args.reviews else (None, None)
            report = summarize(records, outcomes=outcomes, reviews=reviews)
            report["source_decisions_sha256"] = decisions_hash
            report["source_outcomes_sha256"] = outcomes_hash
            report["source_reviews_sha256"] = reviews_hash
        _write_new_json(args.output, report)
    except FileExistsError:
        print(json.dumps({"status": "OUTPUT_ALREADY_EXISTS"}, ensure_ascii=False))
        return 2
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "INVALID_OR_UNAVAILABLE_INPUT",
                          "error_type": type(exc).__name__}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "WRITTEN", "output": str(args.output.resolve()),
                      "report_status": report.get("status", "REVIEW_COMPLETE")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
