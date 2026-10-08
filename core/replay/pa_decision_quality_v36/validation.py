"""Version-bound research analysis validation for live and cached decisions."""
from __future__ import annotations

from typing import Any

from core.replay.pa_decision_quality_v37.schema import load_frozen_a0_schema
from core.trading.model_schemas import (
    require_entry_analysis_for_open,
    validate_schema,
)

from .schema import SCHEMA_VERSION, validate_analysis

_V35_ACTION_SCHEMA, V35_ACTION_SCHEMA_VERSION, _V35_SCHEMA_RECORD = load_frozen_a0_schema()


def schema_version_for_experiment(experiment_id: str) -> str | None:
    """Return the exact analysis contract used by an experiment arm."""
    key = str(experiment_id or "").upper()
    if key == "A0":
        return V35_ACTION_SCHEMA_VERSION
    if key in {"A1", "A2"}:
        return SCHEMA_VERSION
    return None


def collect_evidence_refs(value: Any) -> set[str]:
    """Collect only declared evidence_refs arrays from a frozen model input."""
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_refs" and isinstance(item, list):
                result.update(ref for ref in item if isinstance(ref, str))
            result.update(collect_evidence_refs(item))
    elif isinstance(value, list):
        for item in value:
            result.update(collect_evidence_refs(item))
    return result


def validate_study_analysis(
    experiment_id: str,
    analysis: Any,
    *,
    valid_evidence_refs: set[str],
    declared_schema_version: Any,
    state_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate an analysis using the arm's pinned schema and evidence set."""
    expected = schema_version_for_experiment(experiment_id)
    errors: list[str] = []
    if expected is None:
        errors.append("EXPERIMENT_SCHEMA_UNAVAILABLE")
    elif not isinstance(declared_schema_version, str) or not declared_schema_version.strip():
        errors.append("ANALYSIS_SCHEMA_VERSION_MISSING")
    elif declared_schema_version != expected:
        errors.append("ANALYSIS_SCHEMA_VERSION_MISMATCH")

    if experiment_id == "A0":
        try:
            validate_schema(analysis, _V35_ACTION_SCHEMA)
            require_entry_analysis_for_open(analysis)
        except (TypeError, ValueError, KeyError, AttributeError):
            errors.append("V35_ANALYSIS_SCHEMA_INVALID")
        if not isinstance(analysis, dict):
            errors.append("ANALYSIS_NOT_OBJECT")
        else:
            refs = analysis.get("evidence_refs", [])
            if refs is None:
                refs = []
            if not isinstance(refs, list) or any(
                not isinstance(ref, str) or ref not in valid_evidence_refs for ref in refs
            ):
                errors.append("V35_EVIDENCE_REFERENCE_INVALID")
    else:
        errors.extend(validate_analysis(
            analysis, valid_evidence_refs, state_snapshot=state_snapshot,
        ))

    clean_errors = sorted(set(errors))
    return {
        "status": "VALID" if not clean_errors else "INVALID",
        "valid": not clean_errors,
        "experiment_id": str(experiment_id).upper(),
        "declared_schema_version": declared_schema_version,
        "expected_schema_version": expected,
        "errors": clean_errors,
    }


def compare_model_identity(actual_model_id: Any, requested_model_id: Any) -> str:
    """Compare reported and requested IDs without promoting request metadata."""
    actual = actual_model_id.strip() if isinstance(actual_model_id, str) else ""
    requested = requested_model_id.strip() if isinstance(requested_model_id, str) else ""
    if not actual or not requested:
        return "UNVERIFIED"
    return "MATCHED" if actual == requested else "MISMATCH"


def validation_state_snapshot(state_snapshot: Any) -> dict[str, Any] | None:
    """Keep the minimum redacted state needed to revalidate management references."""
    if not isinstance(state_snapshot, dict):
        return None
    positions = state_snapshot.get("positions", [])
    orders = state_snapshot.get("working_orders", [])
    if not isinstance(positions, list) or not isinstance(orders, list):
        return None
    clean_positions = []
    for item in positions:
        if not isinstance(item, dict) or not isinstance(item.get("position_ref"), str):
            return None
        clean_positions.append({
            key: item[key] for key in ("position_ref", "side", "stop_price") if key in item
        })
    clean_orders = []
    for item in orders:
        if not isinstance(item, dict) or not isinstance(item.get("order_ref"), str):
            return None
        clean_orders.append({"order_ref": item["order_ref"]})
    return {"positions": clean_positions, "working_orders": clean_orders}
