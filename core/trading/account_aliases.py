"""Additive account-id aliases and the Gate TestNet canonical migration.

The first Gate integration called the remote TestNet account ``gate_paper``.
That name is unsafe because it looks like the local simulator and several
older tables also use ``mode=PAPER``.  The canonical runtime identity is now
``gate_testnet``.  This module deliberately has no ledger imports so it can
be called while the ledger schema is being created without an import cycle.

The migration is idempotent and intentionally conservative:

* historical rows are retained and their account scope is moved to the one
  canonical account;
* a colliding legacy encrypted credential row is retained under the legacy
  name rather than overwriting the canonical credential;
* the old local initial-deposit event is not deleted, but remote account
  truth never reads it as Gate equity;
* no account is created merely because an alias was supplied.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any


GATE_TESTNET_ACCOUNT_ID = "gate_testnet"
LEGACY_GATE_PAPER_ACCOUNT_ID = "gate_paper"
ACCOUNT_ALIAS_MIGRATION_VERSION = "gate_testnet_canonical_v1"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _replace_account_json(value: Any, old: str, new: str) -> tuple[Any, bool]:
    """Replace only exact account-id JSON values, recursively."""

    changed = False
    if isinstance(value, str):
        return (new, True) if value == old else (value, False)
    if isinstance(value, list):
        result = []
        for item in value:
            replaced, item_changed = _replace_account_json(item, old, new)
            result.append(replaced)
            changed = changed or item_changed
        return result, changed
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            replaced, item_changed = _replace_account_json(item, old, new)
            result[key] = replaced
            changed = changed or item_changed
        return result, changed
    return value, False


def _update_account_references(
    db: sqlite3.Connection,
    old: str,
    new: str,
    *,
    preserve_colliding_credentials: bool,
) -> None:
    """Move account_id columns and exact JSON references across all tables."""

    tables = [
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        if table in {"accounts", "account_aliases"}:
            continue
        table_columns = _columns(db, table)
        if "account_id" in table_columns:
            if table == "secure_account_credentials" and preserve_colliding_credentials:
                # The old encrypted material is valuable evidence but cannot
                # replace a canonical credential slot.  Keep it isolated.
                collision = db.execute(
                    "SELECT 1 FROM secure_account_credentials WHERE account_id=?",
                    (new,),
                ).fetchone()
                if collision is not None:
                    continue
            db.execute(f"UPDATE {table} SET account_id=? WHERE account_id=?", (new, old))

        json_columns = table_columns.intersection(
            {
                "payload_json",
                "config_json",
                "execution_result_json",
                "protection_plan_json",
                "risk_decision_json",
                "selection_evidence_json",
                "result_json",
                "context_json",
                "profile_json",
                "capability_json",
            }
        )
        for column in json_columns:
            rows = db.execute(
                f"SELECT rowid, {column} FROM {table} WHERE {column} LIKE ?",
                (f"%{old}%",),
            ).fetchall()
            for row in rows:
                raw = row[1]
                try:
                    decoded = json.loads(raw or "null")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                replaced, changed = _replace_account_json(decoded, old, new)
                if changed:
                    db.execute(
                        f"UPDATE {table} SET {column}=? WHERE rowid=?",
                        (json.dumps(replaced, ensure_ascii=False, allow_nan=False), row[0]),
                    )


def _canonical_config(existing: Any, *, aliases: list[str] | None = None) -> dict[str, Any]:
    config = _json_object(existing)
    known = [str(item) for item in (config.get("legacy_aliases") or []) if str(item)]
    for item in aliases or []:
        if item not in known:
            known.append(item)
    if LEGACY_GATE_PAPER_ACCOUNT_ID not in known:
        known.append(LEGACY_GATE_PAPER_ACCOUNT_ID)
    config.update(
        {
            "venue": "gate",
            "provider": "gate",
            "account_type": "GATE_TESTNET",
            "environment": "testnet",
            "execution_mode": "TESTNET",
            "gate_account_kind": "TESTNET",
            "gate_api_environment": "TESTNET",
            "gate_api_base_url": "https://api-testnet.gateapi.io/api/v4",
            "execution_adapter": "GATE_TESTNET_API",
            "gate_account_profile": "gate-account-v1",
            "gate_market_type": "perpetual",
            "settle_currency": "USDT",
            "remote_private_read": "EXPLICIT_ONLY",
            "remote_orders": "TESTNET_ONLY",
            "local_simulator": False,
            "live_execution": "TESTNET_ONLY",
            "legacy_aliases": known,
        }
    )
    return config


def ensure_account_alias_migration(db: sqlite3.Connection) -> None:
    """Create the alias table and migrate the old Gate TestNet row once.

    The caller owns the surrounding transaction.  The function does not call
    ``commit`` so it is safe during ``AccountLedger._ensure_tables``.
    """

    db.execute(
        """CREATE TABLE IF NOT EXISTS account_aliases (
            alias_id TEXT PRIMARY KEY,
            alias_account_id TEXT NOT NULL UNIQUE,
            canonical_account_id TEXT NOT NULL,
            alias_kind TEXT NOT NULL,
            migration_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    if not _table_exists(db, "accounts"):
        return

    legacy = db.execute(
        "SELECT * FROM accounts WHERE account_id=?", (LEGACY_GATE_PAPER_ACCOUNT_ID,)
    ).fetchone()
    canonical = db.execute(
        "SELECT * FROM accounts WHERE account_id=?", (GATE_TESTNET_ACCOUNT_ID,)
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()

    if legacy is not None:
        if canonical is None:
            # Move all dependent rows before changing the referenced account
            # primary key.  SQLite deployments keep FK enforcement off for
            # legacy compatibility, but this order is safe either way.
            _update_account_references(
                db,
                LEGACY_GATE_PAPER_ACCOUNT_ID,
                GATE_TESTNET_ACCOUNT_ID,
                preserve_colliding_credentials=False,
            )
            config = _canonical_config(legacy["config_json"])
            db.execute(
                """UPDATE accounts
                   SET account_id=?, mode='TESTNET', config_json=?
                   WHERE account_id=?""",
                (
                    GATE_TESTNET_ACCOUNT_ID,
                    json.dumps(config, ensure_ascii=False, allow_nan=False),
                    LEGACY_GATE_PAPER_ACCOUNT_ID,
                ),
            )
        else:
            # Merge the old history into the existing canonical account.  Do
            # not merge a colliding encrypted credential slot because that
            # would silently change which secret the user selected.
            _update_account_references(
                db,
                LEGACY_GATE_PAPER_ACCOUNT_ID,
                GATE_TESTNET_ACCOUNT_ID,
                preserve_colliding_credentials=True,
            )
            config = _canonical_config(canonical["config_json"])
            db.execute(
                "UPDATE accounts SET mode='TESTNET', config_json=? WHERE account_id=?",
                (json.dumps(config, ensure_ascii=False, allow_nan=False), GATE_TESTNET_ACCOUNT_ID),
            )
            db.execute(
                "DELETE FROM accounts WHERE account_id=?", (LEGACY_GATE_PAPER_ACCOUNT_ID,)
            )

    canonical = db.execute(
        "SELECT config_json FROM accounts WHERE account_id=?", (GATE_TESTNET_ACCOUNT_ID,)
    ).fetchone()
    if canonical is not None:
        config = _canonical_config(canonical[0])
        db.execute(
            "UPDATE accounts SET mode='TESTNET', config_json=? WHERE account_id=?",
            (json.dumps(config, ensure_ascii=False, allow_nan=False), GATE_TESTNET_ACCOUNT_ID),
        )
        db.execute(
            """INSERT INTO account_aliases(
                alias_id, alias_account_id, canonical_account_id,
                alias_kind, migration_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(alias_account_id) DO UPDATE SET
                canonical_account_id=excluded.canonical_account_id,
                alias_kind=excluded.alias_kind,
                migration_version=excluded.migration_version""",
            (
                f"account_alias:{LEGACY_GATE_PAPER_ACCOUNT_ID}",
                LEGACY_GATE_PAPER_ACCOUNT_ID,
                GATE_TESTNET_ACCOUNT_ID,
                "LEGACY_GATE_ACCOUNT",
                ACCOUNT_ALIAS_MIGRATION_VERSION,
                now,
            ),
        )


def canonical_account_id(store: Any, account_id: str) -> str:
    """Resolve an account id through the durable alias table when present."""

    clean = _clean(account_id)
    if not clean or not hasattr(store, "_connect"):
        return clean
    try:
        with store._connect() as db:
            if _table_exists(db, "account_aliases"):
                row = db.execute(
                    "SELECT canonical_account_id FROM account_aliases WHERE alias_account_id=?",
                    (clean,),
                ).fetchone()
                if row is not None:
                    return str(row[0])
            # Compatibility for a store created by a caller that has the
            # accounts table but has not yet constructed AccountLedger.
            if clean == LEGACY_GATE_PAPER_ACCOUNT_ID and _table_exists(db, "accounts"):
                exists = db.execute(
                    "SELECT 1 FROM accounts WHERE account_id=?", (GATE_TESTNET_ACCOUNT_ID,)
                ).fetchone()
                if exists is not None:
                    return GATE_TESTNET_ACCOUNT_ID
    except sqlite3.Error:
        return clean
    return clean


__all__ = [
    "ACCOUNT_ALIAS_MIGRATION_VERSION",
    "GATE_TESTNET_ACCOUNT_ID",
    "LEGACY_GATE_PAPER_ACCOUNT_ID",
    "canonical_account_id",
    "ensure_account_alias_migration",
]
