"""Sidecar-owned bounded runtime for explicit crypto monitoring.

The existing :class:`~core.monitoring.MonitoringService` is intentionally a
single-cycle engine.  This module owns the lifecycle around that engine: it
keeps only enabled policies in a bounded public WebSocket, periodically runs
the REST/backfill cycle, wakes on closed 15m bars, and publishes durable alert
events to local subscribers.  Construction and application startup are side
effect free; a user action (or an explicitly authorized resume) is required
before the worker starts.
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Protocol

from .monitoring import MonitoringRunResult, MonitoringService
from .providers.base import Bar
from .realtime import BinanceRealtimeStream, RealtimeConnectionState
from .storage import SQLiteStore


MONITORING_RUNTIME_CONTRACT_VERSION = "monitoring_runtime_v1"
_PERSISTED_STATE_KEY = "monitoring_runtime"


class RuntimeStream(Protocol):
    symbols: tuple[str, ...]

    def run_forever(
        self,
        *,
        on_bar: Callable[[str, Bar, bool], None],
        on_state: Callable[[RealtimeConnectionState], None] | None = None,
    ) -> None:
        ...

    def stop(self) -> None:
        ...


StreamFactory = Callable[[tuple[str, ...]], RuntimeStream]


def _iso(value: datetime) -> str:
    point = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return point.astimezone(timezone.utc).isoformat()


class MonitoringRuntime:
    """Explicit, bounded, restart-safe monitoring worker.

    ``start`` is the only method that creates worker/stream threads.  The
    ``resume`` argument is used by the sidecar startup hook and is accepted
    only when a previous explicit user start was durably recorded.  This
    prevents a true resume preference from becoming an implicit first scan.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore,
        service: MonitoringService,
        stream_factory: StreamFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        max_symbols: int | None = None,
        poll_interval_seconds: float = 30.0,
        max_backoff_seconds: float = 120.0,
        stream_join_timeout_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.service = service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_symbols = max(1, min(int(max_symbols or service.max_symbols), 50))
        self.poll_interval_seconds = max(0.05, min(float(poll_interval_seconds), 900.0))
        self.max_backoff_seconds = max(0.1, min(float(max_backoff_seconds), 900.0))
        self.stream_join_timeout_seconds = max(0.1, min(float(stream_join_timeout_seconds), 10.0))
        self.stream_factory = stream_factory or self._default_stream_factory

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._stream: RuntimeStream | None = None
        self._stream_worker: threading.Thread | None = None
        self._subscribers: set[queue.Queue[dict[str, object]]] = set()
        self._seen_alert_ids: set[str] = {
            str(item.get("alert_id"))
            for item in store.list_alerts(limit=100)
            if item.get("alert_id")
        }
        persisted = store.get_scheduler_state(_PERSISTED_STATE_KEY)
        self._resume_eligible = bool(persisted.get("resume_eligible")) if isinstance(persisted, dict) else False
        self._status: dict[str, object] = {
            "contract_version": MONITORING_RUNTIME_CONTRACT_VERSION,
            "state": "stopped",
            "active": False,
            "worker_alive": False,
            "active_symbols": [],
            "max_symbols": self.max_symbols,
            "resource": {"max_symbols": self.max_symbols, "active_symbols": 0, "bounded": True},
            "stream": {"status": "stopped", "provider": "binance_public_ws", "symbols": [], "timeframe": "15m", "reconnect_count": 0, "last_message_at": None, "last_error": None, "public_only": True},
            "run_count": 0,
            "last_cycle_at": None,
            "last_cycle_status": None,
            "last_error": None,
            "consecutive_failures": 0,
            "retry_after_at": None,
            "started_at": None,
            "transition_at": _iso(self.clock()),
            "resume_eligible": self._resume_eligible,
            "last_reason": "startup_no_scan",
        }
        self._persist_locked()

    def _default_stream_factory(self, symbols: tuple[str, ...]) -> RuntimeStream:
        return BinanceRealtimeStream(symbols=symbols, timeframe="15m", max_symbols=self.max_symbols)

    @staticmethod
    def _active_state(state: str) -> bool:
        return state in {"starting", "running", "degraded", "backoff"}

    def _persist_locked(self) -> None:
        payload = {
            "resume_eligible": self._resume_eligible,
            "last_state": self._status.get("state"),
            "last_transition_at": self._status.get("transition_at"),
            "last_reason": self._status.get("last_reason"),
        }
        try:
            self.store.set_scheduler_state(_PERSISTED_STATE_KEY, payload, updated_at=str(self._status["transition_at"]))
        except Exception:
            # Runtime status must remain observable even if the local evidence
            # database is temporarily read-only or unavailable.
            pass

    def _publish_locked(self, event: dict[str, object]) -> None:
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.put_nowait(dict(event))
            except queue.Full:
                # A slow UI subscriber must never block the monitoring worker.
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(dict(event))
                except (queue.Empty, queue.Full):
                    pass

    def _set_status(self, state: str, *, reason: str | None = None, error: str | None = None, **changes: object) -> None:
        with self._lock:
            now = _iso(self.clock())
            self._status.update(changes)
            self._status["state"] = state
            self._status["active"] = self._active_state(state)
            self._status["worker_alive"] = bool(self._worker and self._worker.is_alive())
            self._status["transition_at"] = now
            if reason is not None:
                self._status["last_reason"] = reason
            if error is not None:
                self._status["last_error"] = error[:240]
            elif state in {"running", "paused", "stopped"}:
                self._status["last_error"] = None
            self._status["resume_eligible"] = self._resume_eligible
            self._status["resource"] = {
                "max_symbols": self.max_symbols,
                "active_symbols": len(self._status.get("active_symbols") or []),
                "bounded": True,
            }
            self._persist_locked()
            self._publish_locked({"type": "runtime.status", "contract_version": MONITORING_RUNTIME_CONTRACT_VERSION, "runtime": self._status.copy()})

    def status(self) -> dict[str, object]:
        with self._lock:
            result = dict(self._status)
            result["active_symbols"] = list(self._status.get("active_symbols") or [])
            result["resource"] = dict(self._status.get("resource") or {})
            result["stream"] = dict(self._status.get("stream") or {})
            result["resume_eligible"] = self._resume_eligible
            result["worker_alive"] = bool(self._worker and self._worker.is_alive())
            result["active"] = self._active_state(str(result.get("state")))
            return result

    def subscribe(self) -> queue.Queue[dict[str, object]]:
        subscriber: queue.Queue[dict[str, object]] = queue.Queue(maxsize=128)
        with self._lock:
            self._subscribers.add(subscriber)
            subscriber.put_nowait({"type": "runtime.status", "contract_version": MONITORING_RUNTIME_CONTRACT_VERSION, "runtime": self.status()})
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, object]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def start(self, *, resume: bool = False, user_initiated: bool = True) -> dict[str, object]:
        with self._lock:
            current = str(self._status.get("state"))
            if resume and not user_initiated and not self._resume_eligible:
                self._status["last_reason"] = "resume_not_authorized"
                self._status["transition_at"] = _iso(self.clock())
                self._persist_locked()
                return self.status()
            if user_initiated:
                self._resume_eligible = True
            if self._active_state(current) and self._worker and self._worker.is_alive():
                self._status["last_reason"] = "already_active"
                return self.status()
            if self._worker and self._worker.is_alive():
                self._set_status(
                    "degraded",
                    reason="previous_worker_still_stopping",
                    error="a previous monitoring worker is still shutting down",
                )
                return self.status()
            self._stop_event.clear()
            self._wake_event.clear()
            self._status["started_at"] = self._status.get("started_at") or _iso(self.clock())
            self._status["last_reason"] = "resume" if resume else "explicit_start"
            self._status["last_error"] = None
            self._status["consecutive_failures"] = 0
            self._set_status("starting", reason=str(self._status["last_reason"]), resume_eligible=self._resume_eligible)
            self._worker = threading.Thread(target=self._worker_loop, name="aima-monitoring-runtime", daemon=True)
            self._worker.start()
            return self.status()

    def pause(self) -> dict[str, object]:
        with self._lock:
            if not self._active_state(str(self._status.get("state"))):
                self._set_status("paused", reason="already_inactive")
                return self.status()
            self._stop_event.set()
            self._wake_event.set()
            stream = self._stream
            worker = self._worker
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self.stream_join_timeout_seconds + 1.0)
        self._clear_stream()
        self._set_status("paused", reason="explicit_pause", active_symbols=[], retry_after_at=None)
        return self.status()

    def stop(self, *, clear_resume: bool = True) -> dict[str, object]:
        with self._lock:
            self._stop_event.set()
            self._wake_event.set()
            stream = self._stream
            worker = self._worker
            if clear_resume:
                self._resume_eligible = False
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self.stream_join_timeout_seconds + 1.0)
        self._clear_stream()
        self._set_status("stopped", reason="explicit_stop" if clear_resume else "sidecar_shutdown", active_symbols=[], retry_after_at=None)
        return self.status()

    def _clear_stream(self) -> None:
        with self._lock:
            self._stream = None
            self._stream_worker = None
            stream_payload = dict(self._status.get("stream") or {})
            stream_payload.update({"status": "stopped", "symbols": [], "last_error": None})
            self._status["stream"] = stream_payload

    def _enabled_symbols(self) -> tuple[str, ...]:
        symbols: list[str] = []
        for row in self.store.list_monitoring_policies(enabled=True):
            value = str(row.get("instrument_id") or "").strip().upper()
            if value and value not in symbols:
                symbols.append(value)
            if len(symbols) >= self.max_symbols:
                break
        return tuple(symbols)

    def _stream_state(self, state: RealtimeConnectionState) -> None:
        payload = state.to_dict()
        with self._lock:
            self._status["stream"] = payload
            current = str(self._status.get("state"))
            if state.status in {"degraded", "backfilling"} and self._active_state(current):
                next_state = "degraded"
            elif state.status in {"connected", "connecting"} and current in {"starting", "degraded", "backoff"}:
                next_state = "running"
            else:
                next_state = current
        if next_state != current:
            self._set_status(next_state, reason=f"stream_{state.status}", error=state.last_error)
        else:
            with self._lock:
                self._publish_locked({"type": "runtime.status", "contract_version": MONITORING_RUNTIME_CONTRACT_VERSION, "runtime": self.status()})

    def _on_stream_bar(self, symbol: str, bar: Bar, is_closed: bool) -> None:
        normalized = str(symbol).strip().upper()
        with self._lock:
            active_symbols = set(self._status.get("active_symbols") or [])
        if normalized not in active_symbols:
            return
        try:
            self.store.upsert_market_bars(
                normalized,
                "15m",
                [bar],
                provider="binance_public_ws",
                data_as_of=bar.timestamp,
                now=self.clock(),
            )
        except Exception as exc:
            self._set_status("degraded", reason="stream_bar_persist_failed", error=str(exc))
            return
        if is_closed:
            self._wake_event.set()

    def _replace_stream(self, symbols: tuple[str, ...]) -> None:
        with self._lock:
            previous = self._stream
            previous_worker = self._stream_worker
            self._stream = None
            self._stream_worker = None
        if previous is not None:
            try:
                previous.stop()
            except Exception:
                pass
        if previous_worker is not None and previous_worker is not threading.current_thread():
            previous_worker.join(timeout=self.stream_join_timeout_seconds)
        if not symbols:
            with self._lock:
                self._status["stream"] = {"status": "stopped", "provider": "binance_public_ws", "symbols": [], "timeframe": "15m", "reconnect_count": 0, "last_message_at": None, "last_error": None, "public_only": True}
            return
        try:
            stream = self.stream_factory(symbols)
        except Exception as exc:
            with self._lock:
                self._status["stream"] = {
                    "status": "degraded",
                    "provider": "binance_public_ws",
                    "symbols": list(symbols),
                    "timeframe": "15m",
                    "reconnect_count": 0,
                    "last_message_at": None,
                    "last_error": str(exc)[:240],
                    "public_only": True,
                }
            self._set_status("degraded", reason="stream_create_failed", error=str(exc), active_symbols=list(symbols), retry_after_at=_iso(self.clock()))
            return
        worker = threading.Thread(
            target=stream.run_forever,
            kwargs={"on_bar": self._on_stream_bar, "on_state": self._stream_state},
            name="aima-monitoring-public-ws",
            daemon=True,
        )
        with self._lock:
            self._stream = stream
            self._stream_worker = worker
            self._status["stream"] = {
                "status": "starting",
                "provider": "binance_public_ws",
                "symbols": list(symbols),
                "timeframe": "15m",
                "reconnect_count": 0,
                "last_message_at": None,
                "last_error": None,
                "public_only": True,
            }
        worker.start()

    def _publish_new_alerts(self) -> None:
        policies = {
            str(item.get("instrument_id")): item
            for item in self.store.list_monitoring_policies(enabled=True)
        }
        for alert in reversed(self.store.list_alerts(limit=100)):
            alert_id = str(alert.get("alert_id") or "")
            if not alert_id or alert_id in self._seen_alert_ids:
                continue
            self._seen_alert_ids.add(alert_id)
            symbol = str(alert.get("symbol") or "").strip().upper()
            policy = policies.get(symbol, {})
            in_app = bool(policy.get("notify_in_app", True))
            native = bool(policy.get("notify_native_notification", True))
            route = f"/assets/{symbol}" if symbol else "/alerts"
            event = {
                "type": "alert.created",
                "contract_version": MONITORING_RUNTIME_CONTRACT_VERSION,
                "event_id": f"monitoring-alert:{alert_id}",
                "alert_id": alert_id,
                "alert": alert,
                "route": route,
                "notification": {"in_app": in_app, "native": native},
            }
            with self._lock:
                self._publish_locked(event)

    def _run_cycle(self, symbols: tuple[str, ...]) -> MonitoringRunResult:
        return self.service.run(symbols=symbols, now=self.clock())

    def _worker_loop(self) -> None:
        failure_count = 0
        try:
            while not self._stop_event.is_set():
                symbols = self._enabled_symbols()
                with self._lock:
                    stream_payload = self._status.get("stream") or {}
                    current_stream_symbols = tuple(str(item) for item in stream_payload.get("symbols", []))
                    stream = self._stream
                    stream_worker = self._stream_worker
                    stream_needs_restart = bool(symbols) and (stream is None or stream_worker is None or not stream_worker.is_alive())
                if symbols != current_stream_symbols or stream_needs_restart:
                    self._replace_stream(symbols)
                with self._lock:
                    stream_status = str((self._status.get("stream") or {}).get("status") or "stopped")
                runtime_state = "degraded" if stream_status in {"degraded", "backfilling"} else "running"
                self._set_status(runtime_state, reason="worker_active", active_symbols=list(symbols), retry_after_at=None)
                if not symbols:
                    self._wake_event.wait(self.poll_interval_seconds)
                    self._wake_event.clear()
                    continue
                try:
                    result = self._run_cycle(symbols)
                    with self._lock:
                        self._status["run_count"] = int(self._status.get("run_count") or 0) + 1
                    if result.status == "COMPLETED_WITH_ERRORS":
                        error_items = [
                            str(item.get("error_code") or item.get("status") or "monitoring_cycle_error")
                            for item in result.items
                            if isinstance(item, dict)
                        ]
                        raise RuntimeError("monitoring cycle degraded: " + ",".join(error_items[:3]))
                    failure_count = 0
                    self._set_status(
                        "running",
                        reason="cycle_complete",
                        last_cycle_at=_iso(result.as_of),
                        last_cycle_status=result.status,
                        consecutive_failures=0,
                        retry_after_at=None,
                    )
                    self._publish_new_alerts()
                    self._wake_event.wait(self.poll_interval_seconds)
                    self._wake_event.clear()
                except Exception as exc:
                    failure_count += 1
                    delay = min(self.max_backoff_seconds, max(0.1, 2.0 ** min(failure_count - 1, 8)))
                    retry_at = _iso(self.clock() + timedelta(seconds=delay))
                    self._set_status(
                        "degraded",
                        reason="cycle_failed_backoff",
                        error=str(exc),
                        consecutive_failures=failure_count,
                        retry_after_at=retry_at,
                        last_cycle_status="ERROR",
                    )
                    self._set_status("backoff", reason="bounded_retry", error=str(exc), retry_after_at=retry_at)
                    self._wake_event.wait(min(delay, self.max_backoff_seconds))
                    self._wake_event.clear()
        finally:
            with self._lock:
                stream = self._stream
                stream_worker = self._stream_worker
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
            if stream_worker is not None and stream_worker is not threading.current_thread():
                stream_worker.join(timeout=self.stream_join_timeout_seconds)
            with self._lock:
                self._worker = None
                self._stream = None
                self._stream_worker = None
                self._status["worker_alive"] = False


__all__ = ["MONITORING_RUNTIME_CONTRACT_VERSION", "MonitoringRuntime", "RuntimeStream", "StreamFactory"]
