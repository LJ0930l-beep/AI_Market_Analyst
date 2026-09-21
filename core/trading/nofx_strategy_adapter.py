"""Import NOFX AI strategy settings into the Bonsai/Gate strategy contract.

This adapter translates NOFX's exported strategy instructions, candle cadence,
timeframes, indicator switches, and safe Gate-universe filters into a bounded
runtime contract. Credentials, external API definitions, leverage rules, model
settings, and order execution are never imported.

The upstream schema reference is NoFxAiOS/nofx at
638d4042118995fbf1a38d3822b1139aa3c6b467 (store/strategy.go). No upstream
implementation code is copied into this adapter.
"""
from __future__ import annotations

import json
from typing import Any

from .autonomous_strategy import STRATEGY_SECTION_CHAR_LIMITS


_SECTION_MAP = {
    "role_definition": "role",
    "trading_frequency": "frequency",
    "entry_standards": "entry_standards",
    "decision_process": "decision_process",
}
_FALLBACK_TEMPLATE_BY_INTERVAL = {5: "aggressive_impulse", 15: "aggressive_breakout"}
_SUPPORTED_CONTEXT_TIMEFRAMES = {"5m", "15m", "1h", "1d"}
_INDICATOR_PERIOD_LIMITS = {
    "ema_periods": (2, 200),
    "rsi_periods": (2, 100),
    "atr_periods": (2, 100),
    "bollinger_periods": (2, 200),
}


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _plain_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _periods(value: Any, *, minimum: int, maximum: int, defaults: tuple[int, ...]) -> list[int]:
    rows = value if isinstance(value, (list, tuple)) else defaults
    result: list[int] = []
    for item in rows[:4]:
        if isinstance(item, bool):
            continue
        try:
            period = int(item)
        except (TypeError, ValueError):
            continue
        if minimum <= period <= maximum and period not in result:
            result.append(period)
    return result or list(defaults)


