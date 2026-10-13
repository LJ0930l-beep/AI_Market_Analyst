"""CLI for building a new local V38 stratified-purged market-only dataset."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.stratified_purged_dataset import (
    build_stratified_purged_v38_dataset,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_stratified_purged_v38_dataset(args.archive_directory, args.output_directory)
    except (OSError, ValueError) as exc:
        code = getattr(exc, "code", "V38_STRATIFIED_PURGED_BUILD_FAILED")
        print(json.dumps({"status": "FAILED", "error_code": code}, sort_keys=True))
        return 2
    print(json.dumps({
        "status": "BUILT_LOCAL_ONLY",
        "dataset_id": manifest["dataset_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "contexts": manifest["total_market_contexts"],
        "visible_optimization_validation": manifest["optimization_validation_input_count"],
        "untouched_test_hash_only": manifest["untouched_test_hash_only_count"],
        "model_calls_used": manifest["gemini_research_calls_used"],
        "orders_created": manifest["orders_created"],
        "output_directory": str(args.output_directory.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())