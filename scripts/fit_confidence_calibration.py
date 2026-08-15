"""Fit and persist Phase 3 Calibration v1 without overwriting raw confidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.performance.calibration import calibrated_confidence, fit_calibration
from core.performance.metrics import is_actionable
from core.storage import SQLiteStore


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fit Phase 3 empirical confidence calibration")
    parser.add_argument("--db", default="data/phase3-replay.sqlite3")
    parser.add_argument("--source-type", default="replay", choices=("live", "replay"))
    parser.add_argument("--run-id")
    parser.add_argument("--model-id")
    parser.add_argument("--prompt-version")
    parser.add_argument("--min-sample", type=int, default=100)
    parser.add_argument("--trained-until")
    parser.add_argument("--scope-json", default="{}")
    parser.add_argument("--output", default="data/phase3-calibration.json")
    args = parser.parse_args()

    scope = json.loads(args.scope_json)
    if not isinstance(scope, dict):
        raise SystemExit("--scope-json must be a JSON object")
    scope.setdefault("source_type", args.source_type)
    if args.model_id:
        scope.setdefault("model_id", args.model_id)
    if args.prompt_version:
        scope.setdefault("prompt_version", args.prompt_version)
    store = SQLiteStore(args.db)
    store.initialize()
    records = store.list_prediction_records(
        source_type=args.source_type,
        replay_run_id=args.run_id,
        model_id=args.model_id,
        prompt_version=args.prompt_version,
    )
    result = fit_calibration(
        records,
        scope=scope,
        global_records=records,
        trained_until=_parse_time(args.trained_until),
        min_sample=max(1, args.min_sample),
        version=f"cal-v1-{str(scope.get('model_id') or 'global').replace(':', '-')}",
    )
    payload = result.to_dict()
    store.save_calibration_result(payload)
    updated = 0
    for record in records:
        prediction = record["prediction"]
        if not is_actionable(record):
            calibration = {
                "calibrated_confidence": None,
                "calibration_version": result.version,
                "calibration_scope": "global" if not result.scope else json.dumps(result.scope, sort_keys=True, separators=(",", ":")),
                "calibration_sample_size": result.sample_count,
                "calibration_fallback": result.fallback,
            }
        else:
            raw = float(prediction.get("raw_confidence", 0.0))
            calibration = {
                "calibrated_confidence": calibrated_confidence(raw, result),
                "calibration_version": result.version,
                "calibration_scope": "global" if not result.scope else json.dumps(result.scope, sort_keys=True, separators=(",", ":")),
                "calibration_sample_size": result.sample_count,
                "calibration_fallback": result.fallback,
            }
        store.update_prediction_calibration(prediction["prediction_id"], calibration)
        updated += 1
    payload["updated_predictions"] = updated
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
