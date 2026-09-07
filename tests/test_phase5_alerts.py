import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.alerts import AlertReconciler
from core.instruments import instrument_for
from core.outcomes import Outcome, OutcomeStatus
from core.storage import SQLiteStore
from core.signals import Action, SignalProposal


POINT = datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc)


def make_signal(
    prediction_id: str,
    *,
    context: dict[str, object] | None = None,
    action: Action = Action.LONG,
) -> SignalProposal:
    generated_at = POINT - timedelta(hours=2)
    levels = (100.0, 100.0, 95.0, 107.5, 112.5) if action is Action.LONG else (None, None, None, None, None)
    return SignalProposal(
        prediction_id=prediction_id,
        instrument=instrument_for("AAPL"),
        analysis_timeframe="1h",
        generated_at=generated_at,
        action=action,
        entry_low=levels[0],
        entry_high=levels[1],
        stop=levels[2],
        tp1=levels[3],
        tp2=levels[4],
        signal_validity_minutes=60,
        expected_hold_minutes=120,
        max_hold_minutes=180,
        reevaluate_at=generated_at + timedelta(minutes=60),
        invalidation=("test invalidation",),
        raw_confidence=0.72,
        reason_codes=("test",),
        summary="deterministic alert test signal",
        context_json=json.dumps(context or {}, sort_keys=True),
    )


