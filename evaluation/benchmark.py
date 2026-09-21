"""Automated 50-case benchmark suite for AI Market Analyst V2 (Bonsai 2 27B)."""

from __future__ import annotations

import os
import sys
import json
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.model_client import ModelClient, model_client

RESULTS_DIR = os.path.join("evaluation", "results")
os.makedirs(RESULTS_DIR, exist_ok=True)
CASES_FILE = os.path.join("evaluation", "cases.json")


def run_benchmark(client: ModelClient | None = None, max_cases: int | None = None) -> Dict[str, Any]:
    c = client or model_client
    if not os.path.exists(CASES_FILE):
        print(f"[ERR] Cases file not found at {CASES_FILE}")
        return {"error": "CASES_NOT_FOUND"}

    with open(CASES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    cases = data.get("cases", [])
    if max_cases:
        cases = cases[:max_cases]

    print(f"=== Starting AI Market Analyst Benchmark ({len(cases)} cases) ===")
    print(f"Model Target: {c.model_name} at {c.base_url}")

    results = []
    total_passed = 0
    total_latency_ms = 0.0
    total_tokens = 0
    start_all = time.time()

    for idx, case in enumerate(cases, 1):
        cid = case["id"]
        cat = case["category"]
        prompt = case["prompt"]
        expected = case.get("expected_keywords", [])
        
        mode = "FAST" if cat in ("structured_json", "tool_calling") else "ANALYSIS"
        print(f"[{idx:02d}/{len(cases):02d}] Testing {cid} ({cat}, mode={mode})...", end="", flush=True)

        t0 = time.time()
        passed = False
        error_msg = None
        resp_text = ""
        tokens = 0

        # Category-based max_tokens optimization for 50-case benchmark speed
        cat_max_tokens = {
            "structured_json": 256,
            "tool_calling": 256,
            "math_financial": 180,
            "python_quant": 300,
            "tradingview_pine": 300,
            "crypto_market_analysis": 400,
            "long_context_research": 500,
        }
        max_tok = cat_max_tokens.get(cat, 350)

        try:
            resp = c.chat_completion(
                messages=[
                    {"role": "system", "content": "You are an institutional quantitative trading AI. Follow all constraints strictly. Answer concisely and accurately."},
                    {"role": "user", "content": prompt}
                ],
                mode=mode,
                max_tokens=max_tok,
            )
            lat_ms = (time.time() - t0) * 1000.0
            choice = resp.get("choices", [{}])[0]
            resp_text = resp.get("content") or choice.get("message", {}).get("content", "")
            thinking_text = resp.get("thinking") or choice.get("message", {}).get("reasoning_content", "")
            eval_text = resp_text if resp_text else thinking_text
            usage = resp.get("usage", {})
            tokens = usage.get("total_tokens", len(eval_text.split()))

            # Validation logic
            if cat == "structured_json":
                # Must be strictly valid JSON
                cleaned = resp_text.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.splitlines()
                    cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
                json.loads(cleaned)
                passed = True
            elif expected:
                # Check keyword coverage in both final content and reasoning
                search_corpus = (resp_text + " " + thinking_text).lower()
                matched = [kw for kw in expected if kw.lower() in search_corpus]
                passed = len(matched) >= max(1, len(expected) // 2)
            else:
                passed = bool(eval_text.strip())

        except Exception as e:
            lat_ms = (time.time() - t0) * 1000.0
            error_msg = str(e)
            passed = False

        if passed:
            total_passed += 1
            print(f" PASS ({lat_ms:.0f}ms)")
        else:
            print(f" FAIL ({lat_ms:.0f}ms) - {error_msg or 'Criterion mismatch'}")

        total_latency_ms += lat_ms
        total_tokens += tokens

        results.append({
            "id": cid,
            "category": cat,
            "mode": mode,
            "passed": passed,
            "latency_ms": round(lat_ms, 2),
            "tokens": tokens,
            "error": error_msg,
            "preview": resp_text[:120].replace("\n", " "),
        })

    total_time = time.time() - start_all
    avg_latency = total_latency_ms / len(cases) if cases else 0.0
    pass_rate = (total_passed / len(cases) * 100.0) if cases else 0.0
    tok_per_sec = total_tokens / (total_time + 0.001)

    summary = {
        "model": c.model_name,
        "total_cases": len(cases),
        "passed_cases": total_passed,
        "failed_cases": len(cases) - total_passed,
        "pass_rate_pct": round(pass_rate, 2),
        "total_time_sec": round(total_time, 2),
        "avg_latency_ms": round(avg_latency, 2),
        "overall_tokens_per_sec": round(tok_per_sec, 2),
        "results": results,
    }

    report_path = os.path.join(RESULTS_DIR, "benchmark_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print("\n=========================================")
    print(f" Benchmark Finished!")
    print(f" Passed:    {total_passed}/{len(cases)} ({pass_rate:.1f}%)")
    print(f" Avg Lat:   {avg_latency:.1f} ms")
    print(f" Overall:   {tok_per_sec:.1f} tokens/s")
    print(f" Report:    {report_path}")
    print("=========================================")

    return summary


if __name__ == "__main__":
    run_benchmark()
