"""Durable, human-readable stage trace for one AI-led cycle.

The cycle row is the compact index.  This module owns the normalized stage
projection so a system guard, a real model WAIT, and an executed order cannot
be confused merely because the compatibility action column historically used
``WAIT`` for all three cases.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Iterable


STAGE_ORDER: tuple[str, ...] = (
    "ACCOUNT",
    "AUTHORIZATION",
    "MARKET_DATA",
    "NEWS_EVENTS",
    "KLINE_CONTEXT",
    "STRATEGY_SCAN",
    "AI_MODEL",
    "RISK",
    "EXECUTION",
    "RECONCILIATION",
)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    elif value:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if point.tzinfo is None:
        point = point.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def infer_block_stage(reason: Any, explicit: Any = None) -> str:
    """Map a stable error code to the first blocked pipeline stage."""

    if str(explicit or "").upper() in STAGE_ORDER:
        return str(explicit).upper()
    code = str(reason or "").split(":", 1)[0].strip().upper()
    if code in {"AI_SESSION_NOT_ENABLED", "SESSION_NOT_EXECUTABLE", "AUTHORIZATION_REVOKED_BEFORE_EXECUTION"}:
        return "AUTHORIZATION"
    if code == "LIVE_EXECUTION_LOCKED":
        return "EXECUTION"
    if any(token in code for token in ("ACCOUNT", "REMOTE_ACCOUNT", "CREDENTIAL", "GATE_")):
        # Gate market errors are market facts, not account facts.
        if any(token in code for token in ("MARKET", "TICKER", "OHLCV", "CONTRACT")):
            return "MARKET_DATA"
        return "ACCOUNT"
    if any(token in code for token in ("AUTHORIZATION", "LOCAL_CONFIRMATION", "DIRECTION_NOT", "INSTRUMENT_NOT")):
        return "AUTHORIZATION"
    if any(token in code for token in ("MARKET", "QUOTE", "BAR", "DATA_", "FRESH", "STALE")):
        return "MARKET_DATA"
    if any(token in code for token in ("NEWS", "EVENT")):
        return "NEWS_EVENTS"
    if any(token in code for token in ("KLINE", "INDICATOR", "TIMEFRAME")):
        return "KLINE_CONTEXT"
    if any(token in code for token in ("CALIBRATION", "CANDIDATE", "STRATEGY")):
        return "STRATEGY_SCAN"
    if any(token in code for token in ("MODEL", "SMART", "SCHEMA", "GENERATION", "TIMEOUT")):
        return "AI_MODEL"
    if any(token in code for token in ("RISK", "LEVERAGE", "STOP", "PROTECTION", "QTY", "BUDGET")):
        return "RISK"
    if any(token in code for token in ("GATEWAY", "ORDER", "EXECUTION")):
        return "EXECUTION"
    if any(token in code for token in ("RECONCILIATION", "FILL", "POSITION")):
        return "RECONCILIATION"
    return "AI_MODEL"


def humanize_reason(reason: Any, *, stage: str | None = None) -> str:
    """Return a bounded UI sentence without exposing provider payloads."""

    code = str(reason or "UNKNOWN").split(":", 1)[0].strip().upper()
    messages = {
        "ACCOUNT_REQUIRED": "未选择可执行账户，本轮未读取账户事实，也未调用模型。",
        "AI_SESSION_NOT_ENABLED": "AI 会话未显式启动，本轮未调用模型。",
        "AUTHORIZATION_REQUIRED": "没有当前账户有效的 AI_LED 本地授权，本轮未调用模型。",
        "AUTHORIZATION_SCOPE_MISMATCH": "授权的账户、环境或决策路径与当前会话不一致，本轮已阻断。",
        "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE": "Gate TestNet 账户事实不可用，无法核对权益/保证金，本轮未调用模型。",
        "GATE_CREDENTIALS_REQUIRED": "Gate TestNet 凭证未配置，无法读取远端账户事实。",
        "MARKET_DATA_UNAVAILABLE": "没有满足时效和可执行性要求的行情，本轮未调用模型。",
        "SMART_MODEL_UNAVAILABLE": "Qwen3.5-9B 当前不可用，本轮未生成交易动作。",
        "MODEL_TIMEOUT_DISCARDED": "模型推理超过本轮有效期，结果已丢弃。",
        "CALIBRATION_NOT_READY": "校准证据尚未就绪，本轮未调用模型。",
        "INVALID_MODEL_OUTPUT_SCHEMA": "模型返回不符合严格动作契约，已拒绝执行。",
        "STALE_GENERATION_DISCARDED": "会话代次已变化，模型结果已丢弃。",
    }
    return messages.get(code) or (f"{stage or infer_block_stage(code)} 阶段未通过：{str(reason)[:240]}")


def _evidence(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            result.extend(str(item) for item in value if str(item).strip())
    return list(dict.fromkeys(result))[:32]


def build_stage_trace(
    context: Any,
    *,
    result_status: str,
    result_reason: str,
    decision_origin: str,
    model_called: bool,
    model_result: str,
    completed_at: datetime | None = None,
    execution_result: dict[str, Any] | None = None,
    order_intent: Any | None = None,
) -> list[dict[str, Any]]:
    """Build all ten stages for the immutable cycle snapshot."""

    started = _parse_time(getattr(context, "started_at", None)) or datetime.now(timezone.utc)
    finished = completed_at or started
    block_stage = infer_block_stage(
        result_reason,
        (getattr(context, "stage_block", None) or ""),
    )
    origin = str(decision_origin or "SYSTEM").upper()
    status = str(result_status or "UNKNOWN").upper()
    system_block = origin != "MODEL" or not model_called
    snapshots = getattr(context, "market_snapshots", {}) or {}
    candidates = getattr(context, "candidates", []) or []
    truth = getattr(context, "account_truth", {}) or {}
    authorization_id = getattr(context, "authorization_id", None)
    evidence_context = getattr(context, "evidence_refs", ()) or ()

    def natural_status(stage: str) -> str:
        if system_block:
            index = STAGE_ORDER.index(stage)
            blocker_index = STAGE_ORDER.index(block_stage)
            if index < blocker_index:
                return "PASS"
            if index == blocker_index:
                return "BLOCKED"
            return "SKIPPED"
        if stage == "AI_MODEL":
            return "PASS" if model_called else "BLOCKED"
        if stage == "RISK":
            if status in {"REJECTED", "BLOCKED"} and block_stage == "RISK":
                return "FAILED"
            model_action = str(model_result or "").upper()
            if model_action in {"", "WAIT", "HOLD", "NOT_RUN"}:
                return "SKIPPED"
            inferred_stage = infer_block_stage(result_reason)
            if status in {"REJECTED", "BLOCKED"} and inferred_stage in {
                "ACCOUNT", "AUTHORIZATION", "MARKET_DATA", "NEWS_EVENTS",
                "KLINE_CONTEXT", "STRATEGY_SCAN", "AI_MODEL",
            }:
                return "SKIPPED"
            return "PASS" if status in {"EXECUTED", "REJECTED", "BLOCKED"} else "SKIPPED"
        if stage == "EXECUTION":
            if status == "EXECUTED":
                return "PASS"
            if block_stage == "EXECUTION" or (status == "REJECTED" and result_reason):
                return "FAILED"
            return "SKIPPED"
        if stage == "RECONCILIATION":
            if status == "EXECUTED":
                remote = execution_result or {}
                return "PASS" if remote.get("reconciled", True) is not False else "FAILED"
            return "SKIPPED"
        return "PASS"

    evidence_by_stage: dict[str, list[str]] = {
        "ACCOUNT": _evidence(f"account:{getattr(context, 'account_id', '')}", truth.get("snapshot_id")),
        "AUTHORIZATION": _evidence(f"authorization:{authorization_id}" if authorization_id else None),
        "MARKET_DATA": _evidence(evidence_context, [f"market_snapshot:{symbol}" for symbol in snapshots]),
        "NEWS_EVENTS": _evidence(
            [f"news_revision:{item.get('revision_id')}" for item in (getattr(context, "news_revisions", []) or []) if isinstance(item, dict) and item.get("revision_id")]
        ),
        "KLINE_CONTEXT": _evidence(getattr(context, "indicator_snapshot_id", None)),
        "STRATEGY_SCAN": _evidence([f"candidate:{item.get('candidate_id')}" for item in candidates if isinstance(item, dict) and item.get("candidate_id")]),
        "AI_MODEL": _evidence(getattr(context, "input_hash", None), getattr(context, "model_digest", None)),
        "RISK": _evidence(getattr(order_intent, "intent_id", None)),
        "EXECUTION": _evidence(getattr(order_intent, "intent_id", None)),
        "RECONCILIATION": _evidence((execution_result or {}).get("remote_account_truth", {}).get("snapshot_id")),
    }
    messages = {
        "ACCOUNT": "账户范围已锁定；Gate TestNet 经济事实仅来自远端私有 API。",
        "AUTHORIZATION": "账户授权、方向、标的和有效期在模型外校验。",
        "MARKET_DATA": "使用满足时效约束的行情快照，不用模型输入反推可执行价格。",
        "NEWS_EVENTS": "记录新闻修订与事件状态；没有事件证据时保持 UNKNOWN。",
        "KLINE_CONTEXT": "保留 15m 信号与 1h 上下文的来源时间和指标快照。",
        "STRATEGY_SCAN": "六策略只产生候选证据，不直接发单。",
        "AI_MODEL": "仅允许真实 Qwen3.5-9B 返回严格 JSON 动作。",
        "RISK": "风控、授权、费用、滑点、保护计划在网关内复核。",
        "EXECUTION": "通过统一 ExecutionGateway；没有本地模拟成交冒充远端成交。",
        "RECONCILIATION": "以远端订单/成交/持仓回读作为 Gate TestNet 最终事实。",
    }
    trace: list[dict[str, Any]] = []
    for sequence, stage in enumerate(STAGE_ORDER, start=1):
        stage_status = natural_status(stage)
        stage_start = started
        stage_end = finished if stage_status in {"PASS", "BLOCKED", "FAILED", "SKIPPED"} else None
        duration = max(0.0, (stage_end - stage_start).total_seconds() * 1000.0) if stage_end else None
        reason_code = None
        human_message = messages[stage]
        if stage_status in {"BLOCKED", "FAILED"}:
            reason_code = str(result_reason or "UNKNOWN").split(":", 1)[0]
            human_message = humanize_reason(result_reason, stage=stage)
        if stage_status == "SKIPPED":
            human_message = "因前置阶段未通过，本阶段未运行。"
        trace.append(
            {
                "stage": stage,
                "sequence": sequence,
                "status": stage_status,
                "started_at": _iso(stage_start),
                "completed_at": _iso(stage_end),
                "duration_ms": duration,
                "reason_code": reason_code,
                "human_message": human_message,
                "evidence_refs": evidence_by_stage.get(stage, []),
                "payload": {
                    "decision_origin": origin,
                    "model_called": bool(model_called),
                    "model_result": model_result,
                    "block_stage": block_stage if system_block else None,
                },
            }
        )
    return trace


def persist_stage_trace(store: Any, *, cycle_id: str, account_id: str, trace: list[dict[str, Any]]) -> None:
    if not hasattr(store, "_connect"):
        return
    try:
        with store._connect() as db:
            for item in trace:
                db.execute(
                    """INSERT OR REPLACE INTO ai_cycle_stages(
                        stage_id, cycle_id, account_id, sequence, stage, status,
                        started_at, completed_at, duration_ms, reason_code,
                        human_message, evidence_refs_json, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"stage_{cycle_id}_{str(item.get('stage')).lower()}",
                        cycle_id,
                        account_id,
                        int(item.get("sequence") or 0),
                        item.get("stage"),
                        item.get("status"),
                        item.get("started_at"),
                        item.get("completed_at"),
                        item.get("duration_ms"),
                        item.get("reason_code"),
                        item.get("human_message"),
                        json.dumps(item.get("evidence_refs") or [], ensure_ascii=False, allow_nan=False),
                        json.dumps(item.get("payload") or {}, ensure_ascii=False, allow_nan=False),
                    ),
                )
    except Exception:
        # Cycle persistence must not turn a safe execution result into an
        # unhandled request failure.  The primary cycle row remains the index.
        return


__all__ = [
    "STAGE_ORDER",
    "build_stage_trace",
    "humanize_reason",
    "infer_block_stage",
    "persist_stage_trace",
]
