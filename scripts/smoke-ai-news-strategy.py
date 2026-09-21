"""Public Gate + RSS + real local Bonsai -> isolated PAPER execution smoke.

Never opens a user's database, reads exchange credentials or starts a session.
This verifies model/decision/execution integration, not profitability or the
desktop scheduler's calibration/startup gates.
"""
from __future__ import annotations

import json
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.ai.ollama import OllamaProvider
from core.instruments import instrument_for
from core.model_client import model_client
from core.model_routing import DEFAULT_SMART_MODEL
from core.news_refresh import refresh_public_news
from core.providers.gateio_provider import GatePublicProvider
from core.storage import SQLiteStore
from core.trading.ai_led_engine import AICycleContext, AILedDecisionEngine
from core.trading import ai_session_coordinator as coordinator_module
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.ai_strategy_book import AIStrategyBook
from core.trading.autonomous_strategy import CONTRACT, technical_context, book_cost_evidence
from core.trading.model_schemas import require_confidence_for_open, validate_schema
from core.trading.execution_gateway import ExecutionGateway
from core.trading.ledger import AccountLedger
from core.trading.position_guardian import PositionGuardian
from core.trading.risk_engine import RiskEngine
from core.trading.session_manager import SessionManager


def _prompt_audit_summary(messages: list[dict], schema: dict | None) -> dict:
    """Summarize request shape without writing the full system/user prompt."""
    system = str(messages[0].get("content") or "") if messages else ""
    user_text = str(messages[1].get("content") or "") if len(messages) > 1 else ""
    try:
        payload = json.loads(user_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    field_sizes = sorted(
        ((str(key), coordinator_module._estimate_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))) for key, value in payload.items()),
        key=lambda item: item[1], reverse=True,
    )
    technical = payload.get("technical_context") if isinstance(payload.get("technical_context"), dict) else {}
    technical_summary = {}
    for symbol, item in technical.items():
        frames = item.get("timeframes") if isinstance(item, dict) and isinstance(item.get("timeframes"), dict) else {}
        technical_summary[str(symbol)] = {
            str(tf): {"status": frame.get("status"), "bars": len(frame.get("candles") or [])}
            for tf, frame in frames.items() if isinstance(frame, dict)
        }
    strategy = payload.get("active_strategy") if isinstance(payload.get("active_strategy"), dict) else {}
    profile = strategy.get("profile") if isinstance(strategy.get("profile"), dict) else {}
    account = payload.get("account_truth") if isinstance(payload.get("account_truth"), dict) else {}
    snapshots = payload.get("market_snapshots") if isinstance(payload.get("market_snapshots"), dict) else {}
    repair_details = {}
    if len(messages) > 1 and str(messages[0].get("content") or "").startswith("修复无效 JSON"):
        try:
            repair_payload = json.loads(user_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            repair_payload = {}
        if isinstance(repair_payload, dict):
            previous = repair_payload.get("previous_decision")
            repair_details = {
                "repair_validation_error": str(repair_payload.get("validation_error") or "")[:240],
                "previous_decision": {
                    key: previous.get(key)
                    for key in ("action", "instrument_id", "confidence")
                    if isinstance(previous, dict) and key in previous
                },
            }
    return {
        "prompt_version": None,
        "system_estimated_tokens": coordinator_module._estimate_tokens(system),
        "user_estimated_tokens": coordinator_module._estimate_tokens(user_text),
        "user_field_names": sorted(payload),
        "largest_user_fields": field_sizes[:5],
        "schema_required": list((schema or {}).get("required") or []),
        "allowed_instruments": payload.get("allowed_instruments", []),
        "market_prices": {key: value.get("price") for key, value in snapshots.items() if isinstance(value, dict)},
        "technical": technical_summary,
        "news_revision_count": len(payload.get("news_revisions") or []),
        "candidate_count": len(payload.get("candidates") or []),
        "strategy": {
            key: strategy.get(key) for key in ("name", "template_id", "style") if strategy.get(key) is not None
        } | {"signal_timeframe": profile.get("signal_timeframe")},
        "account": {key: account.get(key) for key in ("status", "source", "equity", "available_margin") if account.get(key) is not None},
    } | repair_details


def _install_model_audit(model, calls: list[dict]) -> None:
    """Record bounded result/schema summaries for each real model call."""
    generate_json = model.generate_json

    def audited_generate_json(messages, *args, **kwargs):
        item = _prompt_audit_summary(messages, kwargs.get("schema"))
        item["prompt_version"] = kwargs.get("prompt_version")
        item["reasoning_effort"] = kwargs.get("reasoning_effort")
        item["max_tokens"] = kwargs.get("max_tokens")
        try:
            result = generate_json(messages, *args, **kwargs)
        except Exception as exc:
            item.update(
                call_status="ERROR",
                error_type=type(exc).__name__,
                error_code=getattr(exc, "code", None),
            )
            calls.append(item)
            raise
        decoded = result[0] if isinstance(result, tuple) and result else result
        if isinstance(decoded, dict):
            item["response"] = {
                "action": decoded.get("action"),
                "instrument_id": decoded.get("instrument_id"),
                "reason": str(decoded.get("reason") or "")[:240],
                "confidence": decoded.get("confidence"),
                "fields": sorted(decoded),
            }
            try:
                validate_schema(decoded, kwargs.get("schema") or {})
                try:
                    require_confidence_for_open(decoded)
                    item["schema_result"] = "PASS"
                except Exception as exc:
                    item["schema_result"] = "FAIL"
                    item["schema_error"] = f"{type(exc).__name__}: {exc}"[:200]
            except Exception as exc:
                item["schema_result"] = "FAIL"
                item["schema_error"] = f"{type(exc).__name__}: {exc}"[:200]
            metadata = result[2] if isinstance(result, tuple) and len(result) > 2 and isinstance(result[2], dict) else {}
            returned_model = str(metadata.get("model_id") or "").strip()
            if returned_model and returned_model not in {DEFAULT_SMART_MODEL}:
                item["coordinator_prevalidation_error"] = "SMART_MODEL_MISMATCH"
            elif item.get("schema_result") == "FAIL":
                item["coordinator_prevalidation_error"] = item.get("schema_error")
            else:
                item["coordinator_prevalidation_error"] = None
            item["receipt"] = {
                key: metadata.get(key)
                for key in ("model_id", "actual_model_id", "model_identity_source", "verified_manifest_model_id")
                if metadata.get(key) is not None
            }
        else:
            item["response"] = {"type": type(decoded).__name__}
            item["schema_result"] = "FAIL"
            item["schema_error"] = "completion was not a JSON object"
        item["call_status"] = "COMPLETED"
        calls.append(item)
        return result

    model.generate_json = audited_generate_json


def _require_smoke_instrument(instrument_id: str, allowed_instruments: tuple[str, ...]) -> None:
    """Never count a WAIT/HOLD carrying a made-up symbol as a successful smoke."""
    if str(instrument_id or "").strip().upper() not in {str(item).strip().upper() for item in allowed_instruments}:
        raise RuntimeError("SMOKE_INVALID_INSTRUMENT_ID")


def _smoke_completion_status(action: str, strategy_plan, technical_status: str, news_count: int) -> str:
    """A valid no-trade decision is complete; only OPEN needs an execution plan."""
    if technical_status != "READY" or news_count <= 0:
        return "PARTIAL"
    if str(action or "").upper() in {"OPEN_LONG", "OPEN_SHORT"} and not strategy_plan:
        return "PARTIAL"
    return "PASS"


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=None,
        help="report path (defaults to evidence/ai_news_strategy_smoke.json)",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    report_path = Path(args.output) if args.output else root / "evidence" / "ai_news_strategy_smoke.json"
    if not report_path.is_absolute():
        report_path = root / report_path
    report = {"contract": CONTRACT, "model": DEFAULT_SMART_MODEL, "external_orders": False,
              "scope": "PUBLIC_DATA_REAL_MODEL_ISOLATED_PAPER", "tested_at": datetime.now(timezone.utc).isoformat()}
    model_calls: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="aima-news-smoke-") as directory:
        store = SQLiteStore(Path(directory) / "paper.db")
        store.initialize()
        ledger = AccountLedger(store)
        ledger.create_account("smoke-paper", mode="PAPER", initial_deposit=Decimal("10000"))
        gateway = ExecutionGateway(store, ledger=ledger)
        guardian = PositionGuardian(store, ledger)
        model = OllamaProvider(base_url=model_client.base_url, model_name=DEFAULT_SMART_MODEL, timeout=55, context_length=8192, max_tokens=1800, retries=0)
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
            paper_positions = ledger.get_open_positions("smoke-paper", venue="simulated", mode="PAPER")
            paper_account = ledger.get_snapshot(
                "smoke-paper", open_positions=paper_positions,
                mark_prices={"BTCUSDT": quote.price}, now=now,
            )
            # The real-model smoke uses the same active strategy contract and
            # locally sourced PAPER account facts as an ordinary cycle. This
            # remains an isolated temporary ledger with no exchange credentials.
            active_strategy = AIStrategyBook(store).active("smoke-paper")
            account_truth = {
                "status": "AVAILABLE",
                "source": "LOCAL_PAPER_LEDGER",
                "observed_at": paper_account.as_of.isoformat(),
                "snapshot_id": paper_account.snapshot_id,
                "equity": float(paper_account.equity),
                "available_margin": float(paper_account.available_margin),
                "used_margin": float(paper_account.allocated_margin),
                "unrealized_pnl": float(paper_account.unrealized_pnl),
                "realized_pnl": float(paper_account.realized_pnl),
                "positions": paper_positions,
                "pending_orders": [],
                "account_id": "smoke-paper",
                "mode": "PAPER",
                "venue": "simulated",
            }
            ctx = AICycleContext(cycle_id="public-model-smoke", account_id="smoke-paper", generation=1,
                                 started_at=now.isoformat(), expires_at=(now + timedelta(seconds=240)).isoformat(),
                                 allowed_instruments=("BTCUSDT",), decision_contract=CONTRACT,
                                 market_snapshots={"BTCUSDT": {"price": quote.price, "data_as_of": quote.timestamp.isoformat(), "fresh": True, **book}},
                                 technical_context=technical_context(store, ("BTCUSDT",), now),
                                 news_revisions=coordinator._news_revisions(("BTCUSDT",), now=now),
                                 account_truth=account_truth,
                                 positions=paper_positions,
                                 strategy_instructions=active_strategy,
                                 universe_snapshot={
                                     "status": "READY", "environment": "PAPER",
                                     "selected_symbols": ["BTCUSDT"], "mode": "CUSTOM",
                                     "selection": "isolated_real_model_smoke",
                                 },
                                 market_data_environment="PUBLIC_GATE",
                                 data_quality={"status": "READY", "quote_as_of": quote.timestamp.isoformat()})
            report["technical_status"] = ctx.technical_context["BTCUSDT"]["status"]
            report["news_count"] = len(ctx.news_revisions)
            report["strategy_template_id"] = active_strategy["template_id"]
            report["account_truth_source"] = account_truth["source"]
            report["paper_equity"] = account_truth["equity"]
            print(f"Calling local {DEFAULT_SMART_MODEL} via {model_client.base_url}", flush=True)
            health = coordinator._health(force=True)
            report["model_health"] = health
            if health.get("status") != "READY":
                raise RuntimeError("LOCAL_BONSAI_NOT_READY")
            # AISessionCoordinator clones OllamaProvider in __init__ to pin
            # per-session inference settings, so instrument the effective
            # provider that will actually be called.
            _install_model_audit(coordinator.model_provider, model_calls)
            output = coordinator._model_output(ctx)
            # WAIT/HOLD are valid model decisions, but a fabricated/missing
            # symbol means the end-to-end market-context contract was not met.
            _require_smoke_instrument(output.instrument_id, ctx.allowed_instruments)
            engine = AILedDecisionEngine(store, gateway, RiskEngine(ledger), ledger, guardian)
            result = engine.execute_cycle(ctx, now=datetime.now(timezone.utc), model_output=output)
            report["model_inference_settings"] = dict(ctx.model_inference_settings)
            report.update(status="PASS", model_action=output.action, execution_status=result.status,
                          reason=result.reason, model_latency_ms=ctx.model_latency_ms, model_digest=ctx.model_digest,
                          strategy_plan=output.extra_fields.get("strategy_plan"), analysis=output.extra_fields,
                          input_hash=ctx.input_hash, prompt_version=ctx.prompt_version,
                          evidence_refs=list(output.evidence_refs), paper_order_created=result.order_intent is not None)
            report["status"] = _smoke_completion_status(
                output.action, output.extra_fields.get("strategy_plan"),
                report["technical_status"], report["news_count"],
            )
        except Exception as exc:
            report.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
            if 'ctx' in locals():
                report["model_raw_response"] = ctx.model_raw_response
        finally:
            report["model_calls"] = model_calls
            coordinator.close()
            ledger.close()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
