import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.instruments import instrument_for, parse_instrument_candidate
from core.outcomes import OutcomeStatus
from core.performance.metrics import build_performance_snapshot
from core.providers import Bar, ProviderError, Quote
from core.scheduler import LocalSchedulerRuntime, ResourceProbeResult, ScanExecution
from core.settlement import LIVE_PERFORMANCE_VERSION, SettlementService
from core.signals import Action, SignalProposal
from core.storage import SQLiteStore


POINT = datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc)


def make_signal(
    prediction_id: str,
    instrument,
    *,
    action: Action = Action.LONG,
    generated_at: datetime = POINT - timedelta(hours=2),
) -> SignalProposal:
    if action is Action.LONG:
        levels = (100.0, 100.0, 95.0, 107.5, 112.5)
    elif action is Action.SHORT:
        levels = (100.0, 100.0, 105.0, 92.5, 87.5)
    else:
        levels = (None, None, None, None, None)
    return SignalProposal(
        prediction_id=prediction_id,
        instrument=instrument,
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
        raw_confidence=0.73,
        reason_codes=("test",),
        summary="deterministic settlement test signal",
        context_json="{}",
    )


def tp1_bar(timestamp: datetime) -> Bar:
    return Bar(timestamp, 100.0, 108.0, 99.0, 106.0, 100.0)


class PointProvider:
    provider_name = "injected_point_provider"
    stale = False

    def __init__(self, bars: dict[str, list[Bar]], failures: set[str] | None = None) -> None:
        self.bars = bars
        self.failures = failures or set()
        self.bar_calls: list[tuple[str, str]] = []

    def get_bars(self, instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        self.bar_calls.append((instrument.symbol, timeframe))
        if instrument.symbol in self.failures:
            raise ProviderError("injected provider unavailable", code="unavailable", provider=self.provider_name)
        return list(self.bars.get(instrument.symbol, ()))[:limit]

    def get_quote(self, instrument) -> Quote:
        bars = self.bars.get(instrument.symbol, ())
        if instrument.symbol in self.failures or not bars:
            raise ProviderError("injected quote unavailable", code="unavailable", provider=self.provider_name)
        bar = bars[-1]
        return Quote(instrument=instrument, timestamp=bar.timestamp, price=bar.close, volume=bar.volume)


class FakeScanExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, instrument, *, timeframe, analysis_time, context_capability, cache):
        self.calls += 1
        return ScanExecution(prediction_id=f"scan-{instrument.symbol}", action="WAIT", status="COMPLETED")


