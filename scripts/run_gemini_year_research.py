"""Preregister a year split and run bounded real Gemini replay in isolation.

Defaults only prepare history. --run executes at most --max-decisions this
invocation; interrupted calls are never silently repeated. No live account
write, real exchange order, strategy activation, or cloud weight training.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.replay.gemini_research import research_plan, save_plan, freeze_window
from core.replay.ai_history import ReplayHistory, canonical, digest
from core.replay.ai_template_runner import run_ai_template_replay, runtime_priority_guard
from core.replay.relay_policy import read_relay_policy, relay_policy_guard
from core.replay.ai_report import render_ai_template_report
from scripts.verify_ai_template_replay import audit_replay
from scripts.continue_ai_validation_segments import research_driver_lock


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def write_cases(directory, reports):
    """External memory candidates with evidence; no inferred win/lesson labels."""
    cases = []
    for report in reports:
        if not report.get("ledger_audit_passed"):
            continue
        for strategy in report["results"]:
            for trade in strategy.get("completed_trades", []):
                cases.append({"case_id": digest({"run": report["run_id"], "strategy": strategy["template_id"], "trade": trade}),
                              "template_id": strategy["template_id"], "known_at": trade["closed_at"],
                              "model_id": report["model_pin"]["model_id"], "run_id": report["run_id"],
                              "config_sha256": report["config_sha256"], "source": "AUDITED_HISTORICAL_SIMULATION",
                              "gate_verified": False, "trade": trade})
    (directory / "memory-candidates.jsonl").write_text(
        "".join(canonical(c) + "\n" for c in cases), encoding="utf-8")
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "reports/btc-eth-year-proxy-20261004")
    parser.add_argument("--rules-manifest", type=Path,
                        default=ROOT / "reports/btc-eth-ai-segments-20261004/calendar-a/history.json")
    parser.add_argument("--directory", type=Path, default=ROOT / "reports/gemini-year-research-20261005")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max-decisions", type=int, default=5)
    parser.add_argument("--runtime-status-url", default="http://127.0.0.1:18765/v2/ai-session/status")
    parser.add_argument("--model-budget-seconds", type=float, default=170.0)
    parser.add_argument("--template-ids", nargs="+", default=["price_action_structure"])
    parser.add_argument("--optimization-window-hours", type=int, choices=(12, 48), default=48)
    args = parser.parse_args()
    if not 1 <= args.max_decisions <= 1008:
        parser.error("max-decisions must be 1..1008; full-year requests are intentionally unsupported")
    if not 30 <= args.model_budget_seconds <= 170:
        parser.error("model-budget-seconds must be 30..170")
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with research_driver_lock(directory):
        plan = research_plan(args.archive, template_ids=args.template_ids,
                             optimization_window_hours=args.optimization_window_hours)
        if args.template_ids == ["price_action_structure"]:
            plan['acceptance_win_rate_target'] = 0.6
        plan["model_budget_seconds"] = args.model_budget_seconds
        plan["relay_inference_policy"] = read_relay_policy()
        save_plan(plan, directory / "research-plan.json")
        print(json.dumps({"event": "plan_verified", "expected_pilot_decisions": plan["expected_pilot_decisions"],
                          "partitions": plan["partitions"]}), flush=True)
        histories = []
        for window in plan["pilot_windows"]:
            target = directory / window["id"]
            target.mkdir(exist_ok=True)
            manifest = target / "history.json"
            freeze_window(plan, window, args.rules_manifest, manifest)
            histories.append((window, target, manifest))
            print(json.dumps({"event": "history_ready", "window": window["id"]}), flush=True)
        if not args.run:
            return 0
        remaining, reports = args.max_decisions, []
        for window, target, manifest in histories:
            if remaining <= 0:
                break
            database = target / "results.sqlite3"
            previous = 0
            if database.exists():
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    checkpoint = db.execute("SELECT checkpoint_json FROM ai_template_replay_runs").fetchone()
                    previous = json.loads(checkpoint[0])["decision_count"]
            def progress(value):
                line = canonical({"observed_at": datetime.now(timezone.utc).isoformat(), "window": window["id"], **value})
                with (directory / "progress.jsonl").open("a", encoding="utf-8") as log:
                    log.write(line + "\n")
                print(line, flush=True)
            report = run_ai_template_replay(ReplayHistory(manifest), db_path=database,
                max_decisions=remaining, resume=database.exists(), runtime_status_url=args.runtime_status_url,
                priority_guard=relay_policy_guard(plan["relay_inference_policy"],
                    runtime_priority_guard(args.runtime_status_url, budget_seconds=args.model_budget_seconds)),
                model_budget_seconds=args.model_budget_seconds, progress=progress,
                template_ids=args.template_ids)
            save(target / "results.json", report)
            render_ai_template_report(report, target / "comparison.html")
            audit = audit_replay(database, manifest)
            save(target / "ledger-audit.json", audit)
            report["ledger_audit_passed"] = audit["status"] == "PASS"
            reports.append(report)
            remaining -= max(0, report["decision_count"] - previous)
            if report["status"] != "COMPLETED":
                break
        memory_count = write_cases(directory, reports)
        summary = {"status": "COMPLETED" if len(reports) == len(histories) and all(
                      r["status"] == "COMPLETED" for r in reports) else "PARTIAL",
                   "scope": "OPTIMIZATION_CALENDAR_PILOT_NOT_ANNUAL_AI_RETURN",
                   "max_new_decisions": args.max_decisions, "model_weights_trained": False,
                   "production_strategy_writes": 0, "private_exchange_calls": 0,
                   "memory_candidates": memory_count,
                   "windows": [{k: r.get(k) for k in ("run_id", "status", "decision_count", "comparison_eligible",
                                                      "ledger_audit_passed", "errors", "results")} for r in reports]}
        save(directory / "pilot-summary.json", summary)
        from scripts.export_gemini_response_files import export
        print(json.dumps({"event": "gemini_json_export", **export(directory)}, ensure_ascii=False), flush=True)
        print(json.dumps({k: v for k, v in summary.items() if k != "windows"}), flush=True)
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
