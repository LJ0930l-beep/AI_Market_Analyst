"""V38 market-only track with no account, execution, model, or order authority."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from core.replay.pa_decision_quality_v36.context import CausalContext, build_context
from core.replay.pa_decision_quality_v37.partitioning import (
    load_frozen_partition_policy,
    partition_for_time,
)

INPUT_SCHEMA_VERSION = "pa-market-only-v38/input-1"
ANALYSIS_SCHEMA_VERSION = "pa-market-only-v38/analysis-1"
RECORD_SCHEMA_VERSION = "pa-market-only-v38/record-1"
REVIEW_SCHEMA_VERSION = "pa-market-only-v38/review-1"
REQUIRED_TIMEFRAMES = ("15m", "5m", "1h", "4h")
ALLOWED_PARTITIONS = {"optimization", "validation", "untouched_test"}

_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SOURCES = {
    "BINANCE_UM_OFFICIAL_MONTHLY_ARCHIVE_RECONSTRUCTED",
    "fixture:deterministic",
}
_FORBIDDEN_FIELDS = {
    "account", "account_snapshot", "available_margin", "available_margin_usdt", "equity",
    "equity_usdt", "execution_constraints", "entry_price", "stop_price", "target_price",
    "order_type", "proposed_notional_usdt", "requested_leverage", "leverage", "position",
    "positions", "position_size", "working_orders", "net_reward_risk", "net_rr",
    "risk_pass", "eligible_proposal", "proposal", "gateway_accepted", "venue_fill",
    "complete_close", "fill", "trade_id", "state_snapshot", "risk_inputs",
}
_FORBIDDEN_ACTIONS = {"OPEN_LONG", "OPEN_SHORT", "LONG", "SHORT", "ELIGIBLE_PROPOSAL"}
_REGIMES = {"BULL_TREND", "BEAR_TREND", "TRADING_RANGE", "BREAKOUT_TRANSITION", "UNCERTAIN"}
_BIASES = {"BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"}
_LOCATIONS = {
    "TREND_PULLBACK", "RANGE_UPPER", "RANGE_LOWER", "RANGE_MIDDLE", "STRUCTURE_LEVEL",
    "BREAKOUT_RETEST", "EXTENDED", "OTHER", "UNKNOWN",
}
_SIGNALS = {
    "H1", "H2", "L1", "L2", "BREAKOUT", "BREAKOUT_PULLBACK", "FAILED_BREAKOUT",
    "REVERSAL", "WEDGE", "TRADING_RANGE_REVERSAL", "NONE", "OTHER", "UNKNOWN",
}
_SIGNAL_QUALITY = {"STRONG", "CONDITIONAL", "WEAK", "NO_SIGNAL", "UNKNOWN"}
_MARKET_ANALYSIS_FIELDS = {
    "schema_version", "action", "market_view",
}
_MARKET_VIEW_FIELDS = {
    "market_regime", "higher_timeframe_bias", "location", "signal", "indicative_bias",
    "candidate_setup", "target_structure", "counter_evidence", "decision_rationale",
    "evidence_refs", "wait_reason",
}


class MarketOnlyError(ValueError):
    """Stable failure code for a malformed or unsafe V38 market-only record."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc(value: Any, code: str) -> datetime:
    if isinstance(value, datetime):
        point = value
    elif isinstance(value, str):
        try:
            point = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise MarketOnlyError(code) from exc
    else:
        raise MarketOnlyError(code)
    if point.tzinfo is None or point.utcoffset() is None:
        raise MarketOnlyError(code)
    return point.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_sha(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MarketOnlyError("MARKET_ONLY_INPUT_NOT_CANONICAL_JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _reject_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in _FORBIDDEN_FIELDS:
                return True
            if normalized == "action" and isinstance(child, str) and child.strip().upper() in _FORBIDDEN_ACTIONS:
                return True
            if _reject_forbidden(child):
                return True
        return False
    if isinstance(value, list):
        return any(_reject_forbidden(item) for item in value)
    return False


def _is_one_of(value: Any, choices: set[str]) -> bool:
    """Check string enums without allowing unhashable malformed JSON to raise."""
    return isinstance(value, str) and value in choices


def _source_provenance(
    bars_by_timeframe: dict[str, Any], context: CausalContext,
) -> dict[str, Any]:
    used = {
        (timeframe, _stamp(bar.end))
        for timeframe, bars in context.bars.items()
        for bar in bars
    }
    selected: list[dict[str, Any]] = []
    for timeframe, rows in bars_by_timeframe.items():
        for row in rows:
            if (timeframe, _stamp(_utc(row.get("bar_end"), "BAR_TIME_INVALID"))) in used:
                selected.append(row)
    sources = sorted({str(row.get("source", "")) for row in selected})
    if any(source not in _ALLOWED_SOURCES for source in sources):
        raise MarketOnlyError("MARKET_SOURCE_NOT_ALLOWLISTED")

    archive_file_hashes: set[str] = set()
    database_hashes: set[str] = set()
    manifest_hashes: set[str] = set()
    for row in selected:
        hashes = row.get("source_file_hashes", [])
        if hashes is None:
            hashes = []
        if not isinstance(hashes, list):
            raise MarketOnlyError("SOURCE_FILE_HASHES_INVALID")
        one_hash = row.get("source_file_hash")
        if one_hash is not None:
            hashes = [*hashes, one_hash]
        for digest in hashes:
            if not isinstance(digest, str) or not _HEX_256.fullmatch(digest):
                raise MarketOnlyError("SOURCE_FILE_HASH_INVALID")
            archive_file_hashes.add(digest)
        for field, collected in (
            ("source_database_sha256", database_hashes),
            ("source_manifest_sha256", manifest_hashes),
        ):
            digest = row.get(field)
            if digest is not None:
                if not isinstance(digest, str) or not _HEX_256.fullmatch(digest):
                    raise MarketOnlyError("SOURCE_MANIFEST_HASH_INVALID")
                collected.add(digest)

    def grades(field: str, default: str) -> list[str]:
        values = {str(row.get(field) or default) for row in selected}
        return sorted(values)

    return {
        "source_kind": sources,
        "source_exchange": sorted({str(row.get("source_exchange") or "UNVERIFIED") for row in selected}),
        "price_evidence_grade": grades("price_evidence_grade", "UNVERIFIED"),
        "availability_evidence_grade": grades("available_at_evidence_grade", "UNVERIFIED"),
        "availability_basis": grades("available_at_basis", "UNSPECIFIED"),
        "volume_unit": grades("volume_unit", "UNSPECIFIED"),
        "archive_file_sha256s": sorted(archive_file_hashes),
        "archive_database_sha256s": sorted(database_hashes),
        "archive_manifest_sha256s": sorted(manifest_hashes),
    }


def build_market_only_input(point: Any) -> dict[str, Any]:
    """Project one V37-style market point into an account-free causal input."""
    if not isinstance(point, dict):
        raise MarketOnlyError("MARKET_POINT_INVALID")
    allowed = {"decision_id", "decision_time", "symbol", "partition", "bars_by_timeframe"}
    if set(point) - allowed:
        extra = set(point) - allowed
        if any(str(key).lower() in _FORBIDDEN_FIELDS for key in extra):
            raise MarketOnlyError("MARKET_ONLY_ACCOUNT_OR_EXECUTION_INPUT_FORBIDDEN")
        raise MarketOnlyError("MARKET_POINT_FIELD_NOT_ALLOWED")
    decision_id = point.get("decision_id")
    symbol = point.get("symbol")
    partition = point.get("partition")
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise MarketOnlyError("DECISION_ID_INVALID")
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{3,20}", symbol):
        raise MarketOnlyError("MARKET_SYMBOL_INVALID")
    if not _is_one_of(partition, ALLOWED_PARTITIONS):
        raise MarketOnlyError("PARTITION_INVALID")

    decision_time = _utc(point.get("decision_time"), "DECISION_TIME_INVALID")
    policy, _ = load_frozen_partition_policy()
    resolved_partition = partition_for_time(decision_time, policy["partitions"])
    if partition != resolved_partition:
        raise MarketOnlyError("PARTITION_LABEL_MISMATCH")

    bars_by_timeframe = point.get("bars_by_timeframe")
    if not isinstance(bars_by_timeframe, dict) or set(bars_by_timeframe) != set(REQUIRED_TIMEFRAMES):
        raise MarketOnlyError("MARKET_TIMEFRAMES_INVALID")
    for timeframe in REQUIRED_TIMEFRAMES:
        rows = bars_by_timeframe.get(timeframe)
        if not isinstance(rows, list):
            raise MarketOnlyError("MARKET_BARS_INVALID")
        for row in rows:
            if not isinstance(row, dict):
                raise MarketOnlyError("MARKET_BAR_INVALID")
            bar_end = _utc(row.get("bar_end"), "BAR_TIME_INVALID")
            available_at = _utc(row.get("available_at"), "BAR_TIME_INVALID")
            if available_at < bar_end:
                raise MarketOnlyError("BAR_AVAILABILITY_PRECEDES_CONFIRMATION")

    context = build_context(bars_by_timeframe, decision_time)
    if context.status != "READY":
        raise MarketOnlyError("MARKET_CONTEXT_INCOMPLETE")
    provenance = _source_provenance(bars_by_timeframe, context)
    refs = sorted(context.evidence_refs)
    if not refs:
        raise MarketOnlyError("MARKET_EVIDENCE_REFS_MISSING")
    available_by_timeframe = {
        timeframe: _stamp(max(bar.available_at for bar in bars))
        for timeframe, bars in sorted(context.bars.items()) if bars
    }
    if set(available_by_timeframe) != set(REQUIRED_TIMEFRAMES):
        raise MarketOnlyError("MARKET_TIMEFRAMES_INCOMPLETE")
    if any(_utc(value, "BAR_TIME_INVALID") >= decision_time for value in available_by_timeframe.values()):
        raise MarketOnlyError("MARKET_INPUT_NOT_CAUSAL")

    payload: dict[str, Any] = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "track": "MARKET_ONLY",
        "decision_id": decision_id.strip(),
        "symbol": symbol,
        "partition": partition,
        "decision_time": _stamp(decision_time),
        "data_available_through_by_timeframe": available_by_timeframe,
        "market_context": {
            "schema_version": "pa-decision-quality-v36/context-1",
            "status": context.status,
            "input_sha256": context.input_sha256,
            "data_as_of_by_timeframe": {
                timeframe: frame.get("data_as_of") for timeframe, frame in sorted(context.frames.items())
            },
            "frames": context.frames,
        },
        "evidence_refs": refs,
        "provenance": provenance,
        "authority": {
            "production_authority": False,
            "order_creation_authorized": False,
            "account_state_included": False,
            "execution_constraints_included": False,
        },
    }
    payload["market_input_sha256"] = _canonical_sha(payload)
    return payload


def validate_market_only_analysis(analysis: Any, *, valid_evidence_refs: set[str]) -> list[str]:
    """Validate a non-executable market opinion without repairing its contents."""
    errors: list[str] = []
    if not isinstance(analysis, dict):
        return ["MARKET_ANALYSIS_NOT_OBJECT"]
    try:
        json.dumps(analysis, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return ["MARKET_ANALYSIS_NOT_JSON_SERIALIZABLE"]
    if _reject_forbidden(analysis):
        errors.append("MARKET_ONLY_EXECUTION_OR_RISK_FIELD_FORBIDDEN")
    if set(analysis) != _MARKET_ANALYSIS_FIELDS:
        errors.append("MARKET_ONLY_SCHEMA_FIELDS_INVALID")
    if analysis.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        errors.append("MARKET_ONLY_SCHEMA_VERSION_INVALID")
    action = analysis.get("action")
    if not _is_one_of(action, {"OBSERVE", "WAIT"}):
        errors.append("MARKET_ONLY_ACTION_INVALID")
    view = analysis.get("market_view")
    if not isinstance(view, dict):
        return sorted({*errors, "MARKET_ONLY_VIEW_INVALID"})
    if set(view) != _MARKET_VIEW_FIELDS:
        errors.append("MARKET_ONLY_VIEW_FIELDS_INVALID")
    if not _is_one_of(view.get("market_regime"), _REGIMES):
        errors.append("MARKET_ONLY_REGIME_INVALID")
    if not _is_one_of(view.get("higher_timeframe_bias"), _BIASES):
        errors.append("MARKET_ONLY_HTF_BIAS_INVALID")
    if not _is_one_of(view.get("location"), _LOCATIONS):
        errors.append("MARKET_ONLY_LOCATION_INVALID")
    signal = view.get("signal")
    if (not isinstance(signal, dict) or set(signal) != {"setup", "quality"}
            or not _is_one_of(signal.get("setup"), _SIGNALS)
            or not _is_one_of(signal.get("quality"), _SIGNAL_QUALITY)):
        errors.append("MARKET_ONLY_SIGNAL_INVALID")
    if not _is_one_of(view.get("indicative_bias"), {"LONG", "SHORT", "NEUTRAL", "UNKNOWN"}):
        errors.append("MARKET_ONLY_INDICATIVE_BIAS_INVALID")

    candidate = view.get("candidate_setup")
    if (not isinstance(candidate, dict) or set(candidate) != {"kind", "description", "evidence_refs"}
            or not _is_one_of(candidate.get("kind"), {"NONE", "CONDITIONAL", "UNASSESSED"})
            or not isinstance(candidate.get("description"), str)
            or not candidate["description"].strip()
            or not _valid_refs(candidate.get("evidence_refs"), valid_evidence_refs, allow_empty=True)):
        errors.append("MARKET_ONLY_CANDIDATE_SETUP_INVALID")

    target = view.get("target_structure")
    if (not isinstance(target, dict) or set(target) != {"kind", "rationale", "evidence_refs"}
            or not _is_one_of(target.get("kind"), {
                "PRIOR_SWING", "RANGE_EXTREME", "TREND_MEASURED_MOVE", "NONE", "UNKNOWN",
            })
            or not isinstance(target.get("rationale"), str)
            or not target["rationale"].strip()
            or not _valid_refs(target.get("evidence_refs"), valid_evidence_refs, allow_empty=True)):
        errors.append("MARKET_ONLY_TARGET_STRUCTURE_INVALID")

    counter = view.get("counter_evidence")
    if not isinstance(counter, list) or not counter or any(
        not isinstance(item, str) or not item.strip() for item in counter
    ):
        errors.append("MARKET_ONLY_COUNTER_EVIDENCE_INVALID")
    rationale = view.get("decision_rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append("MARKET_ONLY_RATIONALE_INVALID")
    if not _valid_refs(view.get("evidence_refs"), valid_evidence_refs, allow_empty=False):
        errors.append("MARKET_ONLY_EVIDENCE_REF_INVALID")
    wait_reason = view.get("wait_reason")
    if action == "WAIT" and (not isinstance(wait_reason, str) or wait_reason not in {
        "UNCERTAIN_CONTEXT", "NO_CONFIRMED_SETUP", "COUNTER_EVIDENCE_DOMINATES",
        "DATA_INSUFFICIENT", "NOT_ASSESSED_BY_STUB",
    }):
        errors.append("MARKET_ONLY_WAIT_REASON_INVALID")
    if action == "OBSERVE" and wait_reason is not None:
        errors.append("MARKET_ONLY_WAIT_REASON_INVALID")
    return sorted(set(errors))


def _valid_refs(value: Any, valid: set[str], *, allow_empty: bool) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and item in valid for item in value)
        and len(value) == len(set(value))
    )


def _offline_stub_analysis(request: dict[str, Any]) -> dict[str, Any]:
    """Return a conspicuously non-model fixture used only to exercise offline plumbing."""
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "action": "WAIT",
        "market_view": {
            "market_regime": "UNCERTAIN",
            "higher_timeframe_bias": "UNCERTAIN",
            "location": "UNKNOWN",
            "signal": {"setup": "UNKNOWN", "quality": "UNKNOWN"},
            "indicative_bias": "UNKNOWN",
            "candidate_setup": {
                "kind": "UNASSESSED",
                "description": "Deterministic offline test fixture; no market interpretation was performed.",
                "evidence_refs": [],
            },
            "target_structure": {
                "kind": "UNKNOWN",
                "rationale": "Not assessed by the offline test fixture.",
                "evidence_refs": [],
            },
            "counter_evidence": ["No model interpretation is represented by this fixture."],
            "decision_rationale": "Deterministic offline fixture; this is not a Gemini decision.",
            "evidence_refs": request["evidence_refs"][:1],
            "wait_reason": "NOT_ASSESSED_BY_STUB",
        },
    }


