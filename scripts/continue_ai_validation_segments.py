"""Run preregistered AI segments serially, sharing the model with production."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import ReplayHistory, digest
from core.replay.ai_report import render_ai_template_report
from core.replay.ai_template_runner import frozen_source_fingerprint, frozen_templates, run_ai_template_replay
from scripts.verify_ai_template_replay import audit_replay


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def validate_segments(plan, frozen, plan_hash):
    if frozen.get("plan_sha256") != plan_hash:
        raise ValueError("SEGMENT_FROZEN_PLAN_DRIFT")
    expected = plan["windows"]
    observed = frozen["segments"]
    if len(expected) != 3 or [s["id"] for s in observed] != [s["id"] for s in expected]:
        raise ValueError("SEGMENT_PLAN_WINDOWS_MISSING_DUPLICATE_OR_REORDERED")
    if len({s["id"] for s in expected}) != 3:
        raise ValueError("SEGMENT_PLAN_DUPLICATE_WINDOWS")
    histories = []
    for scheduled, segment in zip(expected, observed):
        history = ReplayHistory(Path(segment["history_path"]))
        payload = history.payload
        if (payload["manifest_sha256"] != segment["manifest_sha256"]
                or payload.get("validation_plan_sha256") != plan_hash
                or payload["window_start"] != scheduled["start"]
                or payload["window_end"] != scheduled["end"]
                or list(history.symbols) != plan["symbols"] or not payload["complete_data"]):
            raise ValueError("SEGMENT_HISTORY_DOES_NOT_MATCH_REGISTERED_WINDOW")
        native_calls = sum(when.minute % int(template["execution"]["scan_interval_minutes"]) == 0
            for when in history.decision_points for template in plan["templates"])
        if native_calls != plan["expected_calls_per_window"]:
            raise ValueError("SEGMENT_NATIVE_SCAN_COUNT_MISMATCH")
        histories.append(history)
    if len(histories) * plan["expected_calls_per_window"] != plan["expected_total_calls"]:
        raise ValueError("SEGMENT_TOTAL_NATIVE_SCAN_COUNT_MISMATCH")
    return histories


def bind_model_pin(database, pin_path):
    """Bind all independent windows to one actual weight/context identity."""
    database = Path(database).resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        records = connection.execute("SELECT config_json FROM ai_template_replay_runs").fetchall()
    if len(records) != 1:
        raise ValueError("SEGMENT_ONE_MODEL_CONFIG_REQUIRED")
    pin = json.loads(records[0][0])["model_pin"]
    if not pin or not all(pin.get(k) for k in ("actual_model_id", "model_id", "context_length")):
        raise ValueError("SEGMENT_MODEL_PIN_REQUIRED")
    if pin_path.exists():
        if json.loads(pin_path.read_text(encoding="utf-8"))["model_pin"] != pin:
            raise ValueError("SEGMENT_CROSS_WINDOW_MODEL_IDENTITY_DRIFT")
    else:
        save_json(pin_path, {"bound_at": datetime.now(timezone.utc).isoformat(), "model_pin": pin})
    return pin


@contextmanager
def research_driver_lock(root):
    """One OS-held driver lock; a stale file cannot block a later resume."""
    with (root / ".driver.lock").open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("AI_SEGMENT_DRIVER_ALREADY_RUNNING") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--runtime-status-url", default="http://127.0.0.1:18765/v2/ai-session/status")
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with research_driver_lock(root):
        return run_segments(args)


def run_segments(args):
    root = args.directory.resolve()
    registration = json.loads((root / "validation-plan.json").read_text(encoding="utf-8"))
    plan = registration["plan"]
    if (digest(plan) != registration["plan_sha256"] or plan["source_sha256"] != frozen_source_fingerprint()
            or plan["templates"] != frozen_templates()):
        raise ValueError("SEGMENT_PREREGISTRATION_OR_SOURCE_DRIFT")
    frozen = json.loads((root / "frozen-segments.json").read_text(encoding="utf-8"))
    histories = validate_segments(plan, frozen, registration["plan_sha256"])
    states = {}

    def progress(value):
        record = {"observed_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), **value}
        line = json.dumps(record, ensure_ascii=False, allow_nan=False)
        with (root / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        print(line, flush=True)

    summary = []
    for segment, history in zip(frozen["segments"], histories):
        key = segment["id"]
        directory = root / key
        manifest = directory / "history.json"
        if manifest.resolve() != Path(segment["history_path"]).resolve():
            raise ValueError("SEGMENT_HISTORY_DRIFT")
        database = directory / "results.sqlite3"
        while True:
            # No external training sink, private exchange call, or session action.
            report = run_ai_template_replay(history, db_path=database,
                initial_equity=plan["initial_equity"], model_budget_seconds=70,
                max_decisions=5, resume=database.exists(), runtime_status_url=args.runtime_status_url,
                allow_stopped_runtime=True, progress=lambda value: progress({"segment": key, **value}))
            save_json(directory / "results.json", report)
            render_ai_template_report(report, directory / "comparison.html")
            model_pin = bind_model_pin(database, root / "model-pin.json")
            state = (report["status"], report.get("pause_reason"), report["decision_count"])
            if states.get(key) != state:
                progress({"segment": key, "status": report["status"], "pause_reason": report.get("pause_reason"),
                    "decision_count": report["decision_count"], "comparison_eligible": report["comparison_eligible"]})
                states[key] = state
            if report["status"] == "COMPLETED":
                partial_audit = audit_replay(database, manifest)
                save_json(directory / "independent-ledger-audit.json", partial_audit)
                strict_error = None
                try:
                    strict_audit = audit_replay(database, manifest, require_complete=True)
                except ValueError as exc:
                    strict_error = str(exc)
                    strict_audit = {"status": "NOT_QUALIFIED", "reason": strict_error,
                        "note": "Retain model failures and all account results; do not erase or recall failed scans."}
                save_json(directory / "complete-comparison-audit.json", strict_audit)
                summary.append({"id": key, "manifest_sha256": segment["manifest_sha256"],
                    "status": report["status"], "decision_count": report["decision_count"],
                    "model_pin": model_pin,
                    "comparison_eligible": report["comparison_eligible"] and strict_error is None,
                    "errors": report.get("errors", []), "results": report["results"]})
                save_json(root / "segment-results.json", {"plan_sha256": registration["plan_sha256"],
                    "status": "IN_PROGRESS", "completed_segments": summary})
                break
            if report["status"] != "PAUSED":
                save_json(root / "segment-results.json", {"plan_sha256": registration["plan_sha256"],
                    "status": "FAILED", "comparison_eligible": False, "completed_segments": summary,
                    "failed_segment": {"id": key, "status": report["status"],
                        "pause_reason": report.get("pause_reason"), "errors": report.get("errors", []),
                        "decision_count": report["decision_count"], "results": report["results"]}})
                progress({"segment": key, "status": "SEGMENT_REQUIRES_ATTENTION", "root_status": "FAILED"})
                return 2
            if report.get("pause_reason") != "PILOT_DECISION_LIMIT":
                time.sleep(10)
    eligible = all(segment["comparison_eligible"] for segment in summary)
    final = {"plan_sha256": registration["plan_sha256"], "status": "COMPLETED",
        "comparison_eligible": eligible, "completed_segments": summary,
        "scope": "ACTUAL_GEMINI_THREE_INDEPENDENT_GATE_TECHNICAL_SEGMENTS_NOT_ANNUAL_AI_RETURN"}
    save_json(root / "segment-results.json", final)
    progress({"status": "ALL_SEGMENTS_COMPLETED", "comparison_eligible": eligible})
    return 0 if eligible else 3


if __name__ == "__main__":
    raise SystemExit(main())
