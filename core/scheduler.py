"""Bounded local Watchlist scan runtime for the Phase 5 scheduler foundation.

The runtime is deliberately independent from FastAPI routes.  It is opt-in,
serial for model analysis, and injectable at every clock/resource/analysis
boundary so tests never need an uncontrolled background worker or a live model.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, Callable, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .analysis_service import AnalysisError, AnalysisService
from .alerts import AlertReconciler
from .instruments import AssetType, Instrument, TradingHours
from .providers import build_default_provider
from .settlement import SettlementService
from .storage import SQLiteStore


DEFAULT_SCAN_TIMEFRAME = "1h"
CONTEXT_CACHE_VERSION = "context_cache_v1"
CONTEXT_CACHE_TTL_SECONDS = 300
CONTEXT_CACHE_CAPACITY = 128
RESOURCE_BACKOFF_BASE_SECONDS = 30
RESOURCE_BACKOFF_MAX_SECONDS = 900
MODEL_ANALYSIS_CONCURRENCY_LIMIT = 1
EQUITY_MARKET_OPEN = time(9, 30)
EQUITY_MARKET_CLOSE = time(16, 0)
DEFAULT_CONTEXT_CAPABILITY: dict[str, object] = {
    "cache_version": CONTEXT_CACHE_VERSION,
    "market_context": "analysis_service",
    "news": "analysis_service",
    "time_policy": "phase2",
}


_SCHEDULER_LEASE_LOCK = RLock()
_SCHEDULER_LEASES: dict[str, object] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return as_utc(value).isoformat()


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (occurrence - 1) * 7)


def _us_eastern_dst_utc(value: datetime) -> bool:
    """Small stdlib fallback for Windows hosts without an IANA tzdata bundle."""

    current = as_utc(value)
    start_day = _nth_weekday(current.year, 3, 6, 2)
    end_day = _nth_weekday(current.year, 11, 6, 1)
    start = datetime.combine(start_day, time(7, 0), tzinfo=timezone.utc)
    end = datetime.combine(end_day, time(6, 0), tzinfo=timezone.utc)
    return start <= current < end


def _local_time(current: datetime, timezone_name: str) -> datetime:
    try:
        return as_utc(current).astimezone(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError):
        if timezone_name == "America/New_York":
            offset = timedelta(hours=-4 if _us_eastern_dst_utc(current) else -5)
            return (as_utc(current) + offset).replace(tzinfo=timezone(offset))
        if timezone_name == "UTC":
            return as_utc(current)
        raise


def build_context_cache_key(
    *,
    symbol: str,
    timeframe: str,
    as_of: datetime | str,
    provider: str,
    context_capability: dict[str, object],
) -> str:
    """Build a versioned key with every context identity dimension included."""

    normalized_as_of = iso_timestamp(as_of) if isinstance(as_of, datetime) else str(as_of)
    payload = {
        "version": CONTEXT_CACHE_VERSION,
        "symbol": str(symbol).strip().upper(),
        "timeframe": str(timeframe).strip().lower(),
        "as_of": normalized_as_of,
        "provider": str(provider).strip(),
        "context_capability": context_capability,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class SessionDecision:
    allowed: bool
    state: str
    reason: str
    policy: str
    local_time: str | None
    capability: str
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "state": self.state,
            "reason": self.reason,
            "policy": self.policy,
            "local_time": self.local_time,
            "capability": self.capability,
            "limitations": list(self.limitations),
        }


def evaluate_market_session(
    instrument: Instrument,
    now: datetime,
    policy: str,
) -> SessionDecision:
    """Apply deterministic weekday/session rules with instrument timezone.

    The V1 boundary intentionally has no exchange holiday calendar.  Regular
    equities use 09:30-16:00 in their stored timezone; Crypto remains 24/7.
    """

    normalized_policy = str(policy).strip().lower()
    if normalized_policy not in {"market_hours", "always"}:
        raise ValueError("session policy must be market_hours or always")
    current = as_utc(now)
    try:
        local = _local_time(current, instrument.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return SessionDecision(
            False,
            "TIMEZONE_UNAVAILABLE",
            "instrument timezone is unavailable",
            normalized_policy,
            None,
            "timezone_unavailable",
            ("holiday_calendar_not_implemented",),
        )

    local_text = local.isoformat()
    if instrument.asset_type is AssetType.CRYPTO or instrument.trading_hours is TradingHours.AROUND_THE_CLOCK:
        return SessionDecision(
            True,
            "OPEN",
            "crypto_24_7",
            normalized_policy,
            local_text,
            "crypto_24_7",
        )
    if normalized_policy == "always":
        return SessionDecision(
            True,
            "OPEN",
            "explicit_always_policy",
            normalized_policy,
            local_text,
            "explicit_override",
            ("holiday_calendar_not_implemented",),
        )
    if local.weekday() >= 5:
        return SessionDecision(
            False,
            "WEEKEND",
            "equity_weekend_closed",
            normalized_policy,
            local_text,
            "weekday_hours_only",
            ("holiday_calendar_not_implemented",),
        )
    local_clock = local.timetz().replace(tzinfo=None)
    if not (EQUITY_MARKET_OPEN <= local_clock < EQUITY_MARKET_CLOSE):
        return SessionDecision(
            False,
            "MARKET_CLOSED",
            "equity_outside_regular_session",
            normalized_policy,
            local_text,
            "weekday_hours_only",
            ("holiday_calendar_not_implemented",),
        )
    return SessionDecision(
        True,
        "OPEN",
        "equity_regular_session",
        normalized_policy,
        local_text,
        "weekday_hours_only",
        ("holiday_calendar_not_implemented",),
    )


@dataclass(frozen=True, slots=True)
class ResourceProbeResult:
    available: bool
    reason: str = "ready"
    retry_after_seconds: int | None = None
    capability: str = "local_resource_probe"
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
            "capability": self.capability,
            "details": dict(self.details),
        }


class ResourceProbe(Protocol):
    def probe(self) -> ResourceProbeResult:
        ...


class LocalResourceProbe:
    """Bounded, read-only GPU guard with an injectable command runner.

    ``nvidia-smi`` is deliberately called without a shell and with a small
    timeout.  A missing or failed probe is unavailable rather than an assumed
    healthy GPU, so the scheduler can safely back off on CPU-only hosts.
    """

    def __init__(
        self,
        *,
        command_runner: Callable[[list[str], float], str] | None = None,
        timeout_seconds: float = 2.0,
        minimum_free_memory_mb: int = 1024,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("resource probe timeout must be positive")
        if minimum_free_memory_mb < 0:
            raise ValueError("minimum free GPU memory cannot be negative")
        self.command_runner = command_runner or self._run_command
        self.timeout_seconds = float(timeout_seconds)
        self.minimum_free_memory_mb = int(minimum_free_memory_mb)

    @staticmethod
    def _run_command(command: list[str], timeout_seconds: float) -> str:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit status {completed.returncode}"
            raise RuntimeError(detail)
        return completed.stdout

    @staticmethod
    def _csv_rows(raw: str) -> list[list[str]]:
        rows: list[list[str]] = []
        for row in csv.reader(str(raw).splitlines()):
            values = [value.strip() for value in row]
            if not values or not any(values):
                continue
            if values[0].lower() in {"pid", "process id", "memory.free"}:
                continue
            rows.append(values)
        return rows

    @classmethod
    def _processes(cls, raw: str) -> list[dict[str, object]]:
        processes: list[dict[str, object]] = []
        for values in cls._csv_rows(raw):
            if len(values) == 1 and "no running process" in values[0].lower():
                continue
            if len(values) < 2:
                raise ValueError("nvidia-smi process output is malformed")
            process: dict[str, object] = {
                "pid": values[0],
                "name": values[1],
            }
            if len(values) >= 3:
                process["used_gpu_memory_mb"] = values[2]
            processes.append(process)
        return processes

    @classmethod
    def _free_memory_mb(cls, raw: str) -> list[float]:
        values: list[float] = []
        for row in cls._csv_rows(raw):
            value = row[0].lower().replace("mib", "").replace("mb", "").replace(",", "").strip()
            try:
                values.append(float(value))
            except ValueError as exc:
                raise ValueError("nvidia-smi memory output is malformed") from exc
        if not values:
            raise ValueError("nvidia-smi returned no GPU memory values")
        return values

    @staticmethod
    def _competition_reason(process_name: str) -> str | None:
        normalized = process_name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if "ollama" in normalized:
            return None
        if "comfyui" in normalized:
            return "comfyui_gpu_competition"
        if normalized.startswith("python") or "python" in normalized:
            return "python_gpu_worker_competition"
        if "blender" in normalized:
            return "blender_gpu_competition"
        return "gpu_competition"

    def _probe_unavailable(self, detail: str) -> ResourceProbeResult:
        return ResourceProbeResult(
            False,
            reason="gpu_probe_unavailable",
            retry_after_seconds=RESOURCE_BACKOFF_BASE_SECONDS,
            capability="probe_unavailable",
            details={"error": detail},
        )

    def probe(self) -> ResourceProbeResult:
        override = os.environ.get("AI_MARKET_ANALYST_RESOURCE_BLOCK", "").strip()
        if override:
            return ResourceProbeResult(
                False,
                reason=override,
                retry_after_seconds=RESOURCE_BACKOFF_BASE_SECONDS,
                capability="local_environment_override",
                details={"source": "AI_MARKET_ANALYST_RESOURCE_BLOCK"},
            )
        process_command = [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ]
        memory_command = [
            "nvidia-smi",
            "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
        ]
        try:
            processes = self._processes(self.command_runner(process_command, self.timeout_seconds))
            free_memory = self._free_memory_mb(self.command_runner(memory_command, self.timeout_seconds))
        except Exception as exc:  # bounded command/parsing failure is a safe unavailable result
            return self._probe_unavailable(str(exc))

        for process in processes:
            reason = self._competition_reason(str(process["name"]))
            if reason is not None:
                return ResourceProbeResult(
                    False,
                    reason=reason,
                    retry_after_seconds=RESOURCE_BACKOFF_BASE_SECONDS,
                    capability="nvidia_smi",
                    details={"processes": processes, "free_memory_mb": free_memory},
                )
        available_memory = max(free_memory)
        details = {"processes": processes, "free_memory_mb": free_memory}
        if available_memory < self.minimum_free_memory_mb:
            return ResourceProbeResult(
                False,
                reason="gpu_low_free_memory",
                retry_after_seconds=RESOURCE_BACKOFF_BASE_SECONDS,
                capability="nvidia_smi",
                details={**details, "minimum_free_memory_mb": self.minimum_free_memory_mb},
            )
        return ResourceProbeResult(True, capability="nvidia_smi", details=details)


@dataclass(frozen=True, slots=True)
class CacheEntry:
    cache_key: str
    prediction_id: str | None
    created_at: datetime
    expires_at: datetime
    last_accessed_at: datetime
    hit_count: int
    metadata: dict[str, object]

    def to_metadata(self) -> dict[str, object]:
        return {
            **self.metadata,
            "cache_key": self.cache_key,
            "cache_version": CONTEXT_CACHE_VERSION,
            "created_at": iso_timestamp(self.created_at),
            "expires_at": iso_timestamp(self.expires_at),
            "last_accessed_at": iso_timestamp(self.last_accessed_at),
            "hit_count": self.hit_count,
            "prediction_id": self.prediction_id,
        }


class ContextCache:
    """Bounded in-memory scheduler cache; persistence stores metadata only."""

    def __init__(
        self,
        *,
        ttl_seconds: int = CONTEXT_CACHE_TTL_SECONDS,
        capacity: int = CONTEXT_CACHE_CAPACITY,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if ttl_seconds <= 0 or capacity <= 0:
            raise ValueError("cache TTL and capacity must be positive")
        self.ttl_seconds = int(ttl_seconds)
        self.capacity = int(capacity)
        self.clock = clock
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._lock = RLock()

    def _now(self, value: datetime | None = None) -> datetime:
        return as_utc(value or self.clock())

    def _purge(self, now: datetime) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)

    def get(self, cache_key: str, *, now: datetime | None = None) -> CacheEntry | None:
        current = self._now(now)
        with self._lock:
            self._purge(current)
            entry = self._entries.get(cache_key)
            if entry is None:
                self._misses += 1
                return None
            updated = CacheEntry(
                cache_key=entry.cache_key,
                prediction_id=entry.prediction_id,
                created_at=entry.created_at,
                expires_at=entry.expires_at,
                last_accessed_at=current,
                hit_count=entry.hit_count + 1,
                metadata=entry.metadata,
            )
            self._entries[cache_key] = updated
            self._entries.move_to_end(cache_key)
            self._hits += 1
            return updated

    def put(
        self,
        cache_key: str,
        *,
        prediction_id: str | None,
        metadata: dict[str, object],
        now: datetime | None = None,
    ) -> CacheEntry:
        current = self._now(now)
        entry = CacheEntry(
            cache_key=cache_key,
            prediction_id=prediction_id,
            created_at=current,
            expires_at=current + timedelta(seconds=self.ttl_seconds),
            last_accessed_at=current,
            hit_count=0,
            metadata=dict(metadata),
        )
        with self._lock:
            self._entries[cache_key] = entry
            self._entries.move_to_end(cache_key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
        return entry

    def stats(self, *, now: datetime | None = None) -> dict[str, object]:
        current = self._now(now)
        with self._lock:
            self._purge(current)
            return {
                "version": CONTEXT_CACHE_VERSION,
                "entries": len(self._entries),
                "capacity": self.capacity,
                "ttl_seconds": self.ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "restart_behavior": "metadata_only_cold_restart",
            }


@dataclass
class BackoffState:
    base_seconds: int = RESOURCE_BACKOFF_BASE_SECONDS
    max_seconds: int = RESOURCE_BACKOFF_MAX_SECONDS
    attempts: int = 0
    reason: str | None = None
    next_retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.base_seconds <= 0 or self.max_seconds < self.base_seconds:
            raise ValueError("backoff bounds are invalid")

    def fail(self, reason: str, now: datetime, retry_after_seconds: int | None = None) -> int:
        self.attempts += 1
        exponential = self.base_seconds * (2 ** min(self.attempts - 1, 20))
        suggested = max(0, int(retry_after_seconds or 0))
        delay = min(self.max_seconds, max(exponential, suggested))
        self.reason = str(reason)
        self.next_retry_at = as_utc(now) + timedelta(seconds=delay)
        return delay

    def clear(self) -> None:
        self.attempts = 0
        self.reason = None
        self.next_retry_at = None

    def active(self, now: datetime) -> bool:
        return self.next_retry_at is not None and self.next_retry_at > as_utc(now)

    def to_dict(self, *, now: datetime) -> dict[str, object]:
        current = as_utc(now)
        remaining = max(0, int((self.next_retry_at - current).total_seconds())) if self.next_retry_at else 0
        return {
            "active": self.active(current),
            "attempts": self.attempts,
            "reason": self.reason,
            "next_retry_at": iso_timestamp(self.next_retry_at) if self.next_retry_at else None,
            "remaining_seconds": remaining,
            "base_seconds": self.base_seconds,
            "max_seconds": self.max_seconds,
        }


@dataclass(frozen=True, slots=True)
class ScanExecution:
    status: str = "COMPLETED"
    prediction_id: str | None = None
    action: str | None = None
    cache_key: str | None = None
    cache_status: str = "not_checked"
    cache_metadata: dict[str, object] = field(default_factory=dict)
    provider: str | None = None
    as_of: str | None = None
    resource_reason: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


class ScanAnalysisExecutor(Protocol):
    def execute(
        self,
        instrument: Instrument,
        *,
        timeframe: str,
        analysis_time: datetime,
        context_capability: dict[str, object],
        cache: ContextCache,
    ) -> ScanExecution:
        ...


class DefaultScanAnalysisExecutor:
    """Adapt the existing AnalysisService without changing manual Analyze semantics."""

    def __init__(self, service_or_factory: AnalysisService | Callable[[], AnalysisService], store: SQLiteStore) -> None:
        self._service_or_factory = service_or_factory
        self._store = store
        self._service: AnalysisService | None = service_or_factory if isinstance(service_or_factory, AnalysisService) else None

    def _service_instance(self) -> AnalysisService:
        if self._service is None:
            self._service = self._service_or_factory()  # type: ignore[operator]
        return self._service

    def execute(
        self,
        instrument: Instrument,
        *,
        timeframe: str,
        analysis_time: datetime,
        context_capability: dict[str, object],
        cache: ContextCache,
    ) -> ScanExecution:
        service = self._service_instance()
        snapshot = service.market_snapshot(instrument, timeframe=timeframe, limit=120, snapshot_time=analysis_time)
        provider = snapshot.bundle.snapshot.provider
        as_of = iso_timestamp(snapshot.data_as_of)
        cache_key = build_context_cache_key(
            symbol=instrument.symbol,
            timeframe=timeframe,
            as_of=as_of,
            provider=provider,
            context_capability=context_capability,
        )
        cached = cache.get(cache_key, now=analysis_time)
        if cached is not None and cached.prediction_id:
            payload = self._store.load_prediction_payload(cached.prediction_id)
            if payload is not None:
                return ScanExecution(
                    prediction_id=cached.prediction_id,
                    action=str(payload.get("action")) if payload.get("action") is not None else None,
                    cache_key=cache_key,
                    cache_status="hit",
                    cache_metadata=cached.to_metadata(),
                    provider=provider,
                    as_of=as_of,
                )

        result = service.analyze(
            instrument,
            timeframe=timeframe,
            limit=120,
            analysis_time=analysis_time,
            context_capabilities=context_capability,
        )
        actual_provider = result.bundle.snapshot.provider
        actual_as_of = iso_timestamp(result.data_as_of)
        actual_key = build_context_cache_key(
            symbol=instrument.symbol,
            timeframe=timeframe,
            as_of=actual_as_of,
            provider=actual_provider,
            context_capability=context_capability,
        )
        entry = cache.put(
            actual_key,
            prediction_id=result.signal.prediction_id,
            metadata={
                "symbol": instrument.symbol,
                "timeframe": timeframe,
                "as_of": actual_as_of,
                "provider": actual_provider,
                "context_capability": context_capability,
            },
            now=analysis_time,
        )
        model_status = result.model_status
        resource_reason = None
        if not bool(model_status.get("available")) and model_status.get("error_code"):
            resource_reason = str(model_status["error_code"])
        return ScanExecution(
            prediction_id=result.signal.prediction_id,
            action=result.signal.action.value,
            cache_key=actual_key,
            cache_status="miss",
            cache_metadata=entry.to_metadata(),
            provider=actual_provider,
            as_of=actual_as_of,
            resource_reason=resource_reason,
        )


class SchedulerRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, *, context: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})


def _scheduler_lease_key(store: SQLiteStore) -> str:
    raw_path = str(store.path)
    if raw_path == ":memory:":
        return f":memory:{id(store)}"
    return os.path.normcase(str(Path(raw_path).expanduser().resolve()))


class LocalSchedulerRuntime:
    """Explicitly controlled local Watchlist scan runtime."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        analysis_executor: ScanAnalysisExecutor,
        resource_probe: ResourceProbe | Callable[[], ResourceProbeResult] | None = None,
        clock: Callable[[], datetime] = utc_now,
        cache_ttl_seconds: int = CONTEXT_CACHE_TTL_SECONDS,
        cache_capacity: int = CONTEXT_CACHE_CAPACITY,
        backoff_base_seconds: int = RESOURCE_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: int = RESOURCE_BACKOFF_MAX_SECONDS,
        timeframe: str = DEFAULT_SCAN_TIMEFRAME,
        settlement_service: SettlementService | None = None,
        alert_reconciler: AlertReconciler | None = None,
    ) -> None:
        self.store = store
        self.analysis_executor = analysis_executor
        self.resource_probe = resource_probe or LocalResourceProbe()
        self.clock = clock
        self.timeframe = timeframe
        self.settlement_service = settlement_service or SettlementService(
            store=store,
            provider_factory=build_default_provider,
            clock=clock,
        )
        self.alert_reconciler = alert_reconciler or AlertReconciler(store=store, clock=clock)
        self.cache = ContextCache(
            ttl_seconds=cache_ttl_seconds,
            capacity=cache_capacity,
            clock=clock,
        )
        self.backoff = BackoffState(backoff_base_seconds, backoff_max_seconds)
        self._lock = RLock()
        self._run_lock = threading.Lock()
        self._thread: Thread | None = None
        self._stop_event: Event | None = None
        self._lease_key = _scheduler_lease_key(store)
        self._lease_token = object()
        self._lease_held = False
        self._active_run_id: str | None = None
        self._last_resource = ResourceProbeResult(True, capability="not_probed")
        self._last_error: dict[str, object] | None = None
        self.store.initialize()
        self.recovered_runs = self.store.recover_scheduler_runs(recovered_at=iso_timestamp(self._now()))
        self._restore_backoff()

    def _now(self) -> datetime:
        return as_utc(self.clock())

    def _restore_backoff(self) -> None:
        persisted = self.store.get_scheduler_state("backoff")
        if not isinstance(persisted, dict):
            return
        try:
            self.backoff.attempts = max(0, int(persisted.get("attempts", 0)))
            self.backoff.reason = str(persisted["reason"]) if persisted.get("reason") else None
            raw_next = persisted.get("next_retry_at")
            self.backoff.next_retry_at = datetime.fromisoformat(str(raw_next)) if raw_next else None
            if self.backoff.next_retry_at is not None:
                self.backoff.next_retry_at = as_utc(self.backoff.next_retry_at)
        except (TypeError, ValueError):
            self.backoff.clear()

    def _settings(self) -> dict[str, object]:
        values = {item["key"]: item["value"] for item in self.store.list_app_settings()}
        return {
            "enabled": bool(values["scheduler.enabled"]),
            "interval_seconds": int(values["scheduler.interval_seconds"]),
            "configured_concurrency": int(values["scheduler.concurrency"]),
            "session_policy": str(values["scheduler.session_policy"]),
            "effective_concurrency": MODEL_ANALYSIS_CONCURRENCY_LIMIT,
            "timeframe": self.timeframe,
        }

    def _probe(self) -> ResourceProbeResult:
        try:
            probe = self.resource_probe
            result = probe() if callable(probe) and not hasattr(probe, "probe") else probe.probe()  # type: ignore[union-attr]
            if isinstance(result, ResourceProbeResult):
                return result
            if isinstance(result, bool):
                return ResourceProbeResult(result, reason="ready" if result else "resource_unavailable")
            if isinstance(result, dict):
                return ResourceProbeResult(
                    bool(result.get("available", False)),
                    reason=str(result.get("reason", "resource_unavailable")),
                    retry_after_seconds=int(result["retry_after_seconds"]) if result.get("retry_after_seconds") is not None else None,
                    capability=str(result.get("capability", "injected_resource_probe")),
                    details=dict(result.get("details", {})) if isinstance(result.get("details"), dict) else {},
                )
            return ResourceProbeResult(False, reason="invalid_resource_probe_result", capability="probe_error")
        except Exception as exc:  # pragma: no cover - defensive boundary for injected probes
            return ResourceProbeResult(False, reason="resource_probe_error", capability="probe_error", details={"error": str(exc)})

    def _thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _acquire_lease(self) -> None:
        with _SCHEDULER_LEASE_LOCK:
            owner = _SCHEDULER_LEASES.get(self._lease_key)
            if owner is not None and owner is not self._lease_token:
                raise SchedulerRuntimeError(
                    "SCHEDULER_LEASE_HELD",
                    "another scheduler runtime holds this SQLite path",
                    context={"scope": "process_sqlite_path"},
                )
            _SCHEDULER_LEASES[self._lease_key] = self._lease_token
            self._lease_held = True

    def _release_lease(self) -> None:
        with _SCHEDULER_LEASE_LOCK:
            if _SCHEDULER_LEASES.get(self._lease_key) is self._lease_token:
                _SCHEDULER_LEASES.pop(self._lease_key, None)
            self._lease_held = False

    def _backoff_context(self) -> dict[str, object]:
        state = self.backoff.to_dict(now=self._now())
        return {
            "reason": state["reason"],
            "next_retry_at": state["next_retry_at"],
            "remaining_seconds": state["remaining_seconds"],
            "remaining": state["remaining_seconds"],
            "attempts": state["attempts"],
        }

    def _raise_if_backoff_active(self) -> None:
        if self.backoff.active(self._now()):
            raise SchedulerRuntimeError(
                "SCHEDULER_BACKOFF_ACTIVE",
                "scheduler backoff is active; retry after the bounded delay",
                context=self._backoff_context(),
            )

    def _wait_for_backoff(self, stop_event: Event) -> bool:
        while self.backoff.active(self._now()):
            if stop_event.is_set():
                return False
            remaining = int(self.backoff.to_dict(now=self._now())["remaining_seconds"])
            if stop_event.wait(min(1.0, max(0.05, float(remaining)))):
                return False
        return not stop_event.is_set()

    def status(self) -> dict[str, object]:
        settings = self._settings()
        latest = self.store.list_scheduler_runs(limit=1)
        latest_run = latest[0] if latest else None
        cache_stats = self.cache.stats(now=self._now())
        persisted_cache = self.store.list_scheduler_cache_entries(limit=500)
        last_settlement = self.store.get_scheduler_state("last_settlement")
        last_performance_refresh = self.store.get_scheduler_state("last_performance_refresh")
        last_alert_reconciliation = self.store.get_scheduler_state("last_alert_reconciliation")
        running = self._thread_alive()
        if running:
            state = "running"
        elif not settings["enabled"]:
            state = "disabled"
        else:
            state = "stopped"
        next_run_at = latest_run.get("next_run_at") if latest_run else None
        return {
            "state": state,
            "enabled": settings["enabled"],
            "running": running,
            "thread_alive": running,
            "active_run_id": self._active_run_id,
            "interval_seconds": settings["interval_seconds"],
            "configured_concurrency": settings["configured_concurrency"],
            "effective_concurrency": settings["effective_concurrency"],
            "session_policy": settings["session_policy"],
            "timeframe": settings["timeframe"],
            "last_run": latest_run,
            "next_run_at": next_run_at,
            "last_error": self._last_error,
            "resource": self._last_resource.to_dict(),
            "backoff": self.backoff.to_dict(now=self._now()),
            "cache": {
                **cache_stats,
                "persisted_metadata_entries": len(persisted_cache),
                "persisted_hit_count": sum(int(item["hit_count"]) for item in persisted_cache),
            },
            "settlement": last_settlement,
            "performance_refresh": last_performance_refresh,
            "alerts": last_alert_reconciliation,
            "capabilities": {
                "runtime": "local_thread_explicit_lifecycle",
                "model_analysis_concurrency": MODEL_ANALYSIS_CONCURRENCY_LIMIT,
                "session_policy": "weekday_hours_only_no_holiday_calendar",
                "crypto_24_7": True,
                "resource_guard": self._last_resource.capability,
                "cache": "scheduler_only_metadata_persists_cold_restart",
                "real_orders": False,
                "alerts": True,
                "alert_policy": "alert_policy_v1",
                "alert_mode": "local_observability_only",
                "alert_reconciliation": "after_settlement_and_scan",
                "outcome_settlement": True,
                "outcome_settlement_mode": "live_point_in_time_before_model_scan",
                "performance_refresh": "versioned_live_snapshot_no_calibration_mutation",
            },
            "recovered_runs": self.recovered_runs,
        }

    def history(self, *, limit: int = 20) -> dict[str, object]:
        runs = self.store.list_scheduler_runs(limit=limit)
        return {
            "runs": [
                run | {"items": self.store.list_scheduler_items(str(run["run_id"]))}
                for run in runs
            ],
            "status": self.status(),
        }

    def start(self) -> dict[str, object]:
        settings = self._settings()
        if not settings["enabled"]:
            raise SchedulerRuntimeError("SCHEDULER_DISABLED", "scheduler.enabled must be true before start")
        with self._lock:
            if self._thread_alive():
                raise SchedulerRuntimeError("SCHEDULER_ALREADY_RUNNING", "scheduler is already running")
            self._acquire_lease()
            try:
                self._stop_event = Event()
                self._thread = Thread(target=self._loop, args=(self._stop_event,), name="ai-market-analyst-scheduler", daemon=True)
                self._thread.start()
            except Exception:
                self._thread = None
                self._stop_event = None
                self._release_lease()
                raise
        return self.status()

    def stop(self, *, timeout_seconds: float = 5.0) -> dict[str, object]:
        with self._lock:
            event = self._stop_event
            thread = self._thread
            if event is not None:
                event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, timeout_seconds))
        if thread is not None and thread.is_alive():
            raise SchedulerRuntimeError("SCHEDULER_STOP_TIMEOUT", "scheduler did not stop within the bounded timeout")
        with self._lock:
            if self._thread is thread:
                self._thread = None
                self._stop_event = None
            if not self._thread_alive():
                self._release_lease()
        return self.status()

    def close(self) -> dict[str, object]:
        return self.stop()

    def run_once(self) -> dict[str, object]:
        settings = self._settings()
        if not settings["enabled"]:
            raise SchedulerRuntimeError("SCHEDULER_DISABLED", "scheduler.enabled must be true before run-once")
        if self._thread_alive():
            raise SchedulerRuntimeError("SCHEDULER_ALREADY_RUNNING", "stop the background scheduler before run-once")
        if not self._run_lock.acquire(blocking=False):
            raise SchedulerRuntimeError("SCHEDULER_RUN_IN_PROGRESS", "another scheduler run is already in progress")
        try:
            self._acquire_lease()
            try:
                self._raise_if_backoff_active()
                return self._run_once(trigger="manual")
            finally:
                self._release_lease()
        finally:
            self._run_lock.release()

    def _loop(self, stop_event: Event) -> None:
        try:
            while not stop_event.is_set():
                if not self._wait_for_backoff(stop_event):
                    return
                if not self._settings()["enabled"]:
                    return
                if stop_event.is_set():
                    return
                if self._run_lock.acquire(blocking=False):
                    try:
                        self._run_once(trigger="scheduled", stop_event=stop_event)
                    except Exception as exc:  # pragma: no cover - defensive thread boundary
                        self._last_error = {"code": "SCHEDULER_LOOP_ERROR", "message": str(exc)}
                    finally:
                        self._run_lock.release()
                settings = self._settings()
                wait_seconds = max(int(settings["interval_seconds"]), self.backoff.to_dict(now=self._now())["remaining_seconds"])
                stop_event.wait(wait_seconds)
        finally:
            self._release_lease()

    def _run_once(self, *, trigger: str, stop_event: Event | None = None) -> dict[str, object]:
        started = self._now()
        settings = self._settings()
        run_id = f"scan-{uuid4().hex[:16]}"
        self._active_run_id = run_id
        self.store.create_scheduler_run(
            run_id=run_id,
            trigger=trigger,
            started_at=iso_timestamp(started),
            settings=settings,
        )
        counts: dict[str, int] = {
            "planned": 0,
            "completed": 0,
            "reused": 0,
            "wait": 0,
            "skipped_session": 0,
            "skipped_resource": 0,
            "errors": 0,
            "interrupted": 0,
            "settlement_planned": 0,
            "settlement_settled": 0,
            "settlement_pending": 0,
            "settlement_wait": 0,
            "settlement_errors": 0,
            "settlement_provider_errors": 0,
            "settlement_interrupted": 0,
            "settlement_idempotent": 0,
            "settlement_provider_fetches": 0,
            "settlement_provider_reused": 0,
            "performance_refreshes": 0,
            "performance_reused": 0,
            "alerts_created": 0,
            "alerts_deduped": 0,
            "alert_errors": 0,
            "alert_prediction_created": 0,
            "alert_outcome_created": 0,
            "alert_radar_created": 0,
            "alert_news_event_created": 0,
            "alert_operational_created": 0,
            "alert_pruned": 0,
        }
        backoff_triggered = False
        stop_requested = False
        context_capability = dict(DEFAULT_CONTEXT_CAPABILITY)
        try:
            try:
                settlement_result = self.settlement_service.run_once(
                    run_id=run_id,
                    as_of=started,
                    stop_event=stop_event,
                )
                settlement_counts = settlement_result.get("counts", {})
                if isinstance(settlement_counts, dict):
                    for key, value in settlement_counts.items():
                        target = f"settlement_{key}"
                        if target in counts:
                            counts[target] = int(value)
                    counts["performance_refreshes"] = int(settlement_counts.get("performance_refreshes", 0))
                    counts["performance_reused"] = int(settlement_counts.get("performance_reused", 0))
                if settlement_result.get("status") == "INTERRUPTED":
                    stop_requested = True
            except Exception as exc:  # isolate settlement stage from scan items
                counts["settlement_errors"] += 1
                self._last_error = {"code": "SETTLEMENT_STAGE_ERROR", "message": str(exc)}
            entries = self.store.list_watchlist_entries()
            counts["planned"] = len(entries)
            for index, entry in enumerate(entries):
                symbol = str(entry["symbol"])
                item_id = f"{run_id}:{symbol}"
                if stop_event is not None and stop_event.is_set():
                    stop_requested = True
                    for remaining_entry in entries[index:]:
                        remaining_symbol = str(remaining_entry["symbol"])
                        remaining_item_id = f"{run_id}:{remaining_symbol}"
                        self.store.create_scheduler_item(
                            item_id=remaining_item_id,
                            run_id=run_id,
                            symbol=remaining_symbol,
                            timeframe=self.timeframe,
                            status="INTERRUPTED",
                        )
                        self.store.update_scheduler_item(
                            remaining_item_id,
                            finished_at=iso_timestamp(self._now()),
                            skip_reason="STOP_REQUESTED",
                            error_code="SCHEDULER_STOP_REQUESTED",
                            error_detail="scheduler stop requested before item execution",
                        )
                        counts["interrupted"] += 1
                    break
                self.store.create_scheduler_item(
                    item_id=item_id,
                    run_id=run_id,
                    symbol=symbol,
                    timeframe=self.timeframe,
                )
                item_started = self._now()
                self.store.update_scheduler_item(item_id, status="RUNNING", started_at=iso_timestamp(item_started))
                try:
                    if stop_event is not None and stop_event.is_set():
                        stop_requested = True
                        counts["interrupted"] += 1
                        self.store.update_scheduler_item(
                            item_id,
                            status="INTERRUPTED",
                            finished_at=iso_timestamp(self._now()),
                            skip_reason="STOP_REQUESTED",
                            error_code="SCHEDULER_STOP_REQUESTED",
                            error_detail="scheduler stop requested before item execution",
                        )
                        continue
                    instrument = self.store.resolve_instrument(symbol)
                    session = evaluate_market_session(instrument, item_started, str(settings["session_policy"]))
                    if not session.allowed:
                        counts["skipped_session"] += 1
                        self.store.update_scheduler_item(
                            item_id,
                            status="SKIPPED",
                            finished_at=iso_timestamp(self._now()),
                            session_state=session.state,
                            skip_reason=session.reason,
                            cache_status="not_checked",
                        )
                        continue
                    resource = self._probe()
                    self._last_resource = resource
                    if not resource.available:
                        counts["skipped_resource"] += 1
                        self.backoff.fail(resource.reason, item_started, resource.retry_after_seconds)
                        backoff_triggered = True
                        self.store.update_scheduler_item(
                            item_id,
                            status="SKIPPED",
                            finished_at=iso_timestamp(self._now()),
                            session_state=session.state,
                            resource_reason=resource.reason,
                            cache_status="not_checked",
                            capability=resource.to_dict(),
                            retry_after_at=(
                                iso_timestamp(item_started + timedelta(seconds=resource.retry_after_seconds))
                                if resource.retry_after_seconds is not None
                                else None
                            ),
                        )
                        continue
                    if stop_event is not None and stop_event.is_set():
                        stop_requested = True
                        counts["interrupted"] += 1
                        self.store.update_scheduler_item(
                            item_id,
                            status="INTERRUPTED",
                            finished_at=iso_timestamp(self._now()),
                            session_state=session.state,
                            skip_reason="STOP_REQUESTED",
                            error_code="SCHEDULER_STOP_REQUESTED",
                            error_detail="scheduler stop requested before analysis execution",
                        )
                        continue
                    execution = self.analysis_executor.execute(
                        instrument,
                        timeframe=self.timeframe,
                        analysis_time=item_started,
                        context_capability=context_capability,
                        cache=self.cache,
                    )
                    if execution.cache_key and execution.cache_metadata:
                        metadata = execution.cache_metadata
                        self.store.upsert_scheduler_cache_entry(
                            cache_key=execution.cache_key,
                            cache_version=str(metadata.get("cache_version", CONTEXT_CACHE_VERSION)),
                            symbol=str(metadata.get("symbol", symbol)),
                            timeframe=str(metadata.get("timeframe", self.timeframe)),
                            as_of=str(metadata.get("as_of", execution.as_of or iso_timestamp(item_started))),
                            provider=str(metadata.get("provider", execution.provider or "unknown")),
                            context_capability=dict(metadata.get("context_capability", context_capability)),
                            created_at=str(metadata.get("created_at", iso_timestamp(item_started))),
                            expires_at=str(metadata.get("expires_at", iso_timestamp(item_started + timedelta(seconds=self.cache.ttl_seconds)))),
                            prediction_id=execution.prediction_id,
                            status="hit" if execution.cache_status == "hit" else "stored",
                        )
                        self.store.prune_scheduler_cache_entries(
                            now=iso_timestamp(self._now()),
                            capacity=max(100, self.cache.capacity * 4),
                        )
                        if execution.cache_status == "hit":
                            self.store.record_scheduler_cache_hit(execution.cache_key, accessed_at=iso_timestamp(self._now()))
                            counts["reused"] += 1
                    counts["completed"] += 1
                    if execution.action == "WAIT":
                        counts["wait"] += 1
                    if execution.resource_reason:
                        self.backoff.fail(execution.resource_reason, item_started)
                        backoff_triggered = True
                    self.store.update_scheduler_item(
                        item_id,
                        status=execution.status,
                        finished_at=iso_timestamp(self._now()),
                        session_state=session.state,
                        resource_reason=execution.resource_reason,
                        cache_key=execution.cache_key,
                        cache_status=execution.cache_status,
                        error_code=execution.error_code,
                        error_detail=execution.error_detail,
                        prediction_id=execution.prediction_id,
                    )
                except AnalysisError as exc:
                    counts["errors"] += 1
                    self.backoff.fail(exc.code, item_started)
                    backoff_triggered = True
                    self._last_error = {"code": exc.code, "message": str(exc), "symbol": symbol}
                    self.store.update_scheduler_item(
                        item_id,
                        status="ERROR",
                        finished_at=iso_timestamp(self._now()),
                        error_code=exc.code,
                        error_detail=str(exc),
                    )
                except Exception as exc:  # isolate one bad Watchlist item
                    counts["errors"] += 1
                    code = str(getattr(exc, "code", "SCAN_ITEM_ERROR"))
                    self.backoff.fail(code, item_started)
                    backoff_triggered = True
                    self._last_error = {"code": code, "message": str(exc), "symbol": symbol}
                    self.store.update_scheduler_item(
                        item_id,
                        status="ERROR",
                        finished_at=iso_timestamp(self._now()),
                        error_code=code,
                        error_detail=str(exc),
                    )
            try:
                alert_result = self.alert_reconciler.run_once(run_id=run_id, as_of=self._now())
                alert_counts = alert_result.get("counts", {})
                if isinstance(alert_counts, dict):
                    counts["alerts_created"] = int(alert_counts.get("created", 0))
                    counts["alerts_deduped"] = int(alert_counts.get("deduped", 0))
                    counts["alert_errors"] = int(alert_counts.get("errors", 0))
                    counts["alert_pruned"] = int(alert_counts.get("pruned", 0))
                    for source_key in ("prediction", "outcome", "radar", "news_event", "operational"):
                        counts[f"alert_{source_key}_created"] = int(alert_counts.get(f"{source_key}_created", 0))
            except Exception as exc:  # alert reconciliation must not corrupt a scan run
                counts["alert_errors"] += 1
                self._last_error = {"code": "ALERT_RECONCILIATION_ERROR", "message": str(exc)}
            if not backoff_triggered:
                self.backoff.clear()
            if stop_event is not None and stop_event.is_set():
                stop_requested = True
            final_status = (
                "INTERRUPTED"
                if stop_requested
                else "COMPLETED_WITH_ERRORS"
                if counts["errors"] or counts["skipped_resource"] or counts["settlement_errors"] or counts["settlement_provider_errors"] or counts["alert_errors"]
                else "COMPLETED"
            )
            finished = self._now()
            next_run = None if stop_requested else finished + timedelta(
                seconds=max(int(settings["interval_seconds"]), self.backoff.to_dict(now=finished)["remaining_seconds"])
            )
            self.store.update_scheduler_run(
                run_id,
                status=final_status,
                finished_at=iso_timestamp(finished),
                next_run_at=iso_timestamp(next_run) if next_run is not None else None,
                counts=counts,
                error_code="SCHEDULER_STOP_REQUESTED" if stop_requested else None,
                error_detail="scheduler stop requested; remaining Watchlist items were not executed" if stop_requested else None,
            )
        except Exception as exc:
            counts["errors"] += 1
            self._last_error = {"code": "SCHEDULER_RUN_ERROR", "message": str(exc)}
            interrupted = stop_event is not None and stop_event.is_set()
            if interrupted:
                counts["interrupted"] += 1
            self.store.update_scheduler_run(
                run_id,
                status="INTERRUPTED" if interrupted else "FAILED",
                finished_at=iso_timestamp(self._now()),
                counts=counts,
                error_code="SCHEDULER_STOP_REQUESTED" if interrupted else "SCHEDULER_RUN_ERROR",
                error_detail="scheduler stop requested while finishing run" if interrupted else str(exc),
            )
        finally:
            self._active_run_id = None
            self.store.set_scheduler_state("last_run_id", run_id, updated_at=iso_timestamp(self._now()))
            self.store.set_scheduler_state("backoff", self.backoff.to_dict(now=self._now()), updated_at=iso_timestamp(self._now()))
            self.store.set_scheduler_state("cache", self.cache.stats(now=self._now()), updated_at=iso_timestamp(self._now()))
        run = self.store.get_scheduler_run(run_id)
        return {
            "run": run,
            "items": self.store.list_scheduler_items(run_id),
            "status": self.status(),
        }