def run_offline_stub(points: Any) -> dict[str, Any]:
    """Run deterministic stub fixtures; this function has no provider or network path."""
    if not isinstance(points, list) or not points:
        raise MarketOnlyError("MARKET_POINTS_REQUIRED")
    decision_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for point in points:
        request = build_market_only_input(point)
        decision_id = request["decision_id"]
        if decision_id in decision_ids:
            raise MarketOnlyError("DECISION_ID_DUPLICATE")
        decision_ids.add(decision_id)
        analysis = _offline_stub_analysis(request)
        errors = validate_market_only_analysis(analysis, valid_evidence_refs=set(request["evidence_refs"]))
        if errors:
            raise MarketOnlyError("OFFLINE_STUB_FIXTURE_INVALID")
        records.append({
            "schema_version": RECORD_SCHEMA_VERSION,
            "track": "MARKET_ONLY",
            "decision_id": decision_id,
            "symbol": request["symbol"],
            "partition": request["partition"],
            "decision_time": request["decision_time"],
            "market_input_sha256": request["market_input_sha256"],
            "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
            "analysis_source": "OFFLINE_STUB_FIXTURE",
            "model_identity_status": "NOT_APPLICABLE_NO_MODEL_CALL",
            "status": "VALID_STUB_FIXTURE",
            "market_input": request,
            "analysis": analysis,
            "trade_lifecycle": {
                "market_record": "OFFLINE_STUB_ONLY",
                "proposal": "NOT_CREATED",
                "gateway_acceptance": "NOT_OBSERVED",
                "venue_fill": "NOT_OBSERVED",
                "complete_close": "NOT_OBSERVED",
            },
            "authority": {
                "production_authority": False,
                "order_creation_authorized": False,
                "account_risk_pass_claimed": False,
                "net_rr_verified": False,
            },
        })
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "track": "MARKET_ONLY",
        "records": records,
        "metrics": review_market_only_records(records),
    }


