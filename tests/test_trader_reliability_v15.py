"""v1.5 regression tests written before the implementation repair.

The first two cases preserve the two independently reproduced Guardian
failures.  They intentionally enter through the durable ledger and the real
PositionGuardian boundary; no exchange adapter or real account is involved.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from apps.api.v2 import router_for
from core.analysis.ai_trade_analytics import analyze_ai_trading_ledger
from core.news_revision import NewsRevisionRegistry
from core.monitoring_runtime import MonitoringRuntime
from core.providers.base import Bar
from core.storage import SQLiteStore
from core.trading.execution_gateway import ExecutionGateway, OrderIntent, ProtectionPlan, TradingMode
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian


def _paper_position(store: SQLiteStore, *, protection_contract: dict | None = None):
    ledger = AccountLedger(store)
    ledger.create_account(
        "v15-a",
        mode="PAPER",
        initial_deposit=Decimal("1000"),
        config={"venue": "simulated"},
    )
    ledger.record_trade_fill(
        account_id="v15-a",
        instrument_id="BTCUSDT",
        side="BUY",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        mode="PAPER",
        venue="simulated",
        order_id="v15-entry",
        trade_id="v15-entry-fill",
        position_id="v15-position",
        stop_price=90,
        protection_status="ACTIVE",
        protection_contract=protection_contract,
    )
    guardian = PositionGuardian(store, ledger)
    guardian.set_account_scope("v15-a")
    return ledger, guardian


def test_v15_old_cumulative_bar_cannot_trigger_before_position_activation(tmp_path):
    store = SQLiteStore(tmp_path / "v15-old-bar.db")
    store.initialize()
    ledger, guardian = _paper_position(store)
    try:
        activation = datetime.now(timezone.utc)
        old_cumulative_bar = Bar(
            activation - timedelta(minutes=10),
            100,
            105,
            80,
            101,
            10,
        )

        result = guardian.process_bar("BTCUSDT", old_cumulative_bar)

        assert result == []
        positions = ledger.get_open_positions("v15-a", venue="simulated", mode="PAPER")
        assert len(positions) == 1
        assert positions[0]["remaining_contracts"] == 1
    finally:
        ledger.close()


def test_v15_current_bar_crossing_activation_uses_timestamped_point_not_old_open(tmp_path):
    store = SQLiteStore(tmp_path / "v15-current-bar-entry.db")
    store.initialize()
    ledger, guardian = _paper_position(store)
    try:
        with store._connect() as db:
            payload = json.loads(
                db.execute(
                    "SELECT payload_json FROM simulated_positions WHERE position_id=?",
                    ("v15-position",),
                ).fetchone()[0]
            )
        activation = max(
            datetime.fromisoformat(payload["entry_filled_at"]),
            datetime.fromisoformat(payload["protection_effective_at"]),
        )
        event_at = activation + timedelta(seconds=1)
        crossing_bar = Bar(
            activation - timedelta(minutes=10),
            100,
            105,
            80,
            85,
            10,
            event_at=event_at,
            sequence=2,
        )

        result = guardian.process_bar("BTCUSDT", crossing_bar)

        assert len(result) == 1
        assert result[0]["reason"] == "STOP"
        # The old bar open/low are not executable evidence.  The close point
        # is timestamped after protection activation and is therefore the
        # only price used for this crossing.
        assert result[0]["price"] == pytest.approx(85 * 0.999)
        with store._connect() as db:
            fill = db.execute(
                "SELECT event_at FROM trade_fills WHERE position_id=? AND side='SELL'",
                ("v15-position",),
            ).fetchone()
        assert fill[0] == event_at.isoformat()
    finally:
        ledger.close()


def test_v15_runtime_old_cumulative_bar_does_not_turn_into_a_stop_fill(tmp_path):
    store = SQLiteStore(tmp_path / "v15-runtime-old-bar.db")
    store.initialize()
    ledger, guardian = _paper_position(store)
    runtime = MonitoringRuntime(
        store=store,
        ledger=ledger,
        guardian=guardian,
        service=SimpleNamespace(strategy_mode=False, max_symbols=5),
    )
    try:
        old_bar = Bar(
            datetime.now(timezone.utc) - timedelta(minutes=10),
            100,
            105,
            80,
            101,
            10,
        )
        runtime._on_stream_bar("BTCUSDT", old_bar, False)
        assert ledger.get_open_positions("v15-a", venue="simulated", mode="PAPER")
        with store._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND side='SELL'",
                ("v15-a",),
            ).fetchone()[0] == 0
    finally:
        runtime.ai_coordinator.close()
        ledger.close()


def test_v15_news_trigger_without_fresh_quote_is_pending_and_degraded(tmp_path):
    store = SQLiteStore(tmp_path / "v15-news-stale-quote.db")
    store.initialize()
    registry = NewsRevisionRegistry(store)
    revision = registry.record_news(
        "news-stale",
        "headline",
        "body",
        claim_status="PRIMARY_SOURCE_VERIFIED",
        source_url="https://local.invalid/news-stale",
        publisher="fixture",
    )
    ledger, guardian = _paper_position(
        store,
        protection_contract={
            "event_invalidation": [
                {"event_key": revision.revision_id, "invalid_if": "ACTIVE_OR_CORRECTED"}
            ]
        },
    )
    try:
        stale = Bar(
            datetime.now(timezone.utc) - timedelta(seconds=180),
            100,
            102,
            99,
            101,
            1,
        )
        assert guardian.process_bar("BTCUSDT", stale) == []
        registry.correct_news(
            "news-stale",
            "corrected",
            "retracted",
            correction_reason="fixture",
            claim_status="RETRACTED",
        )

        result = guardian.process_bar("BTCUSDT", stale)

        assert result and result[0]["reason"] == "EVENT_INVALIDATION"
        assert result[0]["status"] == "PENDING"
        assert result[0]["economic_fill_recorded"] is False
        positions = ledger.get_open_positions("v15-a", venue="simulated", mode="PAPER")
        assert len(positions) == 1
        assert positions[0]["protection_status"] == "DEGRADED"
        with store._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND side='SELL'",
                ("v15-a",),
            ).fetchone()[0] == 0
    finally:
        ledger.close()


def test_v15_news_correction_is_independent_from_same_quote_deduplication(tmp_path):
    store = SQLiteStore(tmp_path / "v15-news-revision.db")
    store.initialize()
    registry = NewsRevisionRegistry(store)
    revision = registry.record_news(
        "news",
        "headline",
        "body",
        claim_status="PRIMARY_SOURCE_VERIFIED",
        source_url="https://local.invalid/news",
        publisher="fixture",
    )
    contract = {
        "event_invalidation": [
            {"event_key": revision.revision_id, "invalid_if": "ACTIVE_OR_CORRECTED"}
        ]
    }
    ledger, guardian = _paper_position(store, protection_contract=contract)
    try:
        observation_time = datetime.now(timezone.utc)
        bar = Bar(observation_time, 100, 102, 99, 101, 1)
        assert guardian.process_bar("BTCUSDT", bar) == []

        registry.correct_news(
            "news",
            "corrected",
            "retracted",
            correction_reason="test",
            claim_status="RETRACTED",
        )

        assert guardian._event_invalidation_state(contract) is True
        result = guardian.process_bar("BTCUSDT", bar)

        assert len(result) == 1
        assert result[0]["reason"] == "EVENT_INVALIDATION"
        assert ledger.get_open_positions("v15-a", venue="simulated", mode="PAPER") == []
    finally:
        ledger.close()


class _V15QuietStream:
    def __init__(self, symbols):
        self.symbols = symbols
        self._stop = Event()

    def run_forever(self, *, on_bar, on_state=None):
        del on_bar, on_state
        while not self._stop.wait(0.01):
            pass

    def stop(self):
        self._stop.set()


class _V15QuietService:
    strategy_mode = False
    max_symbols = 5
    account_id = None

    def __init__(self):
        self.cancel_event = None

    def run(self, *, symbols=None, now=None):
        del symbols, now
        from core.monitoring import MonitoringRunResult

        return MonitoringRunResult("DISABLED", datetime.now(timezone.utc), tuple(), tuple(), {})


def test_v15_strategy_runtime_fill_is_visible_once_in_ai_analysis_projection(tmp_path):
    store = SQLiteStore(tmp_path / "v15-ai-analysis-projection.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(
        "v15-analysis-a",
        mode="PAPER",
        initial_deposit=Decimal("10000"),
        config={"venue": "simulated"},
    )
    runtime = MonitoringRuntime(
        store=store,
        service=_V15QuietService(),
        stream_factory=lambda symbols: _V15QuietStream(symbols),
        poll_interval_seconds=0.02,
        stream_join_timeout_seconds=0.1,
    )
    app = FastAPI()
    app.include_router(router_for(lambda: store, lambda: runtime, lambda: None))
    plan_body = {
        "account_id": "v15-analysis-a",
        "mode": "PAPER",
        "venue": "simulated",
        "symbol": "BTCUSDT",
        "action": "OPEN_LONG",
        "strategy_id": "ema_trend",
        "strategy_version": "ema-v15",
        "evidence": [{"type": "local_runtime_fixture", "as_of": datetime.now(timezone.utc).isoformat()}],
        "entry_trigger": "a fresh local quote is available",
        "abandon_chase_condition": "do not chase beyond the declared quote",
        "condition_spec": {
            "version": "trade_conditions_v1",
            "entry": {"type": "FRESH_MARK"},
            "abandon_chase": {"type": "MAX_CHASE_BPS", "value": 10.0},
        },
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "take_profit": 120.0,
        "worst_loss_budget": 20.0,
        "why_not_waiting": "the declared fresh quote condition is satisfied",
    }
    try:
        runtime.start(account_id="v15-analysis-a")
        with TestClient(app) as client:
            created = client.post("/v2/trade-plans", json=plan_body)
            assert created.status_code == 200, created.text
            plan_id = created.json()["plan_id"]

            runtime.process_market_event(
                "BTCUSDT",
                Bar(datetime.now(timezone.utc), 100, 101, 99, 100, 1),
            )

            with store._connect() as db:
                fill_count = db.execute(
                    "SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND mode='PAPER'",
                    ("v15-analysis-a",),
                ).fetchone()[0]
            assert fill_count == 1

            # Re-delivering the same runtime observation must not replay the
            # opening plan or create a second economic fill.
            runtime.process_market_event(
                "BTCUSDT",
                Bar(datetime.now(timezone.utc), 100, 101, 99, 100, 1),
            )
            with store._connect() as db:
                assert db.execute(
                    "SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND mode='PAPER'",
                    ("v15-analysis-a",),
                ).fetchone()[0] == 1

                position_id = db.execute(
                    "SELECT position_id FROM simulated_positions WHERE account_id=? AND symbol=?",
                    ("v15-analysis-a", "BTCUSDT"),
                ).fetchone()[0]
                initial_remaining = json.loads(
                    db.execute(
                        "SELECT payload_json FROM simulated_positions WHERE position_id=?",
                        (position_id,),
                    ).fetchone()[0]
                )["remaining_contracts"]

            # A partial close goes through the same API -> armed-plan ->
            # runtime -> gateway -> ledger path, so the projection can prove
            # entry/exit lifecycle without a direct UI-only record.
            reduce_body = {
                **plan_body,
                "action": "REDUCE_POSITION",
                "position_id": position_id,
                "reduce_fraction": 0.25,
                "strategy_version": "ema-v15-reduce",
            }
            reduce_created = client.post("/v2/trade-plans", json=reduce_body)
            assert reduce_created.status_code == 200, reduce_created.text
            runtime.process_market_event(
                "BTCUSDT",
                Bar(datetime.now(timezone.utc), 100, 100.5, 99.5, 100, 1),
            )
            with store._connect() as db:
                remaining = db.execute(
                    "SELECT payload_json FROM simulated_positions WHERE position_id=?",
                    (position_id,),
                ).fetchone()[0]

                assert json.loads(remaining)["remaining_contracts"] == initial_remaining * 0.75

            response = client.get("/v2/ai-analysis", params={"account_id": "v15-analysis-a"})
            assert response.status_code == 200, response.text
            payload = response.json()
            entries = [
                item for item in payload["execution_records"] if item["economic_role"] == "ENTRY"
            ]
            assert len(entries) == 1
            assert entries[0]["account_id"] == "v15-analysis-a"
            assert entries[0]["decision_path"] == "STRATEGY_DRIVEN"
            assert entries[0]["strategy_id"] == "ema_trend"
            assert entries[0]["status"] == "FILLED"
            assert entries[0]["position_id"]
            assert entries[0]["fill_id"]
            assert entries[0]["intent_id"]
            assert entries[0]["mode"] == "PAPER"
            assert entries[0]["venue"] == "simulated"
            assert entries[0]["trade_plan_id"] == plan_id
            exits = [
                item for item in payload["execution_records"] if item["economic_role"] == "EXIT"
            ]
            assert len(exits) == 1
            assert exits[0]["status"] == "FILLED"
            assert exits[0]["position_id"] == position_id
            assert payload["execution_summary"]["fill_count"] == 2
            assert payload["execution_summary"]["entry_fill_count"] == 1
            assert payload["execution_summary"]["exit_fill_count"] == 1
            assert payload["ai_led_performance"]["position_count"] == 0

            # A second account can hold the same symbol without leaking into
            # the first account's analysis projection.
            ledger.create_account(
                "v15-analysis-b",
                mode="PAPER",
                initial_deposit=Decimal("10000"),
                config={"venue": "simulated"},
            )
            ledger.record_trade_fill(
                account_id="v15-analysis-b",
                instrument_id="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                price=Decimal("101"),
                fee=Decimal("0"),
                mode="PAPER",
                venue="simulated",
                order_id="v15-b-entry",
                trade_id="v15-b-fill",
                position_id="v15-b-position",
                stop_price=90,
                protection_status="ACTIVE",
            )
            account_b = client.get("/v2/ai-analysis", params={"account_id": "v15-analysis-b"})
            assert account_b.status_code == 200, account_b.text
            assert {item["account_id"] for item in account_b.json()["execution_records"]} == {"v15-analysis-b"}
            assert {item["symbol"] for item in account_b.json()["execution_records"]} == {"BTCUSDT"}
            assert all(item["account_id"] == "v15-analysis-a" for item in payload["execution_records"])
    finally:
        runtime.stop()
        ledger.close()


def test_v15_ai_analysis_keeps_resting_ack_out_of_economic_fills(tmp_path):
    store = SQLiteStore(tmp_path / "v15-ai-analysis-pending.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account(
        "v15-pending",
        mode="PAPER",
        initial_deposit=Decimal("10000"),
        config={"venue": "simulated"},
    )
    gateway = ExecutionGateway(store, ledger=ledger)
    now = datetime.now(timezone.utc)
    intent = OrderIntent(
        intent_id="v15-pending-intent",
        idempotency_key="v15-pending-idempotency",
        account_id="v15-pending",
        mode=TradingMode.PAPER,
        environment="PAPER",
        venue="simulated",
        instrument_id="BTCUSDT",
        side="BUY",
        order_type="limit",
        quantity=0.1,
        price=99.0,
        protection_plan=ProtectionPlan(stop_price=90.0),
        strategy_id="ema_trend",
        strategy_version="ema-v15",
        decision_path="STRATEGY_DRIVEN",
    )
    market = {
        "symbol": "BTCUSDT",
        "price": 100.0,
        "bid": 100.0,
        "ask": 100.0,
        "fresh": True,
        "data_as_of": now.isoformat(),
        "received_at": now.isoformat(),
    }
    app = FastAPI()
    app.include_router(router_for(lambda: store, lambda: None, lambda: None))
    try:
        receipt = gateway.submit_intent(intent, market_snapshot=market)
        assert receipt["status"] == "ACKNOWLEDGED"
        with TestClient(app) as client:
            response = client.get("/v2/ai-analysis", params={"account_id": "v15-pending"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["execution_records"] == []
        pending = next(
            item for item in payload["execution_orders"]
            if item["intent_id"] == "v15-pending-intent"
        )
        assert pending["status"] == "ACKNOWLEDGED"
        assert pending["economic_status"] == "NOT_FILLED_PENDING"
        assert pending["fill_count"] == 0
        assert payload["execution_summary"]["fill_count"] == 0
    finally:
        ledger.close()


def test_v15_guardian_exit_is_projected_as_exit_not_a_second_entry(tmp_path):
    store = SQLiteStore(tmp_path / "v15-guardian-analysis.db")
    store.initialize()
    ledger, guardian = _paper_position(store)
    try:
        result = guardian.process_bar(
            "BTCUSDT",
            Bar(datetime.now(timezone.utc) + timedelta(seconds=1), 100, 101, 80, 85, 1),
        )
        assert result and result[0]["reason"] == "STOP"

        analysis = analyze_ai_trading_ledger(store, account_id="v15-a")

        assert [item["economic_role"] for item in analysis["execution_records"]].count("ENTRY") == 1
        assert [item["economic_role"] for item in analysis["execution_records"]].count("EXIT") == 1
        exit_record = next(item for item in analysis["execution_records"] if item["economic_role"] == "EXIT")
        assert exit_record["position_id"] == "v15-position"
        assert exit_record["concrete_economic_fill"] is True
    finally:
        ledger.close()
