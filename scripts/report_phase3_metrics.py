"""Produce a compact Phase 3 Markdown/JSON reporting artifact."""

from __future__ import annotations

import argparse
import json
import sys
from statistics import mean, median
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.performance.metrics import aggregate_performance, record_dimensions
from core.storage import SQLiteStore


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * 0.95))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="Report Phase 3 performance and calibration")
    parser.add_argument("--db", default="data/phase3-replay.sqlite3")
    parser.add_argument("--source-type", default="replay", choices=("live", "replay"))
    parser.add_argument("--run-id")
    parser.add_argument("--output", default="data/phase3-metrics-report.json")
    parser.add_argument("--markdown")
    args = parser.parse_args()

    store = SQLiteStore(args.db)
    store.initialize()
    records = store.list_prediction_records(source_type=args.source_type, replay_run_id=args.run_id)
    latencies = store.list_replay_model_latencies(args.run_id) if args.run_id and args.source_type == "replay" else []
    base_scope = {"source_type": args.source_type}
    global_metrics = aggregate_performance(records, scope=base_scope)
    groups: dict[str, list[dict[str, object]]] = {}
    for dimension in ("asset_type", "symbol", "timeframe", "action", "regime", "model_id", "prompt_version", "source_type"):
        values = sorted({record_dimensions(record).get(dimension) for record in records if record_dimensions(record).get(dimension) is not None})
        groups[dimension] = [
            {"value": value, "metrics": aggregate_performance(records, scope=base_scope | {dimension: value})}
            for value in values
        ]
    payload = {
        "phase": 3,
        "source_type": args.source_type,
        "run_id": args.run_id,
        "latency_ms": {
            "count": len(latencies),
            "mean": mean(latencies) if latencies else None,
            "median": median(latencies) if latencies else None,
            "p95": _p95(latencies),
        },
        "global": global_metrics,
        "groups": groups,
        "replay_runs": store.list_replay_runs(),
        "calibrations": store.list_calibration_results(),
        "performance_snapshots": store.list_performance_snapshots(source_type=args.source_type),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown:
        metrics = global_metrics
        markdown = "\n".join(
            [
                "# Phase 3 Metrics Report",
                "",
                f"- Source: `{args.source_type}`",
                f"- Samples: `{metrics['sample_count']}`",
                f"- Actionable / resolved: `{metrics['actionable_count']} / {metrics['resolved_actionable']}`",
                f"- Win Rate: `{metrics['win_rate']}`",
                f"- Avg R / Expectancy: `{metrics['avg_r']} / {metrics['expectancy_r']}`",
                f"- Profit Factor: `{metrics['profit_factor']}`",
                f"- Max Drawdown R: `{metrics['max_drawdown_r']}`",
                f"- Coverage / WAIT Rate: `{metrics['coverage']} / {metrics['wait_rate']}`",
                f"- Brier raw / calibrated: `{metrics['brier_raw']} / {metrics['brier_calibrated']}`",
                f"- ECE raw / calibrated: `{metrics['ece_raw']} / {metrics['ece_calibrated']}`",
                f"- Model latency count / mean / median / P95 ms: `{len(latencies)} / {mean(latencies) if latencies else None} / {median(latencies) if latencies else None} / {_p95(latencies)}`",
                "",
                "Calibration status is reported separately and is never inferred from win rate alone.",
            ]
        )
        Path(args.markdown).write_text(markdown + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
