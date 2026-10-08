"""Prepare the canonical application budget lookup without touching live sources."""
import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TARGET = "core/trading/ai_session_coordinator.py"
OLD = 'def _session_context_length() -> int:\n    raw = os.environ.get("OLLAMA_CONTEXT_LENGTH")\n'
NEW = '''def _session_context_length() -> int:
    # The canonical Gemini application budget must also govern real scans.
    # Preserve legacy lookup only when the canonical option is absent.
    configured = os.environ.get("AIMA_MODEL_INPUT_BUDGET")
    if configured is not None:
        try:
            value = int(configured)
        except (TypeError, ValueError) as error:
            raise ValueError("AI_APPLICATION_INPUT_BUDGET_INVALID") from error
        if value <= 0:
            raise ValueError("AI_APPLICATION_INPUT_BUDGET_INVALID")
        return value
    raw = os.environ.get("OLLAMA_CONTEXT_LENGTH")
'''


def proposed_source():
    original = (ROOT / TARGET).read_text(encoding="utf-8")
    if original.count(OLD) != 1:
        raise ValueError("SESSION_INPUT_BUDGET_PATCH_ANCHOR_CHANGED")
    return original, original.replace(OLD, NEW, 1)


def prepared_lookup():
    import core.trading.ai_session_coordinator as module
    tree = ast.parse(proposed_source()[1])
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_session_context_length")
    namespace = dict(vars(module))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
        "<prepared-canonical-session-budget>", "exec"), namespace)
    return namespace["_session_context_length"]


def prepare(directory):
    directory = Path(directory).resolve()
    original, changed = proposed_source()
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), changed.splitlines(keepends=True),
        fromfile="a/" + TARGET, tofile="b/" + TARGET))
    receipt = {"status": "PREPARED_NOT_APPLIED", "target": TARGET,
        "original_sha256": hashlib.sha256((ROOT / TARGET).read_bytes()).hexdigest(),
        "prepared_text_sha256": hashlib.sha256(changed.encode()).hexdigest(),
        "scope": "CANONICAL_APPLICATION_BUDGET_LOOKUP_NOT_NATIVE_MODEL_CAPACITY_OR_PROFIT",
        "next_freeze_prospective_application_budget": 12288,
        "default_budget_changed": False, "environment_written": False,
        "relay_thinking_output_retry_policy_changed": False,
        "source_applied": False, "model_calls": 0, "live_orders": 0,
        "activation_requirement": "CURRENT_JOB_TERMINAL_PRECHANGE_AUDIT_SAVED_NEW_FREEZE_AND_REAL_PROTOCOL_CHECK_REQUIRED"}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "next-freeze-session-input-budget.patch").write_text(patch, encoding="utf-8")
    (directory / "next-freeze-session-input-budget.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    print(json.dumps(prepare(parser.parse_args().directory), indent=2))
