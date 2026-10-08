"""Offline V36 study orchestration with explicit model-call and risk gates."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from core.replay.pa_decision_quality_v37.a3_policy import load_a3_signal_policy
from core.trading.trade_feasibility import diagnose_trade_proposal

from .context import CausalContext, build_context
from .experiments import (
    EXPERIMENTS,
    a0_cache_identity,
    exact_cache_identity,
    experiment_prompt,
    find_exact_cache,
    prompt_sha256,
    safe_state_snapshot,
)
from .schema import SCHEMA_VERSION
from .validation import (
    collect_evidence_refs,
    compare_model_identity,
    validate_study_analysis,
    validation_state_snapshot,
)


class StudyError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_A3_SIGNAL_POLICY, A3_SIGNAL_POLICY_SHA256 = load_a3_signal_policy()
A3_SIGNAL_RULE_ID = _A3_SIGNAL_POLICY["rule_id"]
A3_MAX_SIGNAL_AGE_SECONDS = _A3_SIGNAL_POLICY["signal"]["maximum_age_seconds"]
A3_MAX_QUOTE_AGE_SECONDS = _A3_SIGNAL_POLICY["execution"]["maximum_quote_age_seconds"]


def _sha(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _decision_time(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise StudyError("DECISION_TIME_INVALID")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise StudyError("DECISION_TIME_INVALID")
    try:
        point = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise StudyError("DECISION_TIME_INVALID") from exc
    if point.tzinfo is None or point.utcoffset() is None:
        raise StudyError("DECISION_TIME_INVALID")
    return point.astimezone(UTC).isoformat().replace("+00:00", "Z")


def validate_model_call_gate(*, run: bool, max_decisions: int | None) -> None:
    if run is True:
        if isinstance(max_decisions, bool) or not isinstance(max_decisions, int) or max_decisions <= 0:
            raise StudyError("MODEL_CALL_REQUIRES_POSITIVE_MAX_DECISIONS")
    elif max_decisions is not None:
        raise StudyError("MAX_DECISIONS_REQUIRES_RUN_FLAG")


def diagnose_open_proposal(proposal: dict[str, Any], risk_inputs: dict[str, Any], *, mode: str) -> dict[str, Any]:
    """Run the unchanged V35 economics on the unchanged proposal, or fail closed."""
    if not isinstance(proposal, dict) or not isinstance(risk_inputs, dict):
        return {"status": "BLOCKED", "reason_codes": ["ECONOMICS_INPUT_INVALID"], "proposal_unchanged": True}
    required = (
        "side", "order_type", "proposed_notional_usdt", "entry_price", "stop_price",
        "target_price", "requested_leverage",
    )
    missing = [key for key in required if proposal.get(key) is None]
    policy_required = (
        "equity", "risk_per_trade_pct", "min_net_rr", "taker_fee_rate", "slippage_rate",
        "contract_size", "amount_step", "price_tick", "available_margin",
        "min_amount", "max_amount", "min_notional", "max_notional", "max_leverage",
    )
    missing.extend(key for key in policy_required if risk_inputs.get(key) is None)
    original_size = proposal.get("proposed_notional_usdt")
    fixed = risk_inputs.get("fixed_notional_usdt", "2000")
    result: dict[str, Any] = {
        "status": "BLOCKED" if missing else "NOT_EVALUATED",
        "reason_codes": ["ECONOMICS_INPUT_MISSING"] if missing else [],
        "missing_fields": sorted(set(missing)),
        "model_proposed_notional_usdt": original_size,
        "proposal_unchanged": True,
        "risk_policy_resized_proposal": False,
        "lifecycle": {"proposal": "OBSERVED", "gateway_acceptance": "NOT_OBSERVED",
                      "venue_fill": "NOT_OBSERVED", "complete_close": "NOT_OBSERVED"},
    }
    if missing:
        return result
    try:
        if Decimal(str(risk_inputs["min_net_rr"])) < Decimal("2.0"):
            result.update(status="REJECTED", reason_codes=["V35_NET_RR_FLOOR_MUST_REMAIN_2_0"])
            return result
        if Decimal(str(risk_inputs["risk_per_trade_pct"])) > Decimal("0.25"):
            result.update(status="REJECTED", reason_codes=["V35_STOP_RISK_CAP_MUST_NOT_EXCEED_0_25_PERCENT"])
            return result
    except (InvalidOperation, TypeError, ValueError):
        result.update(status="REJECTED", reason_codes=["RISK_POLICY_INVALID"])
        return result
    clean_mode = str(mode or "").strip().upper()
    if clean_mode == "FIXED_NOTIONAL":
        try:
            if Decimal(str(original_size)) != Decimal(str(fixed)):
                result.update(status="REJECTED", reason_codes=["FIXED_NOTIONAL_PROPOSAL_MISMATCH"])
                return result
        except (InvalidOperation, TypeError, ValueError):
            result.update(status="REJECTED", reason_codes=["FIXED_NOTIONAL_PROPOSAL_MISMATCH"])
            return result
    try:
        diagnostics = diagnose_trade_proposal(
            mode=clean_mode,
            side=proposal["side"],
            order_type=proposal["order_type"],
            proposed_notional_usdt=proposal["proposed_notional_usdt"],
            entry_price=proposal["entry_price"], stop_price=proposal["stop_price"],
            target_price=proposal["target_price"], requested_leverage=proposal["requested_leverage"],
            equity=risk_inputs["equity"], risk_per_trade_pct=risk_inputs["risk_per_trade_pct"],
            min_net_rr=risk_inputs["min_net_rr"], taker_fee_rate=risk_inputs["taker_fee_rate"],
            slippage_rate=risk_inputs["slippage_rate"], contract_size=risk_inputs["contract_size"],
            amount_step=risk_inputs["amount_step"], price_tick=risk_inputs["price_tick"],
            quote=risk_inputs.get("quote"), fixed_notional_usdt=fixed,
            available_margin=risk_inputs["available_margin"],
            max_margin_pct=risk_inputs.get("max_margin_pct"),
            min_amount=risk_inputs["min_amount"], max_amount=risk_inputs["max_amount"],
            min_notional=risk_inputs["min_notional"], max_notional=risk_inputs["max_notional"],
            max_leverage=risk_inputs["max_leverage"], quote_source=risk_inputs.get("quote_source"),
        )
    except (ArithmeticError, TypeError, ValueError) as exc:
        return {**result, "status": "REJECTED", "reason_codes": [getattr(exc, "code", "ECONOMICS_INVALID")],
                "error_type": type(exc).__name__}
    accepted = diagnostics.get("feasible") is True
    diagnostic_status = diagnostics.get("status")
    return {**result, "status": "ELIGIBLE_PROPOSAL" if accepted else (
                "REJECTED" if diagnostic_status == "INVALID" else diagnostic_status or "REJECTED"
            ),
            "economics_diagnostic_status": diagnostic_status,
            "reason_codes": list(diagnostics.get("reason_codes") or []),
            "economics": diagnostics}


def _a3_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        point = value
    elif isinstance(value, str):
        try:
            point = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if point.tzinfo is None or point.utcoffset() is None:
        return None
    return point.astimezone(UTC)


def _a3_stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _a3_no_candidate(reason_code: str, **details: Any) -> dict[str, Any]:
    return {
        "status": "NO_CANDIDATE",
        "action": "WAIT",
        "rule_id": A3_SIGNAL_RULE_ID,
        "signal_policy_sha256": A3_SIGNAL_POLICY_SHA256,
        "reason_code": reason_code,
        "production_authority": False,
        **details,
    }


def _failed_breakout_candidate(
    context: CausalContext, execution_quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signal_bars = context.bars.get("15m", ())
    entry_bars = context.bars.get("5m", ())
    events: list[dict[str, Any]] = []
    for index in range(1, len(entry_bars)):
        outside, reentry = entry_bars[index - 1], entry_bars[index]
        frozen_range = [
            bar for bar in signal_bars
            if bar.end < outside.start and bar.available_at < outside.start
        ]
        if len(frozen_range) < 20:
            continue
        frozen_range = frozen_range[-20:]
        upper, lower = max(bar.high for bar in frozen_range), min(bar.low for bar in frozen_range)
        if outside.close > upper and reentry.close <= upper and lower < reentry.close:
            events.append({"direction": "SHORT", "outside": outside, "reentry": reentry,
                           "level": upper, "target": lower, "frozen": frozen_range,
                           "reentry_index": index})
        elif outside.close < lower and reentry.close >= lower and reentry.close < upper:
            events.append({"direction": "LONG", "outside": outside, "reentry": reentry,
                           "level": lower, "target": upper, "frozen": frozen_range,
                           "reentry_index": index})
    if not events:
        return _a3_no_candidate("NO_CONFIRMED_FAILED_BREAKOUT")
    event = max(events, key=lambda item: item["reentry"].end)
    outside, reentry = event["outside"], event["reentry"]
    if event["direction"] == "SHORT":
        stop = max(outside.high, reentry.high)
        action = "OPEN_SHORT"
        side = "SHORT"
    else:
        stop = min(outside.low, reentry.low)
        action = "OPEN_LONG"
        side = "LONG"
    if not ((side == "SHORT" and event["target"] < reentry.close < stop)
            or (side == "LONG" and stop < reentry.close < event["target"])):
        return _a3_no_candidate("FAILED_BREAKOUT_GEOMETRY_INVALID")

    signal_available_at = reentry.available_at
    expires_at = signal_available_at + timedelta(seconds=A3_MAX_SIGNAL_AGE_SECONDS)
    signal_detail = {
        "policy_version": A3_SIGNAL_RULE_ID,
        "policy_sha256": A3_SIGNAL_POLICY_SHA256,
        "confirmed_at": _a3_stamp(reentry.end),
        "available_at": _a3_stamp(signal_available_at),
        "expires_at": _a3_stamp(expires_at),
        "maximum_age_seconds": A3_MAX_SIGNAL_AGE_SECONDS,
    }
    if context.decision_time >= expires_at:
        return _a3_no_candidate(
            "FAILED_BREAKOUT_SIGNAL_EXPIRED",
            signal=signal_detail,
        )

    post_confirmation_bars = entry_bars[event["reentry_index"] + 1:]
    invalidating_bar = None
    for bar in post_confirmation_bars:
        if side == "SHORT":
            invalid = bar.close > event["level"] or bar.high >= stop or bar.low <= event["target"]
        else:
            invalid = bar.close < event["level"] or bar.low <= stop or bar.high >= event["target"]
        if invalid:
            invalidating_bar = bar
            break
    if invalidating_bar is not None:
        return _a3_no_candidate(
            "FAILED_BREAKOUT_SIGNAL_INVALIDATED",
            signal=signal_detail,
            invalidated_at=_a3_stamp(invalidating_bar.end),
        )

    decision_time = context.decision_time.astimezone(UTC)
    quote_status = "CURRENT_QUOTE_REQUIRED"
    quote_reason = "A3_CURRENT_EXECUTION_QUOTE_REQUIRED"
    quote_price: float | None = None
    quote_observed_at: datetime | None = None
    quote_source: str | None = None
    quote_side = "best_bid" if side == "SHORT" else "best_ask"
    if isinstance(execution_quote, dict):
        quote_observed_at = _a3_timestamp(execution_quote.get("observed_at"))
        quote_source_value = execution_quote.get("source")
        quote_source = quote_source_value.strip() if isinstance(quote_source_value, str) else None
        try:
            bid = Decimal(str(execution_quote.get("best_bid")))
            ask = Decimal(str(execution_quote.get("best_ask")))
            if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0 or bid > ask:
                raise InvalidOperation
            quote_price = float(bid if side == "SHORT" else ask)
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            quote_price = None

        quote_time_valid = (
            quote_observed_at is not None
            and signal_available_at <= quote_observed_at < decision_time
            and (decision_time - quote_observed_at).total_seconds() <= A3_MAX_QUOTE_AGE_SECONDS
        )
        if not quote_source or quote_price is None or not quote_time_valid:
            quote_status = "QUOTE_REJECTED"
            quote_reason = "A3_EXECUTION_QUOTE_INVALID_STALE_OR_NONCAUSAL"
        else:
            quote_in_range = (
                event["target"] < quote_price < event["level"]
                if side == "SHORT"
                else event["level"] < quote_price < event["target"]
            )
            quote_before_protection = (
                quote_price < stop if side == "SHORT" else quote_price > stop
            )
            if not quote_in_range or not quote_before_protection:
                return _a3_no_candidate(
                    "FAILED_BREAKOUT_TRIGGER_NOT_ACTIVE_AT_CURRENT_QUOTE",
                    signal=signal_detail,
                    quote_observed_at=_a3_stamp(quote_observed_at),
                )
            quote_status = "POINT_IN_TIME_QUOTE_VALIDATED"
            quote_reason = ""

    range_refs = [item.ref for item in event["frozen"]]
    return {
        "status": "RESEARCH_CANDIDATE", "action": action, "side": side,
        "rule_id": A3_SIGNAL_RULE_ID, "signal_policy_sha256": A3_SIGNAL_POLICY_SHA256,
        "production_authority": False,
        "signal": {
            **signal_detail,
            "remaining_seconds_at_decision": int((expires_at - decision_time).total_seconds()),
            "valid_at_decision": True,
        },
        "trigger": {"condition": "5m close returned inside the frozen 20-bar 15m range",
                    "breakout_bar_at": outside.record()["bar_end"],
                    "confirmation_bar_at": reentry.record()["bar_end"],
                    "signal_available_at": _a3_stamp(signal_available_at),
                    "entry_after_confirmation": True},
        "execution": {
            "status": quote_status,
            "reason_code": quote_reason or None,
            "earliest_executable_at": _a3_stamp(signal_available_at),
            "quote_observed_at": _a3_stamp(quote_observed_at) if quote_observed_at else None,
            "quote_source": quote_source,
            "entry_quote_side": quote_side,
            "maximum_quote_age_seconds": A3_MAX_QUOTE_AGE_SECONDS,
            "entry_trigger": _A3_SIGNAL_POLICY["execution"]["entry_trigger"],
            "actual_fill_observed": False,
        },
        "proposal": {
            "entry_price": quote_price if quote_status == "POINT_IN_TIME_QUOTE_VALIDATED" else None,
            "entry_price_basis": f"POINT_IN_TIME_{quote_side.upper()}" if quote_price is not None and quote_status == "POINT_IN_TIME_QUOTE_VALIDATED" else "UNAVAILABLE",
            "historical_confirmation_close": reentry.close,
            "stop_price": stop,
            "target_price": event["target"],
            "target_structure": "opposite edge of the range frozen before the breakout"},
        "evidence_refs": [outside.ref, reentry.ref, *range_refs],
    }


def _response_record(response: Any, *, experiment_id: str, context: CausalContext,
                     state_sha: str | None, requested_model_id: str,
                     state_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response, dict):
        requested_id = requested_model_id.strip() if isinstance(requested_model_id, str) else ""
        return {
            "status": "INVALID_MODEL_RESPONSE",
            "error_code": "MODEL_RESPONSE_NOT_OBJECT",
            "model_id": None,
            "actual_model_id": None,
            "requested_model_id": requested_id,
            "model_identity_status": "UNVERIFIED",
            "analysis_schema_version": EXPERIMENTS[experiment_id]["analysis_schema_version"],
        }
    actual_model_id = response.get("model_id")
    identity_status = compare_model_identity(actual_model_id, requested_model_id)
    actual_id = actual_model_id.strip() if isinstance(actual_model_id, str) and actual_model_id.strip() else None
    requested_id = requested_model_id.strip() if isinstance(requested_model_id, str) else ""
    schema_version = EXPERIMENTS[experiment_id]["analysis_schema_version"]
    if response.get("error_code"):
        code = str(response["error_code"])
        status = "MODEL_TIMEOUT" if "TIMEOUT" in code.upper() else "MODEL_ERROR"
        return {"status": status, "error_code": code,
                "error_type": response.get("error_type"),
                "model_id": actual_id,
                "actual_model_id": actual_id,
                "requested_model_id": requested_id,
                "model_identity_status": identity_status,
                "analysis_schema_version": schema_version,
                "prompt_version": EXPERIMENTS[experiment_id]["prompt_version"],
                "state_sha256": state_sha}
    analysis = response.get("analysis")
    validation = validate_study_analysis(
        experiment_id, analysis, valid_evidence_refs=context.evidence_refs,
        declared_schema_version=schema_version, state_snapshot=state_snapshot,
    )
    try:
        json.dumps(analysis, ensure_ascii=False, allow_nan=False)
        serializable_analysis = analysis
    except (TypeError, ValueError):
        serializable_analysis = None
    raw_response = response.get("raw_model_response")
    identity_error = {
        "MATCHED": [],
        "MISMATCH": ["MODEL_ID_MISMATCH"],
        "UNVERIFIED": ["MODEL_ID_UNVERIFIED"],
    }[identity_status]
    validation_errors = sorted(set(validation["errors"] + identity_error))
    return {
        "status": ("MODEL_ID_MISMATCH" if identity_status == "MISMATCH" else
                   "MODEL_ID_UNVERIFIED" if identity_status == "UNVERIFIED" else
                   "COMPLETED" if validation["valid"] else "INVALID_MODEL_OUTPUT"),
        "analysis": serializable_analysis,
        "analysis_schema_version": schema_version,
        "analysis_validation": validation,
        "validation_errors": validation_errors,
        "model_id": actual_id,
        "actual_model_id": actual_id,
        "requested_model_id": requested_id,
        "model_identity_status": identity_status,
        "prompt_version": EXPERIMENTS[experiment_id]["prompt_version"],
        "raw_model_response": raw_response if isinstance(raw_response, str) else None,
        "raw_model_response_sha256": (
            hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
            if isinstance(raw_response, str) else None
        ),
        "token_usage": _safe_token_usage(response.get("token_usage")),
        "latency_ms": _safe_nonnegative_number(response.get("latency_ms")),
        "state_sha256": state_sha,
        "call_error": response.get("error_code"),
    }


def _safe_nonnegative_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number


def _safe_token_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    aliases = {
        "input": ("input", "prompt_tokens", "input_tokens"),
        "output": ("output", "completion_tokens", "output_tokens"),
    }
    result = {}
    for target, names in aliases.items():
        found = next((value[name] for name in names if name in value), None)
        numeric = _safe_nonnegative_number(found)
        if numeric is not None:
            result[target] = int(numeric)
    return result or None


def _proposal_risk_preflight(analysis: Any, point: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
    action = analysis.get("action") if isinstance(analysis, dict) else None
    if not isinstance(action, str) or action not in {
        "LONG", "SHORT", "OPEN_LONG", "OPEN_SHORT",
    }:
        is_management = isinstance(action, str) and action in {
            "REDUCE_POSITION", "CLOSE_POSITION", "TIGHTEN_STOP",
            "UPDATE_PROTECTION", "CANCEL_ORDER",
        }
        return None, {
            "proposal": "NOT_OBSERVED", "management_proposal": "OBSERVED" if is_management else "NOT_OBSERVED",
            "gateway_acceptance": "NOT_OBSERVED", "venue_fill": "NOT_OBSERVED",
            "complete_close": "NOT_OBSERVED",
        }
    rr = analysis.get("risk_reward") if isinstance(analysis.get("risk_reward"), dict) else {}
    side = "LONG" if action in {"LONG", "OPEN_LONG"} else "SHORT"
    proposal = {
        "side": side,
        "order_type": rr.get("order_type"),
        "proposed_notional_usdt": rr.get("proposed_notional_usdt"),
        "entry_price": rr.get("entry_price"),
        "stop_price": rr.get("stop_price"),
        "target_price": rr.get("target_price"),
        "requested_leverage": rr.get("requested_leverage"),
    }
    risk_inputs = point.get("risk_inputs")
    risk = diagnose_open_proposal(
        proposal, risk_inputs if isinstance(risk_inputs, dict) else {},
        mode=str(point.get("risk_mode") or "FIXED_NOTIONAL"),
    )
    return risk, {
        "proposal": "OBSERVED", "gateway_acceptance": "NOT_OBSERVED",
        "venue_fill": "NOT_OBSERVED", "complete_close": "NOT_OBSERVED",
    }


def run_study(decision_points: list[dict[str, Any]], *, cache_rows: list[dict[str, Any]] | None = None,
              model_caller: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
              model_id: str | None = None, run: bool = False,
              max_decisions: int | None = None) -> dict[str, Any]:
    """Execute cache-only A0, optionally injected A1/A2 calls, and offline A3."""
    validate_model_call_gate(run=run, max_decisions=max_decisions)
    if not isinstance(decision_points, list):
        raise StudyError("DECISION_POINTS_INVALID")
    if model_id is not None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise StudyError("MODEL_ID_INVALID")
        model_id = model_id.strip()
    if run and model_caller is None:
        raise StudyError("MODEL_CALLER_NOT_CONFIGURED")
    if run and (not isinstance(model_id, str) or not model_id.strip()):
        raise StudyError("MODEL_ID_REQUIRED_FOR_EXPLICIT_RUN")
    request_count = 0
    records: list[dict[str, Any]] = []
    for index, point in enumerate(decision_points):
        if not isinstance(point, dict):
            raise StudyError("DECISION_POINT_NOT_OBJECT")
        when = _decision_time(point.get("decision_time"))
        context = build_context(point.get("bars_by_timeframe"), when)
        state = safe_state_snapshot(point.get("state_snapshot"))
        state_sha = _sha(state) if state is not None else None
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "decision_id": str(point.get("decision_id") or f"decision-{index + 1}"),
            "decision_time": when,
            "data_as_of_by_timeframe": context.record()["data_as_of_by_timeframe"],
            "input_sha256": context.input_sha256,
            "state_sha256": state_sha,
            "validation_state_snapshot": validation_state_snapshot(state),
            "context": context.record(),
            "experiments": {},
            "lifecycle": {"proposal": "NOT_OBSERVED", "gateway_acceptance": "NOT_OBSERVED",
                          "venue_fill": "NOT_OBSERVED", "complete_close": "NOT_OBSERVED"},
            "outcome_joined": False,
        }
        for experiment_id in ("A0", "A1", "A2", "A3"):
            spec = EXPERIMENTS[experiment_id]
            if experiment_id == "A3":
                execution_quote = point.get("execution_quote")
                candidate = _failed_breakout_candidate(
                    context,
                    execution_quote if isinstance(execution_quote, dict) else None,
                )
                risk_result = None
                if candidate.get("status") == "RESEARCH_CANDIDATE":
                    policy = point.get("risk_inputs")
                    proposed_notional = point.get("proposed_notional_usdt")
                    if candidate.get("execution", {}).get("status") != "POINT_IN_TIME_QUOTE_VALIDATED":
                        risk_result = {
                            "status": "BLOCKED",
                            "reason_codes": [candidate.get("execution", {}).get("reason_code")
                                             or "A3_CURRENT_EXECUTION_QUOTE_REQUIRED"],
                            "proposal_unchanged": True,
                            "lifecycle": {"proposal": "OBSERVED", "gateway_acceptance": "NOT_OBSERVED",
                                          "venue_fill": "NOT_OBSERVED", "complete_close": "NOT_OBSERVED"},
                        }
                    elif isinstance(policy, dict) and proposed_notional is not None:
                        proposal = {**candidate["proposal"],
                                    "side": candidate["side"],
                                    "order_type": point.get("order_type", "market"),
                                    "proposed_notional_usdt": proposed_notional,
                                    "requested_leverage": point.get("requested_leverage", 1)}
                        risk_result = diagnose_open_proposal(proposal, policy, mode=point.get("risk_mode", "FIXED_NOTIONAL"))
                    else:
                        risk_result = {"status": "BLOCKED", "reason_codes": ["RISK_POLICY_OR_CONTRACT_SNAPSHOT_MISSING"],
                                       "proposal_unchanged": True,
                                       "lifecycle": {"proposal": "OBSERVED", "gateway_acceptance": "NOT_OBSERVED",
                                                     "venue_fill": "NOT_OBSERVED", "complete_close": "NOT_OBSERVED"}}
                record["experiments"][experiment_id] = {
                    "status": candidate["status"], "prompt_version": spec["prompt_version"],
                    "candidate": candidate, "risk_preflight": risk_result,
                    "lifecycle": {"proposal": "OBSERVED" if candidate.get("status") == "RESEARCH_CANDIDATE" else "NOT_OBSERVED",
                                  "gateway_acceptance": "NOT_OBSERVED", "venue_fill": "NOT_OBSERVED",
                                  "complete_close": "NOT_OBSERVED"},
                }
                continue
            if experiment_id == "A0":
                a0_point = {**point, "decision_time": when}
                expected = a0_cache_identity(a0_point)
                row = find_exact_cache(cache_rows, expected) if expected else None
                if row:
                    analysis = deepcopy(row.get("analysis"))
                    source = point.get("a0_original") if isinstance(point.get("a0_original"), dict) else {}
                    original_state = safe_state_snapshot(source.get("state_snapshot"))
                    original_refs = collect_evidence_refs(source.get("model_input"))
                    declared_schema = row.get("analysis_schema_version")
                    if declared_schema is None and isinstance(row.get("identity"), dict):
                        declared_schema = row["identity"].get("analysis_schema_version")
                    validation = validate_study_analysis(
                        "A0", analysis, valid_evidence_refs=original_refs,
                        declared_schema_version=declared_schema,
                        state_snapshot=original_state,
                    )
                    actual_model_id = row.get("actual_model_id")
                    requested_model_id = expected["model_id"]
                    identity_status = compare_model_identity(actual_model_id, requested_model_id)
                    if validation["valid"] and identity_status == "MATCHED":
                        risk, lifecycle = _proposal_risk_preflight(analysis, point)
                    else:
                        risk = None
                        lifecycle = {"proposal": "NOT_OBSERVED", "management_proposal": "NOT_OBSERVED",
                                     "gateway_acceptance": "NOT_OBSERVED", "venue_fill": "NOT_OBSERVED",
                                     "complete_close": "NOT_OBSERVED"}
                    cached = {
                        "status": "CACHE_MATCH", "prompt_version": spec["prompt_version"],
                        "analysis": analysis,
                        "analysis_schema_version": declared_schema,
                        "analysis_validation": validation,
                        "validation_evidence_refs": sorted(original_refs),
                        "validation_state_snapshot": validation_state_snapshot(original_state),
                        "model_id": actual_model_id,
                        "actual_model_id": actual_model_id,
                        "requested_model_id": requested_model_id,
                        "model_identity_status": identity_status,
                        "cached_provenance": {"identity": deepcopy(row.get("identity")),
                                              "analysis_present": isinstance(analysis, dict),
                                              "cached_response_status": row.get("status")},
                        "new_model_calls": 0, "lifecycle": lifecycle,
                    }
                    if risk is not None:
                        cached["risk_preflight"] = risk
                    record["experiments"][experiment_id] = cached
                else:
                    record["experiments"][experiment_id] = {
                        "status": "NOT_RUN_NO_EXACT_CACHE", "prompt_version": spec["prompt_version"],
                        "new_model_calls": 0,
                    }
                continue
            risk_inputs = point.get("risk_inputs")
            risk_mode = str(point.get("risk_mode") or "FIXED_NOTIONAL")
            payload = experiment_prompt(experiment_id, context, state, risk_inputs, risk_mode)
            identity = exact_cache_identity(
                experiment_id=experiment_id, model_id=model_id or "UNKNOWN",
                prompt_sha=prompt_sha256(
                    experiment_id, context, state, risk_inputs, risk_mode,
                ), data_sha=context.input_sha256,
                decision_time=when, state_sha=state_sha or "MISSING",
                prompt_version=spec["prompt_version"],
                analysis_schema_version=spec["analysis_schema_version"],
            )
            cached = find_exact_cache(cache_rows, identity)
            if cached is not None:
                analysis = deepcopy(cached.get("analysis"))
                declared_schema = cached.get("analysis_schema_version")
                if declared_schema is None and isinstance(cached.get("identity"), dict):
                    declared_schema = cached["identity"].get("analysis_schema_version")
                validation = validate_study_analysis(
                    experiment_id, analysis, valid_evidence_refs=context.evidence_refs,
                    declared_schema_version=declared_schema, state_snapshot=state,
                )
                actual_model_id = cached.get("actual_model_id")
                identity_status = compare_model_identity(actual_model_id, identity["model_id"])
                if validation["valid"] and identity_status == "MATCHED":
                    risk, lifecycle = _proposal_risk_preflight(analysis, point)
                else:
                    risk = None
                    lifecycle = {"proposal": "NOT_OBSERVED", "management_proposal": "NOT_OBSERVED",
                                 "gateway_acceptance": "NOT_OBSERVED", "venue_fill": "NOT_OBSERVED",
                                 "complete_close": "NOT_OBSERVED"}
                record["experiments"][experiment_id] = {
                    "status": "CACHE_MATCH", "prompt_version": spec["prompt_version"],
                    "analysis": analysis,
                    "analysis_schema_version": declared_schema,
                    "analysis_validation": validation,
                    "model_id": actual_model_id,
                    "actual_model_id": actual_model_id,
                    "requested_model_id": identity["model_id"],
                    "model_identity_status": identity_status,
                    "cached_provenance": {"identity": deepcopy(cached.get("identity")),
                                          "analysis_present": isinstance(analysis, dict),
                                          "cached_response_status": cached.get("status"),
                                          "raw_model_response_sha256": (
                                              hashlib.sha256(cached["raw_model_response"].encode("utf-8")).hexdigest()
                                              if isinstance(cached.get("raw_model_response"), str) else None
                                          )},
                    "new_model_calls": 0, "lifecycle": lifecycle,
                    **({"risk_preflight": risk} if risk is not None else {}),
                }
            elif context.status != "READY":
                record["experiments"][experiment_id] = {
                    "status": "NOT_RUN_INPUT_INCOMPLETE", "prompt_version": spec["prompt_version"],
                    "reason_code": "CAUSAL_FRAME_MISSING", "new_model_calls": 0,
                }
            elif state is None:
                record["experiments"][experiment_id] = {
                    "status": "NOT_RUN_STATE_SNAPSHOT_MISSING", "prompt_version": spec["prompt_version"],
                    "reason_code": "CURRENT_STATE_REQUIRED", "new_model_calls": 0,
                }
            elif not run:
                record["experiments"][experiment_id] = {
                    "status": "NOT_RUN_MODEL_CALLS_DISABLED", "prompt_version": spec["prompt_version"],
                    "cache_identity": identity, "new_model_calls": 0,
                }
            elif request_count >= int(max_decisions or 0):
                record["experiments"][experiment_id] = {
                    "status": "NOT_RUN_BUDGET_EXHAUSTED", "prompt_version": spec["prompt_version"],
                    "cache_identity": identity, "new_model_calls": 0,
                }
            else:
                request_count += 1
                try:
                    response = model_caller(experiment_id, payload)  # explicit dependency injection only
                except TimeoutError:
                    response = {"error_code": "MODEL_TIMEOUT"}
                except Exception as exc:  # noqa: BLE001 - provider errors become scrubbed records
                    response = {"error_code": "MODEL_CALL_ERROR", "error_type": type(exc).__name__}
                model_record = _response_record(response, experiment_id=experiment_id,
                                                context=context, state_sha=state_sha,
                                                requested_model_id=str(model_id),
                                                state_snapshot=state)
                if model_record.get("status") == "COMPLETED":
                    risk, lifecycle = _proposal_risk_preflight(model_record.get("analysis"), point)
                else:
                    risk = None
                    lifecycle = {"proposal": "NOT_OBSERVED", "management_proposal": "NOT_OBSERVED",
                                 "gateway_acceptance": "NOT_OBSERVED", "venue_fill": "NOT_OBSERVED",
                                 "complete_close": "NOT_OBSERVED"}
                model_record["lifecycle"] = lifecycle
                if risk is not None:
                    model_record["risk_preflight"] = risk
                cache_row = {"identity": identity, **deepcopy(model_record)}
                record["experiments"][experiment_id] = {
                    **model_record, "cache_identity": identity,
                    "new_model_calls": 1, "cache_record": cache_row,
                }
        records.append(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_manifest": {"mode": "EXPLICIT_MODEL_RUN" if run else "OFFLINE_DEFAULT",
                         "model_id": model_id,
                         "model_id_role": "MODEL_CALL_REQUEST" if run else "CACHE_MATCH_METADATA",
                         "model_call_budget": max_decisions if run else 0,
                         "model_calls_used": request_count, "experiment_ids": list(EXPERIMENTS),
                         "outcomes_included": False},
        "decision_records": records,
    }
