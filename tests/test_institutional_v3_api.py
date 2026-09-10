from datetime import datetime, timedelta, timezone
from decimal import Decimal
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v3 import router_for
from core.providers import Bar
from core.storage import SQLiteStore
from core.trading.ledger import AccountLedger


def _client(store: SQLiteStore) -> TestClient:
    app = FastAPI()
    app.include_router(router_for(lambda: store))
    return TestClient(app)


def _store() -> tuple[SQLiteStore, tempfile.TemporaryDirectory]:
    temp = tempfile.TemporaryDirectory()
    store = SQLiteStore(Path(temp.name) / "institutional.sqlite3")
    store.initialize()
    AccountLedger(store).create_account("research-paper", mode="PAPER", initial_deposit=Decimal("10000"), config={"venue": "simulated"})
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    store.upsert_market_bars(
        "BTCUSDT",
        "15m",
        [Bar(now - timedelta(minutes=30), 100, 101, 99, 100.5, 10), Bar(now - timedelta(minutes=15), 100.5, 102, 100, 101, 12)],
        provider="institutional_fixture",
        data_as_of=now - timedelta(seconds=1),
        now=now,
        venue="simulated",
        market_type="perpetual",
        native_symbol="BTCUSDT",
        settle_currency="USDT",
        price_type="last",
        instrument_key="simulated:perpetual:BTCUSDT:USDT:last",
        volume_unit="contracts",
    )
    return store, temp


def test_v3_read_endpoints_are_pure_and_preserve_identity() -> None:
    store, temp = _store()
    try:
        client = _client(store)
        with store._connect() as db:
            task_count_before = int(db.execute("SELECT COUNT(*) FROM institutional_tasks").fetchone()[0])
        latest = client.get("/v3/data/latest?symbol=BTCUSDT&timeframe=15m&limit=1")
        assert latest.status_code == 200, latest.text
        assert len(latest.json()["bars"]) == 1
        assert latest.json()["bars"][0]["instrument_key"] == "simulated:perpetual:BTCUSDT:USDT:last"
        quality = client.get("/v3/data/quality")
        assert quality.status_code == 200
        assert quality.json()["read_only"] is True
        catalog = client.get("/v3/data/catalog")
        assert catalog.status_code == 200
        dataset = client.get("/v3/datasets/bars:simulated:perpetual:BTCUSDT:USDT:last:15m")
        assert dataset.status_code == 200
        assert dataset.json()["dataset"]["quality_status"] == "OBSERVED"
        with store._connect() as db:
            task_count_after = int(db.execute("SELECT COUNT(*) FROM institutional_tasks").fetchone()[0])
        assert task_count_after == task_count_before
    finally:
        temp.cleanup()


def test_v3_backfill_is_idempotent_and_external_dependency_is_honest() -> None:
    store, temp = _store()
    try:
        client = _client(store)
        payload = {
            "dataset_id": "public-btc-fixture",
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "source": "public-provider-not-configured",
            "idempotency_key": "backfill-once",
        }
        first = client.post("/v3/data/backfills", json=payload)
        second = client.post("/v3/data/backfills", json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json()["task"]["task_id"] == second.json()["task"]["task_id"]
        assert first.json()["task"]["status"] == "NOT_CONFIGURED"
        assert first.json()["task"]["result"]["fixture_is_not_production_evidence"] is True
        conflict = client.post("/v3/data/backfills", json={**payload, "source": "another-source"})
        assert conflict.status_code == 409
    finally:
        temp.cleanup()


def test_v3_research_without_authoritative_fills_does_not_read_old_predictions() -> None:
    store, temp = _store()
    try:
        client = _client(store)
        body = {
            "account_id": "research-paper",
            "strategy_id": "ema_trend",
            "symbol": "BTCUSDT",
            "mode": "PAPER",
            "venue": "simulated",
            "timeframe": "15m",
            "min_samples": 1,
            "idempotency_key": "research-once",
        }
        first = client.post("/v3/research/runs", json=body)
        assert first.status_code == 200, first.text
        run = first.json()["run"]
        assert run["status"] == "NOT_RUN_NO_AUTHORITATIVE_FILLS"
        assert run["result"]["input_type"] == "AUTHORITATIVE_EXECUTION_LEDGER_ONLY"
        assert run["result"]["authoritative_fill_count"] == 0
        assert run["result"]["qualification_status"] == "NOT_QUALIFIED_NO_AUTHORITATIVE_FILLS"
        with store._connect() as db:
            assert int(db.execute("SELECT COUNT(*) FROM experiment_trials WHERE run_id=?", (run["run_id"],)).fetchone()[0]) == 1
        second = client.post("/v3/research/runs", json=body)
        assert second.status_code == 200
        assert second.json()["created"] is False
        assert second.json()["run"]["run_id"] == run["run_id"]
        qualifications = client.get("/v3/research/qualifications?account_id=research-paper")
        assert qualifications.status_code == 200
        assert qualifications.json()["qualifications"][0]["status"] == "NOT_QUALIFIED_NO_AUTHORITATIVE_FILLS"
        strategy_qualifications = client.get("/v3/strategies/ema_trend/qualification?account_id=research-paper")
        assert strategy_qualifications.status_code == 200
        assert strategy_qualifications.json()["qualifications"][0]["status"] == "NOT_QUALIFIED_NO_AUTHORITATIVE_FILLS"
        timeline = client.get("/v3/executions/timeline?account_id=research-paper")
        assert timeline.status_code == 200
        assert timeline.json()["authoritative_source"] == "trade_fills"
    finally:
        temp.cleanup()


def test_v3_rejects_spoofed_origin() -> None:
    store, temp = _store()
    try:
        response = _client(store).get("/v3/data/quality", headers={"Origin": "http://localhost.attacker.example"})
        assert response.status_code == 403
    finally:
        temp.cleanup()
