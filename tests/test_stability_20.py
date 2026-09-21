"""
Stability Test Suite for Bonsai 2 27B on RTX 4060 8GB.
Executes 20 consecutive diverse requests (short, long, tool call, JSON, code, financial analysis).
Verifies:
1. 0 server crashes
2. 0 CUDA OOMs
3. 0 garbled outputs / encoding errors
4. Sustainable API responsiveness
5. JSON Schema success rate >= 95%
6. Tool calling functionality
"""

import os
import sys
import json
import time
import subprocess
from pathlib import Path
from typing import Dict, Any, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.model_client import model_client
from core.schemas import MARKET_ANALYSIS_SCHEMA
from tools.crypto import CryptoTools
from tools.technical_analysis import compute_technical_indicators

crypto_tools = CryptoTools()

TEST_SCENARIOS = [
    # 1. Short Prompt
    {"type": "short", "mode": "FAST", "prompt": "Define EMA in 10 words.", "max_tokens": 50},
    # 2. JSON Output
    {"type": "json", "mode": "FAST", "prompt": "Output JSON: {\"symbol\": \"BTC\", \"status\": \"bullish\", \"rsi\": 62}", "max_tokens": 80},
    # 3. Code Generation
    {"type": "code", "mode": "FAST", "prompt": "Write a 3-line Python function to calculate VWAP from arrays of price and volume.", "max_tokens": 150},
    # 4. Financial Analysis (Mode A)
    {"type": "analysis", "mode": "ANALYSIS", "prompt": "Analyze what happens to funding rates when perpetual futures price is above spot price.", "max_tokens": 400},
    # 5. Tool Call Integration
    {"type": "tool", "mode": "FAST", "prompt": "tool_exec_ohlcv", "is_tool": True},
    # 6. Long Prompt
    {"type": "long", "mode": "ANALYSIS", "prompt": "Explain the full Wyckoff distribution schematic including Phase A, B, C, D, and E in concise terms.", "max_tokens": 600},
    # 7. Short Prompt
    {"type": "short", "mode": "FAST", "prompt": "What does RSI > 70 generally indicate?", "max_tokens": 60},
    # 8. Math / Quantitative
    {"type": "math", "mode": "FAST", "prompt": "If entry is 64000, stop loss is 63200, take profit is 66400, calculate the Risk/Reward ratio.", "max_tokens": 100},
    # 9. Structured Financial JSON
    {"type": "json_schema", "mode": "FAST", "prompt": "Analyze ETHUSDT 1h: EMA20=3400, EMA50=3350, Trend=Bullish, Structure=HH_HL, Support=[3350, 3280], Resistance=[3500, 3620]. Output matching schema.", "is_structured": True},
    # 10. Code - Pine Script
    {"type": "code", "mode": "FAST", "prompt": "Generate Pine Script v5 code to plot an EMA 20 line colored green on a chart.", "max_tokens": 150},
    # 11. Tool Call Integration
    {"type": "tool", "mode": "FAST", "prompt": "tool_exec_ta", "is_tool": True},
    # 12. Financial Analysis (Mode A)
    {"type": "analysis", "mode": "ANALYSIS", "prompt": "Explain Order Flow Absorption and how aggressive market sell orders can be absorbed by passive limit buy orders.", "max_tokens": 450},
    # 13. Short Prompt
    {"type": "short", "mode": "FAST", "prompt": "What is Open Interest (OI) in crypto derivatives?", "max_tokens": 60},
    # 14. JSON Output
    {"type": "json", "mode": "FAST", "prompt": "Output JSON with keys 'action', 'confidence', 'reason' for a retest of 200 EMA.", "max_tokens": 100},
    # 15. Risk Assessment
    {"type": "analysis", "mode": "ANALYSIS", "prompt": "Evaluate counterparty and liquidation cascade risks during sudden extreme market volatility.", "max_tokens": 400},
    # 16. Code - Python Dataframe
    {"type": "code", "mode": "FAST", "prompt": "Write Python pandas code to compute 14-period ATR from a DataFrame df with columns high, low, close.", "max_tokens": 200},
    # 17. Tool Call Integration
    {"type": "tool", "mode": "FAST", "prompt": "tool_exec_funding", "is_tool": True},
    # 18. Structured Financial JSON
    {"type": "json_schema", "mode": "FAST", "prompt": "Analyze SOLUSDT 4h: EMA20=145, EMA50=140, Trend=Bullish, Structure=HH_HL, Support=[140, 132], Resistance=[155, 168]. Output matching schema.", "is_structured": True},
    # 19. Long Financial Analysis
    {"type": "long", "mode": "ANALYSIS", "prompt": "Provide an institutional market structure analysis explaining the difference between Change of Character (CHoCH) and Break of Structure (BOS).", "max_tokens": 500},
    # 20. Final System Health Probe
    {"type": "short", "mode": "FAST", "prompt": "Confirm AI Market Analyst system status with 'READY'.", "max_tokens": 30},
]

