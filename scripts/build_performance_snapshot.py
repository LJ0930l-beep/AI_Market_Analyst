"""Build one or more deterministic Phase 3 PerformanceSnapshot artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.performance.metrics import build_performance_snapshot, record_dimensions
from core.storage import SQLiteStore


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Phase 3 performance snapshots")
    parser.add_argument("--db", default="data/phase3-replay.sqlite3")
    parser.add_argument("--source-type", default="replay", choices=("live", "replay"))
    parser.add_argument("--run-id")
    parser.add_argument("--symbol")
    parser.add_argument("--timeframe")
    parser.add_argument("--model-id")
    parser.add_argument("--prompt-version")
    parser.add_argument("--window-days", type=int)
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--group-by", nargs="*", default=[])
    parser.add_argument("--output", default="data/phase3-performance.json")
    args = parser.parse_args()

    store = SQLiteStore(args.db)
    store.initialize()
    records = store.list_prediction_records(
        source_type=args.source_type,
        replay_run_id=args.run_id,
        symbol=args.symbol,
        timeframe=args.timeframe,
        model_id=args.model_id,
        prompt_version=args.prompt_version,
    )
    window_end = _parse_time(args.window_end)
    if args.window_days is not None and window_end is None:
        window_end = datetime.now(timezone.utc)
    window_start = _parse_time(args.window_start)
    if args.window_days is not None:
        window_start = window_end - timedelta(days=max(1, args.window_days))  # type: ignore[operator]
    scope = {"source_type": args.source_type}
    for key, value in (("symbol", args.symbol), ("timeframe", args.timeframe), ("model_id", args.model_id), ("prompt_version", args.prompt_version)):
        if value:
            scope[key] = value
    snapshots = [build_performance_snapshot(records, scope=scope, window_start=window_start, window_end=window_end)]
    for dimension in args.group_by:
        values = sorted({record_dimensions(record).get(dimension) for record in records if record_dimensions(record).get(dimension) is not None})
        for value in values:
            grouped_scope = dict(scope)
            grouped_scope[dimension] = value
            snapshot = build_performance_snapshot(records, scope=grouped_scope, window_start=window_start, window_end=window_end)
            snapshots.append(snapshot)
            store.save_performance_snapshot(snapshot)
    store.save_performance_snapshot(snapshots[0])
    payload = {
        "phase": 3,
        "source_type": args.source_type,
        "record_count": len(records),
        "snapshots": snapshots,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
