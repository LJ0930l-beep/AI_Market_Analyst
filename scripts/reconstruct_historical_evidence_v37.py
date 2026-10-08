"""Build local, offline V37 evidence reports without model or order access."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# Allow both ``python scripts/...py`` and ``python -m scripts....`` invocation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.replay.pa_decision_quality_v36.review import summarize
from core.replay.pa_decision_quality_v36.runner import run_study
from core.replay.pa_decision_quality_v37.evidence_builder import (
    build_reconstructed_point,
    canonical_sha256,
)
from core.replay.pa_decision_quality_v37.inventory import build_source_inventory
from core.replay.pa_decision_quality_v37.recovery import (
    economic_coverage_from_v25,
    recover_v25_summary,
)
from core.replay.pa_decision_quality_v37.schema import load_frozen_a0_schema

CALENDAR_ANCHORS = (
    ("v37-benchmark-btc-20251015-1200z", "2025-10-15T12:00:00Z", "research"),
    ("v37-benchmark-btc-20251214-1200z", "2025-12-14T12:00:00Z", "validation"),
    ("v37-benchmark-btc-20260212-1200z", "2026-02-12T12:00:00Z", "untouched_test"),
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _write_new_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _json_bytes(value)
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError as exc:
        raise ValueError("V37_OUTPUT_EXISTS_REFUSE_OVERWRITE:" + path.name) from exc
    return hashlib.sha256(data).hexdigest()


def _experiment_status_counts(decision_records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for experiment_id in ("A0", "A1", "A2", "A3"):
        counts: Counter[str] = Counter()
        for record in decision_records:
            experiments = record.get("experiments") if isinstance(record, dict) else None
            item = experiments.get(experiment_id) if isinstance(experiments, dict) else None
            if isinstance(item, dict):
                counts[str(item.get("status") or "MISSING")] += 1
        output[experiment_id] = dict(sorted(counts.items()))
    return output


def _run_readonly_cli(script: Path, args: list[str], *, project_root: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", f"scripts.{script.stem}", *args],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"V37_OFFLINE_SUBCOMMAND_FAILED:{script.name}:{completed.returncode}"
        )


def build_reports(project_root: Path, local_data_root: Path, output_dir: Path) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    local_data_root = Path(local_data_root).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("V37_OUTPUT_DIRECTORY_NOT_EMPTY_REFUSE_OVERWRITE")
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_outputs = (
        "source-inventory.json", "a0-recovery-coverage.json",
        "economic-evidence-coverage.json", "market-reconstruction-manifest.json",
        "decision-points.json", "offline-replay-reference.json",
        "offline-replay.json", "offline-review.json", "replay-readiness.json",
    )
    if any((output_dir / name).exists() for name in expected_outputs):
        raise ValueError("V37_OUTPUT_FILE_EXISTS_REFUSE_OVERWRITE")
    sample_path = project_root / "docs/research/v25-price-action-sample-20261008.json"
    archive_directory = local_data_root / "reports/btc-eth-year-proxy-20261004"

    schema, schema_version, schema_record = load_frozen_a0_schema()
    inventory = build_source_inventory(project_root, local_data_root)
    a0_coverage = recover_v25_summary(sample_path)
    economic_coverage = economic_coverage_from_v25(sample_path)

    points = [
        build_reconstructed_point(
            archive_directory,
            symbol="BTCUSDT",
            decision_time=point_time,
            decision_id=decision_id,
            partition=partition,
        )
        for decision_id, point_time, partition in CALENDAR_ANCHORS
    ]
    decision_document = {
        "schema_version": "pa-decision-quality-v37/decision-points-1",
        "dataset_kind": "RECONSTRUCTED_MARKET_BENCHMARK",
        "forensic_dataset_included": False,
        "source_v25_sample_sha256": inventory["tracked_v25_sample"].get("sha256"),
        "sample_selection": {
            "rule": "Fixed 12:00 UTC calendar anchors within the three existing V25 recovery-pilot windows; assigned to partitions by chronological pilot window before any V37 review.",
            "outcome_based_selection": False,
            "v25_decision_id_link": False,
            "purpose": "Causal-input and offline replay engineering only; not a profitability sample.",
        },
        "cache_rows": [],
        "decision_points": [
            {
                "decision_id": point["decision_id"],
                "decision_time": point["decision_time"],
                "symbol": point["symbol"],
                "partition": point["partition"],
                "bars_by_timeframe": point["bars_by_timeframe"],
            }
            for point in points
        ],
    }
    input_path = output_dir / "decision-points.json"
    input_hash = _write_new_json(input_path, decision_document)

    market_manifest = {
        "schema_version": "pa-decision-quality-v37/market-reconstruction-manifest-1",
        "dataset_kind": "RECONSTRUCTED_MARKET_BENCHMARK",
        "source_manifest_sha256": points[0]["source_manifest_sha256"],
        "source_database_sha256": points[0]["source_database_sha256"],
        "price_evidence_grade": "VERIFIED_ARCHIVE_RECONSTRUCTION",
        "availability_evidence_grade": "ASSUMED_PROXY",
        "availability_basis": "ASSUMED_PROXY_60S_AFTER_BAR_END_NOT_OBSERVED",
        "availability_delay_seconds": 60,
        "not_gate_data": True,
        "not_v25_original_decisions": True,
        "strict_causality_checks": {
            "bar_end_strictly_before_decision": True,
            "available_at_strictly_before_decision": True,
            "all_swing_confirmation_times_strictly_before_decision": True,
            "high_timeframe_closed_bars_only": True,
            "source_sequence_complete_and_gap_free": True,
            "source_monthly_archive_hashes_verified": True,
        },
        "points": [
            {
                "decision_id": point["decision_id"],
                "decision_time": point["decision_time"],
                "symbol": point["symbol"],
                "partition": point["partition"],
                "evidence_input_sha256": point["evidence_input_sha256"],
                "v36_context_input_sha256": point["context_input_sha256"],
                "bar_count_by_timeframe": {
                    timeframe: len(bars)
                    for timeframe, bars in point["bars_by_timeframe"].items()
                },
                "source_archive_sha256s": sorted({
                    digest
                    for bars in point["bars_by_timeframe"].values()
                    for bar in bars
                    for digest in bar["source_file_hashes"]
                }),
            }
            for point in points
        ],
    }
    market_manifest["manifest_sha256"] = canonical_sha256(market_manifest)

    # The exact same pure offline entry point used by the V36 CLI is run twice;
    # the CLI below is then executed independently and byte-compared.
    direct_results = []
    for _ in range(2):
        result = run_study(
            decision_document["decision_points"],
            cache_rows=[],
            model_caller=None,
            model_id=None,
            run=False,
            max_decisions=None,
        )
        result["input_sha256"] = input_hash
        direct_results.append(result)
    if direct_results[0] != direct_results[1]:
        raise RuntimeError("V37_DIRECT_OFFLINE_REPLAY_NOT_DETERMINISTIC")
    reference_result = direct_results[0]
    reference_bytes = _json_bytes(reference_result)
    reference_hash = hashlib.sha256(reference_bytes).hexdigest()
    _write_new_json(output_dir / "offline-replay-reference.json", reference_result)

    run_cli_path = output_dir / "offline-replay.json"
    run_script = project_root / "scripts/run_pa_decision_quality_v36.py"
    _run_readonly_cli(
        run_script,
        ["--input", str(input_path), "--output", str(run_cli_path)],
        project_root=project_root,
    )
    cli_bytes = run_cli_path.read_bytes()
    cli_result = json.loads(cli_bytes.decode("utf-8"))
    cli_hash = hashlib.sha256(cli_bytes).hexdigest()
    if cli_result != reference_result or cli_bytes != reference_bytes:
        raise RuntimeError("V37_V36_CLI_OUTPUT_DIFFERS_FROM_REFERENCE")

    review_path = output_dir / "offline-review.json"
    review_script = project_root / "scripts/review_pa_decision_quality_v36.py"
    _run_readonly_cli(
        review_script,
        ["--decisions", str(run_cli_path), "--output", str(review_path)],
        project_root=project_root,
    )
    review_bytes = review_path.read_bytes()
    review_result = json.loads(review_bytes.decode("utf-8"))
    direct_review = summarize(cli_result["decision_records"])
    expected_review = {
        **direct_review,
        "source_decisions_sha256": cli_hash,
        "source_outcomes_sha256": None,
        "source_reviews_sha256": None,
    }
    if review_result != expected_review:
        raise RuntimeError("V37_V36_REVIEW_METRICS_NOT_CONSISTENT")

    arm_counts = _experiment_status_counts(cli_result["decision_records"])
    lifecycle_clear = all(
        record.get("lifecycle", {}).get("gateway_acceptance") == "NOT_OBSERVED"
        and record.get("lifecycle", {}).get("venue_fill") == "NOT_OBSERVED"
        and record.get("lifecycle", {}).get("complete_close") == "NOT_OBSERVED"
        for record in cli_result["decision_records"]
    )
    if not lifecycle_clear or cli_result["run_manifest"].get("model_calls_used") != 0:
        raise RuntimeError("V37_OFFLINE_BOUNDARY_VIOLATION")

    readiness = {
        "schema_version": "pa-decision-quality-v37/replay-readiness-1",
        "historical_forensic_dataset": {
            "status": "BLOCKED_EXACT_A0_UNRECOVERABLE",
            "exact_a0_recovered": a0_coverage["exact_a0_recovered"],
            "reconstructed_benchmark_may_be_relabelled_as_v25": False,
        },
        "reconstructed_market_benchmark": {
            "status": "READY_FOR_OFFLINE_ENGINEERING_REPLAY",
            "decision_point_count": len(points),
            "dataset_sha256": input_hash,
            "market_manifest_sha256": market_manifest["manifest_sha256"],
            "price_source": "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVES",
            "gate_market_input": False,
            "availability_is_assumption": True,
        },
        "offline_replay": {
            "mode": cli_result["run_manifest"]["mode"],
            "model_calls_used": cli_result["run_manifest"]["model_calls_used"],
            "model_id_role": cli_result["run_manifest"]["model_id_role"],
            "a0_a1_a2_a3_status_counts": arm_counts,
            "direct_repeat_results_equal": True,
            "v36_cli_matches_direct_replay_byte_for_byte": True,
            "offline_replay_output_sha256": cli_hash,
            "reference_output_sha256": reference_hash,
            "review_metrics_match_direct_summary": True,
            "review_output_sha256": hashlib.sha256(review_bytes).hexdigest(),
            "review_experiments": review_result.get("experiments", {}),
        },
        "execution_boundary": {
            "gemini_calls": 0,
            "orders_created": 0,
            "gateway_acceptance_observed": False,
            "venue_fills_observed": False,
            "complete_closes_observed_in_replay": False,
            "all_proposals_remain_research_only": lifecycle_clear,
        },
        "v38_entry_conditions": [
            "Obtain original V25 prompts, exact model inputs, point-in-time state snapshots, per-call effective schemas, response payloads, and verifiable actual model identity before claiming historical A0 comparison.",
            "Obtain point-in-time Gate candles/availability and historical economic snapshots before making Gate-specific or feasibility claims.",
            "Any new Gemini study must be separately approved, labeled as a new experiment, and must not overwrite historical evidence or activate production behavior.",
        ],
    }
    market_hash = _write_new_json(output_dir / "market-reconstruction-manifest.json", market_manifest)
    inventory_hash = _write_new_json(output_dir / "source-inventory.json", inventory)
    a0_hash = _write_new_json(output_dir / "a0-recovery-coverage.json", a0_coverage)
    economics_hash = _write_new_json(output_dir / "economic-evidence-coverage.json", economic_coverage)
    readiness_hash = _write_new_json(output_dir / "replay-readiness.json", readiness)
    schema_summary = {
        "schema_version": schema_version,
        "schema_sha256": schema_record["schema_sha256"],
        "source_commit": schema_record["source"]["commit"],
        "schema_key_count": len(schema),
    }
    return {
        "status": "V37_OFFLINE_RECONSTRUCTION_COMPLETE",
        "output_dir": str(output_dir),
        "schema": schema_summary,
        "report_sha256": {
            "source-inventory.json": inventory_hash,
            "a0-recovery-coverage.json": a0_hash,
            "economic-evidence-coverage.json": economics_hash,
            "market-reconstruction-manifest.json": market_hash,
            "decision-points.json": input_hash,
            "offline-replay.json": cli_hash,
            "offline-review.json": hashlib.sha256(review_bytes).hexdigest(),
            "replay-readiness.json": readiness_hash,
        },
        "model_calls_used": 0,
        "orders_created": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path,
                        default=Path(__file__).resolve().parents[1],
                        help="V37 checkout containing code and the tracked sanitized V25 sample")
    parser.add_argument("--local-data-root", type=Path, required=True,
                        help="Existing local checkout with reports/ and data/; sources are opened read-only")
    parser.add_argument("--output-dir", type=Path,
                        help="New local report directory; defaults to <local-data-root>/reports/v37")
    args = parser.parse_args(argv)
    output_dir = args.output_dir or (args.local_data_root / "reports/v37")
    try:
        result = build_reports(args.project_root, args.local_data_root, output_dir)
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError, KeyError) as exc:
        print(json.dumps({
            "status": "V37_RECONSTRUCTION_FAILED",
            "error_code": getattr(exc, "code", type(exc).__name__),
            "error_type": type(exc).__name__,
        }, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
