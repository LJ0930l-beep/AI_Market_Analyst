"""Authoritative account execution scope helpers.

Account ``mode`` is retained for backward-compatible display and old rows.
Managed Gate accounts carry an explicit ``account_type`` and environment;
these helpers make that discriminator win everywhere an execution scope is
needed.  They never infer a private account from a string such as ``paper``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .account_aliases import canonical_account_id


def _config(row: Any) -> dict[str, Any]:
    value = row["config_json"] if hasattr(row, "keys") and "config_json" in row.keys() else None
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_account_scope(store: Any, account_id: str) -> dict[str, str] | None:
    """Return normalized mode/provider/environment for a registered account."""

    if not account_id or not hasattr(store, "_connect"):
        return None
    resolved_account_id = canonical_account_id(store, str(account_id))
    try:
        with store._connect() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone()
            if table is None:
                return None
            row = db.execute(
                "SELECT account_id, mode, config_json FROM accounts WHERE account_id=?",
                (resolved_account_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        # Read-only projections also serve pre-ledger/legacy stores where the
        # account table has not been created yet.
        return None
    if row is None:
        return None
    config = _config(row)
    row_mode = str(row["mode"] or "").strip().upper()
    account_type = str(config.get("account_type") or "").strip().upper()
    if account_type == "GATE_TESTNET":
        mode = "TESTNET"
        provider = "gate"
        environment = "testnet"
    elif account_type == "GATE_LIVE":
        mode = "LIVE"
        provider = "gate"
        environment = "live"
    else:
        mode = str(config.get("execution_mode") or row_mode).strip().upper()
        provider = str(config.get("provider") or config.get("venue") or ("simulated" if mode == "PAPER" else "gate")).strip().lower()
        environment = str(config.get("environment") or mode.lower()).strip().lower()
    return {
        "account_id": str(row["account_id"]),
        "mode": mode,
        "provider": provider,
        "venue": provider,
        "environment": environment,
        "account_type": account_type or ("SIMULATED" if provider == "simulated" else mode),
    }


def effective_mode(store: Any, account_id: str, fallback: str | None = None) -> str | None:
    scope = resolve_account_scope(store, account_id)
    return scope["mode"] if scope else (str(fallback).upper() if fallback else None)


__all__ = ["effective_mode", "resolve_account_scope"]
