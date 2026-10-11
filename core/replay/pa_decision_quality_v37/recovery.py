"""Strict V25 evidence classification with no timestamp-based cache joins."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from core.replay.pa_decision_quality_v36.experiments import (
    a0_cache_identity,
    find_exact_cache,
)
from core.replay.pa_decision_quality_v36.runner import diagnose_open_proposal
from core.replay.pa_decision_quality_v36.validation import (
    collect_evidence_refs,
    compare_model_identity,
    validate_study_analysis,
)

A0_IDENTITY_FIELDS = (
    "experiment_id", "model_id", "prompt_sha256", "data_sha256",
    "decision_time", "state_sha256", "prompt_version", "analysis_schema_version",
)
ORIGINAL_A0_FIELDS = (
    "prompt", "model_input", "state_snapshot", "model_id",
    "prompt_version", "analysis_schema_version",
)
V35_RISK_REQUIRED_FIELDS = (
    "equity", "available_margin", "risk_per_trade_pct", "min_net_rr",
    "taker_fee_rate", "slippage_rate", "contract_size", "amount_step",
    "price_tick", "min_amount", "max_amount", "min_notional",
    "max_notional", "max_leverage",
)


def classify_a0_record(
    decision_point: dict[str, Any], cache_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Accept A0 only after V36 recomputes and matches every exact identity field."""
    original = decision_point.get("a0_original")
    original = original if isinstance(original, dict) else {}
    missing = [field for field in ORIGINAL_A0_FIELDS if original.get(field) in (None, "", {}, [])]
    expected = a0_cache_identity(decision_point)
    if expected is None:
        status = "PARTIAL_EVIDENCE_ONLY"
        blockers = []
        if "prompt" in missing:
            blockers.append("ORIGINAL_PROMPT_UNAVAILABLE")
        if "model_input" in missing:
            blockers.append("ORIGINAL_MODEL_INPUT_UNAVAILABLE")
        if "state_snapshot" in missing:
            blockers.append("ORIGINAL_STATE_UNAVAILABLE")
        if "analysis_schema_version" in missing:
            blockers.append("SCHEMA_UNVERIFIED")
        if "model_id" in missing:
            blockers.append("MODEL_ID_UNVERIFIED")
        if "prompt_version" in missing:
            blockers.append("PROMPT_VERSION_UNVERIFIED")
        return {
            "status": status,
            "exact_identity_recomputed": False,
            "missing_original_fields": missing,
            "blockers": sorted(set(blockers)),
            "cache_candidates_considered": 0,
            "identity_join_method": "NONE_NO_APPROXIMATE_JOIN",
        }

    cached = find_exact_cache(cache_rows, expected)
    if cached is None:
        return {
            "status": "CACHE_IDENTITY_MISMATCH",
            "exact_identity_recomputed": True,
            "missing_original_fields": [],
            "blockers": ["NO_EXACT_CACHE_IDENTITY_MATCH"],
            "cache_candidates_considered": len(cache_rows or []),
            "identity_join_method": "FULL_V36_IDENTITY_EQUALITY_ONLY",
        }

    schema_version = cached.get("analysis_schema_version")
    if schema_version is None and isinstance(cached.get("identity"), dict):
        schema_version = cached["identity"].get("analysis_schema_version")
    if schema_version != expected["analysis_schema_version"]:
        return {
            "status": "SCHEMA_UNVERIFIED",
            "exact_identity_recomputed": True,
            "missing_original_fields": [],
            "blockers": ["ANALYSIS_SCHEMA_VERSION_MISSING_OR_MISMATCH"],
            "cache_candidates_considered": len(cache_rows or []),
            "identity_join_method": "FULL_V36_IDENTITY_EQUALITY_ONLY",
        }
    valid_refs = collect_evidence_refs(original["model_input"])
    validation = validate_study_analysis(
        "A0", cached.get("analysis"),
        valid_evidence_refs=valid_refs,
        declared_schema_version=schema_version,
        state_snapshot=original["state_snapshot"],
    )
    identity_status = compare_model_identity(cached.get("actual_model_id"), expected["model_id"])
    if not validation["valid"] or identity_status != "MATCHED":
        blockers = list(validation["errors"])
        if identity_status == "MISMATCH":
            blockers.append("MODEL_ID_MISMATCH")
        elif identity_status == "UNVERIFIED":
            blockers.append("MODEL_ID_UNVERIFIED")
        return {
            "status": "CACHE_IDENTITY_MISMATCH" if identity_status == "MISMATCH" else "PARTIAL_EVIDENCE_ONLY",
            "exact_identity_recomputed": True,
            "missing_original_fields": [],
            "blockers": sorted(set(blockers)),
            "cache_candidates_considered": len(cache_rows or []),
            "analysis_validation": validation,
            "model_identity_status": identity_status,
            "identity_join_method": "FULL_V36_IDENTITY_EQUALITY_ONLY",
        }
    return {
        "status": "EXACT_A0_RECOVERED",
        "exact_identity_recomputed": True,
        "missing_original_fields": [],
        "blockers": [],
        "cache_candidates_considered": len(cache_rows or []),
        "analysis_validation": validation,
        "model_identity_status": identity_status,
        "identity_join_method": "FULL_V36_IDENTITY_EQUALITY_ONLY",
    }


