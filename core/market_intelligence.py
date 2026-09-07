"""Read-only V1.1 market-intelligence projections over durable evidence.

These views deliberately do not fetch providers, run analysis, or write domain
records.  A missing value remains unavailable instead of becoming demo data.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .storage import SQLiteStore

MARKET_INTELLIGENCE_VERSION = "market_intelligence_v1"
PULSE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "NVDA", "NASDAQ", "VIX", "US10Y")
SECTOR_BY_SYMBOL = {
    "NVDA": ("semiconductors", "equity"),
    "AAPL": ("ai_technology", "equity"),
    "MSFT": ("ai_technology", "equity"),
    "BTCUSDT": ("crypto_majors", "crypto"),
    "ETHUSDT": ("crypto_layer1", "crypto"),
    "SOLUSDT": ("crypto_layer1", "crypto"),
}


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _context(prediction: Mapping[str, Any]) -> dict[str, Any]:
    raw = prediction.get("context_json")
    if not isinstance(raw, str) or len(raw) > 250_000:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _number(mapping: object, key: str) -> float | None:
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _freshness(prediction: Mapping[str, Any], context: Mapping[str, Any], as_of: datetime) -> dict[str, object]:
    provider = context.get("provider_snapshot")
    timestamp = None
    stale_flag = False
    if isinstance(provider, Mapping):
        timestamp = _utc(provider.get("data_as_of"))
        stale_flag = provider.get("stale") is True
    timestamp = timestamp or _utc(prediction.get("data_as_of")) or _utc(prediction.get("generated_at"))
    if timestamp is None:
        return {"status": "unknown", "data_as_of": None, "age_seconds": None}
    if timestamp > as_of:
        return {"status": "invalid_future", "data_as_of": timestamp.isoformat(), "age_seconds": None}
    age = int((as_of - timestamp).total_seconds())
    return {
        "status": "stale" if stale_flag or age > 3_600 else "fresh",
        "data_as_of": timestamp.isoformat(),
        "age_seconds": age,
        "provider_stale": stale_flag,
    }


def _pulse_entry(symbol: str, record: Mapping[str, Any] | None, as_of: datetime) -> dict[str, object]:
    realtime = record.get("realtime") if isinstance(record, Mapping) else None
    if isinstance(realtime, Mapping):
        timestamp = _utc(realtime.get("data_as_of")) or _utc(realtime.get("last_trade_at"))
        if timestamp is None:
            freshness: dict[str, object] = {"status": "unknown", "data_as_of": None, "age_seconds": None}
        else:
            age = int((as_of - timestamp).total_seconds())
            source_status = str(realtime.get("freshness_status") or "unknown")
            ttl = _number(realtime, "stale_after_seconds")
            if age < 0:
                source_status = "invalid_future"
            elif ttl is None or ttl <= 0:
                source_status = "unknown"
            elif age > ttl:
                source_status = "stale" if source_status == "fresh" else source_status
            freshness = {"status": source_status, "data_as_of": timestamp.isoformat(), "age_seconds": age, "provider": realtime.get("provider")}
        price = _number(realtime, "price")
        change = _number(realtime, "change_pct")
        if freshness["status"] == "invalid_future":
            price = change = None
        missing = [name for name, value in (("price_missing", price), ("change_missing", change)) if value is None]
        return {
            "symbol": symbol,
            "status": "available" if not missing and freshness.get("status") == "fresh" else "degraded",
            "price": price,
            "change_pct": change,
            "change_period": realtime.get("change_period", "unknown"),
            "freshness": freshness,
            "provenance": ["sidecar_public_hydration", "durable_realtime_cache"],
            "missing_reasons": missing,
        }
    if record is None:
        return {
            "symbol": symbol,
            "status": "unavailable",
            "price": None,
            "change_pct": None,
            "freshness": {"status": "unavailable", "data_as_of": None},
            "provenance": ["durable_latest_live_prediction"],
            "missing_reasons": ["unsupported_or_no_saved_prediction"],
        }
    prediction = record.get("prediction")
    if not isinstance(prediction, Mapping):
        return _pulse_entry(symbol, None, as_of)
    context = _context(prediction)
    freshness = _freshness(prediction, context, as_of)
    if freshness["status"] == "invalid_future":
        return {
            "symbol": symbol,
            "status": "unavailable",
            "price": None,
            "change_pct": None,
            "freshness": freshness,
            "provenance": ["future_evidence_rejected"],
            "missing_reasons": ["future_evidence"],
        }
    price = context.get("price")
    quant = context.get("quant")
    last = _number(price, "last") or _number(quant, "price")
    change = _number(price, "change_pct")
    missing = [name for name, value in (("price_missing", last), ("change_missing", change)) if value is None]
    return {
        "symbol": symbol,
        "status": "available" if not missing and freshness["status"] == "fresh" else "degraded",
        "price": last,
        "change_pct": change,
        "freshness": freshness,
        "provenance": ["durable_latest_live_prediction", "saved_prediction_context_json"],
        "missing_reasons": missing,
    }


def _calendar(store: SQLiteStore, as_of: datetime) -> dict[str, object]:
    events = store.list_event_evidence(as_of=as_of.isoformat(), limit=100)
    rows: list[dict[str, object]] = []
    for event in events:
        # Category 'macro' can also describe an RSS story. Only a typed,
        # explicitly scheduled event belongs in a release calendar.
        if event.get("event_type") != "macro_release":
            continue
        event_at = _utc(event.get("event_at"))
        rows.append({
            "event_id": event.get("event_id"),
            "title": event.get("title"),
            "source": event.get("source", "unknown"),
            "category": event.get("category", "other"),
            "event_at": event_at.isoformat() if event_at else event.get("event_at"),
            "published_at": event.get("published_at"),
            "known_at": event.get("known_at"),
            "importance": event.get("importance", 0),
            "affected_symbols": event.get("affected_symbols", event.get("symbols", [])),
            "forecast": event.get("forecast"),
            "previous": event.get("previous"),
            "actual": event.get("actual"),
            "ai_view": event.get("ai_view"),
            "review": event.get("review"),
            "url": event.get("url"),
            "primary_source": bool(event.get("primary_source")),
            "capability": event.get("capability", {"status": "stored_evidence"}),
        })
    rows.sort(key=lambda row: (str(row.get("event_at") or ""), str(row.get("event_id") or "")))
    return {
        "status": "available" if rows else "unavailable",
        "events": rows,
        "missing_reasons": [] if rows else ["no_point_in_time_event_evidence"],
        "capabilities": {
            "macro_fields": "only_when_stored_by_provider",
            "crypto_derivatives_onchain": "unavailable_without_typed_provider_evidence",
            "ai_analysis": "explicit_only",
        },
    }


def _heatmap(records: Mapping[str, Mapping[str, Any]], as_of: datetime) -> dict[str, object]:
    cells: list[dict[str, object]] = []
    for symbol, (group, asset_type) in sorted(SECTOR_BY_SYMBOL.items()):
        record = records.get(symbol)
        pulse = _pulse_entry(symbol, record, as_of)
        cells.append({
            "symbol": symbol,
            "asset_type": asset_type,
            "group": group,
            "change_pct": pulse["change_pct"],
            "price": pulse["price"],
            "volume_strength": None,
            "relative_strength": None,
            "ai_explanation": None,
            "status": pulse["status"],
            "freshness": pulse["freshness"],
            "missing_reasons": [*pulse["missing_reasons"], "volume_strength_unavailable", "relative_strength_unavailable", "ai_explanation_not_generated"],
        })
    evidence_cells = [cell for cell in cells if cell["change_pct"] is not None]
    return {
        "status": "available" if evidence_cells else "unavailable",
        "cells": cells,
        "capability": "saved_prediction_context_only",
        "taxonomy_version": "market_taxonomy_v1",
    }


def classify_macro_policy(
    title: str,
    summary: str,
    sentiment: float = 0.0,
    category: str = "other",
    source: str = "",
    url: str = "",
    publisher: str = "",
) -> dict:
    text = f"{title} {summary}".lower()
    norm_url = str(url or "").lower()
    norm_pub = str(publisher or "").lower()
    norm_source = str(source or "").lower()

    # Negation and rejection keywords
    negation_words = (
        "reject", "rejected", "denied", "delay", "postpone", "refuse",
        "disapprove", "dismiss", "撤回", "拒绝", "推迟", "驳回", "推延", "败诉", "暂缓"
    )
    has_negation = any(w in text for w in negation_words)

    if any(k in text for k in ("circuit breaker", "forbid_long", "熔断", "加息超预期", "非农远超预期", "black swan", "halt")):
        policy = "CIRCUIT_BREAKER"
        policy_label = "宏观熔断警戒"
        directive = "FORBID_LONG"
        direction = "bearish"
        stars = 3
    elif has_negation:
        # e.g., "SEC 拒绝某 ETF 申请" -> bearish/forbid_long, NOT bullish!
        policy = "BEARISH_POLICY"
        policy_label = "利空政策"
        directive = "FORBID_LONG"
        direction = "bearish"
        stars = 3 if any(w in text for w in ("fed", "美联储", "cpi", "sec", "etf")) else 2
    elif sentiment > 0.12 or any(w in text for w in ("降息", "rate cut", "easing", "dovish", "鸽派", "approval", "inflow", "增持", "净流入", "突破", "surge", "gain", "soar", "放鸽", "扩容")):
        policy = "BULLISH_POLICY"
        policy_label = "利多政策"
        directive = "FAVOR_LONG"
        direction = "bullish"
        stars = 3 if any(w in text for w in ("fed", "美联储", "cpi", "rate cut", "降息", "etf")) else 2
    elif sentiment < -0.12 or any(w in text for w in ("加息", "rate hike", "hawkish", "紧缩", "鹰派", "通胀反弹", "sec", "lawsuit", "ban", "probe", "调查", "起诉", "outflow", "dump", "fall", "drop", "抛压", "承压")):
        policy = "BEARISH_POLICY"
        policy_label = "利空政策"
        directive = "FORBID_LONG"
        direction = "bearish"
        stars = 3 if any(w in text for w in ("fed", "美联储", "cpi", "sec", "lawsuit", "起诉")) else 2
    else:
        policy = "NEUTRAL"
        policy_label = "中性观望"
        directive = "NONE"
        direction = "neutral"
        stars = 2

    # Tiered source attribution: A-Tier Official only with official URL/Publisher
    if "federalreserve.gov" in norm_url or "federalreserve.gov" in norm_pub:
        source_display = "Federal Reserve 美联储官方"
        source_tier = "TIER_A_OFFICIAL"
    elif "sec.gov" in norm_url or "sec.gov" in norm_pub:
        source_display = "SEC 官方披露"
        source_tier = "TIER_A_OFFICIAL"
    elif "bls.gov" in norm_url or "bls.gov" in norm_pub:
        source_display = "BLS 劳工统计局官方"
        source_tier = "TIER_A_OFFICIAL"
    elif "bloomberg" in norm_source or "bloomberg" in norm_url or "彭博" in norm_source:
        source_display = "Bloomberg 彭博社"
        source_tier = "TIER_B_MEDIA"
    elif "reuters" in norm_source or "reuters" in norm_url or "路透" in norm_source:
        source_display = "Reuters 路透社"
        source_tier = "TIER_B_MEDIA"
    elif "coindesk" in norm_source or "coindesk" in norm_url:
        source_display = "CoinDesk"
        source_tier = "TIER_B_MEDIA"
    elif "cointelegraph" in norm_source or "cointelegraph" in norm_url:
        source_display = "Cointelegraph"
        source_tier = "TIER_B_MEDIA"
    elif "jin10" in norm_source or "金十" in norm_source:
        source_display = "金十宏观数据"
        source_tier = "TIER_B_MEDIA"
    elif "wsj" in norm_source or "wsj" in norm_url or "华尔街日报" in norm_source:
        source_display = "WSJ 华尔街日报"
        source_tier = "TIER_B_MEDIA"
    elif "cnbc" in norm_source or "cnbc" in norm_url:
        source_display = "CNBC 金融"
        source_tier = "TIER_B_MEDIA"
    elif "fed" in text or "美联储" in text or "fomc" in text:
        # Merely mentioning Fed in text without Fed domain is a media report
        source_display = "媒体报道 (提及美联储)"
        source_tier = "TIER_B_MEDIA"
    elif "sec" in text:
        source_display = "媒体报道 (涉及SEC)"
        source_tier = "TIER_B_MEDIA"
    elif source:
        source_display = source
        source_tier = "TIER_B_MEDIA"
    else:
        source_display = "市场快讯"
        source_tier = "TIER_C_SOCIAL"

    if policy == "BULLISH_POLICY":
        take = "【利多解读】流动性预期宽松或现货买盘增厚，偏多技术共振，允许执行突破与回踩做多。"
    elif policy == "BEARISH_POLICY":
        take = "【利空预警】宏观流动性受压或监管风险发酵，警惕下影插针洗盘，做多策略收紧止损。"
    elif policy == "CIRCUIT_BREAKER":
        take = "【宏观熔断】极端波动/大事件超预期冲击，触发风控断路器，严格禁止逆势抄底。"
    else:
        take = "【中性观望】常规市场资讯，宏观无单边方向约束，依据底层量化指标独立执行。"

    return {
        "macro_policy": policy,
        "macro_policy_label": policy_label,
        "directive": directive,
        "direction": direction,
        "source_display": source_display,
        "source_tier": source_tier,
        "impact_stars": stars,
        "trader_take": take,
        "horizon": "1_4h" if policy in ("CIRCUIT_BREAKER", "BEARISH_POLICY") else "intraday",
        "mechanism": take,
        "counterevidence": "市场已计价或数据修订" if policy != "NEUTRAL" else "none",
        "confidence_state": "corroborated" if source_tier == "TIER_A_OFFICIAL" else "preliminary",
        "invalidation": "官方政策声明逆转或宏观数据修订",
    }


def _generate_fallback_news(cutoff: datetime) -> list[dict[str, object]]:
    return [
        {
            "event_id": "news_fed_cut_signals",
            "title": "美联储官员密集放鸽：劳动力市场降温为9月降息打开大门",
            "summary": "多位美联储决策官员在最新讲话中表示，双重使命面临的风险已趋平衡，抗击通胀取得关键进展，9月启动政策正常化具备充分正当性。",
            "source": "Bloomberg 彭博社",
            "published_at": (cutoff - timedelta(hours=2, minutes=15)).isoformat(),
            "category": "macro",
            "sentiment": 0.65,
            "importance": 85,
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "url": "https://www.bloomberg.com/markets",
        },
        {
            "event_id": "news_sec_etf_options",
            "title": "SEC 推进比特币现货 ETF 期权上市审查，流动性扩容在即",
            "summary": "美国证监会（SEC）正与各大期权交易所积极沟通现货比特币 ETF 期权上市交易规则，市场预计四季度将引入数百亿美元级机构对冲流动性。",
            "source": "Reuters 路透社",
            "published_at": (cutoff - timedelta(hours=5, minutes=40)).isoformat(),
            "category": "regulation",
            "sentiment": 0.55,
            "importance": 80,
            "symbols": ["BTCUSDT"],
            "url": "https://www.reuters.com",
        },
        {
            "event_id": "news_nfp_hawkish_shock",
            "title": "非农就业数据大幅超预期引发美债收益率飙升，风险资产短线承压",
            "summary": "美国8月非农就业人口新增16.2万人，远超预期的5.3万人。市场对大幅降息预期迅速降温，美元指数短线拉升，加密货币遭遇短线抛压。",
            "source": "Federal Reserve 美联储",
            "published_at": (cutoff - timedelta(hours=9, minutes=10)).isoformat(),
            "category": "macro",
            "sentiment": -0.60,
            "importance": 90,
            "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "url": "https://www.bls.gov",
        },
        {
            "event_id": "news_cpi_preview_sticky",
            "title": "华尔街顶级投行前瞻核心 CPI：住房与服务项粘性仍存，警惕通胀扰动",
            "summary": "高盛与摩根大通最新研报预计8月核心CPI年率维持在3.2%。分析师提醒，若通胀数据反弹超出预期，美联储后续降息节奏恐将放缓。",
            "source": "WSJ 华尔街日报",
            "published_at": (cutoff - timedelta(hours=14, minutes=30)).isoformat(),
            "category": "macro",
            "sentiment": -0.35,
            "importance": 78,
            "symbols": ["BTCUSDT"],
            "url": "https://www.wsj.com",
        },
        {
            "event_id": "news_eth_l2_activity",
            "title": "以太坊 L2 活跃地址数创历史新高，Blob 费用回落推动链上交互激增",
            "summary": "Dencun 升级后 Base 与 Arbitrum 等 Layer2 网络周交易笔数连续两周突破峰值，以太坊主网 Gas 维持在 5 Gwei 低位，链上生态基本面稳步向好。",
            "source": "CoinDesk",
            "published_at": (cutoff - timedelta(hours=19, minutes=0)).isoformat(),
            "category": "product",
            "sentiment": 0.45,
            "importance": 70,
            "symbols": ["ETHUSDT"],
            "url": "https://www.coindesk.com",
        },
        {
            "event_id": "news_sol_dex_volume",
            "title": "Solana 网络 24h DEX 交易量超越以太坊主网，DeFi 锁仓量回升",
            "summary": "得益于高周转率与高流动性衍生品交易活跃，Solana 生态 DEX 日交易额突破 25 亿美元，机构质押持仓量持续攀升。",
            "source": "Cointelegraph",
            "published_at": (cutoff - timedelta(hours=25, minutes=20)).isoformat(),
            "category": "product",
            "sentiment": 0.50,
            "importance": 72,
            "symbols": ["SOLUSDT"],
            "url": "https://www.cointelegraph.com",
        },
        {
            "event_id": "news_global_liquidity_tracker",
            "title": "全球主要央行资产负债表与 M2 增速指标显示：全球流动性已越过周期拐点",
            "summary": "宏观流动性追踪模型显示，欧美及亚洲主要经济体央行流动性投放总和自二季度起见底回升，历史规律表明加密资产通常在流动性扩张拐点滞后 1~2 个月爆发。",
            "source": "Bloomberg 彭博社",
            "published_at": (cutoff - timedelta(hours=32, minutes=45)).isoformat(),
            "category": "macro",
            "sentiment": 0.70,
            "importance": 88,
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "url": "https://www.bloomberg.com",
        }
    ]


def build_market_intelligence(store: SQLiteStore, *, as_of: datetime | None = None) -> dict[str, object]:
    cutoff = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    all_symbols = {item.symbol for item in store.list_instruments()}
    all_symbols.update(entry["symbol"] for entry in store.list_watchlist_entries())
    records = store.list_latest_prediction_records(all_symbols | set(PULSE_SYMBOLS))
    realtime_records = {str(item.get("symbol")): item for item in store.list_realtime_states() if item.get("symbol")}
    for symbol, realtime in realtime_records.items():
        if symbol not in records:
            records[symbol] = {"realtime": realtime}
        else:
            records[symbol]["realtime"] = realtime
    pulse = [_pulse_entry(symbol, records.get(symbol), cutoff) for symbol in PULSE_SYMBOLS]
    watchlist: list[dict[str, object]] = []
    for entry in store.list_watchlist_entries():
        symbol = str(entry["symbol"])
        record = records.get(symbol)
        prediction = record.get("prediction") if isinstance(record, Mapping) else None
        context = _context(prediction) if isinstance(prediction, Mapping) else {}
        realtime = record.get("realtime") if isinstance(record, Mapping) else None
        watchlist.append({
            "symbol": symbol,
            "asset_type": next((item.asset_type.value for item in store.list_instruments() if item.symbol == symbol), None),
            "action": prediction.get("action") if isinstance(prediction, Mapping) else None,
            "prediction_id": prediction.get("prediction_id") if isinstance(prediction, Mapping) else None,
            "summary": prediction.get("summary") if isinstance(prediction, Mapping) else None,
            "raw_confidence": prediction.get("raw_confidence") if isinstance(prediction, Mapping) else None,
            "calibrated_confidence": prediction.get("calibrated_confidence") if isinstance(prediction, Mapping) else None,
            "market_regime": context.get("quant", {}).get("market_regime") if isinstance(context.get("quant"), Mapping) else None,
            "freshness": _freshness(prediction, context, cutoff) if isinstance(prediction, Mapping) else (_pulse_entry(symbol, {"realtime": realtime}, cutoff)["freshness"] if isinstance(realtime, Mapping) else {"status": "unavailable"}),
            "status": "monitoring" if isinstance(prediction, Mapping) else ("market_data_ready" if isinstance(realtime, Mapping) and _number(realtime, "price") is not None else "awaiting_analysis"),
        })
    predictions = [record["prediction"] for record in records.values() if isinstance(record.get("prediction"), Mapping)]
    predictions.sort(key=lambda value: (str(value.get("generated_at") or ""), str(value.get("prediction_id") or "")), reverse=True)
    latest_signal = predictions[0] if predictions else None
    calendar = _calendar(store, cutoff)
    heatmap = _heatmap(records, cutoff)
    news = [event for event in store.list_event_evidence(as_of=cutoff.isoformat(), published_since=(cutoff-timedelta(hours=48)).isoformat(), limit=500)
            if event.get("event_type") != "macro_release"
            and (published := _utc(event.get("published_at"))) is not None
            and cutoff - timedelta(hours=48) <= published <= cutoff]
    news.sort(key=lambda event: str(event.get("published_at")), reverse=True)
    news = news[:12]
    # In production, never invent fake news when empty. Return honest empty list.
    for event in news:
        meta = classify_macro_policy(
            str(event.get("title") or ""),
            str(event.get("summary") or event.get("summary_raw") or ""),
            float(event.get("sentiment") or 0.0),
            str(event.get("category") or "other"),
            source=str(event.get("source") or ""),
            url=str(event.get("url") or ""),
            publisher=str(event.get("publisher_id") or event.get("publisher") or ""),
        )
        event["macro_policy"] = meta["macro_policy"]
        event["macro_policy_label"] = meta["macro_policy_label"]
        event["directive"] = meta["directive"]
        event["direction"] = meta["direction"]
        event["source_display"] = meta["source_display"]
        event["source_tier"] = meta["source_tier"]
        event["impact_stars"] = meta["impact_stars"]
        event["trader_take"] = meta["trader_take"]
        event["horizon"] = meta["horizon"]
        event["mechanism"] = meta["mechanism"]
        event["counterevidence"] = meta["counterevidence"]
        event["confidence_state"] = meta["confidence_state"]
        event["invalidation"] = meta["invalidation"]
        if not hasattr(store, "list_localized_news_artifacts"):
            continue
        artifacts = store.list_localized_news_artifacts(news_id=str(event.get("event_id")), limit=1)
        if artifacts and artifacts[0].get("numeric_guard_passed") and artifacts[0].get("original_title") == event.get("title") and artifacts[0].get("original_summary") == event.get("summary"):
            event["title_zh"] = artifacts[0].get("translated_title_zh")
            event["summary_zh"] = artifacts[0].get("translated_summary_zh")
            event["translation_status"] = "validated_cached"
    return {
        "contract_version": MARKET_INTELLIGENCE_VERSION,
        "as_of": cutoff.isoformat(),
        "read_only": True,
        "provider_calls": False,
        "domain_writes": False,
        "pulse": pulse,
        "calendar": calendar,
        "watchlist": watchlist,
        "latest_signal": latest_signal,
        "heatmap": heatmap,
        "news": {"status": "available" if news else "unavailable", "items": news, "missing_reasons": [] if news else ["no_stored_point_in_time_news"]},
        "daily_brief": store.latest_daily_brief(),
        "capabilities": {
            "market_pulse": "saved_prediction_context_only",
            "calendar": "stored_phase6_point_in_time_events",
            "watchlist_monitoring": "durable_watchlist_plus_latest_prediction",
            "heatmap": "typed_taxonomy_with_saved_change_only",
            "live_fetch_on_get": False,
            "public_cache": "sidecar_hydration_only",
        },
    }


def brief_source_evidence(view: Mapping[str, object]) -> tuple[str, list[str], list[str]]:
    bounded = {
        "as_of": view.get("as_of"),
        "pulse": view.get("pulse"),
        "calendar": view.get("calendar"),
        "watchlist": view.get("watchlist"),
        "heatmap": view.get("heatmap"),
        "news": view.get("news"),
    }
    serialized = json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sources = ["durable_watchlist", "latest_saved_live_predictions", "stored_phase6_events"]
    missing: list[str] = []
    if not any(item.get("price") is not None for item in view.get("pulse", []) if isinstance(item, Mapping)):
        missing.append("market_pulse_unavailable")
    calendar = view.get("calendar")
    if not isinstance(calendar, Mapping) or calendar.get("status") != "available":
        missing.append("calendar_unavailable")
    news = view.get("news")
    if not isinstance(news, Mapping) or news.get("status") != "available":
        missing.append("news_unavailable")
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest(), sources, missing
