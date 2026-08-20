import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.instruments import instrument_for
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.signals import build_signal
from core.storage import SQLiteStore


class MigrationTests(unittest.TestCase):
    def test_phase1_predictions_are_preserved_and_extended(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE predictions (
                    prediction_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                INSERT INTO predictions VALUES ('legacy', 'AAPL', '2026-01-01T00:00:00+00:00', 'WAIT', '{}');
                """
            )
            connection.commit()
            connection.close()

            store = SQLiteStore(path)
            store.initialize()
            self.assertEqual(store.schema_version(), 5)
            self.assertEqual(store.counts()["predictions"], 1)
            self.assertEqual(store.counts()["watchlist_entries"], 0)
            self.assertEqual(store.counts()["app_settings"], 0)

            instrument = instrument_for("AAPL")
            bars = FixtureProvider().get_bars(instrument, "1h", 120)
            quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
            signal = build_signal(
                instrument,
                quant,
                generated_at=datetime.now(timezone.utc),
                model_id="test-model",
                model_version="test-version",
                prompt_version="phase2-test-v1",
                parse_status="valid",
            )
            store.save_prediction(signal)
            self.assertEqual(store.counts()["predictions"], 2)
            payload = store.load_prediction_payload(signal.prediction_id)
            self.assertEqual(payload["model_id"], "test-model")


if __name__ == "__main__":
    unittest.main()
