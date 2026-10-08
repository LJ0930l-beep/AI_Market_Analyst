"""Read-only, causal qualification of financially settled Gate strategy episodes.

Money comes exclusively from M1's immutable full-cost settlement.  Autonomous
performance is a stricter subset: the entry must be a scoped verified model
decision and every actual close fill must have a native AI-reduce or triggered
Gate-protection actor chain.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from typing import Any, Mapping

from ..trading.ai_strategy_book import STRATEGY_PROFILE_VERSION, TEMPLATES
from ..trading.gate_exit_attribution import (
    canonical_environment,
    canonical_symbol,
    classify_episode_exit_actor,
    frozen_template_config_digests,
    strategy_config_sha256,
    verify_model_decision_cycle,
)


MIN_CLOSED_TRADES = 100
MIN_OBSERVATION_DAYS = 30
_KNOWN_TEMPLATES = {str(template["id"]) for template in TEMPLATES}
_REQUIRED_ENVIRONMENTS = {"live", "testnet"}


def frozen_strategy_manifest() -> dict[str, Any]:
    entries = []
    legacy_entries = []
    config_digests = frozen_template_config_digests()
    for template in TEMPLATES:
        frozen = {
            "id": template["id"], "name": template["name"],
            "style": template["style"],
            "scan_interval_minutes": template["scan_interval_minutes"],
            "profile": template["profile"],
            "sections": template["sections"],
            "execution_defaults": template["execution_defaults"],
        }
        digest = hashlib.sha256(json.dumps(
            frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        legacy = {
            "id": template["id"], "name": template["name"],
            "style": template["style"],
            "scan_interval_minutes": template["scan_interval_minutes"],
            "profile_version": str(template["profile"].get("version") or STRATEGY_PROFILE_VERSION), "sha256": digest,
        }
        legacy_entries.append(legacy)
        entries.append({**legacy, "config_sha256": config_digests.get(str(template["id"]))})
    return {
        "schema_version": "ai_strategy_validation_v2",
        "strategies": entries,
        # Preserve the original pack digest basis; config_sha256 is a derived
        # comparison key, not a mutation of the prompt/template pack.
        "pack_sha256": hashlib.sha256(json.dumps(
            legacy_entries, ensure_ascii=False, sort_keys=True
        ).encode()).hexdigest(),
    }


def _finite_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _as_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _table_exists(db: Any, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _env_from_account_rows(db: Any, account_id: str, requested: str | None) -> tuple[str | None, str | None]:
    if requested:
        env = canonical_environment(requested)
        return (env, None) if env in _REQUIRED_ENVIRONMENTS else (None, "GATE_ENVIRONMENT_INVALID")
    if not _table_exists(db, "gate_accounting_episodes"):
        return None, None
    raw = db.execute(
        "SELECT DISTINCT lower(environment) FROM gate_accounting_episodes WHERE account_id=?",
        (account_id,),
    ).fetchall()
    values = {canonical_environment(row[0]) for row in raw if row[0]}
    values &= _REQUIRED_ENVIRONMENTS
    if len(values) > 1:
        return None, "ACCOUNT_ENVIRONMENT_SCOPE_REQUIRED"
    return (next(iter(values)), None) if values else (None, None)


def _latest_episode_rows(db: Any, account_id: str, environment: str) -> list[dict[str, Any]]:
    if not all(_table_exists(db, name) for name in ("gate_accounting_episodes", "gate_episode_settlements")):
        return []
    rows = db.execute(
        """SELECT e.episode_id,e.account_id,e.environment,e.contract,e.canonical_symbol,
                  e.entry_order_id,e.entry_intent_id,e.cycle_id,e.side,e.strategy_id,
                  e.strategy_version,e.identity_json,e.created_at,
                  s.settlement_id,s.status AS settlement_status,s.settlement_currency,
                  s.gross_realized,s.fee_effect,s.funding_effect,s.dividend_effect,
                  s.total_pnl,s.fee_status AS row_fee_status,
                  s.funding_status AS row_funding_status,s.pnl_source,
                  s.settlement_json,s.settled_at
           FROM gate_accounting_episodes e
           JOIN gate_episode_settlements s ON s.episode_id=e.episode_id
           WHERE e.account_id=? AND lower(e.environment)=?
           ORDER BY e.episode_id,s.settled_at DESC,s.settlement_id DESC""",
        (account_id, environment),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _row_dict(row)
        latest.setdefault(str(item.get("episode_id") or ""), item)
    return list(latest.values())


def _validate_full_cost_settlement(
    db: Any,
    row: Mapping[str, Any],
    *,
    account_id: str,
    environment: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if str(row.get("settlement_status") or "").upper() != "SETTLED_FULL_COST":
        return None, "LATEST_SETTLEMENT_NOT_FULL_COST"
    settlement = _json_object(row.get("settlement_json"))
    if settlement is None:
        return None, "SETTLEMENT_JSON_INVALID"
    episode_id = str(row.get("episode_id") or "")
    entry_order_id = str(row.get("entry_order_id") or "")
    entry_intent_id = str(row.get("entry_intent_id") or "")
    contract = str(row.get("contract") or "")
    symbol = canonical_symbol(row.get("canonical_symbol") or contract)
    side_db = str(row.get("side") or "").upper()
    side = {"BUY": "LONG", "LONG": "LONG", "SELL": "SHORT", "SHORT": "SHORT"}.get(side_db)
    if not episode_id or not entry_order_id or not entry_intent_id or not symbol or side is None:
        return None, "EPISODE_IDENTITY_INCOMPLETE"
    if (
        str(settlement.get("status") or "").upper() != "SETTLED_FULL_COST"
        or str(settlement.get("accounting_episode_id") or "") != episode_id
        or str(settlement.get("account_id") or "") != account_id
        or canonical_environment(settlement.get("environment")) != environment
        or str(settlement.get("entry_order_id") or "") != entry_order_id
        or str(settlement.get("entry_intent_id") or "") != entry_intent_id
        or canonical_symbol(settlement.get("contract")) != symbol
        or str(settlement.get("side") or "").upper() != side
    ):
        return None, "SETTLEMENT_EPISODE_SCOPE_OR_IDENTITY_MISMATCH"
    if str(settlement.get("settlement_currency") or "").upper() != "USDT":
        return None, "SETTLEMENT_CURRENCY_UNVERIFIED"
    if (
        str(settlement.get("trade_history_coverage") or "").upper() != "COMPLETE"
        or str(settlement.get("position_close_history_coverage") or "").upper() != "COMPLETE"
        or not str(settlement.get("fee_status") or "").upper().startswith("VERIFIED_")
        or not str(settlement.get("funding_status") or "").upper().startswith("VERIFIED_")
        or not str(row.get("row_fee_status") or "").upper().startswith("VERIFIED")
        or not str(row.get("row_funding_status") or "").upper().startswith("VERIFIED")
    ):
        return None, "SETTLEMENT_COST_OR_COVERAGE_UNVERIFIED"
    total = _finite_decimal(settlement.get("total_pnl"))
    if total is None or _finite_decimal(row.get("total_pnl")) != total:
        return None, "SETTLEMENT_TOTAL_PNL_INVALID_OR_CONFLICTING"
    for field in ("gross_realized", "fee_effect", "funding_effect", "dividend_effect"):
        value = _finite_decimal(settlement.get(field))
        if value is None or _finite_decimal(row.get(field)) != value:
            return None, f"SETTLEMENT_{field.upper()}_INVALID_OR_CONFLICTING"
    for field in ("entry_trade_ids", "exit_trade_ids"):
        ids = settlement.get(field)
        if not isinstance(ids, list) or not ids or any(not str(value or "").strip() for value in ids):
            return None, f"SETTLEMENT_{field.upper()}_MISSING"
        if len({str(value) for value in ids}) != len(ids):
            return None, f"SETTLEMENT_{field.upper()}_DUPLICATE"
    if not _table_exists(db, "gate_remote_trade_evidence"):
        return None, "NATIVE_SETTLEMENT_TRADE_EVIDENCE_MISSING"
    if not _table_exists(db, "gate_trade_episode_attributions"):
        return None, "M1_EPISODE_ATTRIBUTION_MISSING"

    expected_entry_side = "BUY" if side == "LONG" else "SELL"
    expected_exit_side = "SELL" if side == "LONG" else "BUY"
    entry_ids = {str(value) for value in settlement["entry_trade_ids"]}
    exit_ids = {str(value) for value in settlement["exit_trade_ids"]}
    for trade_id in settlement["entry_trade_ids"]:
        rows = db.execute(
            """SELECT * FROM gate_remote_trade_evidence
               WHERE account_id=? AND lower(environment)=? AND trade_id=?""",
            (account_id, environment, str(trade_id)),
        ).fetchall()
        if len(rows) != 1:
            return None, "NATIVE_ENTRY_FILL_MISSING_OR_AMBIGUOUS"
        trade = _row_dict(rows[0])
        if (
            str(trade.get("venue") or "gate").lower() != "gate"
            or canonical_symbol(trade.get("canonical_symbol") or trade.get("contract")) != symbol
            or str(trade.get("order_id") or "") != entry_order_id
            or str(trade.get("side") or "").upper() != expected_entry_side
        ):
            return None, "NATIVE_ENTRY_FILL_SCOPE_OR_ID_MISMATCH"
        attribution = db.execute(
            """SELECT episode_id,economic_role,attribution_status
               FROM gate_trade_episode_attributions
               WHERE account_id=? AND lower(environment)=? AND trade_id=?""",
            (account_id, environment, str(trade_id)),
        ).fetchall()
        if not any(
            str(item["episode_id"] or "") == episode_id
            and str(item["economic_role"] or "").upper() == "ENTRY"
            and str(item["attribution_status"] or "").upper() == "VERIFIED"
            for item in attribution
        ):
            return None, "M1_ENTRY_EPISODE_ATTRIBUTION_UNVERIFIED"
    for trade_id in settlement["exit_trade_ids"]:
        rows = db.execute(
            """SELECT * FROM gate_remote_trade_evidence
               WHERE account_id=? AND lower(environment)=? AND trade_id=?""",
            (account_id, environment, str(trade_id)),
        ).fetchall()
        if len(rows) != 1:
            return None, "NATIVE_EXIT_FILL_MISSING_OR_AMBIGUOUS"
        trade = _row_dict(rows[0])
        if (
            str(trade.get("venue") or "gate").lower() != "gate"
            or canonical_symbol(trade.get("canonical_symbol") or trade.get("contract")) != symbol
            or str(trade.get("side") or "").upper() != expected_exit_side
            or not str(trade.get("order_id") or "").strip()
        ):
            return None, "NATIVE_EXIT_FILL_SCOPE_OR_DIRECTION_MISMATCH"
        attribution = db.execute(
            """SELECT episode_id,economic_role,attribution_status
               FROM gate_trade_episode_attributions
               WHERE account_id=? AND lower(environment)=? AND trade_id=?""",
            (account_id, environment, str(trade_id)),
        ).fetchall()
        if not any(
            str(item["episode_id"] or "") == episode_id
            and str(item["economic_role"] or "").upper() == "CLOSE"
            and str(item["attribution_status"] or "").upper() == "VERIFIED"
            for item in attribution
        ):
            return None, "M1_EXIT_EPISODE_ATTRIBUTION_UNVERIFIED"
        if any(
            str(item["episode_id"] or "") != episode_id
            and str(item["economic_role"] or "").upper() == "CLOSE"
            and str(item["attribution_status"] or "").upper() == "VERIFIED"
            for item in attribution
        ):
            return None, "M1_EXIT_EPISODE_ATTRIBUTION_AMBIGUOUS"
    verified_entry_ids = {
        str(item[0]) for item in db.execute(
            """SELECT DISTINCT trade_id FROM gate_trade_episode_attributions
               WHERE account_id=? AND lower(environment)=? AND episode_id=?
                 AND upper(economic_role)='ENTRY' AND upper(attribution_status)='VERIFIED'""",
            (account_id, environment, episode_id),
        ).fetchall()
    }
    verified_exit_ids = {
        str(item[0]) for item in db.execute(
            """SELECT DISTINCT trade_id FROM gate_trade_episode_attributions
               WHERE account_id=? AND lower(environment)=? AND episode_id=?
                 AND upper(economic_role)='CLOSE' AND upper(attribution_status)='VERIFIED'""",
            (account_id, environment, episode_id),
        ).fetchall()
    }
    if verified_entry_ids != entry_ids or verified_exit_ids != exit_ids:
        return None, "M1_SETTLEMENT_FILL_SET_MISMATCH"
    if _table_exists(db, "gate_position_close_evidence"):
        evidence_id = str(settlement.get("position_close_evidence_id") or "")
        match = db.execute(
            """SELECT * FROM gate_position_close_evidence
               WHERE account_id=? AND lower(environment)=? AND evidence_id=?
                 AND canonical_symbol=?""",
            (account_id, environment, evidence_id, symbol),
        ).fetchone()
        if not evidence_id or match is None:
            return None, "NATIVE_POSITION_CLOSE_EVIDENCE_MISSING"
        close_row = _row_dict(match)
        close_components = {
            "gross_realized": "pnl_pnl", "fee_effect": "pnl_fee",
            "funding_effect": "pnl_fund", "dividend_effect": "pnl_dividend",
            "total_pnl": "pnl",
        }
        if (
            canonical_symbol(close_row.get("contract")) != canonical_symbol(contract)
            or str(close_row.get("side") or "").upper() != side
            or any(
                _finite_decimal(settlement.get(field)) != _finite_decimal(close_row.get(native_field))
                for field, native_field in close_components.items()
            )
        ):
            return None, "NATIVE_POSITION_CLOSE_COMPONENTS_MISMATCH"
        if settlement.get("closed_at_ms") is not None and str(settlement.get("closed_at_ms")) != str(close_row.get("closed_at_ms")):
            return None, "NATIVE_POSITION_CLOSE_TIME_MISMATCH"
    return {
        "episode_id": episode_id,
        "account_id": account_id,
        "environment": environment,
        "contract": contract,
        "symbol": symbol,
        "entry_order_id": entry_order_id,
        "entry_intent_id": entry_intent_id,
        "cycle_id": str(row.get("cycle_id") or ""),
        "strategy_id": str(row.get("strategy_id") or ""),
        "strategy_version": str(row.get("strategy_version") or ""),
        "side": side,
        "settlement": settlement,
        "total_pnl": total,
        "closed_at_ms": settlement.get("closed_at_ms"),
        "settled_at": str(row.get("settled_at") or ""),
    }, None


def _native_order_id(receipt: Mapping[str, Any]) -> str:
    evidence = _json_object(receipt.get("execution_evidence")) or {}
    return str(
        receipt.get("order_id") or receipt.get("id") or evidence.get("remote_order_id") or ""
    ).strip()


def _entry_provenance(
    db: Any,
    episode: Mapping[str, Any],
    *,
    account_id: str,
    environment: str,
) -> tuple[dict[str, Any] | None, str | None]:
    intent_id = str(episode["entry_intent_id"])
    cycle_id = str(episode["cycle_id"] or "")
    if not cycle_id:
        return None, "ENTRY_ORIGINAL_CYCLE_MISSING"
    intents = db.execute(
        "SELECT * FROM order_intents WHERE account_id=? AND intent_id=?",
        (account_id, intent_id),
    ).fetchall() if _table_exists(db, "order_intents") else []
    if len(intents) != 1:
        return None, "ENTRY_INTENT_MISSING_OR_AMBIGUOUS"
    intent = _row_dict(intents[0])
    expected_side = "BUY" if episode["side"] == "LONG" else "SELL"
    receipt = _json_object(intent.get("execution_result_json")) or {}
    if (
        str(intent.get("account_id") or "") != account_id
        or canonical_environment(intent.get("environment") or intent.get("mode")) != environment
        or str(intent.get("venue") or "").lower() != "gate"
        or canonical_symbol(intent.get("instrument_id")) != episode["symbol"]
        or str(intent.get("side") or "").upper() not in {expected_side, episode["side"]}
        or bool(intent.get("reduce_only"))
        or str(intent.get("decision_path") or "").upper() != "AI_LED"
        or str(intent.get("control_mode") or "").upper() != "AUTONOMOUS"
        or str(intent.get("cycle_id") or "") != cycle_id
        or _native_order_id(receipt) != str(episode["entry_order_id"])
    ):
        return None, "ENTRY_INTENT_NOT_SCOPED_AUTONOMOUS_AI_OPEN"
    if str(intent.get("strategy_id") or "") != str(episode.get("strategy_id") or ""):
        return None, "ENTRY_INTENT_STRATEGY_ID_MISMATCH"
    if str(intent.get("strategy_version") or "") != str(episode.get("strategy_version") or ""):
        return None, "ENTRY_INTENT_STRATEGY_VERSION_MISMATCH"
    cycle_rows = db.execute(
        "SELECT * FROM ai_led_cycles WHERE account_id=? AND cycle_id=?",
        (account_id, cycle_id),
    ).fetchall() if _table_exists(db, "ai_led_cycles") else []
    if len(cycle_rows) != 1:
        return None, "ENTRY_ORIGINAL_CYCLE_MISSING_OR_AMBIGUOUS"
    expected_action = "OPEN_LONG" if episode["side"] == "LONG" else "OPEN_SHORT"
    cycle, reason = verify_model_decision_cycle(
        _row_dict(cycle_rows[0]), account_id=account_id,
        environment=environment, symbol=episode["symbol"],
        expected_actions={expected_action}, order_intent_id=intent_id,
        expected_template_id=str(intent.get("strategy_id") or "") if intent.get("strategy_id") in _KNOWN_TEMPLATES else None,
        expected_reduce_only=False,
    )
    if cycle is None:
        return None, reason or "ENTRY_MODEL_PROVENANCE_INVALID"
    if (
        cycle["cycle_id"] != cycle_id
        or cycle["intent_strategy_version"] != str(intent.get("strategy_version") or "")
    ):
        return None, "ENTRY_CYCLE_INTENT_PROVENANCE_MISMATCH"
    template_id = cycle["template_id"]
    if template_id not in _KNOWN_TEMPLATES:
        return None, "ENTRY_TEMPLATE_NOT_IN_FROZEN_PACK"
    return {
        "intent": intent,
        "cycle": cycle,
        "template_id": template_id,
        "strategy_config_sha256": cycle["strategy_config_sha256"],
        "strategy_revision": cycle["strategy_revision"],
        "intent_strategy_version": str(intent.get("strategy_version") or ""),
        "strategy": cycle["strategy"],
        "entry_receipt": receipt,
    }, None


def _timestamp_sort_value(episode: Mapping[str, Any]) -> tuple[int, str]:
    raw = episode.get("closed_at_ms")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = 0
    return value, str(episode.get("episode_id") or "")


def _performance(samples: list[dict[str, Any]]) -> dict[str, Any]:
    chronological = sorted(samples, key=_timestamp_sort_value)
    total = Decimal("0")
    peak = Decimal("0")
    drawdown = Decimal("0")
    for item in chronological:
        total += item["total_pnl"]
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    days = 0.0
    timestamps = [int(item["closed_at_ms"]) for item in chronological if str(item.get("closed_at_ms") or "").isdigit()]
    if len(timestamps) >= 2:
        days = max(0.0, (max(timestamps) - min(timestamps)) / 86_400_000)
    return {
        "settled_trades": len(samples),
        "net_pnl_usdt": round(float(total), 8) if samples else None,
        "max_drawdown_usdt": round(float(drawdown), 8) if samples else None,
        "observation_days": round(days, 2),
    }


def _cohort_key(provenance: Mapping[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(provenance["template_id"]),
        str(provenance["strategy_config_sha256"]),
        int(provenance["strategy_revision"]),
        str(provenance["intent_strategy_version"]),
    )


def validate_ai_strategy_evidence(
    store: Any,
    account_id: str,
    environment: str | None = None,
) -> dict[str, Any]:
    """Return scoped financial settlements and the stricter causal subset.

    When the account has both Gate environments, callers must pass one.  No
    rows are silently combined across environments or strategy revisions.
    """
    account = str(account_id or "").strip()
    if not account:
        raise ValueError("ACCOUNT_ID_REQUIRED")
    manifest = frozen_strategy_manifest()
    factory_digests = {row["id"]: row.get("config_sha256") for row in manifest["strategies"]}
    settlement_exclusions: Counter[str] = Counter()
    actor_exclusions: Counter[str] = Counter()
    cohorts: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    financial_samples: list[dict[str, Any]] = []
    scoped_environment: str | None = None
    scope_error: str | None = None
    latest_rows: list[dict[str, Any]] = []
    with store._connect() as db:
        scoped_environment, scope_error = _env_from_account_rows(db, account, environment)
        if scoped_environment:
            latest_rows = _latest_episode_rows(db, account, scoped_environment)
        for raw in latest_rows:
            financial, reason = _validate_full_cost_settlement(
                db, raw, account_id=account, environment=scoped_environment or "",
            )
            if financial is None:
                settlement_exclusions[str(reason or "SETTLEMENT_UNVERIFIED")] += 1
                continue
            financial_samples.append(financial)
            provenance, provenance_reason = _entry_provenance(
                db, financial, account_id=account, environment=scoped_environment or "",
            )
            if provenance is None:
                actor_exclusions[str(provenance_reason or "ENTRY_MODEL_PROVENANCE_UNPROVEN")] += 1
                continue
            key = _cohort_key(provenance)
            cohort = cohorts.setdefault(key, {
                "template_id": key[0],
                "strategy_config_sha256": key[1],
                "strategy_revision": key[2],
                "intent_strategy_version": key[3],
                "financial_samples": [],
                "autonomous_samples": [],
                "actor_exclusions": Counter(),
                "actor_types": Counter(),
            })
            cohort["financial_samples"].append(financial)
            actor = classify_episode_exit_actor(
                db,
                account_id=account,
                environment=scoped_environment or "",
                episode_id=financial["episode_id"],
                symbol=financial["symbol"],
                side=financial["side"],
                entry_order_id=financial["entry_order_id"],
                entry_intent_id=financial["entry_intent_id"],
                entry_receipt=provenance["entry_receipt"],
                entry_strategy={
                    "template_id": provenance["template_id"],
                    "strategy_config_sha256": provenance["strategy_config_sha256"],
                    "strategy_revision": provenance["strategy_revision"],
                },
                entry_intent_strategy_version=provenance["intent_strategy_version"],
                settlement=financial["settlement"],
            )
            if actor.get("actor"):
                financial["actor"] = actor
                cohort["autonomous_samples"].append(financial)
                cohort["actor_types"][str(actor["actor"])] += 1
            else:
                reason = str(actor.get("reason") or "EXIT_ACTOR_UNPROVEN")
                cohort["actor_exclusions"][reason] += 1
                actor_exclusions[reason] += 1

    financial_stats = _performance(financial_samples)
    financial_summary = {
        "source": "M1_NATIVE_SETTLED_FULL_COST",
        "venue": "gate",
        "account_id": account,
        "environment": scoped_environment,
        "settled_trades": financial_stats["settled_trades"],
        "net_pnl_usdt": financial_stats["net_pnl_usdt"],
        "max_drawdown_usdt": financial_stats["max_drawdown_usdt"],
        "observation_days": financial_stats["observation_days"],
        "unqualified_financial_trades": max(0, len(financial_samples) - sum(
            len(value["financial_samples"]) for value in cohorts.values()
        )),
    }

    strategy_results = []
    all_comparable = True
    for frozen in manifest["strategies"]:
        strategy_cohorts = []
        for key, value in sorted(cohorts.items(), key=lambda item: item[0]):
            if key[0] != frozen["id"]:
                continue
            financial_cohort_stats = _performance(value["financial_samples"])
            autonomous_stats = _performance(value["autonomous_samples"])
            template_match = key[1] == factory_digests.get(frozen["id"])
            actor_qualified = bool(value["autonomous_samples"])
            cohort_status = (
                "TEMPLATE_CONFIG_MISMATCH" if not template_match
                else "ACTOR_QUALIFIED" if actor_qualified
                else "NOT_COMPARABLE"
            )
            strategy_cohorts.append({
                "strategy_config_sha256": key[1],
                "strategy_revision": key[2],
                "intent_strategy_version": key[3],
                "template_config_match": template_match,
                "qualification_status": cohort_status,
                "actor_qualified": actor_qualified,
                "financial_settled_trades": financial_cohort_stats["settled_trades"],
                "financial_net_pnl_usdt": financial_cohort_stats["net_pnl_usdt"],
                "financial_max_drawdown_usdt": financial_cohort_stats["max_drawdown_usdt"],
                "autonomous_qualified_trades": autonomous_stats["settled_trades"],
                "autonomous_net_pnl_usdt": autonomous_stats["net_pnl_usdt"],
                "autonomous_max_drawdown_usdt": autonomous_stats["max_drawdown_usdt"],
                "observation_days": autonomous_stats["observation_days"],
                "actor_types": dict(value["actor_types"]),
                "actor_exclusions": dict(value["actor_exclusions"]),
            })
        one_cohort = len(strategy_cohorts) == 1
        if one_cohort:
            item = strategy_cohorts[0]
            qualified_count = item["autonomous_qualified_trades"]
            enough = qualified_count >= MIN_CLOSED_TRADES and item["observation_days"] >= MIN_OBSERVATION_DAYS
            if item["qualification_status"] == "TEMPLATE_CONFIG_MISMATCH":
                evidence_status = "TEMPLATE_CONFIG_MISMATCH"
            elif item["qualification_status"] == "NOT_COMPARABLE":
                evidence_status = "NOT_COMPARABLE"
            else:
                evidence_status = "SUFFICIENT_FOR_COMPARISON" if enough else "EVIDENCE_INSUFFICIENT"
            row_metrics = {
                "settled_trades": qualified_count,
                "net_pnl_usdt": item["autonomous_net_pnl_usdt"],
                "max_drawdown_usdt": item["autonomous_max_drawdown_usdt"],
                "observation_days": item["observation_days"],
            }
        elif len(strategy_cohorts) > 1:
            evidence_status = "NOT_COMPARABLE"
            row_metrics = {
                "settled_trades": None,
                "net_pnl_usdt": None,
                "max_drawdown_usdt": None,
                "observation_days": None,
            }
        else:
            evidence_status = "EVIDENCE_INSUFFICIENT"
            row_metrics = {
                "settled_trades": 0,
                "net_pnl_usdt": None,
                "max_drawdown_usdt": None,
                "observation_days": 0.0,
            }
        if evidence_status != "SUFFICIENT_FOR_COMPARISON":
            all_comparable = False
        strategy_results.append({
            **frozen,
            **row_metrics,
            "config_cohorts": strategy_cohorts,
            "evidence_status": evidence_status,
        })

    if scope_error:
        all_comparable = False
    comparison_status = "READY_TO_RANK" if all_comparable else "EVIDENCE_INSUFFICIENT"
    exclusions = {
        "settlement": dict(settlement_exclusions),
        "autonomous_qualification": dict(actor_exclusions),
        "counts": {
            "financial_settled": len(financial_samples),
            "autonomous_qualified": sum(len(value["autonomous_samples"]) for value in cohorts.values()),
            "autonomous_excluded": sum(actor_exclusions.values()),
        },
        "scope_error": scope_error,
    }
    return {
        "account_id": account,
        "environment": scoped_environment,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest,
        "thresholds": {
            "minimum_settled_trades_per_strategy": MIN_CLOSED_TRADES,
            "minimum_observation_days_per_strategy": MIN_OBSERVATION_DAYS,
        },
        "financial_settled": financial_summary,
        "exclusions": exclusions,
        "historical_replay": {
            "status": "NOT_RUN",
            "reason": "MODEL_PROMPT_NEWS_UNIVERSE_AS_OF_REPLAY_NOT_RUN",
        },
        "forward_comparison": {
            "status": comparison_status,
            "results": strategy_results,
        },
        "default_strategy_recommendation": None,
    }


__all__ = [
    "MIN_CLOSED_TRADES",
    "MIN_OBSERVATION_DAYS",
    "frozen_strategy_manifest",
    "validate_ai_strategy_evidence",
]
