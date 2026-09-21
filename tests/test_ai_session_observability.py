from threading import RLock
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.storage import SQLiteStore
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.ai_strategy_book import TEMPLATES, AIStrategyBook
from core.trading.execution_gateway import TradingMode
from core.trading.institutional_schema import ensure_institutional_trader_schema
from core.trading.ledger import AccountLedger


def _client(tmp_path):
    store = SQLiteStore(tmp_path / "ai-session-observability.sqlite3")
    store.initialize()
    AccountLedger(store).create_account(
        "test-account",
        mode="TESTNET",
        config={"provider": "gate", "environment": "testnet"},
    )
    app = FastAPI()
    app.include_router(router_for(lambda: store, lambda: None, lambda: None))
    client = TestClient(
        app,
        headers={"Host": "localhost:8000", "Origin": "http://localhost:5173"},
    )
    return store, client


def test_candidate_list_orders_by_real_update_time_and_places_unknown_last(tmp_path):
    store, client = _client(tmp_path)
    with store._connect() as db:
        ensure_institutional_trader_schema(db)
        rows = [
            (
                "recent-with-unknown-bar",
                "5m",
                "UNKNOWN",
                "2026-09-21T11:55:00+00:00",
                "2026-09-21T11:55:00+00:00",
            ),
            (
                "older-real-bar",
                "5m",
                "2026-09-21T11:30:00+00:00",
                "2026-09-21T11:30:00+00:00",
                "2026-09-21T11:30:00+00:00",
            ),
            ("unknown-time", "15m", "UNKNOWN", "UNKNOWN", "UNKNOWN"),
        ]
        for candidate_id, timeframe, closed_bar, created_at, updated_at in rows:
            db.execute(
                """INSERT INTO ai_strategy_candidates(
                       candidate_id, account_id, provider, environment, symbol,
                       strategy_id, strategy_version, signal_timeframe,
                       closed_15m_bar, status, source_hash, created_at, updated_at
                   ) VALUES (?, 'test-account', 'gate', 'testnet', 'BTCUSDT',
                             'ema_trend', 'v1', ?, ?, 'NO_TRIGGER', ?, ?, ?)""",
                (candidate_id, timeframe, closed_bar, f"hash-{candidate_id}", created_at, updated_at),
            )

    response = client.get("/v2/ai-session/candidates?account_id=test-account&limit=3")

    assert response.status_code == 200, response.text
    candidates = response.json()["candidates"]
    assert [item["candidate_id"] for item in candidates] == [
        "recent-with-unknown-bar",
        "older-real-bar",
        "unknown-time",
    ]


def test_status_reports_active_strategy_cadence_and_labels_constructor_interval_legacy(tmp_path):
    store = SQLiteStore(tmp_path / "ai-session-status.sqlite3")
    store.initialize()
    book = AIStrategyBook(store)
    active = book.active("test-account")
    strategy = next(item for item in TEMPLATES if item["id"] == "aggressive_impulse")
    book.save(
        "test-account",
        name=strategy["name"],
        sections=strategy["sections"],
        expected_revision=active["revision"],
        template_id=strategy["id"],
        execution=strategy["execution_defaults"],
    )

    coordinator = object.__new__(AISessionCoordinator)
    coordinator.store = store
    coordinator._lock = RLock()
    coordinator._thread = None
    coordinator._enabled = False
    coordinator._account_id = "test-account"
    coordinator._mode = TradingMode.TESTNET
    coordinator._venue = "gate"
    coordinator._last_reason = "not_started"
    coordinator._last_error = None
    coordinator._last_cycle_id = None
    coordinator._last_cycle_status = None
    coordinator._last_cycle_at = None
    coordinator._last_operational_state = None
    coordinator._calibration_state = "NOT_STARTED"
    coordinator._calibration_run = None
    coordinator._last_scheduled_at = None
    coordinator._last_started_at = None
    coordinator._last_completed_at = None
    coordinator._last_lag_ms = None
    coordinator._last_duration_ms = None
    coordinator._next_scan_at = None
    coordinator._candidate_count = 0
    coordinator.cycle_interval_seconds = 900  # legacy constructor value
    coordinator.model_budget_seconds = 30
    coordinator.session_manager = SimpleNamespace(status=dict)
    coordinator._health = lambda: {"status": "UNAVAILABLE"}

    status = coordinator.status()

    assert status["schedule"]["interval_minutes"] == 5
    assert status["cycle_interval_seconds"] == 300
    assert status["scan_interval_seconds"] == 300
    assert status["legacy_cycle_interval_seconds"] == 900
    assert status["cycle_interval_source"] == "active_strategy_schedule"
    assert status["schedule"]["alignment"] == "minute % 5 == 0"
