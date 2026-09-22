"""Collect decision cycles into an instruction-tuning (SFT) dataset.

Samples are only collected from non-executable environments. The guard fails
closed: an unrecognised or missing trading mode is refused, because a sample
carries the full decision prompt, which embeds ``account_truth`` (balances and
open positions). Letting a LIVE cycle into a file that is meant to be uploaded
to a cloud training box would publish real account state.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import app_data_paths

logger = logging.getLogger("core.trading.fin_dataset_collector")


# Resolved from the application data root so AIMA_DATA_ROOT is honoured: the
# samples embed full decision prompts, so they belong with runtime data rather
# than inside the source tree.
DATASET_DIR = Path(os.environ.get("AIMA_FIN_TUNING_DIR") or (app_data_paths().data / "fin_tuning"))
DATASET_FILE = DATASET_DIR / "crypto_sft_dataset.jsonl"

COLLECTABLE_MODES = frozenset({"PAPER", "TESTNET"})

INSTRUCTION = (
    "作为资深加密货币自主量化交易决策大脑，根据输入的实时K线技术面、多周期指标、"
    "流动性扫荡形态、宏观要闻及风控约束，严格输出符合交易Schema的JSON决策对象。"
)

VALID_ACTIONS = frozenset({
    "WAIT", "HOLD", "OPEN_LONG", "OPEN_SHORT",
    "REDUCE_POSITION", "CLOSE_POSITION", "TIGHTEN_STOP",
})


def _mode_label(mode: Any) -> str:
    return str(getattr(mode, "value", mode) or "").strip().upper()


def is_collectable(mode: Any, account_id: Optional[str] = None) -> bool:
    """Return True only for environments that hold no real funds."""
    if _mode_label(mode) not in COLLECTABLE_MODES:
        return False
    return "live" not in str(account_id or "").strip().lower()


def record_sft_sample(
    *,
    cycle_id: str,
    system_prompt: str,
    user_prompt: str,
    model_output: Dict[str, Any],
    account_id: Optional[str] = None,
    mode: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Append one verified cycle as a training sample. Never raises."""
    try:
        if not isinstance(model_output, dict) or not model_output:
            return False
        action = model_output.get("action")
        if action not in VALID_ACTIONS:
            return False
        if not is_collectable(mode, account_id):
            logger.info(
                "SFT collection refused: mode=%s account=%s is not a collectable environment",
                _mode_label(mode) or "UNKNOWN", account_id or "UNKNOWN",
            )
            return False

        DATASET_DIR.mkdir(parents=True, exist_ok=True)
        sample = {
            "instruction": INSTRUCTION,
            "input": user_prompt,
            "output": json.dumps(model_output, ensure_ascii=False, indent=2),
            "system": system_prompt,
            "metadata": {
                "cycle_id": cycle_id,
                "action": action,
                "instrument_id": model_output.get("instrument_id"),
                "reason": model_output.get("reason"),
                "account_id": account_id,
                "mode": _mode_label(mode) or None,
                **(metadata or {}),
            },
        }
        with open(DATASET_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        logger.info("Recorded SFT training sample for cycle %s (%s)", cycle_id, action)
        return True
    except Exception as exc:
        logger.warning("Failed to record SFT sample for cycle %s: %s", cycle_id, exc)
        return False


def get_dataset_stats() -> Dict[str, Any]:
    if not DATASET_FILE.exists():
        return {"total_samples": 0, "actions": {}, "file_path": str(DATASET_FILE)}

    total = 0
    actions: Dict[str, int] = {}
    try:
        with open(DATASET_FILE, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                total += 1
                act = row.get("metadata", {}).get("action", "UNKNOWN")
                actions[act] = actions.get(act, 0) + 1
        return {"total_samples": total, "actions": actions, "file_path": str(DATASET_FILE)}
    except Exception as exc:
        return {"error": str(exc), "total_samples": total, "file_path": str(DATASET_FILE)}
