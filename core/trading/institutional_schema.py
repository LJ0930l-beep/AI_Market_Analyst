"""Additive schema for the institutional AI trading workflow.

The product already has a durable execution ledger.  This module only adds
the workflow facts which were missing from that ledger: strategy candidates,
calibration runs/profiles, account-scoped decision memory, and the timing
fields needed to audit an aligned scan.  It is deliberately connection based
so it can be used by SQLite initialization and by isolated test stores without
creating a storage-module import cycle.
"""

from __future__ import annotations

import sqlite3


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_columns(db: sqlite3.Connection, table: str, additions: dict[str, str]) -> None:
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is None:
        # Ledger-owned tables are created lazily by AccountLedger,
        # ExecutionGateway, or AILedDecisionEngine on a few compatibility
        # paths.  The workflow tables above can still be initialized first;
        # the next owner invocation will call this helper again.
        return
    present = _columns(db, table)
    for name, definition in additions.items():
        if name not in present:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _candidate_unique_keys(db: sqlite3.Connection) -> set[tuple[str, ...]]:
    indexes = db.execute("PRAGMA index_list(ai_strategy_candidates)").fetchall()
    unique_keys: set[tuple[str, ...]] = set()
    for index in indexes:
        if not bool(index[2]):
            continue
        name = str(index[1]).replace('"', '""')
        columns = db.execute(f'PRAGMA index_info("{name}")').fetchall()
        unique_keys.add(tuple(str(column[2]) for column in columns))
    return unique_keys


def _migrate_candidate_timeframe_identity(db: sqlite3.Connection) -> None:
    """Replace the legacy candidate key with a timeframe-aware unique key.

    Older releases keyed candidates by account/symbol/rule/closed bar only.
    That aliases a 5m and 15m signal when both close at the same timestamp.
    Keep every historical row and rebuild the table transactionally.
    """
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_strategy_candidates'"
    ).fetchone() is None:
        return
    unique_keys = _candidate_unique_keys(db)
    legacy_key = ("account_id", "symbol", "strategy_id", "closed_15m_bar")
    if legacy_key not in unique_keys:
        return

    column_names = (
        "candidate_id", "account_id", "provider", "environment", "symbol",
        "strategy_id", "strategy_version", "signal_timeframe", "closed_15m_bar",
        "status", "side", "entry_price", "stop_price", "take_profit", "rule_score",
        "calibrated_probability", "calibration_sample_size", "rationale", "source_hash",
        "conditions_json", "trigger_completion_pct", "entry_zone_json", "invalidation",
        "targets_json", "rr", "evidence_json", "signal_time", "expires_at",
        "context_timeframe", "market_regime", "direction_bias", "trigger_status",
        "context_json", "created_at", "updated_at",
    )
    defaults = {
        "provider": "'unknown'", "environment": "'unknown'",
        "strategy_version": "'legacy'", "signal_timeframe": "'15m'",
        "status": "'UNKNOWN'", "side": "NULL", "entry_price": "NULL",
        "stop_price": "NULL", "take_profit": "NULL", "rule_score": "NULL",
        "calibrated_probability": "NULL", "calibration_sample_size": "0",
        "rationale": "''", "source_hash": "''", "conditions_json": "'[]'",
        "trigger_completion_pct": "NULL", "entry_zone_json": "NULL",
        "invalidation": "NULL", "targets_json": "'[]'", "rr": "NULL",
        "evidence_json": "'[]'", "signal_time": "NULL", "expires_at": "NULL",
        "context_timeframe": "NULL", "market_regime": "NULL",
        "direction_bias": "NULL", "trigger_status": "NULL", "context_json": "'{}'",
        # Missing audit timestamps stay explicit instead of inventing a point
        # in time for old records whose schema did not retain those fields.
        "created_at": "'UNKNOWN'", "updated_at": "'UNKNOWN'",
    }
    source_columns = _columns(db, "ai_strategy_candidates")
    select_expressions = []
    for name in column_names:
        if name in source_columns:
            select_expressions.append(name)
        elif name in defaults:
            select_expressions.append(f"{defaults[name]} AS {name}")
        else:
            raise sqlite3.OperationalError(
                f"Cannot migrate ai_strategy_candidates: required legacy column {name!r} is missing"
            )
    columns_sql = ", ".join(column_names)
    select_sql = ", ".join(select_expressions)
    db.execute("SAVEPOINT candidate_timeframe_identity")
    try:
        db.execute(
            """CREATE TABLE ai_strategy_candidates__timeframe_new (
                candidate_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                environment TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                signal_timeframe TEXT NOT NULL DEFAULT '15m',
                closed_15m_bar TEXT NOT NULL,
                status TEXT NOT NULL,
                side TEXT,
                entry_price REAL,
                stop_price REAL,
                take_profit REAL,
                rule_score REAL,
                calibrated_probability REAL,
                calibration_sample_size INTEGER NOT NULL DEFAULT 0,
                rationale TEXT NOT NULL DEFAULT '',
                source_hash TEXT NOT NULL,
                conditions_json TEXT NOT NULL DEFAULT '[]',
                trigger_completion_pct REAL,
                entry_zone_json TEXT,
                invalidation TEXT,
                targets_json TEXT NOT NULL DEFAULT '[]',
                rr REAL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                signal_time TEXT,
                expires_at TEXT,
                context_timeframe TEXT,
                market_regime TEXT,
                direction_bias TEXT,
                trigger_status TEXT,
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(account_id, symbol, strategy_id, signal_timeframe, closed_15m_bar)
            )"""
        )
        db.execute(
            f"INSERT INTO ai_strategy_candidates__timeframe_new ({columns_sql}) "
            f"SELECT {select_sql} FROM ai_strategy_candidates"
        )
        db.execute("DROP TABLE ai_strategy_candidates")
        db.execute(
            "ALTER TABLE ai_strategy_candidates__timeframe_new RENAME TO ai_strategy_candidates"
        )
        db.execute("RELEASE SAVEPOINT candidate_timeframe_identity")
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT candidate_timeframe_identity")
        db.execute("RELEASE SAVEPOINT candidate_timeframe_identity")
        raise


