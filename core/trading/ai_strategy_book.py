"""Versioned, account-scoped trading instructions. Execution limits stay in code."""
from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json

from .autonomous_strategy import POLICY
from .strategy_execution import normalize_execution

DEFAULT_SECTIONS = {
    "role": "你是专注于加密货币合约的多空双向实战量化交易员。以已收盘 15m 为信号周期、1h 为宏观背景结构，结合全市场资金面与新闻情绪，捕捉高确定性趋势突破与箱体高抛低吸机会。执行严格的工业级风险控制与防插针保护，拒绝过度观望与分析瘫痪。",
    "frequency": "按配置周期定期扫描评估。已有持仓保护与防插针风控第一优先；当出现符合量化规则的高盈亏比技术结构时果断下单，严禁频繁追涨杀跌。",
    "entry_standards": "全天候多空自适应工业级交易规则（严格对齐 GitHub 成熟量化策略库标准）：\n"
    "1. 【单边趋势突破市（顺势策略）】：15m 收盘价放量突破唐奇安/布林挤压通道或站上 EMA20，且 1h 宏观同向共振；成交量 ≥ 过去 20 根均量的 1.25 倍。\n"
    "2. 【震荡整理箱体市（均值回归策略）】：采用结构边缘高抛低吸——在 15m support20 支撑位或布林下轨处**本轮立刻挂出**做多限价单（OPEN_LONG）；在 15m resistance20 阻力位或布林上轨处**本轮立刻挂出**做空限价单（OPEN_SHORT）。净盈亏比必须 ≥ 2.0。「预埋」是现在挂单等被动成交，不是等插针发生后再挂；未触达关键位正是挂限价预埋单的充分理由。\n"
    "3. 【工业级防插针止损硬约束（绝对底线）】：严禁设置紧贴市价的极窄自杀止损！止损必须依托 15m/1h 有效摆动分型（Swing High/Low）或订单块外侧：\n"
    "   - 主流标的（BTC/ETH 等）：止损距离必须 ≥ max(1.8×ATR, entry_price × 0.015)（至少 1.8 倍 ATR 且不低于入场价的 1.5%）；\n"
    "   - 高波动/山寨标的（SOL、DOGE 等）：止损距离必须 ≥ max(2.2×ATR, entry_price × 0.025)（至少 2.2 倍 ATR 且不低于入场价的 2.5%）；\n"
    "   - 任何小于该安全垫的止损提案将被风控引擎直接硬拦截（AI_STOP_DISTANCE_TOO_NARROW）。\n"
    "4. 【梯级止盈与保本推损】：目标盈亏比 ≥ 2.2R ~ 3.5R；当浮盈触及 1.5R 时触发保本推损（Break-Even Trailing），锁定胜局，防范极端插针利润回吐。\n"
    "5. 【新闻与宏观情绪】：中性新闻（NEUTRAL）属于标准有效交易背景；只要无直接同品种强力反向利空/利好，即按量化技术位坚定执行。",
    "decision_process": "横向扫描全市场候选标的；优先识别顺势突破或震荡边缘具备充裕防插针安全垫的标的；根据 ATR 严密计算宽幅止损与梯级止盈确保净盈亏比 ≥ 2.0；技术与风控指标达标时果断下单；按严格结构化 JSON 输出决策。",
    "custom_prompt": "拒绝分析瘫痪与过度等待。加密市场 70% 时间处于震荡整理，关键结构位的被动限价挂单正是成熟量化系统获取高盈亏比的基石！严禁以‘处于震荡整理/均线纠缠/新闻中性’为由机械式持续 WAIT！【预埋限价单纪律】：预埋 = 本轮立刻把限价单挂在关键支撑阻力位上等待被动成交，价格尚未到达关键位恰恰是挂预埋单的唯一理由。只有当按当前策略模板确实算不出净盈亏比 ≥ 2.0 的有效入场时，才允许 WAIT，并必须写明缺失的具体条件。严禁编造不存在的价格、指标或新闻。",
}

# Strategy profiles are deliberately data, not executable code.  The model may
# use these values to choose a setup, while the execution gateway continues to
# enforce the account's hard limits.  Keeping the profile versioned makes a
# later replay explain which rules were active for a decision.
STRATEGY_PROFILE_VERSION = "ai_strategy_pack_2026_09_v6_signal_timeframe"


