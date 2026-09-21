"""Run safe, deterministic institutional-repair checks against a disposable SQLite database.

This script is deliberately offline.  It never reads the configured application
database, private credentials, or user accounts, and it never invokes an order
adapter.  The output is acceptance evidence for local contracts only; it is not
evidence of a live provider, Qwen weights, public HTTP availability, or trading
performance.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.evidence import EvidenceBundle, persist_evidence_bundle, validate_weight_digest
from core.instruments import AssetType, Instrument, TradingHours
from core.providers import Bar
from core.storage import SQLiteStore
from core.trading.execution_gateway import CapabilityService, CapabilityStatus
from core.trading.institutional_risk import compute_tca


def _check(check_id: str, condition: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {"id": check_id, "status": "PASS" if condition else "FAIL", "details": details}


def run_acceptance() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="aima-institutional-acceptance-") as temp_dir:
        database_path = Path(temp_dir) / "isolated.sqlite3"
        store = SQLiteStore(database_path)
        store.initialize()

        instrument = Instrument(
            symbol="BTCUSDT",
            asset_type=AssetType.CRYPTO,
            exchange="binance",
            currency="USDT",
            timezone="UTC",
            trading_hours=TradingHours.AROUND_THE_CLOCK,
            contract_type="spot",
            tick_size=0.01,
        )
        start = datetime(2030, 1, 2, 0, 0, tzinfo=timezone.utc)
        inserted = store.upsert_market_bars(
            instrument.symbol,
            "5m",
            [
                Bar(start, 100.01, 100.05, 100.00, 100.03, 12.0, is_closed=True),
                Bar(start + timedelta(minutes=5), 100.03, 100.10, 100.02, 100.08, 10.0, is_closed=True),
            ],
            provider="isolated_fixture",
            data_as_of=start + timedelta(minutes=10),
            instrument=instrument,
        )
        latest = store.latest_bars(instrument.symbol, "5m", limit=1, instrument_key=instrument.instrument_key)
        checks.append(_check(
            "AT-DATA-IDENTITY-LATEST",
            inserted == 2 and len(latest) == 1 and latest[0]["bar_start"].startswith("2030-01-02T00:05:00"),
            {"inserted": inserted, "latest_bar_start": latest[0].get("bar_start") if latest else None, "instrument_key": instrument.instrument_key},
        ))

        bundle = EvidenceBundle.freeze(
            dataset_id="isolated:bars:binance:BTCUSDT:5m",
            as_of=start,
            expires_at=start + timedelta(days=1),
            payload={"fixture": True, "scope": "isolated"},
            bundle_id="bundle_institutional_acceptance_fixture",
            missing=["model_weight_digest"],
            references=["fixture:bars", "fixture:risk"],
            frozen_at=start + timedelta(minutes=1),
        )
        first = persist_evidence_bundle(store, bundle)
        second = persist_evidence_bundle(store, bundle)
        checks.append(_check(
            "AT-EVIDENCE-IDEMPOTENCY-DIGEST",
            first["persisted"] is True and second["persisted"] is False and validate_weight_digest("Bonsai-2-27B-PTQ1_0") is None,
            {"first_persisted": first["persisted"], "second_persisted": second["persisted"], "model_name_is_not_digest": True},
        ))

        tca = compute_tca(
            arrival_price="100",
            side="BUY",
            requested_quantity="3",
            fills=[
                {"quantity": "1", "price": "100.10", "fee": "0.10", "slippage_cost": "0.02"},
                {"quantity": "2", "price": "100.20", "fee": "0.20", "slippage_cost": "0.04"},
            ],
        )
        checks.append(_check(
            "AT-COST-TCA-DECIMAL",
            tca.status == "CALCULATED" and tca.filled_quantity == Decimal("3") and tca.fees == Decimal("0.30") and tca.slippage_cost == Decimal("0.06"),
            {"status": tca.status, "filled_quantity": str(tca.filled_quantity), "fees": str(tca.fees), "slippage_cost": str(tca.slippage_cost)},
        ))

        capabilities = CapabilityService.get_capabilities(store)
        live = capabilities.get("modes", {}).get("LIVE", {})
        checks.append(_check(
            "AT-LIVE-DEFAULT-LOCK",
            live.get("status") == CapabilityStatus.LOCKED.value,
            {"live_status": live.get("status"), "reason_code": live.get("reason_code")},
        ))

    passed = sum(item["status"] == "PASS" for item in checks)
    return {
        "schema_version": "institutional-acceptance-v1",
        "mode": "ISOLATED_DETERMINISTIC_FIXTURE",
        "status": "PASS" if passed == len(checks) else "FAIL",
        "checks": checks,
        "summary": {"passed": passed, "total": len(checks)},
        "external_dependencies": {
            "public_http": "NOT_CONFIGURED",
            "ollama_qwen_weights": "NOT_CONFIGURED",
            "private_trading_account": "NOT_ACCESSED",
            "real_orders": "NOT_ATTEMPTED",
        },
        "fixture_boundary": "Deterministic fixture success is not proof of public HTTP, real Qwen behavior, exchange connectivity, or live performance.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional explicit JSON output path")
    args = parser.parse_args()
    result = run_acceptance()
    serialized = json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