def ensure_institutional_trader_schema(db: sqlite3.Connection) -> None:
    """Create or extend the workflow tables idempotently.

    No existing table is dropped or rewritten.  JSON payloads intentionally
    remain alongside normalized columns: older builds can still read the
    execution rows while newer projections can filter without parsing JSON.
    """

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ai_strategy_candidates (
            candidate_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            signal_timeframe TEXT NOT NULL,
            closed_15m_bar TEXT NOT NULL,
            status TEXT NOT NULL,
            side TEXT,
            entry_price REAL,
            stop_price REAL,
            take_profit REAL,
            rule_score REAL,
            calibrated_probability REAL,
            calibration_sample_size INTEGER NOT NULL DEFAULT 0,
            rationale TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL,
            conditions_json TEXT NOT NULL DEFAULT '[]',
            trigger_completion_pct REAL,
            entry_zone_json TEXT,
            invalidation TEXT,
            targets_json TEXT NOT NULL DEFAULT '[]',
            rr REAL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            signal_time TEXT,
            expires_at TEXT,
            context_timeframe TEXT,
            market_regime TEXT,
            direction_bias TEXT,
            trigger_status TEXT,
            context_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id, symbol, strategy_id, signal_timeframe, closed_15m_bar)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_candidates_scope_time
            ON ai_strategy_candidates(account_id, environment, closed_15m_bar DESC, symbol, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_ai_candidates_status
            ON ai_strategy_candidates(account_id, status, updated_at DESC);

        -- The remote Gate TestNet account is the authority for account
        -- economics.  This append-only snapshot table is a local mirror and
        -- audit trail; it is never populated from the local ledger.
        CREATE TABLE IF NOT EXISTS gate_remote_account_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            equity TEXT,
            available_margin TEXT,
            used_margin TEXT,
            unrealized_pnl TEXT,
            realized_pnl TEXT,
            balance_json TEXT NOT NULL DEFAULT '{}',
            positions_json TEXT NOT NULL DEFAULT '[]',
            pending_orders_json TEXT NOT NULL DEFAULT '[]',
            fills_json TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL,
            endpoint TEXT,
            raw_hash TEXT NOT NULL,
            error_code TEXT,
            message_zh TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gate_remote_account_snapshots_scope
            ON gate_remote_account_snapshots(account_id, observed_at DESC, snapshot_id DESC);

        CREATE TABLE IF NOT EXISTS gate_testnet_e2e_runs (
            run_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            current_stage TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            result_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(account_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_testnet_e2e_runs_scope
            ON gate_testnet_e2e_runs(account_id, started_at DESC);

        CREATE TABLE IF NOT EXISTS ai_cycle_stages (
            stage_id TEXT PRIMARY KEY,
            cycle_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            duration_ms REAL,
            reason_code TEXT,
            human_message TEXT,
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(cycle_id, stage)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_cycle_stages_scope
            ON ai_cycle_stages(account_id, cycle_id, sequence);

        CREATE TABLE IF NOT EXISTS ai_calibration_runs (
            run_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            session_id TEXT,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_bars INTEGER NOT NULL,
            used_bars INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            model_id TEXT,
            model_digest TEXT,
            input_hash TEXT,
            profile_id TEXT,
            error_code TEXT,
            result_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_ai_calibration_runs_scope
            ON ai_calibration_runs(account_id, environment, started_at DESC);

        CREATE TABLE IF NOT EXISTS ai_calibration_profiles (
            profile_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            profile_version TEXT NOT NULL,
            model_id TEXT NOT NULL,
            model_digest TEXT,
            digest_status TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            sample_size INTEGER NOT NULL,
            calibrated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            profile_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(account_id, environment, profile_version, input_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_calibration_profiles_active
            ON ai_calibration_profiles(account_id, environment, active, expires_at DESC);

        CREATE TABLE IF NOT EXISTS ai_decision_memory (
            memory_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            session_id TEXT,
            cycle_id TEXT NOT NULL,
            candidate_id TEXT,
            symbol TEXT,
            action TEXT NOT NULL,
            cycle_status TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            summary_zh TEXT NOT NULL,
            lesson_zh TEXT,
            outcome_status TEXT,
            outcome_pnl REAL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id, cycle_id)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_memory_scope_time
            ON ai_decision_memory(account_id, environment, decision_at DESC, memory_id DESC);

        CREATE TABLE IF NOT EXISTS ai_scan_runs (
            scan_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            candidate_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(account_id, scheduled_at)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_scan_runs_scope
            ON ai_scan_runs(account_id, scheduled_at DESC);

        -- Gate native public-data closure.  These tables are append-only
        -- evidence tables; payload_json keeps the exact exchange row while
        -- normalized columns make freshness, sequence and replay checks
        -- queryable without parsing JSON.
        CREATE TABLE IF NOT EXISTS gate_bootstrap_runs (
            bootstrap_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            native_symbol TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            source_hash TEXT NOT NULL,
            closed_15m_bars INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(provider, environment, symbol, source_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_bootstrap_scope
            ON gate_bootstrap_runs(environment, symbol, completed_at DESC);

        CREATE TABLE IF NOT EXISTS gate_derivative_bars (
            row_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            native_symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            price_type TEXT NOT NULL,
            bar_start TEXT NOT NULL,
            bar_end TEXT NOT NULL,
            event_time TEXT NOT NULL,
            available_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            is_closed INTEGER NOT NULL,
            raw_hash TEXT NOT NULL,
            quality_status TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(provider, environment, native_symbol, timeframe, price_type, bar_start, revision_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_derivative_bars_lookup
            ON gate_derivative_bars(symbol, timeframe, price_type, bar_start DESC);

        CREATE TABLE IF NOT EXISTS gate_order_book_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            native_symbol TEXT NOT NULL,
            sequence TEXT,
            event_at TEXT,
            received_at TEXT NOT NULL,
            source TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            quality_status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(provider, environment, native_symbol, sequence, raw_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_order_book_snapshots_lookup
            ON gate_order_book_snapshots(symbol, received_at DESC);

        CREATE TABLE IF NOT EXISTS gate_order_book_deltas (
            delta_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            native_symbol TEXT NOT NULL,
            first_sequence TEXT,
            last_sequence TEXT,
            event_at TEXT,
            received_at TEXT NOT NULL,
            sequence_status TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(provider, environment, native_symbol, first_sequence, last_sequence, raw_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_order_book_deltas_lookup
            ON gate_order_book_deltas(symbol, received_at DESC);

        CREATE TABLE IF NOT EXISTS gate_trades (
            trade_row_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            native_symbol TEXT NOT NULL,
            trade_id TEXT NOT NULL,
            event_at TEXT,
            received_at TEXT NOT NULL,
            side TEXT,
            price REAL,
            size REAL,
            raw_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(provider, environment, native_symbol, trade_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gate_trades_lookup
            ON gate_trades(symbol, event_at DESC, trade_id DESC);

        CREATE TABLE IF NOT EXISTS gate_liquidations (
            event_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            native_symbol TEXT NOT NULL,
            event_at TEXT,
            received_at TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(provider, environment, native_symbol, raw_hash)
        );

        CREATE TABLE IF NOT EXISTS gate_funding_history (
            row_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            native_symbol TEXT NOT NULL,
            event_at TEXT NOT NULL,
            funding_rate REAL NOT NULL,
            received_at TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(provider, environment, native_symbol, event_at, raw_hash)
        );

        CREATE TABLE IF NOT EXISTS gate_open_interest (
            row_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            native_symbol TEXT NOT NULL,
            event_at TEXT NOT NULL,
            open_interest REAL NOT NULL,
            received_at TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(provider, environment, native_symbol, event_at, raw_hash)
        );

        CREATE TABLE IF NOT EXISTS onchain_flow_events (
            event_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_event_id TEXT,
            asset TEXT,
            amount REAL,
            amount_usd REAL,
            direction TEXT NOT NULL,
            from_label TEXT,
            to_label TEXT,
            transaction_hash TEXT,
            event_at TEXT,
            received_at TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(provider, raw_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_onchain_flow_time
            ON onchain_flow_events(COALESCE(event_at, received_at) DESC, provider);

        -- Normalized inbound alerts from user-configured Whale Alert or
        -- Arkham webhooks.  Raw provider payloads stay available for audit,
        -- while the nullable normalized fields prevent the UI from guessing
        -- an amount, asset, direction, or address when a provider omits it.
        CREATE TABLE IF NOT EXISTS onchain_flow_events (
            event_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_event_id TEXT,
            asset TEXT,
            amount REAL,
            amount_usd REAL,
            direction TEXT,
            from_label TEXT,
            to_label TEXT,
            transaction_hash TEXT,
            event_at TEXT,
            received_at TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(provider, raw_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_onchain_flow_events_time
            ON onchain_flow_events(event_at DESC, received_at DESC);

        CREATE TABLE IF NOT EXISTS indicator_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            bar_end TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            data_source_hash TEXT NOT NULL,
            indicator_version TEXT NOT NULL,
            quality_status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(symbol, timeframe, bar_end, indicator_version, data_source_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_indicator_snapshots_lookup
            ON indicator_snapshots(symbol, timeframe, bar_end DESC);

        CREATE TABLE IF NOT EXISTS ai_runtime_diagnostics (
            diagnostic_id TEXT PRIMARY KEY,
            account_id TEXT,
            session_id TEXT,
            cycle_id TEXT,
            state TEXT NOT NULL,
            code TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_ai_runtime_diagnostics_scope
            ON ai_runtime_diagnostics(account_id, occurred_at DESC);
        """
    )

    # Additive fields on the pre-existing execution and cycle ledgers.
    _add_columns(
        db,
        "ai_strategy_candidates",
        {
            "signal_timeframe": "TEXT NOT NULL DEFAULT '15m'",
            "conditions_json": "TEXT NOT NULL DEFAULT '[]'",
            "trigger_completion_pct": "REAL",
            "entry_zone_json": "TEXT",
            "invalidation": "TEXT",
            "targets_json": "TEXT NOT NULL DEFAULT '[]'",
            "rr": "REAL",
            "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
            "signal_time": "TEXT",
            "expires_at": "TEXT",
            "context_timeframe": "TEXT",
            "market_regime": "TEXT",
            "direction_bias": "TEXT",
            "trigger_status": "TEXT",
        },
    )
    db.execute(
        "UPDATE ai_strategy_candidates SET signal_timeframe='15m' "
        "WHERE signal_timeframe IS NULL OR TRIM(signal_timeframe)=''"
    )
    _migrate_candidate_timeframe_identity(db)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_candidates_scope_time "
        "ON ai_strategy_candidates(account_id, environment, closed_15m_bar DESC, symbol, strategy_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_candidates_status "
        "ON ai_strategy_candidates(account_id, status, updated_at DESC)"
    )
    _add_columns(
        db,
        "order_intents",
        {
            "provider": "TEXT",
            "candidate_id": "TEXT",
            "closed_15m_bar": "TEXT",
            "order_preference": "TEXT NOT NULL DEFAULT 'AUTO'",
            "final_order_type": "TEXT",
            "selection_reason_code": "TEXT",
            "selection_reason": "TEXT",
            "selection_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            "limit_price": "REAL",
            "ttl_seconds": "INTEGER",
            "selection_policy_version": "TEXT",
            "remote_order_status": "TEXT",
        },
    )
    _add_columns(
        db,
        "ai_led_cycles",
        {
            "scheduled_at": "TEXT",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "lag_ms": "REAL",
            "duration_ms": "REAL",
            "next_scan_at": "TEXT",
            "calibration_state": "TEXT",
            "candidate_count": "INTEGER NOT NULL DEFAULT 0",
            "decision_memory_id": "TEXT",
            "provider": "TEXT",
            "environment": "TEXT",
            "operational_state": "TEXT",
            "decision_origin": "TEXT",
            "model_call_status": "TEXT",
            "model_called": "INTEGER NOT NULL DEFAULT 0",
            "model_result": "TEXT",
            "block_stage": "TEXT",
            "human_message": "TEXT",
            "stage_trace_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    )
    _add_columns(
        db,
        "ai_calibration_runs",
        {
            "parse_phase": "TEXT",
            "latency_ms": "REAL",
            "raw_response": "TEXT",
            "schema_version": "TEXT",
        },
    )
    _add_columns(
        db,
        "ai_decision_memory",
        {
            "decision_origin": "TEXT NOT NULL DEFAULT 'MODEL'",
        },
    )
    _add_columns(
        db,
        "trade_fills",
        {
            "provider": "TEXT",
            "environment": "TEXT",
            "candidate_id": "TEXT",
            "cycle_id": "TEXT",
            "strategy_id": "TEXT",
            "strategy_version": "TEXT",
            "fee_source": "TEXT",
        },
    )
    _add_columns(
        db,
        "simulated_positions",
        {
            "provider": "TEXT",
            "environment": "TEXT",
            "candidate_id": "TEXT",
            "cycle_id": "TEXT",
            "strategy_id": "TEXT",
            "strategy_version": "TEXT",
        },
    )
    db.commit()


__all__ = ["ensure_institutional_trader_schema"]
