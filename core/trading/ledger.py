"""Unified AccountLedger - Single Source of Truth for Account Balances, Equity, and Risk (R06).

Replaces scattered and inconsistent equity/capital references (fixing F04).
Maintains immutable economic events and durable snapshots.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, date, time, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
from pathlib import Path
import sqlite3
import threading
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple, Union
import zoneinfo
import hashlib
import uuid

from .account_scope import resolve_account_scope


class LedgerEventType:
    INITIAL_DEPOSIT = "INITIAL_DEPOSIT"
    FILL_ENTRY = "FILL_ENTRY"
    FILL_EXIT = "FILL_EXIT"
    REALIZED_PNL = "REALIZED_PNL"
    FEE = "FEE"
    FUNDING_FEE = "FUNDING_FEE"
    FUNDING_PAYMENT = "FUNDING_PAYMENT"
    RISK_RESERVE = "RISK_RESERVE"
    RISK_RELEASE = "RISK_RELEASE"
    ADJUSTMENT = "ADJUSTMENT"


EventType = LedgerEventType

# Keep the ledger independent from execution_gateway.  Importing
# ProtectionStatus here would create a circular dependency because the
# gateway owns AccountLedger; the durable value is deliberately the stable
# wire value used by both modules.
PROTECTION_ACTIVE = "ACTIVE"


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


@dataclass
class LedgerEvent:
    event_id: str
    account_id: str
    event_type: str
    currency: str = "USDT"
    amount: Decimal = Decimal("0")
    payload: Dict[str, Any] = field(default_factory=dict)
    occurred_at: Optional[datetime] = None
    recorded_at: Optional[datetime] = None


@dataclass
class AccountSnapshot:
    account_id: str
    mode: str
    currency: str
    initial_deposit: Decimal
    wallet_balance: Decimal
    cash: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    cumulative_fees: Decimal
    cumulative_funding_fees: Decimal
    allocated_margin: Decimal
    reserved_risk: Decimal
    net_equity: Decimal
    daily_loss: Decimal
    daily_loss_limit_reached: bool
    as_of: datetime
    snapshot_id: str
    # A non-account-currency fee without a recorded conversion rate is kept
    # out of the monetary projection, but it must remain visible and block
    # new risk until it can be valued from authoritative evidence.
    unvalued_fee_events: int = 0
    # Open non-PAPER positions whose protection is not durably ACTIVE.  This
    # is a risk-blocking fact, not an estimate of loss.
    unverified_protection_count: int = 0
    # Managed Gate accounts expose the remote account snapshot as the
    # authority.  The local ledger remains visible only as an audit/mirror
    # projection when this status is unavailable.
    source: str = "LOCAL_LEDGER"
    remote_truth_status: str = "NOT_AVAILABLE"
    remote_observed_at: Optional[str] = None
    remote_snapshot_id: Optional[str] = None

    @property
    def equity(self) -> Decimal:
        return self.net_equity

    @property
    def available_margin(self) -> Decimal:
        # ``cash`` is already wallet balance less allocated position margin.
        # Subtracting ``allocated_margin`` a second time understated the
        # spendable margin and made the public snapshot disagree with the
        # atomic reservation calculation.
        return max(Decimal("0"), self.cash - self.reserved_risk)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "mode": self.mode,
            "currency": self.currency,
            "initial_deposit": str(self.initial_deposit),
            "wallet_balance": str(self.wallet_balance),
            "cash": str(self.cash),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "cumulative_fees": str(self.cumulative_fees),
            "cumulative_funding_fees": str(self.cumulative_funding_fees),
            "allocated_margin": str(self.allocated_margin),
            "reserved_risk": str(self.reserved_risk),
            "net_equity": str(self.net_equity),
            "daily_loss": str(self.daily_loss),
            "daily_loss_limit_reached": self.daily_loss_limit_reached,
            "unvalued_fee_events": self.unvalued_fee_events,
            "unverified_protection_count": self.unverified_protection_count,
            "source": self.source,
            "remote_truth_status": self.remote_truth_status,
            "remote_observed_at": self.remote_observed_at,
            "remote_snapshot_id": self.remote_snapshot_id,
            "as_of": self.as_of.isoformat(),
            "snapshot_id": self.snapshot_id,
        }


class AccountLedger:
    """Thread-safe, SQLite-backed unified account ledger."""

    def __init__(self, db_path_or_conn: Any = None, *, db_path: Any = None, store: Any = None):
        self._lock = threading.RLock()
        target = db_path if db_path is not None else (store if store is not None else db_path_or_conn)
        if isinstance(target, (str, Path)):
            self._db_path = str(target)
            self._conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._store = None
        elif isinstance(target, sqlite3.Connection):
            self._db_path = None
            self._conn = target
            self._store = None
        elif hasattr(target, "_connect"):
            # Handles V2Store / SQLiteStore wrappers
            self._store = target
            self._db_path = str(getattr(target, "path", getattr(target, "db_path", ":memory:")))
            if hasattr(target, "_memory_connection") and str(getattr(target, "path", "")) == ":memory:":
                if target._memory_connection is None:
                    with target._connect():
                        pass
                self._conn = target._memory_connection
            elif self._db_path and self._db_path != ":memory:":
                self._conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            else:
                self._conn = sqlite3.connect(":memory:", timeout=30.0, check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
        else:
            self._db_path = ":memory:"
            self._store = None
            self._conn = sqlite3.connect(":memory:", timeout=30.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        self._ephemeral_file_connection = bool(self._db_path and self._db_path != ":memory:")
        self._ensure_tables()
        # Store-backed file ledgers are frequently constructed by request
        # handlers and long-lived runtimes.  Keep the schema work durable but
        # do not leave an OS-level file handle open merely because an idle
        # ledger object is still referenced by an app state container.
        if self._ephemeral_file_connection:
            self.close()

    def __enter__(self) -> "AccountLedger":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._conn = sqlite3.connect(self._db_path or ":memory:", timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        return self._conn

    def _canonical_account_id(self, account_id: str) -> str:
        """Resolve the durable Gate alias for every ledger boundary."""

        clean = str(account_id or "").strip()
        if self._store is not None:
            try:
                from .account_aliases import canonical_account_id
                return canonical_account_id(self._store, clean)
            except Exception:
                return clean
        try:
            row = self._get_conn().execute(
                "SELECT canonical_account_id FROM account_aliases WHERE alias_account_id=?",
                (clean,),
            ).fetchone()
            return str(row[0]) if row else clean
        except sqlite3.Error:
            return clean

    @staticmethod
    def _remote_truth_ready(conn: sqlite3.Connection, account_id: str, now: datetime) -> bool:
        """Require a recent complete Gate account snapshot before new risk."""

        try:
            row = conn.execute(
                """SELECT status, observed_at, equity, available_margin
                   FROM gate_remote_account_snapshots
                   WHERE account_id=?
                   ORDER BY observed_at DESC, created_at DESC, snapshot_id DESC LIMIT 1""",
                (account_id,),
            ).fetchone()
        except sqlite3.Error:
            return False
        if row is None or str(row["status"] or "").upper() != "AVAILABLE":
            return False
        if _decimal_or_none(row["equity"]) is None or _decimal_or_none(row["available_margin"]) is None:
            return False
        try:
            observed = datetime.fromisoformat(str(row["observed_at"]).replace("Z", "+00:00"))
            observed = observed.replace(tzinfo=timezone.utc) if observed.tzinfo is None else observed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return False
        age = (now - observed).total_seconds()
        return -60.0 <= age <= 180.0

    def _ensure_tables(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                currency TEXT NOT NULL,
                initial_deposit TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger_events (
                event_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS risk_reservations (
                reservation_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                amount_risk TEXT NOT NULL,
                amount_margin TEXT NOT NULL,
                status TEXT NOT NULL, -- 'PENDING', 'COMMITTED', 'RELEASED', 'EXPIRED'
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                instrument_id TEXT,
                venue TEXT,
                mode TEXT,
                FOREIGN KEY(account_id) REFERENCES accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS daily_loss_records (
                account_id TEXT NOT NULL,
                trading_day TEXT NOT NULL, -- YYYY-MM-DD
                starting_equity TEXT NOT NULL,
                realized_loss TEXT NOT NULL DEFAULT '0',
                fees TEXT NOT NULL DEFAULT '0',
                max_adverse_pnl TEXT NOT NULL DEFAULT '0',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id, trading_day)
            );

            CREATE TABLE IF NOT EXISTS simulated_positions (
                position_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                account_id TEXT,
                venue TEXT NOT NULL DEFAULT 'simulated',
                mode TEXT NOT NULL DEFAULT 'PAPER',
                position_version INTEGER NOT NULL DEFAULT 0,
                protection_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                legacy_unverified INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS trade_fills (
                fill_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                mode TEXT NOT NULL,
                order_id TEXT,
                trade_id TEXT,
                position_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity TEXT NOT NULL,
                price TEXT NOT NULL,
                fee TEXT NOT NULL DEFAULT '0',
                fee_amount TEXT NOT NULL DEFAULT '0',
                fee_currency TEXT NOT NULL DEFAULT 'USDT',
                fx_rate TEXT,
                contract_size TEXT NOT NULL DEFAULT '1',
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                event_at TEXT
            );

            CREATE TABLE IF NOT EXISTS institutional_outbox (
                event_id TEXT PRIMARY KEY,
                aggregate_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                processed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_institutional_outbox_pending
                ON institutional_outbox(status, created_at, event_id);
            """)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(simulated_positions)").fetchall()}
            extended_existing_positions = False
            for column, definition in (
                ("account_id", "TEXT"),
                ("venue", "TEXT NOT NULL DEFAULT 'simulated'"),
                ("mode", "TEXT NOT NULL DEFAULT 'PAPER'"),
                ("position_version", "INTEGER NOT NULL DEFAULT 0"),
                ("protection_status", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
                ("legacy_unverified", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in columns:
                    conn.execute(f"ALTER TABLE simulated_positions ADD COLUMN {column} {definition}")
                    extended_existing_positions = True
            if extended_existing_positions:
                conn.execute("UPDATE simulated_positions SET legacy_unverified=1, protection_status='UNKNOWN'")
            reservation_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(risk_reservations)").fetchall()}
            for column, definition in (
                ("instrument_id", "TEXT"),
                ("venue", "TEXT"),
                ("mode", "TEXT"),
            ):
                if column not in reservation_columns:
                    conn.execute(f"ALTER TABLE risk_reservations ADD COLUMN {column} {definition}")
            fill_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(trade_fills)").fetchall()}
            for column, definition in (
                ("trade_id", "TEXT"),
                ("fee_amount", "TEXT NOT NULL DEFAULT '0'"),
                ("fee_currency", "TEXT NOT NULL DEFAULT 'USDT'"),
                ("fx_rate", "TEXT"),
                ("contract_size", "TEXT NOT NULL DEFAULT '1'"),
                ("event_at", "TEXT"),
            ):
                if column not in fill_columns:
                    conn.execute(f"ALTER TABLE trade_fills ADD COLUMN {column} {definition}")
            # The legacy ``fee`` column remains for compatibility; new rows
            # populate both names so downstream reports can use the explicit
            # fee_amount contract without rewriting old evidence.
            conn.execute("UPDATE trade_fills SET fee_amount=fee WHERE fee_amount IS NULL OR fee_amount='0' AND fee!='0'")
            from .institutional_schema import ensure_institutional_trader_schema
            ensure_institutional_trader_schema(conn)
            # Canonicalize the old Gate TestNet account after every ledger
            # schema creation.  This is additive/idempotent and keeps the
            # alias resolver available to all request-scoped ledger objects.
            from .account_aliases import ensure_account_alias_migration
            ensure_account_alias_migration(conn)
            conn.commit()

    def create_account(
        self,
        account_id: str,
        mode: str,
        currency: str = "USDT",
        initial_deposit: Decimal = Decimal("10000.0"),
        account_timezone: str = "UTC",
        config: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Explicitly register an account with a defined initial capital (no hidden 1000 vs 10000)."""
        account_id = self._canonical_account_id(account_id)
        now = now or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        config_json = json.dumps(config or {}, allow_nan=False)
        deposit_dec = Decimal(str(initial_deposit))

        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR REPLACE INTO accounts 
                   (account_id, mode, currency, initial_deposit, timezone, config_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (account_id, mode, currency, str(deposit_dec), account_timezone, config_json, now_iso),
            )
            # Record INITIAL_DEPOSIT event if not already present
            evt_id = f"evt_init_{account_id}"
            conn.execute(
                """INSERT OR IGNORE INTO ledger_events
                   (event_id, account_id, event_type, currency, amount, payload_json, occurred_at, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evt_id,
                    account_id,
                    LedgerEventType.INITIAL_DEPOSIT,
                    currency,
                    str(deposit_dec),
                    json.dumps({"description": "Initial account deposit"}),
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute(
                """INSERT OR IGNORE INTO institutional_outbox(
                    event_id, aggregate_type, aggregate_id, event_type,
                    payload_json, status, created_at
                ) VALUES (?, 'ACCOUNT_LEDGER', ?, ?, ?, 'PENDING', ?)""",
                (
                    f"ledger:{evt_id}",
                    account_id,
                    LedgerEventType.INITIAL_DEPOSIT,
                    json.dumps({
                        "account_id": account_id,
                        "event_id": evt_id,
                        "event_type": LedgerEventType.INITIAL_DEPOSIT,
                        "currency": currency,
                        "amount": str(deposit_dec),
                        "occurred_at": now_iso,
                    }, allow_nan=False),
                    now_iso,
                ),
            )
            conn.commit()

        return {
            "account_id": account_id,
            "mode": mode,
            "currency": currency,
            "initial_deposit": str(deposit_dec),
            "timezone": account_timezone,
            "created_at": now_iso,
        }

    def record_event(
        self,
        event_or_account_id: Union[LedgerEvent, str, None] = None,
        event_type: Optional[str] = None,
        amount: Optional[Union[Decimal, float]] = None,
        currency: str = "USDT",
        payload: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[datetime] = None,
        event_id: Optional[str] = None,
        now: Optional[datetime] = None,
        *,
        account_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Record an immutable economic transaction. Supports LedgerEvent or separate args."""
        now_ts = now or datetime.now(timezone.utc)
        if isinstance(event_or_account_id, LedgerEvent):
            evt = event_or_account_id
            target_account_id = self._canonical_account_id(evt.account_id)
            event_type = evt.event_type
            amount_dec = Decimal(str(evt.amount))
            currency = evt.currency or "USDT"
            payload = evt.payload
            occurred = evt.occurred_at or now_ts
            evt_id = evt.event_id or f"evt_{occurred.timestamp()}_{event_type}_{target_account_id}"
        else:
            target_account_id = self._canonical_account_id(str(account_id if account_id is not None else event_or_account_id))
            event_type = str(event_type)
            amount_dec = Decimal(str(amount))
            currency = currency or kwargs.get("currency", "USDT")
            payload = payload or kwargs.get("payload", {})
            occurred = occurred_at or now_ts
            evt_id = event_id or f"evt_{occurred.timestamp()}_{event_type}_{target_account_id}"

        payload_json = json.dumps(payload or {}, allow_nan=False)

        with self._lock:
            conn = self._get_conn()
            self._record_event_locked(
                conn,
                event_id=evt_id,
                account_id=target_account_id,
                event_type=event_type,
                currency=currency,
                amount=amount_dec,
                payload=payload or {},
                occurred_at=occurred,
                recorded_at=now_ts,
                commit=True,
            )

        return {
            "event_id": evt_id,
            "account_id": target_account_id,
            "event_type": event_type,
            "amount": str(amount_dec),
            "occurred_at": occurred.isoformat(),
        }

    def _record_event_locked(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        account_id: str,
        event_type: str,
        currency: str,
        amount: Decimal,
        payload: Dict[str, Any],
        occurred_at: datetime,
        recorded_at: datetime,
        commit: bool = False,
    ) -> bool:
        """Insert one economic event exactly once.

        The old implementation inserted duplicate event ids and then updated
        daily loss a second time.  All fill/exit paths use this helper so a
        replay or concurrent retry is economically idempotent.
        """
        # Economic events are never allowed to create an account implicitly.
        # An inferred account would make a fill look valid while detaching it
        # from the account registry and would also make cross-account leakage
        # impossible to audit after the fact.
        account_row = conn.execute(
            "SELECT currency FROM accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if account_row is None:
            raise ValueError("ACCOUNT_NOT_FOUND")
        account_currency = str(account_row["currency"] or "").strip().upper()
        event_currency = str(currency or account_currency).strip().upper()
        event_payload = dict(payload or {})
        cur = conn.execute(
            """INSERT OR IGNORE INTO ledger_events
               (event_id, account_id, event_type, currency, amount, payload_json, occurred_at, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                account_id,
                event_type,
                event_currency,
                str(amount),
                json.dumps(event_payload, allow_nan=False),
                occurred_at.isoformat(),
                recorded_at.isoformat(),
            ),
        )
        inserted = cur.rowcount > 0
        if inserted:
            self._update_daily_loss(
                conn,
                account_id,
                event_type,
                amount,
                occurred_at,
                currency=event_currency,
                payload=event_payload,
            )
            # The outbox row is written in the same SQLite transaction as the
            # economic event.  Consumers can safely retry by event_id without
            # replaying balances, fills, or protection state.
            conn.execute(
                """INSERT OR IGNORE INTO institutional_outbox(
                    event_id, aggregate_type, aggregate_id, event_type,
                    payload_json, status, created_at
                ) VALUES (?, 'ACCOUNT_LEDGER', ?, ?, ?, 'PENDING', ?)""",
                (
                    f"ledger:{event_id}",
                    account_id,
                    event_type,
                    json.dumps({
                        "account_id": account_id,
                        "event_id": event_id,
                        "event_type": event_type,
                        "currency": event_currency,
                        "amount": str(amount),
                        "occurred_at": occurred_at.isoformat(),
                        "payload": event_payload,
                    }, allow_nan=False),
                    recorded_at.isoformat(),
                ),
            )
        if commit:
            conn.commit()
        return inserted

    def get_events(self, account_id: str) -> List[LedgerEvent]:
        """Retrieve all ledger events for an account in chronological order."""
        account_id = self._canonical_account_id(account_id)
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT event_id, account_id, event_type, currency, amount, payload_json, occurred_at, recorded_at
                   FROM ledger_events WHERE account_id=? ORDER BY rowid ASC""",
                (account_id,),
            ).fetchall()
            events: List[LedgerEvent] = []
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
                except Exception:
                    payload = {}
                events.append(
                    LedgerEvent(
                        event_id=r["event_id"],
                        account_id=r["account_id"],
                        event_type=r["event_type"],
                        currency=r["currency"],
                        amount=Decimal(str(r["amount"])),
                        payload=payload,
                    )
                )
            return events

    def _get_trading_day(self, dt: datetime, tz_name: str) -> str:
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        local_dt = dt.astimezone(tz)
        return local_dt.date().isoformat()

    def _update_daily_loss(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        event_type: str,
        amount: Decimal,
        occurred_at: datetime,
        *,
        currency: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        acct_row = conn.execute("SELECT timezone, initial_deposit, currency FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        tz_name = acct_row[0] if acct_row else "UTC"
        account_currency = str(acct_row[2] if acct_row else "USDT").strip().upper()
        event_currency = str(currency or account_currency).strip().upper()
        if event_type == LedgerEventType.FEE and event_currency != account_currency and amount != 0:
            try:
                fx_rate = Decimal(str((payload or {}).get("fx_rate")))
            except (InvalidOperation, TypeError, ValueError):
                fx_rate = None
            # Keep the original event for later reconciliation, but do not
            # create a false daily monetary loss from an unvalued currency.
            if fx_rate is None or not fx_rate.is_finite() or fx_rate <= 0:
                return
            amount = amount * fx_rate
        trading_day = self._get_trading_day(occurred_at, tz_name)

        row = conn.execute(
            "SELECT starting_equity, realized_loss, fees FROM daily_loss_records WHERE account_id=? AND trading_day=?",
            (account_id, trading_day),
        ).fetchone()

        now_iso = datetime.now(timezone.utc).isoformat()
        if not row:
            starting_equity = Decimal(acct_row[1]) if acct_row else Decimal("10000.0")
            realized_loss = Decimal("0")
            fees = Decimal("0")
            if event_type in (LedgerEventType.FILL_EXIT, LedgerEventType.REALIZED_PNL) and amount < 0:
                realized_loss = abs(amount)
            elif event_type in (LedgerEventType.FEE, LedgerEventType.FUNDING_FEE, LedgerEventType.FUNDING_PAYMENT):
                fees = abs(amount)
            conn.execute(
                """INSERT INTO daily_loss_records 
                   (account_id, trading_day, starting_equity, realized_loss, fees, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account_id, trading_day, str(starting_equity), str(realized_loss), str(fees), now_iso),
            )
        else:
            realized_loss = Decimal(row[1])
            fees = Decimal(row[2])
            if event_type in (LedgerEventType.FILL_EXIT, LedgerEventType.REALIZED_PNL) and amount < 0:
                realized_loss += abs(amount)
            elif event_type in (LedgerEventType.FEE, LedgerEventType.FUNDING_FEE, LedgerEventType.FUNDING_PAYMENT):
                fees += abs(amount)
            conn.execute(
                """UPDATE daily_loss_records 
                   SET realized_loss=?, fees=?, updated_at=?
                   WHERE account_id=? AND trading_day=?""",
                (str(realized_loss), str(fees), now_iso, account_id, trading_day),
            )

    def close(self) -> None:
        """Close internal connection if owned."""
        with self._lock:
            if self._conn is not None:
                try:
                    if self._store is None or getattr(self._store, "_memory_connection", None) is not self._conn:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def reserve_risk(
        self,
        account_id: str,
        reservation_id: str,
        amount_risk: Decimal,
        amount_margin: Decimal,
        expires_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
        *,
        instrument_id: Optional[str] = None,
        venue: Optional[str] = None,
        mode: Optional[str] = None,
        max_single_risk_fraction: Optional[Decimal] = None,
        max_portfolio_risk_fraction: Decimal = Decimal("0.01"),
        max_cluster_risk_fraction: Optional[Decimal] = None,
        max_daily_loss_fraction: Optional[Decimal] = None,
        exclude_intent_id: Optional[str] = None,
    ) -> bool:
        """Atomically reserve risk budget for an in-flight order intent.

        The transaction rechecks wallet, pending reservations, committed
        position risk, and the optional single/cluster caps while holding the
        SQLite write lock.  The preflight RiskEngine calculation remains a
        useful sizing step, but this method is the final concurrency-safe
        budget boundary.
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        expires_at = expires_at or (now + timedelta(minutes=15))
        risk_dec = Decimal(str(amount_risk))
        margin_dec = Decimal(str(amount_margin))

        if risk_dec <= 0 or margin_dec < 0:
            return False

        account_id = self._canonical_account_id(account_id)

        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT account_id, amount_risk, amount_margin, status, instrument_id, venue, mode FROM risk_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if existing:
                    conn.commit()
                    # A reservation id is an idempotency key, not a reusable
                    # lock.  Replaying the exact reservation is safe; a
                    # replay with a different account or amount must never
                    # inherit the first request's approval.
                    try:
                        same_amount = (
                            Decimal(str(existing["amount_risk"])) == risk_dec
                            and Decimal(str(existing["amount_margin"])) == margin_dec
                        )
                    except Exception:
                        same_amount = False
                    same_scope = (
                        (existing["instrument_id"] or None) == (str(instrument_id) if instrument_id else None)
                        and (str(existing["venue"]).lower() if existing["venue"] else None) == (str(venue).lower() if venue else None)
                        and (str(existing["mode"]).upper() if existing["mode"] else None) == (str(mode).upper() if mode else None)
                    )
                    return bool(
                        existing["account_id"] == account_id
                        and existing["status"] == "PENDING"
                        and same_amount
                        and same_scope
                    )

                conn.execute(
                    "UPDATE risk_reservations SET status='EXPIRED' WHERE status='PENDING' AND expires_at < ?",
                    (now.isoformat(),),
                )
                acct = conn.execute(
                    "SELECT initial_deposit, mode, config_json, currency FROM accounts WHERE account_id=?", (account_id,)
                ).fetchone()
                if not acct:
                    conn.rollback()
                    return False
                initial = Decimal(str(acct[0]))
                scope = resolve_account_scope(self._store, account_id) if self._store is not None else None
                account_mode = str((scope or {}).get("mode") or acct[1]).upper()
                account_currency = str(acct[3] or "USDT").strip().upper()
                try:
                    account_config = json.loads(acct[2] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    account_config = {}
                is_gate_testnet = str(account_config.get("account_type") or "").upper() == "GATE_TESTNET"
                expected_venue = str(
                    (scope or {}).get("venue")
                    or account_config.get("venue")
                    or ("simulated" if account_mode == "PAPER" else "gate")
                ).strip().lower()

                # Gate TestNet has no local capital base.  A reservation may
                # be created only after a recent complete remote account
                # snapshot has been persisted.  Reduce-only recovery does
                # not call this method and therefore remains available when
                # the private endpoint is degraded.
                if str(account_config.get("account_type") or "").upper() == "GATE_TESTNET":
                    if not self._remote_truth_ready(conn, account_id, now):
                        conn.rollback()
                        return False

                # An open position with unknown/degraded protection is still
                # live risk.  Refuse every new reservation in this account
                # until the protection fact is reconciled; reduce-only exits
                # do not use this method and therefore remain available for
                # recovery.
                protection_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
                ).fetchone()
                if protection_table and not is_gate_testnet:
                    protection_rows = conn.execute(
                        """SELECT payload_json, protection_status, account_id, venue, mode, legacy_unverified
                           FROM simulated_positions
                           WHERE status IN ('OPEN','PARTIALLY_CLOSED')"""
                    ).fetchall()
                    for protection_row in protection_rows:
                        try:
                            protection_payload = json.loads(protection_row["payload_json"] or "{}")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            protection_payload = {}
                        scoped_protection_account = protection_row["account_id"] or protection_payload.get("account_id")
                        scoped_protection_mode = str(protection_row["mode"] or protection_payload.get("mode") or "PAPER").upper()
                        scoped_protection_venue = str(protection_row["venue"] or protection_payload.get("venue") or "simulated").lower()
                        protection_status = str(protection_row["protection_status"] or protection_payload.get("protection_status") or "UNKNOWN").upper()
                        if int(protection_row["legacy_unverified"] or 0):
                            # Ownership and environment of a legacy row cannot
                            # be reconstructed safely.  It blocks the matching
                            # account, or every account when no owner exists,
                            # while explicit reduce-only recovery remains
                            # available because it never reserves new risk.
                            if not scoped_protection_account or scoped_protection_account == account_id:
                                conn.rollback()
                                return False
                            continue
                        if (
                            scoped_protection_account == account_id
                            and scoped_protection_mode == account_mode
                            and scoped_protection_venue == expected_venue
                            and protection_status != "ACTIVE"
                        ):
                            conn.rollback()
                            return False

                # UNKNOWN/SUBMITTING/CANCEL_PENDING orders are still possible
                # exposure even when an older path failed to create a risk
                # reservation.  Do not approve another opening around them.
                # The current gateway intent is inserted before RiskEngine
                # runs, so it is explicitly excluded by its durable identity.
                order_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
                ).fetchone()
                if order_table:
                    active_statuses = (
                        "CREATED", "RISK_APPROVED", "ACKNOWLEDGED", "PARTIALLY_FILLED",
                        "UNKNOWN", "SUBMITTED", "SUBMITTING", "CANCEL_PENDING",
                    )
                    placeholders = ",".join("?" for _ in active_statuses)
                    order_rows = conn.execute(
                        f"SELECT intent_id, account_id, mode, venue, status FROM order_intents WHERE account_id=? AND status IN ({placeholders})",
                        (account_id, *active_statuses),
                    ).fetchall()
                    for order_row in order_rows:
                        if exclude_intent_id and str(order_row["intent_id"]) == str(exclude_intent_id):
                            continue
                        order_mode = str(order_row["mode"] or "").strip().upper()
                        order_venue = str(order_row["venue"] or "").strip().lower()
                        if not order_mode or not order_venue:
                            conn.rollback()
                            return False
                        if order_mode == account_mode and order_venue == expected_venue:
                            conn.rollback()
                            return False

                # An unscoped pending reservation is itself an unknown
                # exposure.  It must not be relabelled by a newer caller or
                # silently ignored when computing the next atomic budget.
                reservation_rows = conn.execute(
                    "SELECT mode, venue FROM risk_reservations WHERE account_id=? AND status='PENDING'",
                    (account_id,),
                ).fetchall()
                for reservation_row in reservation_rows:
                    reservation_mode = str(reservation_row["mode"] or "").strip().upper()
                    reservation_venue = str(reservation_row["venue"] or "").strip().lower()
                    if not reservation_mode or not reservation_venue:
                        conn.rollback()
                        return False
                events = conn.execute(
                    "SELECT event_type, amount, currency, payload_json FROM ledger_events WHERE account_id=?", (account_id,)
                ).fetchall()
                realized = Decimal("0")
                fees = Decimal("0")
                funding = Decimal("0")
                for event in events:
                    amount = Decimal(str(event["amount"]))
                    if event["event_type"] in (LedgerEventType.FILL_EXIT, LedgerEventType.REALIZED_PNL, LedgerEventType.ADJUSTMENT):
                        realized += amount
                    elif event["event_type"] == LedgerEventType.FEE:
                        event_currency = str(event["currency"] or account_currency).strip().upper()
                        if event_currency != account_currency and amount != 0:
                            try:
                                event_payload = json.loads(event["payload_json"] or "{}")
                                fx_rate = Decimal(str(event_payload.get("fx_rate")))
                            except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError, AttributeError):
                                fx_rate = None
                            if fx_rate is None or not fx_rate.is_finite() or fx_rate <= 0:
                                # The fee is an authoritative economic fact,
                                # but its account-currency value is unknown.
                                # Do not approve another risk reservation
                                # while the wallet projection is incomplete.
                                conn.rollback()
                                return False
                            fees += abs(amount) * fx_rate
                        else:
                            fees += abs(amount)
                    elif event["event_type"] in (LedgerEventType.FUNDING_FEE, LedgerEventType.FUNDING_PAYMENT):
                        funding += amount
                wallet = initial + realized - fees + funding
                remote_available_margin: Optional[Decimal] = None
                if str(account_config.get("account_type") or "").upper() == "GATE_TESTNET":
                    # ``_remote_truth_ready`` above only checks freshness and
                    # presence.  Re-read the same append-only fact inside the
                    # transaction so the atomic reservation uses Gate's
                    # equity/free-margin values rather than the zero local
                    # seed used by the canonical account row.
                    remote_row = conn.execute(
                        """SELECT status, equity, available_margin
                           FROM gate_remote_account_snapshots
                           WHERE account_id=?
                           ORDER BY observed_at DESC, created_at DESC, snapshot_id DESC LIMIT 1""",
                        (account_id,),
                    ).fetchone()
                    if remote_row is None or str(remote_row["status"] or "").upper() != "AVAILABLE":
                        conn.rollback()
                        return False
                    remote_equity = _decimal_or_none(remote_row["equity"])
                    remote_available_margin = _decimal_or_none(remote_row["available_margin"])
                    if remote_equity is None or remote_available_margin is None:
                        conn.rollback()
                        return False
                    wallet = remote_equity
                account_meta = conn.execute(
                    "SELECT timezone FROM accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                account_tz = account_meta[0] if account_meta and account_meta[0] else "UTC"
                daily = conn.execute(
                    "SELECT starting_equity, realized_loss, fees FROM daily_loss_records WHERE account_id=? AND trading_day=?",
                    (account_id, self._get_trading_day(now, account_tz)),
                ).fetchone()
                day_loss = (Decimal(str(daily[1])) + Decimal(str(daily[2]))) if daily else Decimal("0")
                starting = Decimal(str(daily[0])) if daily else wallet
                try:
                    daily_fraction = (
                        Decimal(str(max_daily_loss_fraction))
                        if max_daily_loss_fraction is not None
                        else Decimal("0.015")
                    )
                except (InvalidOperation, TypeError, ValueError):
                    conn.rollback()
                    return False
                if not daily_fraction.is_finite() or daily_fraction <= 0:
                    conn.rollback()
                    return False
                daily_fraction = min(Decimal("0.015"), daily_fraction)
                if day_loss > 0 and day_loss >= starting * daily_fraction:
                    conn.rollback()
                    return False
                try:
                    single_fraction = (
                        Decimal(str(max_single_risk_fraction))
                        if max_single_risk_fraction is not None
                        else None
                    )
                    portfolio_fraction = Decimal(str(max_portfolio_risk_fraction))
                    cluster_fraction = (
                        Decimal(str(max_cluster_risk_fraction))
                        if max_cluster_risk_fraction is not None
                        else None
                    )
                except (InvalidOperation, TypeError, ValueError):
                    conn.rollback()
                    return False
                if portfolio_fraction <= 0 or (single_fraction is not None and single_fraction <= 0) or (cluster_fraction is not None and cluster_fraction <= 0):
                    conn.rollback()
                    return False
                reserved_row = conn.execute(
                    "SELECT COALESCE(SUM(CAST(amount_risk AS REAL)), 0), COALESCE(SUM(CAST(amount_margin AS REAL)), 0) FROM risk_reservations WHERE account_id=? AND status='PENDING'",
                    (account_id,),
                ).fetchone()
                existing_reserved = Decimal(str(reserved_row[0] or 0))
                existing_reserved_margin = Decimal(str(reserved_row[1] or 0))
                net_equity = wallet
                if single_fraction is not None and risk_dec > net_equity * single_fraction:
                    conn.rollback()
                    return False
                if existing_reserved + risk_dec > net_equity * portfolio_fraction:
                    conn.rollback()
                    return False

                # Recompute committed position risk in the same transaction.
                # Positions from another environment cannot consume or satisfy
                # this account's budget.  Legacy ownership was handled above;
                # an explicit legacy owner from another account is ignored.
                position_risk = Decimal("0")
                cluster_risk = Decimal("0")
                position_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
                ).fetchone()
                if position_table and not is_gate_testnet:
                    position_rows = conn.execute(
                        """SELECT payload_json, account_id, venue, mode, legacy_unverified
                           FROM simulated_positions
                           WHERE status IN ('OPEN','PARTIALLY_CLOSED')"""
                    ).fetchall()
                    for position_row in position_rows:
                        if int(position_row["legacy_unverified"] or 0):
                            continue
                        try:
                            position = json.loads(position_row["payload_json"] or "{}")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        scoped_account = position_row["account_id"] or position.get("account_id")
                        if scoped_account != account_id:
                            continue
                        row_mode = str(position_row["mode"] or position.get("mode") or "PAPER").upper()
                        row_venue = str(position_row["venue"] or position.get("venue") or "simulated").lower()
                        if row_mode != account_mode or row_venue != expected_venue:
                            continue
                        try:
                            remaining = Decimal(str(position.get("remaining_contracts", position.get("contracts", 0))))
                            entry_price = Decimal(str(position.get("entry", position.get("entry_price", 0))))
                            stop_price = Decimal(str(position.get("stop", position.get("stop_loss", 0))))
                            contract_size = Decimal(str(position.get("contract_size", 1)))
                        except (InvalidOperation, TypeError, ValueError):
                            continue
                        if remaining <= 0 or entry_price <= 0 or stop_price <= 0 or contract_size <= 0:
                            continue
                        risk = abs(entry_price - stop_price) * remaining * contract_size
                        position_risk += risk
                        if instrument_id and str(position.get("symbol", position.get("instrument_id", ""))).upper() == str(instrument_id).upper():
                            cluster_risk += risk
                if position_risk + existing_reserved + risk_dec > net_equity * portfolio_fraction:
                    conn.rollback()
                    return False
                if cluster_fraction is not None and instrument_id and cluster_risk + risk_dec > net_equity * cluster_fraction:
                    conn.rollback()
                    return False

                # Pending reservations are part of the cluster cap when the
                # caller supplies instrument identity.  This closes the race
                # between two not-yet-filled orders for the same instrument.
                if cluster_fraction is not None and instrument_id:
                    pending_cluster = conn.execute(
                        """SELECT COALESCE(SUM(CAST(amount_risk AS REAL)), 0)
                           FROM risk_reservations
                           WHERE account_id=? AND status='PENDING' AND UPPER(COALESCE(instrument_id,''))=?""",
                        (account_id, str(instrument_id).upper()),
                    ).fetchone()
                    pending_cluster_risk = Decimal(str(pending_cluster[0] or 0))
                    if cluster_risk + pending_cluster_risk + risk_dec > net_equity * cluster_fraction:
                        conn.rollback()
                        return False

                # Margin availability is distinct from risk availability.  A
                # pending order holds its margin, while committed positions
                # consume margin until their remaining quantity is zero.
                allocated_margin = Decimal("0")
                if position_table and not is_gate_testnet:
                    position_rows = conn.execute(
                        """SELECT payload_json, account_id, venue, mode, legacy_unverified
                           FROM simulated_positions
                           WHERE status IN ('OPEN','PARTIALLY_CLOSED')"""
                    ).fetchall()
                    for position_row in position_rows:
                        if int(position_row["legacy_unverified"] or 0):
                            continue
                        try:
                            position = json.loads(position_row["payload_json"] or "{}")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        scoped_account = position_row["account_id"] or position.get("account_id")
                        if scoped_account != account_id:
                            continue
                        row_mode = str(position_row["mode"] or position.get("mode") or "PAPER").upper()
                        row_venue = str(position_row["venue"] or position.get("venue") or "simulated").lower()
                        if row_mode != account_mode or row_venue != expected_venue:
                            continue
                        remaining = Decimal(str(position.get("remaining_contracts", position.get("contracts", 0))))
                        contract_size = Decimal(str(position.get("contract_size", 1)))
                        entry_price = Decimal(str(position.get("entry", position.get("entry_price", 0))))
                        leverage = Decimal(str(position.get("leverage", 1)))
                        if remaining > 0 and contract_size > 0 and entry_price > 0 and leverage > 0:
                            allocated_margin += remaining * contract_size * entry_price / leverage
                margin_base = (
                    remote_available_margin
                    if remote_available_margin is not None
                    else wallet - allocated_margin
                )
                if margin_base - existing_reserved_margin < margin_dec:
                    conn.rollback()
                    return False
                conn.execute(
                    """INSERT INTO risk_reservations
                       (reservation_id, account_id, amount_risk, amount_margin, status, expires_at, created_at, instrument_id, venue, mode)
                       VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)""",
                    (
                        reservation_id,
                        account_id,
                        str(risk_dec),
                        str(margin_dec),
                        expires_at.isoformat(),
                        now.isoformat(),
                        str(instrument_id) if instrument_id else None,
                        str(venue) if venue else None,
                        str(mode).upper() if mode else None,
                    ),
                )
                conn.commit()
                return True
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    def release_risk(self, account_id: str, reservation_id: str) -> bool:
        """Release a pending risk reservation on cancellation, rejection, or fill."""
        account_id = self._canonical_account_id(account_id)
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                "UPDATE risk_reservations SET status='RELEASED' WHERE reservation_id=? AND account_id=? AND status='PENDING'",
                (reservation_id, account_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def commit_risk(self, account_id: str, reservation_id: str) -> bool:
        """Mark reservation as committed upon successful fill."""
        account_id = self._canonical_account_id(account_id)
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                "UPDATE risk_reservations SET status='COMMITTED' WHERE reservation_id=? AND account_id=? AND status='PENDING'",
                (reservation_id, account_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_snapshot(
        self,
        account_id: str,
        open_positions: Optional[List[Dict[str, Any]]] = None,
        mark_prices: Optional[Dict[str, float]] = None,
        now: Optional[datetime] = None,
    ) -> AccountSnapshot:
        """Compute the unified account snapshot (Single Source of Truth, R06)."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        mark_prices = mark_prices or {}
        account_id = self._canonical_account_id(account_id)

        with self._lock:
            conn = self._get_conn()
            acct_row = conn.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
            if not acct_row:
                raise ValueError(f"ACCOUNT_NOT_FOUND: Account '{account_id}' is not registered.")

            mode = acct_row["mode"]
            currency = acct_row["currency"]
            initial_deposit = Decimal(acct_row["initial_deposit"])
            tz_name = acct_row["timezone"]
            try:
                account_config = json.loads(acct_row["config_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                account_config = {}
            scope = resolve_account_scope(self._store, account_id) if self._store is not None else None
            mode = (scope or {}).get("mode") or mode
            expected_venue = str(
                (scope or {}).get("venue")
                or account_config.get("venue")
                or ("simulated" if str(mode).upper() == "PAPER" else "gate")
            ).strip()

            events = conn.execute(
                "SELECT event_type, amount, currency, payload_json FROM ledger_events WHERE account_id=?",
                (account_id,),
            ).fetchall()

            has_init_events = False
            initial_deposit_events = Decimal("0")
            realized_pnl = Decimal("0")
            cumulative_fees = Decimal("0")
            cumulative_funding = Decimal("0")
            unvalued_fee_events = 0

            for evt in events:
                etype = evt["event_type"]
                amt = Decimal(evt["amount"])
                if etype == LedgerEventType.INITIAL_DEPOSIT:
                    initial_deposit_events += amt
                    has_init_events = True
                elif etype in (LedgerEventType.FILL_EXIT, LedgerEventType.ADJUSTMENT, LedgerEventType.REALIZED_PNL):
                    realized_pnl += amt
                elif etype == LedgerEventType.FEE:
                    event_currency = str(evt["currency"] or currency).strip().upper()
                    if event_currency == str(currency).strip().upper() or amt == 0:
                        cumulative_fees += abs(amt)
                    else:
                        try:
                            event_payload = json.loads(evt["payload_json"] or "{}")
                            fx_rate = Decimal(str(event_payload.get("fx_rate")))
                        except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError, AttributeError):
                            fx_rate = None
                        if fx_rate is not None and fx_rate.is_finite() and fx_rate > 0:
                            cumulative_fees += abs(amt) * fx_rate
                        else:
                            unvalued_fee_events += 1
                elif etype in (LedgerEventType.FUNDING_FEE, LedgerEventType.FUNDING_PAYMENT):
                    cumulative_funding += amt

            if has_init_events:
                initial_deposit = initial_deposit_events

            # A reservation without a complete environment scope is legacy
            # evidence and is retained conservatively.  New reservations are
            # counted only in this account's normalized venue/mode scope.
            res_rows = conn.execute(
                """SELECT amount_risk FROM risk_reservations
                   WHERE account_id=? AND status='PENDING'
                     AND ((UPPER(COALESCE(mode, ''))=UPPER(?)
                           AND LOWER(COALESCE(venue, ''))=LOWER(?))
                          OR mode IS NULL OR venue IS NULL)""",
                (account_id, str(mode), expected_venue),
            ).fetchall()
            reserved_risk = sum((Decimal(r[0]) for r in res_rows), Decimal("0"))

            is_gate_testnet = str(account_config.get("account_type") or "").upper() == "GATE_TESTNET"
            if open_positions is None and not is_gate_testnet:
                has_sim_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
                ).fetchone()
                if has_sim_table:
                    pos_rows = conn.execute(
                        "SELECT payload_json, account_id, venue, mode, protection_status, legacy_unverified FROM simulated_positions WHERE status IN ('OPEN', 'PARTIALLY_CLOSED')"
                    ).fetchall()
                    open_positions = []
                    for row in pos_rows:
                        if int(row["legacy_unverified"] or 0):
                            continue
                        item = json.loads(row["payload_json"] or "{}")
                        scoped_account = row["account_id"] or item.get("account_id")
                        if scoped_account != account_id:
                            continue
                        row_venue = row["venue"] or item.get("venue") or "simulated"
                        row_mode = row["mode"] or item.get("mode") or "PAPER"
                        if str(row_venue).lower() != expected_venue.lower() or str(row_mode).upper() != str(mode).upper():
                            continue
                        item.setdefault("account_id", scoped_account)
                        item.setdefault("venue", row_venue)
                        item.setdefault("mode", row_mode)
                        item.setdefault("protection_status", row["protection_status"] or "UNKNOWN")
                        open_positions.append(item)
                else:
                    open_positions = []
            elif open_positions is None:
                # Gate TestNet positions are remote facts.  Historical local
                # rows remain available for audit but are never projected into
                # the unified account snapshot.
                open_positions = []

            # Callers may provide a preloaded position list, but that list is
            # not an authority boundary.  Re-apply account and environment
            # scope here so a stale or cross-account cache cannot inflate the
            # portfolio risk of the requested account.
            scoped_positions: list[Dict[str, Any]] = []
            for position in open_positions:
                if not isinstance(position, dict):
                    continue
                if str(position.get("account_id") or "") != str(account_id):
                    continue
                if int(position.get("legacy_unverified", 0) or 0):
                    continue
                position_mode = position.get("mode")
                if position_mode is not None and str(position_mode).upper() != str(mode).upper():
                    continue
                position_venue = position.get("venue")
                if position_venue is not None and str(position_venue).lower() != expected_venue.lower():
                    continue
                scoped_positions.append(position)
            open_positions = scoped_positions

            # Protection is part of the account risk snapshot.  A position
            # whose stop/remote reconciliation is not durably ACTIVE remains
            # open risk and blocks another opening reservation.
            unverified_protection_count = sum(
                1
                for position in open_positions
                if str(position.get("protection_status") or "UNKNOWN").upper() != "ACTIVE"
            )

            unrealized_pnl = Decimal("0")
            allocated_margin = Decimal("0")
            for pos in open_positions:
                symbol = pos.get("symbol", "")
                rem_contracts = Decimal(str(pos.get("remaining_contracts", pos.get("contracts", 0))))
                contract_size = Decimal(str(pos.get("contract_size", 1.0)))
                entry_price = Decimal(str(pos.get("entry", 0.0)))
                side = str(pos.get("side", "LONG")).upper()

                mark = Decimal(str(mark_prices.get(symbol, entry_price)))
                sign = Decimal("1") if side == "LONG" else Decimal("-1")
                if rem_contracts > 0 and entry_price > 0:
                    pos_pnl = (mark - entry_price) * sign * rem_contracts * contract_size
                    unrealized_pnl += pos_pnl
                    # Remote Gate positions may omit leverage.  Preserve the
                    # position fact and let the remote ``used_margin`` field
                    # override this provisional calculation below; never let
                    # a JSON null crash the account snapshot.
                    lev = _decimal_or_none(pos.get("leverage", 1.0)) or Decimal("1")
                    notional = rem_contracts * contract_size * mark
                    allocated_margin += notional / lev if lev > 0 else notional

            wallet_balance = initial_deposit + realized_pnl - cumulative_fees + cumulative_funding
            cash = wallet_balance - allocated_margin
            net_equity = wallet_balance + unrealized_pnl

            # Managed Gate TestNet economics come from the most recent
            # authoritative private snapshot.  The local event projection is
            # intentionally retained for audit/debugging, but it must never
            # be used as a fallback balance for a remote account.
            source = "LOCAL_LEDGER"
            remote_truth_status = "NOT_AVAILABLE"
            remote_observed_at: Optional[str] = None
            remote_snapshot_id: Optional[str] = None
            if str((scope or {}).get("account_type") or "").upper() == "GATE_TESTNET":
                try:
                    remote_row = conn.execute(
                        """SELECT snapshot_id, observed_at, status, equity,
                                  available_margin, used_margin, unrealized_pnl,
                                  realized_pnl
                           FROM gate_remote_account_snapshots
                           WHERE account_id=?
                           ORDER BY observed_at DESC, created_at DESC, snapshot_id DESC LIMIT 1""",
                        (account_id,),
                    ).fetchone()
                except sqlite3.Error:
                    remote_row = None
                if remote_row is not None:
                    remote_truth_status = str(remote_row["status"] or "UNAVAILABLE").upper()
                    remote_observed_at = str(remote_row["observed_at"] or "") or None
                    remote_snapshot_id = str(remote_row["snapshot_id"] or "") or None
                    if remote_truth_status == "AVAILABLE":
                        remote_equity = _decimal_or_none(remote_row["equity"])
                        remote_available = _decimal_or_none(remote_row["available_margin"])
                        remote_used = _decimal_or_none(remote_row["used_margin"])
                        remote_unrealized = _decimal_or_none(remote_row["unrealized_pnl"])
                        remote_realized = _decimal_or_none(remote_row["realized_pnl"])
                        if remote_equity is not None and remote_available is not None:
                            # For the remote projection ``initial_deposit`` is
                            # the observed account baseline used by the daily
                            # circuit-breaker.  It is not a local seeded
                            # deposit and is never written back to accounts.
                            initial_deposit = remote_equity
                            wallet_balance = remote_equity
                            cash = remote_available
                            net_equity = remote_equity
                            if remote_unrealized is not None:
                                unrealized_pnl = remote_unrealized
                            if remote_realized is not None:
                                realized_pnl = remote_realized
                            if remote_used is not None:
                                allocated_margin = remote_used
                            source = "GATE_TESTNET_REMOTE"
                        else:
                            remote_truth_status = "DEGRADED"
                            source = "GATE_TESTNET_REMOTE_DEGRADED"
                    else:
                        source = "GATE_TESTNET_REMOTE_UNAVAILABLE"

            trading_day = self._get_trading_day(now, tz_name)
            dl_row = conn.execute(
                "SELECT starting_equity, realized_loss, fees FROM daily_loss_records WHERE account_id=? AND trading_day=?",
                (account_id, trading_day),
            ).fetchone()

            if dl_row:
                starting_eq = Decimal(dl_row[0])
                day_loss = Decimal(dl_row[1]) + Decimal(dl_row[2])
            else:
                starting_eq = net_equity
                day_loss = Decimal("0")

            daily_loss_limit = starting_eq * Decimal("0.015")
            circuit_broken = (day_loss >= daily_loss_limit and day_loss > 0) or unvalued_fee_events > 0

            snapshot_id = f"snap_{account_id}_{int(now.timestamp())}"

            return AccountSnapshot(
                account_id=account_id,
                mode=mode,
                currency=currency,
                initial_deposit=initial_deposit,
                wallet_balance=wallet_balance,
                cash=cash,
                realized_pnl=realized_pnl,
                unrealized_pnl=unrealized_pnl,
                cumulative_fees=cumulative_fees,
                cumulative_funding_fees=cumulative_funding,
                allocated_margin=allocated_margin,
                reserved_risk=reserved_risk,
                net_equity=net_equity,
                daily_loss=day_loss,
                daily_loss_limit_reached=circuit_broken,
                as_of=now,
                snapshot_id=snapshot_id,
                unvalued_fee_events=unvalued_fee_events,
                unverified_protection_count=unverified_protection_count,
                source=source,
                remote_truth_status=remote_truth_status,
                remote_observed_at=remote_observed_at,
                remote_snapshot_id=remote_snapshot_id,
            )

    def get_open_positions(
        self,
        account_id: str = "default_account",
        *,
        venue: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve active positions in one account/environment scope."""
        account_id = self._canonical_account_id(account_id)
        if self._store is not None:
            scope = resolve_account_scope(self._store, account_id)
            if scope is not None:
                if str(scope.get("account_type") or "").upper() == "GATE_TESTNET":
                    # The exchange private API, not the compatibility table,
                    # owns Gate TestNet positions.  Keep old rows intact for
                    # audit/recovery inspection without exposing them as live
                    # positions to callers.
                    return []
                if venue is None:
                    venue = scope["venue"]
                if mode is None:
                    mode = scope["mode"]
        with self._lock:
            conn = self._get_conn()
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
            ).fetchone()
            if not has_table:
                return []
            rows = conn.execute(
                """SELECT payload_json, account_id, venue, mode, position_version,
                          protection_status, legacy_unverified
                   FROM simulated_positions
                   WHERE status IN ('OPEN', 'PARTIALLY_CLOSED')"""
            ).fetchall()
            positions = []
            for r in rows:
                if int(r["legacy_unverified"] or 0):
                    continue
                p = json.loads(r["payload_json"] or "{}")
                scoped_account = r["account_id"] or p.get("account_id")
                if scoped_account != account_id:
                    continue
                position_venue = r["venue"] or p.get("venue") or "simulated"
                position_mode = r["mode"] or p.get("mode") or "PAPER"
                if venue is not None and str(position_venue).lower() != str(venue).lower():
                    continue
                if mode is not None and str(position_mode).upper() != str(mode).upper():
                    continue
                p["account_id"] = scoped_account
                p.setdefault("venue", position_venue)
                p.setdefault("mode", position_mode)
                p.setdefault("position_version", int(r["position_version"] or 0))
                p.setdefault("protection_status", r["protection_status"] or "UNKNOWN")
                p.setdefault("instrument_id", p.get("symbol", ""))
                p.setdefault("quantity", p.get("remaining_contracts", p.get("contracts", 0.0)))
                p.setdefault("entry_price", p.get("entry", 0.0))
                p.setdefault("stop_loss", p.get("stop", 0.0))
                positions.append(p)
            return positions

    def record_trade_fill(
        self,
        account_id: str,
        instrument_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        mode: str = "PAPER",
        venue: str = "simulated",
        order_id: str = "",
        event_id: Optional[str] = None,
        stop_price: Optional[float] = None,
        take_profit: Optional[float] = None,
        reduce_only: bool = False,
        position_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        contract_size: Decimal = Decimal("1"),
        leverage: Decimal = Decimal("1"),
        protection_status: str = "PENDING",
        fee_currency: str = "USDT",
        fx_rate: Optional[Decimal] = None,
        event_at: Optional[datetime] = None,
        protection_contract: Optional[Dict[str, Any]] = None,
        trade_plan_id: Optional[str] = None,
        slippage_cost: Optional[Decimal] = None,
        provider: Optional[str] = None,
        environment: Optional[str] = None,
        candidate_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        strategy_version: Optional[str] = None,
        fee_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record one fill and its position effect atomically.

        A reduce-only BUY can only reduce an existing SHORT and a reduce-only
        SELL can only reduce an existing LONG.  Position identity and account
        scope are part of the same SQLite transaction as the economic events;
        a replay returns the first result and never creates a second fee or
        exit event.
        """
        account_id = self._canonical_account_id(account_id)
        side_clean = side.upper()
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        event_dt = event_at or now_dt
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        else:
            event_dt = event_dt.astimezone(timezone.utc)
        event_iso = event_dt.isoformat()
        qty_dec = Decimal(str(quantity))
        price_dec = Decimal(str(price))
        fee_dec = Decimal(str(fee))
        try:
            slippage_dec = Decimal(str(slippage_cost)) if slippage_cost is not None else None
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("INVALID_SLIPPAGE_COST")
        contract_dec = Decimal(str(contract_size))
        leverage_dec = Decimal(str(leverage))
        try:
            fx_rate_dec = Decimal(str(fx_rate)) if fx_rate is not None else None
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("INVALID_FX_RATE")
        if (
            not qty_dec.is_finite()
            or not price_dec.is_finite()
            or not fee_dec.is_finite()
            or not contract_dec.is_finite()
            or not leverage_dec.is_finite()
            or fee_dec < 0
            or (slippage_dec is not None and (not slippage_dec.is_finite() or slippage_dec < 0))
            or (fx_rate_dec is not None and (not fx_rate_dec.is_finite() or fx_rate_dec <= 0))
        ):
            raise ValueError("INVALID_FILL_PARAMETERS")
        mode_clean = str(mode.value if hasattr(mode, "value") else mode).upper()
        venue_clean = str(venue or "simulated").strip().lower()
        provider_clean = str(provider or ("gate" if venue_clean == "gate" else venue_clean)).strip().lower()
        environment_clean = str(
            environment
            or {"PAPER": "paper", "TESTNET": "testnet", "LIVE": "live", "RESEARCH": "research"}.get(mode_clean, mode_clean.lower())
        ).strip().lower()
        fee_currency_clean = str(fee_currency or "USDT").strip().upper()
        if not fee_currency_clean or len(fee_currency_clean) > 20:
            raise ValueError("INVALID_FEE_CURRENCY")
        external_fill_key = str(trade_id or event_id or f"fill:{order_id}:{instrument_id}:{side_clean}:{qty_dec}:{price_dec}")
        # Remote trade IDs are often unique only within one account/venue.
        # Namespace the durable idempotency key so a replay from another
        # account cannot be mistaken for an already-recorded fill.
        fill_key = f"{account_id}|{venue_clean}|{mode_clean}|{external_fill_key}"

        with self._lock:
            conn = self._get_conn()
            self._ensure_tables()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing_fill = conn.execute(
                    "SELECT * FROM trade_fills WHERE fill_id=?", (fill_key,)
                ).fetchone()
                if existing_fill:
                    # A trade identity is immutable.  A venue retry carrying
                    # different economics must not be treated as a harmless
                    # replay, otherwise a caller could silently change the
                    # position or fee while the old fill remains authoritative.
                    try:
                        stored_quantity = Decimal(str(existing_fill["quantity"]))
                        stored_price = Decimal(str(existing_fill["price"]))
                    except (InvalidOperation, TypeError, ValueError):
                        raise ValueError("FILL_IDEMPOTENCY_CONFLICT")
                    if (
                        str(existing_fill["symbol"]) != str(instrument_id)
                        or str(existing_fill["side"]).upper() != side_clean
                        or stored_quantity != qty_dec
                        or stored_price != price_dec
                        or (existing_fill["order_id"] and order_id and str(existing_fill["order_id"]) != str(order_id))
                    ):
                        raise ValueError("FILL_IDEMPOTENCY_CONFLICT")
                    try:
                        result = json.loads(existing_fill["payload_json"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        result = None
                    if not isinstance(result, dict) or not result:
                        result = {
                            "status": "RECORDED",
                            "fill_id": fill_key,
                            "instrument_id": existing_fill["symbol"],
                            "side": existing_fill["side"],
                            "position_id": existing_fill["position_id"],
                            "quantity": str(existing_fill["quantity"]),
                            "price": str(existing_fill["price"]),
                            "fee": str(existing_fill["fee"]),
                            "protection_status": "UNKNOWN",
                            "replayed": True,
                        }
                    conn.commit()
                    return result

                account_row = conn.execute(
                    "SELECT account_id, mode, config_json, currency FROM accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if account_row is None:
                    raise ValueError("ACCOUNT_NOT_FOUND")
                account_currency_clean = str(account_row[3] or "USDT").strip().upper()
                try:
                    account_config = json.loads(account_row[2] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    account_config = {}
                expected_venue = str(account_config.get("venue") or ("simulated" if mode_clean == "PAPER" else "gate")).strip()
                if venue_clean.lower() != expected_venue.lower():
                    raise ValueError("ACCOUNT_VENUE_MISMATCH")
                # ``gate_paper`` is a compatibility account id whose legacy
                # row mode may still be PAPER.  Its explicit discriminator is
                # Gate official TestNet, so remote fills must be stored under
                # TESTNET and can never be confused with the old local-paper
                # ledger image.
                expected_mode = str(account_row[1]).upper()
                account_type = str(account_config.get("account_type") or "").upper()
                if account_type == "GATE_TESTNET":
                    expected_mode = "TESTNET"
                elif account_type == "GATE_LIVE":
                    expected_mode = "LIVE"
                if expected_mode != mode_clean:
                    raise ValueError("ACCOUNT_MODE_MISMATCH")
                exit_request = bool(reduce_only or side_clean == "CLOSE")
                remote_only = account_type == "GATE_TESTNET" and mode_clean == "TESTNET" and venue_clean == "gate"
                if qty_dec <= 0 or price_dec <= 0 or contract_dec <= 0 or leverage_dec <= 0:
                    raise ValueError("INVALID_FILL_PARAMETERS")

                if remote_only:
                    # Gate's private API is the authority for this account's
                    # position state.  Keep the normalized fill and economic
                    # events as an audit trail, but never create or mutate a
                    # row in the local simulated-position table.
                    result = {
                        "status": "RECORDED",
                        "fill_id": fill_key,
                        "instrument_id": instrument_id,
                        "side": side_clean,
                        "position_id": position_id,
                        "quantity": str(qty_dec),
                        "cumulative_quantity": str(qty_dec),
                        "remaining_contracts": "0" if exit_request else str(qty_dec),
                        "closed_quantity": str(qty_dec) if exit_request else None,
                        "reduce_only": bool(reduce_only),
                        "protection_status": str(protection_status or "UNKNOWN").upper(),
                        "account_id": account_id,
                        "venue": venue_clean,
                        "mode": mode_clean,
                        "order_id": order_id,
                        "trade_id": external_fill_key,
                        "event_at": event_iso,
                        "trade_plan_id": trade_plan_id,
                        "leverage": str(leverage_dec),
                        "local_mirror": False,
                        "source": "GATE_REMOTE_PRIVATE_API_AUDIT",
                    }
                elif exit_request:
                    target_side = "LONG" if side_clean in ("SELL", "CLOSE") else "SHORT" if side_clean == "BUY" else None
                    if target_side is None:
                        raise ValueError("REDUCE_ONLY_DIRECTION_INVALID")
                    rows = conn.execute(
                        """SELECT position_id, symbol, status, payload_json, account_id, venue, mode,
                                  position_version, protection_status, legacy_unverified
                           FROM simulated_positions
                           WHERE symbol=? AND status IN ('OPEN','PARTIALLY_CLOSED')""",
                        (instrument_id,),
                    ).fetchall()
                    candidates = []
                    for row in rows:
                        if int(row["legacy_unverified"] or 0):
                            continue
                        payload = json.loads(row["payload_json"] or "{}")
                        row_account = row["account_id"] or payload.get("account_id")
                        row_venue = row["venue"] or payload.get("venue") or "simulated"
                        row_mode = row["mode"] or payload.get("mode") or "PAPER"
                        if row_account != account_id or row_venue != venue_clean or str(row_mode).upper() != mode_clean:
                            continue
                        if position_id and row["position_id"] != position_id:
                            continue
                        if str(payload.get("side", "")).upper() != target_side:
                            continue
                        remaining = Decimal(str(payload.get("remaining_contracts", payload.get("contracts", 0))))
                        if remaining > 0:
                            candidates.append((row, payload, remaining))
                    available = sum((item[2] for item in candidates), Decimal("0"))
                    if not candidates or qty_dec > available:
                        raise ValueError("REDUCE_ONLY_EXCEEDS_POSITION")
                    if not position_id and len(candidates) > 1:
                        raise ValueError("REDUCE_ONLY_POSITION_ID_REQUIRED")

                    remaining_to_close = qty_dec
                    affected: list[Dict[str, Any]] = []
                    for row, pos_data, available_qty in candidates:
                        if remaining_to_close <= 0:
                            break
                        closed_qty = min(remaining_to_close, available_qty)
                        entry_p = Decimal(str(pos_data.get("entry", pos_data.get("entry_price", price_dec))))
                        sign = Decimal("1") if target_side == "LONG" else Decimal("-1")
                        # The position's contract multiplier is authoritative
                        # for exits.  Using a caller-supplied/default
                        # multiplier here could silently misstate PnL when a
                        # venue reports a different contract size.
                        position_contract = Decimal(str(pos_data.get("contract_size", contract_dec)))
                        if not position_contract.is_finite() or position_contract <= 0:
                            raise ValueError("POSITION_CONTRACT_SIZE_INVALID")
                        gross_pnl = (price_dec - entry_p) * closed_qty * sign * position_contract
                        prior_realized = Decimal(str(pos_data.get("realized_pnl", 0)))
                        pos_data["realized_pnl"] = float(prior_realized + gross_pnl - fee_dec)
                        remaining_qty = available_qty - closed_qty
                        new_status = "CLOSED" if remaining_qty <= Decimal("1e-12") else "PARTIALLY_CLOSED"
                        pos_data["remaining_contracts"] = float(max(Decimal("0"), remaining_qty))
                        pos_data["quantity"] = pos_data["remaining_contracts"]
                        pos_data["status"] = new_status
                        pos_data["updated_at"] = now_iso
                        version = int(row["position_version"] or pos_data.get("position_version", 0)) + 1
                        pos_data["position_version"] = version
                        pos_data["account_id"] = account_id
                        pos_data["venue"] = venue_clean
                        pos_data["mode"] = mode_clean
                        exit_event_id = f"exit:{fill_key}:{row['position_id']}"
                        self._record_event_locked(
                            conn,
                            event_id=exit_event_id,
                            account_id=account_id,
                            event_type=LedgerEventType.FILL_EXIT,
                            currency=account_currency_clean,
                            amount=gross_pnl,
                            payload={
                                "order_id": order_id,
                                "trade_id": external_fill_key,
                                "position_id": row["position_id"],
                                "instrument_id": instrument_id,
                                "quantity": str(closed_qty),
                                "contract_size": str(position_contract),
                                "price": str(price_dec),
                                "pnl_currency": account_currency_clean,
                                "reduce_only": True,
                                "event_at": event_dt.isoformat(),
                                "provider": provider_clean,
                                "environment": environment_clean,
                                "candidate_id": candidate_id,
                                "cycle_id": cycle_id,
                                "strategy_id": strategy_id,
                                "strategy_version": strategy_version,
                            },
                            occurred_at=event_dt,
                            recorded_at=now_dt,
                        )
                        update_cursor = conn.execute(
                            """UPDATE simulated_positions
                               SET status=?, payload_json=?, updated_at=?, account_id=?, venue=?, mode=?, position_version=?,
                                   provider=COALESCE(?, provider), environment=COALESCE(?, environment),
                                   candidate_id=COALESCE(?, candidate_id), cycle_id=COALESCE(?, cycle_id),
                                   strategy_id=COALESCE(?, strategy_id), strategy_version=COALESCE(?, strategy_version)
                               WHERE position_id=? AND status IN ('OPEN','PARTIALLY_CLOSED') AND position_version=?""",
                            (
                                new_status,
                                json.dumps(pos_data, allow_nan=False),
                                now_iso,
                                account_id,
                                venue_clean,
                                mode_clean,
                                version,
                                provider_clean,
                                environment_clean,
                                candidate_id,
                                cycle_id,
                                strategy_id,
                                strategy_version,
                                row["position_id"],
                                int(row["position_version"] or 0),
                            ),
                        )
                        if update_cursor.rowcount != 1:
                            raise ValueError("REDUCE_ONLY_CONCURRENT_POSITION_CHANGED")
                        affected.append({"position_id": row["position_id"], "closed_quantity": str(closed_qty), "status": new_status})
                        remaining_to_close -= closed_qty
                    if remaining_to_close > Decimal("1e-12"):
                        raise ValueError("REDUCE_ONLY_CONCURRENT_POSITION_CHANGED")
                    result = {
                        "status": "RECORDED",
                        "fill_id": fill_key,
                        "instrument_id": instrument_id,
                        "side": side_clean,
                        "reduce_only": True,
                        "closed_quantity": str(qty_dec),
                        "positions": affected,
                        "position_id": affected[0]["position_id"] if affected else position_id,
                        "account_id": account_id,
                        "venue": venue_clean,
                        "mode": mode_clean,
                        "order_id": order_id,
                        "trade_id": trade_id,
                        "event_at": event_iso,
                        "trade_plan_id": trade_plan_id,
                        "leverage": float(leverage_dec),
                    }
                else:
                    std_side = "LONG" if side_clean in ("LONG", "BUY") else "SHORT" if side_clean == "SHORT" or side_clean == "SELL" else None
                    if std_side is None:
                        raise ValueError("OPEN_DIRECTION_INVALID")
                    pos_id = position_id or f"pos_{instrument_id}_{order_id or uuid.uuid4().hex[:10]}"
                    default_stop = float(price_dec) * (0.98 if std_side == "LONG" else 1.02)
                    act_stop = float(stop_price) if stop_price else default_stop
                    safe_protection_status = str(protection_status or "PENDING").upper()
                    existing_position = conn.execute(
                        """SELECT position_id, status, payload_json, account_id, venue, mode,
                                  position_version, protection_status, legacy_unverified
                           FROM simulated_positions WHERE position_id=?""",
                        (pos_id,),
                    ).fetchone()
                    if existing_position is not None:
                        if int(existing_position["legacy_unverified"] or 0):
                            raise ValueError("LEGACY_POSITION_UNVERIFIED")
                        existing_payload = json.loads(existing_position["payload_json"] or "{}")
                        existing_account = existing_position["account_id"] or existing_payload.get("account_id")
                        existing_venue = existing_position["venue"] or existing_payload.get("venue") or "simulated"
                        existing_mode = existing_position["mode"] or existing_payload.get("mode") or "PAPER"
                        if existing_account != account_id or str(existing_venue).lower() != venue_clean.lower() or str(existing_mode).upper() != mode_clean:
                            raise ValueError("POSITION_SCOPE_MISMATCH")
                        if str(existing_position["status"]).upper() not in {"OPEN", "PARTIALLY_CLOSED"}:
                            raise ValueError("POSITION_ID_NOT_OPEN")
                        if str(existing_payload.get("side", "")).upper() != std_side:
                            raise ValueError("POSITION_DIRECTION_MISMATCH")
                        old_remaining = Decimal(str(existing_payload.get("remaining_contracts", existing_payload.get("contracts", 0))))
                        old_contract_size = Decimal(str(existing_payload.get("contract_size", contract_dec)))
                        if old_contract_size != contract_dec:
                            raise ValueError("POSITION_CONTRACT_SIZE_MISMATCH")
                        old_entry = Decimal(str(existing_payload.get("entry", existing_payload.get("entry_price", price_dec))))
                        total_quantity = old_remaining + qty_dec
                        weighted_entry = ((old_entry * old_remaining) + (price_dec * qty_dec)) / total_quantity
                        current_protection = str(existing_position["protection_status"] or existing_payload.get("protection_status") or "PENDING").upper()
                        if current_protection == PROTECTION_ACTIVE:
                            merged_protection = current_protection
                        else:
                            merged_protection = safe_protection_status
                        existing_payload.update(
                            {
                                "account_id": account_id,
                                "venue": venue_clean,
                                "mode": mode_clean,
                                "provider": provider_clean,
                                "environment": environment_clean,
                                "candidate_id": candidate_id or existing_payload.get("candidate_id"),
                                "cycle_id": cycle_id or existing_payload.get("cycle_id"),
                                "strategy_id": strategy_id or existing_payload.get("strategy_id"),
                                "strategy_version": strategy_version or existing_payload.get("strategy_version"),
                                "entry": float(weighted_entry),
                                "entry_price": float(weighted_entry),
                                "contracts": float(Decimal(str(existing_payload.get("contracts", old_remaining))) + qty_dec),
                                "remaining_contracts": float(total_quantity),
                                "quantity": float(total_quantity),
                                "protection_status": merged_protection,
                                "protected": merged_protection == PROTECTION_ACTIVE,
                                "position_version": int(existing_position["position_version"] or 0) + 1,
                                "updated_at": now_iso,
                            }
                        )
                        existing_payload.setdefault("entry_filled_at", event_iso)
                        if merged_protection == "ACTIVE":
                            existing_payload.setdefault("protection_effective_at", now_iso)
                        if protection_contract is not None:
                            existing_payload["protection_contract"] = json.loads(json.dumps(protection_contract, allow_nan=False))
                        if trade_plan_id:
                            existing_payload["trade_plan_id"] = str(trade_plan_id)
                        if stop_price is not None:
                            existing_payload["stop"] = act_stop
                            existing_payload["stop_loss"] = act_stop
                        if take_profit is not None:
                            existing_payload["targets"] = [float(take_profit)]
                        version = int(existing_position["position_version"] or 0)
                        update_cursor = conn.execute(
                            """UPDATE simulated_positions
                               SET status='OPEN', payload_json=?, updated_at=?, position_version=?, protection_status=?
                               WHERE position_id=? AND status IN ('OPEN','PARTIALLY_CLOSED') AND position_version=?""",
                            (
                                json.dumps(existing_payload, allow_nan=False),
                                now_iso,
                                version + 1,
                                merged_protection,
                                pos_id,
                                version,
                            ),
                        )
                        if update_cursor.rowcount != 1:
                            raise ValueError("POSITION_CONCURRENTLY_CHANGED")
                        result = {
                            "status": "RECORDED",
                            "fill_id": fill_key,
                            "instrument_id": instrument_id,
                            "side": side_clean,
                            "position_id": pos_id,
                            "quantity": str(qty_dec),
                            "cumulative_quantity": str(total_quantity),
                            "remaining_contracts": str(total_quantity),
                            "protection_status": merged_protection,
                            "provider": provider_clean,
                            "environment": environment_clean,
                            "candidate_id": candidate_id or existing_payload.get("candidate_id"),
                            "cycle_id": cycle_id or existing_payload.get("cycle_id"),
                            "strategy_id": strategy_id or existing_payload.get("strategy_id"),
                            "strategy_version": strategy_version or existing_payload.get("strategy_version"),
                        }
                    else:
                        payload = {
                            "position_id": pos_id,
                            "account_id": account_id,
                            "venue": venue_clean,
                            "mode": mode_clean,
                            "provider": provider_clean,
                            "environment": environment_clean,
                            "candidate_id": candidate_id,
                            "cycle_id": cycle_id,
                            "strategy_id": strategy_id,
                            "strategy_version": strategy_version,
                            "symbol": instrument_id,
                            "instrument_id": instrument_id,
                            "side": std_side,
                            "entry": float(price_dec),
                            "entry_price": float(price_dec),
                            "contracts": float(qty_dec),
                            "remaining_contracts": float(qty_dec),
                            "quantity": float(qty_dec),
                            "contract_size": float(contract_dec),
                            "stop": act_stop,
                            "stop_loss": act_stop,
                            "targets": [float(take_profit)] if take_profit else [],
                            "leverage": float(leverage_dec),
                            "order_id": order_id,
                            "protection_status": safe_protection_status,
                            "protected": safe_protection_status == "ACTIVE",
                            "position_version": 0,
                            "created_at": now_iso,
                            "entry_filled_at": event_iso,
                        }
                        if safe_protection_status == "ACTIVE":
                            payload["protection_effective_at"] = now_iso
                        if protection_contract is not None:
                            payload["protection_contract"] = json.loads(json.dumps(protection_contract, allow_nan=False))
                        if trade_plan_id:
                            payload["trade_plan_id"] = str(trade_plan_id)
                        conn.execute(
                            """INSERT INTO simulated_positions
                               (position_id, symbol, status, payload_json, updated_at, account_id, venue, mode, position_version, protection_status, legacy_unverified,
                                provider, environment, candidate_id, cycle_id, strategy_id, strategy_version)
                               VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, ?)""",
                            (
                                pos_id, instrument_id, json.dumps(payload, allow_nan=False), now_iso,
                                account_id, venue_clean, mode_clean, safe_protection_status,
                                provider_clean, environment_clean, candidate_id, cycle_id, strategy_id, strategy_version,
                            ),
                        )
                        result = {
                            "status": "RECORDED",
                            "fill_id": fill_key,
                            "instrument_id": instrument_id,
                            "side": side_clean,
                            "position_id": pos_id,
                            "quantity": str(qty_dec),
                            "cumulative_quantity": str(qty_dec),
                            "remaining_contracts": str(qty_dec),
                            "protection_status": safe_protection_status,
                            "provider": provider_clean,
                            "environment": environment_clean,
                            "candidate_id": candidate_id,
                            "cycle_id": cycle_id,
                            "strategy_id": strategy_id,
                            "strategy_version": strategy_version,
                        }

                result.update(
                    {
                        "provider": provider_clean,
                        "environment": environment_clean,
                        "candidate_id": candidate_id,
                        "cycle_id": cycle_id,
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                    }
                )
                result["slippage_cost"] = str(slippage_dec) if slippage_dec is not None else None
                result["cost_complete"] = slippage_dec is not None

                # Keep a durable, immutable entry event alongside the
                # normalized fill row.  It carries zero cash impact for this
                # margin model, but gives reconciliation and audit tooling a
                # single event stream for both opening and exit fills.
                if not exit_request:
                    result.update(
                        {
                            "account_id": account_id,
                            "venue": venue_clean,
                            "mode": mode_clean,
                            "order_id": order_id,
                            "trade_id": external_fill_key,
                            "reduce_only": False,
                            "event_at": event_iso,
                            "trade_plan_id": trade_plan_id,
                            "leverage": str(leverage_dec),
                        }
                    )
                    self._record_event_locked(
                        conn,
                        event_id=f"entry:{fill_key}",
                        account_id=account_id,
                        event_type=LedgerEventType.FILL_ENTRY,
                        currency=account_currency_clean,
                        amount=Decimal("0"),
                        payload={
                            "order_id": order_id,
                            "trade_id": external_fill_key,
                            "position_id": result.get("position_id"),
                            "instrument_id": instrument_id,
                            "quantity": str(qty_dec),
                            "contract_size": str(contract_dec),
                            "price": str(price_dec),
                            "fee_amount": str(fee_dec),
                            "fee_currency": fee_currency_clean,
                            "fx_rate": str(fx_rate_dec) if fx_rate_dec is not None else None,
                            "event_currency": account_currency_clean,
                            "event_at": event_iso,
                        },
                        occurred_at=event_dt,
                        recorded_at=now_dt,
                    )

                if fee_dec > 0:
                    self._record_event_locked(
                        conn,
                        event_id=f"fee:{fill_key}",
                        account_id=account_id,
                        event_type=LedgerEventType.FEE,
                        currency=fee_currency_clean,
                        amount=fee_dec,
                        payload={
                            "order_id": order_id,
                            "trade_id": external_fill_key,
                            "instrument_id": instrument_id,
                            "fill_id": fill_key,
                            "fee_amount": str(fee_dec),
                            "fee_currency": fee_currency_clean,
                            "fx_rate": str(fx_rate_dec) if fx_rate_dec is not None else None,
                            "event_at": event_iso,
                        },
                        occurred_at=event_dt,
                        recorded_at=now_dt,
                    )
                conn.execute(
                    """INSERT INTO trade_fills
                       (fill_id, account_id, venue, mode, order_id, trade_id, position_id, symbol, side,
                        quantity, price, fee, fee_amount, fee_currency, fx_rate, contract_size, status,
                        payload_json, created_at, event_at, provider, environment, candidate_id, cycle_id,
                        strategy_id, strategy_version, fee_source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        fill_key,
                        account_id,
                        venue_clean,
                        mode_clean,
                        order_id,
                        external_fill_key,
                        result.get("position_id"),
                        instrument_id,
                        side_clean,
                        str(qty_dec),
                        str(price_dec),
                        str(fee_dec),
                        str(fee_dec),
                        fee_currency_clean,
                        str(fx_rate_dec) if fx_rate_dec is not None else None,
                        str(contract_dec),
                        "RECORDED",
                        json.dumps(result, allow_nan=False),
                        now_iso,
                        event_iso,
                        provider_clean,
                        environment_clean,
                        candidate_id,
                        cycle_id,
                        strategy_id,
                        strategy_version,
                        fee_source or ("REMOTE_ADAPTER" if venue_clean == "gate" else "LOCAL_LEDGER"),
                    ),
                )
                conn.commit()
                return result
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    def has_recorded_fill(
        self,
        account_id: str,
        order_id: str,
        trade_id: str,
        *,
        venue: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> bool:
        """Check one exchange trade identity inside its full scope."""
        if not trade_id:
            return False
        venue_clean = str(venue or "gate").strip().lower()
        mode_clean = str(mode or "TESTNET").strip().upper()
        fill_key = f"{account_id}|{venue_clean}|{mode_clean}|{trade_id}"
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                """SELECT 1 FROM trade_fills
                   WHERE fill_id=? AND account_id=? AND order_id=? AND venue=? AND mode=?""",
                (fill_key, account_id, order_id, venue_clean, mode_clean),
            ).fetchone()
            return row is not None

    def get_recorded_fill_quantity(
        self,
        account_id: str,
        order_id: str,
        *,
        venue: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Decimal:
        """Return durable filled quantity for one account-scoped order.

        Exchange reports commonly expose cumulative ``filled`` quantity.  A
        reconciliation retry must record only the delta that is not already
        in ``trade_fills``; this query keeps that calculation inside the same
        account/venue/mode boundary as the fill ledger.
        """
        if not order_id:
            return Decimal("0")
        with self._lock:
            conn = self._get_conn()
            clauses = ["account_id=?", "order_id=?"]
            params: list[Any] = [account_id, order_id]
            if venue:
                clauses.append("venue=?")
                params.append(venue)
            if mode:
                clauses.append("mode=?")
                params.append(mode)
            row = conn.execute(
                f"SELECT COALESCE(SUM(CAST(quantity AS REAL)), 0) FROM trade_fills WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()
            return Decimal(str(row[0] or 0)) if row else Decimal("0")

    def get_recorded_fill_totals(
        self,
        account_id: str,
        order_id: str,
        *,
        venue: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> tuple[Decimal, Decimal]:
        """Return cumulative quantity and fee recorded for one scoped order."""
        if not order_id:
            return Decimal("0"), Decimal("0")
        with self._lock:
            conn = self._get_conn()
            clauses = ["account_id=?", "order_id=?"]
            params: list[Any] = [account_id, order_id]
            if venue:
                clauses.append("venue=?")
                params.append(venue)
            if mode:
                clauses.append("mode=?")
                params.append(mode)
            row = conn.execute(
                f"SELECT COALESCE(SUM(CAST(quantity AS REAL)), 0), COALESCE(SUM(CAST(fee AS REAL)), 0) FROM trade_fills WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()
            if not row:
                return Decimal("0"), Decimal("0")
            return Decimal(str(row[0] or 0)), Decimal(str(row[1] or 0))

    def update_position_stop(
        self,
        account_id: str,
        symbol: str,
        new_stop: float,
        position_id: Optional[str] = None,
        *,
        venue: Optional[str] = None,
        mode: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> bool:
        """Update the stop price of an open position."""
        with self._lock:
            conn = self._get_conn()
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
            ).fetchone()
            if not has_table:
                return False
            rows = conn.execute(
                """SELECT position_id, payload_json, account_id, venue, mode,
                          legacy_unverified, position_version
                   FROM simulated_positions WHERE symbol=? AND status IN ('OPEN','PARTIALLY_CLOSED')""",
                (symbol,),
            ).fetchall()
            now_iso = datetime.now(timezone.utc).isoformat()
            for row in rows:
                if int(row["legacy_unverified"] or 0):
                    continue
                payload = json.loads(row["payload_json"] or "{}")
                if (row["account_id"] or payload.get("account_id")) != account_id:
                    continue
                row_venue = row["venue"] or payload.get("venue") or "simulated"
                row_mode = row["mode"] or payload.get("mode") or "PAPER"
                if venue and str(row_venue).lower() != str(venue).lower():
                    continue
                if mode and str(row_mode).upper() != str(mode).upper():
                    continue
                if position_id and row["position_id"] != position_id:
                    continue
                payload["account_id"] = account_id
                payload["stop"] = float(new_stop)
                payload["stop_loss"] = float(new_stop)
                version = int(row["position_version"] or payload.get("position_version", 0)) + 1
                payload["position_version"] = version
                expected_row_version = int(row["position_version"] or 0)
                if expected_version is not None and expected_row_version != int(expected_version):
                    return False
                update_cursor = conn.execute(
                    """UPDATE simulated_positions
                       SET payload_json=?, updated_at=?, account_id=?, position_version=?
                       WHERE position_id=? AND position_version=?""",
                    (json.dumps(payload, allow_nan=False), now_iso, account_id, version, row["position_id"], expected_row_version),
                )
                if update_cursor.rowcount == 1:
                    conn.commit()
                    return True
                # A concurrent guardian/exit won the CAS.  Do not report a
                # stop update that was not actually persisted.
                conn.rollback()
                if position_id:
                    return False
            return False

    def mark_protection(
        self,
        account_id: str,
        position_id: str,
        status: str,
        *,
        venue: Optional[str] = None,
        mode: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Persist verified protection state for one scoped position."""
        account_id = self._canonical_account_id(account_id)
        status_clean = str(status).upper()
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                """SELECT payload_json, account_id, venue, mode,
                          legacy_unverified, position_version
                   FROM simulated_positions WHERE position_id=?""",
                (position_id,),
            ).fetchone()
            if not row or int(row["legacy_unverified"] or 0):
                return False
            payload = json.loads(row["payload_json"] or "{}")
            if (row["account_id"] or payload.get("account_id")) != account_id:
                return False
            row_venue = row["venue"] or payload.get("venue") or "simulated"
            row_mode = row["mode"] or payload.get("mode") or "PAPER"
            if venue and str(row_venue).lower() != str(venue).lower():
                return False
            if mode and str(row_mode).upper() != str(mode).upper():
                return False
            payload["protection_status"] = status_clean
            payload["protected"] = status_clean == "ACTIVE"
            if status_clean == "ACTIVE":
                # The protection clock is distinct from the entry event.  A
                # bar that started before this durable confirmation cannot be
                # credited with a stop hit merely because its aggregate low
                # crossed the stop.
                payload.setdefault("protection_effective_at", datetime.now(timezone.utc).isoformat())
            if evidence:
                payload["protection_evidence"] = evidence
            version = int(row["position_version"] or payload.get("position_version", 0)) + 1
            payload["position_version"] = version
            update_cursor = conn.execute(
                "UPDATE simulated_positions SET payload_json=?, updated_at=?, protection_status=?, position_version=? WHERE position_id=? AND position_version=?",
                (json.dumps(payload, allow_nan=False), datetime.now(timezone.utc).isoformat(), status_clean, version, position_id, int(row["position_version"] or 0)),
            )
            if update_cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True


def _close_ephemeral_ledger_connection(method):
    """Close file-backed ledger connections after each public operation.

    ``SQLiteStore._connect`` already scopes its file handles per operation,
    while ``AccountLedger`` owns a connection for the duration of a method so
    its multi-step transactions remain atomic.  Request-scoped ledgers can
    otherwise be retained by an app/runtime object and prevent Windows from
    removing a temporary database during shutdown.
    """

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        finally:
            if getattr(self, "_ephemeral_file_connection", False):
                self.close()

    return wrapped


for _ledger_method_name in (
    "create_account",
    "record_event",
    "get_events",
    "reserve_risk",
    "release_risk",
    "commit_risk",
    "get_snapshot",
    "get_open_positions",
    "record_trade_fill",
    "has_recorded_fill",
    "get_recorded_fill_quantity",
    "get_recorded_fill_totals",
    "update_position_stop",
    "mark_protection",
):
    setattr(AccountLedger, _ledger_method_name, _close_ephemeral_ledger_connection(getattr(AccountLedger, _ledger_method_name)))
