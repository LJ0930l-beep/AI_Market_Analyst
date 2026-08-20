import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from core.instruments import instrument_for
from core.outcomes import settle_prediction
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.signals import build_signal
from core.storage import SQLiteStore


class StorageTests(unittest.TestCase):
    def test_prediction_papertrade_outcome_survive_new_store(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market.sqlite3"
            instrument = instrument_for("NVDA")
            provider = FixtureProvider()
            bars = provider.get_bars(instrument, "1h", 120)
            quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
            signal = build_signal(instrument, quant, generated_at=bars[-1].timestamp)
            store = SQLiteStore(path)
            store.initialize()
            store.save_instrument(instrument)
            store.save_snapshot(instrument.symbol, "1h", bars[-1].timestamp.isoformat(), quant.to_dict())
            store.save_prediction(signal)
            store.follow_prediction(signal.prediction_id, signal.generated_at.isoformat())
            outcome = settle_prediction(signal, [
                type(bars[-1])(
                    signal.generated_at + timedelta(hours=1),
                    quant.price,
                    quant.price * 1.02,
                    quant.price * 0.99,
                    quant.price * 1.01,
                    100,
                )
            ])
            store.save_outcome(outcome)
            reopened = SQLiteStore(path)
            reopened.initialize()
            counts = reopened.counts()
            self.assertEqual(counts["instruments"], 1)
            self.assertEqual(counts["predictions"], 1)
            self.assertEqual(counts["paper_trades"], 1)
            self.assertEqual(counts["outcomes"], 1)
            self.assertIsNotNone(reopened.load_prediction_payload(signal.prediction_id))

    def test_phase3_replay_and_calibration_records_are_durable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "phase3.sqlite3"
            instrument = instrument_for("NVDA")
            bars = FixtureProvider().get_bars(instrument, "1h", 120)
            quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
            signal = build_signal(instrument, quant, generated_at=bars[-1].timestamp)
            signal = replace(
                signal,
                source_type="replay",
                replay_run_id="replay-test",
                calibrated_confidence=0.61,
                calibration_version="cal-v1-test",
                calibration_scope="global",
                calibration_sample_size=12,
                calibration_fallback="raw_insufficient_sample",
            )
            store = SQLiteStore(path)
            store.initialize()
            store.create_replay_run(
                run_id="replay-test",
                model_id="qwen3.5:4b",
                prompt_version="phase2-json-v6",
                symbols=["NVDA"],
                timeframes=["1h"],
                sampling_policy={"seed": 1},
                manifest_hash="manifest-hash",
            )
            store.save_prediction(signal)
            store.save_replay_sample(
                run_id="replay-test",
                symbol="NVDA",
                timeframe="1h",
                as_of=bars[-1].timestamp.isoformat(),
                capability_flags={"news_history_available": False},
                prediction_id=signal.prediction_id,
                status="WAIT",
            )
            store.save_performance_snapshot({"scope": {"source_type": "replay"}, "metrics": {"sample_count": 1}, "source_type": "replay"})
            store.save_calibration_result(
                {
                    "calibration_id": "cal-test",
                    "version": "cal-v1-test",
                    "scope": {},
                    "method": "empirical_beta_shrinkage",
                    "params": {"alpha": 5, "beta": 5},
                    "sample_count": 1,
                    "status": "INSUFFICIENT_SAMPLE",
                    "buckets": [{"lower": 0.5, "upper": 0.6, "n": 1, "wins": 0, "empirical_rate": 0.0, "shrunk_rate": 0.4545}],
                }
            )
            reopened = SQLiteStore(path)
            reopened.initialize()
            self.assertEqual(reopened.schema_version(), 10)
            self.assertEqual(reopened.get_replay_run("replay-test")["status"], "RUNNING")
            self.assertEqual(len(reopened.list_replay_samples("replay-test")), 1)
            records = reopened.list_prediction_records(source_type="replay", replay_run_id="replay-test")
            self.assertAlmostEqual(records[0]["prediction"]["calibrated_confidence"], 0.61)
            self.assertEqual(len(reopened.list_calibration_results()), 1)


if __name__ == "__main__":
    unittest.main()
