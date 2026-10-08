from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.replay.pa_decision_quality_v36.context import build_context
from core.replay.pa_decision_quality_v36.experiments import a0_cache_identity
from core.replay.pa_decision_quality_v36.review import summarize
from core.replay.pa_decision_quality_v36.runner import run_study
from core.replay.pa_decision_quality_v36.validation import schema_version_for_experiment
from core.replay.pa_decision_quality_v37.evidence_builder import (
    AVAILABILITY_BASIS,
    EvidenceBuildError,
    aggregate_timeframe_bars,
    build_reconstructed_point,
    open_sqlite_readonly,
)
from core.replay.pa_decision_quality_v37.recovery import (
    classify_a0_record,
    economic_coverage_from_v25,
)
from core.replay.pa_decision_quality_v37.schema import (
    V35_SCHEMA_COMMIT,
    load_frozen_a0_schema,
)


def _minute_rows(start: datetime, count: int) -> list[dict[str, object]]:
    start_ms = int(start.timestamp() * 1000)
    rows = []
    for index in range(count):
        opening = 100 + index / 1000
        rows.append({
            "symbol": "BTCUSDT",
            "open_time_ms": start_ms + index * 60_000,
            "open": opening,
            "high": opening + 0.02,
            "low": opening - 0.02,
            "close": opening + 0.005,
            "volume": 1.0,
        })
    return rows


