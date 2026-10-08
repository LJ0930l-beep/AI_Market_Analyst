"""Freeze and execute chronological held-out candidates; never activate trading."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import ReplayHistory, canonical, digest, utc
from core.replay.ai_template_runner import frozen_templates, run_ai_template_replay, runtime_priority_guard
from core.replay.relay_policy import read_relay_policy, relay_policy_guard
from core.replay.ai_report import render_ai_template_report
from core.replay.gemini_research import research_plan, save_plan, freeze_window
from scripts.analyze_gemini_research import optimization_evidence, VERSION
from scripts.continue_ai_validation_segments import research_driver_lock
from scripts.verify_ai_template_replay import audit_replay
from core.model_routing import is_verified_model_receipt


def candidate_instructions(artifact, source_plan_sha, template_ids=None):
    if artifact.get("plan_sha256") != source_plan_sha or artifact.get("requires_validation") is not True:
        raise ValueError("CANDIDATE_OPTIMIZATION_BINDING_INVALID")
    review, receipt = artifact.get("review", {}), artifact.get("receipt", {})
    messages = artifact.get("request_messages")
    if not isinstance(messages, list) or len(messages) != 2 or json.loads(messages[1]["content"]).get("plan_sha256") != source_plan_sha:
        raise ValueError("CANDIDATE_REQUEST_BINDING_INVALID")
    input_hash = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if review.get("status") != "CANDIDATE_ONLY_UNVALIDATED" or not is_verified_model_receipt(
        receipt, expected_prompt_version=VERSION, expected_input_hash=input_hash
    ):
        raise ValueError("CANDIDATE_MODEL_RECEIPT_INVALID")
    proposals = review.get("proposals")
    expected = tuple(template_ids) if template_ids is not None else tuple(t['template_id'] for t in frozen_templates())
    if (not isinstance(proposals, list) or len(proposals) != len(expected)
            or sorted(row.get('template_id', '') for row in proposals) != sorted(expected)):
        raise ValueError("RESEARCH_CANDIDATE_SET_INVALID")
    result = {row["template_id"]: row["candidate_instruction"] for row in proposals}
    frozen_templates(result, template_ids=expected)
    return result


def require_matching_input_budget(base, optimization):
    expected = optimization.get("application_input_budget")
    actual = base.get("application_input_budget")
    if (type(expected) is not int or expected <= 0
            or type(actual) is not int or actual != expected):
        raise ValueError("HELDOUT_APPLICATION_INPUT_BUDGET_MUST_MATCH_OPTIMIZATION")


def heldout_plan(base, candidate, phase, source_plan_sha):
    if phase not in {"validation", "untouched_test"}:
        raise ValueError("HELDOUT_PARTITION_REQUIRED")
    result = json.loads(canonical(base))
    partition = next(row for row in base["partitions"] if row["id"] == phase)
    start, end = utc(partition["start"]), utc(partition["end"])
    protocol = base.get("heldout_protocol") or {"window_hours": 48, "calendar_offsets_days": [14, 44, 74]}
    if protocol.get("window_hours") != 48 or protocol.get("calendar_offsets_days") != [14, 44, 74]:
        raise ValueError("HELDOUT_PREREGISTERED_PROTOCOL_REQUIRED")
    result["pilot_windows"] = [
        {"id": f"{phase}-{index + 1}", "partition": phase,
         "start": (start + timedelta(days=offset)).isoformat(),
         "end": (start + timedelta(days=offset, hours=48)).isoformat()}
        for index, offset in enumerate(protocol["calendar_offsets_days"])
    ]
    if any(utc(window["end"]) > end for window in result["pilot_windows"]):
        raise ValueError("HELDOUT_WINDOW_OUTSIDE_PARTITION")
    result.update(templates=frozen_templates(candidate, template_ids=[t['template_id'] for t in base['templates']]), candidate_sha256=digest(candidate),
        source_optimization_plan_sha256=source_plan_sha,
        selection="PREREGISTERED_CALENDAR_HELDOUT_WINDOWS_NOT_SELECTED_BY_RETURN",
        scope="HELDOUT_TECHNICAL_AI_PROXY_NOT_ANNUAL_OR_GATE_RETURN")
    result["expected_pilot_decisions"] = sum(
        int(48 * 60 / int(t["execution"]["scan_interval_minutes"])) for t in result["templates"]
    ) * len(result["pilot_windows"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimization-directory", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--phase", choices=("validation", "untouched_test"), default="validation")
    parser.add_argument("--validation-directory", type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--max-decisions", type=int, default=50)
    parser.add_argument("--runtime-status-url", default="http://127.0.0.1:18765/v2/ai-session/status")
    parser.add_argument("--model-budget-seconds", type=float, default=170)
    args = parser.parse_args()
    if not 1 <= args.max_decisions <= 1008 or not 30 <= args.model_budget_seconds <= 170:
        parser.error("invalid decision or model budget")
    source = args.optimization_directory.resolve()
    evidence = optimization_evidence(source)  # All optimization windows completed, real model, audited.
    registration = json.loads((source / "research-plan.json").read_text(encoding="utf-8"))["plan"]
    if registration.get("relay_inference_policy") != read_relay_policy():
        raise ValueError("HELDOUT_RELAY_POLICY_MUST_MATCH_OPTIMIZATION")
    artifact = json.loads((source / "strategy-candidates.json").read_text(encoding="utf-8"))
    identities = [t['template_id'] for t in evidence['styles']]
    candidate = candidate_instructions(artifact, evidence["plan_sha256"], identities)
    # Re-verify archive and current source; never inherit a stale source hash.
    hours = int((utc(registration['pilot_windows'][0]['end']) - utc(registration['pilot_windows'][0]['start'])).total_seconds() / 3600)
    base = research_plan(Path(registration["archive_database"]).parent,
                         template_ids=identities, optimization_window_hours=hours)
    require_matching_input_budget(base, registration)
    if 'acceptance_win_rate_target' in registration:
        base['acceptance_win_rate_target'] = registration['acceptance_win_rate_target']
    plan = heldout_plan(base, candidate, args.phase, evidence["plan_sha256"])
    plan["candidate_artifact_sha256"] = digest(artifact)
    plan["model_budget_seconds"] = args.model_budget_seconds
    plan["relay_inference_policy"] = registration["relay_inference_policy"]
    if args.phase == "untouched_test":
        if not args.validation_directory:
            parser.error("untouched test requires the completed validation directory")
        validated = json.loads((args.validation_directory / "heldout-summary.json").read_text(encoding="utf-8"))
        if validated.get("status") != "COMPLETED" or validated.get("candidate_sha256") != digest(candidate):
            raise ValueError("MATCHING_COMPLETED_VALIDATION_REQUIRED")
        for window in validated["windows"]:
            target = args.validation_directory / window["id"]
            if audit_replay(target / "results.sqlite3", target / "history.json", require_complete=True)["status"] != "PASS":
                raise ValueError("VALIDATION_LEDGER_AUDIT_REQUIRED")
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with research_driver_lock(directory):
        save_plan(plan, directory / "research-plan.json")
        remaining, reports = args.max_decisions, []
        # This manifest already pins the same verified Gate rules used in optimization.
        rules_path = source / registration["pilot_windows"][0]["id"] / "history.json"
        for window in plan["pilot_windows"]:
            target = directory / window["id"]
            target.mkdir(exist_ok=True)
            freeze_window(plan, window, rules_path, target / "history.json")
            if not args.run or remaining <= 0:
                continue
            database = target / "results.sqlite3"
            previous = 0
            if database.exists():
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                    previous = json.loads(db.execute("SELECT checkpoint_json FROM ai_template_replay_runs").fetchone()[0])["decision_count"]
            def progress(value):
                record = canonical({"observed_at": datetime.now(timezone.utc).isoformat(), "window": window["id"], **value})
                with (directory / "progress.jsonl").open("a", encoding="utf-8") as log:
                    log.write(record + "\n")
                print(record, flush=True)
            report = run_ai_template_replay(ReplayHistory(target / "history.json"), db_path=database,
                max_decisions=remaining, resume=database.exists(), runtime_status_url=args.runtime_status_url,
                priority_guard=relay_policy_guard(plan["relay_inference_policy"],
                    runtime_priority_guard(args.runtime_status_url, budget_seconds=args.model_budget_seconds)),
                candidate_instructions=candidate, template_ids=identities,
                model_budget_seconds=args.model_budget_seconds, progress=progress)
            (target / "results.json").write_text(canonical(report), encoding="utf-8")
            render_ai_template_report(report, target / "comparison.html")
            audit = audit_replay(database, target / "history.json")
            (target / "ledger-audit.json").write_text(canonical(audit), encoding="utf-8")
            reports.append({"id": window["id"], "status": report["status"], "audit": audit["status"],
                            "comparison_eligible": report["comparison_eligible"], "results": report["results"]})
            remaining -= report["decision_count"] - previous
            if report["status"] != "COMPLETED":
                break
        if args.run:
            summary = {"status": "COMPLETED" if len(reports) == 3 and all(
                row["status"] == "COMPLETED" and row["audit"] == "PASS" for row in reports) else "PARTIAL",
                "phase": args.phase, "candidate_sha256": digest(candidate), "plan_sha256": digest(plan),
                "scope": plan["scope"], "production_strategy_writes": 0, "windows": reports}
            (directory / "heldout-summary.json").write_text(canonical(summary), encoding="utf-8")
            print(canonical({key: value for key, value in summary.items() if key != "windows"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
