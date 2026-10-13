"""Validate two or more complete V38 blind annotation submissions offline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.blind_label_packets import (
    BlindLabelPacketError,
    import_blind_label_submissions,
    write_json_exclusive,
)

REPORTS_ROOT = (ROOT / "reports" / "v38+").resolve()
DEFAULT_DATASET = REPORTS_ROOT / "dataset-stratified-purged-20261009-v1"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BlindLabelPacketError("BLIND_SUBMISSION_FILE_INVALID") from exc
    if not isinstance(value, dict):
        raise BlindLabelPacketError("BLIND_SUBMISSION_FILE_INVALID")
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
    parser.add_argument("--pair", action="append", nargs=2, metavar=("ESCROW_JSON", "SUBMISSION_JSON"),
                        required=True, help="Repeat once for each independent rater.")
    parser.add_argument("--output", type=Path, required=True,
                        help="New output path under reports/v38+; existing files are never overwritten.")
    args = parser.parse_args(argv)
    if not _inside_reports(args.output):
        parser.error("the combined label artifact must remain under ignored reports/v38+")
    try:
        pairs = [(_read_json(Path(escrow)), _read_json(Path(submission)))
                 for escrow, submission in args.pair]
        result = import_blind_label_submissions(args.dataset_directory, pairs)
        write_json_exclusive(args.output, result)
    except BlindLabelPacketError as exc:
        parser.error(exc.code)
    except OSError as exc:
        parser.error(f"BLIND_SUBMISSION_WRITE_FAILED:{exc.__class__.__name__}")
    print(json.dumps({
        "status": result["validation_status"],
        "output": str(args.output.resolve()),
        "raters": result["rater_count"],
        "decisions": result["decision_count"],
        "annotations": result["annotation_count"],
        "untouched_test_file_read": False,
        "model_calls_used": 0,
        "orders_created": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
