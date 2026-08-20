import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.storage import APP_SETTING_DEFAULTS, SQLiteStore


class Phase5WatchlistTests(unittest.TestCase):
    def test_v5_upgrade_preserves_pre_migration_data_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pre-migration.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                INSERT INTO schema_migrations VALUES (4, '2026-01-01T00:00:00+00:00');
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
            self.assertEqual(store.list_watchlist_entries(), [])
            self.assertEqual(store.list_app_settings()[0]["source"], "default")

            reopened = SQLiteStore(path)
            reopened.initialize()
            self.assertEqual(reopened.schema_version(), 5)
            self.assertEqual(reopened.counts()["predictions"], 1)
            self.assertEqual(reopened.counts()["watchlist_entries"], 0)
            self.assertEqual(reopened.counts()["app_settings"], 0)

    def test_storage_watchlist_and_settings_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "durable.sqlite3"
            first = SQLiteStore(path)
            first.initialize()
            first_added = first.upsert_watchlist_entry(
                "nvda",
                now=datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc),
            )
            second_added = first.upsert_watchlist_entry(
                "NVDA",
                now=datetime(2030, 1, 2, 12, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(first_added["symbol"], "NVDA")
            self.assertEqual(second_added["added_at"], first_added["added_at"])
            self.assertNotEqual(second_added["updated_at"], first_added["updated_at"])
            self.assertEqual(len(first.list_watchlist_entries()), 1)
            with self.assertRaises(ValueError):
                first.upsert_watchlist_entry("not-supported")

            defaults = {item["key"]: item for item in first.list_app_settings()}
            self.assertEqual(defaults["scheduler.enabled"]["value"], False)
            self.assertEqual(defaults["scheduler.interval_seconds"]["value"], 900)
            self.assertEqual(defaults["scheduler.concurrency"]["value"], 1)
            self.assertEqual(defaults["scheduler.session_policy"]["value"], "market_hours")
            stored = first.upsert_app_setting(
                "scheduler.interval_seconds",
                600,
                now=datetime(2030, 1, 2, 12, 2, tzinfo=timezone.utc),
            )
            self.assertEqual(stored["value"], 600)
            self.assertEqual(stored["source"], "stored")
            with self.assertRaises(ValueError):
                first.upsert_app_setting("scheduler.interval_seconds", 30)
            with self.assertRaises(ValueError):
                first.upsert_app_setting("provider.token", "secret")

            restarted = SQLiteStore(path)
            restarted.initialize()
            self.assertEqual(restarted.list_watchlist_entries(), [second_added])
            self.assertEqual(restarted.get_app_setting("scheduler.interval_seconds")["value"], 600)
            self.assertTrue(restarted.delete_watchlist_entry("NVDA"))
            self.assertFalse(restarted.delete_watchlist_entry("NVDA"))
            self.assertTrue(restarted.delete_app_setting("scheduler.interval_seconds"))
            self.assertFalse(restarted.delete_app_setting("scheduler.interval_seconds"))
            self.assertEqual(
                restarted.get_app_setting("scheduler.interval_seconds")["value"],
                APP_SETTING_DEFAULTS["scheduler.interval_seconds"],
            )

    def test_api_crud_is_canonical_typed_and_restart_durable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "api.sqlite3"
            store = SQLiteStore(path)
            app = create_app(store=store)
            client = TestClient(app)
            before = client.get("/stats").json()

            self.assertEqual(client.get("/watchlist").json(), [])
            added = client.post("/watchlist", json={"symbol": "btc"})
            self.assertEqual(added.status_code, 200)
            self.assertEqual(added.json()["symbol"], "BTCUSDT")
            self.assertEqual(added.json()["instrument"]["exchange"], "PUBLIC")
            self.assertIn("added_at", added.json())
            self.assertIn("updated_at", added.json())

            updated = client.put("/watchlist/BTCUSDT", json={"symbol": "BTC"})
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["added_at"], added.json()["added_at"])
            self.assertEqual(len(client.get("/watchlist").json()), 1)
            self.assertEqual(client.post("/watchlist", json={"symbol": "NVDA", "payload": {}}).status_code, 400)
            mismatch = client.put("/watchlist/NVDA", json={"symbol": "TSLA"})
            self.assertEqual(mismatch.status_code, 400)
            invalid = client.post("/watchlist", json={"symbol": "NOPE"})
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(invalid.json()["error"]["code"], "INVALID_SYMBOL")

            deleted = client.delete("/watchlist/btc")
            self.assertEqual(deleted.json(), {"symbol": "BTCUSDT", "deleted": True})
            self.assertEqual(client.delete("/watchlist/BTCUSDT").json(), {"symbol": "BTCUSDT", "deleted": False})

            settings = client.get("/settings")
            self.assertEqual(settings.status_code, 200)
            self.assertEqual({item["key"] for item in settings.json()}, set(APP_SETTING_DEFAULTS))
            self.assertTrue(all(item["source"] == "default" for item in settings.json()))
            changed = client.put("/settings/scheduler.interval_seconds", json={"value": 600})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.json()["value"], 600)
            self.assertEqual(client.get("/settings/scheduler.interval_seconds").json()["value"], 600)
            invalid_setting = client.put("/settings/scheduler.interval_seconds", json={"value": 30})
            self.assertEqual(invalid_setting.status_code, 400)
            self.assertEqual(invalid_setting.json()["error"]["code"], "INVALID_APP_SETTING_VALUE")
            unknown_setting = client.get("/settings/provider.token")
            self.assertEqual(unknown_setting.status_code, 404)
            reset = client.delete("/settings/scheduler.interval_seconds")
            self.assertTrue(reset.json()["deleted"])
            self.assertEqual(reset.json()["setting"]["value"], 900)
            self.assertFalse(client.delete("/settings/scheduler.interval_seconds").json()["deleted"])

            after = client.get("/stats").json()
            self.assertEqual(after["predictions"], before["predictions"])
            self.assertEqual(after["paper_trades"], before["paper_trades"])
            self.assertEqual(after["outcomes"], before["outcomes"])
            self.assertEqual(after["watchlist_entries"], 0)
            self.assertEqual(after["app_settings"], 0)

            client.post("/watchlist", json={"symbol": "AAPL"})
            client.put("/settings/scheduler.enabled", json={"value": True})
            populated = client.get("/stats").json()
            self.assertEqual(populated["watchlist_entries"], 1)
            self.assertEqual(populated["app_settings"], 1)
            restarted_store = SQLiteStore(path)
            restarted_client = TestClient(create_app(store=restarted_store))
            self.assertEqual(restarted_client.get("/watchlist").json()[0]["symbol"], "AAPL")
            self.assertEqual(restarted_client.get("/settings/scheduler.enabled").json()["value"], True)


if __name__ == "__main__":
    unittest.main()
