"""Additive V2 persistence; no mutation of Prediction/Outcome/PaperTrade semantics."""

import json
from datetime import datetime, timezone


def _table_columns(db, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(db, table: str, column: str, definition: str) -> bool:
    if column in _table_columns(db, table):
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


def migrate(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS strategy_subscriptions (
      symbol TEXT NOT NULL, strategy_id TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
      params_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
      PRIMARY KEY(symbol,strategy_id));
    CREATE TABLE IF NOT EXISTS macro_events (
      event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS agent_trade_decisions (
      decision_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, status TEXT NOT NULL,
      payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS simulated_positions (
      position_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, status TEXT NOT NULL,
      payload_json TEXT NOT NULL, updated_at TEXT NOT NULL,
      account_id TEXT, venue TEXT NOT NULL DEFAULT 'simulated',
      mode TEXT NOT NULL DEFAULT 'PAPER', position_version INTEGER NOT NULL DEFAULT 0,
      protection_status TEXT NOT NULL DEFAULT 'UNKNOWN', legacy_unverified INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS simulation_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL,
      payload_json TEXT NOT NULL, created_at TEXT NOT NULL, event_identity TEXT);
    CREATE TABLE IF NOT EXISTS trade_fills (
      fill_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, venue TEXT NOT NULL,
      mode TEXT NOT NULL, order_id TEXT, trade_id TEXT, position_id TEXT, symbol TEXT NOT NULL,
      side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL,
      fee TEXT NOT NULL DEFAULT '0', fee_amount TEXT NOT NULL DEFAULT '0',
      fee_currency TEXT NOT NULL DEFAULT 'USDT', fx_rate TEXT,
      contract_size TEXT NOT NULL DEFAULT '1', status TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
      event_at TEXT);
    """)
    # Older V2 databases created the five-column position table before the
    # execution repair.  Additive migration is intentional: existing rows are
    # never assigned to an account based on a symbol or account-name guess.
    position_was_extended = False
    for column, definition in (
        ("account_id", "TEXT"),
        ("venue", "TEXT NOT NULL DEFAULT 'simulated'"),
        ("mode", "TEXT NOT NULL DEFAULT 'PAPER'"),
        ("position_version", "INTEGER NOT NULL DEFAULT 0"),
        ("protection_status", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
        ("legacy_unverified", "INTEGER NOT NULL DEFAULT 0"),
    ):
        position_was_extended = _add_column(db, "simulated_positions", column, definition) or position_was_extended
    _add_column(db, "simulation_events", "event_identity", "TEXT")
    for column, definition in (
        ("trade_id", "TEXT"),
        ("fee_amount", "TEXT NOT NULL DEFAULT '0'"),
        ("fee_currency", "TEXT NOT NULL DEFAULT 'USDT'"),
        ("fx_rate", "TEXT"),
        ("contract_size", "TEXT NOT NULL DEFAULT '1'"),
        ("event_at", "TEXT"),
    ):
        _add_column(db, "trade_fills", column, definition)
    db.execute("UPDATE trade_fills SET fee_amount=fee WHERE fee_amount IS NULL OR fee_amount='0' AND fee!='0'")
    if position_was_extended:
        db.execute("UPDATE simulated_positions SET legacy_unverified=1, protection_status='UNKNOWN'")
    db.execute("CREATE INDEX IF NOT EXISTS idx_positions_account_scope ON simulated_positions(account_id, venue, mode, status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_trade_fills_scope ON trade_fills(account_id, venue, mode, symbol, created_at)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_simulation_events_identity ON simulation_events(event_identity) WHERE event_identity IS NOT NULL")
    db.execute(
        "INSERT OR IGNORE INTO schema_migrations VALUES(14,?)",
        (datetime.now(timezone.utc).isoformat(),),
    )


class V2Store:
    def list_strategy_subscriptions(self, enabled_only=False):
        with self._connect() as db:
            rows = db.execute(
                """SELECT s.* FROM strategy_subscriptions s
                JOIN watchlist_entries w ON s.symbol=w.symbol
                WHERE (?=0 OR s.enabled=1) ORDER BY s.symbol,s.strategy_id""",
                (int(enabled_only),),
            ).fetchall()
        return [
            {
                **dict(row),
                "enabled": bool(row["enabled"]),
                "params": json.loads(row["params_json"]),
            }
            for row in rows
        ]

    def set_strategy_subscription(self, symbol, strategy_id, enabled, params):
        from .quant.strategies import STRATEGIES

        if strategy_id not in STRATEGIES or type(enabled) is not bool:
            raise ValueError("invalid strategy or enabled flag")
        STRATEGIES[strategy_id](
            params
        )  # validates bounded parameters before persistence
        with self._connect() as db:
            if not db.execute(
                "SELECT 1 FROM watchlist_entries WHERE symbol=?", (symbol,)
            ).fetchone():
                raise ValueError("watchlist membership required")
            count = db.execute(
                "SELECT COUNT(DISTINCT symbol) FROM strategy_subscriptions WHERE enabled=1 AND symbol != ?",
                (symbol,),
            ).fetchone()[0]
            if enabled and count >= 50:
                raise ValueError("50 symbol resource limit")
            if enabled:
                db.execute(
                    "UPDATE strategy_subscriptions SET enabled=0 WHERE symbol=?",
                    (symbol,),
                )
            db.execute(
                "INSERT INTO strategy_subscriptions VALUES(?,?,?,?,?) ON CONFLICT(symbol,strategy_id) DO UPDATE SET enabled=excluded.enabled,params_json=excluded.params_json,updated_at=excluded.updated_at",
                (
                    symbol,
                    strategy_id,
                    int(enabled),
                    json.dumps(params, allow_nan=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def v2_records(self, table, limit=100):
        if table not in {
            "macro_events",
            "agent_trade_decisions",
            "simulated_positions",
            "simulation_events",
        }:
            raise ValueError("invalid ledger")
        with self._connect() as db:
            rows = db.execute(
                f"SELECT payload_json FROM {table} ORDER BY rowid DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
