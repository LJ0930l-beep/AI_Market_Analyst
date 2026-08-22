"""Bounded read-only smoke for the V1.1 deterministic Qwen model router."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def gpu_capability() -> dict[str, object]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    if result.returncode != 0:
        return {"status": "unavailable", "reason": "nvidia_smi_failed"}
    return {"status": "available", "read_only": True, "summary": result.stdout.strip()[:240]}


def run_one(base_url: str, preference: str, language: str) -> dict[str, object]:
    prompt = "请用一句简短中文说明：证据不足时应明确说不知道。" if language == "zh-CN" else "In one short sentence, explain why missing evidence must be stated."
    request = Request(
        f"{base_url.rstrip('/')}/consult/stream",
        data=json.dumps(
            {
                "language": language,
                "model_preference": preference,
                "task": "assistant",
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8"),
        method="POST",
        headers={"Accept": "application/x-ndjson", "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    first_token: float | None = None
    meta: dict[str, object] = {}
    chunks: list[str] = []
    with urlopen(request, timeout=180) as response:
        for raw_line in response:
            event = json.loads(raw_line.decode("utf-8"))
            if event.get("type") == "meta":
                meta = event
            elif event.get("type") == "delta":
                if first_token is None:
                    first_token = time.perf_counter() - started
                chunks.append(str(event.get("content", "")))
            elif event.get("type") == "error":
                raise RuntimeError(str(event.get("error", {}).get("code", "QWEN_SMOKE_FAILED")))
    elapsed = time.perf_counter() - started
    output = "".join(chunks).strip()
    return {
        "preference": preference,
        "language": language,
        "model_id": meta.get("model_id"),
        "model_tier": meta.get("model_tier"),
        "route": meta.get("model_route"),
        "first_token_seconds": round(first_token, 3) if first_token is not None else None,
        "total_seconds": round(elapsed, 3),
        "output_chars": len(output),
        "output_sample": output[:160],
        "read_only": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = {
        "contract": "v11_live_model_smoke_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture": False,
        "gpu": gpu_capability(),
        "runs": [
            run_one(args.base_url, "fast", "zh-CN"),
            run_one(args.base_url, "smart", "en"),
        ],
        "boundary": "No symbol context, database write, analysis, scan, settlement, Follow, PaperTrade, broker or order action.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
