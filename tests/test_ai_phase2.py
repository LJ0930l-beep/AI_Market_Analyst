import unittest
from datetime import datetime, timezone

from core.ai import MockLLMProvider, SignalPolicy, analyze_with_repair, build_system_prompt, to_signal_proposal, validate_model_response
from core.ai.contracts import ModelSignalResponse
from core.context import MarketContext
from core.instruments import instrument_for
from core.news_engine import NewsEngine
from core.providers import FixtureNewsProvider, FixtureProvider
from core.providers.runtime import fetch_market_data
from core.quant import build_quant_snapshot
from core.time_rules import build_time_policy


def _context(symbol: str = "NVDA") -> MarketContext:
    instrument = instrument_for(symbol)
    bundle = fetch_market_data(FixtureProvider(), instrument, "1h", 120)
    quant = build_quant_snapshot(list(bundle.bars), "1h", symbol=instrument.symbol)
    news = NewsEngine(FixtureNewsProvider()).collect(instrument)
    time_policy = build_time_policy(
        "1h",
        price=quant.price,
        atr14=quant.atr14,
        market_regime=quant.market_regime,
        events=news.events,
    )
    return MarketContext(
        instrument=instrument,
        quote=bundle.quote,
        bars=bundle.bars,
        quant=quant,
        news=news.events,
        time_policy=time_policy,
        provider_snapshot=bundle.snapshot,
        market_context={"news_available": news.available},
    )


class AIPhase2Tests(unittest.TestCase):
    def test_mock_model_output_becomes_a_valid_prediction(self):
        context = _context()
        policy = SignalPolicy.from_context(context)
        response, metadata = analyze_with_repair(MockLLMProvider(), context, policy)
        signal = to_signal_proposal(response, context, policy, metadata, generated_at=datetime.now(timezone.utc))
        self.assertIn(signal.action.value, {"LONG", "SHORT", "WAIT"})
        self.assertEqual(signal.model_id, "mock-qwen3.5-4b")
        self.assertEqual(signal.parse_status, "valid")
        self.assertEqual(signal.input_hash, context.input_hash())
        self.assertLessEqual(signal.raw_confidence, 0.65)

    def test_invalid_first_response_gets_exactly_one_repair(self):
        context = _context()
        provider = MockLLMProvider(invalid_first=True)
        response, metadata = analyze_with_repair(provider, context, SignalPolicy.from_context(context))
        self.assertEqual(provider.calls, 2)
        self.assertEqual(metadata.parse_status, "repair_valid")
        self.assertIn(response.action.value, {"LONG", "SHORT", "WAIT"})

    def test_validator_rejects_directionally_wrong_levels(self):
        context = _context()
        policy = SignalPolicy.from_context(context)
        price = context.quant.price
        response = ModelSignalResponse.from_dict(
            {
                "action": "LONG",
                "confidence_raw": 50,
                "entry_preference": "market",
                "entry_zone": {"low": price * 0.999, "high": price * 1.001},
                "stop": price * 1.01,
                "tp1": price * 1.03,
                "tp2": price * 1.04,
                "signal_validity_minutes": 180,
                "holding_horizon_minutes": 1440,
                "re_evaluate_minutes": 90,
                "invalidation": ["bad stop"],
                "thesis": ["test"],
                "risk_factors": [],
            }
        )
        errors = validate_model_response(response, context, policy)
        self.assertTrue(any("LONG levels" in error for error in errors))

    def test_wait_requires_no_price_levels(self):
        with self.assertRaises(ValueError):
            ModelSignalResponse.from_dict(
                {
                    "action": "WAIT",
                    "confidence_raw": 25,
                    "entry_preference": "none",
                    "entry_zone": {"low": 1, "high": 2},
                    "stop": None,
                    "tp1": None,
                    "tp2": None,
                    "signal_validity_minutes": 180,
                    "holding_horizon_minutes": 1440,
                    "re_evaluate_minutes": 90,
                    "invalidation": [],
                    "thesis": ["no edge"],
                    "risk_factors": [],
                }
            )

    def test_json_prompt_makes_wait_levels_explicitly_null(self):
        prompt = build_system_prompt(SignalPolicy.from_context(_context()))
        self.assertIn("entry_zone, stop, tp1, and tp2 MUST all be JSON null", prompt)
        self.assertIn('"entry_zone":null', prompt)

    def test_technical_only_replay_is_explicit_in_policy(self):
        context = _context()
        context = MarketContext(
            instrument=context.instrument,
            quote=context.quote,
            bars=context.bars,
            quant=context.quant,
            news=context.news,
            time_policy=context.time_policy,
            provider_snapshot=context.provider_snapshot,
            market_context={
                "news_available": False,
                "context_capabilities": {"technical_only": True, "replay": True},
            },
        )
        policy = SignalPolicy.from_context(context)
        self.assertTrue(policy.technical_only)
        self.assertFalse(policy.news_available)
        self.assertIn("technical_only", policy.to_dict())

    def test_model_schema_covers_long_short_and_wait(self):
        context = _context()
        policy = SignalPolicy.from_context(context)
        price = context.quant.price
        risk = max(context.quant.atr14, price * 0.005)
        validity = policy.signal_validity_allowed[0]
        holding = policy.holding_horizon_allowed[0]
        reevaluate = policy.reevaluate_allowed[0]
        common = {
            "confidence_raw": 50,
            "signal_validity_minutes": validity,
            "holding_horizon_minutes": holding,
            "re_evaluate_minutes": reevaluate,
            "thesis": ["schema test"],
            "risk_factors": [],
        }
        payloads = [
            {
                **common,
                "action": "LONG",
                "entry_preference": "market",
                "entry_zone": {"low": price * 0.999, "high": price * 1.001},
                "stop": price - risk,
                "tp1": price + 1.6 * risk,
                "tp2": price + 2.4 * risk,
                "invalidation": ["close below stop"],
            },
            {
                **common,
                "action": "SHORT",
                "entry_preference": "market",
                "entry_zone": {"low": price * 0.999, "high": price * 1.001},
                "stop": price + risk,
                "tp1": price - 1.6 * risk,
                "tp2": price - 2.4 * risk,
                "invalidation": ["close above stop"],
            },
            {
                **common,
                "action": "WAIT",
                "entry_preference": "none",
                "entry_zone": None,
                "stop": None,
                "tp1": None,
                "tp2": None,
                "invalidation": [],
            },
        ]
        for payload in payloads:
            response = ModelSignalResponse.from_dict(payload)
            self.assertEqual(validate_model_response(response, context, policy), ())


if __name__ == "__main__":
    unittest.main()
