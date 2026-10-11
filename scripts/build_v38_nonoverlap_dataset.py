"""Build the frozen V38 nonoverlap dataset from an existing local archive only."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.nonoverlap_dataset import (
    NonOverlapDatasetError,
    build_nonoverlap_v38_dataset,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path,
                        help="Existing verified Binance UM public archive directory.")
    parser.add_argument("--output", required=True, type=Path,
                        help="New local output under reports/v38+; existing non-empty output is rejected.")
    args = parser.parse_args(argv)
    try:
        manifest = build_nonoverlap_v38_dataset(args.archive, args.output)
    except (NonOverlapDatasetError, KeyError, OSError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "PASS",
        "dataset_id": manifest["dataset_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "total_market_contexts": manifest["total_market_contexts"],
        "partition_counts": manifest["partition_counts"],
        "paired_temporal_anchor_count": manifest["paired_temporal_anchor_count"],
        "same_symbol_input_window_overlap_count": manifest["same_symbol_input_window_overlap_count"],
        "cross_partition_input_window_overlap_count": manifest["cross_partition_input_window_overlap_count"],
        "model_calls_used": 0,
        "network_calls": 0,
        "orders_created": 0,
        "output": str(Path(args.output).resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
