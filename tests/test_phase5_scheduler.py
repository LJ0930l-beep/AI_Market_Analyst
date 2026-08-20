import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.analysis_service import AnalysisError
from core.instruments import instrument_for
from core.scheduler import (
    CONTEXT_CACHE_VERSION,
    ContextCache,
    LocalResourceProbe,
    LocalSchedulerRuntime,
    ResourceProbeResult,
    SchedulerRuntimeError,
    ScanExecution,
    build_context_cache_key,
    evaluate_market_session,
)
from core.storage import SQLiteStore


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeExecutor:
    def __init__(self, clock: FixedClock, *, failures: set[str] | None = None) -> None:
        self.clock = clock
        self.failures = failures or set()
        self.calls = 0
        self.called = threading.Event()
        self.active = 0
        self.max_active = 0

    def execute(self, instrument, *, timeframe, analysis_time, context_capability, cache):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if instrument.symbol in self.failures:
                raise AnalysisError("provider unavailable", code="provider_unavailable", provider="fake")
            cache_key = build_context_cache_key(
                symbol=instrument.symbol,
                timeframe=timeframe,
                as_of=analysis_time,
                provider="fake-provider",
                context_capability=context_capability,
            )
            cached = cache.get(cache_key, now=analysis_time)
            if cached is not None:
                return ScanExecution(
                    prediction_id=cached.prediction_id,
                    action="WAIT",
                    cache_key=cache_key,
                    cache_status="hit",
                    cache_metadata=cached.to_metadata(),
                    provider="fake-provider",
                    as_of=analysis_time.isoformat(),
                )
            self.calls += 1
            self.called.set()
            prediction_id = f"fake-{instrument.symbol}-{self.calls}"
            entry = cache.put(
                cache_key,
                prediction_id=prediction_id,
                metadata={
                    "symbol": instrument.symbol,
                    "timeframe": timeframe,
                    "as_of": analysis_time.isoformat(),
                    "provider": "fake-provider",
                    "context_capability": context_capability,
                },
                now=analysis_time,
            )
            return ScanExecution(
                prediction_id=prediction_id,
                action="WAIT",
                cache_key=cache_key,
                cache_status="miss",
                cache_metadata=entry.to_metadata(),
                provider="fake-provider",
                as_of=analysis_time.isoformat(),
            )
        finally:
            self.active -= 1


class MutableProbe:
    def __init__(self, result: ResourceProbeResult) -> None:
        self.result = result

    def probe(self) -> ResourceProbeResult:
        return self.result


def ready_probe() -> MutableProbe:
    return MutableProbe(ResourceProbeResult(True, capability="injected_ready"))


class BlockingExecutor(FakeExecutor):
    def __init__(self, clock: FixedClock) -> None:
        super().__init__(clock)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.executed_symbols: list[str] = []

    def execute(self, instrument, *, timeframe, analysis_time, context_capability, cache):
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("blocking executor timed out")
        self.executed_symbols.append(instrument.symbol)
        return super().execute(
            instrument,
            timeframe=timeframe,
            analysis_time=analysis_time,
            context_capability=context_capability,
            cache=cache,
        )


class NvidiaSmiRunner:
    def __init__(self, processes: str, memory: str = "8192") -> None:
        self.processes = processes
        self.memory = memory
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, command: list[str], timeout_seconds: float) -> str:
        self.calls.append((command, timeout_seconds))
        if any("query-compute-apps" in value for value in command):
            return self.processes
        return self.memory


