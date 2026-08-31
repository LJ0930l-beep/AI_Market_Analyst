import json
import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import core.backup as backup_module
from apps.api.main import API_PHASE, API_VERSION, create_app
from core.backup import BackupError, backup_database, restore_database
from core.config import database_path_from_env
from core.instruments import instrument_for
from core.providers import FixtureProvider
from core.quant import build_quant_snapshot
from core.signals import build_signal
from core.storage import SQLiteStore


class Phase7BackupTests(unittest.TestCase):
    def _seed(self, path: Path, prediction_id: str = "phase7-prediction") -> SQLiteStore:
        store = SQLiteStore(path)
        store.initialize()
        instrument = instrument_for("NVDA")
        bars = FixtureProvider().get_bars(instrument, "1h", 120)
        quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
        signal = build_signal(instrument, quant, prediction_id=prediction_id, generated_at=bars[-1].timestamp)
        store.save_instrument(instrument)
        store.save_prediction(signal)
        store.upsert_watchlist_entry("NVDA")
        return store

    def test_consistent_backup_restore_preserves_domain_counts_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            backup = root / "backup-artifact"
            source_store = self._seed(source)
            source_counts = source_store.counts()
            manifest = backup_database(source, backup)
            self.assertEqual(manifest["format_version"], "phase7_backup_v1")
            self.assertEqual(manifest["app_version"], "1.2.0")
            self.assertEqual(manifest["schema_version"], 12)
            self.assertEqual(manifest["counts"]["predictions"], source_counts["predictions"])

            self._seed(target, prediction_id="different-target")
            result = restore_database(backup, target)
            self.assertTrue(result["restored"])
            self.assertIsNotNone(result["safety_backup"])
            self.assertTrue(Path(result["safety_backup"]).is_dir())

            reopened = SQLiteStore(target)
            reopened.initialize()
            self.assertEqual(reopened.counts(), source_counts)
            self.assertIsNotNone(reopened.load_prediction_payload("phase7-prediction"))
            self.assertIsNone(reopened.load_prediction_payload("different-target"))

    def test_tampered_artifact_is_rejected_before_target_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            backup = root / "backup-artifact"
            self._seed(source)
            target_store = self._seed(target, prediction_id="target-only")
            original_counts = target_store.counts()
            backup_database(source, backup)
            database = backup / "database.sqlite3"
            database.write_bytes(database.read_bytes() + b"tamper")
            with self.assertRaises(BackupError):
                restore_database(backup, target)
            self.assertEqual(SQLiteStore(target).counts(), original_counts)

    def test_corrupt_database_is_rejected_even_with_updated_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            backup = root / "backup-artifact"
            self._seed(source)
            target_store = self._seed(target, prediction_id="target-only")
            original_counts = target_store.counts()
            backup_database(source, backup)
            database = backup / "database.sqlite3"
            database.write_bytes(b"not-a-sqlite-database")
            manifest_path = backup / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["database_sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(BackupError):
                restore_database(backup, target)
            self.assertEqual(SQLiteStore(target).counts(), original_counts)

    def test_restore_replace_failure_keeps_target_and_safety_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            backup = root / "backup-artifact"
            self._seed(source)
            target_store = self._seed(target, prediction_id="target-only")
            original_counts = target_store.counts()
            backup_database(source, backup)
            real_replace = os.replace

            def fail_target_replace(source_name, target_name):
                if Path(target_name).resolve() == target.resolve():
                    raise OSError("simulated target replace failure")
                return real_replace(source_name, target_name)

            with patch("core.backup.os.replace", side_effect=fail_target_replace):
                with self.assertRaises(BackupError) as raised:
                    restore_database(backup, target)
            self.assertIn("safety backup", str(raised.exception))
            self.assertEqual(SQLiteStore(target).counts(), original_counts)
            self.assertTrue(list(root.glob("target.sqlite3.pre-restore-*")))

    def test_restore_rejects_target_sidecar_before_safety_backup_or_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            backup = root / "backup-artifact"
            self._seed(source)
            target_store = self._seed(target, prediction_id="target-only")
            original_counts = target_store.counts()
            backup_database(source, backup)
            Path(f"{target}-wal").write_bytes(b"active-wal-marker")
            with self.assertRaisesRegex(BackupError, "offline.*sidecar"):
                restore_database(backup, target)
            self.assertEqual(SQLiteStore(target).counts(), original_counts)
            self.assertFalse(list(root.glob("target.sqlite3.pre-restore-*")))

    def test_restore_rejects_persisted_wal_mode_before_safety_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            backup = root / "backup-artifact"
            self._seed(source)
            self._seed(target, prediction_id="target-only")
            backup_database(source, backup)
            connection = sqlite3.connect(target)
            try:
                self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            finally:
                connection.close()
            with self.assertRaisesRegex(BackupError, "offline.*WAL"):
                restore_database(backup, target)
            self.assertFalse(list(root.glob("target.sqlite3.pre-restore-*")))

    def test_new_target_post_replace_failure_is_quarantined(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "new-target.sqlite3"
            backup = root / "backup-artifact"
            self._seed(source)
            backup_database(source, backup)
            real_metadata = backup_module._read_sqlite_metadata
            calls = 0

            def fail_after_replacement(path):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise BackupError("simulated post-replacement validation failure")
                return real_metadata(path)

            with patch("core.backup._read_sqlite_metadata", side_effect=fail_after_replacement):
                with self.assertRaisesRegex(BackupError, "quarantined"):
                    restore_database(backup, target)
            self.assertFalse(target.exists())
            quarantines = list(root.glob("new-target.sqlite3.restore-failed-*"))
            self.assertEqual(len(quarantines), 1)
            self.assertTrue(quarantines[0].is_file())

    def test_existing_target_post_replace_failure_recovers_from_safety_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            target = root / "target.sqlite3"
            backup = root / "backup-artifact"
            self._seed(source)
            target_store = self._seed(target, prediction_id="target-only")
            original_counts = target_store.counts()
            backup_database(source, backup)
            real_metadata = backup_module._read_sqlite_metadata
            calls = 0

            def fail_after_replacement(path):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise BackupError("simulated post-replacement validation failure")
                return real_metadata(path)

            with patch("core.backup._read_sqlite_metadata", side_effect=fail_after_replacement):
                with self.assertRaisesRegex(BackupError, "safety backup"):
                    restore_database(backup, target)
            self.assertEqual(SQLiteStore(target).counts(), original_counts)
            self.assertTrue(list(root.glob("target.sqlite3.pre-restore-*")))

    def test_unsafe_self_restore_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            backup = root / "backup-artifact"
            self._seed(source)
            backup_database(source, backup)
            with self.assertRaises(BackupError):
                restore_database(backup, backup / "database.sqlite3")

    def test_symlink_database_and_broad_destination_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.sqlite3"
            self._seed(source)
            with self.assertRaises(BackupError):
                backup_database(source, Path.cwd())
            link = root / "link.sqlite3"
            try:
                link.symlink_to(source)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this Windows account")
            with self.assertRaises(BackupError):
                backup_database(link, root / "link-backup")


class Phase7ConfigurationAndApiTests(unittest.TestCase):
    def test_local_database_config_rejects_empty_and_accepts_explicit_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "local.sqlite3"
            with patch.dict(os.environ, {"DATABASE_PATH": str(path)}, clear=False):
                self.assertEqual(database_path_from_env(), path.resolve())
            with patch.dict(os.environ, {"DATABASE_PATH": ""}, clear=False):
                with self.assertRaises(ValueError):
                    database_path_from_env()

    def test_release_health_is_versioned_local_and_model_errors_are_redacted(self):
        class BrokenModel:
            provider_name = "test-model"

            def health(self):
                raise RuntimeError("secret local path C:/private/model-token")

        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "health.sqlite3")
            store.initialize()
            app = create_app(store=store, llm_provider=BrokenModel())
            with TestClient(app) as client:
                health = client.get("/health")
                self.assertEqual(health.json(), {
                    "status": "ok",
                    "phase": API_PHASE,
                    "api_version": API_VERSION,
                    "product": "AI Market Analyst",
                    "real_orders": False,
                    "private_keys": False,
                })
                release = client.get("/health/release")
                self.assertEqual(release.status_code, 200)
                self.assertEqual(release.json()["api_version"], "1.2.0")
                self.assertEqual(release.json()["database"]["schema_version"], 12)
                self.assertNotIn(str(Path(temp).resolve()), json.dumps(release.json()))
                self.assertFalse(release.json()["capabilities"]["external_notifications"])
                model = client.get("/health/model")
                self.assertEqual(model.json()["error_code"], "MODEL_HEALTH_ERROR")
                self.assertNotIn("private/model-token", model.text)


if __name__ == "__main__":
    unittest.main()
