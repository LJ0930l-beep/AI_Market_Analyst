"""Seed a disposable Phase 4 database and serve the real FastAPI app for E2E."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.ai.prompts import PROMPT_VERSION
from core.analysis_service import AnalysisService
from core.events import EventEvidence, source_credibility
from core.outcomes import settle_prediction
from core.performance.calibration import fit_calibration
from core.instruments import InstrumentCandidate
from core.providers import Bar, FixtureNewsProvider, FixtureProvider, InstrumentValidationResult, ProviderError, Quote
from core.quant import build_quant_snapshot
from core.scheduler import DefaultScanAnalysisExecutor, ResourceProbeResult
from core.settlement import SettlementService
from core.signals import Action, SignalProposal, build_signal, signal_from_payload
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


class E2ESchedulerClock:
    """A deterministic weekday market-open clock for scheduler browser coverage."""

    value = datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


class E2EInjectedSchedulerResourceProbe:
    """Deterministic resource guard; it never inspects or stops local processes."""

    def probe(self) -> ResourceProbeResult:
        return ResourceProbeResult(True, reason="e2e_ready", capability="injected_test")


class E2EInjectedSchedulerExecutor:
    """Counts scheduler calls while delegating to the real AnalysisService path."""

    def __init__(self, service: AnalysisService, store: SQLiteStore) -> None:
        self.calls = 0
        self._delegate = DefaultScanAnalysisExecutor(service, store)

    def execute(self, *args, **kwargs):
        self.calls += 1
        return self._delegate.execute(*args, **kwargs)


class E2EInjectedSettlementProvider:
    """Deterministic point-in-time bars for the un-followed fresh LONG."""

    provider_name = "e2e_settlement_provider"
    stale = False

    def __init__(self, signal: SignalProposal) -> None:
        self.signal = signal

    def get_bars(self, instrument, timeframe: str, limit: int = 200) -> list[Bar]:
        if instrument.symbol != self.signal.instrument.symbol:
            return []
        assert self.signal.entry_low is not None
        assert self.signal.entry_high is not None
        assert self.signal.stop is not None
        assert self.signal.tp1 is not None
        entry = (self.signal.entry_low + self.signal.entry_high) / 2.0
        if self.signal.action is Action.LONG:
            bar = Bar(
                self.signal.generated_at + timedelta(hours=1),
                entry,
                self.signal.tp1 * 1.01,
                (entry + self.signal.stop) / 2.0,
                self.signal.tp1,
                100.0,
            )
        else:
            bar = Bar(
                self.signal.generated_at + timedelta(hours=1),
                entry,
                (entry + self.signal.stop) / 2.0,
                self.signal.tp1 * 0.99,
                self.signal.tp1,
                100.0,
            )
        return [bar][:limit]

    def get_quote(self, instrument) -> Quote:
        bars = self.get_bars(instrument, "1h", 1)
        if not bars:
            raise ProviderError("no deterministic settlement bars", code="empty_data", provider=self.provider_name)
        bar = bars[-1]
        return Quote(instrument=instrument, timestamp=bar.timestamp, price=bar.close, volume=bar.volume)


class E2EInjectedEventProvider:
    """Typed, point-in-time event evidence for browser context coverage."""

    provider_name = "e2e_typed_events"

    def get_events(self, instrument, *, as_of: datetime, limit: int = 20) -> list[EventEvidence]:
        event_at = datetime(2025, 12, 31, 20, 0, tzinfo=timezone.utc)
        published_at = datetime(2025, 12, 31, 19, 0, tzinfo=timezone.utc)
        known_at = datetime(2025, 12, 31, 19, 30, tzinfo=timezone.utc)
        title = f"{instrument.symbol} earnings outlook"
        return [
            EventEvidence(
                event_id=f"e2e-{instrument.symbol}-wire",
                source="Reuters",
                source_type="professional",
                category="earnings",
                event_at=event_at,
                published_at=published_at,
                known_at=known_at,
                retrieved_at=as_of,
                importance=72,
                affected_symbols=(instrument.symbol,),
                title=title,
                summary="Deterministic typed event evidence for Phase 6 browser coverage.",
                url=f"https://example.invalid/e2e/{instrument.symbol}/wire",
                sentiment=0.2,
                primary_source=False,
                reported_credibility=80,
                credibility_score=source_credibility("Reuters", reported=80),
                provider=self.provider_name,
                capability={"historical_known_time": True, "injected_test": True},
            ),
            EventEvidence(
                event_id=f"e2e-{instrument.symbol}-ir",
                source="Company IR",
                source_type="official",
                category="earnings",
                event_at=event_at + timedelta(minutes=10),
                published_at=published_at + timedelta(minutes=10),
                known_at=known_at + timedelta(minutes=10),
                retrieved_at=as_of,
                importance=72,
                affected_symbols=(instrument.symbol,),
                title=title,
                summary="Deterministic official-source event evidence for Phase 6 browser coverage.",
                url=f"https://example.invalid/e2e/{instrument.symbol}/ir",
                sentiment=-0.2,
                primary_source=True,
                reported_credibility=80,
                credibility_score=source_credibility("Company IR", primary_source=True, reported=80),
                provider=self.provider_name,
                capability={"historical_known_time": True, "injected_test": True},
            ),
        ][:limit]


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
    now = E2ESchedulerClock.value

    # The browser follows this fresh signal. It is intentionally not followed here.
    fresh = _signal("TSLA", "p4-fresh-long", action=Action.LONG, generated_at=now - timedelta(hours=3))
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
    store.set_scheduler_state(
        "alerts.radar_state",
        {"TSLA": {"category": "NOT_RANKED", "transition_sequence": 0}},
        updated_at=now.isoformat(),
    )

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

    analysis_service = AnalysisService(
        market_provider_factory=lambda _instrument: FixtureProvider(),
        benchmark_provider_factory=lambda _instrument: FixtureProvider(),
        news_provider=FixtureNewsProvider(),
        event_provider=E2EInjectedEventProvider(),
        llm_provider=None,
        store=SQLiteStore(args.db),
    )
    scheduler_store = SQLiteStore(args.db)
    scheduler_executor = E2EInjectedSchedulerExecutor(analysis_service, scheduler_store)
    scheduler_clock = E2ESchedulerClock()
    fresh_payload = scheduler_store.load_prediction_payload("p4-fresh-long")
    if fresh_payload is None:
        raise RuntimeError("fresh E2E settlement prediction was not seeded")
    fresh_signal = signal_from_payload(fresh_payload, instrument=instrument_for("TSLA"))
    settlement_service = SettlementService(
        store=scheduler_store,
        provider_factory=lambda _instrument: E2EInjectedSettlementProvider(fresh_signal),
        clock=scheduler_clock.now,
    )

    print(f"P4_E2E_API_READY http://{args.host}:{args.port}", flush=True)
    uvicorn.run(
        create_app(
            store=scheduler_store,
            analysis_service=analysis_service,
            event_provider=E2EInjectedEventProvider(),
            instrument_validator=E2EInjectedInstrumentValidator(),
            scheduler_executor=scheduler_executor,
            scheduler_resource_probe=E2EInjectedSchedulerResourceProbe(),
            scheduler_clock=scheduler_clock.now,
            settlement_service=settlement_service,
        ),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