def _v36_bars(decision_time: datetime) -> dict[str, list[dict[str, object]]]:
    bars: dict[str, list[dict[str, object]]] = {}
    for timeframe, minutes in (("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240)):
        duration = timedelta(minutes=minutes)
        frame = []
        for index in range(40):
            end = decision_time - duration * (40 - index)
            opening = 100 + index / 100
            high = opening + 1
            low = opening - 1
            if index == 20:
                high = 125
            frame.append({
                "timeframe": timeframe,
                "bar_start": (end - duration).isoformat().replace("+00:00", "Z"),
                "bar_end": end.isoformat().replace("+00:00", "Z"),
                "available_at": (end + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
                "source": "test-archive",
                "quality_status": "FROZEN_RESEARCH",
                "is_closed": True,
                "open": opening,
                "high": high,
                "low": low,
                "close": opening + 0.1,
                "volume": 1.0,
            })
        bars[timeframe] = frame
    return bars


def test_v35_a0_schema_is_hash_pinned_and_independent_of_runtime_schema(monkeypatch):
    schema, version, record = load_frozen_a0_schema()
    assert record["source"]["commit"] == V35_SCHEMA_COMMIT
    assert version == schema_version_for_experiment("A0")
    assert version.endswith(record["schema_sha256"])
    assert "additionalProperties" in schema

    import core.trading.model_schemas as production_schema

    original = deepcopy(production_schema.AI_ACTION_SCHEMA)
    try:
        monkeypatch.setitem(production_schema.AI_ACTION_SCHEMA["properties"], "future_runtime_field", {"type": "string"})
        assert schema_version_for_experiment("A0") == version
        assert load_frozen_a0_schema()[0] == schema
    finally:
        production_schema.AI_ACTION_SCHEMA.clear()
        production_schema.AI_ACTION_SCHEMA.update(original)


def test_readonly_sqlite_has_uri_and_query_only_guards(tmp_path):
    path = tmp_path / "source.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE rows(value INTEGER)")
        db.execute("INSERT INTO rows VALUES (7)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    db = open_sqlite_readonly(path)
    try:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        assert db.execute("SELECT value FROM rows").fetchone()[0] == 7
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO rows VALUES (8)")
    finally:
        db.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_timeframe_aggregation_uses_only_closed_and_available_bars():
    start = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    decision_time = start + timedelta(minutes=10)
    hashes = {"2025-01": "a" * 64}
    bars = aggregate_timeframe_bars(
        _minute_rows(start, 10), symbol="BTCUSDT", timeframe="5m",
        decision_time=decision_time, archive_hash_by_month=hashes,
        manifest_sha256="b" * 64, source_database_sha256="c" * 64,
    )
    assert len(bars) == 1
    assert bars[0]["bar_start"] == start.isoformat().replace("+00:00", "Z")
    assert bars[0]["bar_end"] == (start + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert bars[0]["available_at"] == (start + timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    assert bars[0]["available_at_basis"] == AVAILABILITY_BASIS
    assert bars[0]["available_at_evidence_grade"] == "ASSUMED_PROXY"
    assert bars[0]["price_evidence_grade"] == "VERIFIED_ARCHIVE_RECONSTRUCTION"
    assert bars[0]["source_file_hash"] == "a" * 64
    assert bars[0]["is_closed"] is True


@pytest.mark.parametrize("change", ["duplicate", "gap", "out_of_order"])
def test_minute_data_duplicate_gap_or_order_errors_fail_closed(change):
    start = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    rows = _minute_rows(start, 12)
    if change == "duplicate":
        rows[6] = deepcopy(rows[5])
    elif change == "gap":
        rows.pop(6)
    else:
        rows[5], rows[6] = rows[6], rows[5]
    with pytest.raises(EvidenceBuildError, match="SOURCE_MINUTE_DUPLICATE_OR_GAP"):
        aggregate_timeframe_bars(
            rows, symbol="BTCUSDT", timeframe="5m",
            decision_time=start + timedelta(minutes=12),
            archive_hash_by_month={"2025-01": "a" * 64},
            manifest_sha256="b" * 64, source_database_sha256="c" * 64,
        )


def test_v36_context_ignores_future_bars_and_future_swing_confirmation():
    decision_time = datetime(2025, 4, 1, tzinfo=UTC)
    original = _v36_bars(decision_time)
    with_future = deepcopy(original)
    for timeframe, minutes in (("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240)):
        duration = timedelta(minutes=minutes)
        start = decision_time
        end = start + duration
        with_future[timeframe].append({
            "timeframe": timeframe,
            "bar_start": start.isoformat().replace("+00:00", "Z"),
            "bar_end": end.isoformat().replace("+00:00", "Z"),
            "available_at": (end + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
            "source": "test-archive", "quality_status": "FROZEN_RESEARCH", "is_closed": True,
            "open": 100, "high": 101, "low": 99, "close": 100.1, "volume": 1,
        })
    baseline = build_context(original, decision_time)
    appended = build_context(with_future, decision_time)
    assert baseline.status == appended.status == "READY"
    assert baseline.input_sha256 == appended.input_sha256
    swings = baseline.frames["5m"]["objective_facts"]["confirmed_swings"]
    assert any(item["side"] == "HIGH" for item in swings)
    for frame in baseline.frames.values():
        for swing in frame["objective_facts"]["confirmed_swings"]:
            assert datetime.fromisoformat(swing["confirmed_at"]) < decision_time
            assert datetime.fromisoformat(swing["known_at"]) < decision_time


def _make_archive_fixture(tmp_path: Path) -> tuple[Path, datetime]:
    archive = tmp_path / "archive"
    raw_cache = archive / "raw_cache"
    raw_cache.mkdir(parents=True)
    decision = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)
    start = decision - timedelta(days=8)
    db_path = archive / "research.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE bars(symbol TEXT,open_time_ms INTEGER,open REAL,high REAL,"
            "low REAL,close REAL,volume REAL,PRIMARY KEY(symbol,open_time_ms))"
        )
        rows = _minute_rows(start, 8 * 24 * 60)
        db.executemany(
            "INSERT INTO bars VALUES (:symbol,:open_time_ms,:open,:high,:low,:close,:volume)",
            rows,
        )
    archive_file = raw_cache / "BTCUSDT-1m-2025-01.zip"
    archive_file.write_bytes(b"test-only-official-archive-placeholder")
    archive_hash = hashlib.sha256(archive_file.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "binance_um_research_history_v1",
        "complete_data": True,
        "research_only": True,
        "not_gate_data": True,
        "exchange": "BINANCE_UM",
        "source_type": "OFFICIAL_MONTHLY_PUBLIC_ARCHIVES",
        "symbols": ["BTCUSDT"],
        "window_start": "2025-01-14T00:00:00+00:00",
        "window_end": "2025-01-16T00:00:00+00:00",
        "warmup_start": "2025-01-01T00:00:00+00:00",
        "dataset_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "archived_files": [{
            "kind": "klines", "symbol": "BTCUSDT", "month": "2025-01",
            "url": "https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01.zip",
            "sha256": archive_hash, "bytes": archive_file.stat().st_size,
            "official_checksum_verified": True,
        }],
    }
    (archive / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return archive, decision


def test_archive_builder_preserves_provenance_hashes_and_four_causal_frames(tmp_path):
    archive, decision = _make_archive_fixture(tmp_path)
    point_a = build_reconstructed_point(
        archive, symbol="BTCUSDT", decision_time=decision.isoformat(),
        decision_id="fixture-point", partition="research",
    )
    point_b = build_reconstructed_point(
        archive, symbol="BTCUSDT", decision_time=decision.isoformat(),
        decision_id="fixture-point", partition="research",
    )
    assert point_a["evidence_input_sha256"] == point_b["evidence_input_sha256"]
    assert point_a["context_input_sha256"] == point_b["context_input_sha256"]
    assert set(point_a["bars_by_timeframe"]) == {"5m", "15m", "1h", "4h"}
    for bars in point_a["bars_by_timeframe"].values():
        assert len(bars) >= 32
        for bar in bars:
            assert bar["bar_end"] < point_a["decision_time"]
            assert bar["available_at"] < point_a["decision_time"]
            assert bar["source_file_hashes"] == [bar["source_file_hash"]]
            assert bar["source_manifest_sha256"] == point_a["source_manifest_sha256"]
            assert bar["available_at_basis"] == AVAILABILITY_BASIS
    assert point_a["availability_is_observed_fact"] is False
    assert point_a["context"]["status"] == "READY"


def test_exact_a0_recovery_requires_full_identity_schema_and_actual_model():
    decision_time = "2025-01-15T12:00:00Z"
    point = {
        "decision_time": decision_time,
        "a0_original": {
            "prompt": "frozen historical prompt",
            "model_input": {"evidence_refs": ["bar:15m:x"]},
            "state_snapshot": {
                "equity_usdt": 1000,
                "available_margin_usdt": 900,
                "allowed_instruments": ["BTC_USDT"],
            },
            "model_id": "gemini-model-v35",
            "prompt_version": "v35-as-recorded",
            "analysis_schema_version": schema_version_for_experiment("A0"),
        },
    }
    identity = a0_cache_identity(point)
    analysis = {
        "action": "WAIT", "instrument_id": "BTC_USDT",
        "reason": "No confirmed entry trigger.", "confidence": None,
        "evidence_refs": ["bar:15m:x"],
    }
    exact = {
        "identity": identity,
        "analysis": analysis,
        "analysis_schema_version": identity["analysis_schema_version"],
        "actual_model_id": "gemini-model-v35",
    }
    assert classify_a0_record(point, [exact])["status"] == "EXACT_A0_RECOVERED"
    assert classify_a0_record(point, [dict(exact, actual_model_id="other-model")])["status"] == "CACHE_IDENTITY_MISMATCH"
    unverified = dict(exact)
    unverified.pop("actual_model_id")
    assert classify_a0_record(point, [unverified])["status"] == "PARTIAL_EVIDENCE_ONLY"
    wrong_schema = dict(exact, analysis_schema_version="unknown-effective-schema")
    assert classify_a0_record(point, [wrong_schema])["status"] == "SCHEMA_UNVERIFIED"
    incomplete = classify_a0_record({"decision_time": decision_time, "a0_original": {}}, [])
    assert incomplete["status"] == "PARTIAL_EVIDENCE_ONLY"
    assert "ORIGINAL_PROMPT_UNAVAILABLE" in incomplete["blockers"]
    assert "ORIGINAL_STATE_UNAVAILABLE" in incomplete["blockers"]
    assert "SCHEMA_UNVERIFIED" in incomplete["blockers"]


def test_missing_historical_economics_stay_blocked_without_assumption(tmp_path):
    sample = tmp_path / "v25-sample.json"
    sample.write_text(json.dumps({
        "scans": [{
            "action": "OPEN_LONG", "order_type": "LIMIT",
            "target_notional_usdt": "2000", "entry_price": "100",
            "stop_price": "99", "take_profit": "104", "requested_leverage": 5,
        }],
        "closed_trades": [],
    }), encoding="utf-8")
    result = economic_coverage_from_v25(sample)
    assert result["economic_feasibility"] == "BLOCKED"
    assert result["status_counts"] == {"BLOCKED": 1}
    assert result["proposal_resized"] is False
    assert result["v35_risk_adapter_called"] is True
    assert result["v35_economics_calculation_invoked"] is False
    assert "equity" in result["missing_evidence_field_counts"]
    assert "price_tick" in result["missing_evidence_field_counts"]


def test_offline_v36_replay_and_review_have_zero_calls_and_unobserved_order_lifecycle():
    decision_time = datetime(2025, 4, 1, tzinfo=UTC)
    point = {
        "decision_id": "proxy-engineering-only",
        "decision_time": decision_time.isoformat(),
        "bars_by_timeframe": _v36_bars(decision_time),
    }
    calls: list[object] = []

    def forbidden_model_call(*args):
        calls.append(args)
        raise AssertionError("model calls are prohibited")

    result = run_study(
        [point], cache_rows=[], model_caller=forbidden_model_call,
        model_id=None, run=False, max_decisions=None,
    )
    record = result["decision_records"][0]
    metrics = summarize(result["decision_records"])
    assert result["run_manifest"]["model_calls_used"] == 0
    assert result["run_manifest"]["mode"] == "OFFLINE_DEFAULT"
    assert calls == []
    assert record["experiments"]["A0"]["status"] == "NOT_RUN_NO_EXACT_CACHE"
    assert record["experiments"]["A1"]["status"] == "NOT_RUN_STATE_SNAPSHOT_MISSING"
    assert record["experiments"]["A2"]["status"] == "NOT_RUN_STATE_SNAPSHOT_MISSING"
    assert record["experiments"]["A3"]["status"] in {"RESEARCH_CANDIDATE", "NO_CANDIDATE"}
    assert record["experiments"]["A3"]["lifecycle"]["gateway_acceptance"] == "NOT_OBSERVED"
    assert metrics["experiments"]["A3"]["gateway_accepted_orders"] == 0
    assert metrics["experiments"]["A3"]["venue_fills"] == 0
    assert metrics["experiments"]["A3"]["complete_closes_observed"] == 0
