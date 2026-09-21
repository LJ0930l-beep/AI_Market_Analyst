"""Unit tests for Bonsai 2 27B tools, technical indicators, and RAG knowledge store."""

import unittest
from tools.technical_analysis import (
    calculate_ema,
    calculate_rsi,
    calculate_atr,
    determine_market_structure,
    analyze_ohlcv,
)
from tools.crypto import CryptoTools
from rag.vector_store import VectorStore
from rag.retriever import KnowledgeRetriever
from core.config import config, PRESET_ANALYSIS, PRESET_FAST
from core.schemas import MarketAnalysisOutput, ScenarioPlan


class TestBonsaiToolsAndRAG(unittest.TestCase):
    def test_config_presets(self):
        analysis = config.get_preset("ANALYSIS")
        self.assertTrue(analysis.thinking_enabled)
        self.assertEqual(analysis.temperature, 1.0)
        self.assertEqual(analysis.top_p, 0.95)

        fast = config.get_preset("FAST")
        self.assertFalse(fast.thinking_enabled)
        self.assertEqual(fast.temperature, 0.7)
        self.assertEqual(fast.top_p, 0.8)

    def test_technical_analysis_math(self):
        # 1. EMA
        prices = [10.0 + i for i in range(30)]
        ema20 = calculate_ema(prices, 20)
        self.assertEqual(len(ema20), 11)
        self.assertGreater(ema20[-1], ema20[0])

        # 2. RSI
        rsi = calculate_rsi(prices, 14)
        self.assertGreaterEqual(rsi, 90.0)  # Monotonically increasing prices -> high RSI

        # 3. ATR
        highs = [p + 2.0 for p in prices]
        lows = [p - 2.0 for p in prices]
        atr = calculate_atr(highs, lows, prices, 14)
        self.assertGreater(atr, 0.0)

        # 4. Market Structure
        struct = determine_market_structure(highs, lows, prices)
        self.assertIn(struct["structure"], ["HH_HL", "Range_Bound", "Trading_Range", "LH_LL"])

    def test_analyze_ohlcv_full_dossier(self):
        bars = [
            {"open": 100 + i, "high": 105 + i, "low": 98 + i, "close": 103 + i, "volume": 1000 + i * 10}
            for i in range(40)
        ]
        dossier = analyze_ohlcv(bars)
        self.assertIn("trend", dossier)
        self.assertIn("ema20", dossier)
        self.assertIn("rsi14", dossier)
        self.assertIn("support", dossier)
        self.assertIn("resistance", dossier)
        self.assertIn("volume_state", dossier)

    def test_crypto_tools_decoupling(self):
        # If no real connection, must return DATA_UNAVAILABLE or valid dict, never crash
        ticker = CryptoTools.get_ticker("BTC_USDT")
        self.assertTrue("price" in ticker or ticker.get("status") == "DATA_UNAVAILABLE")

        funding = CryptoTools.get_funding_rate("BTC_USDT")
        self.assertTrue("funding_rate" in funding or funding.get("status") == "DATA_UNAVAILABLE")

    def test_rag_vector_store_retriever(self):
        store = VectorStore("data/test_rag.sqlite3")
        store.add_document("doc1", "Price Action", "technical", "Higher Highs and Higher Lows define an uptrend.")
        retriever = KnowledgeRetriever(store)
        results = retriever.retrieve("Higher Highs", top_k=1)
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("Higher Highs", results[0])

    def test_schemas_13_point_output(self):
        out = MarketAnalysisOutput(
            symbol="BTC_USDT",
            timeframe="15m",
            market_regime="Trending",
            trend="Bullish",
            market_structure="HH_HL",
            location="Near Support",
            support_levels=[81000.0, 80500.0],
            resistance_levels=[83000.0, 84500.0],
            liquidity_zones=[{"type": "FVG", "range": [81200, 81400]}],
            bullish_scenario=ScenarioPlan("Reclaim 82000", "15m close above 82000", [81800, 82000], 80900, [83500, 85000], "Break below 80900"),
            bearish_scenario=ScenarioPlan("Rejection at 83000", "Wick rejection", [82800, 83000], 83500, [81000], "Break above 83500"),
            no_trade_scenario="Weekend low volume range between 81500 and 82200",
            invalidation_conditions=["Volume dry-up", "Macro release volatility"],
            risk_factors=["High funding crowding"],
            data_to_watch=["Next 15m closed candle"],
            confidence_score=88.5,
        )
        d = out.to_dict()
        self.assertEqual(d["symbol"], "BTC_USDT")
        self.assertEqual(len(d["support_levels"]), 2)


if __name__ == "__main__":
    unittest.main()
