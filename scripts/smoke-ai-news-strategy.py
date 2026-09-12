"""Public Gate + RSS + real local Qwen -> isolated PAPER execution smoke.

Never opens a user's database, reads exchange credentials or starts a session.
This verifies model/decision/execution integration, not profitability or the
desktop scheduler's calibration/startup gates.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.ai.ollama import OllamaProvider
from core.instruments import instrument_for
from core.news_refresh import refresh_public_news
from core.providers.gateio_provider import GatePublicProvider
from core.storage import SQLiteStore
from core.trading.ai_led_engine import AICycleContext, AILedDecisionEngine
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.autonomous_strategy import CONTRACT, technical_context, book_cost_evidence
from core.trading.execution_gateway import ExecutionGateway
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.trading.risk_engine import RiskEngine
from core.trading.session_manager import SessionManager


def main():
    root = Path(__file__).resolve().parents[1]
    report_path = root / "evidence" / "ai_news_strategy_smoke.json"
    report = {"contract": CONTRACT, "model": "qwen3.5:9b", "external_orders": False,
              "scope": "PUBLIC_DATA_REAL_MODEL_ISOLATED_PAPER", "tested_at": datetime.now(timezone.utc).isoformat()}
    with tempfile.TemporaryDirectory(prefix="aima-news-smoke-") as directory:
        store = SQLiteStore(Path(directory) / "paper.db")
        store.initialize()
        ledger = AccountLedger(store)
        ledger.create_account("smoke-paper", mode="PAPER", initial_deposit=Decimal("10000"))
        gateway = ExecutionGateway(store, ledger=ledger)
        guardian = PositionGuardian(store, ledger)
        model = OllamaProvider(model_name="qwen3.5:9b", timeout=55, context_length=16384, max_tokens=1800, retries=0)
        coordinator = AISessionCoordinator(store=store, service=SimpleNamespace(), model_provider=model,
                                           session_manager=SessionManager(store), ledger=ledger, guardian=guardian, execution_gateway=gateway)
        try:
            public = GatePublicProvider()
            instrument = instrument_for("BTCUSDT")
            for tf in ("15m", "1h"):
                observed = public.get_bars(instrument, tf, limit=100)
                now = datetime.now(timezone.utc)
                store.upsert_market_bars("BTCUSDT", tf, observed, provider=public.provider_name, data_as_of=now, now=now,
                                         venue="gate", market_type="perpetual", native_symbol="BTC_USDT", settle_currency="USDT", price_type="last", volume_unit="contracts")
                print(f"Fetched {tf}: {len(observed)} bars", flush=True)
            report["news_refresh"] = refresh_public_news(store, symbols=("BTCUSDT",))
            print("News: " + str(report["news_refresh"]["status"]), flush=True)
            quote = public.get_quote(instrument)
            book = book_cost_evidence(public.order_book("BTCUSDT"), quote.price)
            now = datetime.now(timezone.utc)
            ctx = AICycleContext(cycle_id="public-model-smoke", account_id="smoke-paper", generation=1,
                                 started_at=now.isoformat(), expires_at=(now + timedelta(seconds=240)).isoformat(),
                                 allowed_instruments=("BTCUSDT",), decision_contract=CONTRACT,
                                 market_snapshots={"BTCUSDT": {"price": quote.price, "data_as_of": quote.timestamp.isoformat(), "fresh": True, **book}},
                                 technical_context=technical_context(store, ("BTCUSDT",), now),
                                 news_revisions=coordinator._news_revisions(("BTCUSDT",), now=now))
            report["technical_status"] = ctx.technical_context["BTCUSDT"]["status"]
            report["news_count"] = len(ctx.news_revisions)
            print("Calling local qwen3.5:9b", flush=True)
            health = coordinator._health(force=True)
            if health.get("status") != "READY":
                raise RuntimeError("LOCAL_QWEN_NOT_READY")
            output = coordinator._model_output(ctx)
            engine = AILedDecisionEngine(store, gateway, RiskEngine(ledger), ledger, guardian)
            result = engine.execute_cycle(ctx, now=datetime.now(timezone.utc), model_output=output)
            report.update(status="PASS", model_action=output.action, execution_status=result.status,
                          reason=result.reason, model_latency_ms=ctx.model_latency_ms, model_digest=ctx.model_digest,
                          strategy_plan=output.extra_fields.get("strategy_plan"), analysis=output.extra_fields,
                          input_hash=ctx.input_hash, prompt_version=ctx.prompt_version,
                          evidence_refs=list(output.evidence_refs), paper_order_created=result.order_intent is not None)
            if not output.extra_fields.get("strategy_plan") or report["technical_status"] != "READY" or not report["news_count"]:
                report["status"] = "PARTIAL"
        except Exception as exc:
            report.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
            if 'ctx' in locals():
                report["model_raw_response"] = ctx.model_raw_response
        finally:
            coordinator.close()
            ledger.close()
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
