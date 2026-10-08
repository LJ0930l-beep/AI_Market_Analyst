"""Prepare instance-scoped cached health metadata; never edit a running source."""
import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TARGET = "core/ai/ollama.py"
OLD = "                return dict(cached[1])\n"
NEW = '''                from copy import deepcopy
                result = deepcopy(cached[1])
                # The completion identity is shared; application input budgets
                # and rejected overrides belong to the current adapter instance.
                result.update(context_length=self.context_length,
                              configured_context_length=self.context_length,
                              context_length_source="APPLICATION_INPUT_BUDGET",
                              rejected_legacy_overrides=list(self.rejected_legacy_overrides))
                return result
'''


def proposed_source():
    original = (ROOT / TARGET).read_text(encoding="utf-8")
    if original.count(OLD) != 1:
        raise ValueError("MODEL_HEALTH_BUDGET_PATCH_ANCHOR_CHANGED")
    return original, original.replace(OLD, NEW, 1)


def prepared_health():
    import core.ai.ollama as original_module
    tree = ast.parse(proposed_source()[1])
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "OllamaProvider")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "health")
    namespace = dict(vars(original_module))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
        "<prepared-instance-health-budget>", "exec"), namespace)
    return namespace["health"]


def prepare(directory):
    directory = Path(directory).resolve()
    original, changed = proposed_source()
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), changed.splitlines(keepends=True),
        fromfile="a/" + TARGET, tofile="b/" + TARGET))
    receipt = {"status": "PREPARED_NOT_APPLIED", "target": TARGET,
        "original_sha256": hashlib.sha256((ROOT / TARGET).read_bytes()).hexdigest(),
        "prepared_text_sha256": hashlib.sha256(changed.encode()).hexdigest(),
        "scope": "INSTANCE_APPLICATION_BUDGET_AND_CACHE_COPY_NOT_NATIVE_CONTEXT_PROOF",
        "model_identity_policy_changed": False, "cache_ttl_changed": False,
        "model_calls": 0, "live_orders": 0, "source_applied": False,
        "activation_requirement": "CURRENT_JOB_TERMINAL_AND_PRECHANGE_AUDIT_PRESERVED_NEW_FREEZE_REQUIRED"}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "next-freeze-health-budget.patch").write_text(patch, encoding="utf-8")
    (directory / "next-freeze-health-budget.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    print(json.dumps(prepare(parser.parse_args().directory), indent=2))
