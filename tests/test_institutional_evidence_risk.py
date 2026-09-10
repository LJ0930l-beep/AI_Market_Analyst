from datetime import datetime, timedelta, timezone
from decimal import Decimal
import tempfile
from pathlib import Path

from core.evidence import EvidenceBundle, model_weight_digest, persist_evidence_bundle
from core.storage import SQLiteStore
from core.trading.ledger import AccountLedger
from core.trading.institutional_risk import build_exposure_snapshots, cluster_pressure, compute_tca, estimate_paper_capacity


def test_evidence_bundle_is_frozen_idempotent_and_digest_does_not_hash_model_name() -> None:
    temp = tempfile.TemporaryDirectory()
    try:
        store = SQLiteStore(Path(temp.name) / "evidence.sqlite3")
        store.initialize()
        now = datetime.now(timezone.utc)
        bundle = EvidenceBundle.freeze(
            dataset_id="fixture-dataset",
            as_of=now,
            expires_at=now + timedelta(hours=1),
            payload={"close": "100.00"},
            bundle_id="bundle-fixed",
            references=["bar:1"],
        )
        first = persist_evidence_bundle(store, bundle)
        second = persist_evidence_bundle(store, bundle)
        assert first["persisted"] is True
        assert second["persisted"] is False
        assert bundle.validate_references(["bar:1", "news:missing"]) == ("news:missing",)
        assert model_weight_digest(type("Provider", (), {"model_id": "qwen3.5:9b"})()) == (None, "UNKNOWN_NOT_PROVIDED")
    finally:
        temp.cleanup()


def test_model_digest_is_selected_for_requested_smart_model() -> None:
    class DualModelProvider:
        model_name = "qwen3.5:4b"
        weight_digest = "a" * 64

        def health(self, *, model_name=None):
            target = model_name or self.model_name
            return {
                "model_id": target,
                "weight_digest": "b" * 64 if target == "qwen3.5:9b" else "a" * 64,
            }

    digest, status = model_weight_digest(
        DualModelProvider(),
        health_result={"model_id": "qwen3.5:9b", "weight_digest": "b" * 64},
        model_name="qwen3.5:9b",
    )
    assert (digest, status) == ("b" * 64, "OBSERVED_PROVIDER_DIGEST")


def test_institutional_risk_uses_stop_risk_and_marks_unknown_capacity() -> None:
    exposures = build_exposure_snapshots([
        {
            "instrument_key": "simulated:perpetual:BTCUSDT:USDT:last",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "quantity": "2",
            "mark_price": "100",
            "contract_size": "1",
            "stop_loss": "95",
        }
    ])
    pressure = cluster_pressure(exposures, equity="1000", max_fraction="0.02")
    assert pressure["basis"] == "STOP_RISK_AMOUNT"
    assert list(pressure["clusters"].values())[0]["risk_amount"] == "10"
    assert estimate_paper_capacity(None, price=100)["status"] == "NOT_CONFIGURED"
    tca = compute_tca(
        arrival_price="100",
        side="BUY",
        requested_quantity="3",
        fills=[{"quantity": "1", "price": "101", "fee": "0.1", "slippage_cost": "1"}],
    )
    assert tca.status == "PARTIAL_OR_COST_UNKNOWN"
    assert tca.filled_quantity == Decimal("1")


def test_ledger_events_and_fills_publish_one_transactional_outbox_event_each() -> None:
    temp = tempfile.TemporaryDirectory()
    try:
        store = SQLiteStore(Path(temp.name) / "outbox.sqlite3")
        store.initialize()
        ledger = AccountLedger(store)
        ledger.create_account("paper-outbox", mode="PAPER", initial_deposit=Decimal("10000"), config={"venue": "simulated"})
        first = ledger.record_trade_fill(
            "paper-outbox",
            "BTCUSDT",
            "LONG",
            Decimal("2"),
            Decimal("100"),
            Decimal("0.10"),
            mode="PAPER",
            venue="simulated",
            order_id="order-outbox",
            trade_id="trade-outbox",
            protection_status="ACTIVE",
            slippage_cost=Decimal("0.02"),
        )
        with store._connect() as db:
            before = int(db.execute("SELECT COUNT(*) FROM institutional_outbox").fetchone()[0])
        replay = ledger.record_trade_fill(
            "paper-outbox",
            "BTCUSDT",
            "LONG",
            Decimal("2"),
            Decimal("100"),
            Decimal("0.10"),
            mode="PAPER",
            venue="simulated",
            order_id="order-outbox",
            trade_id="trade-outbox",
            protection_status="ACTIVE",
            slippage_cost=Decimal("0.02"),
        )
        with store._connect() as db:
            after = int(db.execute("SELECT COUNT(*) FROM institutional_outbox").fetchone()[0])
            pending = int(db.execute("SELECT COUNT(*) FROM institutional_outbox WHERE status='PENDING'").fetchone()[0])
        assert first["status"] == replay["status"] == "RECORDED"
        assert before == after == pending
        assert before >= 3  # initial deposit, entry, and fee events
    finally:
        temp.cleanup()
