"""Prepare, never activate, separation of unvalidated PA research from production."""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BOOK = "core/trading/ai_strategy_book.py"
RUNNER = "core/replay/ai_template_runner.py"
PA = "price_action_structure"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replace_once(text: str, before: str, after: str) -> str:
    if text.count(before) != 1:
        raise ValueError("ISOLATION_SOURCE_ANCHOR_MISMATCH")
    return text.replace(before, after, 1)


def proposed_sources() -> dict[str, str]:
    book = (ROOT / BOOK).read_text(encoding="utf-8")
    runner = (ROOT / RUNNER).read_text(encoding="utf-8")
    book_anchor = '''    if _template_item["id"] == "price_action_structure":
        # Keep causal timestamp rules and future candidate capacity intact.
        _brief += "\\n研究优化种子：" + _PA_RESEARCH_SEED_INSTRUCTION
'''
    runner_import = "from core.trading.ai_strategy_book import TEMPLATES, _PA_RESEARCH_SEED_INSTRUCTION\n"
    runner_anchor = '''        if candidate_instructions is not None:
            # Research only: immutable style, cadence, ownership, margin and
'''
    runner_replacement = '''        if candidate_instructions is None and item["id"] == "price_action_structure":
            # Optimization only. A later candidate replaces this seed rather
            # than stacking two experimental instructions in held-out replay.
            strategy["sections"]["entry_standards"] += "\\n研究优化种子：" + _PA_RESEARCH_SEED_INSTRUCTION
        if candidate_instructions is not None:
            # Research only: immutable style, cadence, ownership, margin and
'''
    # After a reviewed activation, this preparation helper is a read-only
    # no-op. Reject partially applied or unknown forms instead of duplicating
    # the seed or accepting arbitrary source drift.
    if book_anchor not in book:
        if runner.count(runner_import) != 1 or runner.count(runner_replacement) != 1:
            raise ValueError("ISOLATION_SOURCE_ANCHOR_MISMATCH")
        if '研究优化种子：' in book:
            raise ValueError("ISOLATION_SOURCE_ANCHOR_MISMATCH")
        return {BOOK: book, RUNNER: runner}
    book = replace_once(book, book_anchor, "")
    runner = replace_once(runner, "from core.trading.ai_strategy_book import TEMPLATES\n", runner_import)
    runner = replace_once(runner, runner_anchor, runner_replacement)
    return {BOOK: book, RUNNER: runner}


def prepared_context(sources: dict[str, str]):
    """Evaluate only the strategy data and pure freezing function in memory."""
    from core.replay import ai_template_runner as original
    book_context = {"__name__": "core.trading._prepared_strategy_book",
                    "__package__": "core.trading", "__file__": str(ROOT / BOOK)}
    exec(compile(sources[BOOK], str(ROOT / BOOK), "exec"), book_context)
    tree = ast.parse(sources[RUNNER])
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "frozen_templates")
    scope = dict(vars(original))
    scope["TEMPLATES"] = book_context["TEMPLATES"]
    scope["_PA_RESEARCH_SEED_INSTRUCTION"] = book_context["_PA_RESEARCH_SEED_INSTRUCTION"]
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<prepared-frozen-templates>", "exec"), scope)
    return book_context, scope["frozen_templates"]


def prepare(directory: Path) -> dict:
    from core.replay.ai_template_runner import frozen_templates
    directory = directory.resolve()
    registration = json.loads((directory / "research-plan.json").read_text(encoding="utf-8"))
    if not isinstance(registration.get("plan_sha256"), str) or not registration["plan"].get("research_only"):
        raise ValueError("RESEARCH_REGISTRATION_REQUIRED")
    sources = proposed_sources()
    before = {path: (ROOT / path).read_text(encoding="utf-8") for path in sources}
    book, freeze = prepared_context(sources)
    seed = book["_PA_RESEARCH_SEED_INSTRUCTION"]
    optimization = freeze(template_ids=[PA])
    if optimization != frozen_templates(template_ids=[PA]):
        raise ValueError("OPTIMIZATION_TEMPLATE_SEMANTICS_CHANGED")
    production = next(item for item in book["TEMPLATES"] if item["id"] == PA)
    candidate = "独立验证候选：按已收盘价格结构自主判断，保留全部交易约束。"
    heldout = freeze({PA: candidate}, [PA])[0]
    if seed in json.dumps(production, ensure_ascii=False) or seed in json.dumps(heldout, ensure_ascii=False):
        raise ValueError("EXPERIMENTAL_SEED_LEAK")
    if not heldout["sections"]["custom_prompt"].endswith(candidate):
        raise ValueError("HELDOUT_CANDIDATE_NOT_PRESERVED")
    patch = "".join("".join(difflib.unified_diff(before[path].splitlines(True), source.splitlines(True),
                        fromfile="a/" + path, tofile="b/" + path)) for path, source in sources.items())
    receipt = {
        "status": "PREPARED_NOT_APPLIED", "plan_sha256": registration.get("plan_sha256"),
        "source_before_sha256": {path: sha(text) for path, text in before.items()},
        "proposed_source_sha256": {path: sha(text) for path, text in sources.items()},
        "patch_sha256": sha(patch), "optimization_template_semantics_preserved": True,
        "production_seed_removed_in_memory": True, "heldout_seed_stack_removed_in_memory": True,
        "existing_trade_constraints_changed": False, "model_calls": 0, "exchange_calls": 0,
        "activation_requires": ["No living research invocation", "Preserve original version audits and source",
                                "New source freeze and experiment; do not relabel old results"],
        "not_evidence_of": ["Deployment", "Actual model decisions", "Filled orders", "Profitability"]}
    if any((ROOT / path).read_text(encoding="utf-8") != text for path, text in before.items()):
        raise ValueError("LIVE_SOURCE_CHANGED_DURING_PREPARATION")
    (directory / "next-freeze-production-research-isolation.patch").write_text(patch, encoding="utf-8", newline="")
    (directory / "next-freeze-production-research-isolation.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.directory), ensure_ascii=False))
