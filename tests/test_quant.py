import unittest

from core.instruments import instrument_for
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot


class QuantEngineTests(unittest.TestCase):
    def test_fixture_produces_replayable_snapshot(self):
        instrument = instrument_for("NVDA")
        provider = FixtureProvider()
        bars = provider.get_bars(instrument, "1h", limit=120)
        first = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
        second = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
        self.assertEqual(first, second)
        self.assertEqual(first.symbol, "NVDA")
        self.assertGreater(first.ema20, 0)
        self.assertGreater(first.atr14, 0)
        self.assertIn(first.market_regime, {"bull_trend", "bear_trend", "range"})
        self.assertGreaterEqual(first.rsi14, 0)
        self.assertLessEqual(first.rsi14, 100)

    def test_short_history_is_rejected(self):
        instrument = instrument_for("AAPL")
        bars = FixtureProvider().get_bars(instrument, "1h", limit=60)
        with self.assertRaises(ValueError):
            build_quant_snapshot(bars[:59], "1h", symbol=instrument.symbol)


if __name__ == "__main__":
    unittest.main()

