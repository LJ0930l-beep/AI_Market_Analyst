"""Small durable SQLite store for Prediction/PaperTrade/Outcome separation."""

from __future__ import annotations

import json
import hashlib
import math
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..instruments import Instrument, canonical_instrument_key, instrument_for, instrument_from_payload
from ..news_engine import NewsFetchResult
from ..outcomes.engine import Outcome
from ..providers.news import NewsEvent
from ..providers.runtime import ProviderSnapshot
from ..signals.schema import SignalProposal
from ..v2_store import V2Store, migrate as migrate_v2


_MONITORING_TRIGGER_ALIASES = {
    "REGIME": "REGIME_CHANGE",
    "REGIME_CHANGE": "REGIME_CHANGE",
    "BREAKOUT": "BREAKOUT",
    "BREAKDOWN": "BREAKDOWN",
    "VOLUME": "VOLUME_EXPANSION",
    "VOLUME_EXPANSION": "VOLUME_EXPANSION",
    "VOLATILITY": "VOLATILITY_EXPANSION",
    "VOLATILITY_EXPANSION": "VOLATILITY_EXPANSION",
    "LEVEL_PROXIMITY": "LEVEL_PROXIMITY",
    "INVALIDATION": "SIGNAL_INVALIDATION",
    "SIGNAL_INVALIDATION": "SIGNAL_INVALIDATION",
    "EVENT_RISK": "EVENT_RISK",
    "NEWS_SHOCK": "NEWS_SHOCK",
}


def _normalize_monitoring_trigger(value: object) -> str:
    normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return _MONITORING_TRIGGER_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported monitoring trigger type: {value}") from exc


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


APP_SETTING_DEFINITIONS: dict[str, dict[str, object]] = {
    "simulation.allow_unknown_macro": {
        "default": False, "value_type": "boolean",
        "description": "Explicit simulation-only permission to proceed when macro evidence is unavailable; never overrides an active directional block.",
    },
    "scheduler.enabled": {
        "default": False,
        "value_type": "boolean",
        "description": "Explicit local scheduler opt-in; start remains a separate lifecycle action.",
    },
    "scheduler.interval_seconds": {
        "default": 900,
        "value_type": "integer",
        "minimum": 60,
        "maximum": 86_400,
        "description": "Local Watchlist scan interval used by the explicit scheduler lifecycle.",
    },
    "scheduler.concurrency": {
        "default": 1,
        "value_type": "integer",
        "minimum": 1,
        "maximum": 4,
        "description": "Requested local scan concurrency; model analysis is capped at one.",
    },
    "scheduler.session_policy": {
        "default": "market_hours",
        "value_type": "string",
        "allowed_values": ("market_hours", "always"),
        "description": "Local Watchlist scan session policy; market_hours is the conservative default.",
    },
    "ui.language": {
        "default": "en",
        "value_type": "string",
        "allowed_values": ("en", "zh-CN"),
        "description": "Preferred display language for this local workstation.",
    },
    "ai.response_language": {
        "default": "follow_ui",
        "value_type": "string",
        "allowed_values": ("follow_ui", "en", "zh-CN"),
        "description": "Preferred language for explicit local Qwen responses.",
    },
    "notifications.language": {
        "default": "en",
        "value_type": "string",
        "allowed_values": ("en", "zh-CN"),
        "description": "Display language reserved for local alerts; no external notifier is enabled.",
    },
    "ai.model_preference": {
        "default": "auto",
        "value_type": "string",
        "allowed_values": ("auto", "fast", "smart"),
        "description": "Deterministic server-side Qwen tier preference for explicit AI requests.",
    },
    "desktop.close_to_tray": {
        "default": False,
        "value_type": "boolean",
        "description": "Keep the desktop window hidden in the system tray when its close action is used.",
    },
    "desktop.auto_start": {
        "default": False,
        "value_type": "boolean",
        "description": "Start the desktop application with Windows only after explicit user opt-in.",
    },
    "monitoring.resume": {
        "default": False,
        "value_type": "boolean",
        "description": "Resume explicitly enabled monitoring policies after a user-started application session.",
    },
    "market_hydration.enabled": {
        "default": True,
        "value_type": "boolean",
        "description": "Allow the sidecar to refresh public market/news cache without invoking a model or monitoring policy.",
    },
    "market_hydration.defaults_seeded": {
        "default": False,
        "value_type": "boolean",
        "description": "Internal one-time marker so a user-deleted default public watchlist is not recreated on restart.",
    },
}

APP_SETTING_DEFAULTS: dict[str, object] = {
    key: definition["default"] for key, definition in APP_SETTING_DEFINITIONS.items()
}


def validate_app_setting_value(key: str, value: Any) -> Any:
    definition = APP_SETTING_DEFINITIONS.get(key)
    if definition is None:
        raise ValueError(f"unsupported app setting: {key}")

    value_type = definition["value_type"]
    if value_type == "boolean" and type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    if value_type == "integer":
        if type(value) is not int:
            raise ValueError(f"{key} must be an integer")
        minimum = int(definition["minimum"])
        maximum = int(definition["maximum"])
        if value < minimum or value > maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
    if value_type == "string":
        if type(value) is not str:
            raise ValueError(f"{key} must be a string")
        allowed_values = definition.get("allowed_values", ())
        if value not in allowed_values:
            raise ValueError(f"{key} must be one of: {', '.join(str(item) for item in allowed_values)}")
    return value


