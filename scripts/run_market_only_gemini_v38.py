"""Explicit V38 Gemini research CLI; dry-run is the default and never calls a model."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.replay.pa_decision_quality_v38.gemini_runner import (
    AUTHORIZATION_ID,
    MAX_CALLS,
    MAX_CONTEXTS,
    MAX_OUTPUT_TOKENS,
    ProviderCallResult,
    execute_authorized_optimization,
    load_authorization,
    load_authorized_optimization_inputs,
    load_prompt_templates,
    review_gemini_ledger,
)
from core.replay.pa_decision_quality_v38.market_only import MarketOnlyError

DEFAULT_AUTHORIZATION = ROOT / "configs" / "research" / "authorizations" / "v38-gemini-optimization-run-20261010-v1.json"
LEDGER_RELATIVE_PATH = Path("reports") / "v38+" / "runs" / "v38-gemini-call-intent-ledger-v1.jsonl"


def _shared_worktree_ledger() -> Path:
    """Use one ignored ledger for every linked worktree of this local clone."""
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
        raise ValueError("V38_GEMINI_SHARED_LEDGER_GIT_COMMON_DIR_UNAVAILABLE") from exc
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (ROOT / common_dir).resolve()
    else:
        common_dir = common_dir.resolve()
    if common_dir.name != ".git":
        raise ValueError("V38_GEMINI_SHARED_LEDGER_GIT_COMMON_DIR_INVALID")
    return common_dir.parent / LEDGER_RELATIVE_PATH


def _reports_path(value: str | Path, *, must_not_exist: bool) -> Path:
    raw = Path(value)
    resolved = (ROOT / raw).resolve() if not raw.is_absolute() else raw.resolve()
    allowed_root = (ROOT / "reports" / "v38+").resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError("V38_GEMINI_OUTPUT_MUST_BE_UNDER_IGNORED_REPORTS") from exc
    if must_not_exist and resolved.exists():
        raise ValueError("V38_GEMINI_OUTPUT_EXISTS_REFUSE_OVERWRITE")
    return resolved


def _model_caller() -> tuple[Any, Any]:
    # Importing config/client is intentionally delayed until --execute. Dry-run
    # does not read a model key, contact a local health endpoint, or initialize a
    # provider adapter with an implicit completion probe.
    from core.config import config
    from core.model_client import model_client
    from core.model_routing import DEFAULT_MODEL

    if (config.model_name != DEFAULT_MODEL
            or model_client.model_name != DEFAULT_MODEL
            or model_client._configuration_error(DEFAULT_MODEL) is not None):
        raise ValueError("V38_GEMINI_MODEL_ROUTE_NOT_PINNED_TO_AUTHORIZED_LOOPBACK")
    if not isinstance(config.api_key, str) or not config.api_key.strip():
        raise ValueError("V38_GEMINI_PROXY_CREDENTIAL_UNAVAILABLE")

    def call(messages: list[dict[str, str]], request_id: str) -> ProviderCallResult:
        response = model_client.chat_completion(
            messages,
            mode="FAST",
            response_format={"type": "json_object"},
            stream=False,
            temperature_override=0.0,
            max_tokens=MAX_OUTPUT_TOKENS,
            model_name=DEFAULT_MODEL,
            reasoning_effort="high",
            timeout_sec=min(float(config.timeout_sec), 120.0),
            retries=0,
            request_id=request_id,
        )
        return ProviderCallResult(
            payload=response,
            response_model_id=model_client.last_response_model,
            transport_trace=model_client.last_transport_trace,
        )

    return model_client, call


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    except FileExistsError as exc:
        raise ValueError("V38_GEMINI_OUTPUT_EXISTS_REFUSE_OVERWRITE") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "V38 Gemini market-only research. Default is offline dry-run. "
            "--execute is required to use the separately recorded authorization."
        ),
    )
    parser.add_argument("--input", required=True, type=Path, help="Frozen optimization+validation visible-input JSON.")
    parser.add_argument("--manifest", required=True, type=Path, help="Frozen primary dataset-manifest.json.")
    parser.add_argument("--authorization", type=Path, default=DEFAULT_AUTHORIZATION)
    parser.add_argument("--ledger", type=Path,
                        help="Optional dry-run ledger path. Execution is pinned to the canonical shared linked-worktree ledger.")
    parser.add_argument("--max-contexts", type=int, default=1,
                        help="At most this many optimization contexts per invocation (1-36).")
    parser.add_argument("--output", type=Path,
                        help="New ignored report path; required with --execute and never overwritten.")
    parser.add_argument("--execute", action="store_true",
                        help="Send exactly one completion request per new selected context-arm; no retries or repairs.")
    args = parser.parse_args(argv)
    try:
        authorization = load_authorization(args.authorization)
        prompts = load_prompt_templates(ROOT)
        inputs = load_authorized_optimization_inputs(args.input, args.manifest)
        if not 1 <= args.max_contexts <= MAX_CONTEXTS:
            raise ValueError("GEMINI_RUN_CONTEXT_LIMIT_INVALID")
        # Validate every scheduled wire prompt before even creating a call intent.
        preflight = review_gemini_ledger(inputs, prompts, [])
        if args.execute and args.ledger is not None:
            raise ValueError("V38_GEMINI_EXECUTION_USES_CANONICAL_SHARED_LEDGER_ONLY")
        if args.ledger is None:
            ledger_path = _shared_worktree_ledger()
        else:
            ledger_path = _reports_path(args.ledger, must_not_exist=False)
        if not args.execute:
            print(json.dumps({
                "status": "DRY_RUN_READY_NO_MODEL_CALL",
                "authorization_id": authorization["authorization_id"],
                "model_id": authorization["model_id"],
                "dataset_id": preflight["dataset_id"],
                "dataset_manifest_sha256": preflight["dataset_manifest_sha256"],
                "partition": "optimization",
                "optimization_contexts_available": len(inputs),
                "maximum_authorized_contexts": MAX_CONTEXTS,
                "maximum_authorized_model_request_intents": MAX_CALLS,
                "requested_contexts_this_invocation": args.max_contexts,
                "planned_total_attempt_denominator": preflight["planned_attempt_denominator"],
                "model_calls_used": 0,
                "orders_created": 0,
                "validation_or_untouched_test_sent": False,
                "ledger_path": str(ledger_path),
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.output is None:
            raise ValueError("V38_GEMINI_EXECUTION_REQUIRES_NEW_REPORT_OUTPUT")
        if ledger_path != _shared_worktree_ledger():
            raise ValueError("V38_GEMINI_EXECUTION_REQUIRES_CANONICAL_SHARED_LEDGER")
        report_path = _reports_path(args.output, must_not_exist=True)
        _, call_model = _model_caller()
        report = execute_authorized_optimization(
            inputs, prompts, authorization=authorization, ledger_path=ledger_path,
            call_model=call_model, max_contexts=args.max_contexts,
        )
        _write_report(report_path, report)
    except (OSError, ValueError, MarketOnlyError) as exc:
        safe_message = str(exc)
        if not safe_message.isupper() or len(safe_message) > 128:
            safe_message = "V38_GEMINI_RUN_FAILED"
        print(safe_message, file=sys.stderr)
        return 2
    print(json.dumps({
        "status": "RESEARCH_RUN_RECORDED",
        "authorization_id": AUTHORIZATION_ID,
        "report": str(report_path),
        "report_sha256": report["report_sha256"],
        "planned_total_attempt_denominator": report["planned_attempt_denominator"],
        "dispatch_intent_count": report["dispatch_intent_count"],
        "terminal_result_count": report["terminal_result_count"],
        "valid_analysis_records": report["valid_analysis_records"],
        "provider_cost_usdt": None,
        "orders_created": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
