"""Replay V37.1 market contexts through a labeled local fixture; never calls a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_INPUT_SCHEMA = "pa-decision-quality-v37.1/decision-points-1"

from core.replay.pa_decision_quality_v38.market_only import run_offline_stub


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_v37_points(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = Path(path).resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("V37_MARKET_DATASET_UNAVAILABLE_OR_INVALID") from exc
    if (not isinstance(document, dict)
            or document.get("schema_version") != EXPECTED_INPUT_SCHEMA
            or document.get("dataset_kind") != "RECONSTRUCTED_MARKET_BENCHMARK"
            or document.get("forensic_dataset_included") is not False
            or document.get("cache_rows") != []
            or not isinstance(document.get("decision_points"), list)
            or not document["decision_points"]):
        raise ValueError("V37_MARKET_DATASET_CONTRACT_INVALID")
    points = document["decision_points"]
    if any(not isinstance(point, dict) for point in points):
        raise ValueError("V37_MARKET_DECISION_POINT_INVALID")
    return document, points


def _output_path(value: str) -> Path:
    output = (ROOT / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    allowed_root = (ROOT / "reports" / "v38+").resolve()
    try:
        output.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("V38_OUTPUT_MUST_BE_UNDER_IGNORED_REPORTS_DIRECTORY") from exc
    if output.exists():
        raise ValueError("V38_OUTPUT_EXISTS_REFUSE_OVERWRITE")
    return output


def build_report(input_path: Path) -> dict[str, Any]:
    document, points = load_v37_points(input_path)
    result = run_offline_stub(points)
    source_path = Path(input_path).resolve()
    report = {
        "schema_version": "pa-market-only-v38/offline-stub-report-1",
        "run_mode": "OFFLINE_STUB_FIXTURE",
        "source_dataset_kind": document["dataset_kind"],
        "source_dataset_sha256": _sha256(source_path),
        "source_v37_a3_policy_sha256": document.get("a3_signal_policy_sha256"),
        "gemini_called": False,
        "model_calls_used": 0,
        "orders_created": 0,
        "result": result,
    }
    report["report_sha256"] = _canonical_sha(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline-only V38 market-only plumbing check; this command cannot call Gemini or create orders.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Existing V37.1 decision-points JSON.")
    parser.add_argument("--offline-stub", action="store_true", required=True,
                        help="Required confirmation that only a deterministic local fixture is being used.")
    parser.add_argument("--output", required=True, help="New path below reports/v38+; existing files are never overwritten.")
    args = parser.parse_args(argv)
    try:
        target = _output_path(args.output)
        report = build_report(args.input)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "output": str(target),
        "report_sha256": report["report_sha256"],
        "decision_count": report["result"]["metrics"]["decision_count_denominator"],
        "model_calls_used": 0,
        "orders_created": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
