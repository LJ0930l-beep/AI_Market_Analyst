"""Robust, Idempotent Data Migration Engine (AT26).

Fulfills Chapter 14 & AT26 requirements:
1. Audits legacy accounts: flags unverified 1000/10000 capital accounts as LEGACY_UNVERIFIED.
2. Audits legacy positions/orders: flags records missing fills or protection as RECONCILIATION_REQUIRED.
3. Strict idempotence: running migration multiple times yields identical counts and causes no duplicate errors.
4. Transaction rollback safety on simulated interruption.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

logger = logging.getLogger("core.data_migration")


class LegacyAccountStatus(str, Enum):
    LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"
    VERIFIED = "VERIFIED"


class LegacyOrderStatus(str, Enum):
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    VERIFIED = "VERIFIED"


@dataclass
class MigrationResult:
    success: bool
    accounts_migrated: int = 0
    orders_migrated: int = 0
    report: Dict[str, Any] = field(default_factory=dict)


class DataMigrator:
    def __init__(self, store_or_db: Any):
        self.target = store_or_db

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Provides an isolated SQLite connection supporting Store or file path."""
        if hasattr(self.target, "_connect"):
            with self.target._connect() as conn:
                yield conn
        elif isinstance(self.target, (str, Path)):
            conn = sqlite3.connect(str(self.target))
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        else:
            raise ValueError(f"Unsupported migration target: {type(self.target)}")

    def migrate(self) -> MigrationResult:
        """Standard migration entry point for AT26 tests and CLI runners."""
        now_iso = datetime.now(timezone.utc).isoformat()
        accounts_migrated = 0
        orders_migrated = 0

        with self._connect() as db:
            # 1. Accounts audit
            has_accounts = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone()
            if has_accounts:
                columns = [col[1] for col in db.execute("PRAGMA table_info(accounts)").fetchall()]
                if "status" not in columns:
                    db.execute("ALTER TABLE accounts ADD COLUMN status TEXT")

                rows = db.execute("SELECT * FROM accounts").fetchall()
                for row in rows:
                    aid = row["account_id"]
                    initial_deposit = row["initial_deposit"] if "initial_deposit" in row.keys() else 0.0
                    if initial_deposit is None or float(initial_deposit) <= 0.0:
                        db.execute(
                            "UPDATE accounts SET status = ? WHERE account_id = ?",
                            (LegacyAccountStatus.LEGACY_UNVERIFIED.value, aid),
                        )
                    else:
                        db.execute(
                            "UPDATE accounts SET status = ? WHERE account_id = ?",
                            (LegacyAccountStatus.VERIFIED.value, aid),
                        )
                    accounts_migrated += 1

            # 2. Orders audit
            has_orders = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
            ).fetchone()
            if has_orders:
                columns = [col[1] for col in db.execute("PRAGMA table_info(orders)").fetchall()]
                if "reconciliation_status" not in columns:
                    db.execute("ALTER TABLE orders ADD COLUMN reconciliation_status TEXT")

                rows = db.execute("SELECT * FROM orders").fetchall()
                for row in rows:
                    oid = row["order_id"]
                    filled = row["filled_quantity"] if "filled_quantity" in row.keys() else 0.0
                    st = str(row["status"]).upper() if "status" in row.keys() else ""
                    if filled is None or float(filled) == 0.0 or st in ("OPEN", "PENDING"):
                        db.execute(
                            "UPDATE orders SET reconciliation_status = ? WHERE order_id = ?",
                            (LegacyOrderStatus.RECONCILIATION_REQUIRED.value, oid),
                        )
                    else:
                        db.execute(
                            "UPDATE orders SET reconciliation_status = ? WHERE order_id = ?",
                            (LegacyOrderStatus.VERIFIED.value, oid),
                        )
                    orders_migrated += 1

        return MigrationResult(
            success=True,
            accounts_migrated=accounts_migrated,
            orders_migrated=orders_migrated,
            report={"completed_at": now_iso},
        )

    def run_migration(self, simulate_failure_at_step: Optional[int] = None) -> Dict[str, Any]:
        """Runs the V2R1 data migration with transaction isolation and idempotence (AT26)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        report = {
            "started_at": now_iso,
            "legacy_unverified_accounts": 0,
            "reconciliation_required_positions": 0,
            "reconciliation_required_orders": 0,
            "schema_version": 15,
            "status": "SUCCESS",
        }

        with self._connect() as db:
            try:
                # Step 1: Ensure columns and tables exist
                db.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                """)

                # Check if accounts table exists
                has_accounts = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
                ).fetchone()
                if not has_accounts:
                    db.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        account_id TEXT PRIMARY KEY,
                        venue TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        initial_deposit REAL NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """)

                if simulate_failure_at_step == 1:
                    raise RuntimeError("SIMULATED_FAILURE_AFTER_STEP_1")

                # Step 2: Audit accounts for LEGACY_UNVERIFIED
                account_rows = db.execute("SELECT * FROM accounts").fetchall()
                for acct in account_rows:
                    aid = acct["account_id"]
                    deposit = float(acct["initial_deposit"]) if "initial_deposit" in acct.keys() else 0.0
                    status = acct["status"] if "status" in acct.keys() else None
                    if status in ("LEGACY_UNVERIFIED", "VERIFIED"):
                        continue
                    has_ledger = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_events'"
                    ).fetchone()
                    has_deposit_event = False
                    if has_ledger:
                        ev = db.execute(
                            "SELECT 1 FROM ledger_events WHERE account_id = ? AND event_type = 'INITIAL_DEPOSIT'",
                            (aid,),
                        ).fetchone()
                        has_deposit_event = bool(ev)

                    if not has_deposit_event and deposit <= 0.0:
                        db.execute(
                            "UPDATE accounts SET status = 'LEGACY_UNVERIFIED', updated_at = ? WHERE account_id = ?",
                            (now_iso, aid),
                        )
                        report["legacy_unverified_accounts"] += 1
                    else:
                        db.execute(
                            "UPDATE accounts SET status = 'VERIFIED', updated_at = ? WHERE account_id = ?",
                            (now_iso, aid),
                        )

                if simulate_failure_at_step == 2:
                    raise RuntimeError("SIMULATED_FAILURE_AFTER_STEP_2")

                # Step 3: Audit simulated_positions for RECONCILIATION_REQUIRED
                has_positions = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
                ).fetchone()
                if has_positions:
                    pos_rows = db.execute("SELECT position_id, status, payload_json FROM simulated_positions").fetchall()
                    for p in pos_rows:
                        pos_id = p["position_id"]
                        status = p["status"]
                        if status == "RECONCILIATION_REQUIRED":
                            continue
                        try:
                            payload = json.loads(p["payload_json"])
                        except Exception:
                            payload = {}

                        has_protection = bool(payload.get("stop") or payload.get("targets"))
                        has_fills = bool(payload.get("filled_contracts") or payload.get("entry"))
                        if not has_protection or not has_fills:
                            payload["migration_flag"] = "RECONCILIATION_REQUIRED"
                            db.execute(
                                "UPDATE simulated_positions SET status = 'RECONCILIATION_REQUIRED', payload_json = ?, updated_at = ? WHERE position_id = ?",
                                (json.dumps(payload), now_iso, pos_id),
                            )
                            report["reconciliation_required_positions"] += 1

                # Step 4: Audit order_intents
                has_orders = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_intents'"
                ).fetchone()
                if has_orders:
                    order_rows = db.execute("SELECT intent_id, status, protection_plan_json FROM order_intents").fetchall()
                    for o in order_rows:
                        iid = o["intent_id"]
                        status = o["status"]
                        if status == "RECONCILIATION_REQUIRED":
                            continue
                        plan = o["protection_plan_json"]
                        if not plan or plan == "{}" or plan == "null":
                            db.execute(
                                "UPDATE order_intents SET status = 'RECONCILIATION_REQUIRED', updated_at = ? WHERE intent_id = ?",
                                (now_iso, iid),
                            )
                            report["reconciliation_required_orders"] += 1

                # Step 5: Mark schema migration completed
                db.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (15, ?)",
                    (now_iso,),
                )
            except Exception as e:
                logger.error("Migration failed and rolled back: %s", str(e))
                report["status"] = "FAILED"
                report["error"] = str(e)
                raise

        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        return report
