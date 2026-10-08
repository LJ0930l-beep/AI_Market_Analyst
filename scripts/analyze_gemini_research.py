"""Generate candidate strategy instructions from completed optimization replay.

No production writes. Proposed edits are unvalidated until the validation and
untouched-test partitions have been evaluated. Never call this model on test
partition outcomes to tune the same experiment.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.ai.ollama import OllamaProvider
from core.model_routing import DEFAULT_MODEL, is_verified_model_receipt
from core.trading.model_schemas import validate_schema
from core.trading.ai_session_coordinator import _estimate_tokens
from core.replay.ai_history import digest
from scripts.verify_ai_template_replay import audit_replay

VERSION = "gemini_optimization_review_v1"
STATS = {"template_id", "name", "decision_count", "action_counts", "execution_counts", "roi", "fees",
         "funding_pnl", "realized_gross_pnl", "unrealized_pnl", "closed_trade_count", "win_rate", "profit_factor",
         "profit_factor_status", "max_drawdown", "filled_entry_order_count", "accepted_entry_order_count",
         "pending_order_count", "economic_eligible", "halted_reason"}


def optimization_evidence(directory):
    registration = json.loads((directory / "research-plan.json").read_text(encoding="utf-8"))
    plan = registration["plan"]
    if registration["plan_sha256"] != digest(plan):
        raise ValueError("OPTIMIZATION_PLAN_HASH_INVALID")
    windows = []
    for window in plan["pilot_windows"]:
        if window["partition"] != "optimization":
            raise ValueError("OPTIMIZER_REFUSES_VALIDATION_OR_TEST_RESULTS")
        target = directory / window["id"]
        report = json.loads((target / "results.json").read_text(encoding="utf-8"))
        audit = json.loads((target / "ledger-audit.json").read_text(encoding="utf-8"))
        if (report["status"] != "COMPLETED" or audit["status"] != "PASS"
                or report["decision_source"] != "PRODUCTION_GEMINI"):
            raise ValueError("OPTIMIZATION_COMPLETE_AUDITED_WINDOWS_REQUIRED")
        audit_replay(target / "results.sqlite3", target / "history.json",
                     require_complete=report["comparison_eligible"])
        with sqlite3.connect((target / "results.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
            reasons = {}
            for template_id, raw in db.execute("SELECT template_id,decision_json FROM ai_template_replay_decisions WHERE status='COMPLETED'"):
                decision = json.loads(raw)
                if decision.get("action") == "WAIT":
                    reasons.setdefault(template_id, Counter())[str(decision.get("reason") or "")[:500]] += 1
        windows.append({"window": window["id"], "comparison_eligible": report["comparison_eligible"],
                        "errors": report["errors"][:12],
                        "strategies": [{**{k: value for k, value in s.items() if k in STATS},
                                        "wait_reason_examples": reasons.get(s["template_id"], Counter()).most_common(3)}
                                       for s in report["results"]]})
    return {"plan_sha256": registration["plan_sha256"], "model_id": DEFAULT_MODEL,
            "scope": "OPTIMIZATION_PILOT_TECHNICAL_ONLY_NOT_ANNUAL_OR_GATE_RETURN", "windows": windows,
            "styles": [{"template_id": t["template_id"], "name": t["name"], "profile": t["profile"]} for t in plan["templates"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    evidence = optimization_evidence(directory)
    from core.replay.relay_policy import read_relay_policy
    registration = json.loads((directory / "research-plan.json").read_text(encoding="utf-8"))["plan"]
    expected_policy = registration.get("relay_inference_policy")
    if expected_policy != read_relay_policy():
        raise ValueError("OPTIMIZATION_REVIEW_RELAY_POLICY_MUST_MATCH")
    ids = [t["template_id"] for t in evidence["styles"]]
    schema = {"type": "object", "additionalProperties": False,
              "required": ["status", "proposals"], "properties": {
        "status": {"type": "string", "enum": ["CANDIDATE_ONLY_UNVALIDATED"]},
        "proposals": {"type": "array", "minItems": len(ids), "maxItems": len(ids), "items": {
            "type": "object", "additionalProperties": False,
            "required": ["template_id", "finding", "candidate_instruction", "validation_checks"], "properties": {
                "template_id": {"type": "string", "enum": ids}, "finding": {"type": "string"},
                "candidate_instruction": {"type": "string"},
                "validation_checks": {"type": "array", "items": {"type": "string"}}}}}}}
    messages = [{"role": "system", "content":
        "你是策略实验审查员，使用中文、只输出给定JSON。只分析优化区证据，不能声称云模型已训练。"
        "每套策略提出一条候选指令和验证方法，保持原交易风格、扫描周期、保证金和保护单约束。"
        "不强制开仓，不编造胜率，不因等待多而取消证据核验。扣费收益、回撤、样本量和错误均需考虑。"
        "无平仓样本必须明确收益尚无法评估，提出可核验诊断；这些日历片段不是年度收益，"
        "Binance行情与Gate规则是成交代理。候选只有在独立验证和最终测试通过后才可启用。"
        "注册的template_id各出现一次，每个finding和candidate_instruction不超过180字。"},
        {"role": "user", "content": json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}]
    input_hash = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if sum(_estimate_tokens(m["content"]) for m in messages) + 2048 + 256 > 8192:
        raise ValueError("OPTIMIZATION_REVIEW_INPUT_BUDGET_EXCEEDED")
    output, _, receipt = OllamaProvider(max_tokens=2048, retries=0).generate_json(
        messages, model_name=DEFAULT_MODEL, prompt_version=VERSION, input_hash=input_hash,
        schema=schema, max_tokens=2048, reasoning_effort="high", allow_syntax_repair=False)
    validate_schema(output, schema)
    if expected_policy != read_relay_policy():
        raise ValueError("OPTIMIZATION_REVIEW_RELAY_POLICY_CHANGED")
    if sorted(p["template_id"] for p in output["proposals"]) != sorted(ids) or not is_verified_model_receipt(
            receipt, expected_prompt_version=VERSION, expected_input_hash=input_hash):
        raise ValueError("OPTIMIZATION_REVIEW_RECEIPT_OR_STRATEGY_SET_INVALID")
    result = {"review": output, "receipt": receipt, "plan_sha256": evidence["plan_sha256"],
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "request_messages": messages,
              "production_strategy_writes": 0, "requires_validation": True, "model_weights_trained": False}
    (directory / "strategy-candidates.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": output["status"], "candidate_count": len(ids), "requires_validation": True}), flush=True)

if __name__ == "__main__":
    raise SystemExit(main())
