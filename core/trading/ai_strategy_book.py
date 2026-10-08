"""Versioned, account-scoped trading instructions. Execution limits stay in code."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from .autonomous_strategy import NOFX_GATE_POLICY, POLICY
from .entry_economics import effective_min_net_rr
from .strategy_execution import normalize_execution

DEFAULT_SECTIONS = {
    "role": "你是 Gate TestNet 的自主合约交易员；从真实行情、已有持仓与新闻中选择有依据的一笔机会。",
    "frequency": "按策略配置的周期扫描；先管理已有持仓，再比较允许标的。",
    "entry_standards": "依据已收盘K线和账户事实自主选择趋势、突破、回踩或反转机会；任何缺失数据都标为未知。",
    "decision_process": "自主给出 LONG、SHORT 或 WAIT；开仓时填写限价或市价、USDT 名义金额、杠杆、入场、止损和目标。限价优先，账户及交易所约束由程序复核。",
    "custom_prompt": "不要把单个指标或置信分数当作唯一门槛。新闻缺失不自动否决技术机会；证据不足则写明缺口。",
}

# Strategy profiles are deliberately data, not executable code.  The model may
# use these values to choose a setup, while the execution gateway continues to
# enforce the account's hard limits.  Keeping the profile versioned makes a
# later replay explain which rules were active for a decision.
STRATEGY_PROFILE_VERSION = "ai_strategy_pack_2026_10_v10_fixed_2000"


def _profile(**values):
    return {"version": STRATEGY_PROFILE_VERSION, **values}


TEMPLATES = [
    {"id": "aggressive_impulse", "name": "闪电动量 · 5m 激进", "style": "AGGRESSIVE", "scan_interval_minutes": 5, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.25, "leverage": 100, "max_positions": 3, "max_margin_pct": 18.0, "max_notional_usdt": 2500.0, "min_confidence": 68, "min_net_rr": 1.6, "cooldown_minutes": 5, "order_preference": "AUTO", "scan_interval_minutes": 5, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": False},
     "profile": _profile(strategy_id="aggressive_impulse", family="MOMENTUM_LIQUIDITY_SWEEP", candidate_strategy_ids=["liquidity_sweep", "ema_trend"], signal_timeframe="5m", context_timeframes=["15m", "1h"], required_confirmations=1, minimum_signal_score=2, volume_ratio_min=0.95, atr_stop_multiple=1.80, major_stop_floor_pct=0.60, alt_stop_floor_pct=1.50, minimum_net_rr=1.6, target_r_multiples=[1.8, 3.0], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=480, max_limit_distance_pct=0.85, allow_market_entry=True, market_min_trigger_completion=65, limit_min_trigger_completion=55, allow_future_limit=True, max_entries_per_hour=3, cooldown_minutes=5, news_mode="RISK_FILTER_WITH_SYMBOL_CATALYST"),
     "sections": {**DEFAULT_SECTIONS, "role": "你是专注于加密货币合约的多空双向实战交易员。以已收盘 5m 为触发周期、15m 与 1h 为结构背景，结合全市场资金面与新闻情绪，捕捉 EMA20 动量突破与流动性扫荡反转机会。执行防插针结构化宽止损与动态仓位自适应风控，拒绝死板百分比与过度等待。", "frequency": "由系统每 5 分钟对齐触发评估，信号周期为已收盘 5m，背景周期为 15m 与 1h。每轮先管理已有持仓，再从候选池中挑选唯一最优的单笔机会；宁可一笔做透，不要分散下单。",
      "entry_standards": "入场判定（5m 激进，限价优先）：\n"
      "1. 扫描器 PROPOSAL 是优先机会，按证据评估，不因固定分数直接开仓。\n"
      "2. NO_TRIGGER 非否决。自主比较已收盘 5m 的趋势延续、放量突破、EMA/局部结构回踩、区间边缘和流动性扫荡反转；一类形态成立即可，不强制每轮都回踩 EMA20 或等待新穿越。15m/1h 是背景风险，非机械同向门槛。\n"
      "3. 优先在可核验关键位挂非穿越被动限价，距现价≤0.85%；已收盘突破且报价、盘口和费用允许时可选市价，不追离结构过远的价格。现价取 market_snapshots.price；止损在摆动结构外且距离≥max(信号 ATR×1.8、入场价对应百分比底线)，按止损缩小仓位；费用后净 RR≥1.6，否则 WAIT。\n"
      "4. 美股开盘照常评估，突发重大反向新闻与异常盘口须重新核验；不捏造新闻或结构。",
      "decision_process": "先保护已有持仓；再比较真实 5m 突破延续、回踩与扫荡反转等机会，AI 自主选一笔最有依据的交易。说清选择的结构、失效价及新闻风险，优先被动限价；确有即时触发才市价。用摆动点、ATR 和费用后净 RR 复核，不满足风控就 WAIT；不得虚构证据，JSON 只填可执行字段与必要证据。",
      "custom_prompt": "不要把 EMA20 回踩当作唯一入场模式。已收盘 5m 若出现有量能和局部结构支持的趋势延续或突破，可独立形成机会；回踩、区间边缘和扫荡也可评估。根据新闻、市场报价与盘口，自主选择 LONG、SHORT 或 WAIT。限价优先且距现价≤0.85%，仅在确认后的可成交价格使用市价；止损取结构外侧与 ATR/百分比底线较宽者，费用后净 RR≥1.6。任何关键价格或证据无法确定时说明原因并 WAIT，不虚构。"}},
    {"id": "aggressive_breakout", "name": "趋势回测 · 15m 激进", "style": "AGGRESSIVE", "scan_interval_minutes": 15, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.22, "leverage": 100, "max_positions": 3, "max_margin_pct": 16.0, "max_notional_usdt": 2200.0, "min_confidence": 70, "min_net_rr": 1.6, "cooldown_minutes": 10, "order_preference": "AUTO", "scan_interval_minutes": 15, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": False},
     "profile": _profile(strategy_id="aggressive_breakout", family="VOLATILITY_EXPANSION_RETEST", candidate_strategy_ids=["ema_trend", "liquidity_sweep"], signal_timeframe="15m", context_timeframes=["1h"], required_confirmations=2, minimum_signal_score=3, volume_ratio_min=1.20, atr_stop_multiple=2.00, major_stop_floor_pct=1.80, alt_stop_floor_pct=2.80, minimum_net_rr=1.6, target_r_multiples=[1.8, 3.2], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=900, max_limit_distance_pct=0.95, allow_market_entry=True, market_min_trigger_completion=70, allow_future_limit=True, max_entries_per_hour=2, cooldown_minutes=10, news_mode="RISK_FILTER_WITH_SYMBOL_CATALYST"),
     "sections": {**DEFAULT_SECTIONS, "frequency": "由系统每 15 分钟评估。结合 1h 与 15m 已收盘 K 线，先检查已有持仓保护，自适应识别趋势跟踪或箱体高抛低吸机会。",
      "entry_standards": "全天候多空双模规则（限价为主，确认突破可用市价）：\n"
      "1. 【限价主路径】：趋势突破后在 resistance20/support20 回测位或 EMA20 回踩位立刻预埋挂单；限价距现价不得超过 0.95%，TTL 15 分钟。\n"
      "2. 【市价例外】：当 15m 收盘确认突破、量比达标、触发完成度 ≥70%、报价仍在 entry_zone 且盘口成本合格时允许 MARKET，禁止追涨杀跌。\n"
      "3. 【结构化宽止损哲学】：止损依托有效摆动结构设立合理宽止损，严禁贴脸窄止损。止损距离与开仓数量自动等比例缩放，确保单笔风险恒定。\n"
      "4. 【美股开盘与新闻面】：美股开市正常交易进场，以关键位限价单为主，无重大反向利空利好即可执行。",
      "decision_process": "评估 1h 宏观与 15m 局部；优先在突破回测位预埋限价，满足市价条件才可即时成交；不得在回测未确认时追价市价开单；输出结构化 JSON。",
      "custom_prompt": "每轮独立确认已收盘 15m 突破的方向、量能和 1h 背景；优先在突破位首次回测时预埋不穿越现价的限价单。市价仅用于已确认的延续行情、现价仍在入场区且盘口成本合格。用结构外止损、真实 ATR 和费用后的净盈亏比检查订单；若回测位过远、失效位不清楚或净盈亏比不足则 WAIT 并列明原因，不得虚构完成度。"}},
    {"id": "conservative_pullback", "name": "顺势回踩 · 15m 稳健", "style": "CONSERVATIVE", "scan_interval_minutes": 15, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.15, "leverage": 100, "max_positions": 2, "max_margin_pct": 12.0, "max_notional_usdt": 1500.0, "min_confidence": 70, "min_net_rr": 2.0, "cooldown_minutes": 45, "order_preference": "AUTO", "scan_interval_minutes": 15, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": False},
     "profile": _profile(strategy_id="conservative_pullback", family="HTF_TREND_PULLBACK", candidate_strategy_ids=["ema_trend", "liquidity_sweep", "session_vwap"], signal_timeframe="15m", context_timeframes=["1h"], required_confirmations=3, minimum_signal_score=4, volume_ratio_min=1.05, atr_stop_multiple=2.20, major_stop_floor_pct=2.00, alt_stop_floor_pct=3.00, minimum_net_rr=2.0, target_r_multiples=[2.0, 3.2], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=1200, max_limit_distance_pct=0.80, allow_market_entry=True, market_min_trigger_completion=70, allow_future_limit=True, max_entries_per_hour=1, cooldown_minutes=45, news_mode="REVERSE_NEWS_VETO"),
     "sections": {**DEFAULT_SECTIONS, "frequency": "由系统每 15 分钟复核。以大周期顺势与小周期深度回踩为核心，追求极佳盈亏比。",
      "entry_standards": "顺势回踩规则（限价主路径）：\n"
      "1. 1h 方向明确后，在 15m EMA20、session VWAP 或前突破位 0.45% 内挂被动限价单；净盈亏比 ≥ 2.0。\n"
      "2. 只有回踩已经在收盘 K 线上确认、触发完成度 ≥94% 且盘口成本合格时才可市价；止损放在结构外并由固定风控复核。\n"
      "3. 【新闻背景】：中性新闻正常交易，无相反重大突发新闻即可执行。",
      "decision_process": "检查 1h 方向、已收盘 15m 回踩、量能与新闻风险；顺势结构和失效位明确时，在可成交关键位预埋被动限价，只有回踩已确认且盘口合格才考虑市价。用真实 ATR、费用和止损距离核算净盈亏比及仓位；不得虚构缺失证据，缺少任一关键证据时 WAIT 并说明缺口，输出标准 JSON。"}},
    {"id": "conservative_defense", "name": "多维防守 · 15m 保守", "style": "CONSERVATIVE", "scan_interval_minutes": 15, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.10, "leverage": 100, "max_positions": 2, "max_margin_pct": 10.0, "max_notional_usdt": 1000.0, "min_confidence": 70, "min_net_rr": 2.2, "cooldown_minutes": 75, "order_preference": "AUTO", "scan_interval_minutes": 15, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": False},
     "profile": _profile(strategy_id="conservative_defense", family="VWAP_FUNDING_MEAN_REVERSION", candidate_strategy_ids=["session_vwap", "funding_extreme", "liquidity_sweep"], signal_timeframe="15m", context_timeframes=["1h"], required_confirmations=3, minimum_signal_score=4, volume_ratio_min=1.00, atr_stop_multiple=2.50, major_stop_floor_pct=2.20, alt_stop_floor_pct=3.50, minimum_net_rr=2.2, target_r_multiples=[2.2, 3.5], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=1200, max_limit_distance_pct=0.75, allow_market_entry=True, market_min_trigger_completion=70, allow_future_limit=True, max_entries_per_hour=1, cooldown_minutes=75, news_mode="REVERSE_NEWS_VETO"),
     "sections": {**DEFAULT_SECTIONS, "frequency": "由系统每 15 分钟扫描。以资本保护和资金费率极值套利反转为第一目标，在关键技术位波段共振时进场。",
      "entry_standards": "多维共振保守守卫（资金费率极值 + VWAP 偏离 + 结构确认）：\n"
      "1. 1h、15m、资金费率/OI 至少三项共振，在距现价 0.75% 内的 VWAP/结构位预埋限价单，净盈亏比 ≥ 2.2。\n"
      "2. 市价用于触发完成度 ≥70%、收盘确认且盘口成本合格的即时反转；止损必须位于结构失效点外。\n"
      "3. 【新闻背景】：无重大反向利空利好，中性新闻为常规环境。",
      "decision_process": "核查已核验的资金费率、OI、VWAP、已收盘 15m 结构、1h 背景与新闻风险；缺失的衍生品数据标 UNKNOWN，不得当作共振。三项可核验证据、结构失效位及净盈亏比均成立时优先挂被动限价；否则 WAIT 并列明缺口，输出可审计 JSON。"}},
    {"id": "price_action_structure", "name": "价格结构 · 15m PA", "style": "PRICE_ACTION", "scan_interval_minutes": 15, "order_preference": "AUTO",
     "execution_defaults": {"universe_mode": "ALL", "symbols": [], "risk_per_trade_pct": 0.15, "leverage": 100, "max_positions": 2, "max_margin_pct": 12.0, "max_notional_usdt": 1500.0, "min_confidence": 70, "min_net_rr": 2.0, "cooldown_minutes": 15, "order_preference": "AUTO", "scan_interval_minutes": 15, "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True, "us_open_defense_enabled": False},
     "profile": _profile(version="price_action_structure_v1", strategy_id="price_action_structure", family="PRICE_ACTION_STRUCTURE", candidate_strategy_ids=[], signal_timeframe="15m", context_timeframes=["1h"], minimum_net_rr=2.0, target_r_multiples=[2.0, 3.0], order_preference="AUTO", limit_priority=True, limit_ttl_seconds=900, max_limit_distance_pct=0.95, allow_market_entry=True, allow_future_limit=True, atr_stop_multiple=2.0, major_stop_floor_pct=1.8, alt_stop_floor_pct=2.8, max_entries_per_hour=2, cooldown_minutes=15, news_mode="RISK_FILTER_WITH_SYMBOL_CATALYST"),
     "sections": {**DEFAULT_SECTIONS, "role": "你是 Gate 合约账户的双向价格行为交易员；根据本轮已收盘价格结构、账户与交易条件，自主判断是否有值得执行的机会。", "frequency": "由系统每 15 分钟评估；信号周期为已收盘 15m，1h 只提供结构背景。每轮先管理已有仓位与本系统委托，再比较允许标的。",
      "entry_standards": "关注已确认的 swing high/low、当前收盘前的区间、收盘突破结构位（BOS）、扫流动性后回收，以及突破后的回测。技术摘要只呈现按时间可见的证据；这些结构是供 AI 判断的参考，不是必须同时出现的条件、分数或自动开仓信号。多空对称评估；价格结构清晰时可独立判断，不要求新闻必须存在。",
      "decision_process": "先保护本系统持仓与委托，再比较 15m 触发证据和 1h 背景。AI 自主判断方向、入场方式、USDT 名义金额、杠杆、止损和止盈；止损围绕观点失效位，目标结合费用、滑点和真实账户保证金决定。可用价格行为证据不足、现价位置不合适或账户条件不允许时说明原因并 WAIT；所有提案经同一 Gate 网关核验。",
      "custom_prompt": "technical_context.timeframes.*.price_action 是已收盘K线的有限结构摘要；as_of 是本轮证据截断。pivot_at 是拐点收盘，confirmation_bar_at 是右侧确认收盘；confirmed_at=max(右侧确认收盘,来源实际可用时间)。事件 bar_at 是事件K线收盘；晚到历史数据不得说成当时已知。prior_range 排除最新收盘K线。结构证据不是代码入场门槛；缺少新闻不否决技术机会。"}},
]

# One common decision/execution route, five distinct market hypotheses.  The
# older percentage-score, compulsory ATR/RR and market-entry prose conflicted
# with the model-led Gate route.  Keep the cadence and equity/margin settings
# while replacing only that obsolete instruction text.
_PREVIOUS_NOFX_STYLE_BRIEFS = {
    "aggressive_impulse": "5m 动量与扫荡反转：寻找已收盘突破、放量延续或假突破回收。15m/1h 只作背景；有一类清晰结构即可提出交易。",
    "aggressive_breakout": "15m 波动扩张：关注突破、首次回测及结构延续，并结合 1h 背景判断是否追价。",
    "conservative_pullback": "15m 顺势回踩：先确定 1h 方向，再观察关键位、均线或 VWAP 回踩是否有合理入场空间。",
    "conservative_defense": "15m 防守反转：比较价格、VWAP、资金费率及 OI；缺失的衍生品数据保持未知，不能当确认信号。",
}
# Recognize the prior built-in prompts for an in-place upgrade; user-written
# strategy text remains untouched.
_V7_NOFX_STYLE_BRIEFS = {
    "aggressive_impulse": "5m 快速动量：比较已收盘 K 线的突破延续、扫流动性后回收、短周期趋势重新加速。15m/1h 是环境信息，不要求逐项同向。选当轮证据最充分的一类形态；关键位附近优先预挂不穿越的限价单，错过入场区而动量仍在可用市价。先复核本系统挂单和持仓，失效挂单输出 CANCEL_ORDER；无机会时写清缺少哪根收盘 K 线或哪个可核验价位。",
    "aggressive_breakout": "15m 趋势扩张：比较区间突破、首次回踩和放量延续的质量，用 1h 结构判断是否仍有价格空间。突破后的回测位优先挂限价；收盘已确认且盘口支持即时成交时可市价。避免把旧的已失效突破当机会；若等待，给出突破/回测触发价与失效条件。",
    "conservative_pullback": "15m 顺势回踩：以已观察到的 1h 趋势为背景，选择结构支撑阻力、均线或 VWAP 附近的回踩与反弹。只在可解释的失效价和退出目标之间有足够扣费后空间时挂限价；趋势破坏则等待或管理已有仓位。新闻仅在确有相关性时改变判断，不因资讯空白一律等待。",
    "conservative_defense": "15m 防守交易：比较区间边缘、VWAP 偏离、资金费率与 OI 的真实证据，选择拥挤交易的失效或价格吸收后的反转。衍生品数据缺失时只能用真实价格结构，不可声称共振。优先关键位被动限价，空间不足则等待并标出下一触发价。已有持仓优先更新保护价；本系统旧委托失效时先撤单核验。",
}
# NoFX docs/prompt-guide.zh-CN.md supplies the aggressive/conservative prompt
# rules.  Its Claw402/Hyperliquid direction board has no Gate equivalent here.
_NOFX_STYLE_BRIEFS = {
    "aggressive_impulse": (
        "NoFX 激进模板 · 5m 短线动量变体：优先比较趋势启动、关键位突破、异常放量和短时超买超卖后的反转，"
        "而不是只等待 EMA20 回踩。先检查本系统持仓和委托，再用已收盘 5m 价格结构、量能及 15m/1h 背景判断多空；"
        "一组清晰的价格与成交证据即可提出机会，背景矛盾要解释，不机械否决。短线机会优先在关键价位预挂被动限价；"
        "已确认启动且盘口支持即时成交时可市价。AI 根据失效位、费用和可用保证金自主决定金额、止盈止损和持仓时间。"
    ),
    "aggressive_breakout": (
        "NoFX 激进模板 · 15m 趋势突破变体：主动寻找支撑阻力突破、快速涨跌启动与成交量扩张；"
        "用 1h 趋势和 BTC 市场状态判断行情空间，但不把 BTC 同向当作绝对门槛。比较突破延续与首次回踩，"
        "优先在突破位或回踩位挂限价；只有已收盘确认且盘口流动性、费用和价格位置仍合适时才选市价。"
        "每轮复核已有仓位、过期委托、失效价和实际收益，避免反复追单或无依据地提前平仓。"
    ),
    "conservative_pullback": (
        "NoFX 保守模板 · 15m 顺势回踩变体：以稳定的扣费后收益和可控回撤为目标。先判断 1h 趋势，"
        "再看 15m 是否回到结构支撑阻力、EMA 或 VWAP 附近，并核对量能、K 线收盘与相关消息是否相互支持。"
        "只在失效价清晰、目标空间合理时预挂限价；若趋势已破坏或证据冲突，管理已有仓位并等待。"
        "持仓期间尊重原退出计划，有新证据才调整保护价，避免因短时噪声频繁进出。"
    ),
    "conservative_defense": (
        "NoFX 保守模板 · 15m 防守反转变体：优先保护已有持仓，寻找区间边缘、VWAP 偏离后回归或拥挤仓位失效的机会。"
        "同时核对真实价格、量能、OI、资金费率与新闻；缺失的数据标未知，不能假称多指标共振。"
        "只有方向、失效价和扣费后目标可解释时在关键位挂被动限价；否则列出缺口与下一触发价。"
        "减少横盘中反复试单，对本系统失效委托先撤单核验，再考虑新方案。"
    ),
    "price_action_structure": (
        "价格结构 · 15m 双向策略：以已确认 swing high/low、收盘 BOS、扫流动性回收和突破回测为证据，"
        "结合 1h 背景、实际报价与账户事实。形态只供 AI 自主比较，不设置形态分数或必备组合；"
        "可在无相关新闻时依据充分的技术证据决策，AI 自主选择金额、杠杆、入场及保护价。"
    ),
}
# Match exact prior built-in text when reading existing saved strategies.
# Execution amounts, margin settings and operator-authored supplements stay intact.
_V8_NOFX_STYLE_BRIEFS = dict(_NOFX_STYLE_BRIEFS)
_NOFX_STYLE_BRIEFS = {
    "aggressive_impulse": (
        "NoFX 激进风格 / Gemini High · 5m 动量：比较已收盘突破延续、趋势再加速与扫荡回收，选最清晰的一类，"
        "不把 EMA20 回踩、新闻存在或背景同向当作必备组合。用 15m/1h 解释空间与反证；辨别启动与涨跌末端，"
        "关键位优先预挂限价，不等触价后才申请。价格已启动且即时成交的净收益空间优于等待时可市价；"
        "延续证据不足、追价使目标空间耗尽或账户容量不足时等待，并注明具体触发价/条件。"
    ),
    "aggressive_breakout": (
        "NoFX 激进风格 / Gemini High · 15m 扩张：识别已收盘区间突破、首次回测与趋势延续；"
        "比较突破方向的成交证据和假突破反证，1h/BTC 是背景而非机械同向门槛。首次回测优先限价；"
        "收盘确认且价位、流动性与扣费后空间仍合理时可市价。不能反复交易已失效的旧突破；"
        "有系统挂单时先判断保留或撤销，未核验撤单成功前不把其保证金视为可用。"
    ),
    "conservative_pullback": (
        "NoFX 保守风格 / Gemini High · 15m 顺势回踩：依据 1h 已收盘结构判断趋势，"
        "比较支撑阻力、EMA 或 VWAP 回踩后的拒绝/重新接受与趋势破坏证据。优先关键位限价，"
        "明确失效位与扣费后目标空间，再按账户资金配置金额和杠杆。不要为高胜率假设牺牲亏盈结构；"
        "短时噪声不自动触发平仓，新的结构证据才支持调整保护或退出。新闻空白不单独否决技术机会。"
    ),
    "conservative_defense": (
        "NoFX 保守风格 / Gemini High · 15m 防守反转：先管理系统持仓，比较区间边缘拒绝、"
        "VWAP 偏离回归与拥挤仓位失效。区分逆势猜顶底与已收盘的扫荡回收/吸收证据；"
        "OI、资金费率、CVD 只有真实且新鲜时才辅助确认，缺失保持未知，可凭充分价格结构判断。"
        "避免横盘重复试单；关键位限价优先，失效位或扣费后空间不清楚时列明缺口和下一触发条件。"
    ),
    "price_action_structure": (
        "价格行为 / Gemini High · 15m 价格结构：比较已确认 swing high/low、收盘 BOS、"
        "扫流动性回收与突破回测；1h 提供背景，不设置形态分数或必备组合。"
        "所有证据必须在 as_of 时已知，区间参考排除最新收盘；无相关新闻时也可依充分技术证据判断。"
        "给出支持观点的结构和最强反证，自主配置金额、杠杆、入场及保护价；"
        "不能把未确认拐点、正在形成的K线或晚到历史数据当成已知触发。"
    ),
}

# Retain the exact old default for read-only migration of built-in saved rows.
# Operator-authored text is never replaced by similarity or a version guess.
_V10_NOFX_STYLE_BRIEFS = dict(_NOFX_STYLE_BRIEFS)
_NOFX_STYLE_BRIEFS["price_action_structure"] = (
    "价格行为 / Gemini High：先核查可见新闻是否有新事件、相对预期异动或标的催化，核对来源、发布时间/可用时间和价格反应；"
    "旧闻/重复报道不算新催化，无新闻或档案缺失标UNKNOWN而非没有异动，不因新闻空白自动否决技术机会。"
    "再用多根已收盘4h和1h的swing高低点、BOS、区间与突破后跟随证据判断"
    "趋势、震荡或过渡；在timeframe_analysis分别解释4h/1h，matched_conditions写明环境、交易类型与最强反证。"
    "4h看主结构，1h看当前推进/回调；冲突时解释层级，不机械要求同向。15m用连续K线选择入场，15分钟是扫描间隔而非单根K线决策。"
    "震荡：核验上下边界多次拒绝与区间仍有效，下沿拒绝/扫荡收回做多，上沿拒绝/扫荡收回做空；"
    "区间中部不机械高空低多，收盘突破后持续接受或边界失效则停止旧区间反向交易。"
    "趋势：高低点抬升优先多，降低优先空；结合突破后收盘跟随、回调深度与关键位保持判断强弱，"
    "选择顺势回踩或突破回测，已确认延续且成本/位置合理可市价，不为等待完美回测错失全部机会；"
    "不能把涨跌末端追价或逆势猜顶底当顺势。过渡：区分突破接受与假突破回收；证据不明才WAIT并明确触发/失效条件。"
    "止损在观点失效位，止盈依据区间对侧或下一结构位；有效趋势允许随新结构调整保护并延续利润，"
    "不微利抢平、不拖亏，不为了净盈亏比虚构目标。未知证据不得冒充确认；形态不自动保证盈利。"
)

# Preserve the exact v32 default for read-only migration; custom text stays intact.
_V32_NOFX_STYLE_BRIEFS = dict(_NOFX_STYLE_BRIEFS)
_NOFX_STYLE_BRIEFS["price_action_structure"] = (
    "价格行为 / Gemini High：先核查新闻新事件、相对预期异动、来源可用时间及价格反应；"
    "旧闻不算新催化，新闻档案缺失标UNKNOWN，不因新闻空白自动否决技术机会。"
    "15m是主要交易周期：用多根已收盘K线、已确认swing高低点、BOS、区间和突破跟随判断趋势、震荡或过渡；"
    "1h/4h只提供背景方向、重要支撑阻力与目标空间，不替代15m判断，不机械要求同向。"
    "5m是入场周期：在15m交易位置和方向成立后，用连续已收盘5m的拒绝、扫荡收回、突破接受或回测确认选择入场；"
    "单根5m涨跌不改变15m环境，不把15分钟扫描间隔当单根K线分析。"
    "震荡：核验15m上下边界反复拒绝，下沿5m拒绝/扫荡收回做多，上沿对应做空；"
    "区间中部不机械入场，突破持续接受则停止旧区间反向交易。"
    "趋势：15m高低点抬升优先多，降低优先空；结合跟随强弱、回调深度和关键位保持，"
    "用5m顺势回踩或突破回测入场，已确认延续且成本/位置合理可市价，不强等完美回测或追涨跌末端。"
    "过渡：区分有效突破与假突破回收，证据不明才WAIT并明确下一触发/失效。"
    "止损依据交易观点失效，5m触发的噪声不能冒充15m结构失效；止盈依据15m结构目标并参考1h/4h空间。"
    "结构有效允许调整保护延续利润，不微利抢平、不拖亏，不虚构远端目标。"
    "timeframe_analysis分别解释15m环境、5m入场及1h/4h背景，matched_conditions说明交易类型与最强反证。"
    "未知证据不冒充确认；形态不保证盈利。"
)

# Experimental PA seed authored by the verified Gemini review of the complete
# v25 optimization ledger, supplemented by the operator's explicit regime
# framework. Independent profitability remains unproven.
_PA_RESEARCH_SEED_INSTRUCTION = (
    "BOS回测与扫荡收回分别评估；bar_at及OHLC触位且收在突破方向仅证明基础回测发生，不能说成未触价或完整入场确认。"
    "WAIT须明确缺口和前置触发，不无依据移动目标。费用后空间不足时不编造远端目标。"
    "每笔固定2000USDT名义目标、AUTO订单，AI选杠杆受Gate、账户及保证金限制；"
    "基础回测不是完整入场信号，不触价盲入、不静默缩仓。"
    "结构失效及时撤单或退出；结构有效时留出利润空间，不微利抢平、不拖亏、不强行开仓。"
)

for _template_item in TEMPLATES:
    if _template_item["id"] == "price_action_structure":
        _template_item["profile"].update(
            version="price_action_structure_v3_main15_entry5", signal_timeframe="15m",
            entry_timeframe="5m", context_timeframes=["1h", "4h"],
        )
    _template_item["profile"]["prompt_version"] = STRATEGY_PROFILE_VERSION
    _template_item["execution_defaults"].update(
        sizing_mode="FIXED_NOTIONAL", fixed_notional_usdt=2000.0,
        max_notional_usdt=2000.0, leverage_mode="VENUE_LIMIT",
    )
    _brief = _NOFX_STYLE_BRIEFS[_template_item["id"]]
    _custom_prompt = (
        "as_of 是本轮证据截断。confirmed_swings.pivot_at 是拐点收盘，confirmation_bar_at 是右侧确认收盘；available_at 为来源最晚可用点，"
        "若与 confirmed_at 相同可省略；confirmed_at=max(确认收盘,可用点)。prior_range 排除最新收盘。事件 bar_at 是事件K线收盘，"
        "confirmed_at 是实际可知点；晚到数据不得说成当时已知。引用事实不可晚于 as_of。"
        if _template_item["id"] == "price_action_structure" else
        "不要把某个固定指标或置信分数当作唯一开仓门槛。仅引用本轮可核验的已收盘 K 线、盘口、持仓和新闻；数据缺失保持未知。只管理本系统能核对订单 ID 的挂单。"
    )
    _decision_process = (
        "先核对账户事实和系统持仓/挂单，再比较本轮候选的机会及最强反证，只输出一项动作。"
        "先定失效价和扣费后目标，每笔开仓名义价值固定2000 USDT；根据权益、可用保证金、总占用和交易所上限自主选杠杆。"
        "挂单、提案不等于成交；撤单成功以回读为准。持仓必须带止盈止损，有新证据可动态调整。"
        "WAIT说明具体缺口与下一触发价/条件；不重复旧理由、不把置信分数当胜率，不强行交易。"
    )
    if _template_item["id"] == "price_action_structure":
        _decision_process = (
            "先核对账户、系统持仓/挂单，再查新闻新事件/预期异动及价格反应；"
            "随后用多根已收盘15m判断主要趋势/震荡/过渡，1h/4h只评估背景与空间，再用5m确认入场，解释最强反证。"
            "新闻未知不伪造，无异动技术机会仍可交易。每轮只输出一项动作。"
            "止损按观点失效，目标按真实结构和扣费后空间，不微利抢平、不拖亏。"
            "每笔固定2000 USDT名义目标；AI杠杆受账户保证金及Gate约束，资金不足明确拒绝。"
            "WAIT写明缺口/下一触发；已有结构仍有效可持有或更新保护，失效撤单/退出，回执才证明执行。"
        )
    _template_item["sections"] = {
        "role": "你是 Gemini High 驱动的 Gate 双向自主交易员；目标是扣费后净收益和回撤质量，判断只基于本轮可核验证据。",
        "frequency": f"每 {_template_item['scan_interval_minutes']} 分钟扫描一次；先管理已有持仓，再比较允许标的。",
        "entry_standards": _brief,
        "decision_process": _decision_process,
        "custom_prompt": _custom_prompt,
    }


# Legacy definitions remain for reading immutable historical experiments only.
# New production selection and defaults expose the operator's single PA strategy.
ACTIVE_TEMPLATE_IDS = ("price_action_structure",)
ACTIVE_TEMPLATES = [item for item in TEMPLATES if item["id"] in ACTIVE_TEMPLATE_IDS]


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
        return ACTIVE_TEMPLATES[0]

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
        template_id = str(template.get("id"))
        if str(normalized.get("entry_standards") or "") in {
            _PREVIOUS_NOFX_STYLE_BRIEFS.get(template_id),
            _V7_NOFX_STYLE_BRIEFS.get(template_id),
            _V8_NOFX_STYLE_BRIEFS.get(template_id),
            _V10_NOFX_STYLE_BRIEFS.get(template_id),
            _V32_NOFX_STYLE_BRIEFS.get(template_id),
        }:
            # Upgrade only the unmodified built-in brief.  User-authored
            # strategy prose remains authoritative and never gets replaced
            # merely because the application version changed.
            canonical = deepcopy(template["sections"])
            if normalized.get("custom_prompt") and normalized["custom_prompt"] not in {
                "不要把某个固定指标或置信分数当作唯一开仓门槛。以可核验的 K 线和资金流证据做决定，新闻缺失不自动否决技术机会；证据不足则说明具体缺口。",
                "不要把某个固定指标或置信分数当作唯一开仓门槛。仅引用本轮可核验的已收盘 K 线、盘口、持仓和新闻；数据缺失保持未知。只管理本系统能核对订单 ID 的挂单。",
            }:
                canonical["custom_prompt"] = normalized["custom_prompt"]
            return canonical
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
        default_template = ACTIVE_TEMPLATES[0]
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
                name=row["name"],
                template_id=(row["template_id"] or template["id"]),
                style=(row["style"] or template["style"]),
                # Built-in profiles are versioned machine contracts.  Re-read
                # the current template values so a previously persisted row
                # cannot keep an obsolete allow_market_entry/order policy.
                profile=deepcopy(template["profile"]) if is_builtin_template else stored_profile,
                sections=(
                    self._sections_for_template(template, self._json_object(row["sections_json"], template["sections"]))
                    if is_builtin_template else self._json_object(row["sections_json"], template["sections"])
                ),
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
                if result["template_id"] == "price_action_structure":
                    frames = list(dict.fromkeys([
                        *[tf for tf in runtime["context_timeframes"] if tf not in {"5m", "15m"}], "1h", "4h",
                    ]))
                    result["nofx_runtime"] = {**runtime, "signal_timeframe": "15m", "context_timeframes": ["5m", *frames]}
                    result["profile"]["signal_timeframe"] = "15m"
                    result["profile"]["entry_timeframe"] = "5m"
                    result["profile"]["context_timeframes"] = frames
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
                # The NoFX prompt update must not reset the operator's saved
                # leverage, symbol universe, or margin ceiling on Gate Live.
                raw_execution = {**deepcopy(template.get("execution_defaults") or {}), **raw_execution}
            if is_builtin_template and stored_profile.get("prompt_version") != STRATEGY_PROFILE_VERSION:
                # Explicit operator update; retain the saved margin budget.
                raw_execution.update(sizing_mode="FIXED_NOTIONAL", fixed_notional_usdt=2000.0,
                                     max_notional_usdt=2000.0, leverage_mode="VENUE_LIMIT")
        profile_interval = self._profile_signal_interval(result.get("profile"))
        if profile_interval is not None:
            # The selected template owns cadence.  Keep the persisted
            # execution view and the prompt/evidence frames in agreement.
            raw_execution["scan_interval_minutes"] = profile_interval
        runtime_interval = {"5m": 5, "15m": 15}.get(str((result.get("nofx_runtime") or {}).get("signal_timeframe") or ""))
        if runtime_interval is not None:
            raw_execution["scan_interval_minutes"] = runtime_interval
        result["execution"] = normalize_execution(raw_execution)
        if result['template_id'] not in ACTIVE_TEMPLATE_IDS:
            # Explicit operator specialization: preserve stored revision and
            # account limits, but do not execute retired instructions/runtime.
            # No live DB rewrite or session start occurs in this read path.
            result['retired_template_id'] = result['template_id']
            result['specialization_status'] = 'PRICE_ACTION_EFFECTIVE_PROJECTION_NOT_DB_REWRITE'
            result.update(template_id=default_template['id'], name=default_template['name'],
                          style=default_template['style'], profile=deepcopy(default_template['profile']),
                          sections=deepcopy(default_template['sections']), nofx_runtime=None)
            result['execution'].update(scan_interval_minutes=15, order_preference='AUTO',
                                       sizing_mode='FIXED_NOTIONAL', fixed_notional_usdt=2000.0,
                                       max_notional_usdt=2000.0, leverage_mode='VENUE_LIMIT')
            result['execution'] = normalize_execution(result['execution'])
        result["execution"]["min_net_rr"] = float(effective_min_net_rr(
            result.get("template_id"), result["execution"].get("min_net_rr"),
        ))
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
        if template['id'] not in ACTIVE_TEMPLATE_IDS:
            raise ValueError("STRATEGY_TEMPLATE_RETIRED")
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
        execution.update(sizing_mode='FIXED_NOTIONAL', fixed_notional_usdt=2000.0,
                         max_notional_usdt=2000.0, leverage_mode='VENUE_LIMIT')
        profile = deepcopy(template["profile"])
        execution["min_net_rr"] = float(effective_min_net_rr(
            template["id"], execution.get("min_net_rr"),
        ))
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
            # Decision records already snapshot the strategy used for each
            # cycle.  The strategy library itself keeps only the latest
            # executable instruction set, as requested by the operator.
            db.execute("DELETE FROM ai_strategy_instructions WHERE account_id=?", (account_id,))
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
        active = self.active(account_id)
        policy = (
            {
                **NOFX_GATE_POLICY,
                **{key: active["execution"][key] for key in (
                    "leverage", "margin_cap_mode", "max_margin_pct",
                    "max_margin_usdt", "max_notional_usdt",
                )},
            }
            if str(account_id).lower() in {"gate_testnet", "gate_live"} else dict(POLICY)
        )
        return {"active": active, "templates": ACTIVE_TEMPLATES, "fixed_policy": policy}
