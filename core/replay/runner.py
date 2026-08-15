"""Deterministic, resumable Historical Replay / Walk-Forward runner."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Callable
from uuid import uuid4

from ..analysis_service import AnalysisService
from ..ai import OllamaProvider
from ..instruments import instrument_for
from ..outcomes import settle_prediction
from ..performance.calibration import apply_calibration, fit_calibration
from ..performance.metrics import aggregate_performance, build_performance_snapshot, is_actionable
from ..providers.base import ProviderError
from ..storage import SQLiteStore
from .provider import ReplayNewsProvider, ReplayProvider, build_as_of_points, fetch_historical_bars, future_bars


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    samples: int = 300
    model_id: str = "qwen3.5:4b"
    prompt_version: str = "phase2-json-v8"
    seed: int = 42
    db_path: str = "data/phase3-replay.sqlite3"
    output_path: str | None = None
    manifest_path: str | None = None
    resume: bool = False
    checkpoint_every: int = 5


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * 0.95))))
    return ordered[index]


def _replay_prediction_id(run_id: str, symbol: str, timeframe: str, as_of: datetime) -> str:
    """Return a stable per-sample ID even when routes share an as_of timestamp."""

    key = f"{run_id}|{symbol.upper()}|{timeframe.lower()}|{as_of.astimezone(timezone.utc).isoformat()}"
    return f"replay-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"


def _counts(store: SQLiteStore, run_id: str, total: int) -> dict[str, Any]:
    samples = store.list_replay_samples(run_id)
    records = store.list_prediction_records(source_type="replay", replay_run_id=run_id)
    metrics = aggregate_performance(records, scope={"source_type": "replay"})
    return {
        "planned": total,
        "sample_rows": len(samples),
        "completed": sum(1 for item in samples if item["status"] in {"COMPLETED", "WAIT"}),
        "wait": sum(1 for item in samples if item["status"] == "WAIT"),
        "errors": sum(1 for item in samples if item["status"] == "ERROR"),
        "actionable": metrics["actionable_count"],
        "resolved_actionable": metrics["resolved_actionable"],
        "outcomes": sum(1 for record in records if record.get("outcome") is not None),
    }


def _sample_plan(config: ReplayConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    routes = [(symbol.upper(), timeframe.lower()) for symbol in config.symbols for timeframe in config.timeframes]
    if not routes:
        raise ValueError("at least one symbol and timeframe are required")
    base, remainder = divmod(config.samples, len(routes))
    if base <= 0:
        raise ValueError("samples must be at least the number of symbol/timeframe routes")
    plan: list[dict[str, Any]] = []
    route_manifest: list[dict[str, Any]] = []
    for index, (symbol, timeframe) in enumerate(routes):
        history = fetch_historical_bars(instrument_for(symbol), timeframe, limit=1000)
        count = base + (1 if index < remainder else 0)
        points = build_as_of_points(history.bars, timeframe=timeframe, count=count, seed=config.seed + index)
        route_manifest.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "provider": history.provider,
                "data_start": history.data_start.isoformat(),
                "data_end": history.data_end.isoformat(),
                "bar_count": len(history.bars),
                "as_of": [point.isoformat() for point in points],
            }
        )
        for point in points:
            plan.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "as_of": point,
                    "history": history,
                }
            )
    plan.sort(key=lambda item: (item["as_of"], item["symbol"], item["timeframe"]))
    manifest = {
        "schema_version": "phase3-replay-manifest-v1",
        "symbols": [symbol.upper() for symbol in config.symbols],
        "timeframes": [timeframe.lower() for timeframe in config.timeframes],
        "samples": config.samples,
        "seed": config.seed,
        "model_id": config.model_id,
        "prompt_version": config.prompt_version,
        "sampling_policy": {
            "min_history_bars": 120,
            "deterministic_seed": config.seed,
            "order": "as_of_utc_then_symbol_then_timeframe",
            "news_history_available": False,
        },
        "routes": route_manifest,
    }
    return plan, manifest


def run_replay(
    config: ReplayConfig,
    *,
    llm_provider: object | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run real or test-injected replay samples sequentially with resume support."""

    started = time.perf_counter()
    store = SQLiteStore(config.db_path)
    store.initialize()
    plan, manifest = _sample_plan(config)
    manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    manifest_path = Path(config.manifest_path or f"data/phase3-replay-{manifest_hash[:12]}.manifest.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest["manifest_hash"] = manifest_hash
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    run = store.find_resumable_replay_run(manifest_hash) if config.resume else None
    run_id = run["run_id"] if run else f"replay-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    if run is None:
        store.create_replay_run(
            run_id=run_id,
            model_id=config.model_id,
            prompt_version=config.prompt_version,
            symbols=list(config.symbols),
            timeframes=list(config.timeframes),
            sampling_policy=manifest["sampling_policy"],
            manifest_hash=manifest_hash,
            config={
                "db_path": config.db_path,
                "samples": config.samples,
                "seed": config.seed,
                "manifest_path": str(manifest_path),
            },
            status="RUNNING",
        )
    else:
        store.update_replay_run(run_id, status="RUNNING", error_code=None)

    model = llm_provider or OllamaProvider(model_name=config.model_id, prompt_version=config.prompt_version)
    current_provider: ReplayProvider | None = None

    def market_factory(_instrument):
        if current_provider is None:
            raise ProviderError("replay provider is not initialized", code="replay_provider_not_initialized", provider="replay")
        return current_provider

    analysis_service = AnalysisService(
        market_provider_factory=market_factory,
        news_provider=ReplayNewsProvider(),
        llm_provider=model,
        store=None,
    )
    persistence_service = AnalysisService(store=store, llm_provider=None)
    errors: list[dict[str, Any]] = []
    latency_values: list[float] = []
    plan_by_key = {(item["symbol"], item["timeframe"], item["as_of"].isoformat()): item for item in plan}

    for index, item in enumerate(plan, start=1):
        symbol = item["symbol"]
        timeframe = item["timeframe"]
        as_of: datetime = item["as_of"]
        as_of_text = as_of.isoformat()
        existing = store.get_replay_sample(run_id, symbol, timeframe, as_of_text)
        if existing and existing["status"] in {"COMPLETED", "WAIT"} and existing.get("prediction_id"):
            if progress:
                progress({"completed": index, "total": len(plan), "status": "skipped_existing", "symbol": symbol, "timeframe": timeframe, "as_of": as_of_text})
            continue
        started_at = datetime.now(timezone.utc).isoformat()
        store.save_replay_sample(
            run_id=run_id,
            symbol=symbol,
            timeframe=timeframe,
            as_of=as_of_text,
            capability_flags={"news_history_available": False, "technical_only": True},
            prediction_id=None,
            status="RUNNING",
            started_at=started_at,
        )
        history = item["history"]
        current_provider = ReplayProvider(instrument_for(symbol), history.bars, as_of, underlying_provider=history.provider)
        try:
            result = analysis_service.analyze(
                instrument_for(symbol),
                timeframe=timeframe,
                limit=120,
                analysis_time=as_of,
                source_type="replay",
                replay_run_id=run_id,
                context_capabilities={
                    "replay": True,
                    "news_history_available": False,
                    "technical_only": True,
                    "underlying_market_provider": history.provider,
                },
            )
            observed_timestamps = [bar.timestamp for bar in result.context.bars]
            if observed_timestamps and max(observed_timestamps) > as_of:
                raise RuntimeError("future bar leaked into replay context")
            result = replace(
                result,
                signal=replace(
                    result.signal,
                    prediction_id=_replay_prediction_id(run_id, symbol, timeframe, as_of),
                ),
            )
            calibration_records = store.list_prediction_records(source_type="replay", model_id=config.model_id, prompt_version=config.prompt_version)
            calibration_scope = {"source_type": "replay", "model_id": config.model_id, "prompt_version": config.prompt_version}
            calibration = fit_calibration(
                calibration_records,
                scope=calibration_scope,
                trained_until=as_of,
                min_sample=100,
                version=f"cal-v1-{config.model_id.replace(':', '-')}",
            )
            signal = apply_calibration(result.signal, calibration)
            calibrated_result = replace(result, signal=signal)
            persistence_service.persist(calibrated_result)
            latency = result.model_status.get("latency_ms")
            if latency is not None:
                latency_values.append(float(latency))
            outcome = None
            if signal.action.value in {"LONG", "SHORT"} and not str(signal.parse_status).lower().endswith("failed"):
                outcome = settle_prediction(signal, future_bars(history.bars, as_of))
                store.save_outcome(outcome)
                sample_status = "COMPLETED"
            elif str(signal.parse_status).lower() in {"model_unavailable", "model_not_configured", "parse_error", "repair_failed"}:
                sample_status = "ERROR"
            else:
                sample_status = "WAIT"
            store.save_replay_sample(
                run_id=run_id,
                symbol=symbol,
                timeframe=timeframe,
                as_of=as_of_text,
                capability_flags={"news_history_available": False, "technical_only": True, "underlying_provider": history.provider},
                prediction_id=signal.prediction_id,
                status=sample_status,
                error_code=str(result.model_status.get("error_code")) if result.model_status.get("error_code") else None,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            if sample_status == "ERROR":
                errors.append({"symbol": symbol, "timeframe": timeframe, "as_of": as_of_text, "error_code": signal.parse_status})
            if progress:
                progress({"completed": index, "total": len(plan), "status": sample_status, "symbol": symbol, "timeframe": timeframe, "as_of": as_of_text, "action": signal.action.value})
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            errors.append({"symbol": symbol, "timeframe": timeframe, "as_of": as_of_text, "error_code": code, "message": str(exc)[:500]})
            store.save_replay_sample(
                run_id=run_id,
                symbol=symbol,
                timeframe=timeframe,
                as_of=as_of_text,
                capability_flags={"news_history_available": False, "technical_only": True},
                prediction_id=None,
                status="ERROR",
                error_code=code,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            if progress:
                progress({"completed": index, "total": len(plan), "status": "ERROR", "symbol": symbol, "timeframe": timeframe, "as_of": as_of_text, "error_code": code})
        finally:
            current_provider = None
        if index % max(1, config.checkpoint_every) == 0:
            store.update_replay_run(run_id, counts=_counts(store, run_id, len(plan)))

    records = store.list_prediction_records(source_type="replay", replay_run_id=run_id)
    scope = {"source_type": "replay", "model_id": config.model_id, "prompt_version": config.prompt_version}
    performance = build_performance_snapshot(records, scope=scope)
    store.save_performance_snapshot(performance)
    all_calibration_records = store.list_prediction_records(source_type="replay", model_id=config.model_id, prompt_version=config.prompt_version)
    final_calibration = fit_calibration(
        all_calibration_records,
        scope=scope,
        min_sample=100,
        version=f"cal-v1-{config.model_id.replace(':', '-')}",
    )
    store.save_calibration_result(final_calibration.to_dict())
    latency_values = store.list_replay_model_latencies(run_id)
    counts = _counts(store, run_id, len(plan))
    status = "COMPLETED" if not errors else "COMPLETED_WITH_ERRORS"
    store.update_replay_run(
        run_id,
        status=status,
        counts=counts,
        completed_at=datetime.now(timezone.utc).isoformat(),
        error_code=None if not errors else "sample_errors",
    )
    elapsed = (time.perf_counter() - started) * 1000.0
    output = {
        "phase": 3,
        "run_id": run_id,
        "status": status,
        "manifest_hash": manifest_hash,
        "manifest_path": str(manifest_path),
        "db_path": config.db_path,
        "model_id": config.model_id,
        "prompt_version": config.prompt_version,
        "source_type": "replay",
        "context_capabilities": {"news_history_available": False, "technical_only": True},
        "counts": counts,
        "elapsed_ms": round(elapsed, 3),
        "model_latency_ms": {
            "count": len(latency_values),
            "mean": mean(latency_values) if latency_values else None,
            "median": median(latency_values) if latency_values else None,
            "p95": _p95(latency_values),
        },
        "performance": performance,
        "calibration": final_calibration.to_dict(),
        "errors": errors,
    }
    output_path = Path(config.output_path or f"data/phase3-replay-{run_id}.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output["output_path"] = str(output_path)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
