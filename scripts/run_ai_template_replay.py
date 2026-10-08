"""Run the five production AI templates against a frozen continuous history."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Frozen ai_template_history_v1 JSON")
    parser.add_argument("--db", required=True, help="Separate replay results SQLite database")
    parser.add_argument("--output", required=True, help="Result JSON report")
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--max-decisions", type=int, help="Stop after this many calls for a resumable route pilot")
    parser.add_argument("--model-budget-seconds", type=float, default=70.0)
    parser.add_argument("--runtime-status-url", required=True, help="Loopback production /v2/ai-session/status URL")
    parser.add_argument("--allow-stopped-runtime", action="store_true",
                        help="Allow research when the backend connection is refused; still require an idle model slot")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    from core.replay.ai_history import ReplayHistory
    from core.replay.ai_template_runner import run_ai_template_replay

    def progress(value: dict) -> None:
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)

    try:
        history = ReplayHistory(args.manifest)
        result = run_ai_template_replay(history, db_path=args.db, initial_equity=args.initial_equity,
                                        max_decisions=args.max_decisions, resume=args.resume,
                                        model_budget_seconds=args.model_budget_seconds,
                                        allow_stopped_runtime=args.allow_stopped_runtime,
                                        runtime_status_url=args.runtime_status_url, progress=progress)
        target = Path(args.output).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        progress({"run_id": result["run_id"], "status": result["status"], "pause_reason": result["pause_reason"],
                  "decision_count": result["decision_count"], "complete_window": result["complete_window"],
                  "comparison_eligible": result["comparison_eligible"], "output": str(target)})
        return 0 if result["status"] == "COMPLETED" else 3
    except Exception as exc:
        progress({"status": "FAILED", "error_code": type(exc).__name__ + ":" + str(exc)[:240]})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
