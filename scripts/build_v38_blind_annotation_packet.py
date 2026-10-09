"""Build a self-contained, offline V38 blind label packet for one pseudonymous rater."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.blind_label_packets import (
    BlindLabelPacketError,
    create_blind_annotation_packet,
    render_blind_annotation_html,
    write_json_exclusive,
    write_text_exclusive,
)

REPORTS_ROOT = (ROOT / "reports" / "v38+").resolve()
DEFAULT_DATASET = REPORTS_ROOT / "dataset-stratified-purged-20261009-v1"


def _inside_reports(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(REPORTS_ROOT)
        return True
    except ValueError:
        return False


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-directory", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--rater-id", required=True,
                        help="Pseudonymous reviewer code, e.g. reviewer_a; do not use a real name.")
    parser.add_argument("--packet-directory", type=Path,
                        default=REPORTS_ROOT / "blind-label-packets")
    parser.add_argument("--escrow-directory", type=Path,
                        default=REPORTS_ROOT / "blind-label-escrow")
    args = parser.parse_args(argv)
    if not _inside_reports(args.packet_directory) or not _inside_reports(args.escrow_directory):
        parser.error("packet and escrow outputs must remain under the ignored reports/v38+ directory")
    try:
        packet, escrow = create_blind_annotation_packet(
            args.dataset_directory, rater_id=args.rater_id,
        )
        packet_path = args.packet_directory.resolve() / f"{args.rater_id}-{packet['packet_id']}.html"
        escrow_path = args.escrow_directory.resolve() / f"{args.rater_id}-{packet['packet_id']}.json"
        html = render_blind_annotation_html(packet)
        # A failed second write removes only the first file created by this invocation.
        write_text_exclusive(packet_path, html)
        try:
            write_json_exclusive(escrow_path, escrow)
        except Exception:
            packet_path.unlink(missing_ok=True)
            raise
    except BlindLabelPacketError as exc:
        parser.error(exc.code)
    except OSError as exc:
        parser.error(f"BLIND_PACKET_WRITE_FAILED:{exc.__class__.__name__}")
    print(json.dumps({
        "status": "BLIND_PACKET_READY_FOR_MANUAL_REVIEW",
        "packet_path": str(packet_path),
        "packet_sha256": packet["packet_sha256"],
        "packet_file_sha256": _sha256_text(html),
        "escrow_path": str(escrow_path),
        "escrow_sha256": escrow["escrow_sha256"],
        "rater_id": packet["rater_id"],
        "visible_items": len(packet["items"]),
        "model_calls_used": 0,
        "orders_created": 0,
        "untouched_test_file_read": False,
        "labels_generated": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
