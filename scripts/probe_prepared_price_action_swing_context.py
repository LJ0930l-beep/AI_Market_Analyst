"""Causal archived bars + actual requests, in-memory patch; no model/order call."""
import argparse
import ast
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.replay.gemini_research as research
from core.replay.ai_history import ReplayHistory, digest
from core.replay.ai_template_runner import frozen_source_fingerprint, frozen_templates
from core.trading.ai_session_coordinator import _fit_prompt_payload, _estimate_tokens
from scripts.prepare_price_action_swing_context_patch import FIELD, proposed_namespace, proposed_source
from scripts.verify_ai_template_replay import audit_effective_request


def prepared_research_builder():
    """Reuse unchanged archive validator; substitute only the prepared builder."""
    tree = ast.parse(Path(research.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "research_price_action")
    fn.body = [n for n in fn.body if not isinstance(n, ast.ImportFrom)]
    namespace = dict(vars(research))
    namespace["_build_price_action_structure"] = proposed_namespace()["_build_price_action_structure"]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
        "<prepared-archive-swing-context>", "exec"), namespace)
    return namespace["research_price_action"]


def validate_capacity_chars(chars):
    if isinstance(chars, bool) or not isinstance(chars, int) or not 0 <= chars <= 500:
        raise ValueError("PA_CAPACITY_FIXTURE_CHAR_LIMIT_INVALID")


def capacity_fixture_system(system, chars):
    """Test reserved space, never invent or activate a strategy candidate."""
    validate_capacity_chars(chars)
    if not chars:
        return system, None
    base = frozen_templates(template_ids=("price_action_structure",))[0]["sections"]["custom_prompt"]
    if system.count(base) != 1:
        raise ValueError("PA_CAPACITY_FIXTURE_BASE_INSTRUCTION_NOT_UNIQUE")
    fixture = "容" * chars
    candidate = frozen_templates({"price_action_structure": fixture}, ("price_action_structure",))[0]
    custom = candidate["sections"]["custom_prompt"]
    if len(custom) > 900:
        raise ValueError("PA_CAPACITY_FIXTURE_PRODUCTION_SECTION_WOULD_TRUNCATE")
    return system.replace(base, custom, 1), {
        "chars": chars, "fixture_sha256": digest(fixture),
        "scope": "CHINESE_TEXT_CAPACITY_FIXTURE_NOT_AI_AUTHORED_CANDIDATE",
        "candidate_enabled": False,
    }


