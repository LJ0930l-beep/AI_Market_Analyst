"""Refresh resume-sensitive summary fields for a completed Phase 3 run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.storage import SQLiteStore


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * 0.95))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh a Phase 3 replay JSON summary from SQLite")
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    store = SQLiteStore(args.db)
    store.initialize()
    run = store.get_replay_run(args.run_id)
    if run is None:
        raise SystemExit(f"replay run not found: {args.run_id}")
    output_path = Path(args.output)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    latencies = store.list_replay_model_latencies(args.run_id)
    payload["run_id"] = args.run_id
    payload["status"] = run["status"]
    payload["counts"] = run["counts"]
    payload["model_latency_ms"] = {
        "count": len(latencies),
        "mean": mean(latencies) if latencies else None,
        "median": median(latencies) if latencies else None,
        "p95": _p95(latencies),
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": args.run_id, "status": run["status"], "counts": run["counts"], "model_latency_ms": payload["model_latency_ms"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
