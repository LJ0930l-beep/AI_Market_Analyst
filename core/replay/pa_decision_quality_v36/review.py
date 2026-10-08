"""Read-only outcome joins, evidence-backed attribution and lifecycle metrics."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from .schema import ERROR_CATEGORIES

EXPERIMENT_IDS = ("A0", "A1", "A2", "A3")


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _drawdown(pnls: list[float]) -> float | None:
    if not pnls:
        return None
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for value in pnls:
        cumulative += value
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return round(abs(worst), 8)


def attribute_error(label: str, evidence_refs: list[str], valid_refs: set[str]) -> dict[str, Any]:
    clean = str(label or "UNDETERMINED").upper()
    refs = [item for item in evidence_refs if isinstance(item, str)] if isinstance(evidence_refs, list) else []
    if clean not in ERROR_CATEGORIES:
        return {"category": "UNDETERMINED", "evidence_refs": [], "status": "INVALID_CATEGORY"}
    if clean != "UNDETERMINED" and (not refs or any(ref not in valid_refs for ref in refs)):
        return {"category": "UNDETERMINED", "evidence_refs": [], "status": "EVIDENCE_REQUIRED"}
    return {"category": clean, "evidence_refs": sorted(set(refs)),
            "status": "SUPPORTED" if refs else "UNDETERMINED"}


def evaluate_wait(decision: dict[str, Any], assessment: dict[str, Any],
                  valid_refs: set[str]) -> dict[str, Any]:
    allowed = {"REASONABLE_WAIT", "MISSED_CANDIDATE", "INDETERMINATE"}
    value = str(assessment.get("assessment") or "INDETERMINATE").upper()
    refs = assessment.get("evidence_refs") if isinstance(assessment.get("evidence_refs"), list) else []
    action = str(decision.get("action") or "").upper()
    if action not in {"WAIT", "HOLD"} or value not in allowed:
        return {"assessment": "INDETERMINATE", "status": "NOT_A_WAIT_OR_INVALID",
                "evidence_refs": []}
    if value != "INDETERMINATE" and (
        not refs or any(not isinstance(ref, str) or ref not in valid_refs for ref in refs)
    ):
        return {"assessment": "INDETERMINATE", "status": "EVIDENCE_REQUIRED",
                "evidence_refs": []}
    return {"assessment": value, "status": "SUPPORTED" if refs else "INDETERMINATE",
            "evidence_refs": sorted(set(refs)), "future_outcome_alone_is_not_an_error": True}


def _experiment_key(row: dict[str, Any]) -> tuple[str, str] | None:
    experiment_id = row.get("experiment_id")
    decision_id = row.get("decision_id")
    if (experiment_id not in EXPERIMENT_IDS or decision_id is None
            or not str(decision_id).strip()):
        return None
    return str(experiment_id), str(decision_id)


def _refs_for(record: dict[str, Any]) -> set[str]:
    context = record.get("context")
    frames = context.get("frames") if isinstance(context, dict) else None
    if not isinstance(frames, dict):
        return set()
    return {
        str(ref)
        for frame in frames.values() if isinstance(frame, dict)
        for ref in (frame.get("evidence_refs") if isinstance(frame.get("evidence_refs"), list) else [])
        if isinstance(ref, str)
    }


def summarize(records: list[dict[str, Any]], outcomes: list[dict[str, Any]] | None = None,
              reviews: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Summarize exact experiment/decision joins without mutating any inputs.

    Outcome and review rows must carry an explicit experiment_id. Decision IDs
    are often shared across arms, so an unqualified row is never copied to A1/A2/A3.
    """
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("DECISION_RECORDS_INVALID")
    decision_ids = [str(row.get("decision_id") or "") for row in records]
    if any(not item for item in decision_ids) or len(decision_ids) != len(set(decision_ids)):
        raise ValueError("DECISION_ID_MISSING_OR_DUPLICATED")
    for rows, code in ((outcomes, "OUTCOME_RECORDS_INVALID"), (reviews, "REVIEW_RECORDS_INVALID")):
        if rows is not None and (not isinstance(rows, list)
                                 or any(not isinstance(row, dict) for row in rows)):
            raise ValueError(code)

    outcomes_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    complete_outcome_keys: set[tuple[str, str]] = set()
    for row in outcomes or []:
        key = _experiment_key(row)
        if key is not None:
            if row.get("complete_close") is True:
                if key in complete_outcome_keys:
                    raise ValueError("DUPLICATE_COMPLETE_OUTCOME_KEY")
                complete_outcome_keys.add(key)
            outcomes_by_key[key].append(row)
    reviews_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in reviews or []:
        key = _experiment_key(row)
        if key is not None:
            if key in reviews_by_key:
                raise ValueError("DUPLICATE_REVIEW_KEY")
            reviews_by_key[key] = row

    experiment_metrics: dict[str, Any] = {}
    joined_outcome_count = 0

    for experiment_id in EXPERIMENT_IDS:
        counts = Counter()
        raw_actions = Counter()
        closed_rows: list[tuple[datetime, float, dict[str, Any]]] = []
        returns_r: list[float] = []
        wins_r: list[float] = []
        losses_r: list[float] = []
        fees: list[float] = []
        slippage: list[float] = []
        latencies: list[float] = []
        input_tokens: list[float] = []
        output_tokens: list[float] = []
        market_groups: dict[str, Counter] = defaultdict(Counter)
        attribution = Counter()
        wait_review = Counter()
        eligible_outcome_count = 0

        for record in records:
            decision_id = str(record.get("decision_id") or "")
            experiment_rows = record.get("experiments")
            item = (experiment_rows.get(experiment_id) if isinstance(experiment_rows, dict) else None) or {}
            if not isinstance(item, dict):
                item = {}
            counts["scans"] += 1
            counts[f"status:{item.get('status', 'MISSING')}"] += 1
            analysis = item.get("analysis")
            if isinstance(analysis, dict):
                action = str(analysis.get("action") or "INVALID").upper()
                raw_actions[action] += 1
                counts["valid_model_decisions"] += item.get("status") == "COMPLETED"
                context = analysis.get("context")
                market = context.get("market_regime") if isinstance(context, dict) else None
                market_groups[str(market or "UNKNOWN")][action] += 1
                counts["open_proposals"] += action in {"LONG", "SHORT", "OPEN_LONG", "OPEN_SHORT"}
                counts["wait_decisions"] += action == "WAIT"
                counts["hold_decisions"] += action == "HOLD"
            candidate = item.get("candidate")
            counts["research_candidates"] += (
                experiment_id == "A3" and isinstance(candidate, dict)
                and candidate.get("status") == "RESEARCH_CANDIDATE"
            )
            preflight = item.get("risk_preflight")
            if isinstance(preflight, dict):
                counts["risk_rejected_proposals"] += preflight.get("status") == "REJECTED"
                counts["risk_blocked_proposals"] += preflight.get("status") == "BLOCKED"
            lifecycle = item.get("lifecycle")
            if isinstance(lifecycle, dict):
                counts["management_proposals"] += lifecycle.get("management_proposal") == "OBSERVED"
                counts["gateway_accepted_orders"] += lifecycle.get("gateway_acceptance") == "ACCEPTED"
                counts["venue_fills"] += lifecycle.get("venue_fill") == "FILLED"
                counts["complete_closes_observed"] += lifecycle.get("complete_close") == "CLOSED"
            if item.get("status") in {"MODEL_TIMEOUT", "TIMEOUT"}:
                counts["model_timeouts"] += 1
            latency = _finite(item.get("latency_ms"))
            if latency is not None and latency >= 0:
                latencies.append(latency)
            usage = item.get("token_usage")
            if isinstance(usage, dict):
                in_tokens = _finite(usage.get("input", usage.get("prompt_tokens")))
                out_tokens = _finite(usage.get("output", usage.get("completion_tokens")))
                if in_tokens is not None and in_tokens >= 0:
                    input_tokens.append(in_tokens)
                if out_tokens is not None and out_tokens >= 0:
                    output_tokens.append(out_tokens)

            key = (experiment_id, decision_id)
            review = reviews_by_key.get(key, {})
            valid_refs = _refs_for(record)
            error_rows = review.get("error_attributions", [])
            if isinstance(error_rows, list):
                for label in error_rows:
                    if isinstance(label, dict):
                        result = attribute_error(label.get("category"), label.get("evidence_refs", []), valid_refs)
                        attribution[result["category"]] += 1
            if isinstance(analysis, dict) and analysis.get("action") in {"WAIT", "HOLD"}:
                assessment = review.get("wait_assessment", {})
                result = evaluate_wait(analysis, assessment if isinstance(assessment, dict) else {}, valid_refs)
                wait_review[result["assessment"]] += 1

            decision_time = _utc(record.get("decision_time"))
            for outcome in outcomes_by_key.get(key, []):
                closed_at = _utc(outcome.get("closed_at"))
                if (outcome.get("complete_close") is not True or decision_time is None
                        or closed_at is None or closed_at <= decision_time):
                    continue
                net = _finite(outcome.get("net_pnl_usdt"))
                if net is None:
                    continue
                closed_rows.append((closed_at, net, outcome))
                eligible_outcome_count += 1
                r_value = _finite(outcome.get("net_r"))
                if r_value is None:
                    risk = _finite(outcome.get("initial_stop_risk_usdt"))
                    r_value = net / risk if risk and risk > 0 else None
                if r_value is not None:
                    returns_r.append(r_value)
                    if net > 0:
                        wins_r.append(r_value)
                    elif net < 0:
                        losses_r.append(r_value)
                fee = _finite(outcome.get("fees_usdt"))
                slip = _finite(outcome.get("slippage_usdt"))
                if fee is not None:
                    fees.append(fee)
                if slip is not None:
                    slippage.append(slip)

        joined_outcome_count += eligible_outcome_count
        ordered_rows = sorted(closed_rows, key=lambda row: row[0])
        ordered_pnls = [value for _, value, _ in ordered_rows]
        positives = sum(value for value in ordered_pnls if value > 0)
        negatives = abs(sum(value for value in ordered_pnls if value < 0))

        def average(values: list[float]) -> float | None:
            return round(sum(values) / len(values), 8) if values else None

        experiment_metrics[experiment_id] = {
            "scan_count": counts["scans"],
            "status_counts": {
                key.removeprefix("status:"): value
                for key, value in counts.items() if key.startswith("status:")
            },
            "valid_model_decisions": counts["valid_model_decisions"],
            "raw_action_counts": dict(sorted(raw_actions.items())),
            "open_proposals": counts["open_proposals"],
            "research_candidates": counts["research_candidates"],
            "management_proposals": counts["management_proposals"],
            "risk_rejected_proposals": counts["risk_rejected_proposals"],
            "risk_blocked_proposals": counts["risk_blocked_proposals"],
            "gateway_accepted_orders": counts["gateway_accepted_orders"],
            "venue_fills": counts["venue_fills"],
            "complete_closes_observed": counts["complete_closes_observed"],
            "model_timeouts": counts["model_timeouts"],
            "model_latency_ms": {"sample_count": len(latencies), "average": average(latencies)},
            "token_usage": {
                "input_sample_count": len(input_tokens), "input_total": sum(input_tokens) if input_tokens else None,
                "output_sample_count": len(output_tokens), "output_total": sum(output_tokens) if output_tokens else None,
            },
            "model_cost_usdt": None,
            "model_cost_status": "UNKNOWN_NO_VERIFIED_PRICE_SOURCE",
            "closed_trade_metrics": {
                "sample_count": len(ordered_pnls),
                "win_count": sum(value > 0 for value in ordered_pnls),
                "loss_count": sum(value < 0 for value in ordered_pnls),
                "win_rate": round(sum(value > 0 for value in ordered_pnls) / len(ordered_pnls), 8)
                if ordered_pnls else None,
                "average_net_r": average(returns_r),
                "average_win_r": average(wins_r),
                "average_loss_r": average(losses_r),
                "net_pnl_usdt": round(sum(ordered_pnls), 8) if ordered_pnls else None,
                "profit_factor": round(positives / negatives, 8) if negatives else (
                    None if not positives else "UNDEFINED_NO_LOSSES"
                ),
                "max_drawdown_usdt_from_cumulative_net_pnl": _drawdown(ordered_pnls),
                "max_drawdown_pct": None,
                "fees_usdt": round(sum(fees), 8) if fees else None,
                "slippage_usdt": round(sum(slippage), 8) if slippage else None,
            },
            "actions_by_market_regime": {
                key: dict(value) for key, value in sorted(market_groups.items())
            },
            "error_attribution_counts": dict(sorted(attribution.items())),
            "wait_assessment_counts": dict(sorted(wait_review.items())),
            "joined_complete_outcomes": eligible_outcome_count,
        }

    return {
        "schema_version": "pa-decision-quality-v36/review-2",
        "decision_count": len(records),
        "outcome_join_count": joined_outcome_count,
        "outcome_join_key": ["experiment_id", "decision_id"],
        "unqualified_outcomes_joined": False,
        "outcomes_joined_only_in_review": True,
        "decision_records_mutated": False,
        "experiments": experiment_metrics,
    }
