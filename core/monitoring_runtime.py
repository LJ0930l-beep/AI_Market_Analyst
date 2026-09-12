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
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Protocol

from .monitoring import MonitoringRunResult, MonitoringService
from .providers.base import Bar
from .realtime import BinanceRealtimeStream, RealtimeConnectionState
from .storage import SQLiteStore
from .trading.account_scope import resolve_account_scope


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
        ledger: Any | None = None,
        guardian: Any | None = None,
        session_manager: Any | None = None,
        runtime_lease: Any | None = None,
        execution_gateway: Any | None = None,
    ) -> None:
        self.store = store
        self.service = service
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_symbols = max(1, min(int(max_symbols or getattr(service, "max_symbols", 50)), 50))
        self.poll_interval_seconds = max(0.05, min(float(poll_interval_seconds), 900.0))
        self.max_backoff_seconds = max(0.1, min(float(max_backoff_seconds), 900.0))
        self.stream_join_timeout_seconds = max(0.1, min(float(stream_join_timeout_seconds), 10.0))
        self.stream_factory = stream_factory or self._default_stream_factory

        from .trading.ledger import AccountLedger
        from .trading.position_guardian import PositionGuardian
        from .trading.session_manager import SessionManager, RuntimeLease
        from .trading.ai_session_coordinator import AISessionCoordinator
        from .trading.execution_gateway import ExecutionGateway
        from .trading.trader_capabilities import TraderCapabilityService

        self.ledger = ledger or AccountLedger(self.store)
        self.session_manager = session_manager or SessionManager(self.store)
        self.runtime_lease = runtime_lease or RuntimeLease(self.store)
        self.holder_id = f"runtime_{uuid.uuid4().hex[:8]}"
        self._fencing_token: int | None = None
        self._lease_lost = False
        self._lock = threading.RLock()
        self.execution_gateway = execution_gateway or ExecutionGateway(
            self.store,
            ledger=self.ledger,
            runtime_fence_validator=self._validate_runtime_fence,
        )
        # A runtime owns one gateway for strategy, AI-led, manual test
        # harness, and guardian paths.  A service-created gateway must not
        # become a second un-fenced execution boundary.
        if hasattr(self.execution_gateway, "runtime_fence_validator"):
            self.execution_gateway.runtime_fence_validator = self._validate_runtime_fence
        self.execution_gateway.runtime_lease_holder_id = self.holder_id
        self.execution_gateway.runtime_fencing_token = None
        self.guardian = guardian or PositionGuardian(
            self.store,
            self.ledger,
            execution_gateway=self.execution_gateway,
            clock=self.clock,
        )
        if hasattr(self.guardian, "set_execution_gateway"):
            self.guardian.set_execution_gateway(self.execution_gateway)
        if getattr(self.service, "strategy_mode", False):
            if hasattr(self.service, "execution_gateway"):
                self.service.execution_gateway = self.execution_gateway
            agent = getattr(self.service, "agent", None)
            if agent is not None and hasattr(agent, "gateway"):
                agent.gateway = self.execution_gateway
        if hasattr(self.session_manager, "bind_runtime_lease"):
            self.session_manager.bind_runtime_lease(self.runtime_lease, self.holder_id)
        self.ai_coordinator = AISessionCoordinator(
            store=self.store,
            service=self.service,
            session_manager=self.session_manager,
            ledger=self.ledger,
            guardian=self.guardian,
            execution_gateway=self.execution_gateway,
            clock=self.clock,
        )
        # The runtime is the production scheduler for durable trade plans.
        # Requests may create/read plans, but only this runtime consumes an
        # ARMED opening plan on a market event and routes it through the
        # runtime-owned gateway.
        self.trade_plan_service = TraderCapabilityService(
            self.store,
            gateway=self.execution_gateway,
            runtime=self,
            clock=self.clock,
        )

        self._stop_event = threading.Event()
        if getattr(service, "strategy_mode", False):
            service.cancel_event = self._stop_event
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._stream: RuntimeStream | None = None
        self._stream_worker: threading.Thread | None = None
        self._lease_heartbeat_stop = threading.Event()
        self._lease_heartbeat_thread: threading.Thread | None = None
        # Runtime-owned protection is account-scoped.  The explicit None
        # compatibility mode is used only by older direct runtime harnesses;
        # it still ignores rows without an explicit account identity.
        self._account_id: str | None = str(getattr(service, "account_id", "") or "") or None
        self._subscribers: set[queue.Queue[dict[str, object]]] = set()
        self._seen_alert_ids: set[str] = {
            str(item.get("alert_id"))
            for item in store.list_alerts(limit=100)
            if item.get("alert_id")
        }
        self._last_market_event_at: datetime | None = None
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
        if getattr(self.service, "strategy_mode", False):
            from .gate_stream import GateRealtimeStream
            return GateRealtimeStream(symbols, self.service.gate)
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

    def _lease_heartbeat_loop(self) -> None:
        """Keep ownership durable while a protection stream outlives a session."""
        while not self._lease_heartbeat_stop.wait(3.0):
            token = self._fencing_token
            renewed = False
            try:
                if token is not None and callable(getattr(self.runtime_lease, "renew", None)):
                    renewed = bool(self.runtime_lease.renew("monitoring_runtime", self.holder_id, token, ttl_seconds=10))
                elif token is None:
                    # Compatibility runtimes without the v1.3 lease API cannot
                    # claim a fencing guarantee; retain their old behavior but
                    # expose the degraded capability in status.
                    renewed = bool(self.runtime_lease.acquire("monitoring_runtime", self.holder_id, ttl_seconds=10))
            except Exception:
                renewed = False
            if renewed:
                continue
            if callable(getattr(self.runtime_lease, "invalidate", None)):
                self.runtime_lease.invalidate()
            with self._lock:
                self._fencing_token = None
                self._lease_lost = True
                self.execution_gateway.runtime_fencing_token = None
                # Stop discovery and cancel any in-flight strategy/AI cycle.
                # Guardian and its protected feed are intentionally left
                # alive so the only remaining protection duty is not closed
                # as a side effect of a lease handoff.
                self._stop_event.set()
                self._wake_event.set()
            try:
                self.ai_coordinator.pause()
            except Exception:
                pass
            try:
                session_status = self.session_manager.status()
                if str(session_status.get("state")) in {"RUNNING", "STARTING", "RESUMING"}:
                    self.session_manager.pause()
            except Exception:
                pass
            try:
                invalidate = getattr(self.session_manager, "invalidate_generation", None)
                if callable(invalidate):
                    invalidate("RUNTIME_LEASE_LOST")
            except Exception:
                pass
            self._set_status(
                "degraded",
                reason="runtime_lease_lost",
                error="Runtime lease renewal failed; new risk is fenced and in-flight discovery was cancelled while protection remains active",
                execution_blocked=True,
            )
            return

    def _start_lease_heartbeat(self) -> None:
        with self._lock:
            if self._lease_heartbeat_thread is not None and self._lease_heartbeat_thread.is_alive():
                return
            self._lease_heartbeat_stop.clear()
            self._lease_heartbeat_thread = threading.Thread(
                target=self._lease_heartbeat_loop,
                name="aima-monitoring-lease-heartbeat",
                daemon=True,
            )
            self._lease_heartbeat_thread.start()

    def _stop_lease_heartbeat(self) -> None:
        with self._lock:
            self._lease_heartbeat_stop.set()
            heartbeat = self._lease_heartbeat_thread
            self._lease_heartbeat_thread = None
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=1.0)

    def status(self) -> dict[str, object]:
        with self._lock:
            result = dict(self._status)
            result["active_symbols"] = list(self._status.get("active_symbols") or [])
            result["resource"] = dict(self._status.get("resource") or {})
            result["stream"] = dict(self._status.get("stream") or {})
            if getattr(self.service, "strategy_mode", False):
                result["stream"]["provider"] = "gate_public_ws"
            result["resume_eligible"] = self._resume_eligible
            result["worker_alive"] = bool(self._worker and self._worker.is_alive())
            result["active"] = self._active_state(str(result.get("state")))
            result["account_id"] = self._account_id
            result["guardian"] = self.guardian.get_health()
            result["session"] = self.session_manager.status()
            result["ai_session"] = self.ai_coordinator.status()
            fence_valid, fence_reason = self._validate_runtime_fence(None)
            result["lease"] = {
                "holder_id": self.holder_id,
                "fencing_token": self._fencing_token,
                "lost": self._lease_lost,
                "valid": bool(fence_valid),
                "reason": fence_reason,
            }
            result["execution_blocked"] = bool(self._lease_lost)
            return result

    @property
    def account_id(self) -> str | None:
        """Return the account currently bound to this runtime, if any."""
        with self._lock:
            return self._account_id

    def subscribe(self) -> queue.Queue[dict[str, object]]:
        subscriber: queue.Queue[dict[str, object]] = queue.Queue(maxsize=128)
        with self._lock:
            self._subscribers.add(subscriber)
            subscriber.put_nowait({"type": "runtime.status", "contract_version": MONITORING_RUNTIME_CONTRACT_VERSION, "runtime": self.status()})
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, object]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def start(
        self,
        *,
        resume: bool = False,
        user_initiated: bool = True,
        account_id: str | None = None,
        enable_ai: bool = False,
    ) -> dict[str, object]:
        requested_account_id = str(account_id or self._account_id or "") or None
        if requested_account_id is None:
            # Compatibility for pre-v1.3 direct runtime harnesses: an old
            # position row may have omitted the normalized account column
            # while its payload still carries one explicit, non-legacy
            # account identity.  Adopt that identity only when it is the
            # single durable owner and there are no unassigned duties.  A
            # multi-account or legacy/unknown database remains blocked and
            # requires explicit recovery rather than an ownership guess.
            unscoped_duties = self._runtime_duties_outside(None)
            inferred_accounts = unscoped_duties.get("accounts") or {}
            if (
                not unscoped_duties.get("unassigned")
                and len(inferred_accounts) == 1
            ):
                requested_account_id = str(next(iter(inferred_accounts)))
        if enable_ai and not requested_account_id:
            raise RuntimeError("ACCOUNT_REQUIRED: AI runtime requires a registered account")
        if requested_account_id and hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                if db.execute(
                    "SELECT 1 FROM accounts WHERE account_id=?",
                    (requested_account_id,),
                ).fetchone() is None:
                    raise RuntimeError(f"ACCOUNT_NOT_FOUND: Account '{requested_account_id}' is not registered")
        with self._lock:
            if account_id and self._account_id and self._account_id != str(account_id):
                current = str(self._status.get("state"))
                duties = self._runtime_duties(self._account_id)
                if duties["has_duties"]:
                    raise RuntimeError(
                        "RUNTIME_REBIND_BLOCKED: existing account retains protection or unresolved execution duties; "
                        f"cannot rebind from '{self._account_id}' to '{requested_account_id}'"
                    )
                if self._active_state(current):
                    raise RuntimeError("RUNTIME_ACCOUNT_BOUND: runtime is active on another account")
            # Each registered account has its own scoped ledger, exchange
            # credentials and guardian state.  Historical work in account A
            # must be reported in its own dashboard, never prevent account B
            # from starting a fresh session.  In particular, old local Gate
            # TestNet mirror rows are no longer an application-wide launch
            # lock; current TestNet truth comes from Gate's private API.
            # Concurrency protection with RuntimeLease (AT10)
            if not self.runtime_lease.acquire("monitoring_runtime", self.holder_id):
                self._set_status(
                    "degraded",
                    reason="lease_held_by_another_instance",
                    error="Runtime lease held by another instance",
                )
                raise RuntimeError("Runtime lease held by another instance")

            self._fencing_token = getattr(self.runtime_lease, "fencing_token", None)
            self._lease_lost = False
            self.execution_gateway.runtime_fencing_token = self._fencing_token
            if hasattr(self.session_manager, "bind_runtime_lease"):
                self.session_manager.bind_runtime_lease(
                    self.runtime_lease,
                    self.holder_id,
                    fencing_token=self._fencing_token,
                )
            self._account_id = requested_account_id
            if getattr(self.service, "strategy_mode", False):
                self.service.ai_only = bool(enable_ai)
            if requested_account_id and hasattr(self.service, "account_id"):
                # Fixed-strategy execution is scoped to the same registered
                # account as the optional AI coordinator.  A missing scope
                # remains a visible blocked run instead of falling back to a
                # default account.
                self.service.account_id = requested_account_id
            if hasattr(self.guardian, "set_account_scope"):
                self.guardian.set_account_scope(requested_account_id)

            current = str(self._status.get("state"))
            if resume and not user_initiated and not self._resume_eligible:
                self._status["last_reason"] = "resume_not_authorized"
                self._status["transition_at"] = _iso(self.clock())
                self._persist_locked()
                return self.status()
            if user_initiated:
                self._resume_eligible = True
            self._start_lease_heartbeat()
            if self._active_state(current) and self._worker and self._worker.is_alive():
                self._status["last_reason"] = "already_active"
                if enable_ai:
                    self.ai_coordinator.start(account_id=requested_account_id)
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
            if resume:
                try:
                    self.session_manager.resume()
                except Exception:
                    pass
            else:
                try:
                    self.session_manager.start(user_initiated=user_initiated)
                except Exception:
                    pass
            self.guardian.start()
            if enable_ai:
                self.ai_coordinator.start(account_id=requested_account_id)
            self._worker = threading.Thread(target=self._worker_loop, name="aima-monitoring-runtime", daemon=True)
            self._worker.start()
            return self.status()

    def _protected_symbols(self) -> tuple[str, ...]:
        conn = self.store._connect() if hasattr(self.store, "_connect") else None
        if conn:
            with conn as db:
                has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'").fetchone()
                if has_table:
                    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(simulated_positions)").fetchall()}
                    legacy_filter = " AND legacy_unverified=0" if "legacy_unverified" in columns else ""
                    query = f"SELECT symbol, account_id, payload_json, venue, mode FROM simulated_positions WHERE status IN ('OPEN', 'PARTIALLY_CLOSED'){legacy_filter}"
                    params: list[Any] = []
                    if self._account_id:
                        query += " AND (account_id=? OR (account_id IS NULL AND json_extract(payload_json, '$.account_id')=?))"
                        params.extend([self._account_id, self._account_id])
                    rows = db.execute(query, tuple(params)).fetchall()
                    expected_mode: str | None = None
                    expected_venue: str | None = None
                    if self._account_id:
                        account_row = db.execute(
                            "SELECT mode, config_json FROM accounts WHERE account_id=?",
                            (self._account_id,),
                        ).fetchone()
                        if account_row is None:
                            return ()
                        scope = resolve_account_scope(self.store, self._account_id) or {}
                        expected_mode = str(scope.get("mode") or account_row["mode"]).upper()
                        expected_venue = str(
                            scope.get("venue")
                            or ("simulated" if expected_mode == "PAPER" else "gate")
                        ).lower()
                    symbols: list[str] = []
                    for row in rows:
                        try:
                            row_payload = json.loads(row[2] or "{}")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            row_payload = {}
                        account_id = row[1]
                        if not account_id:
                            account_id = row_payload.get("account_id")
                        row_venue = str(row[3] or row_payload.get("venue") or "simulated").lower()
                        row_mode = str(row[4] or row_payload.get("mode") or "PAPER").upper()
                        if self._account_id and (
                            account_id != self._account_id
                            or row_mode != expected_mode
                            or row_venue != expected_venue
                        ):
                            continue
                        if account_id and row[0]:
                            symbol = str(row[0]).strip().upper()
                            if symbol and symbol not in symbols:
                                symbols.append(symbol)
                    return tuple(symbols)
        return ()

    def _runtime_duties(self, account_id: str | None) -> dict[str, object]:
        """Return durable protection/in-flight duties for one account.

        Scope is resolved from normalized account/venue/mode columns.  A
        caller must never switch this runtime to another account merely
        because the session state says STOPPED; an open position, UNKNOWN
        order, or pending reservation is still a live responsibility.
        """
        result: dict[str, object] = {
            "has_duties": False,
            "open_positions": 0,
            "unresolved_orders": 0,
            "pending_reservations": 0,
            "legacy_unverified": 0,
        }
        if not account_id or not hasattr(self.store, "_connect"):
            return result
        with self.store._connect() as db:
            account = db.execute("SELECT mode, config_json FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            if account is None:
                return result
            scope = resolve_account_scope(self.store, account_id) or {}
            account_mode = str(scope.get("mode") or account["mode"]).upper()
            account_venue = str(
                scope.get("venue")
                or ("simulated" if account_mode == "PAPER" else "gate")
            ).lower()
            open_position_count = 0
            has_positions = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
            ).fetchone()
            if has_positions:
                # Resolve account/mode/venue from the row first, then the
                # payload for additive-migration rows.  A stopped runtime
                # still owns this duty; a missing normalized column must not
                # make an old position look safe to rebind away from.
                position_rows = db.execute(
                    """SELECT account_id, venue, mode, payload_json, legacy_unverified
                       FROM simulated_positions
                       WHERE status IN ('OPEN','PARTIALLY_CLOSED')"""
                ).fetchall()
                for position_row in position_rows:
                    if int(position_row["legacy_unverified"] or 0):
                        result["legacy_unverified"] = int(result["legacy_unverified"]) + 1
                        continue
                    try:
                        payload = json.loads(position_row["payload_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = {}
                    row_account = position_row["account_id"] or payload.get("account_id")
                    row_mode = str(position_row["mode"] or payload.get("mode") or "PAPER").upper()
                    row_venue = str(position_row["venue"] or payload.get("venue") or "simulated").lower()
                    if row_account == account_id and row_mode == account_mode and row_venue == account_venue:
                        open_position_count += 1
            order_count = 0
            has_orders = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'").fetchone()
            if has_orders:
                order_row = db.execute(
                    """SELECT COUNT(*) FROM order_intents
                       WHERE account_id=? AND mode=? AND venue=?
                         AND status IN ('CREATED','RISK_APPROVED','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN','SUBMITTED','SUBMITTING','CANCEL_PENDING')""",
                    (account_id, account_mode, account_venue),
                ).fetchone()
                order_count = int(order_row[0] if order_row else 0)
            reservation_count = 0
            has_reservations = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_reservations'").fetchone()
            if has_reservations:
                res_row = db.execute(
                    """SELECT COUNT(*) FROM risk_reservations
                       WHERE account_id=? AND status IN ('PENDING','COMMITTED')
                         AND (mode IS NULL OR UPPER(mode)=?)
                         AND (venue IS NULL OR LOWER(venue)=?)""",
                    (account_id, account_mode, account_venue),
                ).fetchone()
                reservation_count = int(res_row[0] if res_row else 0)
        result.update({
            "open_positions": open_position_count,
            "unresolved_orders": order_count,
            "pending_reservations": reservation_count,
        })
        result["has_duties"] = any(int(result[key]) > 0 for key in ("open_positions", "unresolved_orders", "pending_reservations", "legacy_unverified"))
        return result

    def _runtime_duties_outside(self, account_id: str | None) -> dict[str, object]:
        """Find durable duties owned by a different account.

        A newly constructed runtime has no in-memory account binding, so the
        per-account check in :meth:`_runtime_duties` is not enough after a
        process restart. Refusing to start B while A still has an open
        position, unresolved order, or pending/committed reservation is the
        safe single-runtime recovery contract: a caller can explicitly
        recover A, but cannot silently strand A's protection while claiming
        that B is running. Legacy/unassigned rows are never adopted and are
        treated as a recovery duty rather than guessed ownership.
        """
        result: dict[str, object] = {
            "has_duties": False,
            "accounts": {},
            "unassigned": 0,
            "legacy_unverified": 0,
        }
        if not hasattr(self.store, "_connect"):
            return result

        account_counts: dict[str, dict[str, int]] = {}

        def add(account: Any, kind: str) -> None:
            key = str(account or "").strip()
            if not key:
                result["unassigned"] = int(result["unassigned"]) + 1
                return
            if account_id and key == str(account_id):
                return
            bucket = account_counts.setdefault(
                key,
                {"open_positions": 0, "unresolved_orders": 0, "pending_reservations": 0},
            )
            bucket[kind] += 1

        with self.store._connect() as db:
            has_positions = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
            ).fetchone()
            if has_positions:
                rows = db.execute(
                    """SELECT account_id, payload_json, legacy_unverified
                       FROM simulated_positions
                       WHERE status IN ('OPEN','PARTIALLY_CLOSED')"""
                ).fetchall()
                for row in rows:
                    if int(row["legacy_unverified"] or 0):
                        result["legacy_unverified"] = int(result["legacy_unverified"]) + 1
                        continue
                    try:
                        payload = json.loads(row["payload_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = {}
                    add(row["account_id"] or payload.get("account_id"), "open_positions")

            has_orders = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
            ).fetchone()
            if has_orders:
                rows = db.execute(
                    """SELECT account_id FROM order_intents
                       WHERE status IN ('CREATED','RISK_APPROVED','ACKNOWLEDGED',
                                        'PARTIALLY_FILLED','UNKNOWN','SUBMITTED','SUBMITTING','CANCEL_PENDING')"""
                ).fetchall()
                for row in rows:
                    add(row["account_id"], "unresolved_orders")

            has_reservations = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='risk_reservations'"
            ).fetchone()
            if has_reservations:
                rows = db.execute(
                    """SELECT account_id FROM risk_reservations
                       WHERE status IN ('PENDING','COMMITTED')"""
                ).fetchall()
                for row in rows:
                    add(row["account_id"], "pending_reservations")

        result["accounts"] = account_counts
        result["has_duties"] = bool(account_counts or int(result["unassigned"]) or int(result["legacy_unverified"]))
        return result

    def _validate_runtime_fence(self, intent: Any | None = None) -> tuple[bool, str]:
        """Gateway callback used immediately before risk and adapter effects."""
        del intent
        with self._lock:
            token = self._fencing_token
            lease_lost = self._lease_lost
            runtime_state = str(self._status.get("state") or "stopped").lower()
        if token is None or lease_lost:
            return False, "RUNTIME_LEASE_FENCED"
        if runtime_state not in {"starting", "running", "degraded", "backoff"}:
            return False, "RUNTIME_NOT_ACTIVE"
        try:
            session_state = str(self.session_manager.status().get("state") or "")
        except Exception:
            return False, "RUNTIME_SESSION_UNAVAILABLE"
        if session_state != "RUNNING":
            return False, "SESSION_NOT_RUNNING"
        validator = getattr(self.runtime_lease, "validate", None)
        if not callable(validator):
            return False, "RUNTIME_LEASE_UNAVAILABLE"
        try:
            valid = bool(validator("monitoring_runtime", self.holder_id, int(token), now=self.clock()))
        except TypeError:
            try:
                valid = bool(validator("monitoring_runtime", self.holder_id, int(token)))
            except Exception:
                return False, "RUNTIME_LEASE_UNAVAILABLE"
        except Exception:
            return False, "RUNTIME_LEASE_UNAVAILABLE"
        return (True, "OK") if valid and not self._lease_lost else (False, "RUNTIME_LEASE_FENCED")

    def pause(self) -> dict[str, object]:
        with self._lock:
            if not self._active_state(str(self._status.get("state"))):
                self._set_status("paused", reason="already_inactive")
                return self.status()
            self._stop_event.set()
            self._wake_event.set()
            worker = self._worker
        self.ai_coordinator.pause()
        try:
            self.session_manager.pause()
        except Exception:
            pass
        # PositionGuardian intentionally remains running in background! (R05, AT08, AT10, RT03)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self.stream_join_timeout_seconds + 1.0)

        protected = self._protected_symbols()
        if protected:
            # Maintain active market stream for protected positions during pause
            self._set_status("paused", reason="explicit_pause", active_symbols=list(protected), retry_after_at=None)
            with self._lock:
                stream_payload = self._status.get("stream") or {}
                current_symbols = tuple(str(item) for item in stream_payload.get("symbols", []))
                stream_worker = self._stream_worker
                stream_alive = stream_worker is not None and stream_worker.is_alive()
            if current_symbols != protected or not stream_alive:
                self._replace_stream(protected)
                # A connection callback may arrive while replacing the stream;
                # the lifecycle state remains PAUSED even when the public feed
                # itself reports a transient degradation.
                with self._lock:
                    if self._status.get("state") == "degraded":
                        self._set_status("paused", reason="explicit_pause_stream_degraded", active_symbols=list(protected), retry_after_at=None)
        else:
            with self._lock:
                stream = self._stream
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
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
        self.ai_coordinator.stop()
        try:
            self.session_manager.terminate()
        except Exception:
            pass
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self.stream_join_timeout_seconds + 1.0)
        protected = self._protected_symbols()
        if protected:
            # Terminating discovery is not permission to turn off protection.
            # Keep the guardian, its market stream, and the lease alive so a
            # second runtime cannot manage the same positions concurrently.
            if hasattr(self.guardian, "is_active") and not self.guardian.is_active():
                self.guardian.start()
            with self._lock:
                current_stream_symbols = tuple(str(item) for item in ((self._status.get("stream") or {}).get("symbols") or []))
                stream_worker = self._stream_worker
                stream_alive = stream_worker is not None and stream_worker.is_alive()
            if current_stream_symbols != protected or not stream_alive:
                self._replace_stream(protected)
            self._set_status(
                "stopped",
                reason="explicit_stop_protection_continues" if clear_resume else "sidecar_shutdown_protection_continues",
                active_symbols=list(protected),
                retry_after_at=None,
            )
        else:
            self.guardian.stop()
            self._stop_lease_heartbeat()
            self.runtime_lease.release("monitoring_runtime", self.holder_id)
            self._fencing_token = None
            self.execution_gateway.runtime_fencing_token = None
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
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
        if getattr(self.service, "ai_only", False):
            return self.ai_coordinator._allowed_symbols()
        if getattr(self.service, "strategy_mode", False):
            return tuple(dict.fromkeys(s["symbol"] for s in self.store.list_strategy_subscriptions(True)))[:self.max_symbols]
        symbols: list[str] = []
        for row in self.store.list_monitoring_policies(enabled=True):
            value = str(row.get("instrument_id") or "").strip().upper()
            if value and value not in symbols:
                symbols.append(value)
            if len(symbols) >= self.max_symbols:
                break
        return tuple(symbols)

    def _all_monitored_symbols(self) -> tuple[str, ...]:
        enabled = list(self._enabled_symbols())
        protected = list(self._protected_symbols())
        return tuple(dict.fromkeys(enabled + protected))[:self.max_symbols]

    @property
    def active_symbols(self) -> tuple[str, ...]:
        return self._all_monitored_symbols()

    def _stream_state(self, state: RealtimeConnectionState) -> None:
        payload = state.to_dict()
        with self._lock:
            self._status["stream"] = payload
            current = str(self._status.get("state"))
            if state.status in {"degraded", "backfilling"} and current in {"starting", "running", "backoff"}:
                next_state = "degraded"
            elif state.status in {"connected", "connecting"} and current in {"starting", "degraded", "backoff"}:
                next_state = "running"
            else:
                next_state = current
        if next_state != current:
            self._set_status(next_state, reason=f"stream_{state.status}", error=state.last_error)
        else:
            # A public-stream callback can race with an isolated test/runtime
            # teardown.  In that window the owned SQLite file may already be
            # closed or moved while the stream thread is still delivering its
            # final state.  Do not let a diagnostic-only status publication
            # turn that expected teardown race into an unhandled thread error.
            try:
                runtime_status = self.status()
            except Exception:
                return
            with self._lock:
                self._publish_locked({"type": "runtime.status", "contract_version": MONITORING_RUNTIME_CONTRACT_VERSION, "runtime": runtime_status})

    def _on_stream_bar(self, symbol: str, bar: Bar, is_closed: bool) -> None:
        normalized = str(symbol).strip().upper()
        self._last_market_event_at = self.clock()
        with self._lock:
            active_symbols = set(self._status.get("active_symbols") or [])
            protected = set(self._protected_symbols())
        if normalized not in active_symbols and normalized not in protected:
            return
        try:
            if getattr(self.service, "strategy_mode", False):
                native = normalized[:-4] + "_USDT" if normalized.endswith("USDT") else normalized
                self.store.upsert_market_bars(
                    normalized, "15m", [bar], provider="gate", data_as_of=getattr(bar, "available_at", None) or self.clock(), now=self.clock(),
                    venue="gate", market_type="perpetual", native_symbol=native, settle_currency="USDT", price_type="last", volume_unit="contracts",
                )
            else:
                self.store.upsert_market_bars(normalized, "15m", [bar], provider="binance_public_ws", data_as_of=getattr(bar, "available_at", None) or self.clock(), now=self.clock())
        except Exception as exc:
            self._set_status("degraded", reason="stream_bar_persist_failed", error=str(exc))
        if is_closed and self._account_id and self._active_state(str(self._status.get("state"))):
            try:
                self.trade_plan_service.process_armed_trade_plans(
                    self._account_id,
                    normalized,
                    self._plan_market_snapshot(normalized, bar),
                    now=self.clock(),
                )
            except Exception as exc:
                self._set_status("degraded", reason="trade_plan_cycle_failed", error=str(exc))
        try:
            self.guardian.update_market_event(
                normalized,
                {"bar": bar, "is_closed": bool(is_closed)},
            )
            self.guardian.process_bar(normalized, bar)
        except Exception as exc:
            # A stream callback failure must be visible to the runtime and
            # operator.  Swallowing it would leave a position looking healthy
            # while its protection cycle was not evaluated.
            self._set_status("degraded", reason="guardian_cycle_failed", error=str(exc))
        if is_closed and self._active_state(str(self._status.get("state"))):
            self._wake_event.set()

    def process_market_event(self, symbol: str, bar: Bar) -> list[dict[str, Any]]:
        """Feed one local market event through plans and PositionGuardian.

        Existing protection is evaluated even while paused/stopped.  New
        opening plans are consumed only while this runtime is actively
        running and fenced; the durable service still rechecks that boundary.
        """
        normalized = str(symbol).strip().upper()
        self._last_market_event_at = self.clock()
        try:
            self.store.upsert_market_bars(
                normalized,
                "15m",
                [bar],
                provider="runtime_direct",
                data_as_of=getattr(bar, "available_at", None) or self.clock(),
                now=self.clock(),
            )
        except Exception as exc:
            self._set_status("degraded", reason="direct_market_persist_failed", error=str(exc))
        if self._account_id and self._active_state(str(self._status.get("state"))):
            try:
                self.trade_plan_service.process_armed_trade_plans(
                    self._account_id,
                    normalized,
                    self._plan_market_snapshot(normalized, bar),
                    now=self.clock(),
                )
            except Exception as exc:
                self._set_status("degraded", reason="trade_plan_cycle_failed", error=str(exc))
        self.guardian.update_market_event(normalized, {"bar": bar})
        # This direct local event API is the bounded PAPER/replay injection
        # path.  Public stream callbacks above deliberately omit replay_now
        # and therefore remain governed by the real wall clock.
        return self.guardian.process_bar(normalized, bar, replay_now=getattr(bar, "timestamp", None))

    def _plan_market_snapshot(self, symbol: str, bar: Bar) -> dict[str, Any]:
        """Build a fresh, local evidence snapshot for the plan evaluator.

        The bar close is used as the executable test quote when a separate
        quote is unavailable.  No high/low is substituted as an ask/bid,
        because that would manufacture a chase breach or a favorable fill.
        """
        point = getattr(bar, "timestamp", None)
        if not isinstance(point, datetime):
            point = self.clock()
        received = self.clock()
        existing = self.store.get_realtime_state(symbol) if hasattr(self.store, "get_realtime_state") else None
        market = dict((existing or {}).get("market") or {}) if isinstance(existing, dict) else {}
        # Only the local PAPER matching engine has an application-owned
        # contract.  For TESTNET/LIVE, a public bar is price/freshness
        # evidence only; amount steps, fees, and native protection capability
        # must come from the connected venue adapter and must not be invented
        # by the runtime.
        account_mode = ""
        if self._account_id and hasattr(self.store, "_connect"):
            with self.store._connect() as db:
                account_row = db.execute(
                    "SELECT mode FROM accounts WHERE account_id=?",
                    (self._account_id,),
                ).fetchone()
            scope = resolve_account_scope(self.store, self._account_id) if account_row else None
            account_mode = str((scope or {}).get("mode") or (account_row[0] if account_row else "")).upper()
        if account_mode == "PAPER":
            market.setdefault("contractSize", 1.0)
            market.setdefault("precision", {"amount": 0.001, "price": 0.01})
            market.setdefault("limits", {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}})
            market.setdefault("taker", 0.0005)
        return {
            "symbol": symbol,
            "price": float(bar.close),
            "bid": float(bar.close),
            "ask": float(bar.close),
            "data_as_of": _iso(point),
            "received_at": _iso(received),
            "fresh": True,
            "freshness_status": "fresh",
            "stale_after_seconds": 120,
            "market": market,
        }

    def check_market_freshness(self, max_gap_seconds: float = 15.0) -> bool:
        """Check if market data has arrived within max_gap_seconds; flag degraded if stale (RT05)."""
        now = self.clock()
        if self._last_market_event_at is None:
            if self._active_state(str(self._status.get("state"))):
                self._set_status("degraded", reason="market_data_not_received")
                return False
            return True
        gap = (now - self._last_market_event_at).total_seconds()
        if gap > max_gap_seconds:
            self._set_status("degraded", reason="market_data_stale")
            return False
        return True

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
            if previous_worker.is_alive():
                with self._lock:
                    self._stream = previous
                    self._stream_worker = previous_worker
                self._set_status("degraded", reason="stream_still_stopping", error="waiting for previous owned stream before replacement")
                return
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
            kwargs={
                "on_bar": self._on_stream_bar,
                "on_state": self._stream_state,
                **({"on_event": self._on_stream_event} if getattr(self.service, "strategy_mode", False) else {}),
            },
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

    def _on_stream_event(self, kind: str, payload: dict[str, object]) -> None:
        """Persist native Gate events without turning them into signals."""

        if not getattr(self.service, "strategy_mode", False) or not isinstance(payload, dict):
            return
        store = self.store
        symbol = str(payload.get("symbol") or "").strip().upper()
        native = str(payload.get("native_symbol") or "").strip().upper()
        if not symbol and native:
            symbol = native.replace("_", "")
        if not symbol or not native:
            return
        environment = str(payload.get("environment") or "LIVE_PUBLIC").upper()
        try:
            if kind == "candle" and payload.get("bar") is not None:
                timeframe = str(payload.get("timeframe") or "15m").lower()
                bar = payload["bar"]
                store.upsert_market_bars(symbol, timeframe, [bar], provider="gate", data_as_of=getattr(bar, "available_at", None) or self.clock(), now=self.clock(), venue="gate", market_type="perpetual", native_symbol=native, settle_currency="USDT", price_type="last", volume_unit="contracts")
            elif kind == "order_book_snapshot":
                store.save_gate_order_book_snapshot(symbol, native, payload, provider="gate", environment=environment, now=self.clock())
            elif kind == "order_book_delta":
                store.save_gate_order_book_delta(symbol, native, payload, provider="gate", environment=environment, now=self.clock())
            elif kind == "trade":
                store.save_gate_trade(symbol, native, payload, provider="gate", environment=environment, now=self.clock())
            elif kind == "liquidation":
                store.save_gate_liquidation(symbol, native, payload, provider="gate", environment=environment, now=self.clock())
            elif kind in {"order_book_untrusted", "order_book_snapshot_failed"}:
                store.record_runtime_diagnostic(state="DATA_BLOCKED", code="ORDER_BOOK_UNTRUSTED", payload={"kind": kind, "native_symbol": native, "sequence_status": payload.get("sequence_status"), "error_code": payload.get("error_code")}, occurred_at=self.clock())
        except Exception as exc:
            self._set_status("degraded", reason="gate_event_persist_failed", error=f"{kind}:{type(exc).__name__}")

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
        if getattr(self.service, "ai_only", False):
            # Coordinator refreshes public data and calls Qwen once per cycle.
            # The monitoring loop continues stream/Guardian maintenance only.
            return MonitoringRunResult("AI_COORDINATOR_OWNS_DECISIONS", self.clock(), (), (), {"symbols": len(symbols)})
        return self.service.run(symbols=symbols, now=self.clock())

    def _worker_loop(self) -> None:
        failure_count = 0
        try:
            while not self._stop_event.is_set():
                strategy_symbols = self._enabled_symbols()
                stream_symbols = self._all_monitored_symbols()
                with self._lock:
                    stream_payload = self._status.get("stream") or {}
                    current_stream_symbols = tuple(str(item) for item in stream_payload.get("symbols", []))
                    stream = self._stream
                    stream_worker = self._stream_worker
                    stream_needs_restart = bool(stream_symbols) and (stream is None or stream_worker is None or not stream_worker.is_alive())
                if stream_symbols != current_stream_symbols or stream_needs_restart:
                    self._replace_stream(stream_symbols)
                if self._stop_event.is_set():
                    break
                with self._lock:
                    stream_status = str((self._status.get("stream") or {}).get("status") or "stopped")
                runtime_state = "degraded" if stream_status in {"degraded", "backfilling"} else "running"
                self._set_status(runtime_state, reason="worker_active", active_symbols=list(stream_symbols), retry_after_at=None)
                if not strategy_symbols:
                    self._wake_event.wait(self.poll_interval_seconds)
                    self._wake_event.clear()
                    continue
                try:
                    result = self._run_cycle(strategy_symbols)
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
                    if self._stop_event.is_set():
                        break
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
            protected = self._protected_symbols()
            if not protected:
                if stream is not None:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                if stream_worker is not None and stream_worker is not threading.current_thread():
                    stream_worker.join(timeout=self.stream_join_timeout_seconds)
            with self._lock:
                self._worker = None
                if not protected:
                    self._stream = None
                    self._stream_worker = None
                self._status["worker_alive"] = False
                if protected:
                    self._status["active_symbols"] = list(protected)


__all__ = ["MONITORING_RUNTIME_CONTRACT_VERSION", "MonitoringRuntime", "RuntimeStream", "StreamFactory"]
