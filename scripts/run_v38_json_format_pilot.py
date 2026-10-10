"""Run one strictly bounded Gemini JSON-envelope parseability pilot.

Dry-run is the default. The pilot is excluded from model-quality denominators.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.format_pilot import (
    _write_report_exclusive,
    run_format_pilot,
    validate_format_pilot_authorization,
)
from core.replay.pa_decision_quality_v38.gemini_runner import (
    build_model_messages,
    load_authorized_optimization_inputs,
)

DEFAULT_INPUT = ROOT / "reports" / "v38+" / "datasets" / "v3-stratified-purged-rebuild-20261010" / "optimization-validation-inputs.json"
DEFAULT_MANIFEST = ROOT / "reports" / "v38+" / "datasets" / "v3-stratified-purged-rebuild-20261010" / "dataset-manifest.json"
DEFAULT_AUTHORIZATION = ROOT / "configs" / "research" / "authorizations" / "v38-gemini-json-format-pilot-20261010-v1.json"
DEFAULT_PROMPT = ROOT / "configs" / "research" / "prompts" / "v38-market-only-a1-json-format-pilot-v1.json"
DEFAULT_OUTPUT = ROOT / "reports" / "v38+" / "runs" / "v38-gemini-json-format-pilot-20261010-v1.json"
DEFAULT_LEDGER_RELATIVE = Path("reports") / "v38+" / "runs" / "v38-gemini-json-format-pilot-ledger-20261010-v1.jsonl"


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(code) from exc


def _root_path(value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (ROOT / value).resolve()


def _shared_repository_root() -> Path:
    """Resolve the common checkout root so linked worktrees share the cap ledger."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("FORMAT_PILOT_SHARED_LEDGER_GIT_COMMON_DIR_UNAVAILABLE") from exc
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (ROOT / common_dir).resolve()
    else:
        common_dir = common_dir.resolve()
    if common_dir.name != ".git":
        raise ValueError("FORMAT_PILOT_SHARED_LEDGER_GIT_COMMON_DIR_INVALID")
    return common_dir.parent


def load_pilot_materials(input_path: Path, manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    auth = _read_json(DEFAULT_AUTHORIZATION, "FORMAT_PILOT_AUTHORIZATION_UNAVAILABLE")
    try:
        prompt_bytes = DEFAULT_PROMPT.read_bytes()
        prompt = json.loads(prompt_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("FORMAT_PILOT_PROMPT_UNAVAILABLE") from exc
    inputs = load_authorized_optimization_inputs(input_path, manifest_path)
    if not inputs:
        raise ValueError("FORMAT_PILOT_OPTIMIZATION_CONTEXT_MISSING")
    market_input = inputs[0]
    validate_format_pilot_authorization(
        auth, prompt, prompt_bytes=prompt_bytes, market_input=market_input,
    )
    messages = build_model_messages(market_input, prompt)
    return market_input, prompt, messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run", action="store_true", help="Send the single explicitly authorized completion.")
    args = parser.parse_args(argv)

    try:
        market_input, prompt_data, messages = load_pilot_materials(
            _root_path(args.input), _root_path(args.manifest),
        )
        shared_root = _shared_repository_root()
        ledger_path = shared_root / DEFAULT_LEDGER_RELATIVE
        output_path = _root_path(args.output)

        # This no-network pass rejects existing output files and unsafe paths
        # before an optional local model-catalog GET or completion dispatch.
        report = run_format_pilot(
            market_input=market_input,
            prompt=prompt_data,
            messages=messages,
            ledger_path=ledger_path,
            report_path=output_path,
            repository_root=ROOT,
            execute=False,
            ledger_root=shared_root,
        )
        if args.run:
            from scripts.run_market_only_gemini_v38 import (
                _model_caller,
                _provider_route_reauthorization_evidence,
            )

            model_client, call_model = _model_caller()
            route_evidence = _provider_route_reauthorization_evidence(client=model_client)
            report = run_format_pilot(
                market_input=market_input,
                prompt=prompt_data,
                messages=messages,
                ledger_path=ledger_path,
                report_path=output_path,
                repository_root=ROOT,
                execute=True,
                call_model=call_model,
                route_readiness=route_evidence,
                ledger_root=shared_root,
            )
            _write_report_exclusive(output_path, report, repository_root=ROOT)
        summary = {
            "status": report["status"],
            "pilot_kind": report["pilot_kind"],
            "authorization_id": report["authorization_id"],
            "dispatch_intent_count": report["dispatch_intent_count"],
            "terminal_result_count": report["terminal_result_count"],
            "quality_sample_eligible": report["quality_sample_eligible"],
            "orders_created": report["orders_created"],
            "report_path": str(output_path) if args.run else None,
        }
        print(json.dumps(summary, sort_keys=True))
        return 0 if report["status"] in {"DRY_RUN", "FORMAT_PILOT_ANALYSIS_VALID"} else 2
    except (OSError, ValueError) as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str):
            message = str(exc)
            code = message if message.startswith(("FORMAT_PILOT_", "V38_")) else "FORMAT_PILOT_FAILED"
        print(json.dumps({"status": "BLOCKED_WITH_EVIDENCE", "error_code": code}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