class SettlementTests(unittest.TestCase):
    def _store(self, path: Path) -> SQLiteStore:
        store = SQLiteStore(path)
        store.initialize()
        return store

    def test_settles_wait_non_watchlist_registered_and_reuses_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "settlement.sqlite3")
            aapl = instrument_for("AAPL")
            registered = parse_instrument_candidate("SOL", "crypto").instrument
            store.save_instrument(registered, registry_source="registered", metadata_status="inferred")
            signals = [
                make_signal("live-a", aapl),
                make_signal("live-b", aapl, generated_at=POINT - timedelta(hours=3)),
                make_signal("live-wait", instrument_for("BTCUSDT"), action=Action.WAIT),
                make_signal("live-registered", registered),
            ]
            for signal in signals:
                store.save_prediction(signal)
            raw_confidence_before = store.load_prediction_payload("live-a")["raw_confidence"]
            calibration_count_before = store.counts()["calibration_results"]
            provider = PointProvider({"AAPL": [tp1_bar(POINT - timedelta(hours=1))], "SOLUSDT": [tp1_bar(POINT - timedelta(hours=1))]})
            factory_calls: list[str] = []

            def factory(instrument):
                factory_calls.append(instrument.symbol)
                return provider

            service = SettlementService(store=store, provider_factory=factory, clock=lambda: POINT)
            result = service.run_once(run_id="run-1", as_of=POINT)
            counts = result["counts"]
            self.assertEqual(counts["planned"], 4)
            self.assertEqual(counts["settled"], 4)
            self.assertEqual(counts["wait"], 1)
            self.assertEqual(counts["provider_fetches"], 2)
            self.assertEqual(counts["provider_reused"], 1)
            self.assertEqual(factory_calls.count("AAPL"), 1)
            self.assertEqual(store.get_outcome_record("live-a")["outcome_status"], OutcomeStatus.TP1.value)
            self.assertEqual(store.get_outcome_record("live-wait")["outcome_status"], OutcomeStatus.NOT_ACTIONABLE.value)
            self.assertEqual(store.get_outcome_record("live-registered")["outcome_status"], OutcomeStatus.TP1.value)
            self.assertEqual(store.counts()["paper_trades"], 0)
            self.assertEqual(store.load_prediction_payload("live-a")["raw_confidence"], raw_confidence_before)
            self.assertEqual(store.counts()["calibration_results"], calibration_count_before)
            records = store.list_prediction_records(source_type="live")
            direct = build_performance_snapshot(records, scope={"source_type": "live"})
            snapshot = store.list_performance_snapshots(source_type="live", limit=1)[0]
            self.assertEqual(snapshot["scope"], {"source_type": "live"})
            self.assertEqual(snapshot["sample_count"], len(records))
            self.assertEqual(snapshot["metrics"]["sample_count"], direct["metrics"]["sample_count"])
            self.assertEqual(snapshot["metrics"]["resolved_actionable"], direct["metrics"]["resolved_actionable"])
            self.assertEqual(snapshot["audit_metadata"]["snapshot_version"], LIVE_PERFORMANCE_VERSION)
            with TestClient(create_app(store=store)) as client:
                api_summary = client.get("/performance/summary?source_type=live")
            self.assertEqual(api_summary.status_code, 200)
            self.assertEqual(api_summary.json()["sample_count"], snapshot["sample_count"])
            self.assertEqual(api_summary.json()["metrics"]["resolved_actionable"], direct["metrics"]["resolved_actionable"])

            second = service.run_once(run_id="run-2", as_of=POINT + timedelta(hours=1))
            self.assertEqual(second["counts"]["planned"], 0)
            self.assertEqual(second["counts"]["performance_reused"], 1)
            self.assertEqual(store.counts()["outcomes"], 4)
            self.assertEqual(store.counts()["performance_snapshots"], 1)

    def test_bad_payload_isolated_from_valid_prediction_and_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "bad-payload.sqlite3")
            valid = make_signal("valid", instrument_for("AAPL"))
            broken = make_signal("broken", instrument_for("NVDA"))
            store.save_prediction(valid)
            store.save_prediction(broken)
            with store._connect() as db:
                db.execute("UPDATE predictions SET payload_json = ? WHERE prediction_id = ?", ("{", "broken"))
            provider = PointProvider({"AAPL": [tp1_bar(POINT - timedelta(hours=1))]})
            service = SettlementService(store=store, provider_factory=lambda _instrument: provider, clock=lambda: POINT)
            result = service.run_once(run_id="bad-run", as_of=POINT)
            self.assertEqual(result["counts"]["errors"], 1)
            self.assertEqual(result["counts"]["settled"], 1)
            self.assertEqual(store.get_outcome_record("valid")["outcome_status"], OutcomeStatus.TP1.value)
            quarantined = store.get_latest_settlement_item("broken")
            self.assertEqual(quarantined["skip_reason"], "PAYLOAD_QUARANTINED")
            self.assertIsNotNone(quarantined["retry_after_at"])
            retry = service.run_once(run_id="bad-run-2", as_of=POINT + timedelta(seconds=30))
            self.assertEqual(retry["counts"]["planned"], 0)
            self.assertEqual(retry["counts"]["errors"], 0)

    def test_provider_failure_is_pending_with_bounded_retry_and_does_not_block_other_group(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "provider-failure.sqlite3")
            store.save_prediction(make_signal("good", instrument_for("AAPL")))
            store.save_prediction(make_signal("bad-provider", instrument_for("NVDA")))
            provider = PointProvider({"AAPL": [tp1_bar(POINT - timedelta(hours=1))]}, failures={"NVDA"})
            service = SettlementService(store=store, provider_factory=lambda _instrument: provider, clock=lambda: POINT)
            result = service.run_once(run_id="provider-run", as_of=POINT)
            self.assertEqual(result["counts"]["settled"], 1)
            self.assertEqual(result["counts"]["provider_errors"], 1)
            self.assertEqual(result["counts"]["pending"], 1)
            pending_item = store.get_latest_settlement_item("bad-provider")
            self.assertEqual(pending_item["status"], "PENDING")
            self.assertEqual(pending_item["capability"]["provider_available"], False)

            again = service.run_once(run_id="provider-run-2", as_of=POINT + timedelta(seconds=30))
            self.assertEqual(again["counts"]["planned"], 0)
            self.assertEqual(again["counts"]["pending"], 0)
            self.assertEqual(len(provider.bar_calls), 1)

    def test_active_retry_does_not_starve_new_prediction_with_small_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "batch-fairness.sqlite3")
            store.save_prediction(make_signal("old-provider-failure", instrument_for("NVDA")))
            provider = PointProvider({"AAPL": [tp1_bar(POINT - timedelta(hours=1))]}, failures={"NVDA"})
            service = SettlementService(
                store=store,
                provider_factory=lambda _instrument: provider,
                clock=lambda: POINT,
                batch_size=1,
            )
            first = service.run_once(run_id="batch-run-1", as_of=POINT)
            self.assertEqual(first["counts"]["provider_errors"], 1)
            store.save_prediction(make_signal("new-after-failure", instrument_for("AAPL")))
            candidates = store.list_unsettled_live_prediction_records(limit=1, as_of=POINT + timedelta(seconds=30))
            self.assertEqual([item["prediction_id"] for item in candidates], ["new-after-failure"])
            second = service.run_once(run_id="batch-run-2", as_of=POINT + timedelta(seconds=30))
            self.assertEqual(second["counts"]["planned"], 1)
            self.assertEqual(second["counts"]["settled"], 1)
            self.assertIsNotNone(store.get_outcome_record("new-after-failure"))

    def test_performance_refresh_reuses_equivalent_metrics_and_prunes_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "performance-retention.sqlite3")
            service = SettlementService(
                store=store,
                provider_factory=lambda _instrument: PointProvider({}),
                clock=lambda: POINT,
                snapshot_retention=2,
            )
            for index in range(4):
                store.save_prediction(
                    make_signal(
                        f"retention-wait-{index}",
                        instrument_for("BTCUSDT"),
                        action=Action.WAIT,
                        generated_at=POINT - timedelta(hours=index + 2),
                    )
                )
                result = service.run_once(run_id=f"retention-{index}", as_of=POINT + timedelta(minutes=index))
                self.assertEqual(result["counts"]["performance_refreshes"], 1)
            self.assertEqual(store.counts()["performance_snapshots"], 2)
            reused = service.run_once(run_id="retention-reused", as_of=POINT + timedelta(hours=1))
            self.assertEqual(reused["counts"]["performance_reused"], 1)
            self.assertEqual(store.counts()["performance_snapshots"], 2)
            self.assertTrue(store.get_scheduler_state("last_performance_refresh")["reused"])

    def test_pending_restarts_and_final_outcome_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "restart.sqlite3"
            generated = POINT - timedelta(hours=1)
            signal = make_signal("restart", instrument_for("AAPL"), generated_at=generated)
            first_store = self._store(path)
            first_store.save_prediction(signal)
            provider = PointProvider({"AAPL": [tp1_bar(generated + timedelta(hours=1))]})
            first = SettlementService(store=first_store, provider_factory=lambda _instrument: provider, clock=lambda: generated + timedelta(minutes=30))
            pending = first.run_once(run_id="restart-1", as_of=generated + timedelta(minutes=30))
            self.assertEqual(pending["counts"]["pending"], 1)
            self.assertIsNone(first_store.get_outcome_record("restart"))

            reopened = self._store(path)
            later = SettlementService(store=reopened, provider_factory=lambda _instrument: provider, clock=lambda: generated + timedelta(hours=4))
            settled = later.run_once(run_id="restart-2", as_of=generated + timedelta(hours=4))
            self.assertEqual(settled["counts"]["settled"], 1)
            self.assertEqual(reopened.get_outcome_record("restart")["outcome_status"], OutcomeStatus.TP1.value)
            self.assertEqual(reopened.counts()["outcomes"], 1)

    def test_resource_probe_block_does_not_block_settlement_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            store = self._store(Path(temp) / "runtime.sqlite3")
            store.upsert_watchlist_entry("AAPL", now=POINT)
            signal = make_signal("runtime-prediction", instrument_for("AAPL"))
            store.save_prediction(signal)
            provider = PointProvider({"AAPL": [tp1_bar(POINT - timedelta(hours=1))]})
            settlement = SettlementService(store=store, provider_factory=lambda _instrument: provider, clock=lambda: POINT)
            executor = FakeScanExecutor()
            runtime = LocalSchedulerRuntime(
                store=store,
                analysis_executor=executor,
                resource_probe=lambda: ResourceProbeResult(False, reason="comfyui_gpu_competition"),
                clock=lambda: POINT,
                settlement_service=settlement,
            )
            store.upsert_app_setting("scheduler.enabled", True)
            result = runtime.run_once()
            counts = result["run"]["counts"]
            self.assertEqual(counts["settlement_settled"], 1)
            self.assertEqual(counts["skipped_resource"], 1)
            self.assertEqual(executor.calls, 0)
            self.assertEqual(store.counts()["outcomes"], 1)
            runtime.close()


if __name__ == "__main__":
    unittest.main()
