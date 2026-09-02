from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.main import create_app
from core.instruments import instrument_for
from core.market_hydration import HYDRATION_CONTRACT_VERSION, MarketHydrationRuntime
from core.market_intelligence import build_market_intelligence
from core.monitoring import MonitoringPolicy
from core.monitoring_runtime import MonitoringRuntime
from core.storage import SQLiteStore


POINT = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def _wait_until(predicate, *, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "bounded worker did not reach the expected state"


class _RuntimeResult:
    status = "COMPLETED"
    as_of = POINT
    items = ()


class _RuntimeService:
    max_symbols = 50

    def run(self, *, symbols, now):
        assert symbols
        return _RuntimeResult()


class _IdlePublicStream:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols
        self._stop = False

    def run_forever(self, *, on_bar, on_state=None) -> None:
        while not self._stop:
            time.sleep(0.01)

    def stop(self) -> None:
        self._stop = True


def _seed_enabled_policy(store: SQLiteStore) -> None:
    store.save_instrument(instrument_for("BTCUSDT"))
    store.upsert_monitoring_policy({**MonitoringPolicy.defaults("BTCUSDT").to_dict(), "enabled": True})


def test_public_hydration_worker_is_sidecar_owned_and_never_runs_analysis(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "hydration.sqlite3")
    store.initialize()
    runtime = MarketHydrationRuntime(
        store=store,
        enabled=True,
        interval_seconds=30,
        initial_delay_seconds=0,
        market_symbols=("BTCUSDT",),
        news_symbols=(),
    )

    def fake_save_market(symbol, instrument, *, now):
        store.save_realtime_state(
            {
                "symbol": symbol,
                "provider": "public_test_provider",
                "price": 42_000.0,
                "change_pct": 1.2,
                "freshness_status": "fresh",
                "last_trade_at": now.isoformat(),
                "data_as_of": now.isoformat(),
                "stale_after_seconds": 3600,
                "reconnect_count": 0,
            },
            now=now,
        )
        return True, "public_test_provider"

    runtime._save_market = fake_save_market  # type: ignore[method-assign]
    try:
        runtime.start()
        _wait_until(lambda: runtime.status()["run_count"] == 1)
        status = runtime.status()
    finally:
        runtime.stop()

    assert status["contract_version"] == HYDRATION_CONTRACT_VERSION
    assert status["state"] == "ready"
    assert status["worker_alive"] is True
    assert status["domain_writes"] == 0
    assert status["provider_calls"] == 1
    assert store.list_monitoring_policies() == []
    assert store.latest_public_hydration_run()["status"] == "ready"
    assert store.counts()["predictions"] == 0
    for entry in store.list_watchlist_entries():
        store.delete_watchlist_entry(entry["symbol"])
    restarted_runtime = MarketHydrationRuntime(store=store, enabled=True, market_symbols=(), news_symbols=())
    restarted_runtime._seed_default_watchlist()
    assert store.list_watchlist_entries() == []


def test_market_intelligence_reads_hydrated_cache_without_fetch_or_domain_write(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "cache-only.sqlite3")
    store.initialize()
    store.save_instrument(instrument_for("BTCUSDT"))
    store.upsert_watchlist_entry("BTCUSDT", now=POINT)
    store.save_realtime_state(
        {
            "symbol": "BTCUSDT",
            "provider": "public_test_provider",
            "price": 42_000.0,
            "change_pct": 2.0,
            "freshness_status": "fresh",
            "last_trade_at": POINT.isoformat(),
            "data_as_of": POINT.isoformat(),
            "stale_after_seconds": 3600,
            "reconnect_count": 0,
        },
        now=POINT,
    )
    before = store.counts()

    view = build_market_intelligence(store, as_of=POINT)
    pulse = {item["symbol"]: item for item in view["pulse"]}["BTCUSDT"]

    assert view["read_only"] is True
    assert view["provider_calls"] is False
    assert view["domain_writes"] is False
    assert pulse["price"] == 42_000.0
    assert pulse["provenance"] == ["sidecar_public_hydration", "durable_realtime_cache"]
    assert store.counts() == before


def test_monitoring_startup_is_off_without_resume_authorization_and_resumes_only_when_explicitly_allowed(tmp_path: Path) -> None:
    def make_app(path: Path, *, resume: bool, eligible: bool):
        store = SQLiteStore(path)
        store.initialize()
        _seed_enabled_policy(store)
        store.upsert_app_setting("monitoring.resume", resume)
        store.set_scheduler_state(
            "monitoring_runtime",
            {"resume_eligible": eligible, "last_state": "paused"},
            updated_at=POINT.isoformat(),
        )
        runtime = MonitoringRuntime(
            store=store,
            service=_RuntimeService(),
            stream_factory=_IdlePublicStream,
            poll_interval_seconds=30,
        )
        app = create_app(
            store=store,
            llm_provider=None,
            monitoring_service=_RuntimeService(),
            monitoring_runtime=runtime,
            market_hydration_enabled=False,
        )
        return store, app

    off_store, off_app = make_app(tmp_path / "resume-off.sqlite3", resume=False, eligible=True)
    with TestClient(off_app) as client:
        off_status = client.get("/monitoring/status")
        assert off_status.status_code == 200
        assert off_status.json()["active"] is False
        assert off_status.json()["last_reason"] == "startup_no_scan"

    on_store, on_app = make_app(tmp_path / "resume-on.sqlite3", resume=True, eligible=True)
    try:
        with TestClient(on_app) as client:
            _wait_until(lambda: client.get("/monitoring/status").json()["active"] is True)
            running = client.get("/monitoring/status").json()
            assert running["worker_alive"] is True
            assert running["active_symbols"] == ["BTCUSDT"]
            paused = client.post("/monitoring/pause")
            assert paused.status_code == 200
            assert paused.json()["active"] is False
    finally:
        assert on_store.get_scheduler_state("monitoring_runtime") is not None


def test_health_contract_reports_desktop_identity_only_with_the_ownership_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AIMA_INSTANCE_ID", "test-instance")
    monkeypatch.setenv("AIMA_OWNERSHIP_TOKEN", "test-token")
    monkeypatch.setenv("AIMA_SIDECAR_BOUND_PORT", "19999")
    store = SQLiteStore(tmp_path / "health.sqlite3")
    app = create_app(store=store, llm_provider=None, market_hydration_enabled=False)

    with TestClient(app) as client:
        public = client.get("/health").json()
        owned = client.get("/health", headers={"X-AIMA-Ownership-Token": "test-token"}).json()

    assert public["ready"] is True
    assert public["ownership_verified"] is False
    assert owned["contract_version"] == "desktop_backend_v1"
    assert owned["instance_id"] == "test-instance"
    assert owned["pid"] > 0
    assert owned["port"] == 19999
    assert owned["ownership_verified"] is True


def test_v121_build_and_capability_contract_is_dynamic_and_webview_cannot_spawn(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    capability = json.loads((root / "src-tauri" / "capabilities" / "default.json").read_text(encoding="utf-8"))
    rust = (root / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    permissions = capability["permissions"]

    assert config["version"] == "1.2.1"
    assert "scripts/build-tauri.ps1" in config["build"]["beforeBuildCommand"]
    assert "select_sidecar_port" in rust
    assert "X-AIMA-Ownership-Token" in rust
    assert "launcher_pid" in rust
    assert "backend health contract or ownership identity mismatch" in rust
    assert "foreign listeners were not touched" in rust
    assert '"--host".to_string()' in rust and '"--port".to_string()' in rust
    assert not any(
        permission == "shell:allow-spawn"
        or (isinstance(permission, dict) and permission.get("identifier") == "shell:allow-spawn")
        for permission in permissions
    )
    assert "AIMA_SIDECAR_PORT" not in (root / "scripts" / "build-tauri-frontend.ps1").read_text(encoding="utf-8")
    assert '.app_name("AI Market Analyst")' in rust
