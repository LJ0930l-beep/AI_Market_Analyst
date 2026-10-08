"""Run/resume one frozen AI replay, yielding to the production model schedule."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--runtime-status-url", default="http://127.0.0.1:18765/v2/ai-session/status")
    parser.add_argument("--allow-stopped-runtime", action="store_true",
                        help="Allow research only when the backend connection is refused and the model slot is idle")
    parser.add_argument("--model-budget-seconds", type=float, default=70.0)
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--batch-decisions", type=int, default=10)
    args = parser.parse_args()
    from core.replay.ai_history import ReplayHistory
    from core.replay.ai_report import render_ai_template_report
    from core.replay.ai_template_runner import run_ai_template_replay

    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    history = ReplayHistory(args.manifest)
    database = directory / "results.sqlite3"
    last_status = None

    def progress(value):
        record = {"observed_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), **value}
        line = json.dumps(record, ensure_ascii=False, allow_nan=False)
        with (directory / "progress.jsonl").open("a", encoding="utf-8") as log:
            log.write(line + "\n")
        print(line, flush=True)

    while True:
        report = run_ai_template_replay(history, db_path=database, initial_equity=args.initial_equity,
            max_decisions=args.batch_decisions, resume=database.exists(),
            allow_stopped_runtime=args.allow_stopped_runtime,
            model_budget_seconds=args.model_budget_seconds, runtime_status_url=args.runtime_status_url, progress=progress)
        target = directory / "results.json"
        temporary = directory / "results.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(target)
        render_ai_template_report(report, directory / "comparison.html")
        state = (report["status"], report.get("pause_reason"), report["decision_count"])
        if state != last_status:
            progress({"status": report["status"], "pause_reason": report.get("pause_reason"),
                      "decision_count": report["decision_count"], "result_path": str(target)})
            last_status = state
        if report["status"] == "COMPLETED":
            return 0
        if report["status"] != "PAUSED":
            return 2
        if report.get("pause_reason") == "PILOT_DECISION_LIMIT":
            continue
        # The process stays live while yielding to a specific confirmed local
        # scheduler. No account/session state is changed or restarted.
        time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
