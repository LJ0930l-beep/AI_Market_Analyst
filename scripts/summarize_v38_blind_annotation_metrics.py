"""Compute preregistered agreement metrics from a validated V38 label batch."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.blind_label_metrics import (
    BlindLabelMetricsError,
    summarize_v38_blind_label_batch,
)
from core.replay.pa_decision_quality_v38.blind_label_packets import (
    BlindLabelPacketError,
    write_json_exclusive,
)

REPORTS_ROOT = (ROOT / "reports" / "v38+").resolve()
DEFAULT_DATASET = REPORTS_ROOT / "dataset-stratified-purged-20261009-v1"


def _read_batch(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BlindLabelMetricsError("V38_LABEL_METRICS_BATCH_UNREADABLE") from exc
    if not isinstance(value, dict):
        raise BlindLabelMetricsError("V38_LABEL_METRICS_BATCH_UNREADABLE")
    return value


def _inside_reports(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(REPORTS_ROOT)
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-directory", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--label-batch", type=Path, required=True,
                        help="Validated label batch under the ignored reports/v38+ directory.")
    parser.add_argument("--output", type=Path, required=True,
                        help="New summary path under reports/v38+; existing files are never overwritten.")
    args = parser.parse_args(argv)
    if not _inside_reports(args.label_batch):
        parser.error("the label batch must remain under the ignored reports/v38+ directory")
    if not _inside_reports(args.output):
        parser.error("the metric summary must remain under the ignored reports/v38+ directory")
    try:
        batch = _read_batch(args.label_batch)
        summary = summarize_v38_blind_label_batch(args.dataset_directory, batch)
        write_json_exclusive(args.output, summary)
    except (BlindLabelMetricsError, BlindLabelPacketError) as exc:
        parser.error(exc.code)
    except OSError as exc:
        parser.error(f"V38_LABEL_METRICS_OUTPUT_FAILED:{exc.__class__.__name__}")
    print(json.dumps({
        "status": "V38_LABEL_METRICS_READY",
        "output": str(args.output.resolve()),
        "decision_count": summary["decision_count"],
        "rater_count": summary["rater_count"],
        "consensus_labels_created": summary["consensus_labels_created"],
        "decision_quality_status": summary["decision_quality_status"],
        "trading_performance_status": summary["trading_performance_status"],
        "model_calls_used": 0,
        "orders_created": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
