"""Read-only evidence gate for comparing the four model-led Gate strategies.

The older bar replay runs deterministic indicator strategies, not these model
instructions.  This report deliberately separates observed forward outcomes
from a causal historical replay so sparse fills cannot produce a fake winner.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any

from ..trading.ai_strategy_book import STRATEGY_PROFILE_VERSION, TEMPLATES


MIN_CLOSED_TRADES = 100
MIN_OBSERVATION_DAYS = 30


def frozen_strategy_manifest() -> dict[str, Any]:
    entries = []
    for template in TEMPLATES:
        frozen = {
            "id": template["id"], "name": template["name"],
            "style": template["style"],
            "scan_interval_minutes": template["scan_interval_minutes"],
            "profile": template["profile"],
            "sections": template["sections"],
            "execution_defaults": template["execution_defaults"],
        }
        digest = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
        entries.append({"id": template["id"], "name": template["name"],
                        "style": template["style"], "scan_interval_minutes": template["scan_interval_minutes"],
                        "profile_version": STRATEGY_PROFILE_VERSION, "sha256": digest})
    return {"schema_version": "ai_strategy_validation_v1", "strategies": entries,
            "pack_sha256": hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}


def _observed_strategy_digest(strategy: dict[str, Any]) -> str:
    stable = {key: strategy.get(key) for key in ("template_id", "profile", "sections", "execution")}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def validate_ai_strategy_evidence(store: Any, account_id: str) -> dict[str, Any]:
    """Compare only fully settled, current-version AI decisions by account."""
    manifest = frozen_strategy_manifest()
    ids = {item["id"] for item in manifest["strategies"]}
    with store._connect() as db:
        cycles = db.execute(
            """SELECT cycle_id,action,status,model_called,payload_json,created_at
               FROM ai_led_cycles WHERE account_id=? ORDER BY created_at ASC""", (account_id,)
        ).fetchall()
        outcomes = db.execute(
            """SELECT m.cycle_id,m.decision_at,m.outcome_status,m.outcome_pnl,
                      i.strategy_id,i.strategy_version
               FROM ai_decision_memory m JOIN order_intents i
                 ON i.account_id=m.account_id AND i.cycle_id=m.cycle_id
               WHERE m.account_id=? AND m.outcome_status IN ('WIN','LOSS','FLAT')
                 AND i.reduce_only=0
               GROUP BY m.memory_id ORDER BY m.decision_at ASC""", (account_id,)
        ).fetchall()
    counters = {strategy_id: {"cycles": 0, "model_calls": 0, "open_proposals": 0,
                              "completed_outcomes": [], "observed_digests": set()} for strategy_id in ids}
    cycle_strategy: dict[str, tuple[str, str]] = {}
    for row in cycles:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        strategy = payload.get("strategy_instructions")
        if not isinstance(strategy, dict):
            continue
        strategy_id = str(strategy.get("template_id") or "")
        profile = strategy.get("profile") if isinstance(strategy.get("profile"), dict) else {}
        if strategy_id not in ids or profile.get("version") != STRATEGY_PROFILE_VERSION:
            continue
        bucket = counters[strategy_id]
        digest = _observed_strategy_digest(strategy)
        bucket["observed_digests"].add(digest)
        cycle_strategy[str(row["cycle_id"])] = (strategy_id, digest)
        bucket["cycles"] += 1
        bucket["model_calls"] += int(bool(row["model_called"]))
        bucket["open_proposals"] += int(str(row["action"] or "").upper() in {"OPEN_LONG", "OPEN_SHORT"})
    for row in outcomes:
        strategy_id = str(row["strategy_id"] or "")
        observed = cycle_strategy.get(str(row["cycle_id"] or ""))
        if strategy_id not in ids or observed is None or observed[0] != strategy_id:
            continue
        pnl = _finite(row["outcome_pnl"])
        if pnl is not None:
            counters[strategy_id]["completed_outcomes"].append((str(row["decision_at"]), pnl))
    results = []
    for frozen in manifest["strategies"]:
        bucket = counters[frozen["id"]]
        samples = bucket.pop("completed_outcomes")
        observed_digests = sorted(bucket.pop("observed_digests"))
        days = 0.0
        if len(samples) >= 2:
            try:
                first = datetime.fromisoformat(samples[0][0].replace("Z", "+00:00"))
                last = datetime.fromisoformat(samples[-1][0].replace("Z", "+00:00"))
                days = max(0.0, (last - first).total_seconds() / 86400)
            except ValueError:
                pass
        equity = peak = drawdown = 0.0
        for _, pnl in samples:
            equity += pnl
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        sufficient = (len(samples) >= MIN_CLOSED_TRADES and days >= MIN_OBSERVATION_DAYS
                      and len(observed_digests) == 1)
        results.append({**frozen, **bucket, "settled_trades": len(samples),
                        "observed_strategy_digests": observed_digests,
                        "observation_days": round(days, 2),
                        "net_pnl_usdt": round(equity, 4) if samples else None,
                        "max_drawdown_usdt": round(drawdown, 4) if samples else None,
                        "evidence_status": "SUFFICIENT_FOR_COMPARISON" if sufficient else "EVIDENCE_INSUFFICIENT"})
    all_sufficient = all(row["evidence_status"] == "SUFFICIENT_FOR_COMPARISON" for row in results)
    return {
        "account_id": account_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest,
        "thresholds": {"minimum_settled_trades_per_strategy": MIN_CLOSED_TRADES,
                       "minimum_observation_days_per_strategy": MIN_OBSERVATION_DAYS},
        "historical_replay": {"status": "NOT_RUN", "reason": "MODEL_PROMPT_AND_AS_OF_NEWS_REPLAY_NOT_VERIFIED"},
        "forward_comparison": {"status": "READY_TO_RANK" if all_sufficient else "EVIDENCE_INSUFFICIENT",
                               "results": results},
        "default_strategy_recommendation": None,
    }