def review_market_only_records(records: Any) -> dict[str, Any]:
    """Revalidate records and report only market-record plumbing, never trade outcomes."""
    if not isinstance(records, list):
        raise MarketOnlyError("MARKET_ONLY_RECORDS_INVALID")
    statuses: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    analysis_valid = 0
    stub_only = 0
    for record in records:
        if not isinstance(record, dict):
            statuses["INVALID_RECORD"] += 1
            continue
        status = str(record.get("status") or "INVALID_RECORD")
        request = record.get("market_input")
        analysis = record.get("analysis")
        if (record.get("schema_version") != RECORD_SCHEMA_VERSION
                or record.get("track") != "MARKET_ONLY"
                or status != "VALID_STUB_FIXTURE"
                or record.get("analysis_source") != "OFFLINE_STUB_FIXTURE"
                or record.get("model_identity_status") != "NOT_APPLICABLE_NO_MODEL_CALL"
                or record.get("analysis_schema_version") != ANALYSIS_SCHEMA_VERSION
                or not isinstance(request, dict)
                or request.get("schema_version") != INPUT_SCHEMA_VERSION
                or record.get("market_input_sha256") != request.get("market_input_sha256")
                or _reject_forbidden(request)
                or not _valid_stored_input(request)
                or record.get("decision_id") != request.get("decision_id")
                or record.get("symbol") != request.get("symbol")
                or record.get("partition") != request.get("partition")
                or record.get("decision_time") != request.get("decision_time")):
            statuses["INVALID_RECORD_CONTRACT"] += 1
            continue
        if validate_market_only_analysis(analysis, valid_evidence_refs=set(request.get("evidence_refs", []))):
            statuses["INVALID_ANALYSIS"] += 1
            continue
        if record.get("authority") != {
            "production_authority": False,
            "order_creation_authorized": False,
            "account_risk_pass_claimed": False,
            "net_rr_verified": False,
        }:
            statuses["INVALID_AUTHORITY"] += 1
            continue
        if record.get("trade_lifecycle") != {
            "market_record": "OFFLINE_STUB_ONLY",
            "proposal": "NOT_CREATED",
            "gateway_acceptance": "NOT_OBSERVED",
            "venue_fill": "NOT_OBSERVED",
            "complete_close": "NOT_OBSERVED",
        }:
            statuses["INVALID_LIFECYCLE"] += 1
            continue
        statuses[status] += 1
        analysis_valid += 1
        stub_only += 1
        actions[analysis["action"]] += 1
    denominator = len(records)
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "decision_count_denominator": denominator,
        "valid_analysis_records": analysis_valid,
        "offline_stub_fixture_records": stub_only,
        "invalid_or_unverified_records": denominator - analysis_valid,
        "status_counts": dict(sorted(statuses.items())),
        "action_counts": dict(sorted(actions.items())),
        "model_calls_used": 0,
        "orders_created": 0,
        "eligible_proposals_created": 0,
        "gateway_acceptances_observed": 0,
        "venue_fills_observed": 0,
        "complete_closes_observed": 0,
        "market_quality_claim_permitted": False,
        "trade_profit_metrics_permitted": False,
    }


