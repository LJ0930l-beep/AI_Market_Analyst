from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.instruments import instrument_for
from core.monitoring import MonitoringPolicy, MonitoringService
from core.monitoring_runtime import MonitoringRuntime
from core.providers import Bar, Quote, ProviderError
from core.realtime import RealtimeConnectionState
from core.storage import SQLiteStore


POINT = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)


def bars() -> list[Bar]:
    start = POINT - timedelta(minutes=15 * 64)
    result = [Bar(start + timedelta(minutes=15 * index), 100 + index * 0.5, 100.4 + index * 0.5, 99.6 + index * 0.5, 100 + index * 0.5, 100) for index in range(64)]
    last = result[-1]
    result[-1] = Bar(last.timestamp, last.open, last.close + 3, last.low, last.close + 2, 300)
    return result


class FakeMarketProvider:
    provider_name = "runtime_test_public"
    stale = False

    def get_quote(self, instrument):
        return Quote(instrument, POINT, bars()[-1].close)

    def get_bars(self, _instrument, timeframe, limit=200):
        source = bars()
        if timeframe == "1h":
            source = [Bar(POINT - timedelta(hours=30 - index), 100 + index, 101 + index, 99 + index, 100 + index, 500) for index in range(30)]
        return source[-limit:]


class FailingMarketProvider(FakeMarketProvider):
    def get_quote(self, _instrument):
        raise ProviderError("public endpoint unavailable", code="network_error", provider=self.provider_name)


class FakeSmartProvider:
    def health(self):
        return {"available": True, "model_available": True, "models": ["qwen3.5:9b"]}

    def generate_json(self, _messages, **_kwargs):
        return ({
            "bias": "LONG_WATCH",
            "confidence": 0.84,
            "regime": "bull_trend",
            "watch_zone": {"low": 130, "high": 132},
            "invalidation": {"price": 126, "conditions": ["closed below EMA20"]},
            "targets": [140, 146],
            "holding_horizon": "next_4h",
            "re_evaluate_at": (POINT + timedelta(minutes=30)).isoformat(),
            "evidence": ["runtime closed-bar test"],
            "news_context": [],
            "event_risk": False,
            "missing_evidence": ["news unavailable"],
        }, "{}", {"latency_ms": 1.0})


class FakeStream:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = tuple(symbol.lower() for symbol in symbols)
        self.stop_event = threading.Event()

    def run_forever(self, *, on_bar, on_state=None) -> None:
        if on_state:
            on_state(RealtimeConnectionState("connected", "binance_public_ws", tuple(item.upper() for item in self.symbols), "15m", 0, POINT))
        self.stop_event.wait()
        if on_state:
            on_state(RealtimeConnectionState("stopped", "binance_public_ws", tuple(item.upper() for item in self.symbols), "15m", 0, POINT))

    def stop(self) -> None:
        self.stop_event.set()


def make_runtime(path: Path, provider: object | None = None) -> tuple[SQLiteStore, MonitoringRuntime]:
    store = SQLiteStore(path)
    store.initialize()
    store.save_instrument(instrument_for("BTCUSDT"))
    policy = replace(MonitoringPolicy.defaults("BTCUSDT"), enabled=True, trigger_types=("breakout", "regime", "volume"))
    service = MonitoringService(
        store=store,
        provider_factory=lambda _instrument: provider or FakeMarketProvider(),
        llm_provider=FakeSmartProvider(),
        clock=lambda: POINT,
    )
    service.upsert_policy(policy)
    runtime = MonitoringRuntime(
        store=store,
        service=service,
        stream_factory=lambda symbols: FakeStream(symbols),
        clock=lambda: POINT,
        poll_interval_seconds=0.05,
        max_backoff_seconds=0.2,
    )
    return store, runtime


def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def test_runtime_is_sidecar_owned_and_pause_removes_work(tmp_path: Path) -> None:
    store, runtime = make_runtime(tmp_path / "runtime.sqlite3")
    subscriber = runtime.subscribe()
    try:
        assert runtime.status()["state"] == "stopped"
        assert runtime.status()["run_count"] == 0
        runtime.start()
        wait_for(lambda: int(runtime.status()["run_count"]) >= 1)
        active = runtime.status()
        assert active["active"] is True
        assert active["worker_alive"] is True
        assert active["active_symbols"] == ["BTCUSDT"]
        assert active["resource"]["max_symbols"] == 50
        events = []
        while not subscriber.empty():
            events.append(subscriber.get_nowait())
        def alert_seen() -> bool:
            while not subscriber.empty():
                events.append(subscriber.get_nowait())
            return any(event.get("type") == "alert.created" for event in events)
        wait_for(alert_seen)
        assert any(event.get("type") == "alert.created" for event in events)

        runtime.pause()
        paused_count = int(runtime.status()["run_count"])
        assert runtime.status()["state"] == "paused"
        assert runtime.status()["active"] is False
        time.sleep(0.15)
        assert int(runtime.status()["run_count"]) == paused_count

        runtime.start(resume=True)
        wait_for(lambda: runtime.status()["state"] in {"running", "degraded", "backoff"})
        assert runtime.status()["active"] is True
    finally:
        runtime.stop()
    assert runtime.status()["state"] == "stopped"
    assert runtime.status()["resume_eligible"] is False
    assert store.get_scheduler_state("monitoring_runtime")["resume_eligible"] is False