def _gate_symbol(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # Gate and NOFX exports use several common separators. Keep only the
    # exchange-neutral product identity used by this app (e.g. BTCUSDT).
    parts = value.strip().upper().split(":", 1)
    symbol = "".join(ch for ch in parts[0] if ch.isalnum())
    if symbol.endswith("USDTUSDT"):
        symbol = symbol[:-4]
    return symbol if 4 <= len(symbol) <= 32 else None


def _nofx_runtime(
    *,
    source: dict[str, Any],
    ai_config: dict[str, Any],
    signal_timeframe: str,
    current_profile: dict[str, Any],
) -> dict[str, Any]:
    indicators = _object(ai_config.get("indicators"))
    klines = _object(indicators.get("klines"))
    selected = klines.get("selected_timeframes")
    if bool(klines.get("enable_multi_timeframe")) and isinstance(selected, (list, tuple)):
        raw_context = selected
    else:
        raw_context = [klines.get("longer_timeframe")] if klines.get("longer_timeframe") else current_profile.get("context_timeframes", [])
    context_timeframes = list(dict.fromkeys(
        str(item).strip().lower()
        for item in raw_context
        if str(item).strip().lower() in _SUPPORTED_CONTEXT_TIMEFRAMES
        and str(item).strip().lower() != signal_timeframe
    ))

    def enabled(key: str, *, default: bool = False) -> bool:
        value = indicators.get(key)
        return value if isinstance(value, bool) else default

    ema_min, ema_max = _INDICATOR_PERIOD_LIMITS["ema_periods"]
    rsi_min, rsi_max = _INDICATOR_PERIOD_LIMITS["rsi_periods"]
    atr_min, atr_max = _INDICATOR_PERIOD_LIMITS["atr_periods"]
    boll_min, boll_max = _INDICATOR_PERIOD_LIMITS["bollinger_periods"]
    normalized_indicators = {
        "raw_klines": {"enabled": True},
        "ema": {"enabled": enabled("enable_ema"), "periods": _periods(indicators.get("ema_periods"), minimum=ema_min, maximum=ema_max, defaults=(20, 50))},
        "macd": {"enabled": enabled("enable_macd")},
        "rsi": {"enabled": enabled("enable_rsi"), "periods": _periods(indicators.get("rsi_periods"), minimum=rsi_min, maximum=rsi_max, defaults=(7, 14))},
        "atr": {"enabled": enabled("enable_atr"), "periods": _periods(indicators.get("atr_periods"), minimum=atr_min, maximum=atr_max, defaults=(14,))},
        "bollinger": {"enabled": enabled("enable_boll"), "periods": _periods(indicators.get("boll_periods"), minimum=boll_min, maximum=boll_max, defaults=(20,))},
        "volume": {"enabled": enabled("enable_volume")},
        "open_interest": {"enabled": enabled("enable_oi"), "status": "CONFIGURED" if enabled("enable_oi") else "DISABLED"},
        "funding_rate": {"enabled": enabled("enable_funding_rate"), "status": "CONFIGURED" if enabled("enable_funding_rate") else "DISABLED"},
    }

    coin_source = _object(source.get("coin_source")) or _object(ai_config.get("coin_source"))
    source_type = str(coin_source.get("source_type") or "all").strip().lower()
    excluded = coin_source.get("excluded_coins")
    excluded_symbols = list(dict.fromkeys(
        symbol for symbol in (_gate_symbol(item) for item in (excluded if isinstance(excluded, (list, tuple)) else []))
        if symbol
    ))[:200]
    candidate_sources = ["gate_active_usdt_perpetuals"]
    unsupported_sources: list[str] = []
    raw_timeframes = selected if isinstance(selected, (list, tuple)) else [klines.get("longer_timeframe")]
    unavailable_timeframes = sorted({
        str(item).strip().lower()
        for item in raw_timeframes
        if item and str(item).strip().lower() not in _SUPPORTED_CONTEXT_TIMEFRAMES | {signal_timeframe}
    })
    for timeframe_name in unavailable_timeframes:
        unsupported_sources.append(f"timeframe:{timeframe_name}（当前 Gate 行情适配器不支持此周期）")
    if source_type not in {"", "all", "gate", "static"}:
        unsupported_sources.append(f"coin_source:{source_type}（本项目使用 Gate 全市场，不调用 NOFX 外部选币源）")
    if coin_source.get("static_coins"):
        unsupported_sources.append("coin_source:static_coins（静态池不会缩小交易所全品种授权范围）")
    for key, label in (
        ("use_ai500", "AI500"),
        ("use_oi_top", "OI_TOP"),
        ("use_oi_low", "OI_LOW"),
        ("use_hyper_all", "HYPERLIQUID_ALL"),
        ("use_hyper_main", "HYPERLIQUID_MAIN"),
    ):
        if coin_source.get(key) is True:
            unsupported_sources.append(f"coin_source:{label}（未接入 NOFX 外部数据源）")
    if indicators.get("external_data_sources"):
        unsupported_sources.append("indicators.external_data_sources（不导入 URL、Header、API key 或 Webhook）")
    if indicators.get("nofxos_api_key"):
        unsupported_sources.append("indicators.nofxos_api_key（凭证不会导入）")

    return {
        "version": 1,
        "source": "NOFX_IMPORT",
        "signal_timeframe": signal_timeframe,
        "context_timeframes": context_timeframes,
        "indicators": normalized_indicators,
        "candidate_sources": candidate_sources,
        "excluded_symbols": excluded_symbols,
        "unsupported_sources": list(dict.fromkeys(unsupported_sources))[:24],
    }


def adapt_nofx_strategy_config(
    configuration: Any,
    *,
    current_strategy: dict[str, Any],
    requested_name: str | None = None,
) -> dict[str, Any]:
    """Return a validated, account-safe strategy save payload.

    NOFX's strategy schema supports product-specific providers and fixed
    leverage controls. Those values are not imported: the returned execution
    settings are copied from this account's current strategy, while cadence is
    normalized to a supported signal timeframe.
    """
    if not isinstance(configuration, dict):
        raise TypeError("NOFX_CONFIG_INVALID")
    wrapper_name = _plain_text(configuration.get("name"))
    source = configuration
    if "config" in configuration:
        wrapped_config = configuration.get("config")
        if isinstance(wrapped_config, str):
            try:
                wrapped_config = json.loads(wrapped_config)
            except json.JSONDecodeError as exc:
                raise ValueError("NOFX_CONFIG_INVALID") from exc
        if not isinstance(wrapped_config, dict):
            raise ValueError("NOFX_CONFIG_INVALID")
        source = wrapped_config

    strategy_type = str(source.get("strategy_type") or "ai_trading").strip().lower()
    if strategy_type != "ai_trading":
        raise ValueError("NOFX_GRID_STRATEGY_UNSUPPORTED")

    ai_config = _object(source.get("ai_config"))
    if not ai_config:
        # Older NOFX exports stored AI settings at the root.
        ai_config = source

    raw_sections = _object(ai_config.get("prompt_sections"))
    current_sections = _object(current_strategy.get("sections"))
    sections: dict[str, str] = {
        key: str(current_sections.get(key) or "").strip()
        for key in STRATEGY_SECTION_CHAR_LIMITS
    }
    imported_fields: list[str] = []
    truncated_fields: list[str] = []
    for source_key, target_key in _SECTION_MAP.items():
        value = _plain_text(raw_sections.get(source_key))
        if value is not None:
            limit = STRATEGY_SECTION_CHAR_LIMITS[target_key]
            if len(value) > limit:
                truncated_fields.append(target_key)
            sections[target_key] = value[:limit]
            imported_fields.append(target_key)

    custom_prompt = _plain_text(ai_config.get("custom_prompt")) or _plain_text(source.get("custom_prompt"))
    if custom_prompt is not None:
        limit = STRATEGY_SECTION_CHAR_LIMITS["custom_prompt"]
        if len(custom_prompt) > limit:
            truncated_fields.append("custom_prompt")
        sections["custom_prompt"] = custom_prompt[:limit]
        imported_fields.append("custom_prompt")

    if not imported_fields:
        raise ValueError("NOFX_STRATEGY_PROMPT_EMPTY")
    for key, limit in STRATEGY_SECTION_CHAR_LIMITS.items():
        # Preserve deterministic text sent to Bonsai. Lengths are measured in
        # Python Unicode code points, matching the frontend counter.
        sections[key] = sections[key][:limit]
        if not sections[key]:
            raise ValueError(f"NOFX_STRATEGY_SECTION_EMPTY:{key}")

    indicators = _object(ai_config.get("indicators"))
    klines = _object(indicators.get("klines"))
    timeframe = str(klines.get("primary_timeframe") or "").strip().lower()
    if timeframe:
        if timeframe not in {"5m", "15m"}:
            raise ValueError("NOFX_TIMEFRAME_UNSUPPORTED")
        interval = 5 if timeframe == "5m" else 15
        imported_fields.append("signal_timeframe")
    else:
        current_profile = _object(current_strategy.get("profile"))
        current_timeframe = str(current_profile.get("signal_timeframe") or "").lower()
        interval = 5 if current_timeframe == "5m" else 15

    from .ai_strategy_book import TEMPLATES

    current_template_id = str(current_strategy.get("template_id") or "")
    matching_template = next(
        (
            item
            for item in TEMPLATES
            if str(item.get("id")) == current_template_id
            and int(item.get("scan_interval_minutes") or 15) == interval
        ),
        None,
    )
    template = matching_template or next(
        item for item in TEMPLATES if str(item.get("id")) == _FALLBACK_TEMPLATE_BY_INTERVAL[interval]
    )
    nofx_runtime = _nofx_runtime(
        source=source,
        ai_config=ai_config,
        signal_timeframe="5m" if interval == 5 else "15m",
        current_profile=_object(current_strategy.get("profile")),
    )
    imported_fields.extend(("context_timeframes", "indicator_switches", "gate_candidate_universe"))

    imported_name = _plain_text(requested_name) or wrapper_name or _plain_text(source.get("name"))
    if not imported_name:
        imported_name = f"NOFX · {str(current_strategy.get('name') or template['name']).strip()}"
    imported_name = imported_name[:80]

    ignored_fields = [
        "固定杠杆与仓位风控（沿用账户现有风险配置，并受 Gate 合约规则约束）",
        "NOFX 自有模型、API 密钥与外部数据源",
        "网格交易与 NOFX 订单执行参数",
        *nofx_runtime["unsupported_sources"],
    ]
    return {
        "name": imported_name,
        "sections": sections,
        "execution": dict(_object(current_strategy.get("execution"))),
        "template_id": str(template["id"]),
        "imported_fields": list(dict.fromkeys(imported_fields)),
        "truncated_fields": list(dict.fromkeys(truncated_fields)),
        "ignored_fields": ignored_fields,
        "scan_interval_minutes": interval,
        "nofx_runtime": nofx_runtime,
    }