def _valid_stored_input(request: dict[str, Any]) -> bool:
    if set(request) != {
        "schema_version", "track", "decision_id", "symbol", "partition", "decision_time",
        "data_available_through_by_timeframe", "market_context", "evidence_refs", "provenance",
        "authority", "market_input_sha256",
    }:
        return False
    if request.get("track") != "MARKET_ONLY" or request.get("partition") not in ALLOWED_PARTITIONS:
        return False
    try:
        decision_time = _utc(request.get("decision_time"), "DECISION_TIME_INVALID")
        policy, _ = load_frozen_partition_policy()
        if partition_for_time(decision_time, policy["partitions"]) != request.get("partition"):
            return False
        availability = request.get("data_available_through_by_timeframe")
        if not isinstance(availability, dict) or set(availability) != set(REQUIRED_TIMEFRAMES):
            return False
        if any(_utc(value, "BAR_TIME_INVALID") >= decision_time for value in availability.values()):
            return False
        refs = request.get("evidence_refs")
        if not _valid_refs(refs, set(refs) if isinstance(refs, list) else set(), allow_empty=False):
            return False
        authority = request.get("authority")
        if authority != {
            "production_authority": False,
            "order_creation_authorized": False,
            "account_state_included": False,
            "execution_constraints_included": False,
        }:
            return False
        without_digest = {key: value for key, value in request.items() if key != "market_input_sha256"}
        return request.get("market_input_sha256") == _canonical_sha(without_digest)
    except (MarketOnlyError, TypeError, ValueError):
        return False


def validate_stored_market_only_input(request: Any) -> bool:
    """Check a persisted V38 input's schema, immutable digest, partition and cutoff."""
    return (
        isinstance(request, dict)
        and request.get("schema_version") == INPUT_SCHEMA_VERSION
        and _valid_stored_input(request)
    )