def test_startup_false_and_resume_authorization_are_separate(tmp_path: Path) -> None:
    store, runtime = make_runtime(tmp_path / "resume.sqlite3")
    assert runtime.status()["run_count"] == 0
    time.sleep(0.1)
    assert runtime.status()["run_count"] == 0
    refused = runtime.start(resume=True, user_initiated=False)
    assert refused["state"] == "stopped"
    assert refused["last_reason"] == "resume_not_authorized"

    runtime.start()
    wait_for(lambda: int(runtime.status()["run_count"]) >= 1)
    runtime.pause()
    runtime.stop(clear_resume=False)
    store2 = SQLiteStore(tmp_path / "resume.sqlite3")
    store2.initialize()
    service2 = MonitoringService(store=store2, provider_factory=lambda _instrument: FakeMarketProvider(), llm_provider=FakeSmartProvider(), clock=lambda: POINT)
    restored = MonitoringRuntime(store=store2, service=service2, stream_factory=lambda symbols: FakeStream(symbols), clock=lambda: POINT, poll_interval_seconds=0.05)
    try:
        resumed = restored.start(resume=True, user_initiated=False)
        assert resumed["state"] == "starting"
        wait_for(lambda: bool(restored.status()["active"]))
    finally:
        runtime.stop()
        restored.stop()


def test_runtime_faults_are_degraded_and_bounded(tmp_path: Path) -> None:
    _store, runtime = make_runtime(tmp_path / "fault.sqlite3", FailingMarketProvider())
    try:
        runtime.start()
        wait_for(lambda: int(runtime.status()["consecutive_failures"]) >= 1)
        status = runtime.status()
        assert status["state"] in {"degraded", "backoff"}
        assert status["retry_after_at"] is not None
        assert status["active"] is True
    finally:
        runtime.stop()


def test_runtime_api_is_explicit_and_lifecycle_actions_are_real(tmp_path: Path) -> None:
    store, runtime = make_runtime(tmp_path / "api-runtime.sqlite3")
    app = create_app(store=store, monitoring_service=runtime.service, monitoring_runtime=runtime)
    try:
        with TestClient(app) as client:
            assert client.get("/monitoring/status").json()["state"] == "stopped"
            started = client.post("/monitoring/start")
            assert started.status_code == 200
            wait_for(lambda: bool(runtime.status()["active"]))
            paused = client.post("/monitoring/pause")
            assert paused.status_code == 200
            assert paused.json()["state"] == "paused"
            resumed = client.post("/monitoring/resume")
            assert resumed.status_code == 200
            wait_for(lambda: bool(runtime.status()["active"]))
            stopped = client.post("/monitoring/stop")
            assert stopped.status_code == 200
            assert stopped.json()["state"] == "stopped"
    finally:
        runtime.stop()


def test_fastapi_startup_does_not_scan_without_explicit_resume_permission(tmp_path: Path) -> None:
    store, runtime = make_runtime(tmp_path / "startup.sqlite3")
    app = create_app(store=store, monitoring_service=runtime.service, monitoring_runtime=runtime)
    try:
        with TestClient(app):
            assert runtime.status()["state"] == "stopped"
            assert runtime.status()["run_count"] == 0
    finally:
        runtime.stop()


def test_fastapi_startup_resumes_only_an_authorized_prior_runtime(tmp_path: Path) -> None:
    path = tmp_path / "authorized-resume.sqlite3"
    store, seed = make_runtime(path)
    seed.start()
    wait_for(lambda: int(seed.status()["run_count"]) >= 1)
    seed.stop(clear_resume=False)
    store.upsert_app_setting("monitoring.resume", True)

    store2 = SQLiteStore(path)
    store2.initialize()
    service2 = MonitoringService(
        store=store2,
        provider_factory=lambda _instrument: FakeMarketProvider(),
        llm_provider=FakeSmartProvider(),
        clock=lambda: POINT,
    )
    restored = MonitoringRuntime(
        store=store2,
        service=service2,
        stream_factory=lambda symbols: FakeStream(symbols),
        clock=lambda: POINT,
        poll_interval_seconds=0.05,
    )
    app = create_app(store=store2, monitoring_service=service2, monitoring_runtime=restored)
    try:
        with TestClient(app):
            wait_for(lambda: bool(restored.status()["active"]))
            assert restored.status()["last_reason"] in {"resume", "worker_active", "cycle_complete"}
    finally:
        seed.stop()
        restored.stop()
