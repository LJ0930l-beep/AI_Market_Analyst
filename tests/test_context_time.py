import unittest
from datetime import datetime, timezone

from core.context import MarketContext
from core.instruments import instrument_for
from core.news_engine import NewsEngine
from core.providers import FixtureNewsProvider, FixtureProvider, NewsEvent
from core.providers.runtime import fetch_market_data
from core.quant import build_quant_snapshot
from core.time_rules import build_time_policy


class ContextAndTimePolicyTests(unittest.TestCase):
    def test_context_is_compressed_and_hashable_without_raw_bars(self):
        instrument = instrument_for("AAPL")
        bundle = fetch_market_data(FixtureProvider(), instrument, "1h", 120)
        quant = build_quant_snapshot(list(bundle.bars), "1h", symbol=instrument.symbol)
        news = NewsEngine(FixtureNewsProvider()).collect(instrument)
        policy = build_time_policy(
            "1h",
            price=quant.price,
            atr14=quant.atr14,
            market_regime=quant.market_regime,
            events=news.events,
            now=datetime.now(timezone.utc),
        )
        context = MarketContext(
            instrument=instrument,
            quote=bundle.quote,
            bars=bundle.bars,
            quant=quant,
            news=news.events,
            time_policy=policy,
            provider_snapshot=bundle.snapshot,
        )
        payload = context.to_prompt_payload()
        self.assertNotIn("bars", payload)
        self.assertEqual(context.input_hash(), context.input_hash())
        self.assertIn("time_policy", payload)
        self.assertEqual(payload["provider_snapshot"]["provider"], "fixture")
        self.assertTrue(policy.signal_validity_allowed)
        self.assertIn(policy.signal_validity_allowed[0], payload["time_policy"]["allowed_values"]["signal_validity_minutes"])

    def test_high_impact_news_caps_policy(self):
        event = NewsEvent(
            event_id="fed",
            source="test",
            published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            title="Fed interest rate decision",
            symbols=("BTCUSDT",),
            category="macro",
            sentiment=-0.2,
            importance=90,
            impact_horizon="1-3d",
        )
        policy = build_time_policy(
            "1h",
            price=100.0,
            atr14=1.0,
            market_regime="bull_trend",
            events=(event,),
        )
        self.assertTrue(policy.event_risk)
        self.assertLessEqual(policy.signal_validity_max, 240)
        self.assertIn("high_impact_event_cap", policy.reason_codes)


if __name__ == "__main__":
    unittest.main()
