import unittest
from datetime import timezone

from core.instruments import instrument_for
from core.news_engine import NewsEngine
from core.providers import FixtureProvider
from core.replay.provider import ReplayNewsProvider, ReplayProvider, build_as_of_points, future_bars
from core.replay.runner import _replay_prediction_id


class ReplayPhase3Tests(unittest.TestCase):
    def test_replay_provider_never_exposes_future_bars(self):
        instrument = instrument_for("NVDA")
        bars = FixtureProvider().get_bars(instrument, "1h", 240)
        as_of = bars[160].timestamp
        provider = ReplayProvider(instrument, bars, as_of, underlying_provider="fixture")
        visible = provider.get_bars(instrument, "1h", 120)
        self.assertLessEqual(max(bar.timestamp for bar in visible), as_of)
        future = future_bars(bars, as_of)
        self.assertTrue(future)
        self.assertGreater(min(bar.timestamp for bar in future), as_of)
        self.assertEqual(provider.get_quote(instrument).timestamp, as_of)

    def test_sampling_is_deterministic(self):
        instrument = instrument_for("BTCUSDT")
        bars = FixtureProvider().get_bars(instrument, "4h", 400)
        first = build_as_of_points(bars, timeframe="4h", count=10, seed=19)
        second = build_as_of_points(bars, timeframe="4h", count=10, seed=19)
        self.assertEqual(first, second)
        self.assertTrue(all(item.tzinfo is not None and item.tzinfo == timezone.utc for item in first))

    def test_replay_news_is_explicitly_unavailable(self):
        result = NewsEngine(ReplayNewsProvider()).collect(instrument_for("AAPL"))
        self.assertFalse(result.available)
        self.assertEqual(result.error_code, "historical_news_unavailable")
        self.assertEqual(result.events, ())

    def test_replay_prediction_id_is_route_specific_and_stable(self):
        as_of = build_as_of_points(
            FixtureProvider().get_bars(instrument_for("AAPL"), "1h", 240),
            timeframe="1h",
            count=1,
            seed=7,
        )[0]
        first = _replay_prediction_id("run-1", "AAPL", "1h", as_of)
        second = _replay_prediction_id("run-1", "NVDA", "1h", as_of)
        self.assertNotEqual(first, second)
        self.assertEqual(first, _replay_prediction_id("run-1", "AAPL", "1h", as_of))


if __name__ == "__main__":
    unittest.main()
