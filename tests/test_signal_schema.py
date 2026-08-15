import unittest
from datetime import datetime, timedelta, timezone

from core.instruments import instrument_for
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.signals import Action, SignalProposal, build_signal


class SignalSchemaTests(unittest.TestCase):
    def test_fixture_signal_is_valid_and_replayable(self):
        instrument = instrument_for("AAPL")
        provider = FixtureProvider()
        bars = provider.get_bars(instrument, "1h", 120)
        quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
        signal = build_signal(instrument, quant, generated_at=bars[-1].timestamp)
        payload = signal.to_dict()
        self.assertEqual(payload["instrument"]["symbol"], "AAPL")
        self.assertIn(signal.action, {Action.LONG, Action.SHORT, Action.WAIT})
        self.assertGreater(signal.signal_valid_until, signal.generated_at)
        self.assertGreaterEqual(signal.raw_confidence, 0)
        self.assertLessEqual(signal.raw_confidence, 1)

    def test_long_requires_valid_risk_levels(self):
        instrument = instrument_for("NVDA")
        generated = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            SignalProposal(
                prediction_id="bad-long",
                instrument=instrument,
                analysis_timeframe="1h",
                generated_at=generated,
                action=Action.LONG,
                entry_low=100,
                entry_high=100,
                stop=95,
                tp1=106,
                tp2=110,
                signal_validity_minutes=60,
                expected_hold_minutes=60,
                max_hold_minutes=120,
                reevaluate_at=generated + timedelta(minutes=60),
                invalidation=("below stop",),
                raw_confidence=0.7,
                reason_codes=("test",),
                summary="invalid R:R",
            )


if __name__ == "__main__":
    unittest.main()

