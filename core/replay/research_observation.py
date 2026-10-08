"""Read-only activity and economic diagnostics, never trading policy."""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import sqlite3
import statistics

from .ai_history import digest
from .ai_simulation import ReplayAccount


def _object(raw):
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def model_actions(row):
    """Include original and repair completions, never invent an action on timeout."""
    context = _object(row.get("context_json"))
    audit = context.get("model_response_audit") or (context.get("model_inference_settings") or {}).get("model_response_audit") or {}
    attempts = audit.get("attempts", []) if isinstance(audit, dict) else []
    actions = [_object(context.get("model_raw_response")).get("action")]
    actions.extend(_object(attempt.get("raw_response")).get("action")
                   for attempt in attempts if isinstance(attempt, dict))
    return set(action for action in actions if isinstance(action, str))


def scan_outcome(row):
    """Diagnostic lifecycle only; acceptance still requires the full ledger."""
    if row["status"] in {"MODEL_STARTED", "MODEL_DONE"}:
        return "IN_PROGRESS_NOT_TERMINAL"
    if row["status"] == "ACCOUNT_HALTED":
        return "ACCOUNT_HALTED"
    if row["status"] == "ERROR":
        error = str(row.get("error_code") or "").upper()
        if "TIMED OUT" in error or "MODEL_TIMEOUT" in error:
            return "MODEL_TRANSPORT_TIMEOUT"
        if "DEADLINE" in error:
            return "MODEL_DEADLINE_EXCEEDED"
        if "INPUT_BUDGET" in error or "CONTEXT_UNKNOWN" in error:
            return "MODEL_INPUT_BUDGET_BLOCK"
        if "SCHEMA" in error or "REPAIR_CHANGED" in error:
            actions = model_actions(row)
            if actions & {"OPEN_LONG", "OPEN_SHORT"}:
                return "OPEN_OUTPUT_CONTRACT_ERROR"
            if "WAIT" in actions:
                return "WAIT_OUTPUT_CONTRACT_ERROR"
            return "OTHER_OUTPUT_CONTRACT_ERROR"
        return "OTHER_ERROR"
    decision = _object(row.get("decision_json"))
    result = _object(row.get("result_json"))
    action, status = decision.get("action"), result.get("status")
    if status in {"BLOCKED", "REJECTED"}:
        reason = str(result.get("reason") or "").upper()
        return "EXECUTION_MARGIN_BLOCK" if "MARGIN" in reason else "EXECUTION_REJECTED"
    if action in {"OPEN_LONG", "OPEN_SHORT"}:
        return "ENTRY_SUBMITTED_NOT_FILL_PROOF" if status == "SUBMITTED" else "OTHER_OPEN_EXECUTION_STATUS"
    if action == "WAIT":
        return "MODEL_WAIT"
    if action == "HOLD":
        return "MODEL_HOLD"
    return "MANAGEMENT_ACTION" if action else "UNKNOWN_ACTION"


def wilson_interval(wins, total, z=1.959963984540054):
    """95% descriptive binomial interval; trading dependence limits inference."""
    if type(wins) is not int or type(total) is not int or not 0 <= wins <= total:
        raise ValueError("INVALID_WIN_COUNTS")
    if not total:
        return None
    p = wins / total
    scale = 1 + z*z/total
    center = (p + z*z/(2*total)) / scale
    radius = z*math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / scale
    return [max(0.0, center-radius), min(1.0, center+radius)]


