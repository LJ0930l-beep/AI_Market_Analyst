"""Small durable SQLite store for Prediction/PaperTrade/Outcome separation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..instruments import Instrument, instrument_for, instrument_from_payload
from ..news_engine import NewsFetchResult
from ..outcomes.engine import Outcome
from ..providers.news import NewsEvent
from ..providers.runtime import ProviderSnapshot
from ..signals.schema import SignalProposal


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


class SQLiteStore:
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
        limit: int = 100,
    ) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM phase6_events ORDER BY event_at DESC, event_id ASC LIMIT ?",
                (max(bounded_limit * 5, bounded_limit),),
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