def probe(directory, candidate_capacity_chars=0, prepared_input_budget=0):
    # Validate before touching archives, including for empty experiments.
    validate_capacity_chars(candidate_capacity_chars)
    if type(prepared_input_budget) is not int or prepared_input_budget not in (0, 8192, 12288, 16384, 32768):
        raise ValueError("PA_CAPACITY_PREPARED_INPUT_BUDGET_INVALID")
    directory = Path(directory).resolve()
    registration = json.loads((directory / "research-plan.json").read_text(encoding="utf-8"))
    plan = registration["plan"]
    before = frozen_source_fingerprint()
    if digest(plan) != registration["plan_sha256"] or plan["source_sha256"] != before:
        raise ValueError("PA_SWING_PROBE_PLAN_OR_SOURCE_INVALID")
    if any(w["partition"] != "optimization" for w in plan["pilot_windows"]):
        raise ValueError("PA_SWING_PROBE_OPTIMIZATION_ONLY")
    build = prepared_research_builder()
    checks, problems = [], []
    for window in plan["pilot_windows"]:
        target = (directory / window["id"]).resolve()
        if target.parent != directory:
            raise ValueError("PA_SWING_PROBE_PATH_OUTSIDE_EXPERIMENT")
        database = target / "results.sqlite3"
        if not database.exists():
            continue
        history = ReplayHistory(target / "history.json")
        if history.payload.get("research_plan_sha256") != registration["plan_sha256"]:
            raise ValueError("PA_SWING_PROBE_HISTORY_PLAN_INVALID")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            def load(bundle_id):
                row = db.execute("SELECT payload_json FROM evidence_bundles WHERE bundle_id=?", (bundle_id,)).fetchone()
                if row is None:
                    raise ValueError("PA_SWING_PROBE_EVIDENCE_MISSING")
                return json.loads(row[0])
            for row in db.execute("SELECT * FROM ai_template_replay_decisions WHERE status='COMPLETED' ORDER BY rowid").fetchall():
                ctx = json.loads(row["context_json"])
                request = audit_effective_request(ctx, load(ctx["evidence_bundle_id"]), load)
                system, capacity = capacity_fixture_system(request["messages"][0]["content"], candidate_capacity_chars)
                wrapper = json.loads(request["messages"][1]["content"])
                repair = "previous_decision" in wrapper
                inputs = wrapper["inputs"] if repair else wrapper
                proposed = deepcopy(inputs)
                history.set_as_of(row["as_of"])
                prior_prices, sizes = {}, {}
                for symbol in inputs["allowed_instruments"]:
                    for frame in ("15m", "1h"):
                        bars = history.latest_bars(symbol, frame, limit=240)
                        summary = build(bars, frame, row["as_of"])
                        baseline = research.research_price_action(bars, frame, row["as_of"])
                        key = symbol + ":" + frame
                        if summary["status"] != "READY" or baseline["status"] != "READY":
                            problems.append({"window": window["id"], "as_of": row["as_of"],
                                "frame": key, "baseline_status": baseline["status"],
                                "proposed_status": summary["status"], "reason": summary.get("reason")})
                            continue
                        # Every prior event/state/time stays unchanged in the raw builder.
                        for event in ("bos", "sweep_reclaim", "breakout_retest", "prior_range", "confirmed_swings"):
                            if baseline[event] != summary[event]:
                                raise ValueError("PA_SWING_PROBE_EXISTING_EVENT_CHANGED")
                        prior_prices[key] = summary[FIELD]
                        # Research adds a scope label after the shared 1400-char check.
                        sizes[key] = len(json.dumps({k: v for k, v in summary.items()
                            if k != "source_scope"}, separators=(",", ":")))
                        proposed["technical_context"][symbol]["timeframes"][frame]["price_action"][FIELD] = deepcopy(summary[FIELD])
                settings = ctx["model_inference_settings"]
                input_budget = prepared_input_budget or settings["context_length"]
                extra = 0
                if repair:
                    empty_wrapper = {**wrapper, "inputs": {}}
                    extra = _estimate_tokens(json.dumps(empty_wrapper, sort_keys=True, ensure_ascii=False, separators=(",", ":"))) + 32
                try:
                    projected, budget = _fit_prompt_payload(proposed, system,
                        context_length=input_budget - extra,
                        reserve=settings["output_token_reserve"], required_symbols=tuple(inputs["allowed_instruments"]),
                        allow_symbol_deferral=False)
                except ValueError as error:
                    problems.append({"window": window["id"], "as_of": row["as_of"], "reason": str(error)})
                    continue
                if projected["account_truth"] != inputs["account_truth"] or projected["allowed_instruments"] != inputs["allowed_instruments"]:
                    raise ValueError("PA_SWING_PROBE_ACCOUNT_OR_SYMBOL_CHANGED")
                for symbol in inputs["allowed_instruments"]:
                    if projected["market_snapshots"][symbol]["price"] != inputs["market_snapshots"][symbol]["price"]:
                        raise ValueError("PA_SWING_PROBE_QUOTE_CHANGED")
                    for frame in ("15m", "1h"):
                        key = symbol + ":" + frame
                        if key in prior_prices and projected["technical_context"][symbol]["timeframes"][frame]["price_action"].get(FIELD) != prior_prices[key]:
                            raise ValueError("PA_SWING_PROBE_PRIOR_PRICES_DROPPED")
                final = {**wrapper, "inputs": projected} if repair else projected
                total = (_estimate_tokens(system)
                    + _estimate_tokens(json.dumps(final, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
                    + settings["output_token_reserve"] + 256)
                if total > input_budget:
                    raise ValueError("PA_SWING_PROBE_FINAL_WRAPPER_OVER_BUDGET")
                checks.append({"window": window["id"], "as_of": row["as_of"],
                    "original_request_hash": request["request_hash"], "prepared_payload_sha256": digest(final),
                    "prepared_system_sha256": digest(system), "candidate_capacity_fixture": capacity,
                    "repair": repair, "prior_prices": prior_prices, "summary_sizes": sizes, "estimated_total_tokens": total,
                    "context_length": input_budget, "original_context_length": settings["context_length"],
                    "budget_steps": budget["steps"],
                    "account_symbols_quotes_preserved": True,
                    "maximum_prior_prices_per_frame": 2})
    if frozen_source_fingerprint() != before:
        raise ValueError("PA_SWING_PROBE_SOURCE_CHANGED")
    report = {"status": "PASS_PREPARED_ARCHIVED_INPUT_PROBE" if checks and not problems else "PREPARED_PROBE_NEEDS_REVIEW",
        "observed_at": datetime.now(timezone.utc).isoformat(), "checks": checks, "problems": problems,
        "plan_sha256": registration["plan_sha256"], "frozen_source_sha256": before,
        "proposed_source_text_sha256": hashlib.sha256(proposed_source()[1].encode()).hexdigest(),
        "probe_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "candidate_capacity_chars": candidate_capacity_chars,
        "prepared_application_input_budget": prepared_input_budget or None,
        "application_budget_applied": False, "native_context_limit_verified": False,
        "scope": "PAST_OPTIMIZATION_ARCHIVE_IN_MEMORY_INPUT_ONLY_NOT_MODEL_OR_PROFIT_PROOF",
        "source_applied": False, "model_calls": 0, "private_exchange_calls": 0, "live_orders": 0}
    name = (f"prepared-swing-capacity-{candidate_capacity_chars}-budget-{prepared_input_budget}-probe.json"
        if prepared_input_budget else f"prepared-swing-capacity-{candidate_capacity_chars}-probe.json"
        if candidate_capacity_chars else "prepared-swing-context-probe.json")
    (directory / name).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return {"status": report["status"], "archived_final_requests_checked": len(checks),
        "problems": problems[:4], "problem_count": len(problems), "model_calls": 0,
        "candidate_capacity_chars": candidate_capacity_chars, "artifact": str(directory / name),
        "prepared_application_input_budget": prepared_input_budget or None,
        "max_estimated_total_tokens": max((c["estimated_total_tokens"] for c in checks), default=0),
        "max_summary_chars": max((s for c in checks for s in c["summary_sizes"].values()), default=0)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--candidate-capacity-chars", type=int, default=0,
        help="Offline Chinese text capacity fixture, not a real strategy candidate (0..500).")
    parser.add_argument("--prepared-input-budget", type=int, default=0,
        help="Offline prospective application budget, never changes model/relay/live config.")
    args = parser.parse_args()
    print(json.dumps(probe(args.directory, args.candidate_capacity_chars, args.prepared_input_budget), indent=2))
