"""SessionManager and RuntimeLease (R05).

Enforces:
- 8-state MonitoringSession lifecycle (IDLE, STARTING, RUNNING, PAUSING, PAUSED, RESUMING, TERMINATING, TERMINATED).
- Generation-based session isolation: incrementing generation upon resume automatically invalidates late AI returns (AT09).
- Database-backed RuntimeLease preventing dual desktop instances from executing concurrently (AT10).
"""

from datetime import datetime, timezone, timedelta
import json
import sqlite3
import threading
import uuid
from functools import wraps
from typing import Any, Dict, Optional, Tuple


class SessionState:
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    TERMINATING = "TERMINATING"
    TERMINATED = "TERMINATED"


class InvalidStateTransitionError(Exception):
    """Raised when an illegal state transition is attempted (returns 409 Conflict)."""
    def __init__(self, current_state: str, target_state: str, message: str = ""):
        self.current_state = current_state
        self.target_state = target_state
        super().__init__(message or f"Cannot transition from {current_state} to {target_state}")


class SessionManager:
    """Manages monitoring session lifecycle, generation versions, and state transitions."""

    def __init__(self, db_path_or_conn: Any, *, runtime_lease: Any | None = None, lease_name: str = "monitoring_runtime", holder_id: str | None = None):
        self._lock = threading.RLock()
        self._runtime_lease = runtime_lease
        self._lease_name = lease_name
        self._lease_holder_id = holder_id
        self._fencing_token: int | None = None
        if isinstance(db_path_or_conn, str):
            self._db_path = db_path_or_conn
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._store = None
        elif isinstance(db_path_or_conn, sqlite3.Connection):
            self._db_path = None
            self._conn = db_path_or_conn
            self._store = None
        elif hasattr(db_path_or_conn, "_connect"):
            self._store = db_path_or_conn
            self._db_path = str(getattr(db_path_or_conn, "path", getattr(db_path_or_conn, "db_path", ":memory:")))
            if hasattr(db_path_or_conn, "_memory_connection") and str(getattr(db_path_or_conn, "path", "")) == ":memory:":
                if db_path_or_conn._memory_connection is None:
                    with db_path_or_conn._connect():
                        pass
                self._conn = db_path_or_conn._memory_connection
            elif self._db_path and self._db_path != ":memory:":
                self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            else:
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
        else:
            self._db_path = ":memory:"
            self._store = None
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

        self._ephemeral_file_connection = bool(self._db_path and self._db_path != ":memory:")
        self._ensure_tables()
        self._load_or_init_session()
        if self._ephemeral_file_connection:
            self.close()

    def close(self) -> None:
        """Release a file-backed connection when the manager is idle."""
        with self._lock:
            if self._conn is not None:
                try:
                    if self._store is None or getattr(self._store, "_memory_connection", None) is not self._conn:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._conn = sqlite3.connect(self._db_path or ":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_tables(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS monitoring_sessions (
                session_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                generation INTEGER NOT NULL,
                state_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS runtime_leases (
                lease_name TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                fencing_token INTEGER NOT NULL DEFAULT 0
            );
            """)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(runtime_leases)").fetchall()}
            if "fencing_token" not in columns:
                conn.execute("ALTER TABLE runtime_leases ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 0")
            conn.commit()

    def _load_or_init_session(self) -> None:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM monitoring_sessions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row:
                self.current_session_id = row["session_id"]
                self.current_state = row["state"]
                self.current_generation = int(row["generation"])
                self.state_version = int(row["state_version"])
            else:
                self.current_session_id = f"sess_{uuid.uuid4().hex[:12]}"
                self.current_state = SessionState.IDLE
                self.current_generation = 0
                self.state_version = 0
                now_iso = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """INSERT INTO monitoring_sessions
                       (session_id, state, generation, state_version, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (self.current_session_id, self.current_state, self.current_generation, self.state_version, now_iso, now_iso),
                )
                conn.commit()

    def _persist(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO monitoring_sessions
               (session_id, state, generation, state_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (self.current_session_id, self.current_state, self.current_generation, self.state_version, now_iso, now_iso),
        )
        conn.commit()

    def start(self, user_initiated: bool = True) -> Dict[str, Any]:
        with self._lock:
            if self.current_state == SessionState.RUNNING:
                return self.status()  # Idempotent

            if self.current_state in (SessionState.TERMINATED, SessionState.IDLE):
                # Start new fresh session
                self.current_session_id = f"sess_{uuid.uuid4().hex[:12]}"
                self.current_generation = 1
                self.state_version = 1
                self.current_state = SessionState.RUNNING
                self._persist()
                return self.status()

            if self.current_state == SessionState.PAUSED:
                # Starting a paused session cleanly resumes it with incremented generation
                return self.resume()

            self.current_state = SessionState.RUNNING
            self.state_version += 1
            self._persist()
            return self.status()

    def pause(self) -> Dict[str, Any]:
        with self._lock:
            if self.current_state == SessionState.PAUSED:
                return self.status()  # Idempotent

            if self.current_state not in (SessionState.RUNNING, SessionState.STARTING):
                raise InvalidStateTransitionError(self.current_state, SessionState.PAUSED, f"Cannot pause session in state {self.current_state}")

            self.current_state = SessionState.PAUSED
            self.state_version += 1
            self._persist()
            return self.status()

    def resume(self) -> Dict[str, Any]:
        with self._lock:
            if self.current_state == SessionState.RUNNING:
                return self.status()  # Idempotent

            if self.current_state != SessionState.PAUSED:
                raise InvalidStateTransitionError(self.current_state, SessionState.RUNNING, f"Cannot resume session in state {self.current_state}")

            # Advance generation to invalidate any late AI returns issued prior to pause
            self.current_generation += 1
            self.state_version += 1
            self.current_state = SessionState.RUNNING
            self._persist()
            return self.status()

    def terminate(self) -> Dict[str, Any]:
        with self._lock:
            if self.current_state == SessionState.TERMINATED:
                return self.status()  # Idempotent

            self.current_state = SessionState.TERMINATED
            self.state_version += 1
            self._persist()
            return self.status()

    def invalidate_generation(self, reason: str = "RUNTIME_LEASE_LOST") -> Dict[str, Any]:
        """Advance the durable generation after an execution-owner loss.

        A lease handoff is stronger than an ordinary UI pause: work already
        submitted to a model must become stale even if it finishes after the
        session is later resumed by another runtime instance.
        """
        del reason  # The generation itself is the durable invalidation fact.
        with self._lock:
            self.current_generation += 1
            self.state_version += 1
            self._persist()
            return self.status()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.current_session_id,
                "state": self.current_state,
                "generation": self.current_generation,
                "state_version": self.state_version,
                "active": self.current_state == SessionState.RUNNING,
            }

    def bind_runtime_lease(self, runtime_lease: Any, holder_id: str, *, lease_name: str = "monitoring_runtime", fencing_token: int | None = None) -> None:
        """Attach a durable lease fence to session validation.

        Session/generation checks alone are process-local.  When a runtime
        owns a persistent lease, every AI boundary also needs the holder and
        monotonically increasing fencing token from that lease.
        """
        with self._lock:
            self._runtime_lease = runtime_lease
            self._lease_name = lease_name
            self._lease_holder_id = holder_id
            self._fencing_token = int(fencing_token) if fencing_token is not None else None

    def clear_runtime_lease_fence(self) -> None:
        with self._lock:
            self._fencing_token = None

    def validate_execution(
        self,
        session_id: str,
        generation: int,
        *,
        lease_name: str | None = None,
        holder_id: str | None = None,
        fencing_token: int | None = None,
    ) -> Tuple[bool, str]:
        """Validate if an AI decision or order proposal matches current running session & generation."""
        with self._lock:
            if self.current_state == SessionState.TERMINATED:
                return False, "SESSION_TERMINATED"
            if self.current_state != SessionState.RUNNING:
                return False, f"SESSION_NOT_RUNNING (current: {self.current_state})"
            if session_id != self.current_session_id:
                return False, f"SESSION_MISMATCH (expected: {self.current_session_id}, got: {session_id})"
            if generation != self.current_generation:
                return False, f"STALE_GENERATION (expected: {self.current_generation}, got: {generation})"
            lease = self._runtime_lease
            lease_name = lease_name or self._lease_name
            holder_id = holder_id or self._lease_holder_id
            if fencing_token is None:
                fencing_token = self._fencing_token
            if lease is not None and holder_id and fencing_token is not None:
                validator = getattr(lease, "validate", None)
                if not callable(validator) or not validator(
                    lease_name or "monitoring_runtime",
                    holder_id,
                    int(fencing_token),
                ):
                    return False, "RUNTIME_LEASE_FENCED"
            return True, "OK"

    def is_proposal_valid(
        self,
        proposal_generation: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> bool:
        """Convenience check if a proposal is valid under the current session and generation."""
        with self._lock:
            if self.current_state != SessionState.RUNNING:
                return False
            if session_id is not None and session_id != self.current_session_id:
                return False
            if proposal_generation is not None and proposal_generation != self.current_generation:
                return False
            return True


class RuntimeLease:
    """Acquires and maintains a durable database lock to prevent dual active processes (AT10)."""

    def __init__(self, db_path_or_conn: Any, default_ttl_seconds: int = 10):
        self._lock = threading.RLock()
        if isinstance(db_path_or_conn, str):
            self._db_path = db_path_or_conn
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._store = None
        elif isinstance(db_path_or_conn, sqlite3.Connection):
            self._db_path = None
            self._conn = db_path_or_conn
            self._store = None
        elif hasattr(db_path_or_conn, "_connect"):
            self._store = db_path_or_conn
            self._db_path = str(getattr(db_path_or_conn, "path", getattr(db_path_or_conn, "db_path", ":memory:")))
            if hasattr(db_path_or_conn, "_memory_connection") and str(getattr(db_path_or_conn, "path", "")) == ":memory:":
                if db_path_or_conn._memory_connection is None:
                    with db_path_or_conn._connect():
                        pass
                self._conn = db_path_or_conn._memory_connection
            elif self._db_path and self._db_path != ":memory:":
                self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            else:
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
        else:
            self._db_path = ":memory:"
            self._store = None
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        self.default_ttl = default_ttl_seconds
        self._fencing_token: int | None = None
        self._last_lease_name: str | None = None
        self._last_holder_id: str | None = None
        self._invalidated = False
        self._ephemeral_file_connection = bool(self._db_path and self._db_path != ":memory:")
        self._ensure_tables()
        if self._ephemeral_file_connection:
            self.close()

    def _ensure_tables(self) -> None:
        with self._lock:
            self._ensure_schema_on_connection(self._get_conn())

    @staticmethod
    def _ensure_schema_on_connection(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runtime_leases (
                lease_name TEXT PRIMARY KEY,
                holder_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                fencing_token INTEGER NOT NULL DEFAULT 0
            );
            """)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(runtime_leases)").fetchall()}
        if "fencing_token" not in columns:
            conn.execute("ALTER TABLE runtime_leases ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._conn = sqlite3.connect(self._db_path or ":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # File-backed connections are intentionally closed after each lease
        # operation on Windows.  The parent directory can also disappear
        # while an old runtime thread is winding down.  Re-apply the additive
        # schema on every reopened connection so status/validation cannot
        # raise a background "no such table" error or silently skip fencing.
        self._ensure_schema_on_connection(self._conn)
        return self._conn

    @property
    def fencing_token(self) -> int | None:
        with self._lock:
            return self._fencing_token

    def invalidate(self) -> None:
        """Permanently fence this executor until a new RuntimeLease is made."""
        with self._lock:
            self._invalidated = True
            self._fencing_token = None

    @staticmethod
    def _parse_expiry(value: Any) -> datetime | None:
        try:
            point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return point if point.tzinfo is not None else point.replace(tzinfo=timezone.utc)

    def current(self, lease_name: str) -> Dict[str, Any] | None:
        with self._lock:
            try:
                conn = self._get_conn()
                row = conn.execute("SELECT * FROM runtime_leases WHERE lease_name=?", (lease_name,)).fetchone()
            except Exception:
                return None
            if row is None:
                return None
            return dict(row)

    def acquire(self, lease_name: str, holder_id: str, ttl_seconds: Optional[int] = None, now: Optional[datetime] = None) -> bool:
        """Acquire a lease epoch and return False when the caller is fenced.

        A caller that already has a token may only keep a still-live epoch;
        it cannot reclaim an expired epoch after a renewal failure.  A fresh
        RuntimeLease instance may take over an expired epoch and receives a
        strictly larger token.
        """
        now = now or datetime.now(timezone.utc)
        ttl = ttl_seconds or self.default_ttl
        expires_at = now + timedelta(seconds=ttl)

        with self._lock:
            if self._invalidated and self._last_holder_id == holder_id:
                return False
            try:
                conn = self._get_conn()
                conn.execute("BEGIN IMMEDIATE")
            except Exception:
                return False
            try:
                row = conn.execute(
                    "SELECT holder_id, expires_at, fencing_token FROM runtime_leases WHERE lease_name=?",
                    (lease_name,),
                ).fetchone()

                next_token = 1
                if row:
                    current_holder = row["holder_id"]
                    current_expiry = self._parse_expiry(row["expires_at"])
                    current_expiry = current_expiry or datetime.min.replace(tzinfo=timezone.utc)
                    current_token = int(row["fencing_token"] or 0)
                    next_token = current_token + 1

                    if current_holder not in {"", None} and current_holder != holder_id and now < current_expiry:
                        # Actively held by another process!
                        conn.rollback()
                        return False
                    if current_holder == holder_id:
                        # Same holder may renew only while its epoch is live.
                        # Once local renewal has failed or the row expired,
                        # this object is not allowed to resurrect itself.
                        if self._fencing_token is not None and int(self._fencing_token) != current_token:
                            conn.rollback()
                            return False
                        if self._fencing_token is not None and now >= current_expiry:
                            conn.rollback()
                            return False
                        if now < current_expiry and self._fencing_token is None:
                            next_token = current_token
                        elif now < current_expiry:
                            next_token = current_token

                conn.execute(
                    """INSERT OR REPLACE INTO runtime_leases (lease_name, holder_id, acquired_at, expires_at, fencing_token)
                       VALUES (?, ?, ?, ?, ?)""",
                    (lease_name, holder_id, now.isoformat(), expires_at.isoformat(), next_token),
                )
                conn.commit()
                self._fencing_token = next_token
                self._last_lease_name = lease_name
                self._last_holder_id = holder_id
                return True
            except Exception:
                conn.rollback()
                return False

    def renew(
        self,
        lease_name: str,
        holder_id: str,
        fencing_token: int,
        ttl_seconds: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Renew only the exact live lease epoch; never acquire or revive it."""
        now = now or datetime.now(timezone.utc)
        ttl = ttl_seconds or self.default_ttl
        expires_at = now + timedelta(seconds=ttl)
        with self._lock:
            if self._invalidated:
                return False
            try:
                conn = self._get_conn()
                conn.execute("BEGIN IMMEDIATE")
            except Exception:
                return False
            try:
                row = conn.execute(
                    "SELECT holder_id, expires_at, fencing_token FROM runtime_leases WHERE lease_name=?",
                    (lease_name,),
                ).fetchone()
                expiry = self._parse_expiry(row["expires_at"]) if row else None
                if (
                    row is None
                    or str(row["holder_id"]) != str(holder_id)
                    or int(row["fencing_token"] or 0) != int(fencing_token)
                    or expiry is None
                    or now >= expiry
                ):
                    conn.rollback()
                    return False
                cur = conn.execute(
                    "UPDATE runtime_leases SET expires_at=? WHERE lease_name=? AND holder_id=? AND fencing_token=?",
                    (expires_at.isoformat(), lease_name, holder_id, int(fencing_token)),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return False
                conn.commit()
                self._fencing_token = int(fencing_token)
                self._last_lease_name = lease_name
                self._last_holder_id = holder_id
                return True
            except Exception:
                conn.rollback()
                return False

    def validate(self, lease_name: str, holder_id: str, fencing_token: int, *, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            try:
                conn = self._get_conn()
                row = conn.execute(
                    "SELECT holder_id, expires_at, fencing_token FROM runtime_leases WHERE lease_name=?",
                    (lease_name,),
                ).fetchone()
                if row is None or str(row["holder_id"]) != str(holder_id) or int(row["fencing_token"] or 0) != int(fencing_token):
                    return False
                expiry = self._parse_expiry(row["expires_at"])
                return expiry is not None and now < expiry
            except Exception:
                return False

    def release(self, lease_name: str, holder_id: str) -> bool:
        with self._lock:
            try:
                conn = self._get_conn()
                cur = conn.execute(
                    "UPDATE runtime_leases SET holder_id='', expires_at=? WHERE lease_name=? AND holder_id=? AND (? IS NULL OR fencing_token=?)",
                    (datetime.now(timezone.utc).isoformat(), lease_name, holder_id, self._fencing_token, self._fencing_token),
                )
                conn.commit()
            except Exception:
                return False
            if cur.rowcount > 0:
                self._fencing_token = None
                self._last_lease_name = lease_name
                self._last_holder_id = holder_id
            return cur.rowcount > 0


def _close_session_manager_connection(method):
    """Scope file handles to one SessionManager operation on Windows."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            if getattr(self, "_ephemeral_file_connection", False):
                self.close()

    return wrapped


for _session_method_name in (
    "start",
    "pause",
    "resume",
    "terminate",
    "invalidate_generation",
    "status",
    "validate_execution",
    "is_proposal_valid",
):
    setattr(SessionManager, _session_method_name, _close_session_manager_connection(getattr(SessionManager, _session_method_name)))


def _close_runtime_lease_connection(method):
    """Scope RuntimeLease file handles to one lease operation."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            if getattr(self, "_ephemeral_file_connection", False):
                self.close()

    return wrapped


def _runtime_lease_close(self) -> None:
    with self._lock:
        if self._conn is not None:
            try:
                if self._store is None or getattr(self._store, "_memory_connection", None) is not self._conn:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None


RuntimeLease.close = _runtime_lease_close
for _lease_method_name in ("acquire", "renew", "validate", "current", "release"):
    setattr(RuntimeLease, _lease_method_name, _close_runtime_lease_connection(getattr(RuntimeLease, _lease_method_name)))