def recover_v25_summary(sample_path: Path) -> dict[str, Any]:
    """Summarize only the sanitized V25 sample; never invent or import cache joins."""
    import json

    payload = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    scans = payload.get("scans") if isinstance(payload, dict) else None
    if not isinstance(scans, list):
        raise TypeError("V25_SAMPLE_SCANS_INVALID")
    status_counts: Counter[str] = Counter()
    blocker_counts: Counter[str] = Counter()
    field_presence: Counter[str] = Counter()
    for scan in scans:
        if not isinstance(scan, dict):
            raise TypeError("V25_SAMPLE_SCAN_NOT_OBJECT")
        for field in ("time_utc", "action", "instrument", "decision_id", "prompt",
                      "model_input", "state_snapshot", "model_id", "prompt_version",
                      "analysis_schema_version", "prompt_sha256", "data_sha256",
                      "state_sha256"):
            if scan.get(field) not in (None, "", {}, []):
                field_presence[field] += 1
        result = classify_a0_record({
            "decision_time": scan.get("time_utc"),
            "a0_original": {},
        }, cache_rows=[])
        status_counts[result["status"]] += 1
        blocker_counts.update(result["blockers"])
    return {
        "schema_version": "pa-decision-quality-v37/a0-recovery-coverage-1",
        "source_scope": "SANITIZED_V25_SAMPLE_ONLY_NO_RAW_PROMPT_OR_CACHE_ROW_JOIN",
        "decision_count": len(scans),
        "forensic_dataset_status": "UNRECOVERABLE_FOR_EXACT_A0" if scans else "UNAVAILABLE",
        "exact_a0_recovered": status_counts.get("EXACT_A0_RECOVERED", 0),
        "status_counts": dict(sorted(status_counts.items())),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "sanitized_sample_identity_field_presence": {
            "sample_count": len(scans),
            "present_rows": dict(sorted(field_presence.items())),
        },
        "cache_join_policy": {
            "exact_identity_fields": list(A0_IDENTITY_FIELDS),
            "timestamp_or_symbol_join_permitted": False,
            "nearby_pilot_caches_relabelled_as_v25": False,
            "cache_rows_imported": 0,
        },
        "limits": [
            "The sanitized V25 sample omits original prompts, original model inputs, state snapshots, exact cache identity hashes, and per-call effective schemas.",
            "The separately inventoried V25 recovery pilot databases are not an exact V36 cache export and have no demonstrated row-level link to these 100 scans.",
            "The frozen V35 base schema cannot establish which input-dependent effective schema a historical call received.",
        ],
    }


def economic_coverage_from_v25(sample_path: Path) -> dict[str, Any]:
    """Use the V36 V35-risk adapter and block when historical economics are absent."""
    import json

    payload = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    scans = payload.get("scans") if isinstance(payload, dict) else None
    if not isinstance(scans, list):
        raise TypeError("V25_SAMPLE_SCANS_INVALID")
    proposals = 0
    status_counts: Counter[str] = Counter()
    missing_counts: Counter[str] = Counter()
    v35_calculator_invoked = False
    for scan in scans:
        action = str(scan.get("action") or "").upper()
        if action not in {"OPEN_LONG", "OPEN_SHORT"}:
            status_counts["NOT_APPLICABLE_NO_OPEN_PROPOSAL"] += 1
            continue
        proposals += 1
        proposal = {
            "side": "LONG" if action == "OPEN_LONG" else "SHORT",
            "order_type": scan.get("order_type"),
            "proposed_notional_usdt": scan.get("target_notional_usdt"),
            "entry_price": scan.get("entry_price"),
            "stop_price": scan.get("stop_price"),
            "target_price": scan.get("take_profit"),
            "requested_leverage": scan.get("requested_leverage"),
        }
        # No hypothetical equity or current contract rules are supplied.
        result = diagnose_open_proposal(proposal, {}, mode="FIXED_NOTIONAL")
        status_counts[str(result.get("status") or "UNKNOWN")] += 1
        missing_counts.update(result.get("missing_fields") or [])
        v35_calculator_invoked = v35_calculator_invoked or isinstance(result.get("economics"), dict)
    closed_trades = payload.get("closed_trades") if isinstance(payload.get("closed_trades"), list) else []
    return {
        "schema_version": "pa-decision-quality-v37/economic-evidence-coverage-1",
        "source_scope": "V25_SANITIZED_SUMMARY_ONLY",
        "proposal_count": proposals,
        "status_counts": dict(sorted(status_counts.items())),
        "missing_evidence_field_counts": dict(sorted(missing_counts.items())),
        "closed_trade_records_observed": len(closed_trades),
        "closed_trade_values_recomputed": False,
        "v35_economic_evaluator": "core.trading.trade_feasibility.diagnose_trade_proposal via V36 diagnose_open_proposal",
        "v35_risk_adapter_called": proposals > 0,
        "v35_economics_calculation_invoked": v35_calculator_invoked,
        "critical_missing_evidence": list(V35_RISK_REQUIRED_FIELDS),
        "economic_feasibility": "BLOCKED" if proposals else "NO_OPEN_PROPOSALS_IN_SAMPLE",
        "proposal_resized": False,
        "lifecycle": {
            "historical_proposal": "OBSERVED_IN_SANITIZED_SUMMARY",
            "gateway_acceptance": "NOT_RECONSTRUCTED",
            "venue_fill": "ONLY_SEPARATELY_REPORTED_CLOSED_TRADE_SUMMARIES",
            "complete_close": "10_RECORDED_CLOSED_TRADES" if len(closed_trades) == 10 else "NOT_ESTABLISHED",
        },
        "limits": [
            "No point-in-time equity, available margin, pending-order exposure, or V25-time contract limits were available for these scans.",
            "The sample cost assumptions are not verified as account- or venue-specific historical settings and were not promoted to facts.",
            "V35 calculation is not run with fabricated values; once required facts are supplied the same V35 evaluator is used by the adapter.",
        ],
    }