class AlertTests(unittest.TestCase):
    def _store(self, path: Path) -> SQLiteStore:
        store = SQLiteStore(path)
        store.initialize()
        return store

    def test_alert_migration_reopen_and_deduplication_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "alerts.sqlite3"
            store = self._store(path)
            signal = make_signal(
                "live-alert",
                context={
                    "time_policy": {"event_risk": True},
                    "risk_events": [{"source": "calendar", "event_id": "cpi-1", "importance": 80, "title": "CPI"}],
                },
            )
            store.save_prediction(signal)
            store.save_outcome(Outcome("live-alert", OutcomeStatus.TP1, POINT, 107.5, 1.5, 1.5, 0.0, 1, False))

            first = AlertReconciler(store=store, clock=lambda: POINT).run_once(as_of=POINT)
            self.assertEqual(store.schema_version(), 14)
            self.assertEqual(first["counts"]["created"], 3)
            self.assertEqual(store.alert_counts(), {"total": 3, "open": 3, "unread": 3, "acknowledged": 0})

            reopened = self._store(path)
            second = AlertReconciler(store=reopened, clock=lambda: POINT + timedelta(minutes=1)).run_once(as_of=POINT + timedelta(minutes=1))
            self.assertEqual(reopened.schema_version(), 14)
            self.assertEqual(reopened.alert_counts()["total"], 3)
            self.assertEqual(second["counts"]["created"], 0)
            self.assertGreaterEqual(second["counts"]["deduped"], 3)
            rows = {row["source"]: row for row in reopened.list_alerts(limit=100)}
            self.assertEqual(rows["news_event"]["occurrence_count"], 2)
            self.assertIn("context.time_policy.event_risk", rows["news_event"]["evidence"]["provenance"])

    def test_news_event_identity_distinguishes_materially_new_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "events.sqlite3")
            signal = make_signal(
                "live-events",
                context={"time_policy": {"event_risk": False}, "news": [{"source": "wire", "event_id": "story-1", "title": "First", "importance": 75}]},
            )
            store.save_prediction(signal)
            first = AlertReconciler(store=store, clock=lambda: POINT).run_once(as_of=POINT)
            first_event = store.list_alerts(source="news_event", limit=1)[0]
            self.assertEqual(first["counts"]["news_event_created"], 1)
            self.assertEqual(first_event["severity"], "WARNING")
            self.assertEqual(first_event["evidence"]["importance"], 75)
            self.assertEqual(first_event["evidence"]["provenance"], ["context.news"])
            payload = store.load_prediction_payload(signal.prediction_id)
            assert payload is not None
            payload["context_json"] = json.dumps({"time_policy": {"event_risk": False}, "news": [{"source": "wire", "event_id": "story-2", "title": "Second"}]})
            with store._connect() as db:  # focused migration/reconciliation test boundary
                db.execute("UPDATE predictions SET payload_json = ?, context_json = ? WHERE prediction_id = ?", (json.dumps(payload), payload["context_json"], signal.prediction_id))
            result = AlertReconciler(store=store, clock=lambda: POINT + timedelta(minutes=2)).run_once(as_of=POINT + timedelta(minutes=2))
            self.assertEqual(result["counts"]["news_event_created"], 1)
            self.assertEqual(store.count_alerts(source="news_event"), 2)

    def test_retention_is_bounded_and_acknowledged_history_is_pruned_first(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "retention.sqlite3")
            for index in range(3):
                store.upsert_alert(
                    alert_id=f"alert-retention-{index}",
                    policy_version="alert_policy_v1",
                    source="operational",
                    severity="WARNING",
                    title=f"Failure {index}",
                    message="bounded test",
                    event_identity=f"operational:{index}",
                    fingerprint=f"fingerprint-{index}",
                    evidence={"index": index},
                    dedupe_key=f"operational:{index}",
                    first_seen_at=(POINT + timedelta(minutes=index)).isoformat(),
                )
            store.acknowledge_alert("alert-retention-0", acknowledged_at=POINT.isoformat())
            result = AlertReconciler(store=store, clock=lambda: POINT, retention_limit=2).run_once(as_of=POINT)
            self.assertEqual(result["retention"]["limit"], 2)
            self.assertEqual(store.alert_counts()["total"], 2)
            self.assertIsNone(store.get_alert("alert-retention-0"))
            self.assertIsNotNone(store.get_alert("alert-retention-1"))
            self.assertIsNotNone(store.get_alert("alert-retention-2"))

    def test_radar_transition_and_operational_failure_are_deduped(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "operational.sqlite3")
            store.upsert_watchlist_entry("AAPL")
            store.save_prediction(make_signal("live-radar"))
            AlertReconciler(store=store, clock=lambda: POINT).run_once(as_of=POINT)
            prior = store.get_scheduler_state("alerts.radar_state")
            self.assertIsInstance(prior, dict)
            store.set_scheduler_state("alerts.radar_state", {"AAPL": {"category": "WAIT", "transition_sequence": 0}})
            store.create_scheduler_run(run_id="run-alert", trigger="test", started_at=POINT.isoformat(), settings={})
            store.create_scheduler_item(item_id="run-alert:AAPL", run_id="run-alert", symbol="AAPL", timeframe="1h", stage="scan")
            store.update_scheduler_item(
                "run-alert:AAPL",
                status="SKIPPED",
                resource_reason="comfyui_competing_gpu",
                as_of=POINT.isoformat(),
                capability={"probe": "nvidia-smi", "available": False},
            )
            reconciler = AlertReconciler(store=store, clock=lambda: POINT)
            first = reconciler.run_once(run_id="run-alert", as_of=POINT)
            second = reconciler.run_once(run_id="run-alert", as_of=POINT + timedelta(minutes=1))
            self.assertEqual(first["counts"]["radar_created"], 1)
            self.assertEqual(first["counts"]["operational_created"], 1)
            self.assertGreaterEqual(second["counts"]["deduped"], 2)
            self.assertEqual(store.count_alerts(source="operational"), 1)

    def test_malformed_prediction_payload_is_quarantined_without_blocking_other_records(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "quarantine.sqlite3")
            bad = make_signal("live-bad-payload")
            good = make_signal("live-good-payload")
            store.save_prediction(bad)
            store.save_prediction(good)
            with store._connect() as db:  # deliberately corrupt one stored record for isolation coverage
                db.execute("UPDATE predictions SET payload_json = ? WHERE prediction_id = ?", (json.dumps({"action": "LONG"}), bad.prediction_id))
            result = AlertReconciler(store=store, clock=lambda: POINT).run_once(as_of=POINT)
            self.assertGreaterEqual(result["counts"]["quarantined"], 1)
            self.assertEqual(store.count_alerts(source="prediction"), 1)
            self.assertEqual(store.list_alerts(source="prediction", limit=1)[0]["prediction_id"], good.prediction_id)

    def test_api_acknowledgement_is_idempotent_and_reads_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "api-alerts.sqlite3")
            store.save_prediction(make_signal("api-alert"))
            AlertReconciler(store=store, clock=lambda: POINT).run_once(as_of=POINT)
            before = store.counts().copy()
            alert_id = store.list_alerts(limit=1)[0]["alert_id"]
            with TestClient(create_app(store=store, llm_provider=None)) as client:
                listing = client.get("/alerts?limit=10")
                status = client.get("/alerts/status")
                invalid_filter = client.get("/alerts?source=unknown")
                acknowledged = client.post(f"/alerts/{alert_id}/acknowledge", json={})
                repeated = client.post(f"/alerts/{alert_id}/acknowledge", json={})
                missing = client.post("/alerts/alert-missing/acknowledge", json={})
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(invalid_filter.status_code, 400)
            self.assertEqual(acknowledged.status_code, 200)
            self.assertEqual(acknowledged.json()["status"], "ACKNOWLEDGED")
            self.assertEqual(repeated.json()["status"], "ACKNOWLEDGED")
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(store.alert_counts()["unread"], 0)
            after = store.counts().copy()
            self.assertEqual(before["predictions"], after["predictions"])
            self.assertEqual(before["paper_trades"], after["paper_trades"])
            self.assertEqual(before["outcomes"], after["outcomes"])


if __name__ == "__main__":
    unittest.main()
