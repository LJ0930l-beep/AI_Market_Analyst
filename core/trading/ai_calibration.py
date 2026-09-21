"""Closed-bar calibration gate for the AI-led session.

Calibration is a lifecycle prerequisite, not a cosmetic status label.  It
uses only closed 15m bars available at the replay as-of time, makes one
operation-profile call to the configured local Bonsai route, and fails closed
when the sample or a real model digest is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import uuid
from typing import Any, Callable

from ..evidence import model_weight_digest
from ..instruments import read_trading_bars
from ..model_routing import DEFAULT_SMART_MODEL, is_verified_bonsai_receipt
from .institutional_schema import ensure_institutional_trader_schema
from .model_schemas import CALIBRATION_PROFILE_SCHEMA


CALIBRATION_PROFILE_VERSION = "ai_operation_profile_v2"
CALIBRATION_PROMPT_VERSION = "ai_calibration_operation_profile_v2"
DEFAULT_CALIBRATION_BARS = 500
DEFAULT_CALIBRATION_LOOKBACK_DAYS = 30
CALIBRATION_TTL_HOURS = 24


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    elif value:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return point.replace(tzinfo=timezone.utc) if point.tzinfo is None else point.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _closed_bars(store: Any, symbols: tuple[str, ...], *, now: datetime, lookback_days: int, limit: int) -> list[dict[str, Any]]:
    start = now - timedelta(days=lookback_days)
    collected: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            # The newest row is commonly the still-forming 15m candle.  Ask
            # for a buffer before filtering so one partial row cannot turn a
            # complete 500-bar bootstrap into a false NOT_READY result.
            #
            # Pinned to the traded-price identity: the dedupe below keeps one
            # row per bar_start, so an unfiltered read would let a mark-price
            # row win over the traded-price row for the same bar and silently
            # calibrate the model against prices it can never fill at.
            rows = read_trading_bars(
                store, symbol, "15m", limit=max(int(limit) + 64, int(limit) * 2)
            )
        except Exception:
            rows = []
        by_start: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            bar_end = _utc(item.get("bar_end"))
            available = _utc(item.get("available_at") or item.get("data_as_of"))
            bar_start = _utc(item.get("bar_start") or item.get("timestamp"))
            if (
                bar_end is None
                or bar_start is None
                or not bool(item.get("is_closed"))
                or bar_end > now
                or bar_start < start
                or available is None
                or available > now
            ):
                continue
            item["symbol"] = symbol
            key = str(bar_start)
            previous = by_start.get(key)
            if previous is None or (str(item.get("available_at") or ""), str(item.get("revision_id") or "")) >= (str(previous.get("available_at") or ""), str(previous.get("revision_id") or "")):
                by_start[key] = item
        # A calibration replay may span several symbols, but it must never
        # use a gap as if it were a continuous time series.  Keep only the
        # most recent contiguous run for each symbol before merging samples.
        ordered = sorted(by_start.values(), key=lambda row: str(row.get("bar_start")))
        contiguous: list[dict[str, Any]] = []
        for item in ordered:
            if contiguous:
                previous_start = _utc(contiguous[-1].get("bar_start"))
                current_start = _utc(item.get("bar_start"))
                if previous_start is None or current_start is None or current_start - previous_start != timedelta(minutes=15):
                    contiguous = []
            contiguous.append(item)
        collected.extend(contiguous)
    collected.sort(key=lambda row: (str(row.get("bar_end")), str(row.get("symbol")), str(row.get("revision_id"))))
    return collected[-max(1, int(limit)):]


class AICalibrationService:
    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        ensure_schema: bool = True,
    ) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if ensure_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.store._connect() as db:
            ensure_institutional_trader_schema(db)

    def active_profile(self, account_id: str, *, environment: str, now: datetime | None = None) -> dict[str, Any] | None:
        current = now or self.clock()
        point = _utc(current) or datetime.now(timezone.utc)
        with self.store._connect() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_calibration_profiles'"
            ).fetchone()
            if table is None:
                return None
            row = db.execute(
                """SELECT * FROM ai_calibration_profiles
                   WHERE account_id=? AND environment=? AND profile_version=? AND active=1 AND expires_at>?
                   ORDER BY calibrated_at DESC LIMIT 1""",
                (account_id, str(environment).lower(), CALIBRATION_PROFILE_VERSION, _iso(point)),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["profile"] = json.loads(result.pop("profile_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["profile"] = {}
            result.pop("profile_json", None)
        return result

    def latest_run(self, account_id: str, *, environment: str) -> dict[str, Any] | None:
        with self.store._connect() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_calibration_runs'"
            ).fetchone()
            if table is None:
                return None
            row = db.execute(
                "SELECT * FROM ai_calibration_runs WHERE account_id=? AND environment=? ORDER BY started_at DESC LIMIT 1",
                (account_id, str(environment).lower()),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["result"] = json.loads(result.pop("result_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["result"] = {}
            result.pop("result_json", None)
        return result

    def run(
        self,
        *,
        account_id: str,
        provider: str,
        environment: str,
        session_id: str | None,
        symbols: tuple[str, ...],
        model_provider: Any,
        now: datetime | None = None,
        min_bars: int = DEFAULT_CALIBRATION_BARS,
        lookback_days: int = DEFAULT_CALIBRATION_LOOKBACK_DAYS,
    ) -> dict[str, Any]:
        point = _utc(now or self.clock()) or datetime.now(timezone.utc)
        run_id = f"calibration_{uuid.uuid4().hex[:16]}"
        requested = max(1, int(min_bars))
        bars = _closed_bars(self.store, tuple(symbols)[:5], now=point, lookback_days=max(1, int(lookback_days)), limit=requested)
        started = _iso(point)
        with self.store._connect() as db:
            ensure_institutional_trader_schema(db)
            db.execute(
                """INSERT INTO ai_calibration_runs(
                    run_id, account_id, session_id, provider, environment, status,
                    requested_bars, used_bars, started_at, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, account_id, session_id, str(provider or "unknown"), str(environment).lower(), "CALIBRATING", requested, len(bars), started, "{}"),
            )

        if len(bars) < requested:
            return self._finish(run_id, account_id, environment, status="NOT_READY", error_code="CALIBRATION_NOT_READY", result={"required_bars": requested, "used_bars": len(bars), "lookback_days": lookback_days})
        if model_provider is None or not callable(getattr(model_provider, "generate_json", None)):
            return self._finish(run_id, account_id, environment, status="NOT_READY", error_code="SMART_MODEL_UNAVAILABLE", result={"required_bars": requested, "used_bars": len(bars)})

        digest, digest_status = model_weight_digest(model_provider, model_name=DEFAULT_SMART_MODEL)
        if not digest:
            return self._finish(run_id, account_id, environment, status="NOT_READY", error_code="MODEL_DIGEST_UNAVAILABLE", result={"required_bars": requested, "used_bars": len(bars), "digest_status": digest_status})

        summary = self._replay_summary(bars)
        input_hash = _hash({"account_id": account_id, "environment": environment, "bars": [(b.get("symbol"), b.get("bar_end"), b.get("close"), b.get("revision_id")) for b in bars], "profile_version": CALIBRATION_PROFILE_VERSION})
        messages = [
            {"role": "system", "content": "Return JSON only. Produce an operation profile from the supplied closed-bar replay. Do not produce chain-of-thought."},
            {"role": "user", "content": json.dumps({"prompt_version": CALIBRATION_PROMPT_VERSION, "input_hash": input_hash, "replay": summary}, sort_keys=True)},
        ]
        # Calibration is part of an AI-led session gate.  It must use the same
        # Smart model as the final action decision; a provider's fast-model
        # default is never an implicit fallback.
        model_id = DEFAULT_SMART_MODEL
        raw_response: str | None = None
        parse_phase = "INITIAL"
        latency_ms: float | None = None

        def call_model(call_messages: list[dict[str, str]], prompt_version: str) -> tuple[dict[str, Any], dict[str, Any]]:
            nonlocal raw_response, latency_ms, parse_phase
            started_call = datetime.now(timezone.utc)
            response = model_provider.generate_json(
                call_messages,
                model_name=model_id,
                prompt_version=prompt_version,
                input_hash=input_hash,
                temperature=0.0,
                schema=CALIBRATION_PROFILE_SCHEMA,
            )
            decoded = response[0] if isinstance(response, tuple) else response
            metadata = response[2] if isinstance(response, tuple) and len(response) >= 3 and isinstance(response[2], dict) else {}
            if not is_verified_bonsai_receipt(metadata):
                raise ValueError("MODEL_RECEIPT_INVALID")
            raw_response = str(response[1])[:12000] if isinstance(response, tuple) and len(response) >= 2 and response[1] is not None else str(metadata.get("raw_response") or "")[:12000] or None
            latency_ms = float(metadata.get("latency_ms")) if metadata.get("latency_ms") is not None else max(0.0, (datetime.now(timezone.utc) - started_call).total_seconds() * 1000.0)
            if not isinstance(decoded, dict):
                raise ValueError("CALIBRATION_PROFILE_SCHEMA_INVALID")
            required_fields = {"risk_regime", "entry_style", "order_preference", "max_concurrent_positions", "notes_zh"}
            if set(decoded) != required_fields:
                raise ValueError("CALIBRATION_PROFILE_SCHEMA_INVALID")
            if any(not isinstance(decoded[field], str) for field in ("risk_regime", "entry_style", "order_preference", "notes_zh")):
                raise ValueError("CALIBRATION_PROFILE_SCHEMA_INVALID")
            if isinstance(decoded["max_concurrent_positions"], bool) or not isinstance(decoded["max_concurrent_positions"], int):
                raise ValueError("CALIBRATION_PROFILE_SCHEMA_INVALID")
            profile = {
                "risk_regime": decoded["risk_regime"].strip()[:40],
                "entry_style": decoded["entry_style"].strip()[:40],
                "order_preference": decoded["order_preference"].upper(),
                "max_concurrent_positions": int(decoded["max_concurrent_positions"]),
                "notes_zh": decoded["notes_zh"][:320],
                "replay": summary,
            }
            if profile["order_preference"] not in {"MARKET", "LIMIT", "AUTO"} or not 1 <= profile["max_concurrent_positions"] <= 5 or not profile["risk_regime"] or not profile["entry_style"]:
                raise ValueError("CALIBRATION_PROFILE_VALUE_INVALID")
            return profile, metadata

        try:
            try:
                profile, _metadata = call_model(messages, CALIBRATION_PROMPT_VERSION)
            except Exception as first_exc:
                if str(first_exc).split(":", 1)[0] in {
                    "MODEL_RECEIPT_INVALID", "MODEL_RESPONSE_IDENTITY_MISMATCH",
                    "MODEL_NOT_ALLOWED", "MODEL_ENDPOINT_NOT_ALLOWED", "MODEL_UNAVAILABLE",
                    "MODEL_MANIFEST_UNAVAILABLE", "MODEL_INFERENCE_ERROR",
                }:
                    raise
                # Exactly one bounded repair attempt.  A failed repair is a
                # hard calibration failure; it must not become a cached
                # profile or a model-generated WAIT.
                parse_phase = "REPAIR"
                repair_messages = [
                    {"role": "system", "content": "只返回符合给定 JSON Schema 的 JSON 对象；不得增加字段，不得输出解释。"},
                    {"role": "user", "content": json.dumps({"schema": CALIBRATION_PROFILE_SCHEMA, "validation_error": str(first_exc)[:240], "previous_response": raw_response, "replay": summary}, ensure_ascii=False, sort_keys=True)},
                ]
                profile, _metadata = call_model(repair_messages, CALIBRATION_PROMPT_VERSION + "_repair")
        except Exception as exc:
            error_code = str(exc).split(":", 1)[0] or "CALIBRATION_PROFILE_FAILED"
            return self._finish(run_id, account_id, environment, status="NOT_READY", error_code=error_code, result={"required_bars": requested, "used_bars": len(bars), "digest_status": digest_status, "parse_phase": parse_phase, "raw_response": raw_response, "schema_version": "calibration_profile_v1"}, model_id=model_id, model_digest=digest, input_hash=input_hash, parse_phase=parse_phase, latency_ms=latency_ms, raw_response=raw_response)

        profile_key = {"account_id": account_id, "environment": environment, "input_hash": input_hash, "digest": digest}
        profile_id = f"profile_{_hash(profile_key)[:24]}"
        expires = point + timedelta(hours=CALIBRATION_TTL_HOURS)
        with self.store._connect() as db:
            ensure_institutional_trader_schema(db)
            db.execute("UPDATE ai_calibration_profiles SET active=0 WHERE account_id=? AND environment=?", (account_id, str(environment).lower()))
            db.execute(
                """INSERT OR REPLACE INTO ai_calibration_profiles(
                    profile_id, account_id, provider, environment, profile_version,
                    model_id, model_digest, digest_status, input_hash, sample_size,
                    calibrated_at, expires_at, active, profile_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (profile_id, account_id, str(provider or "unknown"), str(environment).lower(), CALIBRATION_PROFILE_VERSION, model_id, digest, digest_status, input_hash, len(bars), started, _iso(expires), json.dumps(profile, ensure_ascii=False, sort_keys=True, allow_nan=False)),
            )
        result = self._finish(run_id, account_id, environment, status="READY", error_code=None, result={"profile_id": profile_id, "sample_size": len(bars), "digest_status": digest_status, "input_hash": input_hash, "parse_phase": parse_phase, "schema_version": "calibration_profile_v1", "model_receipt": {key: _metadata.get(key) for key in ("model_id", "actual_model_id", "model_identity_source", "verified_manifest_model_id")}}, model_id=model_id, model_digest=digest, input_hash=input_hash, parse_phase=parse_phase, latency_ms=latency_ms, raw_response=raw_response)
        result["profile_id"] = profile_id
        result["profile"] = profile
        return result

    def _finish(self, run_id: str, account_id: str, environment: str, *, status: str, error_code: str | None, result: dict[str, Any], model_id: str | None = None, model_digest: str | None = None, input_hash: str | None = None, parse_phase: str | None = None, latency_ms: float | None = None, raw_response: str | None = None) -> dict[str, Any]:
        finished = _iso(_utc(self.clock()) or datetime.now(timezone.utc))
        with self.store._connect() as db:
            db.execute(
                """UPDATE ai_calibration_runs SET status=?, completed_at=?, error_code=?, result_json=?,
                   model_id=COALESCE(?, model_id), model_digest=COALESCE(?, model_digest),
                   input_hash=COALESCE(?, input_hash), parse_phase=COALESCE(?, parse_phase),
                   latency_ms=COALESCE(?, latency_ms), raw_response=COALESCE(?, raw_response),
                   schema_version=COALESCE(?, schema_version) WHERE run_id=? AND account_id=?""",
                (status, finished, error_code, json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False), model_id, model_digest, input_hash, parse_phase, latency_ms, raw_response, result.get("schema_version"), run_id, account_id),
            )
        return {"run_id": run_id, "account_id": account_id, "environment": str(environment).lower(), "status": status, "error_code": error_code, "completed_at": finished, **result}

    @staticmethod
    def _replay_summary(bars: list[dict[str, Any]]) -> dict[str, Any]:
        returns: list[float] = []
        by_symbol: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        for item in bars:
            symbol = str(item.get("symbol") or "").strip().upper()
            bar_end = _utc(item.get("bar_end") or item.get("timestamp"))
            if not symbol or bar_end is None:
                continue
            by_symbol.setdefault(symbol, []).append((bar_end, item))

        # _closed_bars merges symbols into one chronological list.  Returns
        # must be calculated inside each instrument, or a BTC-to-altcoin
        # boundary can look like a multi-million-percent candle.
        for series in by_symbol.values():
            series.sort(key=lambda row: row[0])
            for (previous_end, previous), (current_end, current) in zip(series, series[1:]):
                if current_end - previous_end != timedelta(minutes=15):
                    continue
                try:
                    prior = float(previous["close"])
                    close = float(current["close"])
                    if not math.isfinite(prior) or not math.isfinite(close) or prior <= 0:
                        continue
                    value = (close - prior) / prior
                    if math.isfinite(value):
                        returns.append(value)
                except (KeyError, TypeError, ValueError):
                    continue
        wins = sum(1 for value in returns if value > 0)
        return {
            "sample_size": len(bars),
            "symbol_count": len(by_symbol),
            "return_observations": len(returns),
            "positive_fraction": round(wins / len(returns), 6) if returns else None,
            "mean_return": round(sum(returns) / len(returns), 8) if returns else None,
            "first_bar_end": bars[0].get("bar_end") if bars else None,
            "last_bar_end": bars[-1].get("bar_end") if bars else None,
        }


__all__ = [
    "AICalibrationService",
    "CALIBRATION_PROFILE_VERSION",
    "DEFAULT_CALIBRATION_BARS",
    "DEFAULT_CALIBRATION_LOOKBACK_DAYS",
]
