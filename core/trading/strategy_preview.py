"""Read-only strategy preview using the production strategy prompt builder.

This intentionally renders only the durable strategy system instruction. The
live coordinator adds cycle-specific evidence, a serialized user message, token
budget compaction, and (on a repair call) a repair instruction at run time.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .ai_strategy_book import DEFAULT_SECTIONS, AIStrategyBook
from .autonomous_strategy import build_strategy_system_prompt
from .strategy_execution import normalize_execution


def build_strategy_preview(active: dict[str, Any], draft: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a side-effect-free preview and static validation receipt.

    No store writes, market data reads, model calls, session controls, or
    execution gateway access occur in this function.
    """
    draft = draft if isinstance(draft, dict) else {}
    current = active if isinstance(active, dict) else {}
    errors: list[str] = []

    requested_template = draft.get("template_id") or current.get("template_id")
    template = AIStrategyBook._template(requested_template, name=draft.get("name") or current.get("name"))
    if requested_template and template["id"] != str(requested_template).strip():
        errors.append("STRATEGY_TEMPLATE_INVALID")
    name = str(draft.get("name") if draft.get("name") is not None else current.get("name") or template["name"]).strip()
    if not 1 <= len(name) <= 80:
        errors.append("STRATEGY_NAME_INVALID")

    raw_sections = draft.get("sections")
    if raw_sections is None:
        sections = deepcopy(current.get("sections") or template["sections"])
    elif isinstance(raw_sections, dict):
        sections = dict(raw_sections)
    else:
        sections = {}
    if set(sections) != set(DEFAULT_SECTIONS) or any(
        not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000
        for value in sections.values()
    ):
        errors.append("STRATEGY_SECTIONS_INVALID")
    else:
        sections = AIStrategyBook._sections_for_template(template, sections)

    raw_execution = draft.get("execution")
    if raw_execution is None:
        execution = {
            **deepcopy(current.get("execution") or {}),
            **deepcopy(template.get("execution_defaults") or {}),
            "scan_interval_minutes": template["scan_interval_minutes"],
            "order_preference": template["order_preference"],
        }
    elif isinstance(raw_execution, dict):
        # Match AIStrategyBook.save: a supplied execution object is normalized
        # as the complete submitted config; callers should send the form's
        # complete execution object when they want to preview a save.
        execution = dict(raw_execution)
    else:
        execution = {}
        errors.append("STRATEGY_EXECUTION_FIELDS_INVALID")

    profile = deepcopy(template["profile"])
    preference = str(profile.get("order_preference") or "AUTO").upper()
    if bool(profile.get("limit_priority")):
        execution["order_preference"] = "AUTO"
    elif preference in {"MARKET", "LIMIT"}:
        execution["order_preference"] = preference
    profile_interval = AIStrategyBook._profile_signal_interval(profile)
    if profile_interval is not None:
        execution["scan_interval_minutes"] = profile_interval

    try:
        execution = normalize_execution(execution)
    except (TypeError, ValueError) as exc:
        errors.append(str(exc) or "STRATEGY_EXECUTION_INVALID")
        execution = deepcopy(current.get("execution") or {})

    instruction = {
        "name": name or template["name"],
        "template_id": template["id"],
        "style": template["style"],
        "profile": profile,
        "sections": sections,
        "execution": execution,
    }
    system_prompt = build_strategy_system_prompt(instruction)
    valid = not errors
    return {
        "preview_kind": "STRATEGY_SYSTEM_INSTRUCTION_ONLY",
        "valid": valid,
        "validation_errors": errors,
        "summary": {
            "name": instruction["name"],
            "template_id": instruction["template_id"],
            "style": instruction["style"],
            "signal_timeframe": profile.get("signal_timeframe"),
            "scan_interval_minutes": execution.get("scan_interval_minutes"),
            "universe_mode": execution.get("universe_mode"),
            "sizing_mode": execution.get("sizing_mode"),
            "risk_per_trade_pct": execution.get("risk_per_trade_pct"),
            "max_notional_usdt": execution.get("max_notional_usdt"),
            "leverage_preference_ceiling": execution.get("leverage"),
            "order_preference": execution.get("order_preference"),
            "limit_priority": bool(profile.get("limit_priority")),
        },
        "effective_strategy": instruction,
        "strategy_system_instruction": system_prompt,
        "model_called": False,
        "execution_called": False,
        "limitations": [
            "这是策略配置静态预览，不是本轮运行时完整模型请求。",
            "生产运行时还会加入本轮真实市场、K线、新闻、账户与仓位证据，并按模型上下文预算压缩输入。",
            "未调用 Bonsai、未读取或伪造行情、未启动交易会话、未调用下单或交易所接口。",
            "杠杆字段是策略偏好上限；实际杠杆仍受止损风险规则与 Gate 合约规格共同约束。",
        ],
    }
