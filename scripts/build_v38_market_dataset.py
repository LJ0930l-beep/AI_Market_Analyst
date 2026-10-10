"""Build the frozen V38 sample from an existing verified local Binance archive."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.dataset import DatasetBuildError, build_v38_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline-only V38 dataset builder; reads existing local archives and never downloads data.",
    )
    parser.add_argument("--archive", required=True, type=Path,
                        help="Existing verified Binance UM public archive directory.")
    parser.add_argument("--output", required=True, type=Path,
                        help="New local output directory under reports/v38+; existing files are never overwritten.")
    args = parser.parse_args(argv)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    try:
        result = build_v38_dataset(args.archive, output)
    except (DatasetBuildError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "dataset_id": result["dataset_id"],
        "manifest_sha256": result["manifest_sha256"],
        "total_market_contexts": result["total_market_contexts"],
        "optimization_validation_input_count": result["optimization_validation_input_count"],
        "untouched_test_hash_only_count": result["untouched_test_hash_only_count"],
        "model_calls_used": 0,
        "orders_created": 0,
        "output": str(output.resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