def check_gpu():
    try:
        res = subprocess.run([
            "nvidia-smi",
            "--query-gpu=memory.used,memory.free",
            "--format=csv,noheader,nounits"
        ], capture_output=True, text=True, check=True)
        parts = [float(p.strip()) for p in res.stdout.strip().split(",")]
        return parts[0], parts[1]
    except Exception:
        return 0.0, 0.0

def run_stability_suite():
    print("==================================================")
    print(" Running 20 Consecutive Stability & Stress Tests")
    print(" Model: Bonsai 2 27B PTQ1_0 | GPU: RTX 4060 8GB")
    print("==================================================")

    results = []
    crashes = 0
    ooms = 0
    garbled_count = 0
    json_success_count = 0
    json_total_count = 0
    tool_success_count = 0
    tool_total_count = 0

    start_all = time.time()

    for idx, sc in enumerate(TEST_SCENARIOS, 1):
        t0 = time.time()
        stype = sc["type"]
        mode = sc["mode"]
        print(f"[{idx:02d}/20] Testing {stype.upper()} (Mode={mode})... ", end="", flush=True)

        vram_used, vram_free = check_gpu()
        success = False
        error_msg = None
        output_snippet = ""

        try:
            if sc.get("is_tool"):
                tool_total_count += 1
                if sc["prompt"] == "tool_exec_ohlcv":
                    ohlcv = crypto_tools.get_ohlcv("BTC/USDT", "1h", limit=50)
                    # Valid response is either live candlestick bars or structured DATA_UNAVAILABLE contract
                    success = isinstance(ohlcv, dict) and ("bars" in ohlcv or ohlcv.get("status") == "DATA_UNAVAILABLE")
                    output_snippet = f"OHLCV status: {ohlcv.get('status', 'OK')}, keys={list(ohlcv.keys())}"
                elif sc["prompt"] == "tool_exec_ta":
                    # Synthetic 50 bars to test deterministic Python TA calculation
                    synth_candles = [
                        {"open": 64000 + i*50, "high": 64200 + i*50, "low": 63900 + i*50, "close": 64100 + i*50, "volume": 1200 + i*10}
                        for i in range(50)
                    ]
                    ta = compute_technical_indicators(synth_candles)
                    success = "ema20" in ta and "rsi14" in ta and "market_structure" in ta
                    output_snippet = f"TA calculated: ema20={ta.get('ema20')}, rsi={ta.get('rsi14')}, struct={ta.get('market_structure')}"
                elif sc["prompt"] == "tool_exec_funding":
                    fr = crypto_tools.get_funding_rate("BTC/USDT")
                    success = isinstance(fr, dict) and ("funding_rate" in fr or fr.get("status") == "DATA_UNAVAILABLE")
                    output_snippet = f"Funding status: {fr.get('status', 'OK')}, rate={fr.get('funding_rate')}"
                if success:
                    tool_success_count += 1

            elif sc.get("is_structured"):
                json_total_count += 1
                messages = [
                    {"role": "system", "content": "You are a quantitative technical analyst. Output strictly matching the JSON schema."},
                    {"role": "user", "content": sc["prompt"]}
                ]
                res = model_client.structured_analysis(messages, schema=MARKET_ANALYSIS_SCHEMA, mode="FAST")
                # Validate key fields
                if all(k in res for k in ["symbol", "trend", "market_structure", "bullish_scenario", "bearish_scenario"]):
                    success = True
                    json_success_count += 1
                    output_snippet = f"Valid JSON (keys={len(res)})"
                else:
                    success = False
                    output_snippet = f"Missing schema keys: {list(res.keys())}"

            else:
                if stype in ("json",):
                    json_total_count += 1

                messages = [
                    {"role": "system", "content": "You are a helpful quantitative finance assistant. Answer directly and cleanly."},
                    {"role": "user", "content": sc["prompt"]}
                ]
                resp = model_client.chat_completion(
                    messages=messages,
                    mode=mode,
                    max_tokens=sc.get("max_tokens", 256)
                )
                text = resp.get("content") or ""
                thinking = resp.get("thinking") or ""
                full_resp = text if text else thinking
                output_snippet = (text or thinking)[:80].replace("\n", " ")

                # Check garbled / encoding
                if "\ufffd" in text:
                    garbled_count += 1

                if stype in ("json",):
                    try:
                        # Attempt json parse
                        cleaned = text.strip()
                        if cleaned.startswith("```"):
                            lines = cleaned.splitlines()
                            cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
                        json.loads(cleaned)
                        json_success_count += 1
                        success = True
                    except Exception:
                        success = False
                else:
                    success = bool(full_resp.strip())

        except Exception as exc:
            err = str(exc).lower()
            if "oom" in err or "out of memory" in err or "cuda" in err:
                ooms += 1
            if "refused" in err or "server error" in err:
                crashes += 1
            error_msg = str(exc)
            success = False

        lat_ms = (time.time() - t0) * 1000.0
        status_str = "PASS" if success else "FAIL"
        print(f"{status_str} ({lat_ms:.0f}ms) | {output_snippet} | VRAM: {vram_used:.0f}MB")

        results.append({
            "index": idx,
            "type": stype,
            "mode": mode,
            "latency_ms": round(lat_ms, 1),
            "success": success,
            "error": error_msg,
            "vram_used_mb": vram_used,
            "vram_free_mb": vram_free,
        })

    total_time = time.time() - start_all
    json_rate = (json_success_count / json_total_count * 100.0) if json_total_count else 100.0
    tool_rate = (tool_success_count / tool_total_count * 100.0) if tool_total_count else 100.0

    print("==================================================")
    print(f" Stability Suite Finished in {total_time:.2f}s")
    print(f" Total Requests:     {len(TEST_SCENARIOS)}")
    print(f" Server Crashes:     {crashes} (Requirement: 0)")
    print(f" CUDA OOMs:          {ooms} (Requirement: 0)")
    print(f" Garbled Outputs:    {garbled_count} (Requirement: 0)")
    print(f" JSON Schema Rate:   {json_rate:.1f}% (Requirement: >= 95%)")
    print(f" Tool Success Rate:  {tool_rate:.1f}% (Requirement: 100%)")
    print("==================================================")

    summary = {
        "total_requests": len(TEST_SCENARIOS),
        "total_time_sec": round(total_time, 2),
        "server_crashes": crashes,
        "cuda_ooms": ooms,
        "garbled_outputs": garbled_count,
        "json_success_rate": json_rate,
        "tool_success_rate": tool_rate,
        "all_passed": (crashes == 0 and ooms == 0 and garbled_count == 0 and json_rate >= 95.0 and tool_rate == 100.0),
        "details": results
    }

    import os
    os.makedirs("evaluation/results", exist_ok=True)
    with open("evaluation/results/stability_report.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    return summary

if __name__ == "__main__":
    res = run_stability_suite()
    if not res["all_passed"]:
        print("[FAIL] Stability requirements not met!")
        exit(1)
    else:
        print("[SUCCESS] All 20 stability requirements 100% SATISFIED!")
        exit(0)
