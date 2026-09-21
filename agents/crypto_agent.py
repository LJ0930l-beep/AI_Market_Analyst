"""CryptoAgent for AI Market Analyst V2.

Conducts autonomous, rigorous crypto market analysis exclusively using Bonsai 2 27B
via ModelClient, bound to real-time tools and deterministic Python technical indicators.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.model_client import ModelClient, model_client
from core.schemas import MarketAnalysisOutput
from core.logging import get_logger
from tools.crypto import CryptoTools

logger = get_logger("crypto_agent")


class CryptoAgent:
    """Specialized agent for cryptocurrency markets (BTC, ETH, SOL, etc.)."""

    def __init__(self, client: ModelClient | None = None) -> None:
        self.client = client or model_client
        self.tools = CryptoTools()

    def analyze(
        self,
        symbol: str = "BTC_USDT",
        timeframe: str = "15m",
        *,
        mode: str = "ANALYSIS",
    ) -> Dict[str, Any]:
        """Perform comprehensive 13-point institutional analysis on a crypto asset."""
        logger.info("Executing CryptoAgent analysis for %s on %s (mode=%s)", symbol, timeframe, mode)

        # 1. Fetch real-time facts via tools (Never let LLM guess)
        ticker = self.tools.get_ticker(symbol)
        ohlcv_resp = self.tools.get_ohlcv(symbol, timeframe=timeframe, limit=100)
        funding = self.tools.get_funding_rate(symbol)
        oi = self.tools.get_open_interest(symbol)
        news = self.tools.get_crypto_news(symbol, limit=3)

        bars = ohlcv_resp.get("bars", [])
        if not bars:
            logger.warning("No OHLCV bars available for %s, reporting DATA_UNAVAILABLE", symbol)
            return {
                "symbol": symbol,
                "status": "DATA_UNAVAILABLE",
                "reason": "Live candlestick stream returned no bars. Model inference aborted to avoid hallucination.",
            }

        # 2. Compute deterministic technicals
        indicators = self.tools.calculate_indicators(bars)

        # 3. Formulate Prompt containing ONLY verified reality
        system_prompt = (
            "You are the Lead Quantitative Crypto Analyst at an institutional fund. "
            "You reason systematically and objectively about price action, liquidity, and market regime. "
            "NEVER invent prices, indicators, or funding rates. Use only the provided technical facts. "
            "Do NOT provide a simplistic BUY/SELL recommendation. You must explain WHY, WHERE, "
            "under WHAT CONDITIONS a trade activates, and what conditions INVALIDATE the premise."
        )

        user_content = f"""### Market Dossier for {symbol} ({timeframe})
- Current Price: {indicators.get('current_price', ticker.get('price'))}
- 24h Change: {ticker.get('change_24h_pct')}% (High: {ticker.get('high_24h')}, Low: {ticker.get('low_24h')})
- Trend & Structure: {indicators.get('trend')} ({indicators.get('market_structure')})
- Key Location: {indicators.get('location')}
- Indicator Metrics:
  * EMA20: {indicators.get('ema20')}, EMA50: {indicators.get('ema50')}, EMA200: {indicators.get('ema200')}
  * RSI(14): {indicators.get('rsi14')}
  * ATR(14): {indicators.get('atr14')}
  * Volume State: {indicators.get('volume_state')} (ratio: {indicators.get('volume_ratio')}x)
- Dynamic Support Levels: {indicators.get('support')}
- Dynamic Resistance Levels: {indicators.get('resistance')}
- Derivatives Telemetry:
  * Funding Rate: {funding.get('funding_rate')}
  * Open Interest: {oi.get('open_interest_usdt', 'UNAVAILABLE')} USDT
- Recent Catalysts / News: {json.dumps(news.get('news_items', []), ensure_ascii=False)}

Perform a thorough 13-point institutional deduction covering:
1. 市场环境 (Market Regime)
2. 当前趋势 (Trend)
3. 市场结构 (Market Structure)
4. 所处位置 (Location)
5. 关键支撑 (Support)
6. 关键压力 (Resistance)
7. 流动性区域 (Liquidity Zones)
8. 多头情景 (Bullish Scenario: trigger, entry zone, stop loss, take profit, invalidation)
9. 空头情景 (Bearish Scenario: trigger, entry zone, stop loss, take profit, invalidation)
10. 无交易情景 (No Trade Condition)
11. 失效条件 (Invalidation Criteria)
12. 风险因素 (Risk Factors)
13. 需要继续观察的数据 (Metrics to Watch)
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        # 4. Invoke ModelClient
        response = self.client.chat_completion(
            messages=messages,
            mode=mode,
        )

        choice = response.get("choices", [{}])[0]
        reasoning_text = choice.get("message", {}).get("content", "")

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "observed_at": ticker.get("observed_at"),
            "technical_facts": indicators,
            "derivatives_facts": {"funding": funding, "oi": oi},
            "analysis_report": reasoning_text,
            "model_metadata": {
                "model": response.get("model", self.client.model_name),
                "usage": response.get("usage"),
                "mode": mode,
            },
        }
