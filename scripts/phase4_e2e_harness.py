"""Seed a disposable Phase 4 database and serve the real FastAPI app for E2E."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.ai.prompts import PROMPT_VERSION
from core.outcomes import settle_prediction
from core.performance.calibration import fit_calibration
from core.instruments import InstrumentCandidate
from core.providers import Bar, FixtureProvider, InstrumentValidationResult
from core.quant import build_quant_snapshot
from core.signals import Action, SignalProposal, build_signal
from core.storage import SQLiteStore
from core.instruments import instrument_for


MODEL_ID = "p4-e2e-model"
PROMPT = "p4-e2e-prompt-v1"
RUN_ID = "p4-e2e-replay-with-errors"


class E2EInjectedInstrumentValidator:
    """Deterministic dependency for registration UI coverage; not provider proof."""

    def validate(self, candidate: InstrumentCandidate) -> InstrumentValidationResult:
        now = datetime.now(timezone.utc)
        return InstrumentValidationResult(
            provider="e2e-public-probe",
            validated_at=now,
            data_as_of=now,
            mode="injected_test",
        )


def _signal(
    symbol: str,
    prediction_id: str,
    *,
    action: Action,
    generated_at: datetime,
    source_type: str = "live",
    replay_run_id: str | None = None,
) -> SignalProposal:
    instrument = instrument_for(symbol)
    bars = FixtureProvider().get_bars(instrument, "1h", 120)
    quant = build_quant_snapshot(bars, "1h", symbol=instrument.symbol)
    return build_signal(
        instrument,
        quant,
        timeframe="1h",
        generated_at=generated_at,
        prediction_id=prediction_id,
        model_id=MODEL_ID,
        prompt_version=PROMPT,
        data_as_of=bars[-1].timestamp,
        force_action=action,
        parse_status="baseline",
        source_type=source_type,
        replay_run_id=replay_run_id,
        summary=f"Deterministic Phase 4 E2E {action.value} evidence.",
    )


def _tp1_outcome(signal: SignalProposal):
    assert signal.entry_low is not None
    assert signal.entry_high is not None
    assert signal.stop is not None
    assert signal.tp1 is not None
    entry = (signal.entry_low + signal.entry_high) / 2.0
    if signal.action is Action.LONG:
        bar = Bar(signal.generated_at + timedelta(hours=1), entry, signal.tp1 * 1.01, (entry + signal.stop) / 2.0, signal.tp1, 100.0)
    else:
        bar = Bar(signal.generated_at + timedelta(hours=1), entry, (entry + signal.stop) / 2.0, signal.tp1 * 0.99, signal.tp1, 100.0)
    return settle_prediction(signal, [bar])


def _save_prediction(store: SQLiteStore, signal: SignalProposal, *, follow: bool = False, outcome: bool = False) -> None:
    store.save_instrument(signal.instrument)
    store.save_prediction(signal)
    if follow:
        store.follow_prediction(signal.prediction_id, signal.generated_at.isoformat())
    if outcome:
        store.save_outcome(_tp1_outcome(signal))


def seed_database(db_path: Path) -> dict[str, int]:
    store = SQLiteStore(db_path)
    store.initialize()
    now = datetime.now(timezone.utc).replace(microsecond=0)

    # The browser follows this fresh signal. It is intentionally not followed here.
    fresh = _signal("TSLA", "p4-fresh-long", action=Action.LONG, generated_at=now)
    _save_prediction(store, fresh)

    # Explicit browser analysis is model-disabled and therefore persists a WAIT.
    wait = _signal("AAPL", "p4-seeded-wait", action=Action.WAIT, generated_at=now - timedelta(hours=2))
    _save_prediction(store, wait)

    # Existing linked PaperTrade and Outcome evidence for the detail route.
    paper = _signal("NVDA", "p4-paper-outcome", action=Action.LONG, generated_at=now - timedelta(days=2))
    _save_prediction(store, paper, follow=True, outcome=True)

    # One replay prediction and two sample rows, including an explicit error/capability row.
    replay = _signal(
        "NVDA",
        "p4-replay-actionable",
        action=Action.SHORT,
        generated_at=now - timedelta(days=1),
        source_type="replay",
        replay_run_id=RUN_ID,
    )
    _save_prediction(store, replay, outcome=True)
    store.create_replay_run(
        run_id=RUN_ID,
        model_id=MODEL_ID,
        prompt_version=PROMPT,
        symbols=["NVDA", "AAPL"],
        timeframes=["1h"],
        sampling_policy={
            "samples": 2,
            "resume": True,
            "execution": "fixture",
            "min_history_bars": 60,
            "deterministic_seed": 42,
            "order": "symbol,timeframe,as_of",
            "news_history_available": False,
        },
        manifest_hash="p4-e2e-manifest",
        config={
            "samples": 2,
            "seed": 42,
            "db_path": str(db_path),
            "manifest_path": str(db_path.with_suffix(".manifest.json")),
            "requested_via": "phase4-e2e-harness",
            "execution": "local fixture harness",
        },
        status="COMPLETED_WITH_ERRORS",
    )
    sample_time = (now - timedelta(days=1)).isoformat()
    store.save_replay_sample(
        run_id=RUN_ID,
        symbol="NVDA",
        timeframe="1h",
        as_of=sample_time,
        capability_flags={"news_history_available": False, "technical_only": True},
        prediction_id=replay.prediction_id,
        status="COMPLETED",
        started_at=sample_time,
        completed_at=(now - timedelta(days=1) + timedelta(minutes=1)).isoformat(),
    )
    error_time = (now - timedelta(hours=23)).isoformat()
    store.save_replay_sample(
        run_id=RUN_ID,
        symbol="AAPL",
        timeframe="1h",
        as_of=error_time,
        capability_flags={"news_history_available": False, "technical_only": True},
        prediction_id=None,
        status="ERROR",
        error_code="HISTORICAL_NEWS_UNAVAILABLE",
        started_at=error_time,
        completed_at=(now - timedelta(hours=22, minutes=59)).isoformat(),
    )
    store.update_replay_run(
        RUN_ID,
        status="COMPLETED_WITH_ERRORS",
        counts={
            "planned": 2,
            "sample_rows": 2,
            "completed": 1,
            "wait": 0,
            "errors": 1,
            "actionable": 1,
            "resolved_actionable": 1,
            "outcomes": 1,
        },
        completed_at=now.isoformat(),
        error_code="SAMPLE_ERRORS",
    )

    # Seed enough valid resolved-actionable records for an actually ACTIVE calibration artifact.
    calibration_start = now - timedelta(days=20)
    for index in range(100):
        action = Action.LONG if index % 2 == 0 else Action.SHORT
        signal = _signal(
            "AMD" if index % 2 == 0 else "BTCUSDT",
            f"p4-calibration-{index:03d}",
            action=action,
            generated_at=calibration_start + timedelta(hours=index),
        )
        _save_prediction(store, signal, outcome=True)

    live_records = store.list_prediction_records(source_type="live")
    calibration = fit_calibration(
        live_records,
        scope={"source_type": "live", "model_id": MODEL_ID},
        global_records=live_records,
        min_sample=100,
        version="p4-e2e-calibration-v1",
    )
    store.save_calibration_result(calibration.to_dict())

    counts = store.counts()
    print(
        f"P4_E2E_SEEDED db={db_path} predictions={counts['predictions']} "
        f"paper_trades={counts['paper_trades']} outcomes={counts['outcomes']} "
        f"replay_runs={counts['replay_runs']} replay_samples={counts['replay_samples']} "
        f"calibration_sample={calibration.sample_count}",
        flush=True,
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    seed_database(args.db)
    os.environ.update(
        {
            "DATABASE_PATH": str(args.db),
            "MARKET_DATA_MODE": "fixture",
            "NEWS_MODE": "fixture",
            "LLM_MODE": "disabled",
            "API_CORS_ORIGINS": f"http://{args.host}:4173,http://localhost:4173",
        }
    )
    import uvicorn
    from apps.api.main import create_app

    print(f"P4_E2E_API_READY http://{args.host}:{args.port}", flush=True)
    uvicorn.run(
        create_app(instrument_validator=E2EInjectedInstrumentValidator()),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