def assess_strategy(summary, rows, *, partition, audited=False, min_closed_trades=30,
                    win_rate_target=0.5):
    """Never label optimization performance or zero trades a verified winner."""
    if type(min_closed_trades) is not int or min_closed_trades < 30:
        raise ValueError("MINIMUM_RESEARCH_SAMPLE_TOO_SMALL")
    if partition not in {"optimization", "validation", "untouched_test"}:
        raise ValueError("UNKNOWN_RESEARCH_PARTITION")
    if type(win_rate_target) not in (int, float) or win_rate_target not in (0.5, 0.6):
        raise ValueError("UNSUPPORTED_RESEARCH_WIN_RATE_TARGET")
    statuses = Counter(r["status"] for r in rows)
    actions = Counter()
    outcomes = Counter(scan_outcome(row) for row in rows)
    proposed_opens = 0
    streak = max_wait_streak = 0
    for row in sorted(rows, key=lambda r: r["as_of"]):
        decision = json.loads(row.get("decision_json") or "{}")
        action = decision.get("action")
        if row["status"] == "COMPLETED":
            actions[action or "UNKNOWN"] += 1
        if action in {"OPEN_LONG", "OPEN_SHORT"} or model_actions(row) & {"OPEN_LONG", "OPEN_SHORT"}:
            proposed_opens += 1
        streak = streak + 1 if row["status"] == "COMPLETED" and action == "WAIT" else 0
        max_wait_streak = max(max_wait_streak, streak)
    wins, closed = summary["wins"], summary["closed_trade_count"]
    interval = wilson_interval(wins, closed)
    issues = []
    if statuses["ERROR"]:
        issues.append("MODEL_OUTPUT_ERRORS")
    # Diagnostic thresholds, not a forced-trade minimum or execution rule.
    if len(rows) >= 24 and not proposed_opens:
        issues.append("NO_OPEN_PROPOSAL_AFTER_24_SCANS_REVIEW_CONDITIONS")
    if proposed_opens and not actions["OPEN_LONG"] and not actions["OPEN_SHORT"]:
        issues.append("OPEN_PROPOSALS_BLOCKED_BEFORE_EXECUTION")
    if max_wait_streak >= 24:
        issues.append("24_CONSECUTIVE_WAITS_REVIEW_TRIGGER_FOLLOW_THROUGH")
    if closed < min_closed_trades:
        issues.append("INSUFFICIENT_CLOSED_TRADES")
    rate = wins/closed if closed else None
    if rate is not None and rate < win_rate_target:
        issues.append(f"OBSERVED_WIN_RATE_BELOW_{round(win_rate_target * 100)}_PERCENT")
    if summary.get("realized_gross_pnl", 0)-summary.get("fees", 0)+summary.get("funding_pnl", 0) <= 0:
        issues.append("NO_POSITIVE_REALIZED_NET_RETURN")
    if not summary.get("economic_eligible", False):
        issues.append("ECONOMIC_PATH_INVALID")
    eligibility = "DIAGNOSTIC_ONLY_OPTIMIZATION" if partition == "optimization" else "NOT_VERIFIED"
    if partition != "optimization" and audited and not issues and interval and interval[0] >= win_rate_target:
        eligibility = "OBSERVED_VALIDATION_TARGET_MET_NOT_FUTURE_GUARANTEE"
    elapsed = [row["wall_elapsed_seconds"] for row in rows
               if type(row.get("wall_elapsed_seconds")) in (int, float)
               and math.isfinite(row["wall_elapsed_seconds"]) and row["wall_elapsed_seconds"] >= 0]
    return {"decision_rows": len(rows), "statuses": dict(statuses), "valid_actions": dict(actions),
            "scan_outcomes": dict(outcomes),
            "scan_duration_seconds": {"scope": "COMPLETE_SCAN_INCLUDING_HEALTH_AND_REPAIR_NOT_ONLY_THINKING",
                "observed_scans": len(elapsed), "median": statistics.median(elapsed) if elapsed else None,
                "maximum": max(elapsed) if elapsed else None},
            "model_open_proposals_including_blocked": proposed_opens,
            "valid_open_rate": (actions["OPEN_LONG"]+actions["OPEN_SHORT"])/len(rows) if rows else None,
            "max_consecutive_waits": max_wait_streak, "closed_trades": closed,
            "win_rate": rate, "win_rate_wilson_95": interval,
            "registered_win_rate_target": win_rate_target,
            "eligibility": eligibility, "issues": issues,
            "accepted_entries": summary.get("accepted_entry_order_count", 0),
            "filled_entries": summary.get("filled_entry_order_count", 0),
            "roi_snapshot": summary.get("roi"), "max_drawdown": summary.get("max_drawdown"),
            "min_closed_trades": min_closed_trades,
            "interval_limitations": "Descriptive; serial dependence, regime changes and model selection are not covered."}


def observe_directory(directory):
    directory = Path(directory).resolve()
    registration = json.loads((directory/"research-plan.json").read_text(encoding="utf-8"))
    plan = registration["plan"]
    if digest(plan) != registration["plan_sha256"]:
        raise ValueError("RESEARCH_PLAN_HASH_MISMATCH")
    target = plan.get("acceptance_win_rate_target", 0.5)
    if type(target) not in (int, float) or target not in (0.5, 0.6):
        raise ValueError("UNSUPPORTED_RESEARCH_WIN_RATE_TARGET")
    windows, processed = [], 0
    for window in plan["pilot_windows"]:
        database = directory/window["id"]/"results.sqlite3"
        if not database.exists():
            continue
        with sqlite3.connect(database.as_uri()+"?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            db.row_factory = sqlite3.Row
            run = db.execute("SELECT * FROM ai_template_replay_runs").fetchone()
            if run is None:
                continue
            checkpoint = json.loads(run["checkpoint_json"])
            rows = [dict(r) for r in db.execute("SELECT * FROM ai_template_replay_decisions")]
        processed += checkpoint["decision_count"]
        strategies = {}
        for template_id, state in checkpoint["accounts"].items():
            account = ReplayAccount.from_dict(state)
            strategies[template_id] = assess_strategy(account.summary(account.last_prices),
                [r for r in rows if r["template_id"] == template_id], partition=window["partition"],
                win_rate_target=target)
        windows.append({"window": window["id"], "status": run["status"],
                        "processed_decisions": checkpoint["decision_count"], "strategies": strategies})
    return {"schema_version": "research_observation_v1", "scope": "READ_ONLY_CHECKPOINT_DIAGNOSTICS",
            "processed_decisions": processed, "expected_decisions": plan["expected_pilot_decisions"],
            "target_win_rate": target, "future_win_rate_guaranteed": False,
            "production_strategy_writes": 0, "windows": windows}
