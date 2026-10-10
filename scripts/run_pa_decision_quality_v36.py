"""Run the V36 study in offline mode from frozen JSON inputs.

This CLI has no provider integration. A future opt-in model experiment must
inject a caller through the library API and provide a positive decision budget.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

from core.replay.pa_decision_quality_v36.runner import (
    StudyError,
    run_study,
    validate_model_call_gate,
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_new_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="Frozen JSON object with decision_points and optional cache_rows")
    parser.add_argument("--output", type=Path, required=True,
                        help="New report path; existing files are never overwritten")
    parser.add_argument("--run", action="store_true",
                        help="Explicitly enable an injected caller; this CLI has no built-in provider")
    parser.add_argument("--max-decisions", type=int,
                        help="Hard total model-call budget required with --run")
    parser.add_argument("--caller", help="Explicit import path in module:function form")
    parser.add_argument("--model-id", help="Model/version label recorded in the run manifest")
    args = parser.parse_args(argv)
    try:
        if args.input.resolve() == args.output.resolve():
            raise ValueError("INPUT_AND_OUTPUT_MUST_DIFFER")
        validate_model_call_gate(run=args.run, max_decisions=args.max_decisions)
        if not args.run and args.caller:
            raise StudyError("MODEL_CALLER_REQUIRES_RUN_FLAG")
        if args.run and not args.caller:
            raise StudyError("MODEL_CALLER_NOT_CONFIGURED")
        if args.run and not args.model_id:
            raise StudyError("MODEL_ID_REQUIRED_FOR_EXPLICIT_RUN")
        model_caller = None
        if args.caller:
            module_name, separator, function_name = args.caller.partition(":")
            if not separator or not module_name or not function_name:
                raise ValueError("CALLER_MUST_BE_MODULE_COLON_FUNCTION")
            candidate = getattr(importlib.import_module(module_name), function_name, None)
            if not callable(candidate):
                raise ValueError("MODEL_CALLER_NOT_CALLABLE")
            model_caller = candidate
        source_bytes = args.input.read_bytes()
        source = json.loads(source_bytes.decode("utf-8"))
        if not isinstance(source, dict) or not isinstance(source.get("decision_points"), list):
            raise TypeError("INPUT_REQUIRES_DECISION_POINTS_LIST")
        result = run_study(
            source["decision_points"],
            cache_rows=source.get("cache_rows"),
            model_caller=model_caller,
            model_id=args.model_id,
            run=args.run,
            max_decisions=args.max_decisions,
        )
        result["input_sha256"] = hashlib.sha256(source_bytes).hexdigest()
        _write_new_json(args.output, result)
    except FileExistsError:
        print(json.dumps({"status": "OUTPUT_ALREADY_EXISTS"}, ensure_ascii=False))
        return 2
    except (OSError, UnicodeError, ValueError, TypeError, ImportError, AttributeError) as exc:
        print(json.dumps({"status": "INVALID_OR_UNAVAILABLE_INPUT",
                          "error_code": getattr(exc, "code", type(exc).__name__),
                          "error_type": type(exc).__name__}, ensure_ascii=False))
        return 2
    print(json.dumps({
        "status": "WRITTEN", "output": str(args.output.resolve()),
        "model_calls_used": result["run_manifest"]["model_calls_used"],
        "mode": result["run_manifest"]["mode"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
