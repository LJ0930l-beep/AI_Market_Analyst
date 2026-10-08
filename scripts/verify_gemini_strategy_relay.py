"""Read-only real relay verification using explicit non-market fixtures.

No account database, exchange client or execution gateway is instantiated.
This validates structured inference, never profitability or real trade fills.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.ai.ollama import OllamaProvider
from core.model_routing import DEFAULT_MODEL, is_verified_model_receipt
from core.trading.ai_strategy_book import TEMPLATES, STRATEGY_PROFILE_VERSION
from core.trading.autonomous_strategy import build_strategy_system_prompt
from core.trading.ai_session_coordinator import _compact_gate_system_prompt, AI_PROMPT_VERSION
from core.trading.model_schemas import frozen_decision_schema, validate_schema


def main() -> int:
    provider = OllamaProvider()
    results = []
    for template in TEMPLATES:
        instructions = {
            "name": template["name"], "template_id": template["id"],
            "profile": deepcopy(template["profile"]), "sections": deepcopy(template["sections"]),
            "execution": deepcopy(template["execution_defaults"]),
        }
        payload = {
            "test_scope": "Read-only no-market integration fixture, not real market data. No exchange operations.",
            "account_id": "gate_testnet", "mode": "TESTNET", "allowed_instruments": ["BTCUSDT"],
            "evidence_refs": [], "market_snapshots": {}, "technical_context": {}, "news_revisions": [],
            "account_truth": {"status": "READY", "equity": 1000, "available_margin": 1000,
                              "positions": [], "open_orders": []},
        }
        schema = frozen_decision_schema(payload, gate_wait=True)
        messages = [
            {"role": "system", "content": _compact_gate_system_prompt(
                build_strategy_system_prompt(instructions, nofx_gate=True), instructions)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        started = time.perf_counter()
        row = {"template_id": template["id"], "passed": False}
        try:
            output, _, receipt = provider.generate_json(
                messages, model_name=DEFAULT_MODEL, prompt_version=AI_PROMPT_VERSION,
                input_hash=digest, schema=schema, max_tokens=2048, reasoning_effort="high",
                deadline_monotonic=time.monotonic() + 30, allow_syntax_repair=False,
            )
            row["output"] = output
            validate_schema(output, schema)
            verified = is_verified_model_receipt(receipt, expected_prompt_version=AI_PROMPT_VERSION,
                                                 expected_input_hash=digest)
            row.update(actual_model_id=receipt["actual_model_id"], receipt_verified=verified,
                       schema_enforcement=receipt["schema_enforcement"], input_hash=digest,
                       prompt_version=AI_PROMPT_VERSION,
                       passed=verified and output["action"] == "WAIT" and output.get("next_trigger_price") is None
                       and output.get("strategy_analysis", {}).get("next_trigger_price") is None)
        except Exception as exc:
            row["error"] = str(exc)[:320]
        row["latency_ms"] = round((time.perf_counter() - started) * 1000)
        results.append(row)
        print(json.dumps(row, ensure_ascii=True), flush=True)
    path = Path("reports/gemini-antigravity-connection-20261005/strategy-integration.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "scope": "Real High model calls using explicit missing-market fixtures; no exchange operations or profit validation.",
        "profile_version": STRATEGY_PROFILE_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(), "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