def _profile(**values):
    return {"version": STRATEGY_PROFILE_VERSION, **values}


TEMPLATES = [
    {"id": "aggressive_impulse", "name": "闪电动量 · 5m 激进", "style": "AGGRESSIVE", "scan_interval_minutes": 5, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.25, "leverage": 100, "max_positions": 3, "max_margin_pct": 18.0, "max_notional_usdt": 2500.0, "min_confidence": 68, "min_net_rr": 1.8, "cooldown_minutes": 10, "order_preference": "AUTO", "scan_interval_minutes": 5, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": True},
     "profile": _profile(strategy_id="aggressive_impulse", family="MOMENTUM_LIQUIDITY_SWEEP", candidate_strategy_ids=["liquidity_sweep", "ema_trend", "bollinger_squeeze"], signal_timeframe="5m", context_timeframes=["15m", "1h"], required_confirmations=2, minimum_signal_score=3, volume_ratio_min=1.18, atr_stop_multiple=1.20, major_stop_floor_pct=0.60, alt_stop_floor_pct=1.20, minimum_net_rr=1.8, target_r_multiples=[2.0, 2.8], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=480, max_limit_distance_pct=0.85, allow_market_entry=True, market_min_trigger_completion=70, allow_future_limit=True, max_entries_per_hour=3, cooldown_minutes=10, news_mode="RISK_FILTER_WITH_SYMBOL_CATALYST"),
     "sections": {**DEFAULT_SECTIONS, "role": "你是专注于加密货币合约的多空双向实战交易员。以已收盘 5m 为触发周期、15m 与 1h 为结构背景，结合全市场资金面与新闻情绪，捕捉动量突破、流动性扫荡反转与关键位限价回测机会。执行固定风控与防插针保护，拒绝用固定置信度或笼统震荡描述代替证据。", "frequency": "由系统每 5 分钟对齐触发评估，信号周期为已收盘 5m，背景周期为 15m 与 1h。每轮先管理已有持仓，再从候选池中挑选唯一最优的单笔机会；宁可一笔做透，不要分散下单。",
      "entry_standards": "入场判定规则（动量放量突破 + 防插针宽幅安全垫）：\n"
      "1. 【强信号优先（status = PROPOSAL）】：策略规则已被客观触发，按 direction_bias 方向果断给出 OPEN_LONG / OPEN_SHORT。\n"
      "2. 【全 NO_TRIGGER 绝不等于必须 WAIT】：独立依据 5m 触发与 15m/1h 背景：(a) 动量突破——5m 放量站上/跌破 EMA20 或 resistance20 后优先在回测位挂单；(b) 流动性扫荡——5m 刺破 support20/resistance20 后收回结构内可反向开仓；(c) 箱体边缘——靠近 15m 支撑阻力时预埋限价单。\n"
      "3. 【防插针止损底线】：止损距离必须 ≥ max(1.8×ATR, entry_price × 0.015)（主流币不低于 1.5%，山寨币不低于 2.5%），必须设于有效摆动结构之外，严禁超窄止损！净盈亏比必须 ≥ 2.0，止盈建议 2.2R ~ 3.2R。\n"
      "4. 【新闻】：中性新闻正常交易，遇标的直接利好优先做多，直接利空优先做空。",
      "decision_process": "1. 检查已有持仓，确认止损与推损保护。\n2. 交叉核对 5m、15m 与 1h，选定唯一最优标的与方向，不得用固定分数代替证据。\n3. 以 ATR 计算结构止损，确保净盈亏比达标。\n4. 按严格 JSON 格式输出。",
      "custom_prompt": "自动预埋限价或市价进场。市价与限价开仓门槛均为 70%。完成度达到 70% 即可开单：当价格处于入场区内支持市价（MARKET）快速开仓；当需要回测或避免追高时，自动在突破回测位或 EMA20 回踩位挂出限价单（LIMIT），严禁在无持仓且完成度达 70% 时无故放弃开单选择 WAIT。不要无脑追求极端胜率，70% 完成度即可执行。"}},
    {"id": "aggressive_breakout", "name": "趋势回测 · 15m 激进", "style": "AGGRESSIVE", "scan_interval_minutes": 15, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.22, "leverage": 100, "max_positions": 3, "max_margin_pct": 16.0, "max_notional_usdt": 2200.0, "min_confidence": 70, "min_net_rr": 1.9, "cooldown_minutes": 20, "order_preference": "AUTO", "scan_interval_minutes": 15, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": True},
     "profile": _profile(strategy_id="aggressive_breakout", family="VOLATILITY_EXPANSION_RETEST", candidate_strategy_ids=["bollinger_squeeze", "ema_trend", "liquidity_sweep"], signal_timeframe="15m", context_timeframes=["1h"], required_confirmations=2, minimum_signal_score=3, volume_ratio_min=1.20, atr_stop_multiple=1.50, major_stop_floor_pct=0.80, alt_stop_floor_pct=1.50, minimum_net_rr=1.9, target_r_multiples=[2.1, 3.2], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=900, max_limit_distance_pct=0.95, allow_market_entry=True, market_min_trigger_completion=70, allow_future_limit=True, max_entries_per_hour=2, cooldown_minutes=20, news_mode="RISK_FILTER_WITH_SYMBOL_CATALYST"),
     "sections": {**DEFAULT_SECTIONS, "frequency": "由系统每 15 分钟评估。结合 1h 与 15m 已收盘 K 线，先检查已有持仓保护，自适应识别趋势跟踪或箱体高抛低吸机会。",
      "entry_standards": "全天候多空双模规则（限价为主，确认突破可用市价）：\n"
      "1. 【限价主路径】：趋势突破后在 resistance20/support20 回测位或 EMA20 回踩位立刻预埋挂单；限价距现价不得超过 0.95%，TTL 15 分钟。\n"
      "2. 【市价例外】：当 15m 收盘确认突破、量比达标、触发完成度 ≥70%、报价仍在 entry_zone 且盘口成本合格时允许 MARKET，禁止追涨杀跌。\n"
      "3. 【止损防插针保护】：止损必须 ≥ max(1.8×ATR, 1.5%)，严禁设在箱体内，必须设在支撑位下方/阻力位上方至少 0.5 ATR 安全缓冲处。\n"
      "4. 【新闻面】：中性新闻为标准交易背景，无重大反向利空利好即可执行。",
      "decision_process": "评估 1h 宏观与 15m 局部；不得追逐已经偏离结构位的价格；优先在突破回测位设定限价，满足市价例外条件才可即时成交；输出结构化 JSON。",
      "custom_prompt": "优先提交回测限价单或直接市价开仓。市价与限价开仓门槛均为 70%。不要无脑追求极端高胜率，满足 70% 触发完成度即可果断开仓或预埋限价单，不得无故 WAIT。"}},
    {"id": "conservative_pullback", "name": "顺势回踩 · 15m 稳健", "style": "CONSERVATIVE", "scan_interval_minutes": 15, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.15, "leverage": 100, "max_positions": 2, "max_margin_pct": 12.0, "max_notional_usdt": 1500.0, "min_confidence": 70, "min_net_rr": 2.0, "cooldown_minutes": 45, "order_preference": "AUTO", "scan_interval_minutes": 15, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": True},
     "profile": _profile(strategy_id="conservative_pullback", family="HTF_TREND_PULLBACK", candidate_strategy_ids=["ema_trend", "liquidity_sweep", "session_vwap"], signal_timeframe="15m", context_timeframes=["1h"], required_confirmations=3, minimum_signal_score=4, volume_ratio_min=1.05, atr_stop_multiple=1.80, major_stop_floor_pct=1.00, alt_stop_floor_pct=1.80, minimum_net_rr=2.0, target_r_multiples=[2.2, 3.2], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=1200, max_limit_distance_pct=0.80, allow_market_entry=True, market_min_trigger_completion=70, allow_future_limit=True, max_entries_per_hour=1, cooldown_minutes=45, news_mode="REVERSE_NEWS_VETO"),
     "sections": {**DEFAULT_SECTIONS, "frequency": "由系统每 15 分钟复核。以大周期顺势与小周期深度回踩为核心，追求极佳盈亏比。",
      "entry_standards": "顺势回踩规则（限价主路径）：\n"
      "1. 1h 方向明确后，在 15m EMA20、session VWAP 或前突破位 0.45% 内挂被动限价单；净盈亏比 ≥ 2.0。\n"
      "2. 只有回踩已经在收盘 K 线上确认、触发完成度 ≥94% 且盘口成本合格时才可市价；止损放在结构外并由固定风控复核。\n"
      "3. 【新闻背景】：中性新闻正常交易，无相反重大突发新闻即可执行。",
      "decision_process": "检查 1h 方向、15m 结构、量能与新闻否决；不得在方向冲突时开单；在最近可成交关键位设置限价，精算成本后盈亏比并输出标准 JSON。"}},
    {"id": "conservative_defense", "name": "多维防守 · 15m 保守", "style": "CONSERVATIVE", "scan_interval_minutes": 15, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.10, "leverage": 100, "max_positions": 2, "max_margin_pct": 10.0, "max_notional_usdt": 1000.0, "min_confidence": 70, "min_net_rr": 2.2, "cooldown_minutes": 75, "order_preference": "AUTO", "scan_interval_minutes": 15, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": True},
     "profile": _profile(strategy_id="conservative_defense", family="VWAP_FUNDING_MEAN_REVERSION", candidate_strategy_ids=["session_vwap", "funding_extreme", "liquidity_sweep"], signal_timeframe="15m", context_timeframes=["1h"], required_confirmations=3, minimum_signal_score=4, volume_ratio_min=1.00, atr_stop_multiple=2.00, major_stop_floor_pct=1.20, alt_stop_floor_pct=2.20, minimum_net_rr=2.2, target_r_multiples=[2.4, 3.6], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=1200, max_limit_distance_pct=0.75, allow_market_entry=True, market_min_trigger_completion=70, allow_future_limit=True, max_entries_per_hour=1, cooldown_minutes=75, news_mode="REVERSE_NEWS_VETO"),
     "sections": {**DEFAULT_SECTIONS, "frequency": "由系统每 15 分钟扫描。以资本保护和资金费率极值套利反转为第一目标，在关键技术位波段共振时进场。",
      "entry_standards": "多维共振保守守卫（资金费率极值 + VWAP 偏离 + 结构确认）：\n"
      "1. 1h、15m、资金费率/OI 至少三项共振，在距现价 0.75% 内的 VWAP/结构位预埋限价单，净盈亏比 ≥ 2.2。\n"
      "2. 市价用于触发完成度 ≥70%、收盘确认且盘口成本合格的即时反转；止损必须位于结构失效点外。\n"
      "3. 【新闻背景】：无重大反向利空利好，中性新闻为常规环境。",
      "decision_process": "核查资金费率、OI、VWAP 偏离、1h 方向与新闻否决；不得在少于三项共振时开单；优先挂限价并输出可审计 JSON。"}},
]


