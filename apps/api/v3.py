"""Institutional audit API (v3).

The v3 surface is intentionally split into read-only evidence projections and
explicit task writes.  It never fetches a private account, places an order,
or treats a deterministic fixture as proof of a live model/provider result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from core.analysis.ai_trade_analytics import build_execution_ledger_projection
from core.evidence import EvidenceBundle, persist_evidence_bundle
from core.quant.strategies import STRATEGIES, get_strategy_spec
from core.security.local_guard import validate_local_request
from core.trading.institutional_risk import (
    build_exposure_snapshots,
    cluster_pressure,
)
from core.trading.ledger import AccountLedger
from core.trading.risk_engine import RiskEngine
from core.trading.trader_capabilities import TraderCapabilityError, TraderCapabilityService


class DataBackfillBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=1, max_length=180)
    symbol: str = Field(min_length=1, max_length=50)
    timeframe: str = Field(min_length=1, max_length=20)
    source: str = Field(min_length=1, max_length=200)
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=180)


class ResearchRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str = Field(min_length=1, max_length=100)
    strategy_id: str = Field(min_length=1, max_length=120)
    strategy_version: str | None = Field(default=None, max_length=120)
    symbol: str = Field(min_length=1, max_length=50)
    mode: str | None = Field(default=None, pattern="^(PAPER|TESTNET|LIVE)$")
    venue: str | None = Field(default=None, min_length=1, max_length=50)
    timeframe: str = Field(default="15m", min_length=1, max_length=20)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=180)
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    train_fraction: float = Field(default=0.7, ge=0.5, lt=1.0)
    min_samples: int = Field(default=100, ge=1, le=100000)
    parameter_perturbations: list[dict[str, Any]] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=180)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, default=str)


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _clean_query_time(value: AwareDatetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("config_json", "result_json", "payload_json", "missing_json", "reason_codes_json", "evidence_refs_json", "metrics_json", "settings_json", "response_json"):
        if key in result:
            result[key.removesuffix("_json")] = _object(result[key]) if key not in {"missing_json", "reason_codes_json", "evidence_refs_json"} else _array(result[key])
    return result


def _array(value: Any) -> list[Any]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _require_account(store: Any, account_id: str) -> dict[str, str]:
    with store._connect() as db:
        row = db.execute("SELECT account_id, mode, config_json FROM accounts WHERE account_id=?", (account_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"ACCOUNT_NOT_FOUND: Account '{account_id}' is not registered")
    config = _object(row["config_json"])
    mode = str(row["mode"]).upper()
    venue = str(config.get("venue") or ("simulated" if mode == "PAPER" else "gate")).strip().lower()
    return {"account_id": str(row["account_id"]), "mode": mode, "venue": venue}


def _task_key(prefix: str, supplied: str | None, config: dict[str, Any]) -> str:
    return str(supplied or f"{prefix}:{_hash(config)[:32]}").strip()


def _task_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "task_type": row["task_type"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "progress": float(row["progress"] or 0),
        "config": _object(row["config_json"]),
        "result": _object(row["result_json"]) if row["result_json"] else None,
        "error_code": row["error_code"],
        "error_detail": row["error_detail"],
        "cancel_requested": bool(row["cancel_requested"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_or_create_task(
    store: Any,
    *,
    task_type: str,
    idempotency_key: str,
    config: dict[str, Any],
    status: str,
    result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    task_id = f"task_{_hash({'type': task_type, 'key': idempotency_key})[:28]}"
    now = _now().isoformat()
    with store._connect() as db:
        row = db.execute("SELECT * FROM institutional_tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row is not None:
            if _hash(_object(row["config_json"])) != _hash(config):
                raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
            return _task_payload(row), False
        try:
            db.execute(
                """INSERT INTO institutional_tasks(
                    task_id, task_type, idempotency_key, status, progress,
                    config_json, result_json, created_at, updated_at,
                    account_id, venue, mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    task_type,
                    idempotency_key,
                    status,
                    1.0 if status not in {"RUNNING", "QUEUED"} else 0.0,
                    _json(config),
                    _json(result) if result is not None else None,
                    now,
                    now,
                    config.get("account_id"),
                    config.get("venue"),
                    config.get("mode"),
                ),
            )
        except sqlite3.IntegrityError:
            row = db.execute("SELECT * FROM institutional_tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row is None:
                raise
            if _hash(_object(row["config_json"])) != _hash(config):
                raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
            return _task_payload(row), False
        row = db.execute("SELECT * FROM institutional_tasks WHERE task_id=?", (task_id,)).fetchone()
    assert row is not None
    return _task_payload(row), True


def _update_task(store: Any, task_id: str, *, status: str, result: dict[str, Any] | None = None, error_code: str | None = None, error_detail: str | None = None) -> dict[str, Any]:
    with store._connect() as db:
        db.execute(
            """UPDATE institutional_tasks SET status=?, progress=?, result_json=?,
                       error_code=?, error_detail=?, updated_at=? WHERE task_id=?""",
            (status, 1.0 if status not in {"RUNNING", "QUEUED"} else 0.0, _json(result) if result is not None else None, error_code, error_detail, _now().isoformat(), task_id),
        )
        row = db.execute("SELECT * FROM institutional_tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    return _task_payload(row)


def _research_run_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["config"] = _object(result.pop("config_json", "{}"))
    raw_result = result.pop("result_json", None)
    result["result"] = _object(raw_result) if raw_result else None
    return result


def _freeze_research_evidence(store: Any, *, run_id: str, dataset_id: str, config: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    now = _now()
    requested_as_of = config.get("window_end")
    as_of = datetime.fromisoformat(str(requested_as_of)) if requested_as_of else now
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    expires = max(now + timedelta(hours=1), as_of.astimezone(timezone.utc) + timedelta(days=1))
    bundle = EvidenceBundle.freeze(
        dataset_id=dataset_id,
        as_of=as_of,
        expires_at=expires,
        payload={"research_run_id": run_id, "config": config},
        bundle_id=f"bundle_{run_id}",
        missing=missing,
        references=[f"research_run:{run_id}"],
    )
    return persist_evidence_bundle(store, bundle)


def router_for(get_store):
    def verify_local_request(request: Request) -> None:
        ok, reason = validate_local_request(request.headers.get("host"), request.headers.get("origin"))
        if not ok:
            raise HTTPException(status_code=403, detail=reason)

    router = APIRouter(prefix="/v3", dependencies=[Depends(verify_local_request)])

    @router.get("/data/catalog")
    def data_catalog(store=Depends(get_store)) -> dict[str, Any]:
        with store._connect() as db:
            explicit = [dict(row) for row in db.execute("SELECT * FROM data_catalog ORDER BY updated_at DESC, dataset_id").fetchall()]
            derived = [
                dict(row)
                for row in db.execute(
                    """SELECT 'bars:' || instrument_key || ':' || timeframe AS dataset_id,
                              instrument_key, timeframe, MAX(source) AS source,
                              MIN(bar_start) AS as_of_start, MAX(bar_start) AS as_of_end,
                              COUNT(*) AS row_count,
                              CASE WHEN SUM(CASE WHEN quality_status='LEGACY_UNVERIFIED' THEN 1 ELSE 0 END)>0
                                   THEN 'LEGACY_UNVERIFIED' ELSE 'OBSERVED' END AS quality_status,
                              MAX(raw_hash) AS manifest_hash
                       FROM market_bar_versions
                       GROUP BY instrument_key, timeframe
                       ORDER BY MAX(bar_start) DESC"""
                ).fetchall()
            ]
            migrations = [dict(row) for row in db.execute("SELECT * FROM institutional_migration_runs ORDER BY updated_at DESC").fetchall()]
        known = {str(item.get("dataset_id")) for item in explicit}
        datasets = explicit + [item for item in derived if item.get("dataset_id") not in known]
        return {"datasets": datasets, "migrations": migrations, "read_only": True}

    @router.get("/datasets/{dataset_id}")
    def get_dataset(dataset_id: str, store=Depends(get_store)) -> dict[str, Any]:
        """Return one immutable catalog projection without starting a task."""

        with store._connect() as db:
            row = db.execute("SELECT * FROM data_catalog WHERE dataset_id=?", (dataset_id,)).fetchone()
            if row is not None:
                dataset = _row(row)
            else:
                derived = db.execute(
                    """SELECT 'bars:' || instrument_key || ':' || timeframe AS dataset_id,
                              instrument_key, timeframe, MAX(source) AS source,
                              MIN(bar_start) AS as_of_start, MAX(bar_start) AS as_of_end,
                              COUNT(*) AS row_count,
                              CASE WHEN SUM(CASE WHEN quality_status='LEGACY_UNVERIFIED' THEN 1 ELSE 0 END)>0
                                   THEN 'LEGACY_UNVERIFIED' ELSE 'OBSERVED' END AS quality_status,
                              MAX(raw_hash) AS manifest_hash
                       FROM market_bar_versions
                       WHERE 'bars:' || instrument_key || ':' || timeframe=?
                       GROUP BY instrument_key, timeframe""",
                    (dataset_id,),
                ).fetchone()
                dataset = dict(derived) if derived is not None else None
        if dataset is None:
            raise HTTPException(status_code=404, detail="DATASET_NOT_FOUND")
        return {"dataset": dataset, "read_only": True}

    @router.get("/data/latest")
    def data_latest(
        symbol: str,
        timeframe: str = "15m",
        limit: int = 100,
        instrument_key: str | None = None,
        venue: str | None = None,
        market_type: str | None = None,
        price_type: str | None = None,
        store=Depends(get_store),
    ) -> dict[str, Any]:
        try:
            bars = store.latest_bars(symbol, timeframe, limit=limit, instrument_key=instrument_key, venue=venue, market_type=market_type, price_type=price_type)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"symbol": symbol.strip().upper(), "timeframe": timeframe.lower(), "bars": bars, "read_only": True}

    @router.get("/data/range")
    def data_range(
        symbol: str,
        timeframe: str = "15m",
        start: AwareDatetime | None = None,
        end: AwareDatetime | None = None,
        cursor: AwareDatetime | None = None,
        limit: int = 500,
        instrument_key: str | None = None,
        venue: str | None = None,
        market_type: str | None = None,
        price_type: str | None = None,
        store=Depends(get_store),
    ) -> dict[str, Any]:
        try:
            bars = store.range_bars(
                symbol,
                timeframe,
                start=_clean_query_time(start),
                end=_clean_query_time(end),
                cursor=_clean_query_time(cursor),
                limit=limit,
                instrument_key=instrument_key,
                venue=venue,
                market_type=market_type,
                price_type=price_type,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"symbol": symbol.strip().upper(), "timeframe": timeframe.lower(), "bars": bars, "read_only": True}

    @router.get("/data/quality")
    def data_quality(store=Depends(get_store)) -> dict[str, Any]:
        with store._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT quality_status, COUNT(*) AS row_count FROM market_bar_versions GROUP BY quality_status ORDER BY quality_status").fetchall()]
            totals = db.execute("SELECT COUNT(*) AS row_count, COUNT(DISTINCT instrument_key) AS instrument_count FROM market_bar_versions").fetchone()
            migrations = [dict(row) for row in db.execute("SELECT migration_key, source_rows, target_rows, duplicate_rows, orphan_rows, status, source_hash, updated_at FROM institutional_migration_runs ORDER BY updated_at DESC").fetchall()]
        return {
            "status": "OBSERVED" if totals and int(totals["row_count"] or 0) else "NO_DATA",
            "row_count": int(totals["row_count"] or 0) if totals else 0,
            "instrument_count": int(totals["instrument_count"] or 0) if totals else 0,
            "by_quality": rows,
            "migrations": migrations,
            "read_only": True,
        }

    @router.get("/tasks/{task_id}")
    def get_task(task_id: str, store=Depends(get_store)) -> dict[str, Any]:
        with store._connect() as db:
            row = db.execute("SELECT * FROM institutional_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
        return _task_payload(row) | {"read_only": True}

    @router.post("/data/backfills")
    def create_backfill(body: DataBackfillBody, store=Depends(get_store)) -> dict[str, Any]:
        config = body.model_dump(mode="json")
        config.pop("idempotency_key", None)
        config["symbol"] = body.symbol.strip().upper()
        config["timeframe"] = body.timeframe.strip().lower()
        key = _task_key("backfill", body.idempotency_key, config)
        task, created = _get_or_create_task(
            store,
            task_type="DATA_BACKFILL",
            idempotency_key=key,
            config=config,
            status="NOT_CONFIGURED",
            result={
                "status": "NOT_CONFIGURED",
                "reason": "External/public data backfill adapter is not configured for this isolated API task.",
                "fixture_is_not_production_evidence": True,
            },
        )
        return {"task": task, "created": created, "read_only": False}

    @router.get("/research/runs/{run_id}")
    def get_research_run(run_id: str, account_id: str, store=Depends(get_store)) -> dict[str, Any]:
        _require_account(store, account_id)
        with store._connect() as db:
            row = db.execute("SELECT * FROM research_runs WHERE run_id=? AND account_id=?", (run_id, account_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="RESEARCH_RUN_NOT_FOUND")
        return _research_run_row(row) | {"read_only": True}

    @router.get("/research/qualifications")
    def list_qualifications(account_id: str, limit: int = 100, store=Depends(get_store)) -> dict[str, Any]:
        _require_account(store, account_id)
        with store._connect() as db:
            rows = db.execute("SELECT * FROM qualification_history WHERE account_id=? ORDER BY created_at DESC, qualification_id DESC LIMIT ?", (account_id, max(1, min(limit, 500)))).fetchall()
        return {"qualifications": [_row(row) for row in rows], "read_only": True}

    @router.get("/strategies/{strategy_id}/qualification")
    def strategy_qualification(strategy_id: str, account_id: str, limit: int = 100, store=Depends(get_store)) -> dict[str, Any]:
        _require_account(store, account_id)
        normalized_strategy = strategy_id.strip()
        with store._connect() as db:
            rows = db.execute(
                """SELECT * FROM qualification_history
                   WHERE account_id=? AND strategy_id=?
                   ORDER BY created_at DESC, qualification_id DESC LIMIT ?""",
                (account_id, normalized_strategy, max(1, min(limit, 500))),
            ).fetchall()
        return {"account_id": account_id, "strategy_id": normalized_strategy, "qualifications": [_row(row) for row in rows], "read_only": True}

    @router.post("/research/runs")
    def create_research_run(body: ResearchRunBody, store=Depends(get_store)) -> dict[str, Any]:
        scope = _require_account(store, body.account_id)
        start = body.window_start.astimezone(timezone.utc) if body.window_start else None
        end = body.window_end.astimezone(timezone.utc) if body.window_end else None
        if start and end and end <= start:
            raise HTTPException(status_code=422, detail="window_end must be after window_start")
        if end and end > _now():
            raise HTTPException(status_code=422, detail="future research window is not allowed")
        effective_parameters = dict(body.parameters) if body.parameters else None
        try:
            spec = get_strategy_spec(body.strategy_id.strip(), effective_parameters)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"STRATEGY_SPEC_INVALID: {exc}") from exc
        strategy_version = body.strategy_version or spec.code_version
        dataset_id = body.dataset_id or f"bars:{scope['venue']}:{body.symbol.strip().upper()}:{body.timeframe.strip().lower()}"
        config = body.model_dump(mode="json")
        config.pop("idempotency_key", None)
        strategy_class = STRATEGIES[body.strategy_id.strip()]
        effective_config_parameters = dict(strategy_class(effective_parameters).params)
        trial_parameter_sets = [effective_config_parameters]
        trial_specs: list[tuple[dict[str, Any], str]] = []
        for perturbation in body.parameter_perturbations:
            if not isinstance(perturbation, dict):
                raise HTTPException(status_code=422, detail="STRATEGY_SPEC_INVALID: parameter perturbation must be an object")
            candidate = {**effective_config_parameters, **perturbation}
            try:
                trial = strategy_class(candidate)
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"STRATEGY_SPEC_INVALID: {exc}") from exc
            trial_parameter_sets.append(dict(trial.params))
        for parameters in trial_parameter_sets:
            trial_specs.append((parameters, get_strategy_spec(body.strategy_id.strip(), parameters).params_hash))
        config.update(
            {
                "account_id": body.account_id,
                "venue": scope["venue"],
                "mode": scope["mode"],
                "strategy_id": body.strategy_id.strip(),
                "strategy_version": strategy_version,
                "symbol": body.symbol.strip().upper(),
                "timeframe": body.timeframe.strip().lower(),
                "dataset_id": dataset_id,
                "parameters": effective_config_parameters,
            }
        )
        key = _task_key("research", body.idempotency_key, config)
        run_id = f"run_{_hash({'key': key, 'config': config})[:28]}"
        config["run_id"] = run_id
        task, created = _get_or_create_task(store, task_type="RESEARCH_RUN", idempotency_key=key, config=config, status="RUNNING")
        if not created:
            with store._connect() as db:
                existing_run = db.execute("SELECT * FROM research_runs WHERE account_id=? AND run_id=?", (body.account_id, run_id)).fetchone()
            return {"task": task, "run": _research_run_row(existing_run) if existing_run else None, "created": False, "read_only": False}

        # The deterministic run id is placed into the config before the task
        # is created, so a retry resolves the same immutable identity.
        now = _now().isoformat()
        with store._connect() as db:
            db.execute(
                """INSERT INTO research_runs(
                    run_id, dataset_id, strategy_id, strategy_version, params_hash,
                    config_json, status, result_json, created_at, updated_at,
                    account_id, venue, mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
                (run_id, dataset_id, body.strategy_id.strip(), strategy_version, spec.params_hash, _json(config), "RUNNING", now, now, body.account_id, scope["venue"], scope["mode"]),
            )
            for ordinal, (trial_parameters, trial_params_hash) in enumerate(trial_specs):
                trial_id = f"trial_{_hash({'run_id': run_id, 'params_hash': trial_params_hash})[:28]}"
                db.execute(
                    """INSERT OR IGNORE INTO experiment_trials(
                        trial_id, run_id, ordinal, params_json, params_hash,
                        status, metrics_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'REGISTERED', NULL, ?)""",
                    (trial_id, run_id, ordinal, _json(trial_parameters), trial_params_hash, now),
                )

        with store._connect() as db:
            fill_count = int(db.execute("SELECT COUNT(*) FROM trade_fills WHERE account_id=? AND venue=? AND mode=?", (body.account_id, scope["venue"], scope["mode"])).fetchone()[0])
        missing = [] if fill_count else ["authoritative_execution_fills"]
        evidence = _freeze_research_evidence(store, run_id=run_id, dataset_id=dataset_id, config=config, missing=missing)
        config["evidence_bundle_id"] = evidence["bundle_id"]
        if not fill_count:
            result = {
                "run_id": run_id,
                "status": "NOT_RUN_NO_AUTHORITATIVE_FILLS",
                "evidence_status": "EVIDENCE_INSUFFICIENT",
                "account_id": body.account_id,
                "venue": scope["venue"],
                "mode": scope["mode"],
                "strategy_id": body.strategy_id.strip(),
                "strategy_version": strategy_version,
                "params_hash": spec.params_hash,
                "dataset_id": dataset_id,
                "authoritative_fill_count": 0,
                "input_type": "AUTHORITATIVE_EXECUTION_LEDGER_ONLY",
                "evidence_bundle_id": evidence["bundle_id"],
                "qualification_status": "NOT_QUALIFIED_NO_AUTHORITATIVE_FILLS",
                "fixture_is_not_production_evidence": True,
            }
            status = "NOT_RUN_NO_AUTHORITATIVE_FILLS"
        else:
            try:
                service_result = TraderCapabilityService(store).evaluate_stored_strategy(
                    account_id=body.account_id,
                    strategy_id=body.strategy_id.strip(),
                    strategy_version=strategy_version,
                    symbol=body.symbol.strip().upper(),
                    mode=scope["mode"],
                    venue=scope["venue"],
                    timeframe=body.timeframe.strip().lower(),
                    window_start=start,
                    window_end=end,
                    train_fraction=body.train_fraction,
                    min_samples=body.min_samples,
                    parameter_perturbations=body.parameter_perturbations,
                    parameters=effective_parameters,
                )
                result = dict(service_result)
                result.update({"run_id": run_id, "params_hash": spec.params_hash, "dataset_id": dataset_id, "evidence_bundle_id": evidence["bundle_id"], "input_type": "AUTHORITATIVE_EXECUTION_LEDGER_ONLY"})
                result["qualification_status"] = "NOT_QUALIFIED_DSR_PBO_NOT_CONFIGURED"
                status = str(result.get("status") or "EVIDENCE_INSUFFICIENT")
            except TraderCapabilityError as exc:
                result = {"run_id": run_id, "status": "FAILED", "error_code": exc.code, "error": exc.message, "evidence_bundle_id": evidence["bundle_id"]}
                status = "FAILED"
            except Exception as exc:
                result = {"run_id": run_id, "status": "FAILED", "error_code": "RESEARCH_EXECUTION_ERROR", "error": f"{type(exc).__name__}: {exc}"[:240], "evidence_bundle_id": evidence["bundle_id"]}
                status = "FAILED"

        qualification_status = str(result.get("qualification_status") or "NOT_QUALIFIED_DSR_PBO_NOT_CONFIGURED")
        with store._connect() as db:
            db.execute("UPDATE research_runs SET config_json=?, status=?, result_json=?, updated_at=? WHERE run_id=? AND account_id=?", (_json(config), status, _json(result), _now().isoformat(), run_id, body.account_id))
            db.execute(
                """INSERT OR REPLACE INTO qualification_history(
                    qualification_id, strategy_id, strategy_version, params_hash,
                    dataset_id, status, reason_codes_json, evidence_refs_json,
                    metrics_json, created_at, account_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"qual_{run_id}", body.strategy_id.strip(), strategy_version, spec.params_hash, dataset_id, qualification_status, _json([qualification_status]), _json([f"research_run:{run_id}", evidence["bundle_id"]]), _json(result.get("full_sample") or result), _now().isoformat(), body.account_id),
            )
        task = _update_task(store, task["task_id"], status=status, result=result)
        with store._connect() as db:
            run_row = db.execute("SELECT * FROM research_runs WHERE run_id=? AND account_id=?", (run_id, body.account_id)).fetchone()
        return {"task": task, "run": _research_run_row(run_row) if run_row else result, "created": True, "read_only": False}

    @router.post("/research/runs/{run_id}/cancel")
    def cancel_research_run(run_id: str, account_id: str, store=Depends(get_store)) -> dict[str, Any]:
        scope = _require_account(store, account_id)
        with store._connect() as db:
            row = db.execute("SELECT * FROM research_runs WHERE run_id=? AND account_id=?", (run_id, account_id)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="RESEARCH_RUN_NOT_FOUND")
            task = db.execute(
                """SELECT * FROM institutional_tasks
                   WHERE task_type='RESEARCH_RUN' AND account_id=? AND venue=? AND mode=?
                     AND config_json LIKE ? ORDER BY created_at DESC LIMIT 1""",
                (account_id, scope["venue"], scope["mode"], f"%{run_id}%"),
            ).fetchone()
            db.execute("UPDATE research_runs SET status='CANCEL_REQUESTED', updated_at=? WHERE run_id=? AND account_id=? AND status IN ('RUNNING','QUEUED')", (_now().isoformat(), run_id, account_id))
            if task is not None:
                db.execute("UPDATE institutional_tasks SET status='CANCEL_REQUESTED', cancel_requested=1, updated_at=? WHERE task_id=? AND status IN ('RUNNING','QUEUED')", (_now().isoformat(), task["task_id"]))
        return {"run_id": run_id, "status": "CANCEL_REQUESTED", "cancellation": "COOPERATIVE", "read_only": False}

    @router.get("/executions/timeline")
    @router.get("/execution/timeline")
    def execution_timeline(
        account_id: str,
        mode: str | None = None,
        venue: str | None = None,
        limit: int = 500,
        store=Depends(get_store),
    ) -> dict[str, Any]:
        scope = _require_account(store, account_id)
        if mode and mode.upper() != scope["mode"] or venue and venue.lower() != scope["venue"]:
            raise HTTPException(status_code=422, detail="ACCOUNT_EXECUTION_SCOPE_MISMATCH")
        with store._connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM execution_timeline WHERE account_id=? AND mode=? AND venue=? ORDER BY event_at, event_id LIMIT ?", (account_id, scope["mode"], scope["venue"], max(1, min(limit, 2000)))).fetchall()]
        projection = build_execution_ledger_projection(store, account_id, mode=scope["mode"], venue=scope["venue"], page=1, page_size=min(500, max(1, limit)))
        if not rows:
            rows = [{"event_id": item.get("record_id"), "account_id": account_id, "mode": scope["mode"], "venue": scope["venue"], "position_id": item.get("position_id"), "event_type": item.get("economic_role"), "event_at": item.get("event_at"), "source": "execution_ledger_projection", "payload": item} for item in projection.get("execution_records", [])]
        return {"account_id": account_id, "mode": scope["mode"], "venue": scope["venue"], "events": rows, "projection_summary": projection.get("summary"), "authoritative_source": "trade_fills", "read_only": True}

    @router.get("/risk/summary")
    def risk_summary(account_id: str, store=Depends(get_store)) -> dict[str, Any]:
        scope = _require_account(store, account_id)
        projection = build_execution_ledger_projection(store, account_id, mode=scope["mode"], venue=scope["venue"], page=1, page_size=500)
        ledger = AccountLedger(store)
        risk = RiskEngine(ledger).get_risk_summary(account_id, now=_now())
        positions = projection.get("positions") or []
        exposures = build_exposure_snapshots(positions)
        equity = risk.get("net_equity")
        if scope["mode"] == "TESTNET" and scope["venue"] == "gate":
            try:
                from core.trading.gate_account_truth import GateAccountTruthService
                truth = GateAccountTruthService(store).latest(account_id)
            except Exception:
                truth = None
            def remote_number(key: str) -> float | None:
                value = truth.get(key) if truth else None
                if value is None or isinstance(value, bool):
                    return None
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    return None
                return parsed if math.isfinite(parsed) else None

            remote_equity = remote_number("equity")
            remote_available_margin = remote_number("available_margin")
            remote_used_margin = remote_number("used_margin")
            remote_available = bool(
                truth
                and str(truth.get("status") or "").upper() == "AVAILABLE"
                and remote_equity is not None
                and remote_available_margin is not None
                and remote_used_margin is not None
            )
            if remote_available:
                equity = remote_equity
            else:
                # The local ledger may contain a seeded or historical mirror;
                # it is not Gate TestNet account equity.
                equity = None
                risk = dict(risk)
                for key in (
                    "net_equity",
                    "max_portfolio_risk_budget",
                    "cash",
                    "allocated_margin",
                    "reserved_risk",
                    "max_portfolio_risk",
                    "daily_loss",
                    "daily_loss_limit",
                ):
                    risk[key] = None
                risk["account_truth_status"] = "UNAVAILABLE"
            if remote_available and equity is not None:
                risk = dict(risk)
                risk["net_equity"] = equity
                risk["cash"] = str(remote_available_margin)
                risk["allocated_margin"] = str(remote_used_margin)
                risk["account_truth_status"] = "AVAILABLE"
        pressure = cluster_pressure(exposures, equity=equity, max_fraction=risk.get("risk_limits", {}).get("max_cluster_risk", "0.005"))
        cap_val = {
            "status": "UNKNOWN",
            "capacity_quantity": None,
            "reason": "ORDERBOOK_DEPTH_NOT_REPORTED",
        }
        tca_val = {
            "status": "UNKNOWN",
            "filled_quantity": None,
            "notional": None,
            "average_fill_price": None,
            "implementation_shortfall": None,
            "fees": None,
            "slippage_cost": None,
            "reason": "ARRIVAL_PRICE_AND_SCOPED_FILLS_NOT_REPORTED",
        }
        return {
            "account_id": account_id,
            "mode": scope["mode"],
            "venue": scope["venue"],
            "risk": risk,
            "exposures": [item.to_dict() for item in exposures],
            "cluster_pressure": pressure,
            "correlation_status": "UNKNOWN",
            "capacity": cap_val,
            "tca": tca_val,
            "read_only": True,
        }

    @router.get("/ai/evidence")
    def ai_evidence(account_id: str, bundle_id: str | None = None, store=Depends(get_store)) -> dict[str, Any]:
        _require_account(store, account_id)
        with store._connect() as db:
            if bundle_id:
                rows = db.execute("SELECT * FROM evidence_bundles WHERE bundle_id=?", (bundle_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM evidence_bundles ORDER BY frozen_at DESC LIMIT 100").fetchall()
            cycles = db.execute("SELECT cycle_id, payload_json, created_at FROM ai_led_cycles WHERE account_id=? ORDER BY created_at DESC LIMIT 100", (account_id,)).fetchall()
        bundles = []
        for item in rows:
            payload = dict(item)
            frozen_payload = _object(payload.pop("payload_json", "{}"))
            nested_config = _object(frozen_payload.get("config"))
            prompt_inputs = _object(frozen_payload.get("prompt_inputs"))
            bundle_account = frozen_payload.get("account_id") or nested_config.get("account_id") or prompt_inputs.get("account_id")
            if bundle_account and str(bundle_account) != account_id:
                continue
            # A bundle without an explicit account is only exposed when it is
            # linked by one of this account's durable AI cycles.
            linked_cycle_ids = {str(cycle["payload_json"]) for cycle in cycles}
            if not bundle_account and not any(str(item["bundle_id"]) in raw for raw in linked_cycle_ids):
                continue
            payload["payload"] = frozen_payload
            payload["missing"] = _array(payload.pop("missing_json", "[]"))
            bundles.append(payload)
        cycle_items = []
        for item in cycles:
            payload = _object(item["payload_json"])
            cycle_items.append({"cycle_id": item["cycle_id"], "created_at": item["created_at"], "evidence_bundle_id": payload.get("evidence_bundle_id"), "evidence_status": payload.get("evidence_status"), "model_digest": payload.get("model_digest"), "model_digest_status": payload.get("model_digest_status"), "evidence_refs": payload.get("evidence_refs", [])})
        return {"account_id": account_id, "bundles": bundles, "ai_cycles": cycle_items, "read_only": True}

    return router


__all__ = ["DataBackfillBody", "ResearchRunBody", "router_for"]