class SQLiteStore(V2Store):
    def __init__(self, path: str | Path = "data/market_analyst.sqlite3") -> None:
        self.path = Path(path)
        self._memory_connection: sqlite3.Connection | None = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if str(self.path) == ":memory:":
            if self._memory_connection is None:
                self._memory_connection = sqlite3.connect(":memory:")
                self._memory_connection.row_factory = sqlite3.Row
            try:
                yield self._memory_connection
                self._memory_connection.commit()
            finally:
                pass
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        # Preserve a consistent pre-V2 image before additive schema changes.
        if str(self.path) != ":memory:" and self.path.exists():
            with self._connect() as source:
                exists = source.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
                version = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] if exists else 0
                backup = self.path.with_name(self.path.name + ".pre-v2.bak")
                if (version or 0) < 14 and not backup.exists():
                    with closing(sqlite3.connect(str(backup))) as destination:
                        source.backup(destination)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instruments (
                    symbol TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    prediction_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    model_id TEXT,
                    model_version TEXT,
                    prompt_version TEXT,
                    input_hash TEXT,
                    data_as_of TEXT,
                    context_json TEXT,
                    raw_model_response TEXT,
                    parse_status TEXT
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    prediction_id TEXT PRIMARY KEY,
                    followed_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id)
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    prediction_id TEXT PRIMARY KEY,
                    settled_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id)
                );
                CREATE TABLE IF NOT EXISTS news_events (
                    event_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    data_as_of TEXT NOT NULL,
                    stale INTEGER NOT NULL,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS model_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_id TEXT,
                    model_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    latency_ms REAL,
                    input_tokens_est INTEGER,
                    output_chars INTEGER,
                    success INTEGER NOT NULL,
                    error_code TEXT
                );
                """
            )
            self._ensure_prediction_columns(db)
            self._ensure_phase3_tables(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (4, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_phase5_tables(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (5, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_phase5_scheduler_tables(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (6, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_phase5_settlement_columns(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (7, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_phase5_alert_tables(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (8, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_phase6_tables(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (9, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_phase6_memory_provenance_columns(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (10, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_v11_tables(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (11, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_v12_tables(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (12, datetime.now(timezone.utc).isoformat()),
            )
            self._ensure_v13_tables(db)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (13, datetime.now(timezone.utc).isoformat()),
            )
            migrate_v2(db)
            self._ensure_institutional_tables(db)
            # The AI-led workflow is an additive, independently idempotent
            # schema.  Keep it outside the numbered compatibility migrations
            # so copied databases can be upgraded without rewriting legacy
            # evidence or changing the desktop schema contract.
            from ..trading.institutional_schema import ensure_institutional_trader_schema
            ensure_institutional_trader_schema(db)

    @staticmethod
    def _ensure_v11_tables(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_briefs (
                brief_id TEXT PRIMARY KEY,
                generated_at TEXT NOT NULL,
                as_of TEXT NOT NULL,
                language TEXT NOT NULL,
                model_id TEXT NOT NULL,
                model_tier TEXT NOT NULL,
                route_reason TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                missing_json TEXT NOT NULL,
                content TEXT NOT NULL,
                capability_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_daily_briefs_generated
                ON daily_briefs(generated_at DESC, brief_id DESC);
            """
        )

    @staticmethod
    def _ensure_v12_tables(db: sqlite3.Connection) -> None:
        """Create additive V1.2 crypto monitoring and localized evidence tables."""

        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS monitoring_policies (
                instrument_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                primary_timeframe TEXT NOT NULL DEFAULT '15m',
                context_timeframe TEXT NOT NULL DEFAULT '1h',
                trigger_types_json TEXT NOT NULL DEFAULT '[]',
                min_trigger_score REAL NOT NULL DEFAULT 0.65,
                ai_min_confidence REAL NOT NULL DEFAULT 0.60,
                cooldown_minutes INTEGER NOT NULL DEFAULT 60,
                quiet_hours_json TEXT NOT NULL DEFAULT '{}',
                notify_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_monitoring_policies_enabled
                ON monitoring_policies(enabled, updated_at DESC, instrument_id ASC);

            CREATE TABLE IF NOT EXISTS market_bars (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bar_start TEXT NOT NULL,
                bar_end TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                provider TEXT NOT NULL,
                data_as_of TEXT NOT NULL,
                is_closed INTEGER NOT NULL DEFAULT 0,
                received_at TEXT NOT NULL,
                PRIMARY KEY(symbol, timeframe, bar_start)
            );
            CREATE INDEX IF NOT EXISTS idx_market_bars_lookup
                ON market_bars(symbol, timeframe, bar_start DESC);

            CREATE TABLE IF NOT EXISTS market_realtime_state (
                symbol TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                price REAL,
                change_pct REAL,
                volume REAL,
                last_trade_at TEXT,
                data_as_of TEXT,
                freshness_status TEXT NOT NULL,
                stale_after_seconds INTEGER NOT NULL,
                reconnect_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_realtime_state_freshness
                ON market_realtime_state(freshness_status, updated_at DESC, symbol ASC);

            CREATE TABLE IF NOT EXISTS bar_close_ledger (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bar_start TEXT NOT NULL,
                bar_end TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY(symbol, timeframe, bar_start)
            );

            CREATE TABLE IF NOT EXISTS trigger_events (
                trigger_event_id TEXT PRIMARY KEY,
                instrument_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bar_start TEXT NOT NULL,
                bar_end TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_score REAL NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                policy_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                analysis_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trigger_events_symbol_time
                ON trigger_events(instrument_id, timeframe, bar_start DESC, trigger_type ASC);
            CREATE INDEX IF NOT EXISTS idx_trigger_events_status
                ON trigger_events(status, created_at DESC, trigger_event_id ASC);

            CREATE TABLE IF NOT EXISTS opportunity_analyses (
                analysis_id TEXT PRIMARY KEY,
                trigger_event_id TEXT NOT NULL UNIQUE,
                instrument_id TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bias TEXT NOT NULL,
                confidence REAL NOT NULL,
                model_id TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                data_as_of TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                raw_model_response TEXT,
                validator_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(trigger_event_id) REFERENCES trigger_events(trigger_event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_opportunity_analyses_symbol
                ON opportunity_analyses(instrument_id, created_at DESC, analysis_id ASC);

            CREATE TABLE IF NOT EXISTS chart_annotations (
                annotation_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                annotation_type TEXT NOT NULL,
                bar_start TEXT,
                price REAL,
                label TEXT,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(symbol, timeframe, annotation_type, bar_start, price, label)
            );
            CREATE INDEX IF NOT EXISTS idx_chart_annotations_lookup
                ON chart_annotations(symbol, timeframe, created_at DESC, annotation_id ASC);

            CREATE TABLE IF NOT EXISTS localized_news_artifacts (
                news_id TEXT NOT NULL,
                locale TEXT NOT NULL,
                source_language TEXT NOT NULL,
                original_title TEXT NOT NULL,
                original_summary TEXT,
                translated_title_zh TEXT,
                translated_summary_zh TEXT,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                source_hash TEXT NOT NULL,
                model_id TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                translated_at TEXT NOT NULL,
                numeric_guard_passed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                PRIMARY KEY(news_id, locale)
            );
            CREATE INDEX IF NOT EXISTS idx_localized_news_lookup
                ON localized_news_artifacts(news_id, locale, translated_at DESC);
            """
        )

    @staticmethod
    def _ensure_v13_tables(db: sqlite3.Connection) -> None:
        """Create the additive public-hydration evidence ledger."""

        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS public_hydration_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                providers_json TEXT NOT NULL DEFAULT '{}',
                errors_json TEXT NOT NULL DEFAULT '[]',
                cache_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_public_hydration_runs_started
                ON public_hydration_runs(started_at DESC, run_id DESC);
            """
        )

    @staticmethod
    def _ensure_institutional_tables(db: sqlite3.Connection) -> None:
        """Add the audit-era v3 evidence tables without changing schema 14.

        ``schema_migrations`` is intentionally left at the stable v1/v2
        version.  The institutional contract has its own additive migration
        ledger so existing desktop compatibility checks remain valid while a
        copied database can be migrated and re-opened idempotently.
        """

        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS institutional_migration_runs (
                migration_key TEXT PRIMARY KEY,
                source_schema TEXT NOT NULL,
                target_schema TEXT NOT NULL,
                source_rows INTEGER NOT NULL DEFAULT 0,
                target_rows INTEGER NOT NULL DEFAULT 0,
                duplicate_rows INTEGER NOT NULL DEFAULT 0,
                orphan_rows INTEGER NOT NULL DEFAULT 0,
                source_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                report_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS market_bar_versions (
                instrument_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                venue TEXT NOT NULL,
                market_type TEXT NOT NULL,
                native_symbol TEXT NOT NULL,
                settle_currency TEXT NOT NULL,
                price_type TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bar_start TEXT NOT NULL,
                bar_end TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                volume_unit TEXT,
                event_time TEXT NOT NULL,
                first_received_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                source TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                quality_status TEXT NOT NULL,
                is_closed INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(instrument_key, timeframe, bar_start, revision_id)
            );
            CREATE INDEX IF NOT EXISTS idx_market_bar_versions_latest
                ON market_bar_versions(instrument_key, timeframe, bar_start DESC, available_at DESC);
            CREATE INDEX IF NOT EXISTS idx_market_bar_versions_range
                ON market_bar_versions(symbol, timeframe, bar_start, available_at);
            CREATE TABLE IF NOT EXISTS data_catalog (
                dataset_id TEXT PRIMARY KEY,
                instrument_key TEXT,
                timeframe TEXT,
                source TEXT NOT NULL,
                as_of_start TEXT,
                as_of_end TEXT,
                row_count INTEGER NOT NULL DEFAULT 0,
                quality_status TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS institutional_tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                config_json TEXT NOT NULL,
                result_json TEXT,
                error_code TEXT,
                error_detail TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS research_runs (
                run_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                params_hash TEXT NOT NULL,
                config_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS experiment_trials (
                trial_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                params_json TEXT NOT NULL,
                params_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                metrics_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, params_hash),
                FOREIGN KEY(run_id) REFERENCES research_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS qualification_history (
                qualification_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                params_hash TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                status TEXT NOT NULL,
                reason_codes_json TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS risk_clusters (
                cluster_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                instruments_json TEXT NOT NULL,
                max_fraction TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_timeline (
                event_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                venue TEXT NOT NULL,
                position_id TEXT,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(account_id, mode, venue, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_execution_timeline_scope
                ON execution_timeline(account_id, mode, venue, event_at, event_id);
            CREATE TABLE IF NOT EXISTS evidence_bundles (
                bundle_id TEXT PRIMARY KEY,
                as_of TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                frozen_at TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                missing_json TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                bundle_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                weight_digest TEXT,
                digest_status TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                response_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(bundle_id) REFERENCES evidence_bundles(bundle_id)
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
            """
        )
        # Account scope is explicit on institutional task/run records.  The
        # JSON config remains useful for compatibility, but filtering a
        # research or qualification query must not depend on parsing it.
        for table, additions in {
            "institutional_tasks": {
                "account_id": "TEXT",
                "venue": "TEXT",
                "mode": "TEXT",
            },
            "research_runs": {
                "account_id": "TEXT",
                "venue": "TEXT",
                "mode": "TEXT",
            },
            "qualification_history": {
                "account_id": "TEXT",
            },
        }.items():
            columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in additions.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        # Migrate the old ambiguous market-bars image into an explicit
        # LEGACY_UNVERIFIED identity.  This is INSERT OR IGNORE by the full
        # composite key, so reopening a copied database is idempotent.
        old_rows = db.execute(
            "SELECT symbol, timeframe, bar_start, bar_end, open, high, low, close, volume, provider, data_as_of, is_closed, received_at FROM market_bars"
        ).fetchall()
        source_hash = hashlib.sha256(
            json.dumps([tuple(row) for row in old_rows], sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        for row in old_rows:
            legacy_key = canonical_instrument_key("legacy", "unknown", row["symbol"], "UNKNOWN", "last")
            revision = hashlib.sha256(
                json.dumps(tuple(row), sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:32]
            available = row["received_at"] or row["data_as_of"]
            event_time = row["bar_start"]
            db.execute(
                """INSERT OR IGNORE INTO market_bar_versions(
                    instrument_key, symbol, venue, market_type, native_symbol,
                    settle_currency, price_type, timeframe, bar_start, bar_end,
                    revision_id, open, high, low, close, volume, volume_unit,
                    event_time, first_received_at, available_at, fetched_at,
                    source, raw_hash, quality_status, is_closed, payload_json
                ) VALUES (?, ?, 'legacy', 'unknown', ?, 'UNKNOWN', 'last', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'LEGACY_UNVERIFIED', ?, ?)""",
                (
                    legacy_key,
                    row["symbol"],
                    row["symbol"],
                    row["timeframe"],
                    row["bar_start"],
                    row["bar_end"],
                    revision,
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                    "UNKNOWN",
                    event_time,
                    row["received_at"],
                    available,
                    row["received_at"],
                    row["provider"],
                    revision,
                    row["is_closed"],
                    json.dumps({"legacy_row": dict(row)}, sort_keys=True, default=str),
                ),
            )
        target_count = int(db.execute("SELECT COUNT(*) FROM market_bar_versions").fetchone()[0])
        now = datetime.now(timezone.utc).isoformat()
        report = {
            "source_table": "market_bars",
            "target_table": "market_bar_versions",
            "legacy_identity": True,
            "source_hash": source_hash,
            "source_rows": len(old_rows),
            "target_rows": target_count,
            "duplicate_rows": max(0, len(old_rows) - target_count),
            "orphan_rows": 0,
        }
        db.execute(
            """INSERT INTO institutional_migration_runs(
                migration_key, source_schema, target_schema, source_rows,
                target_rows, duplicate_rows, orphan_rows, source_hash, status,
                created_at, updated_at, report_json
            ) VALUES ('market_bars_identity_v1', 'market_bars_v1', 'market_bar_versions_v1', ?, ?, ?, 0, ?, 'COMPLETED', ?, ?, ?)
            ON CONFLICT(migration_key) DO UPDATE SET
                target_rows=excluded.target_rows, duplicate_rows=excluded.duplicate_rows,
                source_hash=excluded.source_hash, updated_at=excluded.updated_at,
                report_json=excluded.report_json""",
            (len(old_rows), target_count, max(0, len(old_rows) - target_count), source_hash, now, now, json.dumps(report, sort_keys=True)),
        )

    @staticmethod
    def _ensure_prediction_columns(db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(predictions)").fetchall()}
        additions = {
            "model_id": "TEXT",
            "model_version": "TEXT",
            "prompt_version": "TEXT",
            "input_hash": "TEXT",
            "data_as_of": "TEXT",
            "context_json": "TEXT",
            "raw_model_response": "TEXT",
            "parse_status": "TEXT",
            "source_type": "TEXT NOT NULL DEFAULT 'live'",
            "replay_run_id": "TEXT",
            "calibrated_confidence": "REAL",
            "calibration_version": "TEXT",
            "calibration_scope": "TEXT",
            "calibration_sample_size": "INTEGER",
            "calibration_fallback": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE predictions ADD COLUMN {name} {definition}")

    @staticmethod
    def _ensure_phase3_tables(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS replay_runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                model_id TEXT NOT NULL,
                prompt_version TEXT,
                symbols_json TEXT NOT NULL,
                timeframes_json TEXT NOT NULL,
                sampling_policy_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                counts_json TEXT NOT NULL DEFAULT '{}',
                config_json TEXT NOT NULL DEFAULT '{}',
                completed_at TEXT,
                error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS replay_samples (
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                as_of TEXT NOT NULL,
                capability_flags_json TEXT NOT NULL DEFAULT '{}',
                prediction_id TEXT,
                status TEXT NOT NULL,
                error_code TEXT,
                started_at TEXT,
                completed_at TEXT,
                PRIMARY KEY (run_id, symbol, timeframe, as_of),
                FOREIGN KEY(run_id) REFERENCES replay_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS performance_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_json TEXT NOT NULL,
                window_start TEXT,
                window_end TEXT,
                sample_count INTEGER NOT NULL,
                actionable_count INTEGER NOT NULL,
                metrics_json TEXT NOT NULL,
                model_id TEXT,
                prompt_version TEXT,
                source_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PRELIMINARY'
            );
            CREATE TABLE IF NOT EXISTS calibration_results (
                calibration_id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                method TEXT NOT NULL,
                params_json TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                trained_until TEXT,
                brier_raw REAL,
                brier_calibrated REAL,
                ece_raw REAL,
                ece_calibrated REAL,
                status TEXT NOT NULL,
                fallback TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calibration_buckets (
                calibration_id TEXT NOT NULL,
                lower_bound REAL NOT NULL,
                upper_bound REAL NOT NULL,
                n INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                empirical_rate REAL,
                shrunk_rate REAL,
                PRIMARY KEY (calibration_id, lower_bound, upper_bound),
                FOREIGN KEY(calibration_id) REFERENCES calibration_results(calibration_id)
            );
            """
        )

    @staticmethod
    def _ensure_phase5_tables(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist_entries (
                symbol TEXT PRIMARY KEY,
                added_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    @staticmethod
    def _ensure_phase5_scheduler_tables(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS scheduler_runs (
                run_id TEXT PRIMARY KEY,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                next_run_at TEXT,
                settings_json TEXT NOT NULL,
                counts_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT,
                error_detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_scheduler_runs_started
                ON scheduler_runs(started_at DESC, run_id DESC);
            CREATE TABLE IF NOT EXISTS scheduler_items (
                item_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                session_state TEXT,
                skip_reason TEXT,
                resource_reason TEXT,
                cache_key TEXT,
                cache_status TEXT,
                error_code TEXT,
                error_detail TEXT,
                prediction_id TEXT,
                stage TEXT NOT NULL DEFAULT 'scan',
                provider TEXT,
                provider_as_of TEXT,
                as_of TEXT,
                capability_json TEXT,
                outcome_status TEXT,
                retry_after_at TEXT,
                FOREIGN KEY(run_id) REFERENCES scheduler_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_scheduler_items_run
                ON scheduler_items(run_id, symbol, item_id);
            CREATE TABLE IF NOT EXISTS scheduler_cache_entries (
                cache_key TEXT PRIMARY KEY,
                cache_version TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                as_of TEXT NOT NULL,
                provider TEXT NOT NULL,
                context_capability_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_accessed_at TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                prediction_id TEXT,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduler_state (
                state_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    @staticmethod
    def _ensure_phase5_settlement_columns(db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(scheduler_items)").fetchall()}
        additions = {
            "stage": "TEXT NOT NULL DEFAULT 'scan'",
            "provider": "TEXT",
            "provider_as_of": "TEXT",
            "as_of": "TEXT",
            "capability_json": "TEXT",
            "outcome_status": "TEXT",
            "retry_after_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE scheduler_items ADD COLUMN {name} {definition}")

    @staticmethod
    def _ensure_phase5_alert_tables(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY,
                policy_version TEXT NOT NULL,
                source TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                symbol TEXT,
                prediction_id TEXT,
                event_identity TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                dedupe_key TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_status_seen
                ON alerts(status, last_seen_at DESC, alert_id DESC);
            CREATE INDEX IF NOT EXISTS idx_alerts_source_seen
                ON alerts(source, last_seen_at DESC, alert_id DESC);
            """
        )

    @staticmethod
    def _ensure_phase6_tables(db: sqlite3.Connection) -> None:
        """Create additive Phase 6 context/evidence tables without touching prior data."""

        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS benchmark_metadata (
                symbol TEXT PRIMARY KEY,
                benchmark_symbol TEXT NOT NULL,
                benchmark_asset_type TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_symbol TEXT NOT NULL,
                mapping_version TEXT NOT NULL,
                relation TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_benchmark_metadata_benchmark
                ON benchmark_metadata(benchmark_symbol, mapping_version);
            CREATE TABLE IF NOT EXISTS phase6_events (
                event_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_type TEXT NOT NULL,
                category TEXT NOT NULL,
                event_at TEXT NOT NULL,
                published_at TEXT,
                known_at TEXT,
                retrieved_at TEXT,
                importance INTEGER NOT NULL,
                affected_symbols_json TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                url TEXT,
                primary_source INTEGER NOT NULL DEFAULT 0,
                reported_credibility INTEGER NOT NULL,
                credibility_score INTEGER NOT NULL,
                revision_known_at TEXT,
                dedupe_hash TEXT,
                provider TEXT NOT NULL,
                capability_json TEXT NOT NULL DEFAULT '{}',
                cluster_id TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_phase6_events_known
                ON phase6_events(known_at, event_at, event_id);
            CREATE INDEX IF NOT EXISTS idx_phase6_events_source
                ON phase6_events(source, event_at, event_id);
            CREATE TABLE IF NOT EXISTS phase6_event_clusters (
                cluster_id TEXT PRIMARY KEY,
                as_of TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_phase6_event_clusters_as_of
                ON phase6_event_clusters(as_of DESC, cluster_id ASC);
            CREATE TABLE IF NOT EXISTS market_memory_features (
                feature_id TEXT PRIMARY KEY,
                prediction_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                feature_as_of TEXT NOT NULL,
                generated_at TEXT,
                symbol TEXT,
                timeframe TEXT,
                outcome_known_at TEXT,
                representation_version TEXT NOT NULL,
                features_json TEXT NOT NULL,
                outcome_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(prediction_id, feature_as_of, representation_version)
            );
            CREATE INDEX IF NOT EXISTS idx_market_memory_as_of
                ON market_memory_features(feature_as_of, representation_version, prediction_id);
            """
        )
        # These six mappings are explicit registry metadata, not provider data.
        # They make restart/reopen behavior durable while keeping registered
        # expansion mappings opt-in through BenchmarkContextService.
        rows = (
            ("AAPL", "QQQ", "Technology"),
            ("NVDA", "SOXX", "Semiconductors"),
            ("TSLA", "SPY", "broad_us_equity"),
            ("AMD", "SOXX", "Semiconductors"),
            ("BTCUSDT", "BTCUSDT", "crypto_market_baseline"),
            ("ETHUSDT", "BTCUSDT", "crypto_market_baseline"),
        )
        now = datetime.now(timezone.utc).isoformat()
        for symbol, benchmark_symbol, mapping_reason in rows:
            asset_type = "crypto" if symbol.endswith("USDT") else "equity"
            payload = {
                "symbol": symbol,
                "benchmark_symbol": benchmark_symbol,
                "benchmark_asset_type": asset_type,
                "provider": "public_market",
                "provider_symbol": benchmark_symbol,
                "mapping_version": "benchmark_mapping_v1",
                "relation": "crypto_market_baseline" if asset_type == "crypto" else ("sector_benchmark" if benchmark_symbol != "SPY" else "broad_market_benchmark"),
                "status": "mapped",
                "mapping_reason": f"explicit_{mapping_reason}",
                "metadata_labels": {"identity": "explicit", "sector": "known" if mapping_reason in {"Technology", "Semiconductors"} else "unknown_or_broad"},
            }
            db.execute(
                """INSERT OR IGNORE INTO benchmark_metadata(
                    symbol, benchmark_symbol, benchmark_asset_type, provider,
                    provider_symbol, mapping_version, relation, status,
                    metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    symbol,
                    benchmark_symbol,
                    asset_type,
                    "public_market",
                    benchmark_symbol,
                    "benchmark_mapping_v1",
                    payload["relation"],
                    "mapped",
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )

    @staticmethod
    def _ensure_phase6_memory_provenance_columns(db: sqlite3.Connection) -> None:
        """Add immutable provenance columns to materialized memory rows."""

        columns = {row[1] for row in db.execute("PRAGMA table_info(market_memory_features)").fetchall()}
        additions = {
            "generated_at": "TEXT",
            "symbol": "TEXT",
            "timeframe": "TEXT",
            "outcome_known_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE market_memory_features ADD COLUMN {name} {definition}")
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_market_memory_provenance
                 ON market_memory_features(symbol, timeframe, generated_at, feature_as_of, prediction_id)"""
        )

    @staticmethod
    def _utc_timestamp(value: datetime | None = None) -> str:
        timestamp = value or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).isoformat()

    def list_watchlist_entries(self) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT symbol, added_at, updated_at FROM watchlist_entries ORDER BY added_at ASC, symbol ASC"
            ).fetchall()
        return [
            {
                "symbol": row["symbol"],
                "added_at": row["added_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def upsert_watchlist_entry(self, symbol: str, *, now: datetime | None = None) -> dict[str, str]:
        canonical_symbol = self.resolve_instrument(symbol).symbol
        timestamp = self._utc_timestamp(now)
        with self._connect() as db:
            row = db.execute(
                "SELECT added_at FROM watchlist_entries WHERE symbol = ?",
                (canonical_symbol,),
            ).fetchone()
            if row is None:
                added_at = timestamp
                db.execute(
                    "INSERT INTO watchlist_entries(symbol, added_at, updated_at) VALUES (?, ?, ?)",
                    (canonical_symbol, added_at, timestamp),
                )
            else:
                added_at = str(row["added_at"])
                db.execute(
                    "UPDATE watchlist_entries SET updated_at = ? WHERE symbol = ?",
                    (timestamp, canonical_symbol),
                )
        return {"symbol": canonical_symbol, "added_at": added_at, "updated_at": timestamp}

    def delete_watchlist_entry(self, symbol: str) -> bool:
        canonical_symbol = self.resolve_instrument(symbol).symbol
        with self._connect() as db:
            result = db.execute("DELETE FROM watchlist_entries WHERE symbol = ?", (canonical_symbol,))
            db.execute("UPDATE strategy_subscriptions SET enabled=0 WHERE symbol=?", (canonical_symbol,))
        return result.rowcount > 0

    @staticmethod
    def _app_setting_record(key: str, row: sqlite3.Row | None) -> dict[str, object]:
        definition = APP_SETTING_DEFINITIONS[key]
        value = definition["default"] if row is None else json.loads(row["value_json"])
        return {
            "key": key,
            "value": value,
            "default_value": definition["default"],
            "value_type": definition["value_type"],
            "updated_at": row["updated_at"] if row is not None else None,
            "source": "stored" if row is not None else "default",
            "description": definition["description"],
        }

    def list_app_settings(self) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = {
                row["setting_key"]: row
                for row in db.execute(
                    "SELECT setting_key, value_json, updated_at FROM app_settings"
                ).fetchall()
            }
        return [self._app_setting_record(key, rows.get(key)) for key in APP_SETTING_DEFINITIONS]

    def get_app_setting(self, key: str) -> dict[str, object]:
        if key not in APP_SETTING_DEFINITIONS:
            raise ValueError(f"unsupported app setting: {key}")
        with self._connect() as db:
            row = db.execute(
                "SELECT setting_key, value_json, updated_at FROM app_settings WHERE setting_key = ?",
                (key,),
            ).fetchone()
        return self._app_setting_record(key, row)

    def upsert_app_setting(self, key: str, value: Any, *, now: datetime | None = None) -> dict[str, object]:
        validate_app_setting_value(key, value)
        timestamp = self._utc_timestamp(now)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO app_settings(setting_key, value_json, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, sort_keys=True), timestamp),
            )
        return self.get_app_setting(key)

    def delete_app_setting(self, key: str) -> bool:
        if key not in APP_SETTING_DEFINITIONS:
            raise ValueError(f"unsupported app setting: {key}")
        with self._connect() as db:
            result = db.execute("DELETE FROM app_settings WHERE setting_key = ?", (key,))
        return result.rowcount > 0

    @staticmethod
    def _scheduler_run_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "run_id": row["run_id"],
            "trigger": row["trigger"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "next_run_at": row["next_run_at"],
            "settings": json.loads(row["settings_json"]),
            "counts": json.loads(row["counts_json"]),
            "error_code": row["error_code"],
            "error_detail": row["error_detail"],
        }

    @staticmethod
    def _scheduler_item_from_row(row: sqlite3.Row) -> dict[str, object]:
        raw_capability = row["capability_json"]
        try:
            capability = json.loads(raw_capability) if raw_capability else None
        except (TypeError, ValueError):
            capability = {"parse_error": "invalid capability evidence"}
        return {
            "item_id": row["item_id"],
            "run_id": row["run_id"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "session_state": row["session_state"],
            "skip_reason": row["skip_reason"],
            "resource_reason": row["resource_reason"],
            "cache_key": row["cache_key"],
            "cache_status": row["cache_status"],
            "error_code": row["error_code"],
            "error_detail": row["error_detail"],
            "prediction_id": row["prediction_id"],
            "stage": row["stage"],
            "provider": row["provider"],
            "provider_as_of": row["provider_as_of"],
            "as_of": row["as_of"],
            "capability": capability,
            "outcome_status": row["outcome_status"],
            "retry_after_at": row["retry_after_at"],
        }

    def create_scheduler_run(
        self,
        *,
        run_id: str,
        trigger: str,
        started_at: str,
        settings: dict[str, object],
        status: str = "RUNNING",
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO scheduler_runs(
                    run_id, trigger, status, started_at, settings_json
                ) VALUES (?, ?, ?, ?, ?)""",
                (run_id, trigger, status, started_at, json.dumps(settings, sort_keys=True)),
            )

    def update_scheduler_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        finished_at: str | None = None,
        next_run_at: str | None = None,
        counts: dict[str, object] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        updates: list[str] = []
        values: list[object] = []
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if finished_at is not None:
            updates.append("finished_at = ?")
            values.append(finished_at)
        if next_run_at is not None:
            updates.append("next_run_at = ?")
            values.append(next_run_at)
        if counts is not None:
            updates.append("counts_json = ?")
            values.append(json.dumps(counts, sort_keys=True))
        if error_code is not None:
            updates.append("error_code = ?")
            values.append(error_code)
        if error_detail is not None:
            updates.append("error_detail = ?")
            values.append(error_detail)
        if not updates:
            return
        values.append(run_id)
        with self._connect() as db:
            result = db.execute(
                f"UPDATE scheduler_runs SET {', '.join(updates)} WHERE run_id = ?",
                tuple(values),
            )
            if result.rowcount == 0:
                raise KeyError(f"scheduler run not found: {run_id}")

    def get_scheduler_run(self, run_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM scheduler_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._scheduler_run_from_row(row) if row is not None else None

    def list_scheduler_runs(self, *, limit: int = 20) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM scheduler_runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [self._scheduler_run_from_row(row) for row in rows]

    def create_scheduler_item(
        self,
        *,
        item_id: str,
        run_id: str,
        symbol: str,
        timeframe: str,
        status: str = "PENDING",
        stage: str = "scan",
        prediction_id: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO scheduler_items(
                    item_id, run_id, symbol, timeframe, status, stage, prediction_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (item_id, run_id, symbol, timeframe, status, stage, prediction_id),
            )

    def update_scheduler_item(
        self,
        item_id: str,
        *,
        status: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        session_state: str | None = None,
        skip_reason: str | None = None,
        resource_reason: str | None = None,
        cache_key: str | None = None,
        cache_status: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        prediction_id: str | None = None,
        stage: str | None = None,
        provider: str | None = None,
        provider_as_of: str | None = None,
        as_of: str | None = None,
        capability: dict[str, object] | None = None,
        outcome_status: str | None = None,
        retry_after_at: str | None = None,
    ) -> None:
        updates: list[str] = []
        values: list[object] = []
        for name, value in (
            ("status", status),
            ("started_at", started_at),
            ("finished_at", finished_at),
            ("session_state", session_state),
            ("skip_reason", skip_reason),
            ("resource_reason", resource_reason),
            ("cache_key", cache_key),
            ("cache_status", cache_status),
            ("error_code", error_code),
            ("error_detail", error_detail),
            ("prediction_id", prediction_id),
            ("stage", stage),
            ("provider", provider),
            ("provider_as_of", provider_as_of),
            ("as_of", as_of),
            ("capability_json", json.dumps(capability, sort_keys=True) if capability is not None else None),
            ("outcome_status", outcome_status),
            ("retry_after_at", retry_after_at),
        ):
            if value is not None:
                updates.append(f"{name} = ?")
                values.append(value)
        if not updates:
            return
        values.append(item_id)
        with self._connect() as db:
            result = db.execute(
                f"UPDATE scheduler_items SET {', '.join(updates)} WHERE item_id = ?",
                tuple(values),
            )
            if result.rowcount == 0:
                raise KeyError(f"scheduler item not found: {item_id}")

    def list_scheduler_items(self, run_id: str) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM scheduler_items WHERE run_id = ? ORDER BY item_id ASC",
                (run_id,),
            ).fetchall()
        return [self._scheduler_item_from_row(row) for row in rows]

    def get_latest_settlement_item(self, prediction_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM scheduler_items
                   WHERE stage = 'settlement' AND prediction_id = ?
                   ORDER BY COALESCE(finished_at, started_at, '') DESC, item_id DESC
                   LIMIT 1""",
                (prediction_id,),
            ).fetchone()
        return self._scheduler_item_from_row(row) if row is not None else None

    def recover_scheduler_runs(self, *, recovered_at: str | None = None) -> int:
        timestamp = recovered_at or self._utc_timestamp()
        with self._connect() as db:
            runs = db.execute("SELECT run_id FROM scheduler_runs WHERE status = 'RUNNING'").fetchall()
            db.execute(
                """UPDATE scheduler_runs
                   SET status = 'INTERRUPTED', finished_at = ?, error_code = 'PROCESS_RESTARTED',
                       error_detail = 'scheduler run was interrupted before restart'
                 WHERE status = 'RUNNING'""",
                (timestamp,),
            )
            db.execute(
                """UPDATE scheduler_items
                   SET status = 'INTERRUPTED', finished_at = ?, error_code = 'PROCESS_RESTARTED',
                       error_detail = 'scheduler item was interrupted before restart'
                 WHERE status = 'RUNNING'""",
                (timestamp,),
            )
        return len(runs)

    def upsert_scheduler_cache_entry(
        self,
        *,
        cache_key: str,
        cache_version: str,
        symbol: str,
        timeframe: str,
        as_of: str,
        provider: str,
        context_capability: dict[str, object],
        created_at: str,
        expires_at: str,
        prediction_id: str | None,
        status: str,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO scheduler_cache_entries(
                    cache_key, cache_version, symbol, timeframe, as_of, provider,
                    context_capability_json, created_at, expires_at, last_accessed_at,
                    hit_count, prediction_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    cache_version = excluded.cache_version,
                    symbol = excluded.symbol,
                    timeframe = excluded.timeframe,
                    as_of = excluded.as_of,
                    provider = excluded.provider,
                    context_capability_json = excluded.context_capability_json,
                    expires_at = excluded.expires_at,
                    last_accessed_at = excluded.last_accessed_at,
                    prediction_id = excluded.prediction_id,
                    status = excluded.status""",
                (
                    cache_key,
                    cache_version,
                    symbol,
                    timeframe,
                    as_of,
                    provider,
                    json.dumps(context_capability, sort_keys=True),
                    created_at,
                    expires_at,
                    created_at,
                    prediction_id,
                    status,
                ),
            )

    def record_scheduler_cache_hit(self, cache_key: str, *, accessed_at: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE scheduler_cache_entries
                   SET hit_count = hit_count + 1, last_accessed_at = ?, status = 'hit'
                 WHERE cache_key = ?""",
                (accessed_at, cache_key),
            )

    def prune_scheduler_cache_entries(self, *, now: str, capacity: int = 500) -> None:
        bounded_capacity = max(1, min(int(capacity), 5000))
        with self._connect() as db:
            db.execute("DELETE FROM scheduler_cache_entries WHERE expires_at <= ?", (now,))
            db.execute(
                """DELETE FROM scheduler_cache_entries
                 WHERE cache_key NOT IN (
                     SELECT cache_key FROM scheduler_cache_entries
                      ORDER BY last_accessed_at DESC, cache_key ASC LIMIT ?
                 )""",
                (bounded_capacity,),
            )

    def list_scheduler_cache_entries(self, *, limit: int = 100) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM scheduler_cache_entries ORDER BY last_accessed_at DESC, cache_key ASC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [
            {
                "cache_key": row["cache_key"],
                "cache_version": row["cache_version"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "as_of": row["as_of"],
                "provider": row["provider"],
                "context_capability": json.loads(row["context_capability_json"]),
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "last_accessed_at": row["last_accessed_at"],
                "hit_count": int(row["hit_count"]),
                "prediction_id": row["prediction_id"],
                "status": row["status"],
            }
            for row in rows
        ]

    def set_scheduler_state(self, key: str, value: object, *, updated_at: str | None = None) -> None:
        timestamp = updated_at or self._utc_timestamp()
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO scheduler_state(state_key, value_json, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, sort_keys=True), timestamp),
            )

    def get_scheduler_state(self, key: str) -> object | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT value_json FROM scheduler_state WHERE state_key = ?",
                (key,),
            ).fetchone()
        return json.loads(row["value_json"]) if row is not None else None

    def save_benchmark_metadata(self, payload: dict[str, Any], *, updated_at: str | None = None) -> None:
        """Persist one explicit benchmark mapping; provider data is never inferred here."""

        required = ("symbol", "benchmark_symbol", "benchmark_asset_type", "provider", "provider_symbol", "mapping_version", "relation", "status")
        if any(not isinstance(payload.get(key), str) or not str(payload[key]).strip() for key in required):
            raise ValueError("benchmark metadata is missing an allowlisted identity field")
        symbol = str(payload["symbol"]).strip().upper()
        timestamp = updated_at or self._utc_timestamp()
        with self._connect() as db:
            db.execute(
                """INSERT INTO benchmark_metadata(
                    symbol, benchmark_symbol, benchmark_asset_type, provider,
                    provider_symbol, mapping_version, relation, status,
                    metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    benchmark_symbol = excluded.benchmark_symbol,
                    benchmark_asset_type = excluded.benchmark_asset_type,
                    provider = excluded.provider,
                    provider_symbol = excluded.provider_symbol,
                    mapping_version = excluded.mapping_version,
                    relation = excluded.relation,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at""",
                (
                    symbol,
                    str(payload["benchmark_symbol"]).strip().upper(),
                    str(payload["benchmark_asset_type"]).strip().lower(),
                    str(payload["provider"]).strip(),
                    str(payload["provider_symbol"]).strip().upper(),
                    str(payload["mapping_version"]).strip(),
                    str(payload["relation"]).strip(),
                    str(payload["status"]).strip(),
                    json.dumps(payload, sort_keys=True),
                    timestamp,
                ),
            )

    def get_benchmark_metadata(self, symbol: str) -> dict[str, object] | None:
        normalized = str(symbol).strip().upper()
        if not normalized:
            return None
        with self._connect() as db:
            row = db.execute("SELECT * FROM benchmark_metadata WHERE symbol = ?", (normalized,)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["metadata_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.update(
            {
                "symbol": row["symbol"],
                "benchmark_symbol": row["benchmark_symbol"],
                "benchmark_asset_type": row["benchmark_asset_type"],
                "provider": row["provider"],
                "provider_symbol": row["provider_symbol"],
                "mapping_version": row["mapping_version"],
                "relation": row["relation"],
                "status": row["status"],
                "updated_at": row["updated_at"],
            }
        )
        return payload

    def list_benchmark_metadata(self, *, limit: int = 500) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 1_000))
        with self._connect() as db:
            rows = db.execute(
                "SELECT symbol FROM benchmark_metadata ORDER BY symbol ASC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        return [record for row in rows if (record := self.get_benchmark_metadata(row["symbol"])) is not None]

    def save_event_context(self, payload: dict[str, Any]) -> None:
        """Save explicit analysis event evidence and its cluster audit trail."""

        events = payload.get("events") if isinstance(payload.get("events"), list) else []
        clusters = payload.get("clusters") if isinstance(payload.get("clusters"), list) else []
        cluster_by_event: dict[str, str] = {}
        timestamp = self._utc_timestamp()
        with self._connect() as db:
            for cluster in clusters:
                if not isinstance(cluster, dict) or not isinstance(cluster.get("cluster_id"), str):
                    continue
                cluster_id = str(cluster["cluster_id"])
                for event_id in cluster.get("event_ids", []):
                    if isinstance(event_id, str):
                        cluster_by_event[event_id] = cluster_id
                db.execute(
                    """INSERT OR REPLACE INTO phase6_event_clusters(cluster_id, as_of, created_at, payload_json)
                       VALUES (?, ?, ?, ?)""",
                    (cluster_id, str(cluster.get("as_of") or payload.get("as_of") or timestamp), timestamp, json.dumps(cluster, sort_keys=True)),
                )
            for event in events:
                if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
                    continue
                event_id = str(event["event_id"])
                symbols = event.get("affected_symbols", event.get("symbols", []))
                if not isinstance(symbols, list):
                    symbols = []
                capability = event.get("capability") if isinstance(event.get("capability"), dict) else {}
                db.execute(
                    """INSERT OR IGNORE INTO phase6_events(
                        event_id, source, source_type, category, event_at, published_at,
                        known_at, retrieved_at, importance, affected_symbols_json, title,
                        summary, url, primary_source, reported_credibility, credibility_score,
                        revision_known_at, dedupe_hash, provider, capability_json, cluster_id,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        str(event.get("source", "unknown")),
                        str(event.get("source_type", "unknown")),
                        str(event.get("category", "other")),
                        str(event.get("event_at") or event.get("published_at") or timestamp),
                        event.get("published_at"),
                        event.get("known_at"),
                        event.get("retrieved_at"),
                        int(event.get("importance", 0) or 0),
                        json.dumps([str(value).upper() for value in symbols], sort_keys=True),
                        str(event.get("title", "")),
                        event.get("summary", event.get("summary_raw")),
                        event.get("url"),
                        int(bool(event.get("primary_source"))),
                        int(event.get("reported_credibility", event.get("credibility", 50)) or 0),
                        int(event.get("credibility_score", event.get("credibility", 50)) or 0),
                        event.get("revision_known_at"),
                        event.get("dedupe_hash"),
                        str(event.get("provider", payload.get("provider", "unknown"))),
                        json.dumps(capability, sort_keys=True),
                        cluster_by_event.get(event_id),
                        json.dumps(event, sort_keys=True),
                    ),
                )

    def list_event_evidence(
        self,
        *,
        symbol: str | None = None,
        as_of: str | None = None,
        published_since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload_json FROM phase6_events
                WHERE (? IS NULL OR julianday(published_at) >= julianday(?))
                  AND (? IS NULL OR julianday(published_at) <= julianday(?))
                ORDER BY CASE WHEN ? IS NULL THEN event_at ELSE published_at END DESC, event_id ASC LIMIT ?""",
                (published_since, published_since, published_since, as_of, published_since, max(bounded_limit * 5, bounded_limit)),
            ).fetchall()
        cutoff = _parse_utc_timestamp(as_of) if as_of is not None else None
        if as_of is not None and cutoff is None:
            raise ValueError("as_of must be an ISO timestamp with timezone")
        normalized_symbol = symbol.strip().upper() if symbol else None
        results: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            symbols = payload.get("affected_symbols", payload.get("symbols", []))
            if normalized_symbol and normalized_symbol not in {str(value).upper() for value in symbols if isinstance(value, str)}:
                continue
            known_at = _parse_utc_timestamp(payload.get("known_at"))
            published_at = _parse_utc_timestamp(payload.get("published_at"))
            revision_at = _parse_utc_timestamp(payload.get("revision_known_at"))
            if cutoff is not None and (known_at is None or known_at > cutoff or (published_at is not None and published_at > cutoff) or (revision_at is not None and revision_at > cutoff)):
                continue
            if cutoff is not None and known_at is None:
                continue
            results.append(payload)
            if len(results) >= bounded_limit:
                break
        return results

    def list_event_clusters(self, *, symbol: str | None = None, as_of: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM phase6_event_clusters ORDER BY as_of DESC, cluster_id ASC LIMIT ?",
                (bounded_limit * 5,),
            ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if as_of:
                cutoff = _parse_utc_timestamp(as_of)
                payload_as_of = _parse_utc_timestamp(payload.get("as_of"))
                if cutoff is None or payload_as_of is None or payload_as_of > cutoff:
                    continue
            symbols = payload.get("affected_symbols", payload.get("symbols", []))
            if symbol and symbol.strip().upper() not in {str(value).upper() for value in symbols if isinstance(value, str)}:
                continue
            results.append(payload)
            if len(results) >= bounded_limit:
                break
        return results

    def save_memory_feature(self, payload: dict[str, Any]) -> None:
        required = (
            "feature_id",
            "prediction_id",
            "source_type",
            "feature_as_of",
            "generated_at",
            "symbol",
            "timeframe",
            "representation_version",
            "features",
        )
        if any(not payload.get(key) for key in required):
            raise ValueError("memory feature is missing required identity or provenance")
        feature_as_of = _parse_utc_timestamp(payload["feature_as_of"])
        generated_at = _parse_utc_timestamp(payload["generated_at"])
        outcome_known_at = _parse_utc_timestamp(payload.get("outcome_known_at")) if payload.get("outcome_known_at") else None
        if feature_as_of is None or generated_at is None or (payload.get("outcome_known_at") and outcome_known_at is None):
            raise ValueError("memory feature timestamps must be timezone-aware ISO values")
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO market_memory_features(
                    feature_id, prediction_id, source_type, feature_as_of,
                    generated_at, symbol, timeframe, outcome_known_at,
                    representation_version, features_json, outcome_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(payload["feature_id"]),
                    str(payload["prediction_id"]),
                    str(payload["source_type"]),
                    feature_as_of.isoformat(),
                    generated_at.isoformat(),
                    str(payload["symbol"]).strip().upper(),
                    str(payload["timeframe"]).strip(),
                    outcome_known_at.isoformat() if outcome_known_at else None,
                    str(payload["representation_version"]),
                    json.dumps(payload["features"], sort_keys=True),
                    json.dumps(payload["outcome"], sort_keys=True) if isinstance(payload.get("outcome"), dict) else None,
                    self._utc_timestamp(),
                ),
            )

    def list_memory_features(self, *, as_of: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 1_000))
        cutoff = _parse_utc_timestamp(as_of) if as_of is not None else None
        if as_of is not None and cutoff is None:
            raise ValueError("as_of must be an ISO timestamp with timezone")
        query = "SELECT * FROM market_memory_features ORDER BY feature_as_of DESC, prediction_id ASC LIMIT ?"
        # Retention is capped at 1,000 rows, so read the bounded table before
        # applying historical filtering instead of allowing newer rows to
        # starve a point-in-time query.
        params: list[Any] = [1_000]
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            feature_as_of = _parse_utc_timestamp(row["feature_as_of"])
            if cutoff is not None and (feature_as_of is None or feature_as_of > cutoff):
                continue
            generated_at = _parse_utc_timestamp(row["generated_at"])
            outcome_known_at = _parse_utc_timestamp(row["outcome_known_at"])
            try:
                features = json.loads(row["features_json"])
                outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else None
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            result.append(
                {
                    "feature_id": row["feature_id"],
                    "prediction_id": row["prediction_id"],
                    "source_type": row["source_type"],
                    "feature_as_of": row["feature_as_of"],
                    "generated_at": generated_at.isoformat() if generated_at else row["generated_at"],
                    "symbol": row["symbol"],
                    "timeframe": row["timeframe"],
                    "outcome_known_at": outcome_known_at.isoformat() if outcome_known_at else row["outcome_known_at"],
                    "representation_version": row["representation_version"],
                    "features": features,
                    "outcome": outcome,
                    "created_at": row["created_at"],
                }
            )
            if len(result) >= bounded_limit:
                break
        return result

    def prune_memory_features(self, *, keep: int = 1_000) -> int:
        bounded_keep = max(1, min(int(keep), 1_000))
        with self._connect() as db:
            result = db.execute(
                """DELETE FROM market_memory_features
                     WHERE feature_id NOT IN (
                         SELECT feature_id FROM market_memory_features
                          ORDER BY feature_as_of DESC, prediction_id ASC LIMIT ?
                     )""",
                (bounded_keep,),
            )
        return result.rowcount

    def list_memory_source_records(
        self,
        *,
        as_of: str,
        source_type: str | None = None,
        timeframe: str | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Return prediction features with outcomes known strictly before the cutoff."""

        bounded_limit = max(1, min(int(limit), 100_000))
        cutoff = _parse_utc_timestamp(as_of)
        if cutoff is None:
            raise ValueError("as_of must be an ISO timestamp with timezone")
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("COALESCE(p.source_type, 'live') = ?")
            params.append(source_type)
        if timeframe:
            clauses.append("json_extract(p.payload_json, '$.analysis_timeframe') = ?")
            params.append(timeframe)
        params.append(bounded_limit)
        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT p.payload_json AS prediction_json, o.payload_json AS outcome_json, o.settled_at
                       FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.prediction_id
                      {where_clause}
                      ORDER BY p.generated_at ASC, p.prediction_id ASC LIMIT ?""",
                tuple(params),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                prediction = json.loads(row["prediction_json"])
                if not isinstance(prediction, dict):
                    continue
                outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else None
                generated_at = _parse_utc_timestamp(prediction.get("generated_at"))
                if generated_at is None or generated_at >= cutoff:
                    continue
                if outcome is not None and (not isinstance(outcome, dict) or _parse_utc_timestamp(row["settled_at"]) is None or _parse_utc_timestamp(row["settled_at"]) > cutoff):
                    outcome = None
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            result.append({"prediction": prediction, "outcome": outcome})
        return result

    @staticmethod
    def _alert_from_row(row: sqlite3.Row) -> dict[str, object]:
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence = {"parse_error": "stored alert evidence is invalid"}
        if not isinstance(evidence, dict):
            evidence = {"value": evidence}
        return {
            "alert_id": row["alert_id"],
            "policy_version": row["policy_version"],
            "source": row["source"],
            "severity": row["severity"],
            "status": row["status"],
            "title": row["title"],
            "message": row["message"],
            "symbol": row["symbol"],
            "prediction_id": row["prediction_id"],
            "event_identity": row["event_identity"],
            "fingerprint": row["fingerprint"],
            "evidence": evidence,
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "occurrence_count": int(row["occurrence_count"]),
            "acknowledged_at": row["acknowledged_at"],
            "acknowledged_by": row["acknowledged_by"],
            "dedupe_key": row["dedupe_key"],
        }

    def upsert_alert(
        self,
        *,
        alert_id: str,
        policy_version: str,
        source: str,
        severity: str,
        title: str,
        message: str,
        event_identity: str,
        fingerprint: str,
        evidence: dict[str, Any],
        dedupe_key: str,
        first_seen_at: str,
        symbol: str | None = None,
        prediction_id: str | None = None,
    ) -> dict[str, object]:
        evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT alert_id FROM alerts WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if existing is None:
                db.execute(
                    """INSERT INTO alerts(
                        alert_id, policy_version, source, severity, status, title, message,
                        symbol, prediction_id, event_identity, fingerprint, evidence_json,
                        first_seen_at, last_seen_at, occurrence_count, dedupe_key
                    ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        alert_id,
                        policy_version,
                        source,
                        severity,
                        title,
                        message,
                        symbol,
                        prediction_id,
                        event_identity,
                        fingerprint,
                        evidence_json,
                        first_seen_at,
                        first_seen_at,
                        dedupe_key,
                    ),
                )
                created = True
            else:
                db.execute(
                    """UPDATE alerts
                          SET last_seen_at = ?, occurrence_count = occurrence_count + 1
                        WHERE dedupe_key = ?""",
                    (first_seen_at, dedupe_key),
                )
                created = False
            row = db.execute("SELECT * FROM alerts WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        assert row is not None
        return {"created": created, "deduped": not created, "alert": self._alert_from_row(row)}

    def get_alert(self, alert_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
        return self._alert_from_row(row) if row is not None else None

    def list_alerts(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
        severity: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[Any] = []
        for field, value in (("source", source), ("severity", severity), ("status", status)):
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        query = "SELECT * FROM alerts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY last_seen_at DESC, alert_id DESC LIMIT ? OFFSET ?"
        params.extend((max(1, min(int(limit), 100)), max(0, min(int(offset), 100_000))))
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [self._alert_from_row(row) for row in rows]

    def count_alerts(
        self,
        *,
        source: str | None = None,
        severity: str | None = None,
        status: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        for field, value in (("source", source), ("severity", severity), ("status", status)):
            if value:
                clauses.append(f"{field} = ?")
                params.append(value)
        query = "SELECT COUNT(*) FROM alerts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as db:
            return int(db.execute(query, tuple(params)).fetchone()[0])

    def alert_counts(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("SELECT status, COUNT(*) AS count FROM alerts GROUP BY status").fetchall()
            total = int(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
        by_status = {str(row["status"]): int(row["count"]) for row in rows}
        open_count = by_status.get("OPEN", 0)
        return {
            "total": total,
            "open": open_count,
            "unread": open_count,
            "acknowledged": by_status.get("ACKNOWLEDGED", 0),
        }

    def acknowledge_alert(
        self,
        alert_id: str,
        *,
        acknowledged_at: str,
        acknowledged_by: str = "local_user",
    ) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT status FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "OPEN":
                db.execute(
                    """UPDATE alerts
                          SET status = 'ACKNOWLEDGED', acknowledged_at = ?, acknowledged_by = ?
                        WHERE alert_id = ?""",
                    (acknowledged_at, acknowledged_by, alert_id),
                )
            updated = db.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
        assert updated is not None
        return self._alert_from_row(updated)

    def prune_alerts(self, *, keep: int = 500) -> int:
        bounded_keep = max(1, min(int(keep), 500))
        with self._connect() as db:
            result = db.execute(
                """DELETE FROM alerts
                     WHERE alert_id NOT IN (
                         SELECT alert_id FROM alerts
                          ORDER BY CASE WHEN status = 'OPEN' THEN 0 ELSE 1 END,
                                   last_seen_at DESC, alert_id DESC
                          LIMIT ?
                     )""",
                (bounded_keep,),
            )
        return result.rowcount

    def list_live_prediction_records_for_alerts(self, *, limit: int = 500) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT p.prediction_id, p.symbol, p.generated_at, p.payload_json,
                          o.payload_json AS outcome_json, o.status AS outcome_status
                     FROM predictions p
                LEFT JOIN outcomes o ON o.prediction_id = p.prediction_id
                    WHERE COALESCE(p.source_type, 'live') = 'live'
                    ORDER BY p.generated_at DESC, p.prediction_id DESC
                    LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record: dict[str, Any] = {
                "prediction_id": row["prediction_id"],
                "symbol": row["symbol"],
                "generated_at": row["generated_at"],
                "outcome_status": row["outcome_status"],
                "prediction": None,
                "outcome": None,
            }
            try:
                prediction = json.loads(row["payload_json"])
                if not isinstance(prediction, dict):
                    raise ValueError("stored prediction payload must be an object")
                record["prediction"] = prediction
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                record["prediction_error"] = str(exc)
            if row["outcome_json"]:
                try:
                    outcome = json.loads(row["outcome_json"])
                    record["outcome"] = outcome if isinstance(outcome, dict) else None
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    record["outcome_error"] = str(exc)
            records.append(record)
        return records

    @staticmethod
    def _instrument_record_from_row(row: sqlite3.Row) -> dict[str, object]:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError("stored instrument payload is not an object")
        labels = payload.get("metadata_labels", {})
        if not isinstance(labels, dict):
            labels = {}
        return {
            "instrument": instrument_from_payload(payload),
            "registry_source": str(payload.get("registry_source", "canonical")),
            "metadata_status": str(payload.get("metadata_status", "canonical")),
            "metadata_labels": {str(key): str(value) for key, value in labels.items()},
            "validation_provider": payload.get("validation_provider"),
            "validated_at": payload.get("validated_at"),
        }

    def get_instrument_record(self, symbol: str) -> dict[str, object] | None:
        normalized = symbol.strip().upper() if isinstance(symbol, str) else ""
        if not normalized:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT symbol, payload_json FROM instruments WHERE symbol = ?",
                (normalized,),
            ).fetchone()
        return self._instrument_record_from_row(row) if row is not None else None

    def get_instrument(self, symbol: str) -> Instrument | None:
        record = self.get_instrument_record(symbol)
        return record["instrument"] if record is not None else None  # type: ignore[return-value]

    def list_instrument_records(self) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT symbol, payload_json FROM instruments ORDER BY symbol ASC"
            ).fetchall()
        return [self._instrument_record_from_row(row) for row in rows]

    def list_instruments(self) -> list[Instrument]:
        return [record["instrument"] for record in self.list_instrument_records()]  # type: ignore[misc]

    def resolve_instrument(self, symbol: str) -> Instrument:
        # A durable registry entry is authoritative once it has been explicitly
        # imported or registered.  This matters for symbols introduced by a
        # later release: re-hydrating an older inferred instrument must not
        # silently replace its exchange/metadata with a new canonical mapping.
        if isinstance(symbol, str) and symbol == symbol.strip():
            stored = self.get_instrument(symbol)
            if stored is not None:
                return stored
        try:
            return instrument_for(symbol)
        except (TypeError, ValueError) as canonical_error:
            if not isinstance(symbol, str) or symbol != symbol.strip():
                raise canonical_error
            instrument = self.get_instrument(symbol)
            if instrument is None:
                raise canonical_error
            return instrument

    def save_instrument(
        self,
        instrument: Instrument,
        *,
        registry_source: str | None = None,
        metadata_status: str | None = None,
        metadata_labels: dict[str, str] | None = None,
        validation_provider: str | None = None,
        validated_at: str | None = None,
    ) -> None:
        existing: dict[str, object] = {}
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM instruments WHERE symbol = ?",
                (instrument.symbol,),
            ).fetchone()
            if row is not None:
                raw_existing = json.loads(row["payload_json"])
                if isinstance(raw_existing, dict):
                    existing = raw_existing
        effective_source = registry_source or str(existing.get("registry_source", "canonical"))
        effective_status = metadata_status or str(existing.get("metadata_status", "canonical"))
        existing_labels = existing.get("metadata_labels")
        effective_labels = metadata_labels or (existing_labels if isinstance(existing_labels, dict) else {})
        effective_provider = validation_provider if validation_provider is not None else existing.get("validation_provider")
        effective_validated_at = validated_at if validated_at is not None else existing.get("validated_at")
        payload = {
            "symbol": instrument.symbol,
            "asset_type": instrument.asset_type.value,
            "exchange": instrument.exchange,
            "currency": instrument.currency,
            "timezone": instrument.timezone,
            "trading_hours": instrument.trading_hours.value,
            "sector": instrument.sector,
            "registry_source": effective_source,
            "metadata_status": effective_status,
            "metadata_labels": effective_labels,
            "validation_provider": effective_provider,
            "validated_at": effective_validated_at,
        }
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO instruments(symbol, payload_json) VALUES (?, ?)",
                (instrument.symbol, json.dumps(payload, sort_keys=True)),
            )

    def save_snapshot(self, symbol: str, timeframe: str, captured_at: str, payload: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO market_snapshots(symbol, timeframe, captured_at, payload_json) VALUES (?, ?, ?, ?)",
                (symbol, timeframe, captured_at, json.dumps(payload, sort_keys=True)),
            )

    def save_public_hydration_run(
        self,
        *,
        run_id: str,
        started_at: str,
        finished_at: str | None,
        status: str,
        providers: dict[str, object],
        errors: list[str],
        cache: dict[str, object],
    ) -> None:
        """Persist bounded cache-refresh evidence without domain semantics."""

        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO public_hydration_runs(
                    run_id, started_at, finished_at, status, providers_json,
                    errors_json, cache_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(run_id),
                    str(started_at),
                    finished_at,
                    str(status),
                    json.dumps(providers, sort_keys=True, ensure_ascii=True),
                    json.dumps(errors[:20], sort_keys=True, ensure_ascii=True),
                    json.dumps(cache, sort_keys=True, ensure_ascii=True),
                ),
            )

    def latest_public_hydration_run(self) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM public_hydration_runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        try:
            providers = json.loads(row["providers_json"])
            errors = json.loads(row["errors_json"])
            cache = json.loads(row["cache_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            providers, errors, cache = {}, [], {}
        return {
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "providers": providers if isinstance(providers, dict) else {},
            "errors": errors if isinstance(errors, list) else [],
            "cache": cache if isinstance(cache, dict) else {},
        }

    def save_prediction(self, signal: SignalProposal) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO predictions(
                    prediction_id, symbol, generated_at, action, payload_json,
                    model_id, model_version, prompt_version, input_hash,
                    data_as_of, context_json, raw_model_response, parse_status,
                    source_type, replay_run_id, calibrated_confidence,
                    calibration_version, calibration_scope, calibration_sample_size,
                    calibration_fallback
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.prediction_id,
                    signal.instrument.symbol,
                    signal.generated_at.isoformat(),
                    signal.action.value,
                    json.dumps(signal.to_dict(), sort_keys=True),
                    signal.model_id,
                    signal.model_version,
                    signal.prompt_version,
                    signal.input_hash,
                    signal.data_as_of.isoformat() if signal.data_as_of else None,
                    signal.context_json,
                    signal.raw_model_response,
                    signal.parse_status,
                    signal.source_type,
                    signal.replay_run_id,
                    signal.calibrated_confidence,
                    signal.calibration_version,
                    signal.calibration_scope,
                    signal.calibration_sample_size,
                    signal.calibration_fallback,
                ),
            )

    def save_news_events(self, symbol: str, result: NewsFetchResult) -> None:
        with self._connect() as db:
            for event in result.events:
                db.execute(
                    "INSERT OR REPLACE INTO news_events(event_id, symbol, published_at, payload_json) VALUES (?, ?, ?, ?)",
                    (event.event_id, symbol, event.published_at.isoformat(), json.dumps(event.to_dict(), sort_keys=True)),
                )

    def get_news_event(self, event_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM news_events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return {"event_id": row["event_id"], "symbol": row["symbol"], "published_at": row["published_at"], "payload": payload}

    def list_news_events(self, symbol: str, *, limit: int = 50) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 200))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM news_events WHERE symbol = ? ORDER BY published_at DESC, event_id ASC LIMIT ?",
                (symbol.strip().upper(), bounded),
            ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            results.append({"event_id": row["event_id"], "symbol": row["symbol"], "published_at": row["published_at"], "payload": payload})
        return results

    def save_provider_snapshot(self, symbol: str, snapshot: ProviderSnapshot) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO provider_snapshots(symbol, provider, fetched_at, data_as_of, stale, error_code) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    symbol,
                    snapshot.provider,
                    snapshot.fetched_at.isoformat(),
                    snapshot.data_as_of.isoformat(),
                    int(snapshot.stale),
                    snapshot.error_code,
                ),
            )

    def save_model_run(
        self,
        *,
        prediction_id: str | None,
        model_id: str,
        started_at: str,
        latency_ms: float | None,
        input_tokens_est: int | None,
        output_chars: int | None,
        success: bool,
        error_code: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO model_runs(
                    prediction_id, model_id, started_at, latency_ms,
                    input_tokens_est, output_chars, success, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prediction_id,
                    model_id,
                    started_at,
                    latency_ms,
                    input_tokens_est,
                    output_chars,
                    int(success),
                    error_code,
                ),
            )

    def create_replay_run(
        self,
        *,
        run_id: str,
        model_id: str,
        prompt_version: str | None,
        symbols: list[str],
        timeframes: list[str],
        sampling_policy: dict[str, Any],
        manifest_hash: str,
        config: dict[str, Any] | None = None,
        status: str = "RUNNING",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO replay_runs(
                    run_id, created_at, model_id, prompt_version, symbols_json,
                    timeframes_json, sampling_policy_json, manifest_hash, status,
                    counts_json, config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    now,
                    model_id,
                    prompt_version,
                    json.dumps(symbols, sort_keys=True),
                    json.dumps(timeframes, sort_keys=True),
                    json.dumps(sampling_policy, sort_keys=True),
                    manifest_hash,
                    status,
                    "{}",
                    json.dumps(config or {}, sort_keys=True),
                ),
            )

    def update_replay_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        counts: dict[str, Any] | None = None,
        completed_at: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM replay_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"replay run not found: {run_id}")
            db.execute(
                """UPDATE replay_runs
                   SET status = ?, counts_json = ?, completed_at = ?, error_code = ?
                 WHERE run_id = ?""",
                (
                    status or row["status"],
                    json.dumps(counts, sort_keys=True) if counts is not None else row["counts_json"],
                    completed_at if completed_at is not None else row["completed_at"],
                    error_code if error_code is not None else row["error_code"],
                    run_id,
                ),
            )

    def get_replay_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM replay_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "model_id": row["model_id"],
            "prompt_version": row["prompt_version"],
            "symbols": json.loads(row["symbols_json"]),
            "timeframes": json.loads(row["timeframes_json"]),
            "sampling_policy": json.loads(row["sampling_policy_json"]),
            "manifest_hash": row["manifest_hash"],
            "status": row["status"],
            "counts": json.loads(row["counts_json"] or "{}"),
            "config": json.loads(row["config_json"] or "{}"),
            "completed_at": row["completed_at"],
            "error_code": row["error_code"],
        }

    def list_replay_runs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        bounded_offset = max(0, min(int(offset), 100_000))
        query = "SELECT run_id FROM replay_runs"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?"
        params.extend((bounded_limit, bounded_offset))
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [self.get_replay_run(row["run_id"]) for row in rows]  # type: ignore[misc]

    def find_resumable_replay_run(self, manifest_hash: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT run_id FROM replay_runs
                   WHERE manifest_hash = ? AND status <> 'COMPLETED'
                   ORDER BY created_at DESC LIMIT 1""",
                (manifest_hash,),
            ).fetchone()
        return self.get_replay_run(row["run_id"]) if row else None

    def save_replay_sample(
        self,
        *,
        run_id: str,
        symbol: str,
        timeframe: str,
        as_of: str,
        capability_flags: dict[str, Any],
        prediction_id: str | None,
        status: str,
        error_code: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO replay_samples(
                    run_id, symbol, timeframe, as_of, capability_flags_json,
                    prediction_id, status, error_code, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    symbol,
                    timeframe,
                    as_of,
                    json.dumps(capability_flags, sort_keys=True),
                    prediction_id,
                    status,
                    error_code,
                    started_at,
                    completed_at,
                ),
            )

    def get_replay_sample(self, run_id: str, symbol: str, timeframe: str, as_of: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM replay_samples
                   WHERE run_id = ? AND symbol = ? AND timeframe = ? AND as_of = ?""",
                (run_id, symbol, timeframe, as_of),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "as_of": row["as_of"],
            "capability_flags": json.loads(row["capability_flags_json"] or "{}"),
            "prediction_id": row["prediction_id"],
            "status": row["status"],
            "error_code": row["error_code"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    def list_replay_samples(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM replay_samples
                   WHERE run_id = ? ORDER BY as_of, symbol, timeframe""",
                (run_id,),
            ).fetchall()
        return [self.get_replay_sample(run_id, row["symbol"], row["timeframe"], row["as_of"]) for row in rows]  # type: ignore[misc]

    def save_performance_snapshot(self, payload: dict[str, Any]) -> int:
        scope = payload.get("scope") or {}
        metrics = payload.get("metrics") or payload
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO performance_snapshots(
                    scope_json, window_start, window_end, sample_count,
                    actionable_count, metrics_json, model_id, prompt_version,
                    source_type, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    json.dumps(scope, sort_keys=True),
                    payload.get("window_start"),
                    payload.get("window_end"),
                    int(payload.get("sample_count", metrics.get("sample_count", 0))),
                    int(payload.get("actionable_count", metrics.get("actionable_count", 0))),
                    json.dumps(metrics, sort_keys=True),
                    payload.get("model_id") or scope.get("model_id"),
                    payload.get("prompt_version") or scope.get("prompt_version"),
                    payload.get("source_type") or scope.get("source_type") or "live",
                    payload.get("created_at") or datetime.now(timezone.utc).isoformat(),
                    payload.get("status", metrics.get("status", "PRELIMINARY")),
                ),
            )
            return int(cursor.lastrowid)

    def list_performance_snapshots(self, *, source_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        query = "SELECT * FROM performance_snapshots"
        params: list[Any] = []
        if source_type:
            query += " WHERE source_type = ?"
            params.append(source_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(bounded_limit)
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            results.append(
                {
                    "snapshot_id": row["snapshot_id"],
                    "scope": json.loads(row["scope_json"]),
                    "window_start": row["window_start"],
                    "window_end": row["window_end"],
                    "sample_count": row["sample_count"],
                    "actionable_count": row["actionable_count"],
                    "metrics": metrics,
                    "audit_metadata": metrics.get("audit_metadata") if isinstance(metrics, dict) else None,
                    "model_id": row["model_id"],
                    "prompt_version": row["prompt_version"],
                    "source_type": row["source_type"],
                    "created_at": row["created_at"],
                    "status": row["status"],
                }
            )
        return results

    def prune_performance_snapshots(self, *, source_type: str = "live", keep: int = 100) -> int:
        """Keep at most 100 snapshots per source type for bounded local runtime storage."""

        bounded_keep = max(1, min(int(keep), 100))
        with self._connect() as db:
            result = db.execute(
                """DELETE FROM performance_snapshots
                     WHERE source_type = ?
                       AND snapshot_id NOT IN (
                           SELECT snapshot_id FROM performance_snapshots
                            WHERE source_type = ?
                            ORDER BY created_at DESC, snapshot_id DESC
                            LIMIT ?
                       )""",
                (source_type, source_type, bounded_keep),
            )
        return result.rowcount

    def save_calibration_result(self, payload: dict[str, Any]) -> None:
        buckets = payload.get("buckets") or []
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO calibration_results(
                    calibration_id, version, scope_json, method, params_json,
                    sample_count, trained_until, brier_raw, brier_calibrated,
                    ece_raw, ece_calibrated, status, fallback, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["calibration_id"],
                    payload["version"],
                    json.dumps(payload.get("scope") or {}, sort_keys=True),
                    payload.get("method", "empirical_beta_shrinkage"),
                    json.dumps(payload.get("params") or {}, sort_keys=True),
                    int(payload.get("sample_count", 0)),
                    payload.get("trained_until"),
                    payload.get("brier_raw"),
                    payload.get("brier_calibrated"),
                    payload.get("ece_raw"),
                    payload.get("ece_calibrated"),
                    payload.get("status", "INSUFFICIENT_SAMPLE"),
                    payload.get("fallback"),
                    payload.get("created_at") or datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.execute("DELETE FROM calibration_buckets WHERE calibration_id = ?", (payload["calibration_id"],))
            for bucket in buckets:
                db.execute(
                    """INSERT INTO calibration_buckets(
                        calibration_id, lower_bound, upper_bound, n, wins,
                        empirical_rate, shrunk_rate
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload["calibration_id"],
                        float(bucket["lower"]),
                        float(bucket["upper"]),
                        int(bucket.get("n", 0)),
                        int(bucket.get("wins", 0)),
                        bucket.get("empirical_rate"),
                        bucket.get("shrunk_rate"),
                    ),
                )

    def list_calibration_results(self, *, limit: int = 20) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 100))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM calibration_results ORDER BY created_at DESC LIMIT ?",
                (bounded_limit,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            buckets = db_buckets = []
            with self._connect() as db:
                db_buckets = db.execute(
                    "SELECT lower_bound, upper_bound, n, wins, empirical_rate, shrunk_rate FROM calibration_buckets WHERE calibration_id = ? ORDER BY lower_bound",
                    (row["calibration_id"],),
                ).fetchall()
            buckets = [
                {
                    "lower": item["lower_bound"],
                    "upper": item["upper_bound"],
                    "n": item["n"],
                    "wins": item["wins"],
                    "empirical_rate": item["empirical_rate"],
                    "shrunk_rate": item["shrunk_rate"],
                }
                for item in db_buckets
            ]
            results.append(
                {
                    "calibration_id": row["calibration_id"],
                    "version": row["version"],
                    "scope": json.loads(row["scope_json"]),
                    "method": row["method"],
                    "params": json.loads(row["params_json"]),
                    "sample_count": row["sample_count"],
                    "trained_until": row["trained_until"],
                    "brier_raw": row["brier_raw"],
                    "brier_calibrated": row["brier_calibrated"],
                    "ece_raw": row["ece_raw"],
                    "ece_calibrated": row["ece_calibrated"],
                    "status": row["status"],
                    "fallback": row["fallback"],
                    "created_at": row["created_at"],
                    "buckets": buckets,
                }
            )
        return results

    def update_prediction_calibration(self, prediction_id: str, calibration: dict[str, Any]) -> None:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM predictions WHERE prediction_id = ?", (prediction_id,)).fetchone()
            if row is None:
                raise KeyError(f"prediction not found: {prediction_id}")
            payload = json.loads(row["payload_json"])
            for key in (
                "calibrated_confidence",
                "calibration_version",
                "calibration_scope",
                "calibration_sample_size",
                "calibration_fallback",
            ):
                payload[key] = calibration.get(key)
            db.execute(
                """UPDATE predictions SET payload_json = ?, calibrated_confidence = ?,
                    calibration_version = ?, calibration_scope = ?, calibration_sample_size = ?,
                    calibration_fallback = ? WHERE prediction_id = ?""",
                (
                    json.dumps(payload, sort_keys=True),
                    calibration.get("calibrated_confidence"),
                    calibration.get("calibration_version"),
                    calibration.get("calibration_scope"),
                    calibration.get("calibration_sample_size"),
                    calibration.get("calibration_fallback"),
                    prediction_id,
                ),
            )

    @staticmethod
    def _linked_record_from_row(row: sqlite3.Row) -> dict[str, Any]:
        prediction = json.loads(row["prediction_json"])
        paper_trade = None
        if row["followed_at"] is not None:
            paper_trade = {
                "prediction_id": row["prediction_id"],
                "followed_at": row["followed_at"],
                "status": row["paper_status"],
            }
        outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else None
        return {
            "prediction_id": row["prediction_id"],
            "prediction": prediction,
            "paper_trade": paper_trade,
            "outcome": outcome,
            "outcome_status": row["outcome_status"],
        }

    def get_prediction_record(self, prediction_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT p.prediction_id AS prediction_id, p.payload_json AS prediction_json,
                          pt.followed_at AS followed_at, pt.status AS paper_status,
                          o.payload_json AS outcome_json, o.status AS outcome_status
                     FROM predictions p
                LEFT JOIN paper_trades pt ON pt.prediction_id = p.prediction_id
                LEFT JOIN outcomes o ON o.prediction_id = p.prediction_id
                    WHERE p.prediction_id = ?""",
                (prediction_id,),
            ).fetchone()
        return self._linked_record_from_row(row) if row is not None else None

    def list_paper_trade_records(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        symbol: str | None = None,
        timeframe: str | None = None,
        action: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
        outcome_status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("p.symbol = ?")
            params.append(symbol.upper())
        if timeframe:
            clauses.append("json_extract(p.payload_json, '$.analysis_timeframe') = ?")
            params.append(timeframe)
        if action:
            clauses.append("p.action = ?")
            params.append(action)
        if source_type:
            clauses.append("COALESCE(p.source_type, 'live') = ?")
            params.append(source_type)
        if status:
            clauses.append("pt.status = ?")
            params.append(status)
        if outcome_status:
            clauses.append("o.status = ?")
            params.append(outcome_status)
        query = """SELECT p.prediction_id AS prediction_id, p.payload_json AS prediction_json,
                          pt.followed_at AS followed_at, pt.status AS paper_status,
                          o.payload_json AS outcome_json, o.status AS outcome_status
                     FROM paper_trades pt
                     JOIN predictions p ON p.prediction_id = pt.prediction_id
                LEFT JOIN outcomes o ON o.prediction_id = p.prediction_id"""
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY pt.followed_at DESC, pt.prediction_id DESC LIMIT ? OFFSET ?"
        params.extend((max(1, min(int(limit), 500)), max(0, min(int(offset), 100_000))))
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [
            {
                "prediction_id": record["prediction_id"],
                "followed_at": record["paper_trade"]["followed_at"],
                "status": record["paper_trade"]["status"],
                "prediction": record["prediction"],
                "outcome": record["outcome"],
                "outcome_status": record["outcome_status"],
            }
            for record in (self._linked_record_from_row(row) for row in rows)
        ]

    def get_paper_trade_record(self, prediction_id: str) -> dict[str, Any] | None:
        record = self.get_prediction_record(prediction_id)
        if record is None or record["paper_trade"] is None:
            return None
        return {
            "prediction_id": prediction_id,
            "followed_at": record["paper_trade"]["followed_at"],
            "status": record["paper_trade"]["status"],
            "prediction": record["prediction"],
            "outcome": record["outcome"],
            "outcome_status": record["outcome_status"],
        }

    def list_outcome_records(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        symbol: str | None = None,
        timeframe: str | None = None,
        action: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("p.symbol = ?")
            params.append(symbol.upper())
        if timeframe:
            clauses.append("json_extract(p.payload_json, '$.analysis_timeframe') = ?")
            params.append(timeframe)
        if action:
            clauses.append("p.action = ?")
            params.append(action)
        if source_type:
            clauses.append("COALESCE(p.source_type, 'live') = ?")
            params.append(source_type)
        if status:
            clauses.append("o.status = ?")
            params.append(status)
        query = """SELECT p.prediction_id AS prediction_id, p.payload_json AS prediction_json,
                          pt.followed_at AS followed_at, pt.status AS paper_status,
                          o.payload_json AS outcome_json, o.status AS outcome_status
                     FROM outcomes o
                     JOIN predictions p ON p.prediction_id = o.prediction_id
                LEFT JOIN paper_trades pt ON pt.prediction_id = p.prediction_id"""
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY o.settled_at DESC, o.prediction_id DESC LIMIT ? OFFSET ?"
        params.extend((max(1, min(int(limit), 500)), max(0, min(int(offset), 100_000))))
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            record = self._linked_record_from_row(row)
            outcome = dict(record["outcome"] or {})
            outcome["prediction"] = record["prediction"]
            outcome["paper_trade"] = record["paper_trade"]
            outcome["outcome"] = record["outcome"]
            outcome["outcome_status"] = record["outcome_status"]
            results.append(outcome)
        return results

    def get_outcome_record(self, prediction_id: str) -> dict[str, Any] | None:
        record = self.get_prediction_record(prediction_id)
        if record is None or record["outcome"] is None:
            return None
        outcome = dict(record["outcome"])
        outcome["prediction"] = record["prediction"]
        outcome["paper_trade"] = record["paper_trade"]
        outcome["outcome"] = record["outcome"]
        outcome["outcome_status"] = record["outcome_status"]
        return outcome

    def list_unsettled_live_prediction_records(
        self,
        *,
        limit: int = 100,
        as_of: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded live predictions without outcomes and active retry windows."""

        bounded_limit = max(1, min(int(limit), 500))
        if isinstance(as_of, datetime):
            retry_cutoff = self._utc_timestamp(as_of)
        elif isinstance(as_of, str) and as_of:
            try:
                retry_cutoff = self._utc_timestamp(datetime.fromisoformat(as_of.replace("Z", "+00:00")))
            except ValueError:
                retry_cutoff = as_of
        else:
            retry_cutoff = self._utc_timestamp()
        with self._connect() as db:
            rows = db.execute(
                """WITH latest_settlement AS (
                         SELECT prediction_id, retry_after_at,
                                ROW_NUMBER() OVER (
                                    PARTITION BY prediction_id
                                    ORDER BY COALESCE(finished_at, started_at, '') DESC, item_id DESC
                                ) AS latest_rank
                           FROM scheduler_items
                          WHERE stage = 'settlement'
                     )
                     SELECT p.prediction_id, p.symbol, p.generated_at, p.action, p.payload_json
                       FROM predictions p
                  LEFT JOIN outcomes o ON o.prediction_id = p.prediction_id
                  LEFT JOIN latest_settlement ls
                         ON ls.prediction_id = p.prediction_id AND ls.latest_rank = 1
                      WHERE COALESCE(p.source_type, 'live') = 'live'
                        AND o.prediction_id IS NULL
                        AND (ls.retry_after_at IS NULL OR ls.retry_after_at <= ?)
                      ORDER BY p.generated_at ASC, p.prediction_id ASC
                      LIMIT ?""",
                (retry_cutoff, bounded_limit),
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record: dict[str, Any] = {
                "prediction_id": row["prediction_id"],
                "symbol": row["symbol"],
                "generated_at": row["generated_at"],
                "action": row["action"],
                "prediction": None,
            }
            try:
                prediction = json.loads(row["payload_json"])
                if not isinstance(prediction, dict):
                    raise ValueError("stored prediction payload must be an object")
                record["prediction"] = prediction
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                record["prediction_error"] = str(exc)
            records.append(record)
        return records

    def list_prediction_records(
        self,
        *,
        source_type: str | None = None,
        replay_run_id: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("COALESCE(p.source_type, 'live') = ?")
            params.append(source_type)
        if replay_run_id:
            clauses.append("p.replay_run_id = ?")
            params.append(replay_run_id)
        if symbol:
            clauses.append("p.symbol = ?")
            params.append(symbol.upper())
        if timeframe:
            clauses.append("json_extract(p.payload_json, '$.analysis_timeframe') = ?")
            params.append(timeframe)
        if model_id:
            clauses.append("p.model_id = ?")
            params.append(model_id)
        if prompt_version:
            clauses.append("p.prompt_version = ?")
            params.append(prompt_version)
        query = """SELECT p.payload_json AS prediction_json, o.payload_json AS outcome_json,
                    o.status AS outcome_status
                 FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.prediction_id"""
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY p.generated_at ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, min(int(limit), 100000)))
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                prediction = json.loads(row["prediction_json"])
                if not isinstance(prediction, dict):
                    continue
                outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else None
                if outcome is not None and not isinstance(outcome, dict):
                    continue
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            results.append({"prediction": prediction, "outcome": outcome, "outcome_status": row["outcome_status"]})
        return results

    def list_latest_prediction_records(
        self,
        symbols: Iterable[str],
        *,
        source_type: str = "live",
    ) -> dict[str, dict[str, Any]]:
        """Return the newest read-only prediction/outcome record per requested symbol."""

        normalized_symbols = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        if not normalized_symbols:
            return {}
        placeholders = ", ".join("?" for _ in normalized_symbols)
        query = f"""SELECT p.payload_json AS prediction_json, o.payload_json AS outcome_json,
                           o.status AS outcome_status
                      FROM predictions p
                 LEFT JOIN outcomes o ON o.prediction_id = p.prediction_id
                     WHERE COALESCE(p.source_type, 'live') = ?
                       AND p.symbol IN ({placeholders})
                  ORDER BY p.symbol ASC, p.generated_at DESC, p.prediction_id DESC"""
        with self._connect() as db:
            rows = db.execute(query, (source_type, *normalized_symbols)).fetchall()
        results: dict[str, dict[str, Any]] = {}
        for row in rows:
            prediction = json.loads(row["prediction_json"])
            if not isinstance(prediction, dict):
                continue
            instrument = prediction.get("instrument")
            nested_symbol = instrument.get("symbol") if isinstance(instrument, dict) else None
            symbol = str(prediction.get("symbol") or nested_symbol or "").upper()
            if symbol and symbol not in results:
                results[symbol] = {
                    "prediction": prediction,
                    "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None,
                    "outcome_status": row["outcome_status"],
                }
        return results

    def list_replay_model_latencies(self, replay_run_id: str) -> list[float]:
        """Return successful model latencies for every prediction in one replay run."""

        with self._connect() as db:
            rows = db.execute(
                """SELECT mr.latency_ms
                   FROM model_runs mr
                   JOIN predictions p ON p.prediction_id = mr.prediction_id
                  WHERE p.replay_run_id = ? AND mr.latency_ms IS NOT NULL
                  ORDER BY p.generated_at ASC, mr.id ASC""",
                (replay_run_id,),
            ).fetchall()
        return [float(row["latency_ms"]) for row in rows]

    def follow_prediction(self, prediction_id: str, followed_at: str, status: str = "OPEN") -> None:
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM predictions WHERE prediction_id = ?", (prediction_id,)).fetchone()
            if exists is None:
                raise KeyError(f"prediction not found: {prediction_id}")
            updated = db.execute(
                "UPDATE paper_trades SET status = ? WHERE prediction_id = ?",
                (status, prediction_id),
            )
            if updated.rowcount == 0:
                db.execute(
                    "INSERT INTO paper_trades(prediction_id, followed_at, status) VALUES (?, ?, ?)",
                    (prediction_id, followed_at, status),
                )

    def save_outcome(self, outcome: Outcome) -> bool:
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM predictions WHERE prediction_id = ?", (outcome.prediction_id,)).fetchone()
            if exists is None:
                raise KeyError(f"prediction not found: {outcome.prediction_id}")
            result = db.execute(
                """INSERT INTO outcomes(prediction_id, settled_at, status, payload_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(prediction_id) DO NOTHING""",
                (outcome.prediction_id, outcome.settled_at.isoformat(), outcome.status.value, json.dumps(outcome.to_dict(), sort_keys=True)),
            )
        return result.rowcount > 0

    def save_daily_brief(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = (
            "brief_id", "generated_at", "as_of", "language", "model_id",
            "model_tier", "route_reason", "source_hash", "sources", "missing",
            "content", "capability",
        )
        if any(key not in payload for key in required):
            raise ValueError("daily brief is missing required audit fields")
        for key in ("generated_at", "as_of"):
            if _parse_utc_timestamp(payload[key]) is None:
                raise ValueError("daily brief timestamps must be timezone-aware")
        if payload["language"] not in {"en", "zh-CN"}:
            raise ValueError("daily brief language is unsupported")
        if not isinstance(payload["content"], str) or not payload["content"].strip():
            raise ValueError("daily brief content must be non-empty")
        with self._connect() as db:
            db.execute(
                """INSERT INTO daily_briefs(
                       brief_id, generated_at, as_of, language, model_id,
                       model_tier, route_reason, source_hash, sources_json,
                       missing_json, content, capability_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["brief_id"], payload["generated_at"], payload["as_of"],
                    payload["language"], payload["model_id"], payload["model_tier"],
                    payload["route_reason"], payload["source_hash"],
                    json.dumps(payload["sources"], sort_keys=True),
                    json.dumps(payload["missing"], sort_keys=True), payload["content"],
                    json.dumps(payload["capability"], sort_keys=True),
                ),
            )
        return dict(payload)

    def latest_daily_brief(self, *, language: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM daily_briefs"
        params: tuple[object, ...] = ()
        if language is not None:
            if language not in {"en", "zh-CN"}:
                raise ValueError("daily brief language is unsupported")
            query += " WHERE language = ?"
            params = (language,)
        query += " ORDER BY generated_at DESC, brief_id DESC LIMIT 1"
        with self._connect() as db:
            row = db.execute(query, params).fetchone()
        if row is None:
            return None
        return {
            "brief_id": row["brief_id"],
            "generated_at": row["generated_at"],
            "as_of": row["as_of"],
            "language": row["language"],
            "model_id": row["model_id"],
            "model_tier": row["model_tier"],
            "route_reason": row["route_reason"],
            "source_hash": row["source_hash"],
            "sources": json.loads(row["sources_json"]),
            "missing": json.loads(row["missing_json"]),
            "content": row["content"],
            "capability": json.loads(row["capability_json"]),
        }

    @staticmethod
    def _monitoring_policy_from_row(row: sqlite3.Row) -> dict[str, object]:
        def load_json(name: str, fallback: object) -> object:
            try:
                return json.loads(row[name])
            except (TypeError, ValueError, json.JSONDecodeError):
                return fallback

        notify = load_json("notify_json", {})
        if not isinstance(notify, dict):
            notify = {}
        notify_in_app = notify.get("in_app", notify.get("app", True))
        notify_native = notify.get("native_notification", notify.get("desktop", True))
        return {
            "contract_version": "monitoring_policy_v1",
            "instrument_id": row["instrument_id"],
            "enabled": bool(row["enabled"]),
            "primary_timeframe": row["primary_timeframe"],
            "context_timeframe": row["context_timeframe"],
            "trigger_types": [
                _normalize_monitoring_trigger(item)
                for item in (load_json("trigger_types_json", []) if isinstance(load_json("trigger_types_json", []), list) else [])
            ],
            "min_trigger_score": float(row["min_trigger_score"]),
            "ai_min_confidence": float(row["ai_min_confidence"]),
            "cooldown_minutes": int(row["cooldown_minutes"]),
            "quiet_hours": load_json("quiet_hours_json", {}),
            "notify": {**notify, "in_app": bool(notify_in_app), "native_notification": bool(notify_native), "desktop": bool(notify_native)},
            "notify_in_app": bool(notify_in_app),
            "notify_native_notification": bool(notify_native),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert_monitoring_policy(self, policy: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
        instrument_id = str(policy.get("instrument_id") or "").strip().upper()
        if not instrument_id:
            raise ValueError("monitoring policy instrument_id is required")
        timestamp = self._utc_timestamp(now)
        trigger_types = policy.get("trigger_types", [])
        quiet_hours = policy.get("quiet_hours", {})
        notify = policy.get("notify", {})
        raw_notify_in_app = policy.get("notify_in_app", notify.get("in_app", notify.get("app", True)) if isinstance(notify, dict) else True)
        raw_notify_native = policy.get("notify_native_notification", notify.get("native_notification", notify.get("desktop", True)) if isinstance(notify, dict) else True)
        if not isinstance(trigger_types, list) or not all(isinstance(item, str) and item.strip() for item in trigger_types):
            raise ValueError("monitoring policy trigger_types must be a list of strings")
        if not isinstance(quiet_hours, dict) or not isinstance(notify, dict):
            raise ValueError("monitoring policy quiet_hours and notify must be objects")
        if type(raw_notify_in_app) is not bool or type(raw_notify_native) is not bool:
            raise ValueError("monitoring policy notification preferences must be booleans")
        canonical_trigger_types = tuple(dict.fromkeys(_normalize_monitoring_trigger(item) for item in trigger_types))
        if type(policy.get("enabled", False)) is not bool:
            raise ValueError("monitoring policy enabled must be a boolean")
        primary_timeframe = str(policy.get("primary_timeframe", "15m")).strip().lower()
        context_timeframe = str(policy.get("context_timeframe", "1h")).strip().lower()
        if primary_timeframe != "15m" or context_timeframe not in {"15m", "1h"}:
            raise ValueError("monitoring policy timeframes must be 15m and 1h")
        if not trigger_types:
            raise ValueError("monitoring policy requires at least one trigger type")
        raw_min_trigger_score = policy.get("min_trigger_score", 0.65)
        raw_ai_min_confidence = policy.get("ai_min_confidence", 0.60)
        raw_cooldown_minutes = policy.get("cooldown_minutes", 60)
        if isinstance(raw_min_trigger_score, bool) or not isinstance(raw_min_trigger_score, (int, float)):
            raise ValueError("min_trigger_score must be a number")
        if isinstance(raw_ai_min_confidence, bool) or not isinstance(raw_ai_min_confidence, (int, float)):
            raise ValueError("ai_min_confidence must be a number")
        if isinstance(raw_cooldown_minutes, bool) or type(raw_cooldown_minutes) is not int:
            raise ValueError("cooldown_minutes must be an integer")
        try:
            min_trigger_score = float(raw_min_trigger_score)
            ai_min_confidence = float(raw_ai_min_confidence)
            cooldown_minutes = raw_cooldown_minutes
        except (TypeError, ValueError) as exc:
            raise ValueError("monitoring policy numeric fields are invalid") from exc
        if not math.isfinite(min_trigger_score) or not 0.0 <= min_trigger_score <= 1.0:
            raise ValueError("min_trigger_score must be in [0, 1]")
        if not math.isfinite(ai_min_confidence) or not 0.0 <= ai_min_confidence <= 1.0:
            raise ValueError("ai_min_confidence must be in [0, 1]")
        if not 1 <= cooldown_minutes <= 24 * 60:
            raise ValueError("cooldown_minutes must be between 1 and 1440")
        with self._connect() as db:
            existing = db.execute(
                "SELECT created_at FROM monitoring_policies WHERE instrument_id = ?",
                (instrument_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing is not None else timestamp
            db.execute(
                """INSERT OR REPLACE INTO monitoring_policies(
                    instrument_id, enabled, primary_timeframe, context_timeframe,
                    trigger_types_json, min_trigger_score, ai_min_confidence,
                    cooldown_minutes, quiet_hours_json, notify_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    instrument_id,
                    int(bool(policy.get("enabled", False))),
                    primary_timeframe,
                    context_timeframe,
                    json.dumps(sorted(canonical_trigger_types), ensure_ascii=True),
                    min_trigger_score,
                    ai_min_confidence,
                    cooldown_minutes,
                    json.dumps(quiet_hours, sort_keys=True, ensure_ascii=True),
                    json.dumps({**notify, "in_app": raw_notify_in_app, "native_notification": raw_notify_native, "desktop": raw_notify_native}, sort_keys=True, ensure_ascii=True),
                    created_at,
                    timestamp,
                ),
            )
            row = db.execute("SELECT * FROM monitoring_policies WHERE instrument_id = ?", (instrument_id,)).fetchone()
        assert row is not None
        return self._monitoring_policy_from_row(row)

    def get_monitoring_policy(self, instrument_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM monitoring_policies WHERE instrument_id = ?", (instrument_id.strip().upper(),)).fetchone()
        return self._monitoring_policy_from_row(row) if row is not None else None

    def list_monitoring_policies(self, *, enabled: bool | None = None) -> list[dict[str, object]]:
        query = "SELECT * FROM monitoring_policies"
        params: tuple[object, ...] = ()
        if enabled is not None:
            query += " WHERE enabled = ?"
            params = (int(enabled),)
        query += " ORDER BY instrument_id ASC"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [self._monitoring_policy_from_row(row) for row in rows]

    def delete_monitoring_policy(self, instrument_id: str) -> bool:
        with self._connect() as db:
            result = db.execute("DELETE FROM monitoring_policies WHERE instrument_id = ?", (instrument_id.strip().upper(),))
        return result.rowcount > 0

    @staticmethod
    def _bar_seconds(timeframe: str) -> int:
        values = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14_400, "1d": 86_400}
        try:
            return values[timeframe.lower()]
        except KeyError as exc:
            raise ValueError(f"unsupported bar timeframe: {timeframe}") from exc

    def upsert_market_bars(
        self,
        symbol: str,
        timeframe: str,
        bars: Iterable[object],
        *,
        provider: str,
        data_as_of: datetime | str,
        now: datetime | None = None,
        instrument: Instrument | None = None,
        instrument_key: str | None = None,
        venue: str | None = None,
        market_type: str | None = None,
        native_symbol: str | None = None,
        settle_currency: str | None = None,
        price_type: str | None = None,
        volume_unit: str | None = None,
    ) -> int:
        interval = self._bar_seconds(timeframe)
        received_at = self._utc_timestamp(now)
        data_as_of_text = self._utc_timestamp(data_as_of) if isinstance(data_as_of, datetime) else str(data_as_of)
        parsed_data_as_of = _parse_utc_timestamp(data_as_of_text)
        if parsed_data_as_of is None:
            raise ValueError("market bar data_as_of must be an ISO timestamp")
        provider_name = str(provider or "").strip()
        if not provider_name:
            raise ValueError("market bar provider is required")
        normalized_symbol = symbol.strip().upper()
        if instrument is not None:
            instrument_key = instrument.instrument_key
            venue = instrument.venue
            market_type = instrument.market_type
            native_symbol = instrument.native_symbol
            settle_currency = instrument.settle_currency
            price_type = instrument.price_type
        explicit_identity = any(value is not None for value in (instrument, instrument_key, venue, market_type, native_symbol, settle_currency, price_type))
        if instrument_key is None:
            instrument_key = canonical_instrument_key(
                venue or (provider_name if explicit_identity else "legacy"),
                market_type or "unknown",
                native_symbol or normalized_symbol,
                settle_currency or "UNKNOWN",
                price_type or "last",
            )
        identity_parts = instrument_key.split(":")
        if len(identity_parts) != 5:
            raise ValueError("instrument_key must have five canonical components")
        identity_venue, identity_market, identity_native, identity_settle, identity_price = identity_parts
        if native_symbol and identity_native != str(native_symbol).strip().upper():
            raise ValueError("instrument_key/native_symbol mismatch")
        # Providers may use a lossless separator in their native contract id
        # (Gate uses BTC_USDT) while the application request uses BTCUSDT.
        # Compare the compact symbol only for this request-to-identity
        # validation; keep ``identity_native`` unchanged in the stored key so
        # the provider's actual identifier remains auditable.
        compact_requested = "".join(char for char in normalized_symbol if char.isalnum())
        compact_identity = "".join(char for char in identity_native if char.isalnum())
        if identity_native != normalized_symbol and compact_requested != compact_identity:
            raise ValueError("instrument_key does not identify the requested symbol")
        fetched_at = received_at
        rows: list[tuple[object, ...]] = []
        legacy_rows: list[tuple[object, ...]] = []
        for item in bars:
            if isinstance(item, dict):
                timestamp = item.get("timestamp") or item.get("bar_start")
                values = (item.get("open"), item.get("high"), item.get("low"), item.get("close"), item.get("volume"))
                item_event_time = item.get("event_time", item.get("event_at"))
                item_available_at = item.get("available_at", item.get("data_as_of"))
                item_first_received = item.get("first_received_at", item.get("received_at"))
                item_fetched_at = item.get("fetched_at", item.get("received_at"))
                item_revision = item.get("revision_id")
                item_source = item.get("source", item.get("provider"))
                item_volume_unit = item.get("volume_unit", volume_unit)
                item_closed = item.get("is_closed")
            else:
                timestamp = getattr(item, "timestamp", None) or getattr(item, "bar_start", None)
                values = tuple(getattr(item, field, None) for field in ("open", "high", "low", "close", "volume"))
                item_event_time = getattr(item, "event_time", None) or getattr(item, "event_at", None)
                item_available_at = getattr(item, "available_at", None) or getattr(item, "data_as_of", None)
                item_first_received = getattr(item, "first_received_at", None) or getattr(item, "received_at", None)
                item_fetched_at = getattr(item, "fetched_at", None) or getattr(item, "received_at", None)
                item_revision = getattr(item, "revision_id", None)
                item_source = getattr(item, "source", None)
                item_volume_unit = getattr(item, "volume_unit", None) or volume_unit
                item_closed = getattr(item, "is_closed", None)
            if isinstance(timestamp, datetime):
                bar_start = self._utc_timestamp(timestamp)
                bar_end = self._utc_timestamp(timestamp + timedelta(seconds=interval))
            else:
                bar_start = str(timestamp or "")
                parsed = _parse_utc_timestamp(bar_start)
                if parsed is None:
                    raise ValueError("market bar timestamp must be an ISO timestamp")
                bar_start = self._utc_timestamp(parsed)
                bar_end = self._utc_timestamp(parsed + timedelta(seconds=interval))
            if not bar_start or any(value is None for value in values):
                raise ValueError("market bar is incomplete")
            try:
                numeric = tuple(float(value) for value in values)
            except (TypeError, ValueError) as exc:
                raise ValueError("market bar numeric fields must be numbers") from exc
            if any(not math.isfinite(value) for value in numeric):
                raise ValueError("market bar numeric fields must be finite")
            open_price, high_price, low_price, close_price, volume = numeric
            if low_price <= 0 or close_price <= 0 or volume < 0 or high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
                raise ValueError("market bar violates OHLCV bounds")
            start_time = _parse_utc_timestamp(bar_start)
            end_time = _parse_utc_timestamp(bar_end)
            if start_time is None or end_time is None:
                raise ValueError("market bar timestamps must be timezone-aware ISO values")
            def timestamp_text(value: object, fallback: str) -> str:
                if isinstance(value, datetime):
                    return self._utc_timestamp(value)
                parsed = _parse_utc_timestamp(value)
                return self._utc_timestamp(parsed) if parsed is not None else fallback

            event_time = timestamp_text(item_event_time, bar_start)
            first_received = timestamp_text(item_first_received, received_at)
            available_at = timestamp_text(item_available_at, data_as_of_text)
            item_fetched = timestamp_text(item_fetched_at, fetched_at)
            closed = int(item_closed) if item_closed is not None else int(end_time <= (_parse_utc_timestamp(received_at) or datetime.now(timezone.utc)))
            raw_payload = {
                "symbol": normalized_symbol,
                "timeframe": timeframe.lower(),
                "bar_start": bar_start,
                "bar_end": bar_end,
                "open": numeric[0],
                "high": numeric[1],
                "low": numeric[2],
                "close": numeric[3],
                "volume": numeric[4],
                "volume_unit": item_volume_unit or "UNKNOWN",
                "event_time": event_time,
                "data_as_of": data_as_of_text,
                "first_received_at": first_received,
                "available_at": available_at,
                "fetched_at": item_fetched,
                "received_at": received_at,
                "source": str(item_source or provider_name),
            }
            raw_hash = hashlib.sha256(json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
            revision_id = str(item_revision or raw_hash[:32]).strip()
            quality_status = "VALID"
            if not explicit_identity or identity_venue == "legacy" or identity_market == "unknown" or identity_settle == "UNKNOWN":
                quality_status = "LEGACY_UNVERIFIED"
            elif parsed_data_as_of > end_time:
                quality_status = "RECONSTRUCTED_LATE"
            row = (
                instrument_key,
                normalized_symbol,
                identity_venue,
                identity_market,
                identity_native,
                identity_settle,
                identity_price,
                timeframe.lower(),
                bar_start,
                bar_end,
                revision_id,
                *numeric,
                item_volume_unit or "UNKNOWN",
                event_time,
                first_received,
                available_at,
                item_fetched,
                str(item_source or provider_name),
                raw_hash,
                quality_status,
                closed,
                json.dumps({**raw_payload, "provider": provider_name}, sort_keys=True, default=str),
            )
            rows.append(row)
            if not explicit_identity:
                legacy_rows.append((normalized_symbol, timeframe.lower(), bar_start, bar_end, *numeric, provider_name, data_as_of_text, closed, received_at))
        if not rows:
            return 0
        with self._connect() as db:
            cursor = db.executemany(
                """INSERT INTO market_bar_versions(
                    instrument_key, symbol, venue, market_type, native_symbol,
                    settle_currency, price_type, timeframe, bar_start, bar_end,
                    revision_id, open, high, low, close, volume, volume_unit,
                    event_time, first_received_at, available_at, fetched_at,
                    source, raw_hash, quality_status, is_closed, payload_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?
                )
                ON CONFLICT(instrument_key, timeframe, bar_start, revision_id) DO UPDATE SET
                    available_at=excluded.available_at, fetched_at=excluded.fetched_at,
                    first_received_at=excluded.first_received_at, quality_status=excluded.quality_status,
                    is_closed=excluded.is_closed, payload_json=excluded.payload_json""",
                rows,
            )
            # Keep the old table only for unqualified compatibility callers.
            # Qualified writes never overwrite another venue's legacy row.
            if legacy_rows:
                db.executemany(
                    """INSERT OR REPLACE INTO market_bars(
                        symbol, timeframe, bar_start, bar_end, open, high, low, close, volume,
                        provider, data_as_of, is_closed, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    legacy_rows,
                )
        return len(rows)

    def range_bars(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 500,
        cursor: str | None = None,
        instrument_key: str | None = None,
        venue: str | None = None,
        market_type: str | None = None,
        price_type: str | None = None,
    ) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 2_000))
        start_text = self._utc_timestamp(start) if isinstance(start, datetime) else (str(start) if start else None)
        end_text = self._utc_timestamp(end) if isinstance(end, datetime) else (str(end) if end else None)
        for value, name in ((start_text, "start"), (end_text, "end"), (cursor, "cursor")):
            if value is not None and _parse_utc_timestamp(value) is None:
                raise ValueError(f"{name} must be an ISO timestamp")
        normalized_symbol = symbol.strip().upper()
        conditions = ["symbol = ?", "timeframe = ?"]
        params: list[object] = [normalized_symbol, timeframe.lower()]
        if instrument_key:
            conditions.append("instrument_key = ?")
            params.append(instrument_key)
        if venue:
            conditions.append("venue = ?")
            params.append(str(venue).strip().lower())
        if market_type:
            conditions.append("market_type = ?")
            params.append(str(market_type).strip().lower())
        if price_type:
            conditions.append("price_type = ?")
            params.append(str(price_type).strip().lower())
        if start_text:
            conditions.append("bar_start >= ?")
            params.append(start_text)
        if end_text:
            conditions.append("bar_start < ?")
            params.append(end_text)
        if cursor:
            conditions.append("bar_start > ?")
            params.append(cursor)
        params.append(bounded)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT instrument_key, symbol, venue, market_type, native_symbol,
                           settle_currency, price_type, timeframe, bar_start, bar_end,
                           revision_id, open, high, low, close, volume, volume_unit,
                           event_time, first_received_at, available_at, fetched_at,
                           source, raw_hash, quality_status, is_closed, payload_json
                    FROM market_bar_versions WHERE {' AND '.join(conditions)}
                    ORDER BY bar_start ASC, available_at ASC, revision_id ASC LIMIT ?""",
                tuple(params),
            ).fetchall()
            old_rows = db.execute(
                """SELECT symbol, timeframe, bar_start, bar_end, open, high, low, close, volume,
                           provider, data_as_of, is_closed, received_at
                    FROM market_bars WHERE symbol=? AND timeframe=? ORDER BY bar_start ASC""",
                (normalized_symbol, timeframe.lower()),
            ).fetchall()
        result = [dict(row) for row in rows]
        present = {(row.get("bar_start"), row.get("source")) for row in result}
        # Direct legacy inserts made by old integrations remain readable, but
        # never replace a qualified versioned row.
        for row in old_rows:
            candidate = dict(row)
            if (candidate["bar_start"], candidate["provider"]) in present:
                continue
            candidate.update(
                {
                    "instrument_key": canonical_instrument_key("legacy", "unknown", candidate["symbol"], "UNKNOWN", "last"),
                    "venue": "legacy",
                    "market_type": "unknown",
                    "native_symbol": candidate["symbol"],
                    "settle_currency": "UNKNOWN",
                    "price_type": "last",
                    "revision_id": hashlib.sha256(json.dumps(candidate, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32],
                    "volume_unit": "UNKNOWN",
                    "event_time": candidate["bar_start"],
                    "first_received_at": candidate["received_at"],
                    "available_at": candidate["received_at"] or candidate["data_as_of"],
                    "fetched_at": candidate["received_at"],
                    "source": candidate["provider"],
                    "raw_hash": None,
                    "quality_status": "LEGACY_UNVERIFIED",
                    "payload_json": "{}",
                }
            )
            if (instrument_key and candidate["instrument_key"] != instrument_key) or venue and candidate["venue"] != str(venue).lower() or market_type and candidate["market_type"] != str(market_type).lower():
                continue
            if start_text and candidate["bar_start"] < start_text or end_text and candidate["bar_start"] >= end_text or cursor and candidate["bar_start"] <= cursor:
                continue
            result.append(candidate)
        result.sort(key=lambda item: (str(item.get("bar_start")), str(item.get("available_at")), str(item.get("revision_id"))))
        return result[:bounded]

    def latest_bars(self, symbol: str, timeframe: str, *, limit: int = 500, **filters: object) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 2_000))
        # Apply identity filters before limiting, then return chronological
        # order as required by strategy consumers.
        rows = self.range_bars(symbol, timeframe, limit=2_000, **filters)
        current: dict[tuple[object, object], dict[str, object]] = {}
        for row in rows:
            key = (row.get("instrument_key"), row.get("bar_start"))
            previous = current.get(key)
            if previous is None or (
                (str(row.get("available_at") or ""), str(row.get("revision_id") or ""))
                >= (str(previous.get("available_at") or ""), str(previous.get("revision_id") or ""))
            ):
                current[key] = row
        return sorted(current.values(), key=lambda item: str(item.get("bar_start") or ""))[-bounded:]

    def list_market_bars(self, symbol: str, timeframe: str, *, limit: int = 500, **filters: object) -> list[dict[str, object]]:
        return self.latest_bars(symbol, timeframe, limit=limit, **filters)

    @staticmethod
    def _gate_json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)

    @staticmethod
    def _gate_time(value: object, fallback: str) -> str:
        if isinstance(value, datetime):
            return SQLiteStore._utc_timestamp(value)
        parsed = _parse_utc_timestamp(value)
        return SQLiteStore._utc_timestamp(parsed) if parsed is not None else str(value or fallback)

    @staticmethod
    def _gate_event_hash(value: object) -> str:
        return hashlib.sha256(SQLiteStore._gate_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _gate_bar_payload(bar: object) -> dict[str, object]:
        if isinstance(bar, dict):
            return dict(bar)
        return {
            name: getattr(bar, name, None)
            for name in (
                "timestamp", "bar_end", "open", "high", "low", "close", "volume",
                "event_time", "available_at", "first_received_at", "fetched_at",
                "revision_id", "volume_unit", "source", "is_closed",
            )
        }

    def save_gate_bootstrap(self, bootstrap: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
        """Persist one native Gate bootstrap and all its derivative evidence.

        The method is intentionally additive and idempotent.  Replaying the
        same native rows never duplicates bars, trades or order-book facts;
        changed rows receive a new revision/raw hash.
        """

        if not isinstance(bootstrap, dict):
            raise ValueError("Gate bootstrap must be an object")
        symbol = str(bootstrap.get("symbol") or "").strip().upper()
        native = str(bootstrap.get("native_symbol") or "").strip().upper()
        if not symbol or not native:
            raise ValueError("Gate bootstrap symbol and native_symbol are required")
        provider = str(bootstrap.get("provider") or "gate").strip().lower()
        environment = str(bootstrap.get("environment") or "LIVE_PUBLIC").strip().upper()
        quote = bootstrap.get("quote") if isinstance(bootstrap.get("quote"), dict) else {}
        completed = self._utc_timestamp(now)
        stable = {
            "provider": provider,
            "environment": environment,
            "symbol": symbol,
            "native_symbol": native,
            "quote": quote,
            "bars": {
                str(timeframe): [self._gate_bar_payload(item) for item in rows]
                for timeframe, rows in (bootstrap.get("bars") or {}).items()
                if isinstance(rows, list)
            },
            "mark_bars": [self._gate_bar_payload(item) for item in (bootstrap.get("mark_bars") or [])],
            "index_bars": [self._gate_bar_payload(item) for item in (bootstrap.get("index_bars") or [])],
        }
        source_hash = self._gate_event_hash(stable)
        bootstrap_id = f"gate_bootstrap_{source_hash[:24]}"
        quality = bootstrap.get("quality") if isinstance(bootstrap.get("quality"), dict) else {}
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO gate_bootstrap_runs(
                    bootstrap_id, provider, environment, symbol, native_symbol,
                    status, started_at, completed_at, source_hash,
                    closed_15m_bars, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    bootstrap_id, provider, environment, symbol, native,
                    str(quality.get("status") or "READY"), completed, completed,
                    source_hash, int(quality.get("closed_15m_bars") or 0),
                    self._gate_json(bootstrap),
                ),
            )
        quote_as_of = quote.get("timestamp") or completed
        bars_written = 0
        for timeframe, rows in (bootstrap.get("bars") or {}).items():
            if not isinstance(rows, list):
                continue
            bars_written += self.upsert_market_bars(
                symbol,
                str(timeframe),
                rows,
                provider="gate",
                data_as_of=quote_as_of,
                now=now,
                venue="gate",
                market_type="perpetual",
                native_symbol=native,
                settle_currency="USDT",
                price_type="last",
                volume_unit="contracts",
            )
        for price_type in ("mark", "index"):
            rows = bootstrap.get(f"{price_type}_bars") or []
            if isinstance(rows, list):
                bars_written += self.upsert_market_bars(
                    symbol,
                    "15m",
                    rows,
                    provider="gate",
                    data_as_of=quote_as_of,
                    now=now,
                    venue="gate",
                    market_type="perpetual",
                    native_symbol=native,
                    settle_currency="USDT",
                    price_type=price_type,
                    volume_unit="contracts",
                )
                self._save_gate_derivative_bars(symbol, native, price_type, rows, provider, environment, now=now)
        funding = bootstrap.get("funding")
        if isinstance(funding, dict):
            self.save_gate_funding(symbol, native, funding, provider=provider, environment=environment, now=now)
            self.save_gate_open_interest(symbol, native, funding.get("open_interest_history") or [], provider=provider, environment=environment, now=now)
        book = bootstrap.get("order_book")
        if isinstance(book, dict):
            self.save_gate_order_book_snapshot(symbol, native, book, provider=provider, environment=environment, now=now)
        for trade in bootstrap.get("trades") or []:
            if isinstance(trade, dict):
                self.save_gate_trade(symbol, native, trade, provider=provider, environment=environment, now=now)
        for event in bootstrap.get("liquidations") or []:
            if isinstance(event, dict):
                self.save_gate_liquidation(symbol, native, event, provider=provider, environment=environment, now=now)
        return {"bootstrap_id": bootstrap_id, "source_hash": source_hash, "status": str(quality.get("status") or "READY"), "bars_written": bars_written, "provider": provider, "environment": environment, "symbol": symbol, "native_symbol": native, "synthetic": False}

    def _save_gate_derivative_bars(self, symbol: str, native: str, price_type: str, bars: list[object], provider: str, environment: str, *, now: datetime | None = None) -> int:
        received = self._utc_timestamp(now)
        rows: list[tuple[object, ...]] = []
        for bar in bars:
            item = self._gate_bar_payload(bar)
            start = self._gate_time(item.get("timestamp") or item.get("bar_start"), received)
            end = self._gate_time(item.get("bar_end"), start)
            event = self._gate_time(item.get("event_time") or item.get("event_at"), start)
            available = self._gate_time(item.get("available_at"), received)
            revision = str(item.get("revision_id") or self._gate_event_hash(item)[:32])
            raw_hash = self._gate_event_hash({"symbol": symbol, "native_symbol": native, "price_type": price_type, "timeframe": "15m", "bar": item})
            row_id = f"gate_bar_{raw_hash[:24]}"
            rows.append((row_id, provider, environment, symbol, native, "15m", price_type, start, end, event, available, received, revision, float(item["open"]), float(item["high"]), float(item["low"]), float(item["close"]), float(item["volume"]), int(bool(item.get("is_closed"))), raw_hash, "VALID", self._gate_json(item)))
        if not rows:
            return 0
        with self._connect() as db:
            cursor = db.executemany(
                """INSERT OR IGNORE INTO gate_derivative_bars(
                    row_id, provider, environment, symbol, native_symbol,
                    timeframe, price_type, bar_start, bar_end, event_time,
                    available_at, received_at, revision_id, open, high, low,
                    close, volume, is_closed, raw_hash, quality_status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return max(0, int(cursor.rowcount))

    def save_gate_order_book_snapshot(self, symbol: str, native_symbol: str, snapshot: dict[str, object], *, provider: str = "gate", environment: str = "LIVE_PUBLIC", now: datetime | None = None) -> dict[str, object]:
        received = self._utc_timestamp(now)
        payload = dict(snapshot)
        raw_hash = self._gate_event_hash(payload)
        snapshot_id = f"gate_book_snapshot_{raw_hash[:24]}"
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO gate_order_book_snapshots(
                    snapshot_id, provider, environment, symbol, native_symbol,
                    sequence, event_at, received_at, source, raw_hash,
                    quality_status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, provider, environment, symbol.upper(), native_symbol.upper(), str(payload.get("sequence")) if payload.get("sequence") is not None else None, str(payload.get("event_at")) if payload.get("event_at") is not None else None, received, str(payload.get("source") or "gate_native_rest_order_book"), raw_hash, str(payload.get("quality_status") or "VALID"), self._gate_json(payload)),
            )
        return {"snapshot_id": snapshot_id, "raw_hash": raw_hash, "inserted": cursor.rowcount == 1, "sequence": payload.get("sequence")}

    def save_gate_order_book_delta(self, symbol: str, native_symbol: str, delta: dict[str, object], *, provider: str = "gate", environment: str = "LIVE_PUBLIC", now: datetime | None = None) -> dict[str, object]:
        received = self._utc_timestamp(now)
        payload = dict(delta)
        raw_hash = self._gate_event_hash(payload)
        delta_id = f"gate_book_delta_{raw_hash[:24]}"
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO gate_order_book_deltas(
                    delta_id, provider, environment, symbol, native_symbol,
                    first_sequence, last_sequence, event_at, received_at,
                    sequence_status, raw_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (delta_id, provider, environment, symbol.upper(), native_symbol.upper(), str(payload.get("first_sequence") or payload.get("U")) if payload.get("first_sequence", payload.get("U")) is not None else None, str(payload.get("last_sequence") or payload.get("u")) if payload.get("last_sequence", payload.get("u")) is not None else None, str(payload.get("event_at")) if payload.get("event_at") is not None else None, received, str(payload.get("sequence_status") or "UNKNOWN"), raw_hash, self._gate_json(payload)),
            )
        return {"delta_id": delta_id, "raw_hash": raw_hash, "inserted": cursor.rowcount == 1}

    def save_gate_trade(self, symbol: str, native_symbol: str, trade: dict[str, object], *, provider: str = "gate", environment: str = "LIVE_PUBLIC", now: datetime | None = None) -> dict[str, object]:
        received = self._utc_timestamp(now)
        payload = dict(trade)
        trade_id = str(payload.get("trade_id") or payload.get("id") or "").strip()
        if not trade_id:
            raise ValueError("Gate trade id is required")
        raw_hash = self._gate_event_hash(payload)
        row_id = f"gate_trade_{provider}_{environment}_{native_symbol}_{trade_id}"
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO gate_trades(
                    trade_row_id, provider, environment, symbol, native_symbol,
                    trade_id, event_at, received_at, side, price, size,
                    raw_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row_id, provider, environment, symbol.upper(), native_symbol.upper(), trade_id, str(payload.get("event_at")) if payload.get("event_at") is not None else None, received, str(payload.get("side") or "UNKNOWN"), float(payload["price"]) if payload.get("price") is not None else None, float(payload["size"]) if payload.get("size") is not None else None, raw_hash, self._gate_json(payload)),
            )
        return {"trade_row_id": row_id, "raw_hash": raw_hash, "inserted": cursor.rowcount == 1}

    def save_gate_liquidation(self, symbol: str, native_symbol: str, event: dict[str, object], *, provider: str = "gate", environment: str = "LIVE_PUBLIC", now: datetime | None = None) -> dict[str, object]:
        received = self._utc_timestamp(now)
        payload = dict(event)
        raw_hash = self._gate_event_hash(payload)
        event_id = f"gate_liquidation_{raw_hash[:24]}"
        with self._connect() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO gate_liquidations(event_id, provider, environment, symbol, native_symbol, event_at, received_at, raw_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (event_id, provider, environment, symbol.upper(), native_symbol.upper(), str(payload.get("event_at")) if payload.get("event_at") is not None else None, received, raw_hash, self._gate_json(payload)))
        return {"event_id": event_id, "raw_hash": raw_hash, "inserted": cursor.rowcount == 1}

    def save_gate_funding(self, symbol: str, native_symbol: str, funding: dict[str, object], *, provider: str = "gate", environment: str = "LIVE_PUBLIC", now: datetime | None = None) -> int:
        received = self._utc_timestamp(now)
        history = funding.get("history") if isinstance(funding.get("history"), list) else []
        rows = []
        for point in history:
            if not isinstance(point, dict) or point.get("timestamp") is None or point.get("fundingRate") is None:
                continue
            event_at = self._gate_time(point.get("timestamp"), received)
            raw_hash = self._gate_event_hash(point)
            rows.append((f"gate_funding_{raw_hash[:24]}", provider, environment, symbol.upper(), native_symbol.upper(), event_at, float(point["fundingRate"]), received, raw_hash, self._gate_json(point)))
        if rows:
            with self._connect() as db:
                cursor = db.executemany("""INSERT OR IGNORE INTO gate_funding_history(row_id, provider, environment, symbol, native_symbol, event_at, funding_rate, received_at, raw_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
                return max(0, int(cursor.rowcount))
        return 0

    def save_gate_open_interest(self, symbol: str, native_symbol: str, points: list[object], *, provider: str = "gate", environment: str = "LIVE_PUBLIC", now: datetime | None = None) -> int:
        received = self._utc_timestamp(now)
        rows = []
        for point in points:
            if not isinstance(point, dict) or point.get("timestamp") is None or point.get("openInterestAmount") is None:
                continue
            event_at = self._gate_time(point.get("timestamp"), received)
            raw_hash = self._gate_event_hash(point)
            rows.append((f"gate_oi_{raw_hash[:24]}", provider, environment, symbol.upper(), native_symbol.upper(), event_at, float(point["openInterestAmount"]), received, raw_hash, self._gate_json(point)))
        if rows:
            with self._connect() as db:
                cursor = db.executemany("""INSERT OR IGNORE INTO gate_open_interest(row_id, provider, environment, symbol, native_symbol, event_at, open_interest, received_at, raw_hash, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
                return max(0, int(cursor.rowcount))
        return 0

    def save_indicator_snapshot(self, snapshot: dict[str, object]) -> dict[str, object]:
        payload = dict(snapshot)
        symbol = str(payload.get("symbol") or "").upper()
        timeframe = str(payload.get("timeframe") or "15m").lower()
        bar_end = str(payload.get("bar_end") or "")
        indicator_version = str(payload.get("indicator_version") or "indicators_v2")
        data_source_hash = str(payload.get("data_source_hash") or self._gate_event_hash(payload))
        snapshot_key = {
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_end": bar_end,
            "indicator_version": indicator_version,
            "data_source_hash": data_source_hash,
        }
        snapshot_id = str(payload.get("snapshot_id") or f"indicator_{self._gate_event_hash(snapshot_key)[:24]}")
        generated = str(payload.get("generated_at") or self._utc_timestamp())
        with self._connect() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO indicator_snapshots(snapshot_id, provider, environment, symbol, timeframe, bar_end, generated_at, data_source_hash, indicator_version, quality_status, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (snapshot_id, str(payload.get("provider") or "gate"), str(payload.get("environment") or "LIVE_PUBLIC"), symbol, timeframe, bar_end, generated, data_source_hash, indicator_version, str(payload.get("quality_status") or "VALID"), self._gate_json(payload)))
        return {"snapshot_id": snapshot_id, "data_source_hash": data_source_hash, "inserted": cursor.rowcount == 1}

    def get_indicator_snapshot(self, symbol: str, timeframe: str = "15m") -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM indicator_snapshots WHERE symbol=? AND timeframe=? ORDER BY bar_end DESC, generated_at DESC LIMIT 1", (str(symbol).upper(), str(timeframe).lower())).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["payload"] = {}
        return item

    def get_gate_bootstrap_status(self, symbol: str, *, environment: str = "LIVE_PUBLIC") -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM gate_bootstrap_runs WHERE symbol=? AND environment=? ORDER BY completed_at DESC LIMIT 1", (str(symbol).upper(), str(environment).upper())).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["payload"] = {}
        return item

    def record_runtime_diagnostic(self, *, state: str, code: str, account_id: str | None = None, session_id: str | None = None, cycle_id: str | None = None, payload: dict[str, object] | None = None, occurred_at: datetime | None = None) -> dict[str, object]:
        diagnostic_id = f"diag_{self._gate_event_hash({account_id, session_id, cycle_id, state, code, occurred_at or ''})[:24]}"
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO ai_runtime_diagnostics(diagnostic_id, account_id, session_id, cycle_id, state, code, occurred_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (diagnostic_id, account_id, session_id, cycle_id, str(state), str(code), self._utc_timestamp(occurred_at), self._gate_json(payload or {})))
        return {"diagnostic_id": diagnostic_id, "state": state, "code": code}

    def save_realtime_state(self, state: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
        symbol = str(state.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("realtime state symbol is required")
        timestamp = self._utc_timestamp(now)
        payload = dict(state)
        payload["symbol"] = symbol
        provider = str(state.get("provider") or "unknown").strip()
        freshness_status = str(state.get("freshness_status") or "unavailable").strip().lower()
        if not provider or freshness_status not in {"fresh", "stale", "degraded", "unavailable"}:
            raise ValueError("realtime state provider or freshness_status is invalid")

        def optional_number(value: object, *, positive: bool = False, non_negative: bool = False) -> float | None:
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError("realtime state numeric fields must be numbers")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("realtime state numeric fields must be numbers") from exc
            if not math.isfinite(number) or (positive and number <= 0) or (non_negative and number < 0):
                raise ValueError("realtime state numeric fields must be finite and bounded")
            return number

        price = optional_number(state.get("price"), positive=True)
        change_pct = optional_number(state.get("change_pct"))
        volume = optional_number(state.get("volume"), non_negative=True)
        try:
            stale_after_seconds = int(state.get("stale_after_seconds") or 120)
            reconnect_count = int(state.get("reconnect_count") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("realtime state integer fields are invalid") from exc
        if stale_after_seconds < 1 or reconnect_count < 0:
            raise ValueError("realtime state integer fields are out of bounds")
        for field in ("last_trade_at", "data_as_of"):
            value = state.get(field)
            if value is not None and _parse_utc_timestamp(str(value)) is None:
                raise ValueError(f"realtime state {field} must be an ISO timestamp")
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO market_realtime_state(
                    symbol, provider, price, change_pct, volume, last_trade_at, data_as_of,
                    freshness_status, stale_after_seconds, reconnect_count, last_error,
                    updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    symbol,
                    provider,
                    price,
                    change_pct,
                    volume,
                    state.get("last_trade_at"),
                    state.get("data_as_of"),
                    freshness_status,
                    stale_after_seconds,
                    reconnect_count,
                    state.get("last_error"),
                    timestamp,
                    json.dumps(payload, sort_keys=True, ensure_ascii=True),
                ),
            )
        return payload

    def get_realtime_state(self, symbol: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM market_realtime_state WHERE symbol = ?", (symbol.strip().upper(),)).fetchone()
        if row is None:
            return None
        payload = dict(row)
        try:
            payload.update(json.loads(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return payload

    def list_realtime_states(self) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM market_realtime_state ORDER BY symbol ASC").fetchall()
        return [self.get_realtime_state(str(row["symbol"])) or dict(row) for row in rows]

    def claim_bar_close(self, symbol: str, timeframe: str, bar_start: str, bar_end: str, *, closed_at: datetime | str, processed_at: datetime | str | None = None) -> bool:
        closed_text = self._utc_timestamp(closed_at) if isinstance(closed_at, datetime) else str(closed_at)
        processed_text = self._utc_timestamp(processed_at) if isinstance(processed_at, datetime) else str(processed_at or closed_text)
        with self._connect() as db:
            result = db.execute(
                """INSERT OR IGNORE INTO bar_close_ledger(
                    symbol, timeframe, bar_start, bar_end, closed_at, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (symbol.strip().upper(), timeframe.lower(), bar_start, bar_end, closed_text, processed_text),
            )
        return result.rowcount == 1

    @staticmethod
    def _trigger_event_from_row(row: sqlite3.Row) -> dict[str, object]:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return {
            "trigger_event_id": row["trigger_event_id"],
            "instrument_id": row["instrument_id"],
            "timeframe": row["timeframe"],
            "bar_start": row["bar_start"],
            "bar_end": row["bar_end"],
            "trigger_type": row["trigger_type"],
            "trigger_score": float(row["trigger_score"]),
            "fingerprint": row["fingerprint"],
            "policy_version": row["policy_version"],
            "status": row["status"],
            "analysis_status": row["analysis_status"],
            "payload": payload,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def insert_trigger_event(self, event: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
        event_id = str(event.get("trigger_event_id") or "").strip()
        fingerprint = str(event.get("fingerprint") or "").strip()
        if not event_id or not fingerprint:
            raise ValueError("trigger event id and fingerprint are required")
        timestamp = self._utc_timestamp(now)
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("trigger event payload must be an object")
        with self._connect() as db:
            insert_result = db.execute(
                """INSERT OR IGNORE INTO trigger_events(
                    trigger_event_id, instrument_id, timeframe, bar_start, bar_end,
                    trigger_type, trigger_score, fingerprint, policy_version, status,
                    analysis_status, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    str(event.get("instrument_id") or "").strip().upper(),
                    str(event.get("timeframe") or "15m").lower(),
                    str(event.get("bar_start") or ""),
                    str(event.get("bar_end") or ""),
                    str(event.get("trigger_type") or "unknown"),
                    float(event.get("trigger_score") or 0.0),
                    fingerprint,
                    str(event.get("policy_version") or "trigger_policy_v2"),
                    str(event.get("status") or "PENDING"),
                    str(event.get("analysis_status") or "NOT_REQUESTED"),
                    json.dumps(payload, sort_keys=True, ensure_ascii=True),
                    timestamp,
                    timestamp,
                ),
            )
            inserted = insert_result.rowcount == 1
            row = db.execute("SELECT * FROM trigger_events WHERE fingerprint = ?", (fingerprint,)).fetchone()
        assert row is not None
        result = self._trigger_event_from_row(row)
        result["created"] = inserted
        return result

    def update_trigger_event(self, trigger_event_id: str, *, status: str | None = None, analysis_status: str | None = None, now: datetime | None = None) -> dict[str, object] | None:
        fields: list[str] = []
        params: list[object] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if analysis_status is not None:
            fields.append("analysis_status = ?")
            params.append(analysis_status)
        if fields:
            fields.append("updated_at = ?")
            params.append(self._utc_timestamp(now))
            params.append(trigger_event_id)
            with self._connect() as db:
                db.execute(f"UPDATE trigger_events SET {', '.join(fields)} WHERE trigger_event_id = ?", tuple(params))
        with self._connect() as db:
            row = db.execute("SELECT * FROM trigger_events WHERE trigger_event_id = ?", (trigger_event_id,)).fetchone()
        return self._trigger_event_from_row(row) if row is not None else None

    def list_trigger_events(self, *, symbol: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 500))
        query = "SELECT * FROM trigger_events"
        params: list[object] = []
        if symbol:
            query += " WHERE instrument_id = ?"
            params.append(symbol.strip().upper())
        query += " ORDER BY bar_start DESC, trigger_event_id ASC LIMIT ?"
        params.append(bounded)
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [self._trigger_event_from_row(row) for row in rows]

    def save_opportunity_analysis(self, analysis: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
        analysis_id = str(analysis.get("analysis_id") or "").strip()
        trigger_event_id = str(analysis.get("trigger_event_id") or "").strip()
        if not analysis_id or not trigger_event_id:
            raise ValueError("opportunity analysis ids are required")
        timestamp = self._utc_timestamp(now)
        payload = analysis.get("payload", analysis)
        if not isinstance(payload, dict):
            raise ValueError("opportunity analysis payload must be an object")
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO opportunity_analyses(
                    analysis_id, trigger_event_id, instrument_id, timeframe, bias, confidence,
                    model_id, prompt_version, data_as_of, payload_json, raw_model_response,
                    validator_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    analysis_id,
                    trigger_event_id,
                    str(analysis.get("instrument_id") or "").strip().upper(),
                    str(analysis.get("timeframe") or "15m").lower(),
                    str(analysis.get("bias") or "WAIT"),
                    float(analysis.get("confidence") or 0.0),
                    str(analysis.get("model_id") or ""),
                    str(analysis.get("prompt_version") or ""),
                    str(analysis.get("data_as_of") or timestamp),
                    json.dumps(payload, sort_keys=True, ensure_ascii=True),
                    analysis.get("raw_model_response"),
                    str(analysis.get("validator_status") or "VALID"),
                    timestamp,
                ),
            )
        return analysis

    def list_opportunity_analyses(self, *, symbol: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 500))
        query = "SELECT * FROM opportunity_analyses"
        params: list[object] = []
        if symbol:
            query += " WHERE instrument_id = ?"
            params.append(symbol.strip().upper())
        query += " ORDER BY created_at DESC, analysis_id DESC LIMIT ?"
        params.append(bounded)
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            results.append({**payload, "analysis_id": row["analysis_id"], "trigger_event_id": row["trigger_event_id"], "validator_status": row["validator_status"], "created_at": row["created_at"], "raw_model_response": row["raw_model_response"]})
        return results

    def save_chart_annotations(self, annotations: Iterable[dict[str, object]], *, now: datetime | None = None) -> int:
        timestamp = self._utc_timestamp(now)
        rows: list[tuple[object, ...]] = []
        for annotation in annotations:
            annotation_id = str(annotation.get("annotation_id") or "").strip()
            if not annotation_id:
                raise ValueError("chart annotation_id is required")
            payload = annotation.get("payload", annotation)
            if not isinstance(payload, dict):
                raise ValueError("chart annotation payload must be an object")
            rows.append((
                annotation_id,
                str(annotation.get("symbol") or "").strip().upper(),
                str(annotation.get("timeframe") or "15m").lower(),
                str(annotation.get("annotation_type") or "marker"),
                annotation.get("bar_start"),
                annotation.get("price"),
                annotation.get("label"),
                str(annotation.get("source") or "python"),
                json.dumps(payload, sort_keys=True, ensure_ascii=True),
                timestamp,
            ))
        if not rows:
            return 0
        with self._connect() as db:
            db.executemany(
                """INSERT OR REPLACE INTO chart_annotations(
                    annotation_id, symbol, timeframe, annotation_type, bar_start, price,
                    label, source, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def list_chart_annotations(self, symbol: str, timeframe: str, *, limit: int = 100) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM chart_annotations WHERE symbol = ? AND timeframe = ?
                   ORDER BY COALESCE(bar_start, created_at) ASC, annotation_id ASC LIMIT ?""",
                (symbol.strip().upper(), timeframe.lower(), bounded),
            ).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            results.append({**dict(row), "payload": payload})
        return results

    def save_localized_news_artifact(self, artifact: dict[str, object]) -> dict[str, object]:
        required = ("news_id", "locale", "source_language", "original_title", "source_hash", "model_id", "prompt_version", "translated_at", "status")
        if any(not str(artifact.get(key) or "").strip() for key in required):
            raise ValueError("localized news artifact is incomplete")
        evidence = artifact.get("evidence", {})
        if not isinstance(evidence, dict):
            raise ValueError("localized news evidence must be an object")
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO localized_news_artifacts(
                    news_id, locale, source_language, original_title, original_summary,
                    translated_title_zh, translated_summary_zh, evidence_json, source_hash,
                    model_id, prompt_version, translated_at, numeric_guard_passed, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(artifact["news_id"]),
                    str(artifact["locale"]),
                    str(artifact["source_language"]),
                    str(artifact["original_title"]),
                    artifact.get("original_summary"),
                    artifact.get("translated_title_zh"),
                    artifact.get("translated_summary_zh"),
                    json.dumps(evidence, sort_keys=True, ensure_ascii=True),
                    str(artifact["source_hash"]),
                    str(artifact["model_id"]),
                    str(artifact["prompt_version"]),
                    str(artifact["translated_at"]),
                    int(bool(artifact.get("numeric_guard_passed", False))),
                    str(artifact["status"]),
                ),
            )
        return artifact

    def list_localized_news_artifacts(self, *, news_id: str | None = None, locale: str = "zh-CN", limit: int = 100) -> list[dict[str, object]]:
        bounded = max(1, min(int(limit), 500))
        query = "SELECT * FROM localized_news_artifacts WHERE locale = ?"
        params: list[object] = [locale]
        if news_id:
            query += " AND news_id = ?"
            params.append(news_id)
        query += " ORDER BY translated_at DESC, news_id ASC LIMIT ?"
        params.append(bounded)
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        results: list[dict[str, object]] = []
        for row in rows:
            result = dict(row)
            try:
                result["evidence"] = json.loads(row["evidence_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                result["evidence"] = {}
            result["numeric_guard_passed"] = bool(row["numeric_guard_passed"])
            results.append(result)
        return results

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            return {
                table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "instruments",
                    "market_snapshots",
                    "predictions",
                    "paper_trades",
                    "outcomes",
                    "news_events",
                "provider_snapshots",
                "model_runs",
                "replay_runs",
                "replay_samples",
                "performance_snapshots",
                "calibration_results",
                "calibration_buckets",
                "watchlist_entries",
                "app_settings",
                "scheduler_runs",
                "scheduler_items",
                "scheduler_cache_entries",
                "alerts",
                "benchmark_metadata",
                "phase6_events",
                "phase6_event_clusters",
                "market_memory_features",
                "daily_briefs",
                "monitoring_policies",
                "market_bars",
                "market_realtime_state",
                "bar_close_ledger",
                "trigger_events",
                "opportunity_analyses",
                "chart_annotations",
                "localized_news_artifacts",
            )
        }

    def schema_version(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def backup_to(self, destination: str | Path) -> None:
        """Create a consistent SQLite backup before applying or testing migrations."""

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source:
            target = sqlite3.connect(str(destination_path))
            try:
                source.backup(target)
                target.commit()
            finally:
                target.close()

    def load_prediction_payload(self, prediction_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT payload_json FROM predictions WHERE prediction_id = ?", (prediction_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_prediction_payloads(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        action: str | None = None,
        source_type: str | None = None,
        replay_run_id: str | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        outcome_status: str | None = None,
        has_outcome: bool | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("p.symbol = ?")
            params.append(symbol.upper())
        if timeframe:
            clauses.append("json_extract(p.payload_json, '$.analysis_timeframe') = ?")
            params.append(timeframe)
        if action:
            clauses.append("p.action = ?")
            params.append(action)
        if source_type:
            clauses.append("COALESCE(p.source_type, 'live') = ?")
            params.append(source_type)
        if replay_run_id:
            clauses.append("p.replay_run_id = ?")
            params.append(replay_run_id)
        if model_id:
            clauses.append("p.model_id = ?")
            params.append(model_id)
        if prompt_version:
            clauses.append("p.prompt_version = ?")
            params.append(prompt_version)
        if outcome_status:
            clauses.append("o.status = ?")
            params.append(outcome_status)
        if has_outcome is True:
            clauses.append("o.prediction_id IS NOT NULL")
        elif has_outcome is False:
            clauses.append("o.prediction_id IS NULL")
        query = "SELECT p.payload_json FROM predictions p LEFT JOIN outcomes o ON o.prediction_id = p.prediction_id"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY p.generated_at DESC, p.prediction_id DESC LIMIT ? OFFSET ?"
        params.extend((max(1, min(int(limit), 500)), max(0, min(int(offset), 100_000))))
        with self._connect() as db:
            rows = db.execute(query, tuple(params)).fetchall()
        return [json.loads(row[0]) for row in rows]