class AIStrategyBook:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ai_strategy_instructions (
                account_id TEXT NOT NULL, revision INTEGER NOT NULL,
                name TEXT NOT NULL, sections_json TEXT NOT NULL,
                updated_at TEXT NOT NULL, PRIMARY KEY(account_id, revision))""")

            columns = {row[1] for row in db.execute('PRAGMA table_info(ai_strategy_instructions)')}
            if 'execution_json' not in columns:
                db.execute("ALTER TABLE ai_strategy_instructions ADD COLUMN execution_json TEXT NOT NULL DEFAULT '{}'")
            if 'template_id' not in columns:
                db.execute("ALTER TABLE ai_strategy_instructions ADD COLUMN template_id TEXT")
            if 'style' not in columns:
                db.execute("ALTER TABLE ai_strategy_instructions ADD COLUMN style TEXT")
            if 'profile_json' not in columns:
                db.execute("ALTER TABLE ai_strategy_instructions ADD COLUMN profile_json TEXT NOT NULL DEFAULT '{}'")
            if 'nofx_runtime_json' not in columns:
                db.execute("ALTER TABLE ai_strategy_instructions ADD COLUMN nofx_runtime_json TEXT NOT NULL DEFAULT '{}'")

    @staticmethod
    def _template(template_id: str | None = None, *, name: str | None = None) -> dict:
        if template_id:
            for template in TEMPLATES:
                if template["id"] == str(template_id).strip():
                    return template
        if name:
            for template in TEMPLATES:
                if template["name"] == str(name).strip():
                    return template
        return TEMPLATES[2]

    @staticmethod
    def _json_object(value: object, fallback: dict) -> dict:
        if isinstance(value, dict) and value:
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict) and parsed:
                return parsed
        return dict(fallback)

    @staticmethod
    def _profile_signal_interval(profile: dict | None) -> int | None:
        """Return the scan cadence implied by a strategy profile.

        A template's signal timeframe is part of its executable contract.  A
        stale UI save must not leave a 15m strategy running a 5m evidence
        gate (or the reverse), because that marks otherwise usable cycles as
        blocked and teaches the model to emit the same safe WAIT repeatedly.
        """
        timeframe = str((profile or {}).get("signal_timeframe") or "").strip().lower()
        return {"5m": 5, "15m": 15}.get(timeframe)

    @staticmethod
    def _normalize_nofx_runtime(value: object) -> dict | None:
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("source") != "NOFX_IMPORT":
            raise ValueError("NOFX_RUNTIME_CONFIG_INVALID")
        timeframe = str(value.get("signal_timeframe") or "").strip().lower()
        if timeframe not in {"5m", "15m"}:
            raise ValueError("NOFX_TIMEFRAME_UNSUPPORTED")
        allowed_timeframes = {"5m", "15m", "1h", "1d"}
        raw_context = value.get("context_timeframes")
        context = list(dict.fromkeys(
            str(item).strip().lower()
            for item in raw_context
            if str(item).strip().lower() in allowed_timeframes and str(item).strip().lower() != timeframe
        )) if isinstance(raw_context, (list, tuple)) else []
        if len(context) > 4:
            raise ValueError("NOFX_CONTEXT_TIMEFRAMES_INVALID")
        source_rows = value.get("candidate_sources")
        candidate_sources = [str(item) for item in source_rows if item == "gate_active_usdt_perpetuals"][:2] if isinstance(source_rows, (list, tuple)) else []
        if not candidate_sources:
            raise ValueError("NOFX_CANDIDATE_SOURCE_UNSUPPORTED")
        raw_excluded = value.get("excluded_symbols")
        excluded = list(dict.fromkeys(
            str(item).strip().upper()
            for item in raw_excluded
            if isinstance(item, str) and item.strip() and len(item.strip()) <= 32 and item.strip().isalnum()
        ))[:200] if isinstance(raw_excluded, (list, tuple)) else []
        raw_unsupported = value.get("unsupported_sources")
        unsupported = [str(item).strip()[:180] for item in raw_unsupported if isinstance(item, str) and item.strip()][:24] if isinstance(raw_unsupported, (list, tuple)) else []

        raw_indicators = value.get("indicators") if isinstance(value.get("indicators"), dict) else {}
        definitions = {
            "raw_klines": (False, None, 2, 200, ()),
            "ema": (True, "periods", 2, 200, (20, 50)),
            "macd": (False, None, 2, 200, ()),
            "rsi": (True, "periods", 2, 100, (7, 14)),
            "atr": (True, "periods", 2, 100, (14,)),
            "bollinger": (True, "periods", 2, 200, (20,)),
            "volume": (False, None, 2, 200, ()),
            "open_interest": (False, "status", 2, 200, ()),
            "funding_rate": (False, "status", 2, 200, ()),
        }
        normalized_indicators: dict[str, dict] = {}
        for name, (has_periods, extra_key, minimum, maximum, defaults) in definitions.items():
            item = raw_indicators.get(name) if isinstance(raw_indicators.get(name), dict) else {}
            enabled = item.get("enabled") if isinstance(item.get("enabled"), bool) else False
            normalized = {"enabled": enabled}
            if has_periods:
                periods = item.get("periods")
                if not isinstance(periods, (list, tuple)):
                    periods = []
                bounded = []
                for raw_period in periods[:4]:
                    if isinstance(raw_period, bool):
                        continue
                    try:
                        period = int(raw_period)
                    except (TypeError, ValueError):
                        continue
                    if minimum <= period <= maximum and period not in bounded:
                        bounded.append(period)
                normalized["periods"] = bounded or list(defaults)
            if extra_key == "status":
                status = str(item.get("status") or ("DISABLED" if not enabled else "AVAILABLE")).upper()
                normalized["status"] = status if status in {"AVAILABLE", "UNAVAILABLE", "DISABLED", "CONFIGURED"} else "UNAVAILABLE"
            normalized_indicators[name] = normalized
        normalized_indicators["raw_klines"]["enabled"] = True
        return {
            "version": 1,
            "source": "NOFX_IMPORT",
            "signal_timeframe": timeframe,
            "context_timeframes": context,
            "indicators": normalized_indicators,
            "candidate_sources": candidate_sources,
            "excluded_symbols": excluded,
            "unsupported_sources": unsupported,
        }

    @staticmethod
    def _sections_for_template(template: dict, sections: dict) -> dict:
        """Remove known legacy prose that contradicts a selected template.

        Earlier saves could keep the previous template's frequency and entry
        wording while persisting the new profile.  That made the machine
        contract say 15m/LIMIT while the model saw 5m/MARKET instructions.
        Preserve the user's free-form supplement, but restore the built-in
        template text when the persisted frequency is an unambiguous conflict.
        """
        normalized = dict(sections or {})
        expected = int(template.get("scan_interval_minutes") or 15)
        frequency = str(normalized.get("frequency") or "")
        legacy_phrase = "每 5 分钟" if expected == 15 else "每 15 分钟"
        if legacy_phrase in frequency:
            canonical = deepcopy(template["sections"])
            custom_prompt = str(normalized.get("custom_prompt") or "").strip()
            if custom_prompt:
                canonical["custom_prompt"] = custom_prompt
            return canonical
        return normalized

    def active(self, account_id: str) -> dict:
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM ai_strategy_instructions WHERE account_id=? ORDER BY revision DESC LIMIT 1", (account_id,)).fetchone()
        default_template = TEMPLATES[2]
        result = {
            "account_id": account_id,
            "revision": 0,
            "name": default_template["name"],
            "template_id": default_template["id"],
            "style": default_template["style"],
            "profile": deepcopy(default_template["profile"]),
            "sections": dict(default_template["sections"]),
            "nofx_runtime": None,
            "updated_at": None,
        }
        migrate_builtin_execution = row is None
        if row:
            template = self._template(row["template_id"] if "template_id" in row.keys() else None, name=row["name"])
            stored_template_id = str(row["template_id"] or "").strip() if "template_id" in row.keys() else ""
            builtin_ids = {str(item["id"]) for item in TEMPLATES}
            builtin_names = {str(item["name"]) for item in TEMPLATES}
            is_builtin_template = stored_template_id in builtin_ids or (not stored_template_id and str(row["name"]) in builtin_names)
            stored_profile = self._json_object(row["profile_json"] if "profile_json" in row.keys() else None, template["profile"])
            migrate_builtin_execution = bool(
                is_builtin_template
                and str(stored_profile.get("version") or "") != str(template["profile"].get("version") or "")
            )
            result.update(
                revision=row["revision"],
                name=template["name"] if migrate_builtin_execution else row["name"],
                template_id=(row["template_id"] or template["id"]),
                style=(row["style"] or template["style"]),
                # Built-in profiles are versioned machine contracts.  Re-read
                # the current template values so a previously persisted row
                # cannot keep an obsolete allow_market_entry/order policy.
                profile=deepcopy(template["profile"]) if is_builtin_template else stored_profile,
                sections=(
                    deepcopy(template["sections"])
                    if migrate_builtin_execution
                    else self._sections_for_template(template, self._json_object(row["sections_json"], template["sections"]))
                ) if is_builtin_template else self._json_object(row["sections_json"], template["sections"]),
                updated_at=row["updated_at"],
            )
            stored_nofx_runtime = self._json_object(
                row["nofx_runtime_json"] if "nofx_runtime_json" in row.keys() else None,
                {},
            )
            try:
                result["nofx_runtime"] = self._normalize_nofx_runtime(stored_nofx_runtime) if stored_nofx_runtime else None
            except ValueError:
                # Preserve visibility of corrupt legacy data without allowing
                # it to change scheduling, symbols, or model inputs.
                result["nofx_runtime"] = None
                result["nofx_runtime_status"] = "INVALID_STORED_CONFIG"
            if result.get("nofx_runtime"):
                runtime = result["nofx_runtime"]
                result["profile"] = {
                    **dict(result.get("profile") or {}),
                    "signal_timeframe": runtime["signal_timeframe"],
                    "context_timeframes": list(runtime["context_timeframes"]),
                }
        raw_execution = self._json_object(row["execution_json"], {}) if row else None
        if row is None:
            raw_execution = {
                **deepcopy(default_template.get("execution_defaults") or {}),
                "scan_interval_minutes": default_template["scan_interval_minutes"],
                "order_preference": default_template["order_preference"],
            }
        else:
            # Rows written before template metadata existed were effectively
            # AUTO even when the selected template was a LIMIT strategy.  The
            # profile is the authoritative default for those rows so the UI,
            # prompt and gateway expose the same execution behavior.
            profile_preference = str((result.get("profile") or {}).get("order_preference") or "AUTO").upper()
            if profile_preference in {"MARKET", "LIMIT"}:
                raw_execution["order_preference"] = profile_preference
            if migrate_builtin_execution:
                # A profile-version change is a strategy migration.  Apply the
                 # new risk/execution defaults once so fixed symbol lists and
                 # short TTL values do not survive. Leverage is a user ceiling;
                 # actual leverage comes from Gate metadata and stop-distance risk.
                raw_execution = {**raw_execution, **deepcopy(template.get("execution_defaults") or {})}
        profile_interval = self._profile_signal_interval(result.get("profile"))
        if profile_interval is not None:
            # The selected template owns cadence.  Keep the persisted
            # execution view and the prompt/evidence frames in agreement.
            raw_execution["scan_interval_minutes"] = profile_interval
        runtime_interval = {"5m": 5, "15m": 15}.get(str((result.get("nofx_runtime") or {}).get("signal_timeframe") or ""))
        if runtime_interval is not None:
            raw_execution["scan_interval_minutes"] = runtime_interval
        result["execution"] = normalize_execution(raw_execution)
        result["digest"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return result

    def save(
        self,
        account_id: str,
        *,
        name: str,
        sections: dict,
        expected_revision: int,
        execution: dict | None = None,
        template_id: str | None = None,
        nofx_runtime: dict | None = None,
        replace_nofx_runtime: bool = False,
    ) -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("STRATEGY_NAME_INVALID")
        if set(sections) != set(DEFAULT_SECTIONS) or any(not isinstance(v, str) or not 1 <= len(v.strip()) <= 2000 for v in sections.values()):
            raise ValueError("STRATEGY_SECTIONS_INVALID")
        current = self.active(account_id)
        runtime = self._normalize_nofx_runtime(nofx_runtime) if replace_nofx_runtime else current.get("nofx_runtime")
        template = self._template(template_id or current.get("template_id"), name=name)
        if template_id is not None and template["id"] != str(template_id).strip():
            raise ValueError("STRATEGY_TEMPLATE_INVALID")
        sections = self._sections_for_template(template, sections)
        if execution is None:
            execution = {
                **current["execution"],
                **deepcopy(template.get("execution_defaults") or {}),
                "scan_interval_minutes": template["scan_interval_minutes"],
                "order_preference": template["order_preference"],
            }
        execution = normalize_execution(execution)
        profile = deepcopy(template["profile"])
        profile_preference = str(profile.get("order_preference") or "AUTO").upper()
        if bool(profile.get("limit_priority")):
            # Limit-first profiles use AUTO so the deterministic selector may
            # take a confirmed, liquid breakout at market while routing every
            # pullback/retest to a passive limit.  Persisting MARKET here would
            # silently disable the strategy's primary execution contract.
            execution["order_preference"] = "AUTO"
        elif profile_preference in {"MARKET", "LIMIT"}:
            # A built-in template owns its order mode.  The UI can still
            # expose the setting, but a stale/manual MARKET value must not
            # widen a LIMIT-only strategy at the gateway.
            execution["order_preference"] = profile_preference
        profile_interval = self._profile_signal_interval(profile)
        if profile_interval is not None:
            execution["scan_interval_minutes"] = profile_interval
        runtime_interval = {"5m": 5, "15m": 15}.get(str((runtime or {}).get("signal_timeframe") or ""))
        if runtime_interval is not None:
            execution["scan_interval_minutes"] = runtime_interval
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT COALESCE(MAX(revision),0) FROM ai_strategy_instructions WHERE account_id=?", (account_id,)).fetchone()[0]
            if revision != expected_revision:
                raise ValueError("STRATEGY_REVISION_CONFLICT")
            db.execute(
                """INSERT INTO ai_strategy_instructions(
                    account_id, revision, name, sections_json, updated_at,
                    execution_json, template_id, style, profile_json, nofx_runtime_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    account_id,
                    revision + 1,
                    name.strip(),
                    json.dumps(sections, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(execution, ensure_ascii=False),
                    template["id"],
                    template["style"],
                    json.dumps(profile, ensure_ascii=False),
                    json.dumps(runtime or {}, ensure_ascii=False),
                ),
            )
        return self.active(account_id)

    def view(self, account_id: str) -> dict:
        return {"active": self.active(account_id), "templates": TEMPLATES, "fixed_policy": dict(POLICY)}
