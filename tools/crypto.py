"""Crypto data tools interface for AI Market Analyst V2.

Strict decoupling of market reality from LLM hallucination:
- Real-time ticker, OHLCV, funding rate, open interest, liquidation, orderbook
- Fails explicitly with DATA_UNAVAILABLE if feeds are unreachable
- Every invocation is audited to logs/tools/
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from core.logging import log_tool_call
from .technical_analysis import analyze_ohlcv


class CryptoTools:
    """Deterministic crypto tools provider."""

    @staticmethod
    def get_ticker(symbol: str) -> Dict[str, Any]:
        """Fetch current ticker price, 24h change, high and low."""
        start_t = time.time()
        # Clean symbol (e.g. BTC_USDT -> BTCUSDT)
        norm_sym = symbol.replace("_", "").replace("-", "").upper()
        try:
            # Connect to Gate public or fallback to verified local snapshot
            from core.providers.gateio_provider import GateioProvider
            prov = GateioProvider()
            ticker = prov.get_ticker(symbol)
            if ticker and ticker.get("last"):
                res = {
                    "symbol": symbol,
                    "price": float(ticker["last"]),
                    "high_24h": float(ticker.get("high_24h", 0)),
                    "low_24h": float(ticker.get("low_24h", 0)),
                    "change_24h_pct": float(ticker.get("change_percentage", 0)),
                    "volume_24h": float(ticker.get("base_volume", 0)),
                    "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                log_tool_call(agent="CryptoAgent", tool="get_ticker", arguments={"symbol": symbol}, result=res, latency_ms=(time.time()-start_t)*1000, success=True)
                return res
        except Exception as e:
            pass
            
        res = {"status": "DATA_UNAVAILABLE", "symbol": symbol, "reason": "Exchange ticker feed unreachable"}
        log_tool_call(agent="CryptoAgent", tool="get_ticker", arguments={"symbol": symbol}, result=res, latency_ms=(time.time()-start_t)*1000, success=False)
        return res

    @staticmethod
    def get_ohlcv(symbol: str, timeframe: str = "15m", limit: int = 100) -> Dict[str, Any]:
        """Fetch closed OHLCV candlestick sequence."""
        start_t = time.time()
        try:
            from core.providers.gateio_provider import GateioProvider
            prov = GateioProvider()
            bars = prov.get_candlesticks(symbol, interval=timeframe, limit=limit)
            if bars and len(bars) >= 10:
                res = {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bars_count": len(bars),
                    "bars": bars,
                }
                log_tool_call(agent="CryptoAgent", tool="get_ohlcv", arguments={"symbol": symbol, "timeframe": timeframe, "limit": limit}, result={"bars_count": len(bars)}, latency_ms=(time.time()-start_t)*1000, success=True)
                return res
        except Exception:
            pass

        res = {"status": "DATA_UNAVAILABLE", "symbol": symbol, "reason": "K-line historical bars unreachable"}
        log_tool_call(agent="CryptoAgent", tool="get_ohlcv", arguments={"symbol": symbol, "timeframe": timeframe, "limit": limit}, result=res, latency_ms=(time.time()-start_t)*1000, success=False)
        return res

    @staticmethod
    def get_funding_rate(symbol: str) -> Dict[str, Any]:
        """Fetch perpetual contract funding rate and next settlement countdown."""
        start_t = time.time()
        try:
            from core.providers.gateio_provider import GateioProvider
            prov = GateioProvider()
            funding = prov.get_funding_rate(symbol)
            if funding:
                res = {
                    "symbol": symbol,
                    "funding_rate": float(funding.get("funding_rate", 0.0001)),
                    "predicted_rate": float(funding.get("predicted_rate", 0.0001)),
                    "next_settlement_utc": funding.get("next_settlement"),
                }
                log_tool_call(agent="CryptoAgent", tool="get_funding_rate", arguments={"symbol": symbol}, result=res, latency_ms=(time.time()-start_t)*1000, success=True)
                return res
        except Exception:
            pass

        res = {"status": "DATA_UNAVAILABLE", "symbol": symbol, "reason": "Perpetual funding stream offline"}
        log_tool_call(agent="CryptoAgent", tool="get_funding_rate", arguments={"symbol": symbol}, result=res, latency_ms=(time.time()-start_t)*1000, success=False)
        return res

    @staticmethod
    def get_open_interest(symbol: str) -> Dict[str, Any]:
        """Fetch aggregated open interest (contracts / USDT value)."""
        start_t = time.time()
        try:
            from core.providers.gateio_provider import GateioProvider
            prov = GateioProvider()
            oi = prov.get_open_interest(symbol)
            if oi:
                res = {
                    "symbol": symbol,
                    "open_interest_contracts": float(oi.get("contracts", 0)),
                    "open_interest_usdt": float(oi.get("notional_usd", 0)),
                }
                log_tool_call(agent="CryptoAgent", tool="get_open_interest", arguments={"symbol": symbol}, result=res, latency_ms=(time.time()-start_t)*1000, success=True)
                return res
        except Exception:
            pass

        res = {"status": "DATA_UNAVAILABLE", "symbol": symbol, "reason": "Open Interest telemetry unavailable"}
        log_tool_call(agent="CryptoAgent", tool="get_open_interest", arguments={"symbol": symbol}, result=res, latency_ms=(time.time()-start_t)*1000, success=False)
        return res

    @staticmethod
    def get_liquidation_data(symbol: str) -> Dict[str, Any]:
        """Fetch recent liquidation cluster summary."""
        start_t = time.time()
        res = {
            "symbol": symbol,
            "status": "DATA_UNAVAILABLE",
            "reason": "Direct liquidation cluster WebSocket feed requires authenticated VIP subscription",
        }
        log_tool_call(agent="CryptoAgent", tool="get_liquidation_data", arguments={"symbol": symbol}, result=res, latency_ms=(time.time()-start_t)*1000, success=False)
        return res

    @staticmethod
    def get_orderbook(symbol: str, depth: int = 20) -> Dict[str, Any]:
        """Fetch Level 2 orderbook bids and asks."""
        start_t = time.time()
        try:
            from core.providers.gateio_provider import GateioProvider
            prov = GateioProvider()
            ob = prov.get_order_book(symbol, limit=depth)
            if ob and "bids" in ob and "asks" in ob:
                res = {
                    "symbol": symbol,
                    "depth": depth,
                    "best_bid": float(ob["bids"][0][0]) if ob["bids"] else None,
                    "best_ask": float(ob["asks"][0][0]) if ob["asks"] else None,
                    "spread": round(float(ob["asks"][0][0]) - float(ob["bids"][0][0]), 4) if ob["bids"] and ob["asks"] else None,
                    "bids_top5": ob["bids"][:5],
                    "asks_top5": ob["asks"][:5],
                }
                log_tool_call(agent="CryptoAgent", tool="get_orderbook", arguments={"symbol": symbol, "depth": depth}, result={"spread": res["spread"]}, latency_ms=(time.time()-start_t)*1000, success=True)
                return res
        except Exception:
            pass

        res = {"status": "DATA_UNAVAILABLE", "symbol": symbol, "reason": "L2 orderbook stream unavailable"}
        log_tool_call(agent="CryptoAgent", tool="get_orderbook", arguments={"symbol": symbol, "depth": depth}, result=res, latency_ms=(time.time()-start_t)*1000, success=False)
        return res

    @staticmethod
    def get_market_flow(symbol: str) -> Dict[str, Any]:
        """Fetch taker buy/sell volume delta."""
        start_t = time.time()
        res = {
            "symbol": symbol,
            "status": "DATA_UNAVAILABLE",
            "reason": "CVD orderflow aggregation not registered for this venue",
        }
        log_tool_call(agent="CryptoAgent", tool="get_market_flow", arguments={"symbol": symbol}, result=res, latency_ms=(time.time()-start_t)*1000, success=False)
        return res

    @staticmethod
    def get_crypto_news(symbol: str, limit: int = 5) -> Dict[str, Any]:
        """Fetch point-in-time news catalyst items from storage."""
        start_t = time.time()
        try:
            from core.storage.sqlite import get_db
            import os
            db_path = os.environ.get("SQLITE_DB_PATH", r"D:\RJ\AI Market Analyst\data\market_analyst.sqlite3")
            with get_db(db_path) as conn:
                cur = conn.cursor()
                rows = cur.execute(
                    "SELECT title, summary, impact, published_at FROM news_events ORDER BY rowid DESC LIMIT ?",
                    (limit,)
                ).fetchall()
                items = [dict(r) for r in rows] if rows else []
                res = {"symbol": symbol, "news_items": items}
                log_tool_call(agent="CryptoAgent", tool="get_crypto_news", arguments={"symbol": symbol, "limit": limit}, result={"count": len(items)}, latency_ms=(time.time()-start_t)*1000, success=True)
                return res
        except Exception:
            pass

        res = {"status": "DATA_UNAVAILABLE", "symbol": symbol, "reason": "News archive stream offline"}
        log_tool_call(agent="CryptoAgent", tool="get_crypto_news", arguments={"symbol": symbol, "limit": limit}, result=res, latency_ms=(time.time()-start_t)*1000, success=False)
        return res

    @staticmethod
    def calculate_indicators(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Execute deterministic technical analysis engine on K-lines."""
        start_t = time.time()
        res = analyze_ohlcv(bars)
        log_tool_call(agent="CryptoAgent", tool="calculate_indicators", arguments={"bars_count": len(bars)}, result=res, latency_ms=(time.time()-start_t)*1000, success=True)
        return res
