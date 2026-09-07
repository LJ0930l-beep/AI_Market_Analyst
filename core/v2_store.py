"""Additive V2 persistence; no mutation of Prediction/Outcome/PaperTrade semantics."""

import json
from datetime import datetime, timezone


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
      payload_json TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS simulation_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT NOT NULL,
      payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
    """)
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
