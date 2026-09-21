"""Standardized schemas for AI Market Analyst V2.

Mandates the 13-point institutional trading analysis output and strict tool schemas.
Prohibits unbacked BUY/SELL verdicts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class ScenarioPlan:
    hypothesis: str
    trigger_condition: str
    entry_zone: List[float]
    stop_loss: float
    take_profit: List[float]
    invalidation: str


@dataclass
class MarketAnalysisOutput:
    symbol: str
    timeframe: str
    # 1. 市场环境
    market_regime: str  # e.g., "Trending", "Ranging", "High Volatility Expansion", "Compression"
    # 2. 当前趋势
    trend: str  # e.g., "Bullish", "Bearish", "Neutral"
    # 3. 市场结构
    market_structure: str  # e.g., "HH_HL", "LH_LL", "Range_Bound", "Failed_Breakout"
    # 4. 所处位置
    location: str  # e.g., "Near Support", "Near Resistance", "Equilibrium (Fair Value)"
    # 5. 关键支撑
    support_levels: List[float]
    # 6. 关键压力
    resistance_levels: List[float]
    # 7. 流动性区域
    liquidity_zones: List[Dict[str, Any]]
    # 8. 多头情景
    bullish_scenario: ScenarioPlan
    # 9. 空头情景
    bearish_scenario: ScenarioPlan
    # 10. 无交易情景
    no_trade_scenario: str
    # 11. 失效条件
    invalidation_conditions: List[str]
    # 12. 风险因素
    risk_factors: List[str]
    # 13. 需要继续观察的数据
    data_to_watch: List[str]
    # 原始思维链 / 推理概要
    thinking_summary: Optional[str] = None
    confidence_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass
class ToolExecutionLog:
    timestamp: str
    agent: str
    tool: str
    arguments: Dict[str, Any]
    result: Any
    latency_ms: float
    success: bool
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# JSON Schema for Mode B (FAST) structured outputs
MARKET_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "timeframe": {"type": "string"},
        "market_regime": {"type": "string"},
        "trend": {"type": "string"},
        "market_structure": {"type": "string"},
        "location": {"type": "string"},
        "support_levels": {"type": "array", "items": {"type": "number"}},
        "resistance_levels": {"type": "array", "items": {"type": "number"}},
        "liquidity_zones": {"type": "array", "items": {"type": "object"}},
        "bullish_scenario": {
            "type": "object",
            "properties": {
                "hypothesis": {"type": "string"},
                "trigger_condition": {"type": "string"},
                "entry_zone": {"type": "array", "items": {"type": "number"}},
                "stop_loss": {"type": "number"},
                "take_profit": {"type": "array", "items": {"type": "number"}},
                "invalidation": {"type": "string"}
            },
            "required": ["hypothesis", "trigger_condition", "entry_zone", "stop_loss", "take_profit", "invalidation"]
        },
        "bearish_scenario": {
            "type": "object",
            "properties": {
                "hypothesis": {"type": "string"},
                "trigger_condition": {"type": "string"},
                "entry_zone": {"type": "array", "items": {"type": "number"}},
                "stop_loss": {"type": "number"},
                "take_profit": {"type": "array", "items": {"type": "number"}},
                "invalidation": {"type": "string"}
            },
            "required": ["hypothesis", "trigger_condition", "entry_zone", "stop_loss", "take_profit", "invalidation"]
        },
        "no_trade_scenario": {"type": "string"},
        "invalidation_conditions": {"type": "array", "items": {"type": "string"}},
        "risk_factors": {"type": "array", "items": {"type": "string"}},
        "data_to_watch": {"type": "array", "items": {"type": "string"}},
        "confidence_score": {"type": "number"}
    },
    "required": [
        "symbol", "timeframe", "market_regime", "trend", "market_structure", "location",
        "support_levels", "resistance_levels", "bullish_scenario", "bearish_scenario",
        "no_trade_scenario", "invalidation_conditions", "risk_factors", "data_to_watch"
    ]
}