class SchedulerCoreTests(unittest.TestCase):
    def test_local_resource_probe_is_read_only_bounded_and_conservative(self):
        healthy_runner = NvidiaSmiRunner("123, Ollama, 100\n")
        healthy = LocalResourceProbe(command_runner=healthy_runner, timeout_seconds=0.25).probe()
        self.assertTrue(healthy.available)
        self.assertEqual(healthy.capability, "nvidia_smi")
        self.assertEqual(len(healthy_runner.calls), 2)
        self.assertTrue(all(isinstance(command, list) for command, _timeout in healthy_runner.calls))
        self.assertTrue(all(timeout == 0.25 for _command, timeout in healthy_runner.calls))

        for process_name, reason in (
            ("ComfyUI", "comfyui_gpu_competition"),
            ("python3", "python_gpu_worker_competition"),
            ("blender", "blender_gpu_competition"),
        ):
            result = LocalResourceProbe(command_runner=NvidiaSmiRunner(f"456, {process_name}, 200\n")).probe()
            self.assertFalse(result.available)
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.capability, "nvidia_smi")

        low_memory = LocalResourceProbe(command_runner=NvidiaSmiRunner("", "512\n")).probe()
        self.assertFalse(low_memory.available)
        self.assertEqual(low_memory.reason, "gpu_low_free_memory")
        self.assertEqual(low_memory.details["minimum_free_memory_mb"], 1024)

        def timed_out(_command: list[str], _timeout_seconds: float) -> str:
            raise TimeoutError("runner timeout")

        unavailable = LocalResourceProbe(command_runner=timed_out).probe()
        self.assertFalse(unavailable.available)
        self.assertEqual(unavailable.capability, "probe_unavailable")
        self.assertEqual(unavailable.reason, "gpu_probe_unavailable")

    def test_local_resource_probe_treats_ollama_as_allowed_and_reports_unknown_compute_process(self):
        ollama = LocalResourceProbe(command_runner=NvidiaSmiRunner("123, /usr/bin/ollama, 100\n")).probe()
        self.assertTrue(ollama.available)
        unknown = LocalResourceProbe(command_runner=NvidiaSmiRunner("456, custom_gpu_worker, 100\n")).probe()
        self.assertFalse(unknown.available)
        self.assertEqual(unknown.reason, "gpu_competition")

    def test_session_policy_handles_dst_weekend_close_crypto_and_explicit_always(self):
        equity = instrument_for("AAPL")
        crypto = instrument_for("BTCUSDT")

        self.assertTrue(evaluate_market_session(equity, datetime(2024, 3, 8, 14, 30, tzinfo=timezone.utc), "market_hours").allowed)
        self.assertTrue(evaluate_market_session(equity, datetime(2024, 3, 11, 13, 30, tzinfo=timezone.utc), "market_hours").allowed)
        self.assertEqual(
            evaluate_market_session(equity, datetime(2024, 3, 9, 15, 0, tzinfo=timezone.utc), "market_hours").state,
            "WEEKEND",
        )
        self.assertEqual(
            evaluate_market_session(equity, datetime(2024, 3, 11, 13, 29, tzinfo=timezone.utc), "market_hours").state,
            "MARKET_CLOSED",
        )
        self.assertEqual(
            evaluate_market_session(equity, datetime(2024, 3, 11, 20, 0, tzinfo=timezone.utc), "market_hours").state,
            "MARKET_CLOSED",
        )
        crypto_decision = evaluate_market_session(crypto, datetime(2024, 3, 9, 15, 0, tzinfo=timezone.utc), "market_hours")
        self.assertTrue(crypto_decision.allowed)
        self.assertEqual(crypto_decision.reason, "crypto_24_7")
        always = evaluate_market_session(equity, datetime(2024, 3, 9, 15, 0, tzinfo=timezone.utc), "always")
        self.assertTrue(always.allowed)
        self.assertEqual(always.reason, "explicit_always_policy")
        self.assertIn("holiday_calendar_not_implemented", always.limitations)

    def test_cache_key_contains_every_identity_dimension_and_ttl_capacity_restart_boundary(self):
        capability = {"news": "fixture", "context": "v1"}
        base = build_context_cache_key(
            symbol="AAPL", timeframe="1h", as_of="2030-01-02T12:00:00+00:00", provider="fixture", context_capability=capability
        )
        self.assertIn('"symbol":"AAPL"', base)
        for change in (
            {"symbol": "NVDA"},
            {"timeframe": "4h"},
            {"as_of": "2030-01-02T12:01:00+00:00"},
            {"provider": "other"},
            {"context_capability": {"news": "live", "context": "v1"}},
        ):
            fields = {"symbol": "AAPL", "timeframe": "1h", "as_of": "2030-01-02T12:00:00+00:00", "provider": "fixture", "context_capability": capability}
            fields.update(change)
            self.assertNotEqual(base, build_context_cache_key(**fields))

        clock = FixedClock(datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc))
        cache = ContextCache(ttl_seconds=10, capacity=1, clock=clock.now)
        entry = cache.put(base, prediction_id="prediction-1", metadata={"symbol": "AAPL"})
        self.assertEqual(cache.get(base).prediction_id, entry.prediction_id)
        clock.advance(11)
        self.assertIsNone(cache.get(base))
        second = cache.put("second", prediction_id="prediction-2", metadata={"symbol": "NVDA"})
        cache.put("third", prediction_id="prediction-3", metadata={"symbol": "TSLA"})
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertEqual(cache.stats()["version"], CONTEXT_CACHE_VERSION)
        restarted = ContextCache(ttl_seconds=10, capacity=1, clock=clock.now)
        self.assertEqual(restarted.stats()["entries"], 0)
        self.assertIsNotNone(second)

    def test_v6_scheduler_schema_restart_and_running_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            pre_migration = Path(temp) / "pre-v6.sqlite3"
            connection = sqlite3.connect(pre_migration)
            connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            connection.execute("INSERT INTO schema_migrations VALUES (5, '2030-01-02T00:00:00+00:00')")
            connection.commit()
            connection.close()
            upgraded = SQLiteStore(pre_migration)
            upgraded.initialize()
            self.assertEqual(upgraded.schema_version(), 6)
            self.assertEqual(upgraded.counts()["scheduler_runs"], 0)

            path = Path(temp) / "scheduler.sqlite3"
            store = SQLiteStore(path)
            store.initialize()
            self.assertEqual(store.schema_version(), 6)
            self.assertEqual(store.counts()["scheduler_runs"], 0)
            store.create_scheduler_run(
                run_id="crashed-run",
                trigger="scheduled",
                started_at="2030-01-02T12:00:00+00:00",
                settings={"enabled": True},
            )
            store.create_scheduler_item(item_id="crashed-run:AAPL", run_id="crashed-run", symbol="AAPL", timeframe="1h", status="RUNNING")
            store.update_scheduler_item("crashed-run:AAPL", started_at="2030-01-02T12:00:01+00:00")
            clock = FixedClock(datetime(2030, 1, 2, 12, 1, tzinfo=timezone.utc))
            runtime = LocalSchedulerRuntime(store=store, analysis_executor=FakeExecutor(clock), clock=clock.now)
            self.assertEqual(store.get_scheduler_run("crashed-run")["status"], "INTERRUPTED")
            self.assertEqual(store.list_scheduler_items("crashed-run")[0]["status"], "INTERRUPTED")
            reopened = SQLiteStore(path)
            reopened.initialize()
            self.assertEqual(reopened.schema_version(), 6)
            self.assertEqual(reopened.counts()["scheduler_items"], 1)
            runtime.close()

    def test_run_once_is_disabled_by_default_serial_and_isolates_item_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "run.sqlite3")
            store.initialize()
            store.upsert_watchlist_entry("AAPL")
            store.upsert_watchlist_entry("NVDA")
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            executor = FakeExecutor(clock, failures={"NVDA"})
            runtime = LocalSchedulerRuntime(store=store, analysis_executor=executor, resource_probe=ready_probe(), clock=clock.now)
            with self.assertRaisesRegex(Exception, "scheduler.enabled"):
                runtime.run_once()
            store.upsert_app_setting("scheduler.enabled", True)
            store.upsert_app_setting("scheduler.concurrency", 4)
            result = runtime.run_once()
            self.assertEqual(result["run"]["status"], "COMPLETED_WITH_ERRORS")
            self.assertEqual(result["run"]["counts"]["planned"], 2)
            self.assertEqual(result["run"]["counts"]["completed"], 1)
            self.assertEqual(result["run"]["counts"]["errors"], 1)
            items = {item["symbol"]: item for item in result["items"]}
            self.assertEqual(items["AAPL"]["status"], "COMPLETED")
            self.assertEqual(items["NVDA"]["status"], "ERROR")
            self.assertEqual(executor.max_active, 1)
            self.assertEqual(runtime.status()["effective_concurrency"], 1)
            runtime.close()

    def test_cache_dedupes_scheduler_runs_and_persists_hit_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "cache.sqlite3")
            store.initialize()
            store.upsert_watchlist_entry("AAPL")
            store.upsert_app_setting("scheduler.enabled", True)
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            executor = FakeExecutor(clock)
            runtime = LocalSchedulerRuntime(store=store, analysis_executor=executor, resource_probe=ready_probe(), clock=clock.now)
            first = runtime.run_once()
            second = runtime.run_once()
            self.assertEqual(first["items"][0]["cache_status"], "miss")
            self.assertEqual(second["items"][0]["cache_status"], "hit")
            self.assertEqual(executor.calls, 1)
            self.assertEqual(store.list_scheduler_cache_entries()[0]["hit_count"], 1)
            self.assertEqual(runtime.status()["cache"]["restart_behavior"], "metadata_only_cold_restart")
            runtime.close()

    def test_resource_guard_backoff_is_bounded_and_does_not_kill_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "resource.sqlite3")
            store.initialize()
            store.upsert_watchlist_entry("AAPL")
            store.upsert_app_setting("scheduler.enabled", True)
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            probe = MutableProbe(ResourceProbeResult(False, reason="comfyui_gpu_competition", retry_after_seconds=99_999, capability="injected_test"))
            runtime = LocalSchedulerRuntime(
                store=store,
                analysis_executor=FakeExecutor(clock),
                resource_probe=probe,
                clock=clock.now,
                backoff_base_seconds=5,
                backoff_max_seconds=20,
            )
            result = runtime.run_once()
            self.assertEqual(result["run"]["counts"]["skipped_resource"], 1)
            self.assertEqual(result["items"][0]["status"], "SKIPPED")
            self.assertEqual(result["items"][0]["resource_reason"], "comfyui_gpu_competition")
            self.assertLessEqual(result["status"]["backoff"]["remaining_seconds"], 20)
            self.assertEqual(result["status"]["resource"]["capability"], "injected_test")
            probe.result = ResourceProbeResult(True, capability="injected_test")
            runtime.close()
            restarted = LocalSchedulerRuntime(
                store=SQLiteStore(Path(temp) / "resource.sqlite3"),
                analysis_executor=FakeExecutor(clock),
                resource_probe=probe,
                clock=clock.now,
                backoff_base_seconds=5,
                backoff_max_seconds=20,
            )
            self.assertEqual(restarted.status()["backoff"]["reason"], "comfyui_gpu_competition")
            restarted.close()

    def test_backoff_blocks_manual_and_background_until_restored_retry_time(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "backoff-restart.sqlite3"
            store = SQLiteStore(path)
            store.initialize()
            store.upsert_watchlist_entry("BTCUSDT")
            store.upsert_app_setting("scheduler.enabled", True)
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            blocked = MutableProbe(ResourceProbeResult(False, reason="provider_unavailable", retry_after_seconds=30, capability="injected"))
            first = LocalSchedulerRuntime(
                store=store,
                analysis_executor=FakeExecutor(clock),
                resource_probe=blocked,
                clock=clock.now,
                backoff_base_seconds=5,
                backoff_max_seconds=60,
            )
            first.run_once()
            first.close()

            executor = FakeExecutor(clock)
            restarted = LocalSchedulerRuntime(
                store=SQLiteStore(path),
                analysis_executor=executor,
                resource_probe=ready_probe(),
                clock=clock.now,
                backoff_base_seconds=5,
                backoff_max_seconds=60,
            )
            with self.assertRaises(SchedulerRuntimeError) as raised:
                restarted.run_once()
            self.assertEqual(raised.exception.code, "SCHEDULER_BACKOFF_ACTIVE")
            self.assertEqual(raised.exception.context["reason"], "provider_unavailable")
            self.assertEqual(raised.exception.context["next_retry_at"], restarted.status()["backoff"]["next_retry_at"])
            self.assertGreater(raised.exception.context["remaining_seconds"], 0)
            self.assertEqual(raised.exception.context["remaining"], raised.exception.context["remaining_seconds"])

            restarted.start()
            time.sleep(0.1)
            self.assertEqual(executor.calls, 0)
            clock.advance(61)
            self.assertTrue(executor.called.wait(timeout=2))
            restarted.stop()

    def test_process_lease_is_canonical_per_database_and_releases_for_other_instances(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "alias").mkdir()
            first_path = root / "shared.sqlite3"
            second_path = root / "alias" / ".." / "shared.sqlite3"
            first_store = SQLiteStore(first_path)
            second_store = SQLiteStore(second_path)
            first_store.initialize()
            second_store.initialize()
            first_store.upsert_app_setting("scheduler.enabled", True)
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            first = LocalSchedulerRuntime(store=first_store, analysis_executor=FakeExecutor(clock), resource_probe=ready_probe(), clock=clock.now)
            second = LocalSchedulerRuntime(store=second_store, analysis_executor=FakeExecutor(clock), resource_probe=ready_probe(), clock=clock.now)
            first.start()
            with self.assertRaises(SchedulerRuntimeError) as started:
                second.start()
            self.assertEqual(started.exception.code, "SCHEDULER_LEASE_HELD")
            with self.assertRaises(SchedulerRuntimeError) as manual:
                second.run_once()
            self.assertEqual(manual.exception.code, "SCHEDULER_LEASE_HELD")
            first.stop()
            second.run_once()
            second.close()

            other_path = root / "other.sqlite3"
            other_store = SQLiteStore(other_path)
            other_store.initialize()
            other_store.upsert_app_setting("scheduler.enabled", True)
            other = LocalSchedulerRuntime(store=other_store, analysis_executor=FakeExecutor(clock), resource_probe=ready_probe(), clock=clock.now)
            other.run_once()
            other.close()

    def test_cooperative_stop_interrupts_remaining_items_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stop.sqlite3"
            store = SQLiteStore(path)
            store.initialize()
            for symbol in ("AAPL", "NVDA", "BTCUSDT"):
                store.upsert_watchlist_entry(symbol)
            store.upsert_app_setting("scheduler.enabled", True)
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            executor = BlockingExecutor(clock)
            runtime = LocalSchedulerRuntime(store=store, analysis_executor=executor, resource_probe=ready_probe(), clock=clock.now)
            runtime.start()
            self.assertTrue(executor.entered.wait(timeout=2))
            with self.assertRaises(SchedulerRuntimeError) as timed_out:
                runtime.stop(timeout_seconds=0.1)
            self.assertEqual(timed_out.exception.code, "SCHEDULER_STOP_TIMEOUT")
            executor.release.set()
            finished = runtime.stop(timeout_seconds=2)
            self.assertFalse(finished["thread_alive"])
            run = store.list_scheduler_runs(limit=1)[0]
            self.assertEqual(run["status"], "INTERRUPTED")
            self.assertEqual(run["counts"]["interrupted"], 2)
            items = store.list_scheduler_items(str(run["run_id"]))
            self.assertTrue(all(item["status"] != "RUNNING" for item in items))
            self.assertEqual(executor.executed_symbols, ["AAPL"])
            second = LocalSchedulerRuntime(store=SQLiteStore(path), analysis_executor=FakeExecutor(clock), resource_probe=ready_probe(), clock=clock.now)
            second.run_once()
            second.close()

    def test_explicit_start_is_singleton_and_stop_leaves_no_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "lifecycle.sqlite3")
            store.initialize()
            store.upsert_app_setting("scheduler.enabled", True)
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            runtime = LocalSchedulerRuntime(store=store, analysis_executor=FakeExecutor(clock), resource_probe=ready_probe(), clock=clock.now)
            runtime.start()
            with self.assertRaisesRegex(Exception, "already running"):
                runtime.start()
            stopped = runtime.stop()
            self.assertFalse(stopped["running"])
            self.assertFalse(stopped["thread_alive"])

    def test_api_exposes_structured_lifecycle_and_history(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "api.sqlite3"
            store = SQLiteStore(path)
            store.initialize()
            store.upsert_watchlist_entry("BTCUSDT")
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            app = create_app(
                store=store,
                scheduler_executor=FakeExecutor(clock),
                scheduler_clock=clock.now,
                scheduler_resource_probe=MutableProbe(ResourceProbeResult(True, capability="injected_test")),
            )
            client = TestClient(app)
            self.assertEqual(client.get("/scheduler/status").json()["enabled"], False)
            disabled = client.post("/scheduler/run-once")
            self.assertEqual(disabled.status_code, 409)
            self.assertEqual(disabled.json()["error"]["code"], "SCHEDULER_DISABLED")
            self.assertEqual(client.put("/settings/scheduler.enabled", json={"value": True}).status_code, 200)
            started = client.post("/scheduler/start")
            self.assertEqual(started.status_code, 200)
            self.assertTrue(started.json()["running"])
            duplicate_start = client.post("/scheduler/start")
            self.assertEqual(duplicate_start.status_code, 409)
            stopped = client.post("/scheduler/stop")
            self.assertEqual(stopped.status_code, 200)
            self.assertFalse(stopped.json()["thread_alive"])
            run = client.post("/scheduler/run-once")
            self.assertEqual(run.status_code, 200)
            self.assertEqual(run.json()["run"]["status"], "COMPLETED")
            history = client.get("/scheduler/history")
            self.assertEqual(history.status_code, 200)
            self.assertGreaterEqual(len(history.json()["runs"]), 2)
            self.assertEqual(client.put("/settings/scheduler.enabled", json={"value": False}).status_code, 200)
            self.assertFalse(client.get("/scheduler/status").json()["thread_alive"])
            self.assertGreaterEqual(client.get("/stats").json()["scheduler_runs"], 1)

    def test_api_exposes_backoff_context_for_manual_run_once_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "api-backoff.sqlite3")
            store.initialize()
            store.upsert_watchlist_entry("BTCUSDT")
            clock = FixedClock(datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc))
            app = create_app(
                store=store,
                scheduler_executor=FakeExecutor(clock),
                scheduler_clock=clock.now,
                scheduler_resource_probe=MutableProbe(
                    ResourceProbeResult(False, reason="provider_unavailable", retry_after_seconds=30, capability="injected")
                ),
            )
            client = TestClient(app)
            self.assertEqual(client.put("/settings/scheduler.enabled", json={"value": True}).status_code, 200)
            self.assertEqual(client.post("/scheduler/run-once").status_code, 200)
            blocked = client.post("/scheduler/run-once")
            self.assertEqual(blocked.status_code, 409)
            payload = blocked.json()["error"]
            self.assertEqual(payload["code"], "SCHEDULER_BACKOFF_ACTIVE")
            self.assertEqual(payload["context"]["reason"], "provider_unavailable")
            self.assertIn("next_retry_at", payload["context"])
            self.assertIn("remaining_seconds", payload["context"])


if __name__ == "__main__":
    unittest.main()
