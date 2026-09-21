"""Structured audit and tool execution logger for AI Market Analyst V2."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

TOOLS_LOG_DIR = os.path.join("logs", "tools")
os.makedirs(TOOLS_LOG_DIR, exist_ok=True)

logger = logging.getLogger("ai_market_analyst")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def log_tool_call(
    *,
    agent: str,
    tool: str,
    arguments: Dict[str, Any],
    result: Any,
    latency_ms: float,
    success: bool,
    error: str | None = None,
) -> None:
    """Log tool call execution to daily JSONL file in logs/tools/."""
    now = datetime.now(timezone.utc)
    entry = {
        "timestamp": now.isoformat(),
        "agent": agent,
        "tool": tool,
        "arguments": arguments,
        "result": result,
        "latency_ms": round(latency_ms, 2),
        "success": success,
        "error": error,
    }
    date_str = now.strftime("%Y-%m-%d")
    log_file = os.path.join(TOOLS_LOG_DIR, f"tools_{date_str}.jsonl")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Failed to write tool log: %s", e)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ai_market_analyst.{name}")
