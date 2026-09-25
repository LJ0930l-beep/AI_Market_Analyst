"""Production AI-led session coordinator.

This module turns the configured local AI provider into strategy-driven
:class:`AILedDecisionEngine` cycles. It deliberately keeps
model selection, account scope, market snapshots, generation checks and
cancellation outside the model.  Exchange credentials and risk controls are
the executable boundary; a second local TradingAuthorization is not.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from .ai_led_engine import (
    AIActionOutput,
    AICycleContext,
    AILedDecisionEngine,
    ALLOWED_AI_ACTIONS,
    is_verified_bonsai_inference_receipt,
)
from .execution_gateway import (
    ControlMode,
    DecisionPath,
    ExecutionGateway,
    TradingMode,
)
from .ledger import AccountLedger
from .position_guardian import PositionGuardian
from .risk_engine import RiskEngine
from ..evidence import EvidenceBundle, model_weight_digest, persist_evidence_bundle
from ..model_routing import DEFAULT_SMART_MODEL
from .account_scope import resolve_account_scope
from .ai_calibration import AICalibrationService
from .ai_strategy_book import AIStrategyBook
from .candidate_scanner import CandidateScanner
from .decision_memory import (
    list_decision_memory,
    memory_for_prompt,
    reconcile_decision_outcomes,
    record_decision_memory,
)
from .institutional_schema import ensure_institutional_trader_schema
from .market_universe import MarketUniverse
from .model_schemas import AI_ACTION_SCHEMA, normalize_limit_ttl_alias, normalize_misplaced_strategy_plan, normalize_news_impact, normalize_wait_conditions, normalize_wait_symbol_alias, require_confidence_for_open, require_entry_analysis_for_open, require_nofx_gate_open_contract, validate_schema
from .account_aliases import canonical_account_id, GATE_TESTNET_ACCOUNT_ID
from .gate_account_truth import GateAccountTruthService
from .gate_accounts import build_gate_trader
from .bonsai_recovery import schedule_bonsai_recovery
from .ai_cycle_trace import humanize_reason, infer_block_stage
from .autonomous_strategy import CONTRACT, POLICY, NOFX_GATE_POLICY, NET_RR_ROUNDING_TOLERANCE, build_strategy_system_prompt, technical_context, compact_technical, relevant_news_for_entry, minimum_stop_distance, book_cost_evidence, number
from .ai_led_engine import _slim_technical_context
from .fin_dataset_collector import record_sft_sample
from .strategy_schedule import StrategySchedule, aligned_at
from .dynamic_risk_policy import ENTRY_ACTIONS, evaluate_dynamic_risk
from ..analysis.ai_trade_analytics import build_performance_context

logger = logging.getLogger("core.trading.ai_session_coordinator")

AI_COORDINATOR_CONTRACT_VERSION = "ai_session_coordinator_v1"
AI_PROMPT_VERSION = "ai_news_technical_strategy_v8_nofx_gate"
TRADE_JSON_GUIDE = (
    "JSON字段规则：只输出单个JSON对象。必需action、instrument_id、reason、confidence；action只能WAIT/HOLD/OPEN_LONG/OPEN_SHORT/REDUCE_POSITION/CLOSE_POSITION/TIGHTEN_STOP/UPDATE_PROTECTION/CANCEL_ORDER，"
    "reason用简体中文且不超过120字，只写结论和可审计依据；不输出思维链或逐步推理。confidence为0-100数字或null。开仓填写entry_price、stop_price、take_profit、requested_risk_fraction、requested_leverage、"
    "position_size_usdt（可选名义金额请求，只会被策略金额、止损风险、保证金和交易所规则进一步下压）、"
    "order_preference、limit_price、ttl_seconds、evidence_refs；entry_zone、news_context、timeframe_analysis、strategy_analysis、strategy_plan、"
    "invalidation_condition都是可选解释字段，别为凑格式编造。持仓管理按动作填写position_id/new_stop_price/new_take_profit/reduce_fraction。"
    "若填写news_context.impact，只能为POSITIVE/NEGATIVE/NEUTRAL/UNKNOWN字符串。"
    "按market_snapshots中的slippage与fee_rate估算净盈亏比；成本过高时优先审视被动限价，不能据实满足策略门槛就WAIT。"
    "requested_risk_fraction用小数，例如0.0025表示0.25%；不允许超过策略单笔风险上限。"
    "只用固定字段及其规定类型，不加额外键；WAIT/HOLD不填开仓字段。等待的缺失条件写在strategy_analysis.missing_conditions数组，下一触发价写next_trigger_price。"
)
MIN_CYCLE_INTERVAL_SECONDS = 60.0
DEFAULT_CYCLE_INTERVAL_SECONDS = 900.0
MAX_CYCLE_SYMBOLS = 8
MAX_UNIVERSE_SYMBOLS = 3
INITIAL_DEEP_SCAN_SYMBOLS = 2
MAX_DEEP_SCAN_SYMBOLS = 3
MODEL_BUDGET_SECONDS = 170.0
DECISION_MODEL_REQUEST_TIMEOUT_SECONDS = 120.0
INFERENCE_SOFT_CONTEXT_LIMIT = 7000  # Leave room for local 27B prefill latency as well as JSON output.
INTENT_TTL_SECONDS = 240.0
MARKET_MAX_AGE_SECONDS = 120.0
MODEL_CONTEXT_LENGTH = 8192
MIN_MODEL_CONTEXT_LENGTH = 8192
DECISION_OUTPUT_TOKEN_BUDGET = 2048
MIN_DECISION_OUTPUT_TOKENS = 1024
PROMPT_BUDGET_SAFETY_MARGIN_TOKENS = 256
MODEL_KEEP_ALIVE = "45m"


def _deep_scan_symbol_limit(wait_streak: int) -> int:
    """Keep model-facing breadth small even after repeated WAIT decisions."""
    if wait_streak >= 6:
        return MAX_DEEP_SCAN_SYMBOLS
    return INITIAL_DEEP_SCAN_SYMBOLS


def derive_strategy_plan_from_text(decoded: dict[str, Any]) -> dict[str, Any] | None:
    """Project a one-sentence strategy_plan onto the required object shape.

    Only facts the model already supplied are reused here: the thesis is its
    literal text and the conditions restate its own entry/stop/take-profit
    numbers. Nothing is invented, and callers must stamp the derived provenance
    so the audit record never presents this as model-authored structure.
    """
    text = str(decoded.get("strategy_plan") or "").strip()[:800]
    if not text:
        return None
    instrument = str(decoded.get("instrument_id") or "TRADE").strip() or "TRADE"

    def labelled(field: str) -> str | None:
        value = decoded.get(field)
        return f"{field} {value}" if value not in (None, "") else None

    entry = labelled("entry_price") or labelled("limit_price")
    exits = [item for item in (labelled("stop_price"), labelled("take_profit")) if item]
    return {
        "name": f"{instrument[:80]}_PLAN",
        "thesis": text,
        "entry_conditions": [entry or text[:300]],
        "exit_conditions": exits or [text[:300]],
    }


def _estimate_tokens(text: str) -> int:
    """Conservatively estimate tokens for mixed Chinese/JSON model inputs.

    CJK/full-width glyphs are charged individually. Remaining text is charged
    at 1.75 characters per token, calibrated against the largest observed
    structured cycle payload rather than a prose-only 4-character heuristic.
    """
    cjk = 0
    other = 0
    for char in str(text or ""):
        if ord(char) >= 0x3000:
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / 1.75)


def _assert_prompt_fits(
    *,
    system_content: str,
    user_content: str,
    context_length: int,
    reserve: int,
    token_counter: Callable[[str], int] | None = None,
) -> int:
    count_tokens = token_counter or _estimate_tokens
    system_tokens = count_tokens(system_content)
    user_tokens = count_tokens(user_content)
    estimated = system_tokens + user_tokens
    if int(context_length or 0) <= 0:
        raise ValueError("MODEL_CONTEXT_UNKNOWN: refusing to send a trading prompt without a verified runtime window")
    if estimated + max(0, int(reserve or 0)) > int(context_length):
        largest_fields: list[str] = []
        try:
            payload = json.loads(user_content)
            if isinstance(payload, dict):
                largest_fields = [
                    f"{key}~{size}"
                    for key, size in sorted(
                        ((str(key), _estimate_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))) for key, value in payload.items()),
                        key=lambda item: item[1],
                        reverse=True,
                    )
                ]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        largest = f"; largest_user_fields={','.join(largest_fields)}" if largest_fields else ""
        raise ValueError(
            "AI_INPUT_BUDGET_EXCEEDED: estimated prompt plus response reserve "
            f"(system={system_tokens} + user={user_tokens} + reserve={max(0, int(reserve or 0))} tokens{largest}) exceeds the "
            f"{int(context_length)}-token model window and would be truncated"
        )
    return estimated


def _fit_prompt_payload(
    payload: dict[str, Any],
    system_content: str,
    context_length: int,
    reserve: int = MIN_DECISION_OUTPUT_TOKENS,
    *,
    signal_timeframe: str | None = None,
    token_counter: Callable[[str], int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project optional prompt detail down to a verified model-window budget.

    The frozen source evidence is kept separately. This function only removes
    redundant/older detail from the model-facing projection, preserving every
    allowed symbol, candidate, news identity, risk gate and signal-timeframe
    candle. It never changes the verified context length or output reserve.
    """
    if not isinstance(payload, dict):
        raise ValueError("AI_PROMPT_PAYLOAD_INVALID")
    if int(context_length or 0) <= 0:
        _assert_prompt_fits(
            system_content=system_content,
            user_content=json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            context_length=context_length,
            reserve=reserve,
        )
    projected = copy.deepcopy(payload)
    count_tokens = token_counter or _estimate_tokens
    steps: list[str] = []
    required_reserve = max(0, int(reserve or 0)) + PROMPT_BUDGET_SAFETY_MARGIN_TOKENS
    system_tokens = _estimate_tokens(system_content)
    if token_counter is not None:
        system_tokens = count_tokens(system_content)

    def user_content() -> str:
        return json.dumps(projected, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def fits() -> bool:
        return system_tokens + count_tokens(user_content()) + required_reserve <= int(context_length)

    def result() -> tuple[dict[str, Any], dict[str, Any]]:
        estimated = system_tokens + count_tokens(user_content())
        return projected, {
            "compacted": bool(steps),
            "steps": list(steps),
            "estimated_input_tokens": estimated,
            "context_length": int(context_length),
            "reserve_tokens": max(0, int(reserve or 0)),
            "safety_margin_tokens": PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
            "tokenizer": "BONSAI_RUNTIME" if token_counter is not None else "CONSERVATIVE_ESTIMATE",
            "news_semantics_compacted": any(step == "shorten_news_summaries" for step in steps),
        }

    if fits():
        return result()

    def record(step: str, changed: bool) -> bool:
        if changed:
            steps.append(step)
        return fits()

    # The derivatives matrix already carries per-symbol OI change, funding and
    # crowding; its raw history duplicates that information in this decision.
    radar = projected.get("market_radar")
    radar = radar if isinstance(radar, dict) else {}
    open_interest = radar.get("open_interest")
    if isinstance(open_interest, dict):
        changed = open_interest.pop("series", None) is not None
        changed = open_interest.pop("series_columns", None) is not None or changed
        if record("remove_redundant_oi_history", changed):
            return result()

    # Keep aggregate liquidation totals; individual event rows remain in the
    # immutable source evidence and in the Radar UI.
    liquidations = radar.get("liquidations")
    if isinstance(liquidations, dict):
        changed = liquidations.pop("recent", None) is not None
        changed = liquidations.pop("recent_columns", None) is not None or changed
        if record("remove_optional_liquidation_rows", changed):
            return result()

    # Preserve each selected event's ID, title, source, time and impact.
    news = projected.get("news_revisions")
    if isinstance(news, list):
        changed = False
        for item in news:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("summary"), str) and len(item["summary"]) > 96:
                item["summary"] = item["summary"][:96]
                changed = True
            if "url" in item:
                item.pop("url", None)
                changed = True
        if record("shorten_news_summaries", changed):
            return result()

    # ATR/lock state and its reason remain explicit; source diagnostics are
    # still preserved in the unabridged evidence bundle.
    dynamic_risk = projected.get("dynamic_risk")
    if isinstance(dynamic_risk, dict):
        keep = ("status", "entry_allowed", "blocked_until", "reasons", "evidence_status", "atr_adaptive_sizing")
        compact_risk = {key: dynamic_risk[key] for key in keep if key in dynamic_risk}
        reasons = compact_risk.get("reasons")
        if isinstance(reasons, list):
            compact_risk["reasons"] = [str(item)[:96] for item in reasons[:3]]
        elif isinstance(reasons, str):
            compact_risk["reasons"] = reasons[:192]
        changed = compact_risk != dynamic_risk
        if changed:
            projected["dynamic_risk"] = compact_risk
        if record("compact_dynamic_risk_context", changed):
            return result()

    # Retain calibration state and its concise summary stats, not historical
    # replay timestamps or free-form notes already stored for the UI.
    calibration = projected.get("calibration")
    if isinstance(calibration, dict) and isinstance(calibration.get("profile"), dict):
        profile = calibration["profile"]
        profile_fields = ("entry_style", "max_concurrent_positions", "order_preference", "risk_regime")
        compact_profile = {key: profile[key] for key in profile_fields if key in profile}
        replay = profile.get("replay")
        if isinstance(replay, dict):
            replay_fields = ("mean_return", "positive_fraction", "return_observations", "sample_size", "symbol_count")
            compact_replay = {key: replay[key] for key in replay_fields if key in replay}
            if compact_replay:
                compact_profile["replay"] = compact_replay
        changed = compact_profile != profile
        if changed:
            calibration["profile"] = compact_profile
        if record("compact_calibration_profile", changed):
            return result()

    # The quote and spread/freshness are decision-critical. Exchange aliases,
    # duplicate last prices and unrelated volume columns are not.
    snapshots = projected.get("market_snapshots")
    if isinstance(snapshots, dict):
        snapshot_fields = (
            "symbol", "price", "bid", "ask", "mark", "index", "fundingRate", "openInterest",
            "data_as_of", "source", "freshness_status", "fresh", "slippage", "fee_rate",
        )
        changed = False
        for symbol, item in list(snapshots.items()):
            if not isinstance(item, dict):
                continue
            compact_snapshot = {key: item[key] for key in snapshot_fields if key in item and item[key] is not None}
            market = item.get("market") if isinstance(item.get("market"), dict) else {}
            if "fee_rate" not in compact_snapshot and market.get("taker") is not None:
                try:
                    fee_rate = float(market["taker"])
                except (TypeError, ValueError):
                    fee_rate = None
                if fee_rate is not None and math.isfinite(fee_rate) and fee_rate >= 0:
                    compact_snapshot["fee_rate"] = fee_rate
            changed = compact_snapshot != item or changed
            snapshots[symbol] = compact_snapshot
        if record("compact_market_snapshots", changed):
            return result()

    # Keep the latest trade-flow sample for each symbol. The full CVD ladder
    # remains available to the radar UI and frozen source evidence.
    cvd = radar.get("cvd")
    if isinstance(cvd, dict) and isinstance(cvd.get("series"), dict):
        changed = False
        for symbol, rows in list(cvd["series"].items()):
            if isinstance(rows, list) and len(rows) > 1:
                cvd["series"][symbol] = rows[-1:]
                changed = True
        if record("shorten_cvd_history", changed):
            return result()

    # The per-symbol derivatives matrix carries the current OI status and
    # values. Its top-level OI summary repeats those facts in the prompt.
    if isinstance(radar.get("derivatives_matrix"), list) and radar["derivatives_matrix"]:
        if record("remove_redundant_oi_summary", radar.pop("open_interest", None) is not None):
            return result()

    # A previous WAIT is not experience about whether today's setup works.
    # Repeating it wastes candle budget and anchors the next model decision.
    memories = projected.get("decision_memory")
    if isinstance(memories, list) and memories and all(
        isinstance(row, dict)
        and str(row.get("action") or "").upper() in {"WAIT", "HOLD", "SYSTEM_BLOCKED"}
        for row in memories
    ):
        projected["decision_memory"] = []
        if record("drop_unsettled_wait_memory", True):
            return result()

    # The account balance is already supplied in account_truth. When there
    # are no settled AI trades, zero win rate and PnL are not a performance
    # signal and should not displace closed candles from an 8K prompt.
    performance = projected.get("performance_context")
    if isinstance(performance, dict) and performance.get("closed_trades") == 0:
        keep = ("status", "stance", "closed_trades", "capital_stale")
        compact_performance = {key: performance[key] for key in keep if key in performance}
        changed = compact_performance != performance
        if changed:
            projected["performance_context"] = compact_performance
        if record("compact_unsettled_performance", changed):
            return result()

    # Unconfigured radar feeds are UNKNOWN, not zero-valued trade evidence.
    # Their full status remains in the frozen source bundle and UI.
    changed = False
    for key in ("cross_market", "onchain"):
        item = radar.get(key)
        if isinstance(item, dict) and str(item.get("status") or "").upper() in {
            "CONFIG_REQUIRED", "NO_DATA", "UNAVAILABLE",
        }:
            radar.pop(key, None)
            changed = True
    if record("drop_unavailable_optional_radar", changed):
        return result()

    # A bounded 8K decision needs the newest identified headline per symbol;
    # older same-symbol revisions remain frozen for audit. Preserve every
    # symbol's news coverage and all market-wide events.
    news = projected.get("news_revisions")
    if isinstance(news, list) and len(news) > 1:
        latest_by_symbol: set[str] = set()
        compact_news = []
        for item in news:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if str(item.get("scope") or "").upper() == "MARKET_WIDE" or not symbol:
                compact_news.append(item)
            elif symbol not in latest_by_symbol:
                compact_news.append(item)
                latest_by_symbol.add(symbol)
        changed = len(compact_news) < len(news)
        if changed:
            projected["news_revisions"] = compact_news
        if record("keep_latest_news_per_symbol", changed):
            return result()

    universe = projected.get("market_universe")
    if isinstance(universe, dict):
        keep = ("status", "environment", "eligible_count", "selected_symbols", "mode", "unsupported_sources")
        compact_universe = {key: universe[key] for key in keep if key in universe}
        changed = compact_universe != universe
        if changed:
            projected["market_universe"] = compact_universe
        if record("compact_universe_metadata", changed):
            return result()

    # Candles on the selected signal frame remain the main evidence. Context
    # frames keep their latest closed bar and all indicators/status metadata.
    technical = projected.get("technical_context")
    if isinstance(technical, dict):
        changed = False
        for symbol, instrument in technical.items():
            if symbol in {"candle_columns", "indicator_columns"} or not isinstance(instrument, dict):
                continue
            frames = instrument.get("timeframes")
            if not isinstance(frames, dict):
                continue
            for timeframe, frame in frames.items():
                if not isinstance(frame, dict):
                    continue
                bar_key = "candles" if isinstance(frame.get("candles"), list) else ("bars" if isinstance(frame.get("bars"), list) else None)
                if not bar_key:
                    continue
                keep_count = 3 if str(timeframe).lower() == str(signal_timeframe or "").lower() else 1
                bars_list = frame[bar_key]
                if len(bars_list) > keep_count:
                    frame[bar_key] = bars_list[-keep_count:]
                    changed = True
        if record("trim_older_candle_history", changed):
            return result()

    # Candidate coverage is preserved one-per-symbol. Keep at least the best
    # condition plus an invalidation; shorten verbose prose only as needed.
    candidates = projected.get("candidates")
    if isinstance(candidates, list):
        changed = False
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            conditions = candidate.get("conditions")
            if isinstance(conditions, list):
                compact_conditions = [str(item)[:28] for item in conditions[:1]]
                changed = compact_conditions != conditions or changed
                candidate["conditions"] = compact_conditions
            rationale = candidate.get("rationale")
            if isinstance(rationale, str) and len(rationale) > 96:
                candidate["rationale"] = rationale[:96]
                changed = True
            invalidation = candidate.get("invalidation")
            if isinstance(invalidation, str) and len(invalidation) > 56:
                candidate["invalidation"] = invalidation[:56]
                changed = True
            proposal = candidate.get("proposal")
            if isinstance(proposal, dict):
                p_rationale = proposal.get("rationale")
                if isinstance(p_rationale, str) and len(p_rationale) > 96:
                    proposal["rationale"] = p_rationale[:96]
                    changed = True
                p_conditions = proposal.get("conditions")
                if isinstance(p_conditions, list) and len(p_conditions) > 1:
                    proposal["conditions"] = [str(item)[:28] for item in p_conditions[:1]]
                    changed = True
        if record("shorten_candidate_text", changed):
            return result()

    # Trim optional raw exchange blobs and large memory summaries
    account = projected.get("account_truth")
    if isinstance(account, dict) and isinstance(account.get("pending_orders"), list):
        changed = False
        for order in account["pending_orders"]:
            if isinstance(order, dict) and "raw_exchange_blob" in order:
                order.pop("raw_exchange_blob", None)
                changed = True
        if record("trim_pending_order_blobs", changed):
            return result()

    memories = projected.get("decision_memory")
    if isinstance(memories, list):
        changed = False
        for memory in memories:
            if isinstance(memory, dict) and isinstance(memory.get("summary_zh"), str) and len(memory["summary_zh"]) > 96:
                memory["summary_zh"] = memory["summary_zh"][:96]
                changed = True
        if record("shorten_decision_memory_summaries", changed):
            return result()

    universe = projected.get("universe_snapshot")
    if isinstance(universe, dict) and isinstance(universe.get("candidate_metrics"), list):
        changed = False
        for metric in universe["candidate_metrics"]:
            if isinstance(metric, dict) and "metric_blob" in metric:
                metric.pop("metric_blob", None)
                changed = True
        if record("trim_universe_metric_blobs", changed):
            return result()

    # Preserve news meaning for the leading revision before shedding any
    # remaining optional prose. Signal-frame candles and risk state stay intact.
    if not fits() and isinstance(technical, dict):
        changed = False
        for symbol, instrument in technical.items():
            if symbol in {"candle_columns", "indicator_columns"} or not isinstance(instrument, dict):
                continue
            frames = instrument.get("timeframes")
            if not isinstance(frames, dict):
                continue
            for timeframe, frame in frames.items():
                if not isinstance(frame, dict):
                    continue
                bar_key = "candles" if isinstance(frame.get("candles"), list) else ("bars" if isinstance(frame.get("bars"), list) else None)
                if not bar_key:
                    continue
                keep_count = 2 if str(timeframe).lower() == str(signal_timeframe or "").lower() else 0
                bars_list = frame[bar_key]
                if len(bars_list) > keep_count:
                    frame[bar_key] = bars_list[-keep_count:] if keep_count else []
                    changed = True
        if record("trim_secondary_candles_keep_two_signal_bars", changed):
            return result()

    if not fits():
        changed = False
        news = projected.get("news_revisions")
        if isinstance(news, list):
            for item in news:
                if isinstance(item, dict) and isinstance(item.get("summary"), str) and len(item["summary"]) > 48:
                    item["summary"] = item["summary"][:48]
                    changed = True
        candidates = projected.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if isinstance(candidate.get("rationale"), str) and len(candidate["rationale"]) > 48:
                    candidate["rationale"] = candidate["rationale"][:48]
                    changed = True
                proposal = candidate.get("proposal")
                if isinstance(proposal, dict) and isinstance(proposal.get("rationale"), str) and len(proposal["rationale"]) > 48:
                    proposal["rationale"] = proposal["rationale"][:48]
                    changed = True
        if record("deep_compact_prose_fields", changed):
            return result()

    if not fits():
        changed = False
        experience = projected.get("strategy_experience")
        if isinstance(experience, dict) and isinstance(experience.get("recent_closed_trades"), list):
            trimmed_trades = []
            for trade in experience["recent_closed_trades"]:
                if isinstance(trade, dict):
                    trade_keep = ("cycle_id", "symbol", "strategy_template_id", "outcome", "pnl_usdt", "pnl_pct", "closed_at")
                    compact_trade = {k: trade[k] for k in trade_keep if k in trade}
                    trimmed_trades.append(compact_trade)
                    changed = compact_trade != trade or changed
                else:
                    trimmed_trades.append(trade)
            experience["recent_closed_trades"] = trimmed_trades
        if record("compact_strategy_experience_trades", changed):
            return result()

    if not fits():
        changed = False
        # Dedup news to keep at most 1 leading revision per symbol while preserving symbol coverage
        news = projected.get("news_revisions")
        if isinstance(news, list) and len(news) > 3:
            seen_symbols = set()
            compacted_news = []
            for item in news:
                if not isinstance(item, dict):
                    continue
                sym = item.get("symbol")
                if sym and sym not in seen_symbols:
                    seen_symbols.add(sym)
                    compacted_news.append(item)
                elif (
                    str(item.get("scope") or "").upper() == "MARKET_WIDE"
                    and not any(str(row.get("scope") or "").upper() == "MARKET_WIDE" for row in compacted_news)
                ):
                    compacted_news.append(item)
                elif not sym and len(compacted_news) < 3:
                    compacted_news.append(item)
            if compacted_news and len(compacted_news) < len(news):
                projected["news_revisions"] = compacted_news
                changed = True
        if record("dedup_news_revisions_per_symbol", changed):
            return result()

    if not fits():
        changed = False
        radar = projected.get("market_radar")
        if isinstance(radar, dict):
            for key in ("cross_market", "correlations", "funding"):
                sub = radar.get(key)
                if isinstance(sub, dict):
                    for sub_k in ("history", "series", "matrix", "details"):
                        if sub_k in sub:
                            sub.pop(sub_k, None)
                            changed = True
        if record("compact_market_radar_deep", changed):
            return result()

    if not fits() and isinstance(technical, dict):
        changed = False
        for symbol, instrument in technical.items():
            if symbol in {"candle_columns", "indicator_columns"} or not isinstance(instrument, dict):
                continue
            frames = instrument.get("timeframes")
            if not isinstance(frames, dict):
                continue
            for timeframe, frame in frames.items():
                if not isinstance(frame, dict):
                    continue
                indicators = frame.get("indicators")
                if isinstance(indicators, dict):
                    for ind_name, ind_val in list(indicators.items()):
                        if isinstance(ind_val, list) and len(ind_val) > 1:
                            indicators[ind_name] = ind_val[-1:]
                            changed = True
                        elif isinstance(ind_val, dict) and "series" in ind_val:
                            ind_val.pop("series", None)
                            changed = True
        if record("compact_technical_indicators_deep", changed):
            return result()

    if not fits():
        changed = False
        radar = projected.get("market_radar")
        if isinstance(radar, dict):
            for optional_key in ("onchain", "cross_market", "correlations"):
                if optional_key in radar:
                    radar.pop(optional_key, None)
                    changed = True
        if record("trim_optional_radar_subsystems", changed):
            return result()

    if not fits():
        changed = False
        candidates = projected.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if isinstance(candidate.get("reason"), str) and len(candidate["reason"]) > 16:
                    candidate["reason"] = candidate["reason"][:16]
                    changed = True
                if isinstance(candidate.get("invalidation"), str) and len(candidate["invalidation"]) > 16:
                    candidate["invalidation"] = candidate["invalidation"][:16]
                    changed = True
                if isinstance(candidate.get("conditions"), list) and len(candidate["conditions"]) > 1:
                    candidate["conditions"] = [str(candidate["conditions"][0])[:16]]
                    changed = True
                proposal = candidate.get("proposal")
                if isinstance(proposal, dict):
                    if isinstance(proposal.get("rationale"), str) and len(proposal["rationale"]) > 16:
                        proposal["rationale"] = proposal["rationale"][:16]
                        changed = True
                    if "conditions" in proposal:
                        proposal.pop("conditions", None)
                        changed = True
        if record("ultra_compact_candidate_prose", changed):
            return result()

    if not fits():
        changed = False
        news = projected.get("news_revisions")
        if isinstance(news, list):
            for item in news:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("title"), str) and len(item["title"]) > 16:
                    item["title"] = item["title"][:16]
                    changed = True
                if isinstance(item.get("summary"), str) and len(item["summary"]) > 16:
                    item["summary"] = item["summary"][:16]
                    changed = True
        if record("ultra_compact_news_prose", changed):
            return result()

    if not fits():
        changed = False
        account = projected.get("account_truth")
        if isinstance(account, dict):
            positions = account.get("positions")
            if isinstance(positions, list):
                pos_keep = ("symbol", "side", "quantity", "entry_price", "mark_price", "leverage", "unrealized_pnl")
                new_positions = [{k: p[k] for k in pos_keep if k in p} for p in positions if isinstance(p, dict)]
                if new_positions != positions:
                    account["positions"] = new_positions
                    changed = True
            orders = account.get("pending_orders")
            if isinstance(orders, list):
                ord_keep = ("order_id", "symbol", "side", "type", "price", "amount", "status")
                new_orders = [{k: o[k] for k in ord_keep if k in o} for o in orders if isinstance(o, dict)]
                if new_orders != orders:
                    account["pending_orders"] = new_orders
                    changed = True
        if record("compact_account_truth_fields", changed):
            return result()

    # An extended no-opportunity scan can select eight contracts while the
    # verified 8K model window only fits a smaller decision set. Keep every
    # held/pending contract and the highest-ranked remaining candidates;
    # omit complete lower-ranked symbols instead of sending a truncated prompt
    # or letting one oversized scan suppress the model call altogether.
    if not fits():
        allowed = projected.get("allowed_instruments")
        account = projected.get("account_truth")
        owned: set[str] = set()
        if isinstance(account, dict):
            for key in ("positions", "pending_orders"):
                for item in account.get(key) or []:
                    if isinstance(item, dict):
                        symbol = str(item.get("symbol") or item.get("instrument_id") or "").upper()
                        if symbol:
                            owned.add(symbol)
        if isinstance(allowed, list):
            for symbol in reversed(list(allowed)):
                if fits() or len(allowed) <= max(1, len(owned)):
                    break
                if str(symbol).upper() in owned:
                    continue
                allowed.remove(symbol)
                if isinstance(projected.get("technical_context"), dict):
                    projected["technical_context"].pop(symbol, None)
                if isinstance(projected.get("market_snapshots"), dict):
                    projected["market_snapshots"].pop(symbol, None)
                if isinstance(projected.get("candidates"), list):
                    projected["candidates"] = [
                        row for row in projected["candidates"]
                        if not isinstance(row, dict) or str(row.get("symbol") or row.get("instrument_id") or "").upper() != str(symbol).upper()
                    ]
                universe = projected.get("market_universe")
                if isinstance(universe, dict) and isinstance(universe.get("selected_symbols"), list):
                    universe["selected_symbols"] = [item for item in universe["selected_symbols"] if str(item).upper() != str(symbol).upper()]
                quality = projected.get("data_quality")
                if isinstance(quality, dict) and isinstance(quality.get("symbols"), list):
                    quality["symbols"] = [item for item in quality["symbols"] if str(item).upper() != str(symbol).upper()]
                refs = projected.get("evidence_refs")
                if isinstance(refs, list):
                    projected["evidence_refs"] = [
                        ref for ref in refs
                        if not str(ref).startswith((f"market_snapshot:{symbol}:", f"technical_snapshot:{symbol}:"))
                    ]
                steps.append(f"defer_symbol_for_model_window:{symbol}")
            if fits():
                return result()

    if not fits():
        _assert_prompt_fits(
            system_content=system_content,
            user_content=user_content(),
            context_length=context_length,
            reserve=required_reserve,
            token_counter=count_tokens,
        )
    estimated = _assert_prompt_fits(
        system_content=system_content,
        user_content=user_content(),
        context_length=context_length,
        reserve=required_reserve,
        token_counter=count_tokens,
    )
    result_metadata = result()[1]
    result_metadata["estimated_input_tokens"] = estimated
    return projected, result_metadata


def _session_context_length() -> int:
    raw = os.environ.get("OLLAMA_CONTEXT_LENGTH")
    if raw is None:
        return MODEL_CONTEXT_LENGTH
    try:
        override = int(raw)
    except (TypeError, ValueError):
        return MODEL_CONTEXT_LENGTH
    return override if override > 0 else MODEL_CONTEXT_LENGTH


def _session_keep_alive() -> str | int:
    raw = os.environ.get("OLLAMA_KEEP_ALIVE")
    if raw is None or not raw.strip():
        return MODEL_KEEP_ALIVE
    value = raw.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    if re.fullmatch(r"\d+(?:ms|s|m|h)", value):
        return value
    return MODEL_KEEP_ALIVE


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _as_utc(value)
    if value:
        try:
            return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            return None
    return None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _compact_market_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "symbol", "price", "last", "bid", "ask", "mark", "index", "open", "high", "low",
        "change", "percentage", "baseVolume", "quoteVolume", "volume", "fundingRate",
        "openInterest", "data_as_of", "received_at", "source", "freshness_status", "fresh",
        "environment", "market_data_environment",
    )
    return {key: snapshot[key] for key in fields if key in snapshot and snapshot[key] is not None}


def _radar_group(source: dict[str, Any], *, fields: tuple[str, ...] = ()) -> dict[str, Any]:
    result = {
        "status": str(source.get("status") or "NO_DATA").upper(),
        # Never label an unavailable/missing feed as a source we merely expect.
        "source": source.get("source") or "UNKNOWN",
        "as_of": source.get("as_of"),
    }
    result.update({key: source[key] for key in fields if key in source})
    return result


def _minutes_from_anchor(value: Any, anchor: Any) -> int | None:
    point, reference = _parse_time(value), _parse_time(anchor)
    if point is None or reference is None:
        return None
    return round((reference - point).total_seconds() / 60)


def _compact_market_radar(radar: Any, symbols: tuple[str, ...]) -> dict[str, Any]:
    """Allowlist a small, timestamped radar view for one AI decision."""
    raw = radar if isinstance(radar, dict) else {}
    selected = set(symbols[:MAX_UNIVERSE_SYMBOLS])

    cvd_source = raw.get("cvd") if isinstance(raw.get("cvd"), dict) else {}
    cvd_rows: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols[:MAX_UNIVERSE_SYMBOLS]}
    for row in cvd_source.get("series", []) if isinstance(cvd_source.get("series"), list) else []:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() not in selected:
            continue
        symbol = str(row["symbol"]).upper()
        cvd_rows[symbol].append([
            _minutes_from_anchor(row.get("time"), cvd_source.get("as_of")),
            row.get("price"), row.get("buy_contracts"), row.get("sell_contracts"),
            row.get("delta_contracts"), row.get("cvd_contracts"), row.get("trade_count"),
        ])
    cvd = _radar_group(cvd_source, fields=("unit", "synthetic"))
    cvd["series_columns"] = "min,price,buy,sell,delta,cvd,trades"
    cvd["series"] = {
        symbol: rows[-3:]
        for symbol, rows in cvd_rows.items()
        if rows
    }

    oi_source = raw.get("open_interest") if isinstance(raw.get("open_interest"), dict) else {}
    oi_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in oi_source.get("series", []) if isinstance(oi_source.get("series"), list) else []:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() not in selected:
            continue
        venue = str(row.get("venue") or "unknown").lower()
        symbol = str(row["symbol"]).upper()
        item = [row.get("venue"), row.get("symbol"),
                _minutes_from_anchor(row.get("time"), oi_source.get("as_of")),
                row.get("open_interest"), row.get("open_interest_usdt"), row.get("price"), row.get("unit")]
        oi_groups.setdefault((venue, symbol), []).append(item)
    oi = _radar_group(oi_source, fields=("synthetic", "binance_status"))
    oi["series_columns"] = "venue,symbol,min,oi,oi_usdt,price,unit"
    oi["series"] = [
        item
        for (_venue, _symbol), rows in sorted(oi_groups.items())
        for item in rows[-3:]
    ]

    matrix = []
    for row in raw.get("derivatives_matrix", []) if isinstance(raw.get("derivatives_matrix"), list) else []:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() not in selected:
            continue
        gate = row.get("gate") if isinstance(row.get("gate"), dict) else {}
        binance = row.get("binance") if isinstance(row.get("binance"), dict) else {}
        matrix.append([
            str(row.get("symbol") or "").upper(),
            gate.get("status", "AVAILABLE" if gate.get("time") else "NO_DATA"),
            gate.get("oi_change_pct"), gate.get("funding_rate_pct"),
            binance.get("status", "UNAVAILABLE"),
            binance.get("oi_change_pct"), binance.get("funding_rate_pct"),
            row.get("crowding_score"), row.get("crowding_label"), row.get("price_change_pct"),
        ])

    liquidation_source = raw.get("liquidations") if isinstance(raw.get("liquidations"), dict) else {}
    liquidations = _radar_group(liquidation_source, fields=("synthetic",))
    if liquidations["status"] == "AVAILABLE":
        liquidations["window_hours"] = liquidation_source.get("window_hours")
        counts = liquidation_source.get("counts") if isinstance(liquidation_source.get("counts"), dict) else {}
        notionals = liquidation_source.get("estimated_notional") if isinstance(liquidation_source.get("estimated_notional"), dict) else {}
        # The upstream radar aggregates only the selected universe. Keep just
        # its documented side totals; arbitrary symbol keys from another
        # provider response must not widen the prompt universe.
        liquidations["counts"] = {key: counts[key] for key in ("LONG", "SHORT", "UNKNOWN") if key in counts}
        liquidations["estimated_notional"] = {key: notionals[key] for key in ("LONG", "SHORT", "UNKNOWN") if key in notionals}
        liquidations["recent"] = [
            [row.get(key) for key in (
                "symbol", "time", "direction", "size_contracts", "price", "estimated_notional",
            )]
            for row in (liquidation_source.get("recent") or [])
            if isinstance(row, dict) and str(row.get("symbol") or "").upper() in selected
        ][:2]
        liquidations["recent_columns"] = "symbol,time,direction,size,price,notional"

    onchain_source = raw.get("onchain") if isinstance(raw.get("onchain"), dict) else {}
    onchain = _radar_group(onchain_source, fields=("synthetic",))
    token_symbols = {symbol[:-4] if symbol.endswith("USDT") else symbol for symbol in selected}
    onchain_events = [
        [row.get("provider"), row.get("asset"), row.get("event_at") or row.get("received_at"),
         row.get("direction"), row.get("amount_usd") or row.get("amount")]
        for row in (onchain_source.get("events") or [])
        if isinstance(row, dict) and str(row.get("asset") or "").upper() in token_symbols
    ][:2]
    if onchain_events:
        onchain["events"] = onchain_events
        onchain["event_columns"] = "provider,asset,time,direction,amount"

    cross_source = raw.get("cross_market") if isinstance(raw.get("cross_market"), dict) else {}
    cross_market = _radar_group(cross_source, fields=("synthetic",))
    cross_rows = [row for row in (cross_source.get("items") or [])[:4] if isinstance(row, dict)]
    if cross_rows and any(row.get("value") is not None or row.get("change_pct") is not None for row in cross_rows):
        cross_market["items"] = [
            [row.get("symbol"), row.get("value"), row.get("change_pct"), row.get("status")]
            for row in cross_rows
        ]
        cross_market["item_columns"] = "symbol,value,change_pct,status"
    elif cross_rows:
        cross_market["missing_symbols"] = [str(row.get("symbol") or "UNKNOWN") for row in cross_rows]

    return {
        "status": str(raw.get("status") or "NO_DATA").upper(),
        "cvd": cvd,
        "open_interest": oi,
        "derivatives_matrix_columns": "symbol,gate_status,gate_oi_pct,gate_funding_pct,binance_status,binance_oi_pct,binance_funding_pct,crowding_score,label,price_pct",
        "derivatives_matrix": matrix,
        "liquidations": liquidations,
        "onchain": onchain,
        "cross_market": cross_market,
    }


def _load_market_radar_snapshot(store: Any, symbols: tuple[str, ...], now: datetime) -> dict[str, Any]:
    """Read the existing radar once; a failed optional feed never becomes a trade veto."""
    try:
        from ..analysis.market_radar import build_market_radar

        radar = build_market_radar(
            store,
            symbols=symbols[:MAX_UNIVERSE_SYMBOLS],
            now=now,
            include_external=True,
        )
        return _compact_market_radar(radar, symbols)
    except Exception:
        logger.warning("Market radar unavailable for AI decision", exc_info=True)
        unavailable = {"status": "UNAVAILABLE", "source": None, "as_of": None}
        return _compact_market_radar({
            "status": "UNAVAILABLE",
            "generated_at": _iso(now),
            "cvd": {**unavailable, "synthetic": None},
            "open_interest": {**unavailable, "binance_status": "UNAVAILABLE", "synthetic": None},
            "liquidations": {**unavailable, "window_hours": 24, "counts": {}, "estimated_notional": {}, "recent": [], "synthetic": None},
            "onchain": {**unavailable, "events": [], "synthetic": None},
            "cross_market": {"status": "CONFIG_REQUIRED", "source": None, "as_of": None, "message": "跨市场数据源未配置。", "items": []},
        }, symbols)


def _load_verified_gate_derivatives(store: Any, symbols: tuple[str, ...], now: datetime) -> dict[str, dict[str, Any]]:
    """Read fresh Gate public OI/funding rows for the NOFX indicator snapshot.

    ``verified`` is added only after reading the project-owned Gate tables,
    requiring provider=gate, finite values, and bounded event age. Arbitrary
    caller input and stale rows cannot claim verified provenance.
    """
    output: dict[str, dict[str, Any]] = {}
    if not hasattr(store, "_connect") or not symbols:
        return output
    specs = (
        ("open_interest", "gate_open_interest", "open_interest", 20 * 60, "contracts"),
        ("funding_rate", "gate_funding_history", "funding_rate", 12 * 60 * 60, "decimal_fraction"),
    )
    try:
        with store._connect() as db:
            tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for symbol in symbols[:MAX_UNIVERSE_SYMBOLS]:
                records: dict[str, Any] = {}
                for name, table, column, max_age, unit in specs:
                    if table not in tables:
                        continue
                    row = db.execute(
                        f"SELECT provider, environment, event_at, {column} AS value FROM {table} "
                        "WHERE symbol=? AND LOWER(provider)='gate' ORDER BY event_at DESC LIMIT 1",
                        (symbol.upper(),),
                    ).fetchone()
                    if row is None:
                        continue
                    event_at = row["event_at"]
                    observed = _parse_time(event_at)
                    if observed is None and event_at is not None:
                        try:
                            epoch = float(event_at)
                            if math.isfinite(epoch):
                                if epoch > 10_000_000_000:
                                    epoch /= 1000.0
                                observed = datetime.fromtimestamp(epoch, timezone.utc)
                        except (TypeError, ValueError, OverflowError, OSError):
                            observed = None
                    value = row["value"]
                    if (
                        observed is None or observed > now + timedelta(seconds=30)
                        or (now - observed).total_seconds() > max_age
                        or isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                    ):
                        continue
                    records[name] = {
                        "status": "AVAILABLE",
                        "verified": True,
                        "source": f"gate_public_derivatives_rest:{str(row['environment'] or 'unknown').lower()}",
                        "as_of": observed.isoformat(),
                        "value": float(value),
                        "unit": unit,
                    }
                if records:
                    output[symbol.upper()] = records
    except Exception:
        logger.warning("Verified Gate derivative inputs unavailable", exc_info=True)
    return output


def _compact_decision_experience(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    recent_decisions: list[dict[str, Any]] = []
    settled: list[dict[str, Any]] = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        decision = {
            "decision_at": row.get("decision_at"),
            "action": row.get("action"),
            "status": row.get("cycle_status") or row.get("status"),
            "symbol": row.get("symbol"),
            "strategy_template_id": str(payload.get("strategy_template_id") or "custom")[:32],
        }
        recent_decisions.append({key: value for key, value in decision.items() if value is not None})
        if str(row.get("action") or "").upper() not in {"OPEN_LONG", "OPEN_SHORT"}:
            continue
        outcome = str(row.get("outcome_status") or "").upper()
        if outcome not in {"WIN", "LOSS", "FLAT"}:
            continue
        try:
            pnl = float(row.get("outcome_pnl"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pnl):
            continue
        settled.append({
            "decision_at": str(row.get("decision_at") or "")[:20],
            "symbol": str(row.get("symbol") or "UNKNOWN")[:24],
            "strategy_template_id": str(payload.get("strategy_template_id") or "custom")[:32],
            "outcome_status": outcome,
            "net_pnl_usdt": pnl,
            "lesson_zh": str(row.get("lesson_zh") or "")[:32],
        })

    wins = sum(item["outcome_status"] == "WIN" for item in settled)
    losses = sum(item["outcome_status"] == "LOSS" for item in settled)
    flats = sum(item["outcome_status"] == "FLAT" for item in settled)
    strategies: dict[str, list[dict[str, Any]]] = {}
    for item in settled:
        strategies.setdefault(item["strategy_template_id"], []).append(item)
    by_strategy = []
    for template_id, outcomes in sorted(strategies.items(), key=lambda item: (-len(item[1]), item[0]))[:4]:
        strategy_wins = sum(item["outcome_status"] == "WIN" for item in outcomes)
        strategy_losses = sum(item["outcome_status"] == "LOSS" for item in outcomes)
        denominator = strategy_wins + strategy_losses
        by_strategy.append({
            "strategy_template_id": template_id,
            "settled_count": len(outcomes),
            "win_rate_pct": round(strategy_wins * 100 / denominator, 1) if denominator else None,
            "net_realized_pnl_usdt": round(sum(item["net_pnl_usdt"] for item in outcomes), 4),
        })
    experience = {
        "status": "AVAILABLE" if settled else "NO_SETTLED_OUTCOMES",
        "sample_size": len(settled),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate_pct": round(wins * 100 / (wins + losses), 1) if wins + losses else None,
        "net_realized_pnl_usdt": round(sum(item["net_pnl_usdt"] for item in settled), 4),
        "by_strategy": by_strategy,
        "recent_closed_trades": settled[:3],
        "interpretation": "历史已结算样本，仅作复盘参考，不代表未来胜率。" if settled else "尚无可核验的已结算开仓样本。",
    }
    return recent_decisions[:1], experience


def _compact_position(position: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "position_id", "instrument_id", "symbol", "side", "direction", "quantity", "size",
        "contracts", "entry_price", "mark_price", "current_price", "unrealized_pnl",
        "realized_pnl", "leverage", "liquidation_price", "stop_price", "stop_loss",
        "take_profit", "notional", "margin", "opened_at", "status",
    )
    return {key: position[key] for key in fields if key in position and position[key] is not None}


def _compact_news_revision(revision: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: revision[key]
        for key in ("revision_id", "scope", "symbol", "published_at", "known_at", "source", "impact")
        if key in revision and revision[key] is not None
    }
    if revision.get("symbols") and (
        str(revision.get("scope") or "").upper() == "MARKET_WIDE" or not result.get("symbol")
    ):
        result["symbols"] = list(dict.fromkeys(str(value) for value in revision["symbols"] if str(value).strip()))[:8]
    result["title"] = str(revision.get("title") or "")[:56]
    result["summary"] = str(revision.get("summary") or "")[:56]
    return result


def _select_prompt_news(revisions: list[Any], symbols: tuple[str, ...], *, limit: int = 4) -> list[dict[str, Any]]:
    rows = [item for item in revisions if isinstance(item, dict) and item.get("revision_id")]
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        identity = str(row.get("revision_id") or "")
        if identity and identity not in seen and len(selected) < limit:
            seen.add(identity)
            selected.append(row)

    # Preserve at least one direct event per scanned symbol before filling the
    # remaining prompt budget with shared macro context and newer revisions.
    for symbol in symbols:
        for row in rows:
            raw_symbols = row.get("symbols")
            row_symbols = (
                {str(value).upper() for value in raw_symbols if str(value).strip()}
                if isinstance(raw_symbols, (list, tuple))
                else set()
            )
            if str(row.get("scope") or "SYMBOL").upper() == "SYMBOL" and (
                str(row.get("symbol") or "").upper() == symbol
                or symbol in row_symbols
            ):
                add(row)
                break
    for row in rows:
        if str(row.get("scope") or "").upper() == "MARKET_WIDE":
            add(row)
            if sum(str(item.get("scope") or "").upper() == "MARKET_WIDE" for item in selected) >= 1:
                break
    for row in rows:
        add(row)
    return [_compact_news_revision(row) for row in selected]


def _focused_entry_repair_inputs(inputs: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Keep the frozen facts needed to complete an existing OPEN, one asset only.

    The first completion has already chosen action, price and size. Repeating
    the entire multi-asset decision payload for missing analysis fields wastes
    the local model's 55-second transport window. This projection contains no
    new facts and the immutable-field check still forbids a changed trade.
    """
    market = inputs.get("market_snapshots") or {}
    technical = inputs.get("technical_context") or {}
    news = [
        row for row in (inputs.get("news_revisions") or [])
        if isinstance(row, dict) and (
            str(row.get("scope") or "").upper() == "MARKET_WIDE"
            or str(row.get("symbol") or "").upper() == symbol
            or symbol in {str(item).upper() for item in (row.get("symbols") or [])}
        )
    ][:4]
    news_ids = {str(row.get("revision_id") or "") for row in news}
    refs = [
        ref for ref in (inputs.get("evidence_refs") or [])
        if isinstance(ref, str) and (
            ref.startswith((f"market_snapshot:{symbol}:", f"technical_snapshot:{symbol}:"))
            or (ref.startswith("news_revision:") and ref.split(":", 1)[1] in news_ids)
        )
    ]
    return {
        "repair_scope": "COMPLETE_ANALYSIS_FOR_EXISTING_OPEN_ONLY",
        "account_id": inputs.get("account_id"),
        "mode": inputs.get("mode"),
        "venue": inputs.get("venue"),
        "allowed_instruments": [symbol],
        "market_snapshots": {symbol: market[symbol]} if symbol in market else {},
        "technical_context": {
            key: technical[key]
            for key in ("indicator_columns", "candle_columns", symbol)
            if key in technical
        },
        "news_coverage_status": "RECENT_REVISIONS_INCLUDED" if news else "NO_RECENT_RELEVANT_REVISION_IN_INPUT",
        "news_revisions": news,
        "evidence_refs": refs,
    }


def _model_stop_repair_bounds(context: AICycleContext, decoded: dict[str, Any]) -> dict[str, float] | None:
    """Identify a model's narrow stop using the exact execution-side floor."""
    action = str(decoded.get("action") or "").upper()
    symbol = str(decoded.get("instrument_id") or "").upper()
    if action not in {"OPEN_LONG", "OPEN_SHORT"} or symbol not in context.allowed_instruments:
        return None
    try:
        entry = float(decoded["entry_price"])
        stop = float(decoded["stop_price"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (entry, stop)):
        return None
    profile = (context.strategy_instructions or {}).get("profile") or {}
    timeframe = str(profile.get("signal_timeframe") or "15m")
    frames = ((context.technical_context.get(symbol) or {}).get("timeframes") or {})
    minimum = minimum_stop_distance(entry, symbol, frames, profile, timeframe)
    if abs(entry - stop) >= minimum - max(entry * 1e-10, 1e-12):
        return None
    boundary = entry - minimum if action == "OPEN_LONG" else entry + minimum
    return {
        "entry_price": entry,
        "proposed_stop_price": stop,
        "minimum_stop_distance": round(minimum, 10),
        "stop_boundary": round(boundary, 10),
        "minimum_net_rr": float(((context.strategy_instructions or {}).get("execution") or {}).get("min_net_rr") or POLICY["min_net_reward_risk"]),
    }


def _model_limit_repair_bounds(context: AICycleContext, decoded: dict[str, Any]) -> dict[str, Any] | None:
    """Detect a limit that would cross the frozen executable quote."""
    action = str(decoded.get("action") or "").upper()
    symbol = str(decoded.get("instrument_id") or "").upper()
    if action not in {"OPEN_LONG", "OPEN_SHORT"} or symbol not in context.allowed_instruments:
        return None
    if str(decoded.get("order_preference") or "AUTO").upper() != "LIMIT":
        return None
    try:
        quote = float((context.market_snapshots.get(symbol) or {}).get("price"))
        limit = float(decoded["limit_price"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) and value > 0 for value in (quote, limit)):
        return None
    profile = (context.strategy_instructions or {}).get("profile") or {}
    try:
        max_distance = float(profile.get("max_limit_distance_pct") or 0)
    except (TypeError, ValueError):
        max_distance = 0.0
    crosses = limit > quote if action == "OPEN_LONG" else limit < quote
    too_far = max_distance > 0 and abs(limit - quote) / quote * 100 > max_distance
    if not crosses and not too_far:
        return None
    return {
        "quote": quote,
        "proposed_limit_price": limit,
        "passive_side": "AT_OR_BELOW_QUOTE" if action == "OPEN_LONG" else "AT_OR_ABOVE_QUOTE",
        "max_distance_pct": max_distance,
        "reason": "CROSSES_QUOTE" if crosses else "LIMIT_TOO_FAR",
    }


def _model_net_rr_repair_bounds(context: AICycleContext, decoded: dict[str, Any]) -> dict[str, float] | None:
    """Give Bonsai the exact cost-adjusted target bound for its own passive entry."""
    action = str(decoded.get("action") or "").upper()
    symbol = str(decoded.get("instrument_id") or "").upper()
    if action not in {"OPEN_LONG", "OPEN_SHORT"} or symbol not in context.allowed_instruments:
        return None
    profile = (context.strategy_instructions or {}).get("profile") or {}
    if not profile.get("allow_future_limit"):
        return None
    snap = context.market_snapshots.get(symbol) or {}
    market = snap.get("market") if isinstance(snap.get("market"), dict) else {}
    try:
        entry = float(decoded.get("limit_price") if decoded.get("limit_price") is not None else decoded["entry_price"])
        quote = float(snap["price"])
        stop = float(decoded["stop_price"])
        target = float(decoded["take_profit"])
        fee = float(snap.get("fee_rate") if snap.get("fee_rate") is not None else market.get("taker"))
        slip = float(snap["slippage"])
        minimum = float(((context.strategy_instructions or {}).get("execution") or {}).get("min_net_rr") or POLICY["min_net_reward_risk"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (entry, quote, stop, target, fee, slip, minimum)):
        return None
    if min(entry, stop, target, minimum) <= 0 or fee < 0 or not 0 <= slip < 1:
        return None
    sign = 1 if action == "OPEN_LONG" else -1
    preference = str(decoded.get("order_preference") or "AUTO").upper()
    if preference != "LIMIT":
        if preference != "AUTO" or not profile.get("limit_priority"):
            return None
        max_distance_pct = number(profile.get("max_limit_distance_pct"))
        if (sign == 1 and entry >= quote) or (sign == -1 and entry <= quote):
            return None
        if max_distance_pct is not None and abs(entry - quote) / quote * 100 > max_distance_pct:
            return None
    if sign * (entry - stop) <= 0 or sign * (target - entry) <= 0:
        return None
    loss = sign * (entry - stop) + stop * slip + (entry + stop) * fee
    reward = sign * (target - entry) - target * slip - (entry + target) * fee
    if loss <= 0 or reward / loss + NET_RR_ROUNDING_TOLERANCE >= minimum:
        return None
    if sign == 1:
        denominator = 1 - slip - fee
        bound = (minimum * loss + entry * (1 + fee)) / denominator if denominator > 0 else float("nan")
    else:
        denominator = 1 + slip + fee
        bound = (entry * (1 - fee) - minimum * loss) / denominator
    if not math.isfinite(bound) or bound <= 0:
        return None
    return {
        "entry_price": entry,
        "stop_price": stop,
        "proposed_target": target,
        "observed_net_rr": round(reward / loss, 6),
        "minimum_net_rr": minimum,
        "required_target_bound": round(bound, 10),
    }


def _wait_limit_distance_contradiction(prompt_payload: dict[str, Any], decoded: dict[str, Any]) -> dict[str, Any] | None:
    """Catch a WAIT that calls a verified in-range EMA limit out of range."""
    if str(decoded.get("action") or "").upper() != "WAIT":
        return None
    symbol = str(decoded.get("instrument_id") or "").upper()
    strategy = prompt_payload.get("active_strategy") or {}
    geometry = (strategy.get("risk_geometry") or {}).get(symbol)
    if not isinstance(geometry, dict) or geometry.get("ema20_within_limit_distance") is not True:
        return None
    try:
        maximum = float(geometry.get("max_limit_distance_pct"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(maximum) or maximum <= 0:
        return None
    reason = str(decoded.get("reason") or "")
    ticker = symbol[:-4] if symbol.endswith("USDT") else symbol
    start = reason.upper().find(ticker)
    if start < 0:
        return None
    # Only inspect the chosen asset's first claim, not a different asset's
    # legitimately distant EMA later in a multi-symbol explanation.
    segment = reason[start:start + 180]
    cap = re.escape(format(maximum, "g"))
    if re.search(rf"(?:超|超过|超出)\s*{cap}\s*%", segment) is None:
        return None
    return {
        "symbol": symbol,
        "quote": geometry.get("quote"),
        "ema20": geometry.get("ema20_example_entry"),
        "verified_distance_pct": geometry.get("ema20_limit_distance_pct"),
        "max_limit_distance_pct": maximum,
    }


def _select_prompt_candidates(candidates: list[Any], symbols: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = [item for item in candidates if isinstance(item, dict)]
    rows.sort(
        key=lambda row: (
            0 if str(row.get("status") or "").upper() in {"PROPOSAL", "READY"} else 1,
            -float(row.get("trigger_completion_pct") or 0)
            if isinstance(row.get("trigger_completion_pct"), (int, float)) else 0,
            -float((row.get("proposal") or {}).get("rule_score") or 0)
            if isinstance(row.get("proposal"), dict) and isinstance((row.get("proposal") or {}).get("rule_score"), (int, float))
            else 0,
        )
    )
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for symbol in symbols:
        for row in rows:
            if str(row.get("symbol") or "").strip().upper() == symbol and id(row) not in used:
                selected.append(row)
                used.add(id(row))
                break
    if not symbols:
        selected = rows[:6]
    return selected


def _require_relevant_news_ref_for_open(
    decoded: object, prompt_news: list[dict[str, Any]], now: datetime,
) -> None:
    """Let the one model repair supply a missing verified news citation.

    This is a model-authored analysis requirement, not a Python-made trading
    decision. The execution validator independently checks the same citation.
    """
    if not isinstance(decoded, dict) or decoded.get("action") not in {"OPEN_LONG", "OPEN_SHORT"}:
        return
    symbol = str(decoded.get("instrument_id") or "").upper()
    required = {
        f"news_revision:{row.get('revision_id')}"
        for row in prompt_news
        if isinstance(row, dict) and row.get("revision_id")
        and relevant_news_for_entry(row, symbol, now)
    }
    if required and not required.intersection(decoded.get("evidence_refs") or []):
        raise ValueError("INVALID_ACTION_SCHEMA:OPEN_CONTEXT_REQUIRED:news_evidence_ref")


class AISessionCoordinator:
    """Bounded, cancellable strategy cycle owner for one trading session."""

    def __init__(
        self,
        *,
        store: Any,
        service: Any,
        session_manager: Any,
        ledger: AccountLedger,
        guardian: PositionGuardian,
        execution_gateway: ExecutionGateway | None = None,
        risk_engine: RiskEngine | None = None,
        model_provider: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        cycle_interval_seconds: float = DEFAULT_CYCLE_INTERVAL_SECONDS,
        model_budget_seconds: float = MODEL_BUDGET_SECONDS,
        calibration_min_bars: int = 500,
        calibration_lookback_days: int = 30,
    ) -> None:
        self.store = store
        self.service = service
        self.session_manager = session_manager
        self.ledger = ledger
        self.guardian = guardian
        self.gateway = execution_gateway or ExecutionGateway(store, ledger=ledger)
        self.risk_engine = risk_engine or RiskEngine(ledger)
        self.model_provider = model_provider if model_provider is not None else self._provider_from_service(service)
        from ..ai.ollama import OllamaProvider
        if isinstance(self.model_provider, OllamaProvider):
            # Dedicated session settings do not mutate consultation/translation.
            original = self.model_provider
            self.model_provider = OllamaProvider(
                base_url=original.base_url, model_name=DEFAULT_SMART_MODEL,
                timeout=DECISION_MODEL_REQUEST_TIMEOUT_SECONDS, context_length=_session_context_length(), max_tokens=DECISION_OUTPUT_TOKEN_BUDGET,
                temperature=0, retries=0, quantization=original.quantization, think=False,
                keep_alive=_session_keep_alive(),
            )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cycle_interval_seconds = max(MIN_CYCLE_INTERVAL_SECONDS, min(float(cycle_interval_seconds), 3600.0))
        self.model_budget_seconds = max(0.1, min(float(model_budget_seconds), MODEL_BUDGET_SECONDS))
        self.calibration_min_bars = max(1, int(calibration_min_bars))
        self.calibration_lookback_days = max(1, int(calibration_lookback_days))

        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._inflight_future: Future[Any] | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aima-ai-cycle")
        self._account_id: str | None = None
        self._mode: TradingMode | None = None
        self._venue = "simulated"
        self._enabled = False
        self._last_reason = "not_started"
        self._last_error: str | None = None
        self._last_cycle_id: str | None = None
        self._last_cycle_status: str | None = None
        self._last_operational_state: str | None = None
        self._last_cycle_at: str | None = None
        self._health_cache: dict[str, Any] | None = None
        self._health_checked_at: datetime | None = None
        self._active_context: AICycleContext | None = None
        self._active_future: Future[Any] | None = None
        self._calibration = AICalibrationService(store, clock=self.clock)
        self._scanner = CandidateScanner(store, clock=self.clock)
        self._strategy_book = AIStrategyBook(store)
        self._schedule = StrategySchedule(store)
        self._market_universe = MarketUniverse()
        self._calibration_state = "NOT_STARTED"
        self._calibration_run: dict[str, Any] | None = None
        self._last_scheduled_at: str | None = None
        self._last_started_at: str | None = None
        self._last_completed_at: str | None = None
        self._last_lag_ms: float | None = None
        self._last_duration_ms: float | None = None
        self._next_scan_at: str | None = None
        self._candidate_count = 0
        self._current_symbol_limit = INITIAL_DEEP_SCAN_SYMBOLS
        self._ensure_tables()

    @staticmethod
    def _provider_from_service(service: Any) -> Any | None:
        provider = getattr(service, "llm_provider", None)
        if provider is not None:
            return provider
        smart = getattr(service, "smart", None)
        return getattr(smart, "llm_provider", None) if smart is not None else None

    def _active_strategy(self, account_id: str | None) -> dict[str, Any]:
        if not account_id:
            return {}
        book = getattr(self, "_strategy_book", None)
        if book is None:
            book = AIStrategyBook(self.store)
            self._strategy_book = book
        try:
            return book.active(account_id)
        except Exception:
            logger.exception("Failed to load active AI strategy for %s", account_id)
            return {}

    def _strategy_scan_minutes(self, account_id: str | None) -> int:
        strategy = self._active_strategy(account_id)
        nofx_runtime = strategy.get("nofx_runtime") if isinstance(strategy.get("nofx_runtime"), dict) else {}
        nofx_timeframe = str(nofx_runtime.get("signal_timeframe") or "").strip().lower()
        if nofx_timeframe in {"5m", "15m"}:
            return int(nofx_timeframe[:-1])
        profile = strategy.get("profile") if isinstance(strategy.get("profile"), dict) else {}
        signal_timeframe = str(profile.get("signal_timeframe") or "").strip().lower()
        if signal_timeframe in {"5m", "15m"}:
            return int(signal_timeframe[:-1])
        execution = strategy.get("execution") if isinstance(strategy.get("execution"), dict) else {}
        try:
            interval = int(execution.get("scan_interval_minutes"))
        except (TypeError, ValueError):
            interval = 15
        return interval if interval in {5, 15} else 15

    @staticmethod
    def _strategy_scan_contract(strategy: dict[str, Any]) -> tuple[str, tuple[str, ...], tuple[str, ...] | None]:
        profile = strategy.get("profile") if isinstance(strategy.get("profile"), dict) else {}
        nofx_runtime = strategy.get("nofx_runtime") if isinstance(strategy.get("nofx_runtime"), dict) else {}
        signal_timeframe = str(nofx_runtime.get("signal_timeframe") or profile.get("signal_timeframe") or "").strip().lower()
        if signal_timeframe not in {"5m", "15m"}:
            execution = strategy.get("execution") if isinstance(strategy.get("execution"), dict) else {}
            signal_timeframe = f"{execution.get('scan_interval_minutes', 15)}m"
            if signal_timeframe not in {"5m", "15m"}:
                signal_timeframe = "15m"
        raw_context = nofx_runtime.get("context_timeframes") if isinstance(nofx_runtime.get("context_timeframes"), (list, tuple)) else profile.get("context_timeframes")
        context_timeframes = tuple(
            dict.fromkeys(
                str(item).strip().lower()
                for item in raw_context
                if str(item).strip().lower() in {"5m", "15m", "1h", "8h", "1d"}
                and str(item).strip().lower() != signal_timeframe
            )
        ) if isinstance(raw_context, (list, tuple)) else None
        raw_ids = profile.get("candidate_strategy_ids")
        strategy_ids = tuple(
            dict.fromkeys(str(item).strip() for item in raw_ids if str(item).strip())
        ) if isinstance(raw_ids, (list, tuple)) else None
        return signal_timeframe, context_timeframes, strategy_ids

    def _next_aligned_scan(self, value: datetime, minutes: int = 15) -> datetime:
        return aligned_at(_as_utc(value), minutes, next_slot=True)

    def _aligned_scan_at(self, value: datetime, minutes: int = 15) -> datetime:
        return aligned_at(_as_utc(value), minutes)

    def _evaluate_cycle_dynamic_risk(
        self,
        account_id: str,
        strategy: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Evaluate executable account locks from this cycle's strategy snapshot.

        The same result is shown to Bonsai and enforced again after inference,
        immediately before the sole AI-led execution gateway. Missing or
        malformed policy/evidence is never represented as an affirmative
        entry authorization.
        """
        raw_execution = strategy.get("execution") if isinstance(strategy, dict) else None
        try:
            from .strategy_execution import normalize_execution

            execution = normalize_execution(raw_execution)
            result = evaluate_dynamic_risk(self.store, account_id, execution, now=now)
        except Exception as exc:
            logger.exception("Dynamic risk evaluation failed for account %s", account_id)
            return {
                "status": "UNAVAILABLE",
                "entry_allowed": False,
                "reasons": ["DYNAMIC_RISK_EVALUATION_UNAVAILABLE"],
                "blocked_until": None,
                "evaluated_at": _iso(now),
                "atr_adaptive_sizing": {
                    "enabled": None,
                    "status": "UNAVAILABLE",
                },
                "evidence": {},
                "evidence_status": "UNAVAILABLE",
                "evidence_source": "account_trade_fills.realized_exit_payload",
                "error_code": type(exc).__name__,
                "enforcement": "COORDINATOR_PRE_EXECUTION_OPEN_GATE",
            }

        exits = (result.get("evidence") or {}).get("recent_authoritative_exits")
        if not isinstance(exits, list):
            exits = []
        if not execution.get("consecutive_loss_lock_enabled", True):
            loss_evidence_status = "DISABLED_BY_STRATEGY"
        elif len(exits) < 2:
            # No qualifying local, realized reduce-only exit evidence means
            # the two-loss lock cannot be inferred; do not fabricate losses.
            result.setdefault("evidence", {})["consecutive_loss"] = "NO_AUTHORITATIVE_EXIT_EVIDENCE"
            loss_evidence_status = "NO_AUTHORITATIVE_EXIT_EVIDENCE"
        else:
            loss_evidence_status = "AUTHORITATIVE_RECENT_EXIT_EVIDENCE"
        result.update({
            "evidence_status": loss_evidence_status,
            "evidence_source": "account_trade_fills.reduce_only_realized_pnl",
            "strategy_revision": (strategy or {}).get("revision") if isinstance(strategy, dict) else None,
            "strategy_template_id": (strategy or {}).get("template_id") if isinstance(strategy, dict) else None,
            "enforcement": "COORDINATOR_PRE_EXECUTION_OPEN_GATE",
        })
        atr = result.get("atr_adaptive_sizing")
        if isinstance(atr, dict):
            atr["source"] = "ai_led_engine.size_position.stop_distance_and_risk_budget"
            atr["sizing_contract"] = "ATR-informed stop distance scales quantity against fixed risk budget"
        return result

    def _execute_model_decision(
        self,
        context: AICycleContext,
        output: AIActionOutput,
        *,
        now: datetime,
    ) -> Any:
        """Enforce account dynamic locks only on new positions.

        Position reductions, closes and stop tightening continue through the
        normal engine while an entry lock is active.
        """
        action = str(getattr(output, "action", "") or "").strip().upper()
        risk = context.dynamic_risk if isinstance(context.dynamic_risk, dict) else {}
        margin_only_gate = (
            context.decision_contract == CONTRACT
            and str(context.venue or "").lower() == "gate"
            and str(context.mode.value if isinstance(context.mode, TradingMode) else context.mode).upper() in {"TESTNET", "LIVE"}
        )
        # Persist the evaluated facts alongside the model output so the
        # decision timeline can explain both the advice and any system gate.
        output.extra_fields = dict(getattr(output, "extra_fields", {}) or {})
        output.extra_fields["dynamic_risk"] = risk
        if action in ENTRY_ACTIONS and not margin_only_gate:
            if risk.get("entry_allowed") is not True:
                reasons = risk.get("reasons") if isinstance(risk.get("reasons"), list) else []
                reason = "DYNAMIC_RISK_ENTRY_BLOCKED: " + (
                    ",".join(str(item) for item in reasons if str(item).strip())
                    or "DYNAMIC_RISK_EVIDENCE_UNAVAILABLE"
                )
                result = self._blocked_cycle(context, reason)
                result.status = "BLOCKED"
                result.reason = reason
                result.operational_state = "SYSTEM_BLOCKED"
                self._correct_persisted_outcome(result)
                return result

        engine = AILedDecisionEngine(
            store=self.store,
            execution_gateway=self.gateway,
            risk_engine=self.risk_engine,
            ledger=self.ledger,
            guardian=self.guardian,
            model_runner=None,
        )
        return engine.execute_cycle(context, now=now, model_output=output)

    def _select_market_universe(
        self,
        strategy: dict[str, Any],
        *,
        mode: TradingMode,
        scheduled_at: datetime,
        positions: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        account_id: str | None = None,
        symbol_limit: int = MAX_UNIVERSE_SYMBOLS,
    ) -> dict[str, Any]:
        execution = strategy.get("execution") if isinstance(strategy.get("execution"), dict) else {}
        if mode is TradingMode.PAPER:
            # Local paper accounts do not have a venue-owned contract list.
            # Keep them on explicitly configured/locally monitored symbols and
            # never make a public exchange request as a hidden paper fallback.
            configured = [
                str(item).strip().upper()
                for item in execution.get("symbols", [])
                if str(item).strip()
            ] if isinstance(execution.get("symbols"), (list, tuple)) else []
            if str(execution.get("universe_mode") or "ALL").upper() == "CUSTOM":
                local_symbols = configured
            else:
                local_symbols = list(self._allowed_symbols(None))
            try:
                local_positions = self.ledger.get_open_positions(
                    account_id or self._account_id or "",
                    venue="simulated",
                    mode=TradingMode.PAPER.value,
                ) if account_id or getattr(self, "_account_id", None) else []
            except Exception:
                local_positions = []
            held = [
                str(item.get("instrument_id") or item.get("symbol") or "").strip().upper()
                for item in [*positions, *local_positions]
                if isinstance(item, dict)
            ]
            selected = list(dict.fromkeys([*held, *local_symbols]))[:symbol_limit]
            return {
                "status": "READY" if selected else "EMPTY",
                "environment": "PAPER",
                "contract_count": len(set(local_symbols)),
                "eligible_count": len(set(local_symbols)),
                "selected_symbols": selected,
                "mode": str(execution.get("universe_mode") or "ALL").upper(),
                "selection": "local_paper_positions_and_configured_symbols",
                "candidate_metrics": [],
            }

        universe = getattr(self, "_market_universe", None)
        if universe is None:
            universe = MarketUniverse()
            self._market_universe = universe
        return universe.select(
            execution,
            testnet=mode is TradingMode.TESTNET,
            scheduled_at=scheduled_at,
            positions=positions,
            limit=symbol_limit,
            nofx_runtime=strategy.get("nofx_runtime") if isinstance(strategy.get("nofx_runtime"), dict) else None,
        )

    def _ensure_tables(self) -> None:
        try:
            with self.store._connect() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS ai_led_cycles (
                        cycle_id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        latency_ms REAL,
                        order_intent_id TEXT,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                columns = {str(row[1]) for row in db.execute("PRAGMA table_info(ai_led_cycles)").fetchall()}
                for column, definition in (("session_id", "TEXT"), ("generation", "INTEGER"), ("authorization_id", "TEXT"), ("market_snapshot_hash", "TEXT")):
                    if column not in columns:
                        db.execute(f"ALTER TABLE ai_led_cycles ADD COLUMN {column} {definition}")
                ensure_institutional_trader_schema(db)
        except Exception:
            logger.exception("could not initialize AI cycle persistence")

    def _account_record(self, account_id: str) -> dict[str, Any] | None:
        with self.store._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["config"] = json.loads(result.get("config_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["config"] = {}
        return result

    def _configure_scope(self, account_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
        if not account_id:
            return None, "ACCOUNT_REQUIRED"
        canonical = canonical_account_id(self.store, str(account_id))
        account = self._account_record(canonical)
        if account is None:
            return None, "ACCOUNT_NOT_FOUND"
        try:
            scope = resolve_account_scope(self.store, canonical)
            mode = TradingMode(str((scope or {}).get("mode") or account.get("mode", "")).upper())
        except ValueError:
            return None, "ACCOUNT_MODE_INVALID"
        config = account.get("config") if isinstance(account.get("config"), dict) else {}
        venue = str((scope or {}).get("venue") or config.get("provider") or config.get("venue") or ("simulated" if mode is TradingMode.PAPER else "gate")).strip() or "simulated"
        if scope:
            account["execution_scope"] = scope
        with self._lock:
            self._account_id = str(canonical)
            self._mode = mode
            self._venue = venue
            self._calibration_state = "CALIBRATING"
            self._calibration_run = None
        return account, None

    def _health(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            provider = self.model_provider
            checked = self._health_checked_at
            cached = dict(self._health_cache or {})
        now = _as_utc(self.clock())
        if not force:
            if cached and checked and (now - checked).total_seconds() < 5:
                return cached
        if provider is None:
            try:
                from core.model_client import model_client
                if model_client.is_healthy():
                    from ..ai.ollama import OllamaProvider
                    provider = OllamaProvider(
                        base_url="http://127.0.0.1:8080/v1",
                        model_name=DEFAULT_SMART_MODEL,
                        timeout=DECISION_MODEL_REQUEST_TIMEOUT_SECONDS,
                        context_length=_session_context_length(),
                        max_tokens=DECISION_OUTPUT_TOKEN_BUDGET,
                        temperature=0,
                        retries=0,
                        think=False,
                        keep_alive=_session_keep_alive(),
                    )
                    with self._lock:
                        self.model_provider = provider
            except Exception:
                provider = None
        if provider is None or not callable(getattr(provider, "health", None)):
            result = {
                "status": "UNAVAILABLE",
                "provider": None,
                "required_model": DEFAULT_SMART_MODEL,
                "available": False,
                "model_available": False,
                "reason_code": "SMART_MODEL_UNAVAILABLE",
                "checked_at": _iso(now),
            }
        else:
            try:
                try:
                    raw = provider.health(model_name=DEFAULT_SMART_MODEL)
                except TypeError:
                    raw = provider.health()
                raw = raw if isinstance(raw, dict) else {}
                model_available = bool(raw.get("model_available")) and (
                    str(raw.get("model_id") or "") == DEFAULT_SMART_MODEL
                )
                available = bool(raw.get("available"))
                try:
                    context_length = int(raw.get("context_length") or getattr(provider, "context_length", 0) or 0)
                except (TypeError, ValueError):
                    context_length = 0
                if hasattr(provider, "context_length"):
                    # The provider manifest is authoritative. A configured
                    # client window must never overrule the running server.
                    try:
                        provider.context_length = context_length
                    except (AttributeError, TypeError):
                        pass
                status = "READY" if available and model_available and context_length > 0 else "UNAVAILABLE"
                reason_code = (
                    None if status == "READY"
                    else str(raw.get("error_code") or ("MODEL_CONTEXT_UNKNOWN" if available and model_available else "SMART_MODEL_UNAVAILABLE"))
                )
                result = {
                    **raw,
                    "status": status,
                    "provider": raw.get("provider") or getattr(provider, "provider_name", provider.__class__.__name__),
                    "required_model": DEFAULT_SMART_MODEL,
                    "available": available,
                    "model_available": model_available,
                    "reason_code": reason_code,
                    "checked_at": _iso(now),
                }
            except Exception as exc:
                result = {
                    "status": "UNAVAILABLE",
                    "provider": getattr(provider, "provider_name", provider.__class__.__name__),
                    "required_model": DEFAULT_SMART_MODEL,
                    "available": False,
                    "model_available": False,
                    "reason_code": "SMART_MODEL_UNAVAILABLE",
                    "error": f"{type(exc).__name__}: {exc}",
                    "checked_at": _iso(now),
                }
        if result.get("status") == "UNAVAILABLE" and result.get("reason_code") in {"MODEL_UNAVAILABLE", "SMART_MODEL_UNAVAILABLE"}:
            if schedule_bonsai_recovery():
                result["recovery"] = "STARTING_LOCAL_BONSAI"
        with self._lock:
            self._health_cache = dict(result)
            self._health_checked_at = now
        return result

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            enabled = self._enabled
            account_id = self._account_id
            mode = self._mode.value if self._mode else None
            venue = self._venue
            reason = self._last_reason
            last_error = self._last_error
            last_cycle_id = self._last_cycle_id
            last_cycle_status = self._last_cycle_status
            last_cycle_at = self._last_cycle_at
            calibration_state = self._calibration_state
            calibration_run = dict(self._calibration_run or {}) if self._calibration_run else None
            last_scheduled_at = self._last_scheduled_at
            last_started_at = self._last_started_at
            last_completed_at = self._last_completed_at
            last_lag_ms = self._last_lag_ms
            last_duration_ms = self._last_duration_ms
            next_scan_at = self._next_scan_at
            candidate_count = self._candidate_count
        session = self.session_manager.status()
        health = self._health()
        if not enabled:
            state = "STOPPED"
        elif thread is not None and thread.is_alive():
            state = "RUNNING"
        else:
            state = "PAUSED"
        scope = resolve_account_scope(self.store, account_id) if account_id else None
        strategy = self._active_strategy(account_id)
        scan_interval_minutes = self._strategy_scan_minutes(account_id)
        scan_interval_seconds = scan_interval_minutes * 60
        return {
            "contract_version": AI_COORDINATOR_CONTRACT_VERSION,
            "state": state,
            "decision_contract": CONTRACT,
            "decision_policy": (
                {
                    **NOFX_GATE_POLICY,
                    "execution_route": f"AI_AUTHORED_GATE_{str(mode).upper()}",
                    "opening_checks": [
                        "managed_gate_account" if check == "managed_gate_testnet_account" else check
                        for check in NOFX_GATE_POLICY["opening_checks"]
                    ],
                    "max_margin_pct": (strategy.get("execution") or {}).get("max_margin_pct"),
                }
                if str(mode or "").upper() in {"TESTNET", "LIVE"} and str(venue or "").lower() == "gate"
                else dict(POLICY)
            ),
            "enabled": enabled,
            "worker_alive": bool(thread and thread.is_alive()),
            "account_id": account_id,
            "venue": venue,
            "mode": mode,
            "provider": (scope or {}).get("provider") if scope else venue,
            "environment": (scope or {}).get("environment") if scope else (mode.lower() if mode else None),
            "session_id": session.get("session_id"),
            "generation": session.get("generation"),
            "session_state": session.get("state"),
            # Keep the legacy field for existing clients, but report the
            # strategy-owned cadence that the worker actually schedules.
            "cycle_interval_seconds": scan_interval_seconds,
            "scan_interval_seconds": scan_interval_seconds,
            # Constructor configuration is retained for compatibility and
            # diagnostics only; it does not control the strategy scheduler.
            "legacy_cycle_interval_seconds": self.cycle_interval_seconds,
            "cycle_interval_source": "active_strategy_schedule",
            "schedule": {
                "timezone": "UTC",
                "alignment": f"minute % {scan_interval_minutes} == 0",
                "interval_minutes": scan_interval_minutes,
                "strategy_id": strategy.get("template_id"),
                "strategy_revision": strategy.get("revision"),
                "strategy_name": strategy.get("name"),
                "catch_up": False,
                "last_scheduled_at": last_scheduled_at,
                "last_started_at": last_started_at,
                "last_completed_at": last_completed_at,
                "last_lag_ms": last_lag_ms,
                "last_duration_ms": last_duration_ms,
                "next_scan_at": next_scan_at,
            },
            "max_symbols": MAX_CYCLE_SYMBOLS,
            "universe_symbol_limit": getattr(self, "_current_symbol_limit", INITIAL_DEEP_SCAN_SYMBOLS),
            "candidate_count": candidate_count,
            "calibration_state": calibration_state,
            "calibration_run": calibration_run,
            "model_budget_seconds": self.model_budget_seconds,
            "model": health,
            "last_reason": reason,
            "last_error": last_error,
            "last_cycle_id": last_cycle_id,
            "last_cycle_status": last_cycle_status,
            "last_operational_state": self._last_operational_state,
            "last_cycle_at": last_cycle_at,
        }

    def _allowed_symbols(self, authorization: Any | None = None) -> tuple[str, ...]:
        allowed = [str(item).strip().upper() for item in getattr(authorization, "allowed_instruments", []) if str(item).strip() and str(item).strip() != "*"]
        allow_all = authorization is None or any(str(item).strip() == "*" for item in getattr(authorization, "allowed_instruments", []))
        policies = []
        try:
            policies = [str(item.get("instrument_id") or "").strip().upper() for item in self.store.list_monitoring_policies(enabled=True)]
        except Exception:
            policies = []
        ordered = []
        # A global monitoring policy may wake the coordinator, but it cannot
        # expand the instruments granted by the account-scoped authorization.
        policy_scope = [symbol for symbol in policies if symbol and (allow_all or symbol in allowed)]
        for symbol in policy_scope + allowed:
            if symbol and symbol not in ordered:
                ordered.append(symbol)
        # Market scans must remain usable before an operator has made a
        # monitoring-policy record.  These are public Gate instruments, not
        # a fabricated market-data fallback.
        if not ordered and authorization is None:
            ordered.extend(("BTCUSDT", "ETHUSDT", "SOLUSDT"))
        return tuple(ordered[:MAX_CYCLE_SYMBOLS])

    def _market_snapshots(
        self,
        symbols: tuple[str, ...],
        *,
        now: datetime,
        timeframe: str = "15m",
        universe_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        snapshots: dict[str, dict[str, Any]] = {}
        metrics = (universe_snapshot or {}).get("candidate_metrics")
        universe_metrics = {
            str(item.get("symbol") or "").upper(): item
            for item in (metrics if isinstance(metrics, list) else [])
            if isinstance(item, dict)
        }
        with self._lock:
            scoped_mode = self._mode
        scoped_provider = None
        for symbol in symbols:
            raw: dict[str, Any] | None = None
            try:
                raw = self.store.get_realtime_state(symbol)
            except Exception:
                raw = None
            expected_public_environment = (
                "TESTNET_PUBLIC" if scoped_mode is TradingMode.TESTNET else "LIVE_PUBLIC"
            )
            if scoped_mode in {TradingMode.TESTNET, TradingMode.LIVE} and (
                not isinstance(raw, dict)
                or str(raw.get("market_data_environment") or raw.get("environment") or "").upper() != expected_public_environment
            ):
                # The app-scoped background monitor can overwrite a symbol's
                # global realtime row with LIVE_PUBLIC data between the AI
                # refresh and this read. Never let that quote price a TestNet
                # proposal; obtain account-environment evidence directly.
                try:
                    if scoped_provider is None:
                        from ..providers.gateio_provider import GatePublicProvider
                        scoped_provider = GatePublicProvider(testnet=scoped_mode is TradingMode.TESTNET)
                    market = scoped_provider.market(symbol)
                    ticker = scoped_provider._native_ticker(symbol)
                    observed = _as_utc(self.clock())
                    last = float(ticker.get("last"))
                    if not math.isfinite(last) or last <= 0:
                        raise ValueError("GATE_TICKER_INVALID")
                    raw = {
                        "symbol": symbol, "price": last,
                        "data_as_of": _iso(observed), "received_at": _iso(observed),
                        "source": (
                            "gate_testnet_native_rest_ticker"
                            if expected_public_environment == "TESTNET_PUBLIC"
                            else "gate_live_native_rest_ticker"
                        ),
                        "environment": expected_public_environment,
                        "market_data_environment": expected_public_environment,
                        "freshness_status": "fresh",
                        "market": {key: market[key] for key in (
                            "id", "symbol", "contractSize", "precision", "limits",
                            "leverage_max", "taker", "maker",
                        ) if key in market},
                    }
                    try:
                        raw.update(book_cost_evidence(scoped_provider.order_book(symbol, limit=20), last))
                    except Exception as exc:
                        raw.update(slippage=None, liquidity_ok=False,
                                   cost_evidence_status="UNAVAILABLE",
                                   cost_evidence_error=str(getattr(exc, "code", None) or type(exc).__name__))
                except Exception:
                    logger.warning("Gate scoped snapshot unavailable for %s", symbol, exc_info=True)
                    continue
            if raw and raw.get("price") is not None:
                snapshot = dict(raw)
                snapshot.setdefault("source", snapshot.get("provider", "realtime"))
            else:
                try:
                    bars = self.store.list_market_bars(symbol, timeframe, limit=1)
                except Exception:
                    bars = []
                if not bars:
                    continue
                bar = dict(bars[-1])
                snapshot = {
                    "symbol": symbol,
                    "price": bar.get("close"),
                    "data_as_of": bar.get("data_as_of") or bar.get("bar_end"),
                    "received_at": bar.get("received_at") or _iso(now),
                    "source": bar.get("provider") or "market_bars",
                    "freshness_status": "fresh",
                }
            try:
                price = float(snapshot.get("price"))
                data_as_of = _parse_time(snapshot.get("data_as_of") or snapshot.get("timestamp") or snapshot.get("bar_end"))
                received_at = _parse_time(snapshot.get("received_at") or snapshot.get("updated_at"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0 or data_as_of is None:
                continue
            # A direct TestNet quote may be observed after the cycle's earlier
            # `now` was frozen for calibration/scanning. Compare with the
            # current clock, or a correct late refresh looks 5+ seconds into
            # the future and gets silently discarded.
            freshness_now = max(now, _as_utc(self.clock()))
            age = (freshness_now - data_as_of).total_seconds()
            freshness = str(snapshot.get("freshness_status", "fresh")).lower()
            if (
                snapshot.get("fresh") is False
                or snapshot.get("stale") is True
                or snapshot.get("executable") is False
                or age < -5
                or age > MARKET_MAX_AGE_SECONDS
                or freshness in {"stale", "degraded", "unknown", "unavailable"}
            ):
                continue
            snapshot["price"] = price
            snapshot["last"] = price
            snapshot["data_as_of"] = _iso(data_as_of)
            snapshot["received_at"] = _iso(received_at or now)
            snapshot["fresh"] = True
            # The universe has already fetched this cycle's active Gate
            # contracts. Preserve its observed fee before the model and entry
            # validator run; neither may invent a fee when Gate omits it.
            if scoped_mode is not TradingMode.PAPER:
                metric = universe_metrics.get(symbol)
                if metric is not None:
                    fee = metric.get("taker_fee_rate")
                    if fee is not None:
                        try:
                            fee = float(fee)
                        except (TypeError, ValueError):
                            fee = None
                    if fee is not None and math.isfinite(fee) and fee >= 0:
                        snapshot["fee_rate"] = fee
                        snapshot["fee_rate_source"] = metric.get("contract_source") or "gate_contract_universe"
                        snapshot["fee_rate_observed_at"] = (universe_snapshot or {}).get("observed_at")
            # Only the local PAPER simulator has an application-owned
            # matching contract.  A remote account must provide venue rules
            # through its adapter/market snapshot; otherwise the gateway
            # cannot size new risk without inventing a contract, fee, or
            # slippage assumption.
            if scoped_mode is TradingMode.PAPER:
                if not isinstance(snapshot.get("market"), dict):
                    snapshot["market"] = {
                        "contractSize": 1.0,
                        "precision": {"amount": 0.001, "price": 0.01},
                        "limits": {"amount": {"min": 0.001, "max": 1_000_000.0, "step": 0.001}},
                        "taker": 0.0005,
                    }
                snapshot.setdefault(
                    "market_contract_evidence",
                    {
                        "status": "DEFINED_LOCAL_PAPER_CONTRACT",
                        "source": "PAPER_SIMULATION_MARKET_CONTRACT",
                        "exchange_metadata": False,
                    },
                )
            snapshots[symbol] = snapshot
        return snapshots

    def _build_context(
        self,
        *,
        cycle_id: str,
        now: datetime,
        session_id: str,
        generation: int,
        account_id: str,
        mode: TradingMode,
        venue: str,
        authorization: Any,
        snapshots: dict[str, dict[str, Any]],
        candidates: list[dict[str, Any]] | None = None,
        calibration: dict[str, Any] | None = None,
        account_truth: dict[str, Any] | None = None,
        news_revisions: list[dict[str, Any]] | None = None,
        scheduled_at: str | None = None,
        allowed_instruments: tuple[str, ...] | list[str] | None = None,
        strategy_instructions: dict[str, Any] | None = None,
        universe_snapshot: dict[str, Any] | None = None,
    ) -> AICycleContext:
        instruments = (
            tuple(dict.fromkeys(str(item).strip().upper() for item in allowed_instruments if str(item).strip()))
            if allowed_instruments is not None
            else tuple(snapshots.keys()) or tuple(self._allowed_symbols(authorization))
        )
        strategy_profile = (strategy_instructions or {}).get("profile")
        nofx_runtime = (strategy_instructions or {}).get("nofx_runtime")
        nofx_runtime = nofx_runtime if isinstance(nofx_runtime, dict) else None
        strategy_timeframe = (
            str((nofx_runtime or {}).get("signal_timeframe") or (strategy_profile or {}).get("signal_timeframe") or "").strip().lower()
            if isinstance(strategy_profile, dict) or isinstance(nofx_runtime, dict)
            else ""
        )
        technical_interval = 5 if strategy_timeframe == "5m" else 15
        limits = getattr(authorization, "limits", {}) or {}
        max_risk = float(limits.get("max_single_risk_pct", 0.0025))
        max_risk = min(max_risk, 0.0025)
        scope = resolve_account_scope(self.store, account_id) or {}
        scope_environment = str(scope.get("environment") or mode.value).lower()
        observed_environments = sorted({str(item.get("market_data_environment") or item.get("environment") or "").lower() for item in snapshots.values() if isinstance(item, dict) and (item.get("market_data_environment") or item.get("environment"))})
        market_data_environment = observed_environments[0] if len(observed_environments) == 1 else ("MIXED" if observed_environments else None)
        quality_values = [item.get("data_quality") for item in snapshots.values() if isinstance(item, dict) and isinstance(item.get("data_quality"), dict)]
        quality_statuses = {str(item.get("status") or "UNKNOWN").upper() for item in quality_values}
        data_quality = {
            "status": "BLOCKED" if not snapshots else ("DEGRADED" if any(status not in {"READY", "AVAILABLE", "FRESH"} for status in quality_statuses) else "READY"),
            "source": "gate_native_rest" if any(str(item.get("source") or "").startswith("gate_native") or str(item.get("provider") or "").startswith("gate") for item in snapshots.values() if isinstance(item, dict)) else "runtime_market_snapshot",
            "environment": market_data_environment or scope_environment,
            "synthetic": any(bool(item.get("synthetic")) for item in snapshots.values() if isinstance(item, dict)),
            "symbols": list(instruments),
        }
        candidate_rows = list(candidates or [])
        strategy_states = {str(item.get("strategy_id")): str(item.get("status") or "UNKNOWN") for item in candidate_rows if isinstance(item, dict) and item.get("strategy_id")}
        strategy_readiness = {
            "status": "READY" if snapshots and candidate_rows and all(state not in {"ERROR", "DATA_BLOCKED", "UNAVAILABLE"} for state in strategy_states.values()) else ("DATA_BLOCKED" if not snapshots else "NO_CANDIDATE_EVIDENCE"),
            "strategies": strategy_states,
            "candidate_count": len(candidate_rows),
            "calibration_status": str((calibration or {}).get("status") or "UNKNOWN"),
        }
        indicator_snapshot_id = next((str(item.get("indicator_snapshot_id")) for item in snapshots.values() if isinstance(item, dict) and item.get("indicator_snapshot_id")), None)
        remote_positions = (account_truth or {}).get("positions") if isinstance(account_truth, dict) else None
        scoped_positions = (
            [dict(item) for item in remote_positions if isinstance(item, dict)]
            if str((account_truth or {}).get("status") or "").upper() == "AVAILABLE" and isinstance(remote_positions, list)
            else self.ledger.get_open_positions(account_id, venue=venue, mode=mode.value)
        )
        dynamic_risk = (
            {"status": "NOT_APPLICABLE", "entry_allowed": True, "reasons": [], "policy": "MARGIN_ONLY"}
            if nofx_runtime is not None and mode in {TradingMode.TESTNET, TradingMode.LIVE} and str(venue).lower() == "gate"
            else self._evaluate_cycle_dynamic_risk(account_id, strategy_instructions, now)
        )
        runtime_timeframes = None
        runtime_indicator_config = None
        if nofx_runtime is not None:
            runtime_timeframes = [nofx_runtime.get("signal_timeframe"), *(nofx_runtime.get("context_timeframes") or [])]
            runtime_indicators = nofx_runtime.get("indicators") if isinstance(nofx_runtime.get("indicators"), dict) else {}
            def indicator_enabled(name: str) -> bool:
                item = runtime_indicators.get(name)
                return bool(item.get("enabled")) if isinstance(item, dict) else False
            runtime_indicator_config = {
                "enable_raw_klines": True,
                "enable_ema": indicator_enabled("ema"),
                "ema_periods": (runtime_indicators.get("ema") or {}).get("periods", [20, 50]),
                "enable_macd": indicator_enabled("macd"),
                "enable_rsi": indicator_enabled("rsi"),
                "rsi_periods": (runtime_indicators.get("rsi") or {}).get("periods", [7, 14]),
                "enable_atr": indicator_enabled("atr"),
                "atr_periods": (runtime_indicators.get("atr") or {}).get("periods", [14]),
                "enable_boll": indicator_enabled("bollinger"),
                "boll_periods": (runtime_indicators.get("bollinger") or {}).get("periods", [20]),
                "enable_volume": indicator_enabled("volume"),
                "enable_oi": indicator_enabled("open_interest"),
                "enable_funding_rate": indicator_enabled("funding_rate"),
            }
        verified_derivatives = (
            _load_verified_gate_derivatives(self.store, instruments, now)
            if runtime_indicator_config and (runtime_indicator_config["enable_oi"] or runtime_indicator_config["enable_funding_rate"])
            else None
        )
        performance_context = build_performance_context(
            self.store,
            account_id,
            venue=venue,
            mode=mode.value if hasattr(mode, "value") else str(mode),
        )
        return AICycleContext(
            cycle_id=cycle_id,
            account_id=account_id,
            generation=generation,
            started_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
            allowed_instruments=instruments,
            max_risk_fraction=max_risk,
            mode=mode,
            venue=venue,
            environment=mode.value,
            provider=venue,
            execution_environment=scope_environment,
            session_id=session_id,
            authorization_id=getattr(authorization, "authorization_id", None),
            authorization_version=getattr(authorization, "version", None),
            lease_holder_id=getattr(self.gateway, "runtime_lease_holder_id", None),
            fencing_token=getattr(self.gateway, "runtime_fencing_token", None),
            market_snapshots=snapshots,
            positions=scoped_positions,
            candidates=candidate_rows,
            decision_memory=memory_for_prompt(self.store, account_id),
            calibration=dict(calibration or {}),
            market_data_environment=market_data_environment,
            data_quality=data_quality,
            strategy_readiness=strategy_readiness,
            indicator_snapshot_id=indicator_snapshot_id,
            account_truth=dict(account_truth or {}),
            news_revisions=list(news_revisions or []),
            scheduled_at=scheduled_at,
            decision_contract=CONTRACT,
            technical_context=technical_context(
                self.store,
                instruments,
                now,
                interval=technical_interval,
                timeframes=runtime_timeframes,
                nofx_indicators=runtime_indicator_config,
                verified_derivatives=verified_derivatives,
            ),
            strategy_instructions=dict(strategy_instructions or {}),
            universe_snapshot=dict(universe_snapshot or {}),
            dynamic_risk=dynamic_risk,
            performance_context=performance_context,
        )

    def _model_output(self, context: AICycleContext) -> AIActionOutput:
        context.model_call_attempted = False
        context.model_call_completed = False
        provider = self.model_provider
        # Bonsai 2 27B bridge: use model_client when OllamaProvider is absent.
        # When the sidecar starts before Ollama is detected, provider may be None
        # even though model_client (pointing to llama-server on :8080) is healthy.
        # In that case we auto-create an OllamaProvider shim so the rest of this
        # method (which calls provider.generate_json) continues to work correctly.
        from ..model_client import model_client as _mc
        if provider is None or not callable(getattr(provider, "generate_json", None)):
            if not _mc.is_healthy():
                raise RuntimeError("SMART_MODEL_UNAVAILABLE")
            from ..ai.ollama import OllamaProvider
            provider = OllamaProvider(
                base_url="http://127.0.0.1:8080/v1",
                model_name=DEFAULT_SMART_MODEL,
                timeout=DECISION_MODEL_REQUEST_TIMEOUT_SECONDS,
                context_length=_session_context_length(),
                max_tokens=DECISION_OUTPUT_TOKEN_BUDGET,
                temperature=0,
                retries=0,
                think=False,
                keep_alive=_session_keep_alive(),
            )
            self.model_provider = provider

        def compact_candidate(row: dict[str, Any]) -> dict[str, Any]:
            fields = (
                "candidate_id", "symbol", "signal_timeframe",
                "status", "direction_bias", "rr", "conditions",
                "trigger_completion_pct", "entry_zone", "invalidation",
                "market_regime",
            )
            compact = {key: row[key] for key in fields if key in row}
            if isinstance(compact.get("conditions"), list):
                compact["conditions"] = [str(item)[:40] for item in compact["conditions"][:2]]
            if isinstance(compact.get("invalidation"), str):
                compact["invalidation"] = compact["invalidation"][:80]
            proposal = row.get("proposal")
            if isinstance(proposal, dict):
                proposal_fields = (
                    "side", "entry", "stop", "targets", "rationale",
                )
                compact["proposal"] = {
                    key: proposal[key]
                    for key in proposal_fields
                    if key in proposal
                }
                if isinstance(compact["proposal"].get("rationale"), str):
                    compact["proposal"]["rationale"] = compact["proposal"]["rationale"][:40]
                if isinstance(compact["proposal"].get("conditions"), list):
                    compact["proposal"]["conditions"] = [str(item)[:80] for item in compact["proposal"]["conditions"][:4]]
                if isinstance(compact["proposal"].get("targets"), list):
                    compact["proposal"]["targets"] = compact["proposal"]["targets"][:2]
            if isinstance(compact.get("conditions"), list):
                compact["conditions"] = [str(item)[:40] for item in compact["conditions"][:2]]
            return compact

        strategy_profile = context.strategy_instructions.get("profile")
        strategy_execution = context.strategy_instructions.get("execution")
        nofx_gate = (
            (context.mode.value if isinstance(context.mode, TradingMode) else str(context.mode)).upper() in {"TESTNET", "LIVE"}
            and str(context.venue or "").lower() == "gate"
        )
        execution_fields = (
            "direction", "universe_mode", "symbols", "scan_interval_minutes", "order_preference",
            # Keep the AI's account of the execution envelope aligned with the
            # same normalized fields consumed by the order sizer. In
            # particular, max_positions is the canonical name; the older
            # max_open_positions alias was never read by strategy_execution.
            "sizing_mode", "fixed_notional_usdt", "equity_notional_pct", "max_notional_usdt",
            "leverage", "risk_per_trade_pct", "max_positions", "max_margin_pct",
            "min_confidence", "min_net_rr", "cooldown_minutes", "atr_adaptive_sizing",
            "consecutive_loss_lock_enabled", "us_open_defense_enabled",
        )
        if nofx_gate:
            execution_fields = (
                "direction", "universe_mode", "symbols", "scan_interval_minutes",
                "max_notional_usdt", "max_positions", "max_margin_pct",
            )
        strategy_summary = {
            key: context.strategy_instructions[key]
            for key in ("name", "template_id", "style")
            if key in context.strategy_instructions
        }
        # The full machine profile and written strategy already appear once in
        # the system message. Keep only execution-side knobs in the user data.
        if isinstance(strategy_execution, dict):
            strategy_summary["execution"] = {
                key: strategy_execution[key]
                for key in execution_fields
                if key in strategy_execution
            }
        # The local model repeatedly proposed 1.3-ATR stops while the fixed
        # gate requires a wider floor.  Give it the actual signal ATR and
        # machine-policy geometry before it chooses prices.  This is guidance
        # from observed data; the execution gate still recalculates at the
        # model's exact entry and rejects any narrow stop or low net RR.
        if isinstance(strategy_profile, dict) and not nofx_gate:
            signal_frame = str(strategy_profile.get("signal_timeframe") or "15m")
            geometry: dict[str, dict[str, Any]] = {}
            for symbol in context.allowed_instruments[:MAX_UNIVERSE_SYMBOLS]:
                snap = context.market_snapshots.get(symbol)
                technical = context.technical_context.get(symbol)
                if not isinstance(snap, dict) or not isinstance(technical, dict):
                    continue
                frame = (technical.get("timeframes") or {}).get(signal_frame)
                indicators = frame.get("indicators") if isinstance(frame, dict) else None
                try:
                    quote = float(snap.get("price") or snap.get("last"))
                    atr = float((indicators or {}).get("atr14_simple"))
                    atr_multiple = float(strategy_profile["atr_stop_multiple"])
                    floor_pct = float(strategy_profile["major_stop_floor_pct" if symbol.startswith(("BTC", "ETH")) else "alt_stop_floor_pct"])
                except (TypeError, ValueError, KeyError):
                    continue
                if not all(math.isfinite(v) and v > 0 for v in (quote, atr, atr_multiple, floor_pct)):
                    continue
                try:
                    ema20 = float((indicators or {}).get("ema20"))
                except (TypeError, ValueError):
                    ema20 = 0.0
                geometry[symbol] = {
                    "quote": round(quote, 8),
                    "signal_atr": round(atr, 8),
                    "min_stop_distance_at_quote": round(max(atr * atr_multiple, quote * floor_pct / 100), 8),
                    "min_net_rr": (strategy_execution or {}).get("min_net_rr", strategy_profile.get("minimum_net_rr")),
                }
                if math.isfinite(ema20) and ema20 > 0:
                    ema_stop_distance = max(atr * atr_multiple, ema20 * floor_pct / 100)
                    try:
                        max_limit_pct = float(strategy_profile.get("max_limit_distance_pct") or 0)
                    except (TypeError, ValueError):
                        max_limit_pct = 0.0
                    ema_limit_distance_pct = abs(ema20 - quote) / quote * 100
                    geometry[symbol].update({
                        "ema20_example_entry": round(ema20, 8),
                        "long_stop_must_be_at_or_below_if_entry_at_ema20": round(ema20 - ema_stop_distance, 8),
                        "short_stop_must_be_at_or_above_if_entry_at_ema20": round(ema20 + ema_stop_distance, 8),
                        "ema20_limit_distance_pct": round(ema_limit_distance_pct, 4),
                        "max_limit_distance_pct": max_limit_pct,
                        "ema20_within_limit_distance": max_limit_pct > 0 and ema_limit_distance_pct <= max_limit_pct,
                        "ema20_passive_direction": "OPEN_LONG" if ema20 < quote else "OPEN_SHORT" if ema20 > quote else "AT_QUOTE",
                    })
                    # Show the model the *minimum* target required by the
                    # same fee/slippage math as validate_entry. A structural
                    # stop farther than this floor requires a farther target;
                    # these figures are guidance, never an execution waiver.
                    try:
                        market = snap.get("market") or {}
                        fee = float(snap.get("fee_rate", market.get("taker")))
                        slip = float(snap["slippage"])
                        rr = float((strategy_execution or {}).get("min_net_rr") or POLICY["min_net_reward_risk"])
                    except (TypeError, ValueError, KeyError):
                        fee = slip = rr = float("nan")
                    if all(math.isfinite(v) for v in (fee, slip, rr)) and 0 <= fee < 1 and 0 <= slip < 1 and rr > 0:
                        long_stop = ema20 - ema_stop_distance
                        short_stop = ema20 + ema_stop_distance
                        long_loss = ema20 - long_stop + long_stop * slip + (ema20 + long_stop) * fee
                        short_loss = short_stop - ema20 + short_stop * slip + (ema20 + short_stop) * fee
                        long_denominator = 1 - slip - fee
                        short_denominator = 1 + slip + fee
                        if long_stop > 0 and long_denominator > 0 and long_loss > 0 and short_loss > 0:
                            geometry[symbol].update({
                                "long_min_target_if_entry_at_ema20": round((rr * long_loss + ema20 * (1 + fee)) / long_denominator, 8),
                                "short_max_target_if_entry_at_ema20": round((ema20 * (1 - fee) - rr * short_loss) / short_denominator, 8),
                                "target_bounds_include_fee_slippage": True,
                                "target_bounds_assume_stop_at_minimum_only": True,
                            })
            if geometry:
                strategy_summary["risk_geometry"] = geometry
        nofx_runtime = context.strategy_instructions.get("nofx_runtime")
        if isinstance(nofx_runtime, dict):
            strategy_summary["nofx_runtime"] = {
                key: nofx_runtime[key]
                for key in (
                    "version", "source", "signal_timeframe", "context_timeframes",
                    "indicators", "candidate_sources", "excluded_symbols", "unsupported_sources",
                )
                if key in nofx_runtime
            }

        raw_positions = context.account_truth.get("positions")
        if not isinstance(raw_positions, list) or not raw_positions:
            raw_positions = context.positions
        account_truth = {
            key: context.account_truth.get(key)
            for key in (
                "status", "source", "observed_at", "snapshot_id", "equity", "available_margin",
                "used_margin", "unrealized_pnl",
            )
            if context.account_truth.get(key) is not None
        }
        account_truth["positions"] = [
            _compact_position(item) for item in raw_positions if isinstance(item, dict)
        ]
        pending_orders = context.account_truth.get("pending_orders")
        if isinstance(pending_orders, list):
            order_fields = ("order_id", "client_order_id", "symbol", "instrument_id", "side", "type", "price", "stop_price", "amount", "remaining", "status", "reduce_only")
            account_truth["pending_orders"] = [
                {key: item[key] for key in order_fields if key in item and item[key] is not None}
                for item in pending_orders[:4] if isinstance(item, dict)
            ]

        candidates = _select_prompt_candidates(context.candidates, context.allowed_instruments)
        static_proposals = [
            row for row in candidates
            if str(row.get("status") or "").upper() in {"PROPOSAL", "READY"}
        ]
        decision_time = _parse_time(context.started_at) or _as_utc(self.clock())
        prompt_news = _select_prompt_news(
            [
                row for row in context.news_revisions
                if isinstance(row, dict) and any(
                    relevant_news_for_entry(row, symbol, decision_time)
                    for symbol in context.allowed_instruments
                )
            ],
            context.allowed_instruments,
        )
        market_radar = _load_market_radar_snapshot(self.store, context.allowed_instruments, decision_time)
        try:
            # Outcomes are reconciled only against locally mirrored, fully
            # closed fills. Open/partial/unfilled entries remain unsettled.
            reconcile_decision_outcomes(self.store, context.account_id, limit=20)
            memory_rows = list_decision_memory(self.store, context.account_id, limit=20)
        except Exception:
            logger.warning("Decision experience unavailable for AI prompt", exc_info=True)
            memory_rows = []
        if not memory_rows:
            memory_rows = list(context.decision_memory or [])
        recent_decisions, strategy_experience = _compact_decision_experience(memory_rows)

        prompt_payload = {
            # Contract and fixed risk policy already live in the versioned
            # system prompt. Keep the user message focused on per-cycle facts.
            "active_strategy": strategy_summary,
            "market_universe": {
                key: context.universe_snapshot[key]
                for key in (
                    "status", "environment", "contract_count", "eligible_count",
                    "selected_symbols", "mode", "rotation_index", "selection",
                    "candidate_sources", "excluded_symbols", "unsupported_sources",
                )
                if key in context.universe_snapshot
            },
            "technical_context": compact_technical(
                context.technical_context,
                signal_timeframe=(
                    str(strategy_profile.get("signal_timeframe") or "15m")
                    if isinstance(strategy_profile, dict) else "15m"
                ),
            ),
            "account_id": context.account_id,
            "mode": context.mode.value if isinstance(context.mode, TradingMode) else str(context.mode),
            "venue": context.venue,
            "allowed_instruments": list(context.allowed_instruments),
            "account_truth": account_truth,
            "market_snapshots": {
                symbol: _compact_market_snapshot(snapshot)
                for symbol, snapshot in context.market_snapshots.items()
                if isinstance(snapshot, dict)
            },
            "market_radar": market_radar,
            "news_coverage_status": "RECENT_REVISIONS_INCLUDED" if prompt_news else "NO_RECENT_RELEVANT_REVISION_IN_INPUT",
            "news_revisions": [
                _compact_news_revision(item)
                for item in prompt_news
                if isinstance(item, dict)
            ],
            "calibration": {
                key: context.calibration[key]
                for key in ("status", "profile_id", "sample_size", "expires_at", "error_code", "profile")
                if key in context.calibration
            },
            "market_data_environment": context.market_data_environment,
            "data_quality": context.data_quality,
            # Readiness is an operational gate, but registered candidate
            # NO_TRIGGER counts are not a veto on an AI-authored setup. The
            # candidate list below already exposes positive proposals; keep
            # negative scanner results only in the frozen audit evidence.
            "strategy_readiness": {
                key: context.strategy_readiness[key]
                for key in ("status", "calibration_status")
                if isinstance(context.strategy_readiness, dict)
                and key in context.strategy_readiness
            },
            "indicator_snapshot_id": context.indicator_snapshot_id,
            "decision_memory": recent_decisions,
            "strategy_experience": strategy_experience,
            "performance_context": context.performance_context,
            "dynamic_risk": context.dynamic_risk,
            "generation": context.generation,
        }
        # A zero-valued static scan is not evidence against a model-authored
        # setup. Repeating that count in every prompt anchored the local model
        # on "no candidates" even when the strategy explicitly allows a
        # verified K-line entry. Keep the full scan in the audit cycle; only
        # pass actual positive proposals as optional supporting evidence.
        if static_proposals:
            prompt_payload["candidates"] = [compact_candidate(row) for row in static_proposals]
        provider_name = str(
            getattr(provider, "provider_name", None)
            or getattr(provider, "model_id", None)
            or provider.__class__.__name__
        )
        model_id = str(getattr(provider, "model_id", None) or DEFAULT_SMART_MODEL)
        model_version = str(
            getattr(provider, "model_version", None)
            or getattr(provider, "version", None)
            or model_id
        )
        with self._lock:
            health_snapshot = dict(self._health_cache or {})
        model_digest, model_digest_status = model_weight_digest(
            provider,
            health_result=health_snapshot,
            model_name=DEFAULT_SMART_MODEL,
        )
        model_quantization = str(
            health_snapshot.get("quantization")
            or getattr(provider, "quantization", None)
            or "UNKNOWN_NOT_PROVIDED"
        )
        model_inference_settings = {
            "temperature": 0.0,
            "context_length": getattr(provider, "context_length", None),
            "max_tokens": getattr(provider, "max_tokens", None),
            "think": getattr(provider, "think", None),
            "reasoning_effort": "none",
            # These identify an observed completion only. The requested alias
            # above is not proof of which local weights served the request.
            "actual_model_id": None,
            "model_identity_source": None,
            "verified_manifest_model_id": None,
            "schema": "AI_ACTION_SCHEMA",
            "schema_enforcement": "PENDING_PROVIDER_RESULT",
            "local_schema_validation": "PENDING",
        }
        context.model_quantization = model_quantization
        context.model_inference_settings = model_inference_settings
        evidence_refs_list = (
            [f"market_snapshot:{symbol}:{_digest(snapshot)[:16]}" for symbol, snapshot in sorted(context.market_snapshots.items())]
            + [f"technical_snapshot:{symbol}:{_digest(snapshot)[:16]}" for symbol, snapshot in sorted(context.technical_context.items())]
            + [f"market_radar:{_digest(market_radar)[:16]}"]
            + ([f"authorization:{context.authorization_id}"] if context.authorization_id else [])
            + ([f"session:{context.session_id}:{context.generation}"] if context.session_id else [])
            + ([f"account_snapshot:{context.account_truth.get('snapshot_id')}"] if context.account_truth.get("snapshot_id") else [])
            + ([f"indicator_snapshot:{context.indicator_snapshot_id}"] if context.indicator_snapshot_id else [])
        )
        for candidate in context.candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("candidate_id"):
                evidence_refs_list.append(f"candidate:{candidate['candidate_id']}")
            candidate_refs = candidate.get("evidence_refs")
            if isinstance(candidate_refs, (list, tuple)):
                evidence_refs_list.extend(str(item) for item in candidate_refs if str(item).strip())
        for revision in context.news_revisions:
            if isinstance(revision, dict) and revision.get("revision_id"):
                evidence_refs_list.append(f"news_revision:{revision['revision_id']}")
        evidence_refs = tuple(dict.fromkeys(evidence_refs_list))
        bundle_id = f"bundle_{context.cycle_id}"
        selected_news_ids = {str(item.get("revision_id") or "") for item in prompt_news}
        visible_evidence_refs = [
            item for item in evidence_refs
            if item.startswith(("market_snapshot:", "technical_snapshot:", "market_radar:"))
            or (
                item.startswith("news_revision:")
                and item.split(":", 1)[1] in selected_news_ids
            )
        ]
        prompt_payload["evidence_bundle_id"] = bundle_id
        prompt_payload["evidence_refs"] = visible_evidence_refs
        context_length = int(getattr(provider, "context_length", 0) or 0)
        system_prompt = build_strategy_system_prompt(
            context.strategy_instructions, context_length=context_length, nofx_gate=nofx_gate,
        )
        open_contract = require_nofx_gate_open_contract if nofx_gate else require_entry_analysis_for_open

        def validate_open_news(decision: object) -> None:
            if not nofx_gate:
                _require_relevant_news_ref_for_open(decision, prompt_news, decision_time)
        if "JSON字段规则：" not in system_prompt:
            system_prompt += "\n\n" + TRADE_JSON_GUIDE
        configured_output_tokens = int(getattr(provider, "max_tokens", 0) or DECISION_OUTPUT_TOKEN_BUDGET)
        if not 1 <= configured_output_tokens <= DECISION_OUTPUT_TOKEN_BUDGET:
            raise ValueError("MODEL_MAX_TOKENS_INVALID")
        minimum_output_tokens = min(MIN_DECISION_OUTPUT_TOKENS, configured_output_tokens)
        tokenizer_state = {"available": True, "name": "BONSAI_RUNTIME"}
        token_cache: dict[str, int] = {}

        def count_prompt_tokens(content: str) -> int:
            cached = token_cache.get(content)
            if cached is not None:
                return cached
            if tokenizer_state["available"]:
                try:
                    count = int(_mc.count_tokens(content, timeout_sec=5.0))
                    if count < 0:
                        raise ValueError("negative token count")
                    token_cache[content] = count
                    return count
                except Exception as exc:
                    tokenizer_state["available"] = False
                    tokenizer_state["name"] = "CONSERVATIVE_ESTIMATE"
                    logger.warning(
                        "Bonsai tokenizer unavailable; using conservative prompt estimate (%s)",
                        type(exc).__name__,
                    )
            count = _estimate_tokens(content)
            token_cache[content] = count
            return count

        prompt_budget_error: ValueError | None = None
        prompt_budget: dict[str, Any]
        try:
            signal_timeframe = (
                str(strategy_profile.get("signal_timeframe") or "15m")
                if isinstance(strategy_profile, dict) else "15m"
            )
            soft_context = min(context_length, INFERENCE_SOFT_CONTEXT_LIMIT)
            try:
                prompt_payload, prompt_budget = _fit_prompt_payload(
                    prompt_payload, system_prompt, soft_context,
                    reserve=minimum_output_tokens,
                    signal_timeframe=signal_timeframe,
                    token_counter=count_prompt_tokens,
                )
                model_inference_settings["soft_context_fallback"] = False
            except ValueError as soft_error:
                if context_length <= soft_context or "AI_INPUT_BUDGET_EXCEEDED" not in str(soft_error):
                    raise
                # The soft target is for latency, not an extra entry gate.
                # Preserve the verified full-window path when decision-critical
                # facts cannot fit in the smaller projection.
                prompt_payload, prompt_budget = _fit_prompt_payload(
                    prompt_payload, system_prompt, context_length,
                    reserve=minimum_output_tokens,
                    signal_timeframe=signal_timeframe,
                    token_counter=count_prompt_tokens,
                )
                model_inference_settings["soft_context_fallback"] = True
            prompt_output_budget = min(
                int(getattr(provider, "max_tokens", 0) or DECISION_OUTPUT_TOKEN_BUDGET),
                context_length
                - int(prompt_budget["estimated_input_tokens"])
                - PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
            )
            if prompt_output_budget < minimum_output_tokens:
                raise ValueError(
                    "AI_INPUT_BUDGET_EXCEEDED: verified input leaves less than the required output reserve"
                )
            model_inference_settings.update({
                "estimated_input_tokens": prompt_budget["estimated_input_tokens"],
                "verified_context_length": context_length,
                "inference_soft_context_limit": soft_context,
                "output_token_reserve": prompt_output_budget,
                "tokenizer": tokenizer_state["name"],
                "prompt_compaction": {
                    "applied": prompt_budget["compacted"],
                    "steps": prompt_budget["steps"],
                    "safety_margin_tokens": PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
                    "news_semantics_compacted": prompt_budget.get("news_semantics_compacted", False),
                },
            })
        except ValueError as exc:
            # Keep and freeze the complete source prompt/evidence for audit,
            # then fail closed after persistence without calling the model.
            prompt_budget_error = exc
            model_inference_settings["prompt_compaction"] = {
                "applied": False,
                "steps": [],
                "safety_margin_tokens": PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
                "error_code": "AI_INPUT_BUDGET_EXCEEDED" if "AI_INPUT_BUDGET_EXCEEDED" in str(exc) else "MODEL_CONTEXT_UNKNOWN",
            }
        serialized_prompt_payload = json.dumps(prompt_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        input_hash = _digest({
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": serialized_prompt_payload},
            ]
        })
        model_inference_settings["request_hash"] = input_hash
        bundle = EvidenceBundle.freeze(
            dataset_id=f"ai_cycle:{context.cycle_id}",
            as_of=context.started_at,
            expires_at=context.expires_at,
            payload={
                "prompt_inputs": prompt_payload,
                "prompt_request": {
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": serialized_prompt_payload},
                    ],
                    "request_hash": input_hash,
                    "response_format": {"type": "json_object"},
                    "local_validation_schema": AI_ACTION_SCHEMA,
                    "max_tokens": model_inference_settings.get("output_token_reserve"),
                    "safety_margin_tokens": PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
                    "tokenizer": model_inference_settings.get("tokenizer"),
                },
                "source_evidence": {
                    "market_radar": market_radar,
                    "market_snapshots": context.market_snapshots,
                    "technical_context": _slim_technical_context(context.technical_context),
                    "news_revisions": context.news_revisions,
                    "candidates": context.candidates,
                    "account_truth": {
                        key: context.account_truth[key]
                        for key in (
                            "status", "source", "api_environment", "observed_at", "snapshot_id",
                            "equity", "available_margin", "used_margin", "unrealized_pnl",
                            "realized_pnl", "positions", "pending_orders", "remote_truth", "account_id",
                        )
                        if key in context.account_truth
                    },
                    "positions": context.positions,
                    "strategy_instructions": context.strategy_instructions,
                    "market_universe": context.universe_snapshot,
                    "calibration": context.calibration,
                },
                "evidence_refs": list(evidence_refs),
                "model": {
                    "provider": provider_name,
                    "model_id": model_id,
                    "model_version": model_version,
                    "weight_digest": model_digest,
                    "quantization": model_quantization,
                    "inference_settings": model_inference_settings,
                    "prompt_version": AI_PROMPT_VERSION,
                    "weight_digest_status": model_digest_status,
                },
            },
            bundle_id=bundle_id,
            references=evidence_refs,
            missing=[] if model_digest else ["model_weight_digest"],
            frozen_at=context.started_at,
        )
        persisted_bundle = persist_evidence_bundle(self.store, bundle)
        context.evidence_bundle_id = bundle.bundle_id
        context.evidence_status = str(persisted_bundle.get("status") or "FROZEN")
        context.evidence_refs = evidence_refs
        if prompt_budget_error is not None:
            raise prompt_budget_error
        # The model is never allowed to choose these facts.  They are
        # stamped onto the immutable cycle record by the coordinator.
        context.model_id = model_id
        context.model_version = model_version
        context.prompt_version = AI_PROMPT_VERSION
        context.input_hash = input_hash
        context.model_digest = model_digest
        context.model_digest_status = model_digest_status
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": serialized_prompt_payload},
        ]
        def call_model(
            call_messages: list[dict[str, str]],
            prompt_version: str,
            *,
            schema: dict[str, Any] | None = None,
        ) -> Any:
            request_schema = schema or AI_ACTION_SCHEMA
            model_messages = [dict(item) for item in call_messages]
            system_prompt = str(model_messages[0].get("content") or "") if model_messages else ""
            if request_schema and "JSON字段规则：" not in system_prompt:
                system_prompt += "\n\n" + TRADE_JSON_GUIDE
                if model_messages:
                    model_messages[0]["content"] = system_prompt
                else:
                    model_messages = [{"role": "system", "content": system_prompt}]
            user_prompt = str(model_messages[1].get("content") or "") if len(model_messages) > 1 else ""
            context_length = int(getattr(provider, "context_length", 0) or 0)
            minimum_call_output_tokens = min(MIN_DECISION_OUTPUT_TOKENS, configured_output_tokens)
            if context_length <= 0:
                _assert_prompt_fits(
                    system_content=system_prompt,
                    user_content=user_prompt,
                    context_length=context_length,
                    reserve=minimum_call_output_tokens + PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
                    token_counter=count_prompt_tokens,
                )
            prompt_tokens = count_prompt_tokens(system_prompt) + count_prompt_tokens(user_prompt)
            available_output_tokens = context_length - prompt_tokens - PROMPT_BUDGET_SAFETY_MARGIN_TOKENS
            if available_output_tokens < minimum_call_output_tokens:
                _assert_prompt_fits(
                    system_content=system_prompt,
                    user_content=user_prompt,
                    context_length=context_length,
                    reserve=minimum_call_output_tokens + PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
                    token_counter=count_prompt_tokens,
                )
            output_token_budget = min(configured_output_tokens, available_output_tokens)
            estimated_tokens = _assert_prompt_fits(
                system_content=system_prompt,
                user_content=user_prompt,
                context_length=context_length,
                reserve=output_token_budget + PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
                token_counter=count_prompt_tokens,
            )
            attempt_hash = _digest({
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            })
            context.model_inference_settings.update({
                "estimated_input_tokens": estimated_tokens,
                "verified_context_length": context_length,
                "output_token_reserve": output_token_budget,
                "tokenizer": tokenizer_state["name"],
            })
            if prompt_version == AI_PROMPT_VERSION:
                context.input_hash = attempt_hash
                context.model_inference_settings["request_hash"] = attempt_hash
            else:
                attempts = context.model_inference_settings.setdefault("model_attempts", [])
                attempt_record = {
                    "prompt_version": prompt_version,
                    "request_hash": attempt_hash,
                    "estimated_input_tokens": estimated_tokens,
                    "output_token_reserve": output_token_budget,
                    "tokenizer": tokenizer_state["name"],
                }
                repair_suffix = (
                    "stop_repair" if prompt_version.endswith("_stop_repair")
                    else "entry_repair" if prompt_version.endswith("_entry_repair")
                    else "repair"
                )
                repair_bundle_id = f"{context.evidence_bundle_id}_{repair_suffix}"
                repair_bundle = EvidenceBundle.freeze(
                    dataset_id=f"ai_cycle:{context.cycle_id}:repair",
                    as_of=context.started_at,
                    expires_at=context.expires_at,
                    payload={
                        "source_bundle_id": context.evidence_bundle_id,
                        "prompt_request": {
                            "messages": model_messages,
                            "request_hash": attempt_hash,
                            "response_format": {"type": "json_object"},
                            "local_validation_schema": request_schema,
                            "max_tokens": output_token_budget,
                            "estimated_input_tokens": estimated_tokens,
                            "verified_context_length": context_length,
                            "safety_margin_tokens": PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
                            "tokenizer": tokenizer_state["name"],
                        },
                    },
                    bundle_id=repair_bundle_id,
                    references=tuple(context.evidence_refs) + (f"evidence_bundle:{context.evidence_bundle_id}",),
                    frozen_at=context.started_at,
                )
                persist_evidence_bundle(self.store, repair_bundle)
                attempt_record["evidence_bundle_id"] = repair_bundle_id
                attempts.append(attempt_record)
            started = time.perf_counter()
            context.model_call_attempted = True
            try:
                result = provider.generate_json(
                    model_messages,
                    model_name=DEFAULT_SMART_MODEL,
                    prompt_version=prompt_version,
                    input_hash=attempt_hash,
                    temperature=0.0,
                    schema=request_schema,
                    max_tokens=output_token_budget,
                    reasoning_effort="none",
                )
            finally:
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
                context.model_latency_ms = elapsed_ms
                context.model_call_prompt_version = prompt_version
            if result is not None:
                context.model_call_completed = True
            metadata = result[2] if isinstance(result, tuple) and len(result) > 2 and isinstance(result[2], dict) else {}
            for receipt_key in ("actual_model_id", "model_identity_source", "verified_manifest_model_id"):
                receipt_value = metadata.get(receipt_key)
                context.model_inference_settings[receipt_key] = str(receipt_value)[:300] if receipt_value else None
            returned_model = str(metadata.get("model_id") or "").strip()
            valid_models = {DEFAULT_SMART_MODEL}
            if returned_model and returned_model not in valid_models:
                raise ValueError("SMART_MODEL_MISMATCH")
            context.model_latency_ms = metadata.get("latency_ms", context.model_latency_ms)
            if metadata.get("schema_enforcement"):
                context.model_inference_settings["schema_enforcement"] = metadata["schema_enforcement"]
            context.model_raw_response = str(metadata.get("raw_response"))[:12000] if metadata.get("raw_response") is not None else None
            context.model_id = str(metadata.get("model_id") or context.model_id or DEFAULT_SMART_MODEL)
            context.model_version = str(metadata.get("model_version") or context.model_version or context.model_id)
            context.prompt_version = str(metadata.get("prompt_version") or prompt_version)
            return result

        response: Any = None
        previous_decision: dict[str, Any] | None = None
        schema_repaired = False
        wait_fact_repaired = False
        try:
            response = call_model(messages, AI_PROMPT_VERSION)
            candidate_decoded = response[0] if isinstance(response, tuple) else response
            if isinstance(candidate_decoded, dict) and isinstance(candidate_decoded.get("strategy_plan"), str):
                derived_plan = derive_strategy_plan_from_text(candidate_decoded)
                if derived_plan is not None:
                    candidate_decoded["strategy_plan"] = derived_plan
                    context.model_inference_settings["strategy_plan_structure_source"] = "DERIVED_FROM_MODEL_STRING"
            projection = normalize_news_impact(candidate_decoded)
            if projection is not None:
                context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
            projection = normalize_limit_ttl_alias(candidate_decoded)
            if projection is not None:
                context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
            projection = normalize_misplaced_strategy_plan(candidate_decoded)
            if projection is not None:
                context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
            projection = normalize_wait_conditions(candidate_decoded)
            if projection is not None:
                context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
            projection = normalize_wait_symbol_alias(candidate_decoded, context.allowed_instruments)
            if projection is not None:
                context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
            validate_schema(candidate_decoded, AI_ACTION_SCHEMA)
            require_confidence_for_open(candidate_decoded)
            open_contract(candidate_decoded)
            validate_open_news(candidate_decoded)
        except Exception as first_error:
            if "AI_INPUT_BUDGET_EXCEEDED" in str(first_error) or "MODEL_CONTEXT_UNKNOWN" in str(first_error):
                raise
            # A transport/provider failure yielded no completion. Do not turn
            # an HTTP 404 or timeout into a second, misleading "repair" call.
            if response is None:
                raise
            # One and only one bounded repair attempt.  The retry is still
            # schema-constrained, preserves a valid proposed action, and cannot
            # change account, authorization or market facts stamped above.
            candidate_decision = response[0] if isinstance(response, tuple) else response
            candidate_action = (
                str(candidate_decision.get("action") or "").strip().upper()
                if isinstance(candidate_decision, dict)
                else ""
            )
            candidate_instrument = (
                str(candidate_decision.get("instrument_id") or "").strip().upper()
                if isinstance(candidate_decision, dict)
                else ""
            )
            candidate_reason = (
                candidate_decision.get("reason")
                if isinstance(candidate_decision, dict)
                else None
            )
            authorized_instruments = {
                str(item).strip().upper()
                for item in context.allowed_instruments
                if str(item).strip()
            }
            # A provider error envelope (for example {"error": "..."}) is
            # not a decision.  Only pin a proposed action when it has the
            # minimum identity and authorization facts needed to preserve it.
            if (
                isinstance(candidate_decision, dict)
                and candidate_action in ALLOWED_AI_ACTIONS
                and candidate_instrument in authorized_instruments
                and isinstance(candidate_reason, str)
                and bool(candidate_reason.strip())
            ):
                previous_decision = candidate_decision
            else:
                previous_decision = None
            repair_schema = {
                **AI_ACTION_SCHEMA,
                "required": list(AI_ACTION_SCHEMA.get("required") or ()),
                "properties": dict(AI_ACTION_SCHEMA.get("properties") or {}),
            }
            proposed_action = (
                str(previous_decision.get("action") or "").strip().upper()
                if isinstance(previous_decision, dict)
                else ""
            )
            if proposed_action in ALLOWED_AI_ACTIONS:
                action_schema = dict(repair_schema["properties"].get("action") or {})
                action_schema["enum"] = [proposed_action]
                repair_schema["properties"]["action"] = action_schema
                if proposed_action in {"OPEN_LONG", "OPEN_SHORT"}:
                    confidence_schema = dict(repair_schema["properties"].get("confidence") or {})
                    confidence_schema["type"] = "number"
                    repair_schema["properties"]["confidence"] = confidence_schema
                    for required_field in (
                        ("entry_price", "stop_price", "take_profit", "position_size_usdt", "requested_leverage", "order_preference", "evidence_refs")
                        if nofx_gate else
                        ("entry_price", "stop_price", "take_profit", "requested_risk_fraction", "evidence_refs")
                    ):
                        if required_field not in repair_schema["required"]:
                            repair_schema["required"].append(required_field)
            repair_inputs = (
                _focused_entry_repair_inputs(prompt_payload, candidate_instrument)
                if proposed_action in {"OPEN_LONG", "OPEN_SHORT"}
                and str(first_error).startswith("INVALID_ACTION_SCHEMA:OPEN_CONTEXT_REQUIRED")
                else prompt_payload
            )
            repair_payload = {
                "validation_error": str(first_error)[:240],
                "previous_decision": previous_decision,
                # Error envelopes still retain the full original facts. A
                # recognized OPEN with missing analysis gets only its chosen
                # asset's frozen facts; it may not change the decision.
                "inputs": repair_inputs,
            }
            repair_instruction = (
                "\n\n修复任务：只输出符合本次 JSON 动作契约的对象。"
                "若 previous_decision 存在且其 action、标的和 reason 已通过基本识别与授权检查，只修复验证指出的问题并保留已有决策字段；"
                "错误对象、缺少标的或未授权标的都不是有效决策。若无有效决策，根据 inputs 中的行情、新闻、策略和账户事实重新决策。"
                "instrument_id 必须来自 allowed_instruments，WAIT/HOLD 也必须填写授权标的；解释用简体中文。"
                "validation_error 里的 output.<字段>:type 表示该字段类型不符。"
                + ("OPEN 必须填写入场价、止损、止盈、名义金额、杠杆、LIMIT/MARKET 和真实 evidence_refs；"
                   if nofx_gate else "OPEN 必须填写入场价、止损、止盈、单笔风险和真实 evidence_refs；")
                + "策略计划、新闻摘要、逐周期说明与入场区间可选。"
                "若填写 strategy_plan，必须是对象 {name, thesis, entry_conditions, exit_conditions}。"
                "如果只缺 news_evidence_ref，可在原 evidence_refs 后追加一条 inputs 中适用于该标的的 news_revision 引用；"
                "不得删除或改写原引用，也不得改变动作、标的、入场、止损、止盈或金额。"
            )
            repair_system = system_prompt + repair_instruction
            empty_inputs_user = json.dumps(
                {**repair_payload, "inputs": {}},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            wrapper_tokens = count_prompt_tokens(empty_inputs_user)
            repair_payload["inputs"], repair_compaction = _fit_prompt_payload(
                repair_inputs,
                repair_system,
                context_length=max(0, context_length - wrapper_tokens - 32),
                reserve=minimum_output_tokens,
                signal_timeframe=(
                    str(strategy_profile.get("signal_timeframe") or "15m")
                    if isinstance(strategy_profile, dict) else "15m"
                ),
                token_counter=count_prompt_tokens,
            )
            repair_messages = [
                {"role": "system", "content": repair_system},
                {
                    "role": "user",
                    "content": json.dumps(
                        repair_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ]
            context.model_inference_settings["repair_prompt_compaction"] = {
                "applied": repair_compaction["compacted"],
                "steps": repair_compaction["steps"],
                "news_semantics_compacted": repair_compaction.get("news_semantics_compacted", False),
            }
            response = call_model(
                repair_messages,
                AI_PROMPT_VERSION + "_repair",
                schema=repair_schema,
            )
            schema_repaired = True
        decoded = response[0] if isinstance(response, tuple) else response
        projection = normalize_news_impact(decoded)
        if projection is not None:
            context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
        projection = normalize_limit_ttl_alias(decoded)
        if projection is not None:
            context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
        projection = normalize_misplaced_strategy_plan(decoded)
        if projection is not None:
            context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
        projection = normalize_wait_conditions(decoded)
        if projection is not None:
            context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
        projection = normalize_wait_symbol_alias(decoded, context.allowed_instruments)
        if projection is not None:
            context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)
        try:
            validate_schema(decoded, AI_ACTION_SCHEMA)
        except ValueError as repair_error:
            # The repair round is the model's last chance to emit the object
            # shape. If it still flattens strategy_plan to a sentence, convert
            # that sentence and record that the structure is runtime-derived;
            # every other contract violation still blocks the cycle.
            if str(repair_error) != "INVALID_ACTION_SCHEMA:output.strategy_plan:type" or not isinstance(decoded, dict):
                raise
            derived = derive_strategy_plan_from_text(decoded)
            if derived is None:
                raise
            decoded["strategy_plan"] = derived
            validate_schema(decoded, AI_ACTION_SCHEMA)
            context.model_inference_settings["strategy_plan_structure_source"] = "DERIVED_FROM_MODEL_STRING"
            logger.warning(
                "strategy_plan arrived as text after repair; projected onto the contract shape for %s",
                context.cycle_id,
            )
        require_confidence_for_open(decoded)
        open_contract(decoded)
        validate_open_news(decoded)
        context.model_inference_settings["local_schema_validation"] = "PASS"
        if not isinstance(decoded, dict):
            raise ValueError("INVALID_MODEL_JSON")
        if isinstance(previous_decision, dict):
            immutable_fields = (
                "action", "instrument_id", "entry_price", "stop_price", "take_profit",
                "requested_risk_fraction", "position_size_usdt", "requested_leverage", "order_preference",
                "limit_price", "ttl_seconds", "candidate_id", "strategy_candidate_id",
            )
            if any(
                field in previous_decision and decoded.get(field) != previous_decision[field]
                for field in immutable_fields
            ):
                raise ValueError("MODEL_REPAIR_CHANGED_DECISION")
            if "evidence_refs" in previous_decision:
                before = previous_decision["evidence_refs"]
                after = decoded.get("evidence_refs")
                repair_news_refs = {
                    f"news_revision:{row.get('revision_id')}"
                    for row in (repair_payload["inputs"].get("news_revisions") or [])
                    if isinstance(row, dict) and row.get("revision_id")
                    and relevant_news_for_entry(row, str(previous_decision["instrument_id"]).upper(), decision_time)
                }
                if (
                    not isinstance(before, list) or not isinstance(after, list)
                    or after[:len(before)] != before
                    or any(ref not in repair_news_refs for ref in after[len(before):])
                ):
                    raise ValueError("MODEL_REPAIR_CHANGED_DECISION")

        if not schema_repaired and not nofx_gate:
            wait_conflict = _wait_limit_distance_contradiction(prompt_payload, decoded)
            if wait_conflict is not None:
                symbol = wait_conflict["symbol"]
                focused_inputs = _focused_entry_repair_inputs(prompt_payload, symbol)
                selected_strategy = dict(prompt_payload.get("active_strategy") or {})
                selected_strategy["risk_geometry"] = {symbol: (selected_strategy.get("risk_geometry") or {})[symbol]}
                focused_inputs["active_strategy"] = selected_strategy
                focused_inputs["account_truth"] = prompt_payload.get("account_truth")
                fact_system = system_prompt + (
                    "\n\n事实复核：上一份 WAIT 声称所选标的 EMA20 限价超过策略距离上限，"
                    "但冻结输入已计算该距离在上限内。只修正这一事实后重新评估所选标的；"
                    "如结构失效价或费用后目标仍不成立可继续 WAIT，须写真实原因。"
                    "如条件均成立可由你自主输出 OPEN；不得捏造结构或降低风控。只输出完整 JSON。"
                )
                fact_user = json.dumps({
                    "previous_decision": decoded,
                    "verified_fact": wait_conflict,
                    "inputs": focused_inputs,
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                fact_schema = {**AI_ACTION_SCHEMA, "properties": dict(AI_ACTION_SCHEMA["properties"])}
                instrument_schema = dict(fact_schema["properties"]["instrument_id"])
                instrument_schema["enum"] = [symbol]
                fact_schema["properties"]["instrument_id"] = instrument_schema
                context.model_inference_settings["wait_fact_check"] = {"status": "PENDING", **wait_conflict}
                if (
                    count_prompt_tokens(fact_system) + count_prompt_tokens(fact_user)
                    + minimum_output_tokens + PROMPT_BUDGET_SAFETY_MARGIN_TOKENS > context_length
                ):
                    raise ValueError("MODEL_WAIT_FACT_REPAIR_BUDGET_EXCEEDED")
                corrected_response = call_model(
                    [{"role": "system", "content": fact_system}, {"role": "user", "content": fact_user}],
                    AI_PROMPT_VERSION + "_wait_fact_repair", schema=fact_schema,
                )
                corrected = corrected_response[0] if isinstance(corrected_response, tuple) else corrected_response
                normalize_news_impact(corrected)
                normalize_limit_ttl_alias(corrected)
                normalize_wait_conditions(corrected)
                normalize_wait_symbol_alias(corrected, context.allowed_instruments)
                validate_schema(corrected, AI_ACTION_SCHEMA)
                require_confidence_for_open(corrected)
                open_contract(corrected)
                validate_open_news(corrected)
                if str(corrected.get("instrument_id") or "").upper() != symbol:
                    raise ValueError("MODEL_WAIT_FACT_REPAIR_CHANGED_SYMBOL")
                if _wait_limit_distance_contradiction(prompt_payload, corrected) is not None:
                    raise ValueError("MODEL_WAIT_FACT_REPAIR_REPEATED_CONTRADICTION")
                decoded = corrected
                wait_fact_repaired = True
                context.model_inference_settings["wait_fact_check"]["status"] = "MODEL_REEVALUATED"

        # Give a schema-valid OPEN one bounded model-authored geometry retry
        # when its stop is too narrow or its LIMIT would cross the frozen
        # quote. Python never edits trade prices; risk remains authoritative.
        if isinstance(decoded, dict) and not nofx_gate:
            stop_bounds = _model_stop_repair_bounds(context, decoded)
            limit_bounds = _model_limit_repair_bounds(context, decoded)
            rr_bounds = _model_net_rr_repair_bounds(context, decoded) if stop_bounds is None and limit_bounds is None else None
            if (stop_bounds is not None or limit_bounds is not None or rr_bounds is not None) and (schema_repaired or wait_fact_repaired):
                # Bonsai's first response plus its schema repair already use
                # the bounded two-call budget. A third 27B inference has
                # repeatedly timed out and discarded an otherwise auditable
                # OPEN. Keep the exact model prices for the risk gate to reject
                # with its concrete reason instead of hiding them as timeout.
                repair_key = "entry_geometry_repair" if limit_bounds is not None else "stop_geometry_repair" if stop_bounds is not None else "net_rr_repair"
                context.model_inference_settings[repair_key] = {
                    "attempted": False,
                    "status": "SKIPPED_AFTER_SCHEMA_REPAIR" if schema_repaired else "SKIPPED_AFTER_WAIT_FACT_REPAIR",
                    "reason": "TWO_MODEL_CALL_LIMIT",
                    "required_min_stop_distance": stop_bounds["minimum_stop_distance"] if stop_bounds else None,
                    "limit_issue": limit_bounds["reason"] if limit_bounds else None,
                    "observed_net_rr": rr_bounds["observed_net_rr"] if rr_bounds else None,
                }
            elif stop_bounds is not None or limit_bounds is not None or rr_bounds is not None:
                original_decision = dict(decoded)
                original_raw_response = context.model_raw_response
                original_call_prompt_version = context.model_call_prompt_version
                original_prompt_version = context.prompt_version
                repair_key = "entry_geometry_repair" if limit_bounds is not None else "stop_geometry_repair" if stop_bounds is not None else "net_rr_repair"
                repair_version = "_entry_repair" if limit_bounds is not None else "_stop_repair" if stop_bounds is not None else "_net_rr_repair"
                symbol = str(decoded["instrument_id"]).upper()
                focused_inputs = _focused_entry_repair_inputs(prompt_payload, symbol)
                selected_strategy = dict(prompt_payload.get("active_strategy") or {})
                risk_geometry = selected_strategy.get("risk_geometry")
                if isinstance(risk_geometry, dict):
                    selected_strategy["risk_geometry"] = {
                        symbol: risk_geometry[symbol]
                    } if symbol in risk_geometry else {}
                focused_inputs["active_strategy"] = selected_strategy
                repair_schema = {
                    **AI_ACTION_SCHEMA,
                    "properties": dict(AI_ACTION_SCHEMA.get("properties") or {}),
                }
                for field, value in (("action", decoded["action"]), ("instrument_id", symbol)):
                    field_schema = dict(repair_schema["properties"].get(field) or {})
                    field_schema["enum"] = [value]
                    repair_schema["properties"][field] = field_schema
                if limit_bounds is not None:
                    repair_instruction = (
                        "\n\n限价几何修正：上一份 OPEN 的 LIMIT 穿越当前报价或距离超限，尚未送单。"
                        "保持动作、标的、订单类型、风险金额、杠杆、置信度及证据引用不变。"
                        "你可以重新给出被动侧的 entry_price=limit_price、相应 entry_zone、结构外 stop_price、"
                        "费用后达标的 take_profit，并同步更新 reason、invalidation_condition、strategy_plan。"
                        "做多限价不得高于报价，做空限价不得低于报价；距报价不得超过策略上限。"
                        "止损仍须满足 ATR/百分比底线。不得改为市价或编造结构；做不到就保持原提案供风控拒绝。"
                    )
                elif stop_bounds is not None:
                    repair_instruction = (
                        "\n\n止损几何修正：上一份 OPEN 的止损小于固定风险底线，尚未送单。"
                        "保持原动作、标的、入场价、订单类型、金额、杠杆及证据引用，"
                        "重新给出结构外的 stop_price 与费用后达标的 take_profit，"
                        "并同步更新 reason、invalidation_condition、strategy_plan。"
                        "不得改为市价、改变入场价或捏造目标结构；做不到就保持原提案供风控拒绝。"
                    )
                else:
                    repair_instruction = (
                        "\n\n费用后盈亏比复核：上一份 OPEN 的目标未达到冻结盘口手续费、滑点和策略净 RR 门槛，尚未送单。"
                        "保持动作、标的、入场价、止损、限价类型、金额、杠杆、置信度及证据引用不变。"
                        "核对 required_target_bound，只有已收盘 K 线结构支持该方向更远的目标时，"
                        "才重新给出 take_profit，并同步更新 reason、strategy_plan。"
                        "不能验证该目标时保持原提案供风控拒绝，不得编造结构或降低门槛。"
                    )
                repair_system = system_prompt + repair_instruction + "只输出完整 JSON 对象。"
                repair_user = json.dumps({
                    "previous_decision": original_decision,
                    "required_geometry": (
                        {"stop": stop_bounds, "limit": limit_bounds}
                        if limit_bounds is not None else stop_bounds if stop_bounds is not None else rr_bounds
                    ),
                    "inputs": focused_inputs,
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                context.model_inference_settings[repair_key] = {
                    "attempted": True,
                    "original_entry": original_decision.get("entry_price"),
                    "original_stop": original_decision.get("stop_price"),
                    "required_min_stop_distance": stop_bounds["minimum_stop_distance"] if stop_bounds else None,
                    "quote": limit_bounds["quote"] if limit_bounds else None,
                    "limit_issue": limit_bounds["reason"] if limit_bounds else None,
                    "observed_net_rr": rr_bounds["observed_net_rr"] if rr_bounds else None,
                    "status": "PENDING",
                }
                try:
                    if (
                        count_prompt_tokens(repair_system)
                        + count_prompt_tokens(repair_user)
                        + minimum_output_tokens
                        + PROMPT_BUDGET_SAFETY_MARGIN_TOKENS
                        > context_length
                    ):
                        raise ValueError("AI_GEOMETRY_REPAIR_BUDGET_EXCEEDED")
                    corrected_response = call_model(
                        [{"role": "system", "content": repair_system},
                         {"role": "user", "content": repair_user}],
                        AI_PROMPT_VERSION + repair_version,
                        schema=repair_schema,
                    )
                    corrected = corrected_response[0] if isinstance(corrected_response, tuple) else corrected_response
                    normalize_news_impact(corrected)
                    normalize_limit_ttl_alias(corrected)
                    normalize_wait_conditions(corrected)
                    normalize_wait_symbol_alias(corrected, context.allowed_instruments)
                    validate_schema(corrected, AI_ACTION_SCHEMA)
                    require_confidence_for_open(corrected)
                    require_entry_analysis_for_open(corrected)
                    _require_relevant_news_ref_for_open(corrected, prompt_news, decision_time)
                    mutable = (
                        {"stop_price", "take_profit", "reason", "invalidation_condition", "strategy_plan"}
                        if stop_bounds is not None or limit_bounds is not None
                        else {"take_profit", "reason", "strategy_plan"}
                    )
                    if limit_bounds is not None:
                        mutable.update({"entry_price", "limit_price", "entry_zone"})
                    if any(
                        corrected.get(field) != value
                        for field, value in original_decision.items() if field not in mutable
                    ) or any(field not in original_decision and field not in mutable for field in corrected):
                        raise ValueError("MODEL_GEOMETRY_REPAIR_CHANGED_DECISION")
                    if _model_stop_repair_bounds(context, corrected) is not None:
                        raise ValueError("MODEL_GEOMETRY_REPAIR_STILL_NARROW")
                    if _model_limit_repair_bounds(context, corrected) is not None:
                        raise ValueError("MODEL_GEOMETRY_REPAIR_LIMIT_INVALID")
                    if rr_bounds is not None and _model_net_rr_repair_bounds(context, corrected) is not None:
                        raise ValueError("MODEL_GEOMETRY_REPAIR_NET_RR_TOO_LOW")
                    decoded = corrected
                    context.model_inference_settings[repair_key].update(
                        status="MODEL_REVISED",
                        revised_entry=corrected.get("entry_price"),
                        revised_stop=corrected.get("stop_price"),
                        revised_target=corrected.get("take_profit"),
                    )
                except Exception as repair_error:
                    # Keep the first verified model proposal and its explicit
                    # rejection path. A failed retry may never create an order.
                    decoded = original_decision
                    context.model_raw_response = original_raw_response
                    context.model_call_prompt_version = original_call_prompt_version
                    context.prompt_version = original_prompt_version
                    context.model_inference_settings[repair_key].update(
                        status="FAILED_OR_UNSAFE",
                        reason=f"{type(repair_error).__name__}: {repair_error}"[:160],
                    )
        action = str(decoded.get("action", "")).strip().upper()
        if action not in ALLOWED_AI_ACTIONS:
            raise ValueError("INVALID_ACTION_SCHEMA")
        if action in {"OPEN_LONG", "OPEN_SHORT"}:
            settings = context.model_inference_settings if isinstance(context.model_inference_settings, dict) else {}
            receipt = {
                "model_id": context.model_id,
                "actual_model_id": settings.get("actual_model_id"),
                "model_identity_source": settings.get("model_identity_source"),
                "verified_manifest_model_id": settings.get("verified_manifest_model_id"),
                "model_version": context.model_version,
            }
            if (
                not context.model_call_attempted
                or not context.model_call_completed
                or not is_verified_bonsai_inference_receipt(receipt)
            ):
                raise ValueError("BONSAI_INFERENCE_RECEIPT_UNVERIFIED")
        instrument = str(decoded.get("instrument_id") or "").strip().upper()
        if not instrument:
            instrument = context.allowed_instruments[0] if context.allowed_instruments else ""
        reason = str(decoded.get("reason") or "").strip()
        if not reason or len(reason) > 2000:
            raise ValueError("INVALID_ACTION_REASON")
        allowed_fields = {
            "action", "instrument_id", "reason", "position_id", "order_id", "entry_condition", "next_trigger_price",
            "entry_price", "stop_price", "take_profit", "requested_risk_fraction",
            "position_size_usdt", "requested_leverage", "new_stop_price", "new_take_profit", "reduce_fraction", "evidence_refs",
            "order_preference", "limit_price", "ttl_seconds", "candidate_id", "closed_15m_bar",
            "strategy_candidate_id", "strategy_id", "market_summary", "timeframe_analysis",
            "strategy_analysis", "news_context", "entry_zone", "take_profit_1", "take_profit_2",
            "strategy_plan",
            "confidence", "invalidation_condition",
        }
        if set(decoded) - allowed_fields:
            raise ValueError("INVALID_ACTION_SCHEMA: unexpected model fields")
        values: dict[str, Any] = {}
        position_id = decoded.get("position_id")
        if position_id is not None:
            if not isinstance(position_id, str) or not position_id.strip() or len(position_id) > 160:
                raise ValueError("INVALID_POSITION_ID")
            values["position_id"] = position_id.strip()
        order_id = decoded.get("order_id")
        if order_id is not None:
            if not isinstance(order_id, str) or not order_id.strip() or len(order_id) > 160:
                raise ValueError("INVALID_ORDER_ID")
            values["order_id"] = order_id.strip()
        entry_condition = decoded.get("entry_condition")
        if entry_condition is not None:
            if not isinstance(entry_condition, str) or len(entry_condition) > 500:
                raise ValueError("INVALID_ENTRY_CONDITION")
            values["entry_condition"] = entry_condition
        numeric_fields = (
            "entry_price", "stop_price", "take_profit", "requested_risk_fraction", "position_size_usdt",
            "new_stop_price", "new_take_profit", "next_trigger_price", "reduce_fraction",
        )
        for field in numeric_fields:
            if field not in decoded or decoded[field] is None:
                continue
            if isinstance(decoded[field], bool) or not isinstance(decoded[field], (int, float)):
                raise ValueError(f"INVALID_{field.upper()}")
            try:
                number = float(decoded[field])
            except (TypeError, ValueError):
                raise ValueError(f"INVALID_{field.upper()}")
            if not math.isfinite(number):
                raise ValueError(f"INVALID_{field.upper()}")
            if field == "position_size_usdt" and number <= 0:
                raise ValueError("INVALID_POSITION_SIZE_USDT")
            values[field] = number
        if "requested_leverage" in decoded and decoded["requested_leverage"] is not None:
            if isinstance(decoded["requested_leverage"], bool) or not isinstance(decoded["requested_leverage"], int):
                raise ValueError("INVALID_REQUESTED_LEVERAGE")
            try:
                leverage = int(decoded["requested_leverage"])
            except (TypeError, ValueError):
                raise ValueError("INVALID_REQUESTED_LEVERAGE")
            if leverage < 1:
                raise ValueError("INVALID_REQUESTED_LEVERAGE")
            values["requested_leverage"] = leverage
        preference = decoded.get("order_preference", "AUTO")
        if not isinstance(preference, str) or preference.upper() not in {"MARKET", "LIMIT", "AUTO"}:
            raise ValueError("INVALID_ORDER_PREFERENCE")
        values["order_preference"] = preference.upper()
        for field in ("limit_price",):
            if field in decoded and decoded[field] is not None:
                try:
                    number = float(decoded[field])
                except (TypeError, ValueError):
                    raise ValueError(f"INVALID_{field.upper()}")
                if not math.isfinite(number) or number <= 0:
                    raise ValueError(f"INVALID_{field.upper()}")
                values[field] = number
        if "ttl_seconds" in decoded and decoded["ttl_seconds"] is not None:
            try:
                ttl = int(decoded["ttl_seconds"])
            except (TypeError, ValueError):
                raise ValueError("INVALID_TTL_SECONDS")
            if ttl < 60 or ttl > 1800:
                raise ValueError("INVALID_TTL_SECONDS")
            values["ttl_seconds"] = ttl
        for field in ("candidate_id", "closed_15m_bar"):
            if field in decoded and decoded[field] is not None:
                if not isinstance(decoded[field], str) or len(decoded[field]) > 200:
                    raise ValueError(f"INVALID_{field.upper()}")
                values[field] = decoded[field]
        if values.get("candidate_id") is None and decoded.get("strategy_candidate_id") is not None:
            values["candidate_id"] = decoded["strategy_candidate_id"]
        evidence_refs = decoded.get("evidence_refs", [])
        if not isinstance(evidence_refs, list) or any(not isinstance(item, str) or len(item) > 200 for item in evidence_refs):
            raise ValueError("INVALID_EVIDENCE_REFS")
        allowed_evidence_refs = set(context.evidence_refs)
        if any(item not in allowed_evidence_refs for item in evidence_refs):
            raise ValueError("INVALID_EVIDENCE_REF")
        values["evidence_refs"] = tuple(evidence_refs[:32])
        extra_fields: dict[str, Any] = {}
        if decoded.get("strategy_plan") is not None:
            plan = decoded["strategy_plan"]
            if not isinstance(plan, dict) or set(plan) != {"name", "thesis", "entry_conditions", "exit_conditions"}:
                raise ValueError("INVALID_STRATEGY_PLAN")
            for key, maximum in (("name", 100), ("thesis", 800)):
                if not isinstance(plan[key], str) or not plan[key].strip() or len(plan[key]) > maximum:
                    raise ValueError("INVALID_STRATEGY_PLAN")
            for key in ("entry_conditions", "exit_conditions"):
                if not isinstance(plan[key], list) or not 1 <= len(plan[key]) <= 8 or any(not isinstance(v, str) or not v.strip() or len(v) > 300 for v in plan[key]):
                    raise ValueError("INVALID_STRATEGY_PLAN")
            extra_fields["strategy_plan"] = plan
            if (
                context.model_inference_settings.get("strategy_plan_structure_source")
                == "DERIVED_FROM_MODEL_STRING"
            ):
                extra_fields["strategy_plan_structure_source"] = "DERIVED_FROM_MODEL_STRING"

        def bounded_text(field: str, maximum: int) -> None:
            value = decoded.get(field)
            if value is None:
                return
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"INVALID_{field.upper()}")
            extra_fields[field] = value

        bounded_text("strategy_candidate_id", 200)
        bounded_text("strategy_id", 100)
        bounded_text("market_summary", 1000)
        bounded_text("invalidation_condition", 800)
        for field in ("take_profit_1", "take_profit_2", "confidence"):
            if field not in decoded or decoded[field] is None:
                continue
            if isinstance(decoded[field], bool) or not isinstance(decoded[field], (int, float)):
                raise ValueError(f"INVALID_{field.upper()}")
            try:
                number = float(decoded[field])
            except (TypeError, ValueError):
                raise ValueError(f"INVALID_{field.upper()}")
            if not math.isfinite(number):
                raise ValueError(f"INVALID_{field.upper()}")
            if field == "confidence":
                if not 0 <= number <= 100:
                    raise ValueError("INVALID_CONFIDENCE")
                if 0 < number <= 1.0:
                    number = round(number * 100.0, 2)
            if field.startswith("take_profit") and number <= 0:
                raise ValueError(f"INVALID_{field.upper()}")
            extra_fields[field] = number

        timeframe_analysis = decoded.get("timeframe_analysis")
        if timeframe_analysis is not None:
            if not isinstance(timeframe_analysis, dict) or set(timeframe_analysis) - {"5m", "15m", "1h", "4h", "1d"}:
                raise ValueError("INVALID_TIMEFRAME_ANALYSIS")
            extra_fields["timeframe_analysis"] = {}
            for key, value in timeframe_analysis.items():
                if value is not None and (not isinstance(value, str) or len(value) > 800):
                    raise ValueError("INVALID_TIMEFRAME_ANALYSIS")
                extra_fields["timeframe_analysis"][key] = value

        strategy_analysis = decoded.get("strategy_analysis")
        if strategy_analysis is not None:
            if not isinstance(strategy_analysis, dict) or set(strategy_analysis) - {
                "strategy_id", "matched_conditions", "missing_conditions",
                "trigger_completion_pct", "next_trigger_price", "entry_condition",
            }:
                raise ValueError("INVALID_STRATEGY_ANALYSIS")
            normalized_analysis: dict[str, Any] = {}
            if strategy_analysis.get("strategy_id") is not None:
                if not isinstance(strategy_analysis["strategy_id"], str) or len(strategy_analysis["strategy_id"]) > 100:
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                normalized_analysis["strategy_id"] = strategy_analysis["strategy_id"]
            for key in ("matched_conditions", "missing_conditions"):
                values_list = strategy_analysis.get(key, [])
                if not isinstance(values_list, list) or any(not isinstance(item, str) or len(item) > 300 for item in values_list):
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                normalized_analysis[key] = values_list[:32]
            if strategy_analysis.get("trigger_completion_pct") is not None:
                try:
                    completion = float(strategy_analysis["trigger_completion_pct"])
                except (TypeError, ValueError):
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                if not math.isfinite(completion) or not 0 <= completion <= 100:
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                normalized_analysis["trigger_completion_pct"] = completion
            if strategy_analysis.get("next_trigger_price") is not None:
                try:
                    trigger_price = float(strategy_analysis["next_trigger_price"])
                except (TypeError, ValueError):
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                if not math.isfinite(trigger_price) or trigger_price <= 0:
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                normalized_analysis["next_trigger_price"] = trigger_price
            entry_condition = strategy_analysis.get("entry_condition")
            if entry_condition is not None:
                if not isinstance(entry_condition, str) or not entry_condition.strip() or len(entry_condition) > 500:
                    raise ValueError("INVALID_STRATEGY_ANALYSIS")
                normalized_analysis["entry_condition"] = entry_condition
            extra_fields["strategy_analysis"] = normalized_analysis

        news_context = decoded.get("news_context")
        if news_context is not None:
            if not isinstance(news_context, dict) or set(news_context) - {"impact", "summary"}:
                raise ValueError("INVALID_NEWS_CONTEXT")
            impact = news_context.get("impact")
            if impact is not None and impact not in {"POSITIVE", "NEGATIVE", "NEUTRAL", "UNKNOWN"}:
                raise ValueError("INVALID_NEWS_CONTEXT")
            summary = news_context.get("summary")
            if summary is not None and (not isinstance(summary, str) or len(summary) > 800):
                raise ValueError("INVALID_NEWS_CONTEXT")
            extra_fields["news_context"] = {"impact": impact, "summary": summary}

        entry_zone = decoded.get("entry_zone")
        if entry_zone is not None:
            if not isinstance(entry_zone, dict) or set(entry_zone) - {"low", "high"}:
                raise ValueError("INVALID_ENTRY_ZONE")
            normalized_zone: dict[str, float | None] = {}
            for key in ("low", "high"):
                value = entry_zone.get(key)
                if value is None:
                    normalized_zone[key] = None
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise ValueError("INVALID_ENTRY_ZONE")
                if not math.isfinite(number) or number <= 0:
                    raise ValueError("INVALID_ENTRY_ZONE")
                normalized_zone[key] = number
            if normalized_zone["low"] is not None and normalized_zone["high"] is not None and normalized_zone["low"] > normalized_zone["high"]:
                raise ValueError("INVALID_ENTRY_ZONE")
            extra_fields["entry_zone"] = normalized_zone

        record_sft_sample(
            cycle_id=context.cycle_id,
            system_prompt=system_prompt,
            user_prompt=serialized_prompt_payload,
            model_output=decoded,
            account_id=getattr(context, "account_id", None),
            mode=getattr(context, "mode", None),
            metadata={
                "strategy_id": getattr(context, "strategy_id", None),
                "confidence": decoded.get("confidence"),
            },
        )

        if extra_fields:
            values["extra_fields"] = extra_fields
        if (action == "WAIT" and context.decision_contract == CONTRACT
                and str(context.venue or "").lower() == "gate"
                and str(context.mode.value if isinstance(context.mode, TradingMode) else context.mode).upper() in {"TESTNET", "LIVE"}):
            analysis = decoded.get("strategy_analysis") if isinstance(decoded.get("strategy_analysis"), dict) else {}
            missing = analysis.get("missing_conditions") if isinstance(analysis.get("missing_conditions"), list) else []
            if not any(str(item).strip() for item in missing):
                raise ValueError("WAIT_MISSING_CONDITIONS_REQUIRED")
            if values.get("next_trigger_price") is None and not str(values.get("entry_condition") or "").strip():
                raise ValueError("WAIT_NEXT_TRIGGER_REQUIRED")
        return AIActionOutput(action=action, instrument_id=instrument, reason=reason, **values)

    def _cancel_model_generation(self, context: AICycleContext) -> None:
        provider = self.model_provider
        for name in ("cancel_generation", "cancel"):
            callback = getattr(provider, name, None)
            if callable(callback):
                try:
                    callback(context.cycle_id, context.generation)
                except TypeError:
                    try:
                        callback(context.cycle_id)
                    except Exception:
                        pass
                except Exception:
                    pass
                break

    def _cancel_active_generation(self) -> None:
        """Ask the provider to stop in-flight work before a lease/session loss.

        ThreadPoolExecutor cannot kill a provider call.  The explicit cancel
        hook is therefore best effort, while the session generation and
        gateway fence remain the authoritative commit boundary.
        """
        with self._lock:
            context = self._active_context
            future = self._active_future
        if future is not None:
            future.cancel()
        if context is not None:
            self._cancel_model_generation(context)

    def _calibrate_scope(
        self,
        *,
        account_id: str,
        venue: str,
        mode: TradingMode,
        session_id: str | None,
        symbols: tuple[str, ...],
        now: datetime,
    ) -> dict[str, Any]:
        scope = resolve_account_scope(self.store, account_id) or {}
        environment = str(scope.get("environment") or mode.value).lower()
        active = self._calibration.active_profile(account_id, environment=environment, now=now)
        if active is not None:
            result = {
                "status": "READY",
                "profile_id": active.get("profile_id"),
                "profile": active.get("profile") or {},
                "model_digest": active.get("model_digest"),
                "digest_status": active.get("digest_status"),
                "sample_size": active.get("sample_size"),
                "expires_at": active.get("expires_at"),
            }
            with self._lock:
                self._calibration_state = "READY"
                self._calibration_run = result
            return result
        with self._lock:
            self._calibration_state = "CALIBRATING"
        result = self._calibration.run(
            account_id=account_id,
            provider=venue,
            environment=environment,
            session_id=session_id,
            symbols=symbols,
            model_provider=self.model_provider,
            now=now,
            min_bars=self.calibration_min_bars,
            lookback_days=self.calibration_lookback_days,
        )
        with self._lock:
            self._calibration_state = "READY" if result.get("status") == "READY" else "NOT_READY"
            self._calibration_run = dict(result)
        return result

    def _news_revisions(self, symbols: tuple[str, ...], *, now: datetime) -> list[dict[str, Any]]:
        """Load only point-in-time stored event revisions for the model context."""

        if not callable(getattr(self.store, "list_event_evidence", None)):
            return []
        revisions: list[dict[str, Any]] = []
        published_since = _iso(now - timedelta(hours=48))
        as_of = _iso(now)
        for symbol in symbols:
            try:
                rows = self.store.list_event_evidence(
                    symbol=symbol,
                    as_of=as_of,
                    published_since=published_since,
                    limit=4,
                )
            except Exception:
                rows = []
            for item in rows if isinstance(rows, list) else []:
                if not isinstance(item, dict):
                    continue
                revision_id = item.get("revision_id") or item.get("event_id") or item.get("news_id")
                if not revision_id:
                    continue
                affected_symbols = list(
                    item.get("affected_symbols") or item.get("symbols") or [symbol]
                )
                scope = str(item.get("scope") or "SYMBOL").strip().upper()
                if scope not in {"SYMBOL", "MARKET_WIDE"}:
                    scope = "SYMBOL"
                event_symbol = symbol if scope == "SYMBOL" else None
                revision = {
                        "revision_id": str(revision_id),
                        "scope": scope,
                        "symbol": event_symbol,
                        "symbols": affected_symbols,
                        "title": str(item.get("title") or item.get("headline") or "")[:320],
                        "summary": str(item.get("summary") or item.get("description") or "")[:1000],
                        "url": str(item.get("url") or item.get("source_url") or "")[:500],
                        "published_at": item.get("published_at"),
                        "known_at": item.get("known_at"),
                        "source": str(item.get("source") or item.get("publisher") or "")[:160],
                        "impact": str(item.get("impact") or item.get("sentiment") or "UNKNOWN").upper(),
                    }
                if relevant_news_for_entry(revision, symbol, now):
                    revisions.append(revision)
        deduped: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for item in revisions:
            identity = str(item["revision_id"])
            if identity in seen:
                previous = deduped[seen[identity]]
                if item.get("scope") == "MARKET_WIDE":
                    previous["scope"] = "MARKET_WIDE"
                    previous["symbol"] = None
                    previous["symbols"] = list(
                        dict.fromkeys(
                            str(value)
                            for value in [*(previous.get("symbols") or []), *(item.get("symbols") or [])]
                            if str(value).strip()
                        )
                    )
                continue
            seen[identity] = len(deduped)
            deduped.append(item)
        return deduped[:20]

    def _blocked_cycle(self, context: AICycleContext, reason: str) -> Any:
        block_stage = infer_block_stage(reason, getattr(context, "stage_block", None))
        attempted = bool(getattr(context, "model_call_attempted", False))
        completed_flag = getattr(context, "model_call_completed", None)
        # Older callers/tests that set attempted directly retain their
        # historical semantics. Production _model_output always initializes
        # completed_flag, so a transport failure is never persisted as called.
        model_called = attempted if completed_flag is None else attempted and bool(completed_flag)
        output = AIActionOutput(
            action="WAIT",
            instrument_id=context.allowed_instruments[0] if context.allowed_instruments else "",
            reason=reason,
            decision_origin="SYSTEM",
            extra_fields={
                "is_model_decision": False,
                "model_called": model_called,
                "model_attempted": attempted,
                "model_result": "INVALID_OR_DISCARDED" if model_called else "MODEL_UNAVAILABLE" if attempted else "NOT_RUN",
                "block_stage": block_stage,
                "operational_state": "SYSTEM_BLOCKED",
                "blocked_code": str(reason).split(":", 1)[0],
                "human_message": humanize_reason(reason, stage=block_stage),
                "dynamic_risk": context.dynamic_risk if isinstance(context.dynamic_risk, dict) else {},
            },
        )
        engine = AILedDecisionEngine(
            store=self.store,
            execution_gateway=self.gateway,
            risk_engine=self.risk_engine,
            ledger=self.ledger,
            guardian=self.guardian,
            model_runner=None,
        )
        # AILedDecisionEngine combines context.model_call_attempted with the
        # output flag; downgrade only failed transport attempts before writing.
        context.model_call_attempted = model_called
        result = engine.execute_cycle(context, now=self.clock(), model_output=output)
        if attempted and not model_called:
            try:
                with self.store._connect() as db:
                    db.execute(
                        "UPDATE ai_led_cycles SET model_call_status='MODEL_UNAVAILABLE' WHERE cycle_id=?",
                        (context.cycle_id,),
                    )
            except Exception:
                logger.warning("Could not persist unavailable model-call status", exc_info=True)
        return result

    def _refresh_remote_account_truth(self, account_id: str, mode: TradingMode) -> dict[str, Any]:
        """Read Gate TestNet facts before calibration or model generation.

        A TestNet cycle cannot use the local ledger as an account substitute.
        This method intentionally returns a blocked-shaped fact on every
        failure; the caller decides whether to persist a system-blocked cycle.
        """

        scope = resolve_account_scope(self.store, account_id) or {}
        expected_type = "GATE_TESTNET" if mode is TradingMode.TESTNET else "GATE_LIVE" if mode is TradingMode.LIVE else ""
        if not expected_type or str(scope.get("account_type") or "").upper() != expected_type:
            return {}
        truth_service = GateAccountTruthService(self.store, clock=self.clock)
        try:
            trader = build_gate_trader(self.store, account_id)
        except Exception:
            trader = None
        if trader is None:
            return truth_service.refresh(account_id, object(), include_trades=False)
        return truth_service.refresh(account_id, trader, include_trades=False)

    def run_cycle_once(self, scheduled_at: str | datetime | None = None) -> Any:
        if not self._cycle_lock.acquire(blocking=False):
            raise RuntimeError("AI_CYCLE_ALREADY_RUNNING")
        try:
            if self._inflight_future is not None and not self._inflight_future.done():
                raise RuntimeError("AI_MODEL_STILL_RUNNING")
            return self._run_cycle_once(scheduled_at)
        finally:
            self._cycle_lock.release()

    def _run_cycle_once(self, scheduled_at: str | datetime | None = None) -> Any:
        """Run one complete aligned cycle; manual callers may provide a schedule."""
        with self._lock:
            account_id = self._account_id
            mode = self._mode
            venue = self._venue
            enabled = self._enabled
        session = self.session_manager.status()
        now = _as_utc(self.clock())
        strategy = self._active_strategy(account_id)
        scan_interval_minutes = self._strategy_scan_minutes(account_id)
        scheduled_point = (
            _parse_time(scheduled_at)
            if scheduled_at is not None
            else self._aligned_scan_at(now, scan_interval_minutes)
        )
        scheduled_point = scheduled_point or self._aligned_scan_at(now, scan_interval_minutes)
        scheduled_text = _iso(scheduled_point)
        cycle_started = now
        with self._lock:
            self._last_scheduled_at = scheduled_text
            self._last_started_at = _iso(cycle_started)
            self._next_scan_at = _iso(self._next_aligned_scan(now, scan_interval_minutes))
            self._last_lag_ms = max(0.0, (now - scheduled_point).total_seconds() * 1000.0)
        cycle_id = f"cycle_{now.strftime('%Y%m%dT%H%M%S%fZ')}_{threading.get_ident()}"
        if not enabled or not account_id or mode is None:
            missing_account = not account_id
            account_id = account_id or "unconfigured"
            context = AICycleContext(
                cycle_id=cycle_id,
                account_id=account_id,
                generation=int(session.get("generation") or 0),
                started_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
                allowed_instruments=(),
                mode=mode or TradingMode.PAPER,
                venue=venue,
                session_id=session.get("session_id"),
                strategy_instructions=strategy,
            )
            result = self._blocked_cycle(context, "ACCOUNT_REQUIRED" if missing_account else "AI_SESSION_NOT_ENABLED")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        valid, validation_reason = self.session_manager.validate_execution(str(session.get("session_id")), int(session.get("generation") or 0))
        if not valid:
            context = AICycleContext(
                cycle_id=cycle_id,
                account_id=account_id,
                generation=int(session.get("generation") or 0),
                started_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=INTENT_TTL_SECONDS)),
                allowed_instruments=(),
                mode=mode,
                venue=venue,
                session_id=session.get("session_id"),
                strategy_instructions=strategy,
            )
            result = self._blocked_cycle(context, f"SESSION_NOT_EXECUTABLE: {validation_reason}")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        # TradingAuthorization used to stop the model before it could inspect
        # the market.  The account's Gate credential and downstream risk
        # engine now own authorization; there is no local scope record to
        # grant, expire or revoke here.
        authorization = None

        # Gate TestNet is a remote-account execution environment.  Perform
        # this read-only truth check before calibration/model calls so a
        # missing credential or degraded private response cannot be turned
        # into a model WAIT or a local 10000-unit risk budget.
        account_truth = self._refresh_remote_account_truth(account_id, mode)
        if mode in {TradingMode.TESTNET, TradingMode.LIVE} and (
            str(account_truth.get("status") or "").upper() != "AVAILABLE"
            or account_truth.get("equity") is None
            or account_truth.get("available_margin") is None
        ):
            context = self._build_context(
                cycle_id=cycle_id,
                now=now,
                session_id=str(session.get("session_id")),
                generation=int(session.get("generation") or 0),
                account_id=account_id,
                mode=mode,
                venue=venue,
                authorization=authorization,
                snapshots={},
                candidates=[],
                calibration={},
                account_truth=account_truth,
                scheduled_at=scheduled_text,
                strategy_instructions=strategy,
            )
            reason = str(account_truth.get("error_code") or "REMOTE_ACCOUNT_TRUTH_UNAVAILABLE")
            result = self._blocked_cycle(context, reason)
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        # Exchange truth has been read and the runtime lease is valid.  Settle
        # old submitted intents before asking the model for new risk: a Gate
        # order can finish with zero fills while its local ACK/reservation
        # survives, making every subsequent OPEN fail at reservation time.
        if mode in {TradingMode.TESTNET, TradingMode.LIVE} and hasattr(self.gateway, "reconcile_in_flight_orders"):
            try:
                self.gateway.reconcile_in_flight_orders(account_id, mode, remote_truth=account_truth)
            except Exception:
                logger.exception("Gate TestNet in-flight order reconciliation failed")
        if mode in {TradingMode.TESTNET, TradingMode.LIVE} and hasattr(self.gateway, "cleanup_closed_gate_protection"):
            try:
                cleaned = self.gateway.cleanup_closed_gate_protection(account_id=account_id, remote_truth=account_truth)
                if cleaned:
                    # The model must receive a fresh Gate snapshot after the
                    # orphan is removed, not the pre-cancel pending list.
                    account_truth = self._refresh_remote_account_truth(account_id, mode)
            except Exception:
                logger.exception("Gate TestNet closed-position protection cleanup failed")

        # Probe the required Smart model before calibration.  This prevents a
        # provider whose normal default is Bonsai-2-27B-PTQ1_0 from making any
        # calibration call when Bonsai-2-27B-PTQ1_0 is unavailable.
        health = self._health(force=True)
        if health.get("status") != "READY":
            context = self._build_context(
                cycle_id=cycle_id,
                now=now,
                session_id=str(session.get("session_id")),
                generation=int(session.get("generation") or 0),
                account_id=account_id,
                mode=mode,
                venue=venue,
                authorization=authorization,
                snapshots={},
                candidates=[],
                calibration={},
                scheduled_at=scheduled_text,
                account_truth=account_truth,
                strategy_instructions=strategy,
            )
            result = self._blocked_cycle(context, str(health.get("reason_code") or "SMART_MODEL_UNAVAILABLE"))
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        signal_timeframe, context_timeframes, candidate_strategy_ids = self._strategy_scan_contract(strategy)
        remote_positions = account_truth.get("positions") if isinstance(account_truth.get("positions"), list) else []
        remote_pending = account_truth.get("pending_orders") if isinstance(account_truth.get("pending_orders"), list) else []
        owned_pending: list[dict[str, Any]] = []
        if mode in {TradingMode.TESTNET, TradingMode.LIVE} and remote_pending:
            with self.store._connect() as db:
                rows = db.execute(
                    """SELECT execution_result_json FROM order_intents WHERE account_id=? AND mode=?
                       AND venue='gate' AND reduce_only=0 AND intent_id LIKE 'intent_ai_%'
                       AND status IN ('ACKNOWLEDGED','SUBMITTED','PARTIALLY_FILLED','CANCEL_PENDING')""",
                    (account_id, mode.value),
                ).fetchall()
            own_ids = set()
            for row in rows:
                try:
                    receipt = json.loads(row["execution_result_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if receipt.get("order_id"):
                    own_ids.add(str(receipt["order_id"]))
            owned_pending = [
                {"symbol": order.get("symbol"), "instrument_id": order.get("symbol")}
                for order in remote_pending if isinstance(order, dict)
                and not order.get("reduce_only") and str(order.get("order_id") or "") in own_ids
            ]
        wait_streak = 0
        if mode in {TradingMode.TESTNET, TradingMode.LIVE}:
            with self.store._connect() as db:
                recent = db.execute(
                    """SELECT action,status FROM ai_led_cycles WHERE account_id=?
                       AND COALESCE(decision_origin,'MODEL')='MODEL'
                       ORDER BY created_at DESC LIMIT 12""",
                    (account_id,),
                ).fetchall()
            for previous in recent:
                if str(previous["action"] or "").upper() == "WAIT" and str(previous["status"] or "").upper() == "WAITING":
                    wait_streak += 1
                else:
                    break
        symbol_limit = _deep_scan_symbol_limit(wait_streak)
        with self._lock:
            self._current_symbol_limit = symbol_limit
        try:
            universe_snapshot = self._select_market_universe(
                strategy,
                mode=mode,
                scheduled_at=scheduled_point,
                positions=[*[dict(item) for item in remote_positions if isinstance(item, dict)], *owned_pending],
                account_id=account_id,
                symbol_limit=symbol_limit,
            )
            universe_snapshot["wait_streak"] = wait_streak
            universe_snapshot["symbol_limit"] = symbol_limit
            symbols = tuple(
                dict.fromkeys(
                    str(item).strip().upper()
                    for item in universe_snapshot.get("selected_symbols", [])
                    if str(item).strip()
                )
            )[:MAX_CYCLE_SYMBOLS]
        except Exception as exc:
            logger.exception("AI market universe selection failed")
            universe_snapshot = {
                "status": "UNAVAILABLE",
                "environment": "TESTNET" if mode is TradingMode.TESTNET else "LIVE",
                "selected_symbols": [],
                "error": f"{type(exc).__name__}: {exc}"[:320],
            }
            context = self._build_context(
                cycle_id=cycle_id,
                now=now,
                session_id=str(session.get("session_id")),
                generation=int(session.get("generation") or 0),
                account_id=account_id,
                mode=mode,
                venue=venue,
                authorization=authorization,
                snapshots={},
                candidates=[],
                account_truth=account_truth,
                scheduled_at=scheduled_text,
                allowed_instruments=(),
                strategy_instructions=strategy,
                universe_snapshot=universe_snapshot,
            )
            result = self._blocked_cycle(context, "MARKET_UNIVERSE_UNAVAILABLE")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        if not symbols:
            context = self._build_context(
                cycle_id=cycle_id,
                now=now,
                session_id=str(session.get("session_id")),
                generation=int(session.get("generation") or 0),
                account_id=account_id,
                mode=mode,
                venue=venue,
                authorization=authorization,
                snapshots={},
                candidates=[],
                account_truth=account_truth,
                scheduled_at=scheduled_text,
                allowed_instruments=(),
                strategy_instructions=strategy,
                universe_snapshot=universe_snapshot,
            )
            result = self._blocked_cycle(context, "MARKET_UNIVERSE_EMPTY")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        refresh = getattr(self.service, "refresh_ai_inputs", None)
        if callable(refresh):
            # Read public K-lines/news only. Failure remains missing evidence;
            # no fabricated candidate or model WAIT is created here.
            try:
                parameters = inspect.signature(refresh).parameters
                accepts_context = "context_timeframes" in parameters or any(
                    item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
                )
                if accepts_context:
                    refresh(
                        symbols,
                        scan_interval_minutes=scan_interval_minutes,
                        context_timeframes=context_timeframes,
                        **({"testnet": mode is TradingMode.TESTNET} if "testnet" in parameters else {}),
                    )
                else:
                    refresh(symbols, scan_interval_minutes=scan_interval_minutes)
            except Exception:
                logger.exception("AI public input refresh failed")
            now = _as_utc(self.clock())
        calibration = self._calibrate_scope(
            account_id=account_id,
            venue=venue,
            mode=mode,
            session_id=str(session.get("session_id") or "") or None,
            symbols=symbols,
            now=now,
        )
        if calibration.get("status") != "READY":
            context = self._build_context(
                cycle_id=cycle_id,
                now=now,
                session_id=str(session.get("session_id")),
                generation=int(session.get("generation") or 0),
                account_id=account_id,
                mode=mode,
                venue=venue,
                authorization=authorization,
                snapshots={},
                calibration=calibration,
                scheduled_at=scheduled_text,
                account_truth=account_truth,
                allowed_instruments=symbols,
                strategy_instructions=strategy,
                universe_snapshot=universe_snapshot,
            )
            result = self._blocked_cycle(context, str(calibration.get("error_code") or "CALIBRATION_NOT_READY"))
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        candidates = self._scanner.scan(
            account_id=account_id,
            provider=venue,
            environment=str((resolve_account_scope(self.store, account_id) or {}).get("environment") or mode.value).lower(),
            symbols=symbols,
            now=now,
            calibration_profile=calibration,
            strategy_ids=candidate_strategy_ids,
            signal_timeframe_override=signal_timeframe,
            context_timeframes_override=context_timeframes,
        )
        with self._lock:
            self._candidate_count = len(candidates)
        snapshots = self._market_snapshots(symbols, now=now, timeframe=signal_timeframe, universe_snapshot=universe_snapshot)
        news_revisions = self._news_revisions(symbols, now=now)
        context = self._build_context(
            cycle_id=cycle_id,
            now=now,
            session_id=str(session.get("session_id")),
            generation=int(session.get("generation") or 0),
            account_id=account_id,
            mode=mode,
            venue=venue,
            authorization=authorization,
            snapshots=snapshots,
            candidates=candidates,
            calibration=calibration,
            account_truth=account_truth,
            news_revisions=news_revisions,
            scheduled_at=scheduled_text,
            allowed_instruments=symbols,
            strategy_instructions=strategy,
            universe_snapshot=universe_snapshot,
        )
        if not snapshots:
            result = self._blocked_cycle(context, "MARKET_DATA_UNAVAILABLE")
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        health = self._health(force=True)
        if health.get("status") != "READY":
            result = self._blocked_cycle(context, str(health.get("reason_code") or "SMART_MODEL_UNAVAILABLE"))
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result

        future: Future[Any] = self._executor.submit(self._model_output, context)
        self._inflight_future = future
        with self._lock:
            self._active_context = context
            self._active_future = future
        try:
            output = future.result(timeout=min(self.model_budget_seconds, INTENT_TTL_SECONDS))
        except FutureTimeout:
            future.cancel()
            self._cancel_model_generation(context)
            result = self._blocked_cycle(context, "MODEL_TIMEOUT_DISCARDED")
            result.status = "TIMEOUT_DISCARDED"
            self._correct_persisted_outcome(result)
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        except Exception as exc:
            future.cancel()
            if bool(context.model_call_completed) and isinstance(exc, ValueError):
                # A completed Bonsai response that fails local validation is
                # not a model outage. Keep the exact validation detail while
                # presenting the correct stage and user-facing cause.
                reason = f"INVALID_MODEL_OUTPUT_SCHEMA: {type(exc).__name__}: {exc}"
            elif str(exc).startswith("AI_INPUT_BUDGET_EXCEEDED"):
                reason = f"AI_INPUT_BUDGET_EXCEEDED: {exc}"
            else:
                reason = f"SMART_MODEL_UNAVAILABLE: {type(exc).__name__}: {exc}"
            result = self._blocked_cycle(context, reason)
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        finally:
            with self._lock:
                if self._active_future is future:
                    self._active_future = None
                    self._active_context = None

        valid, validation_reason = self.session_manager.validate_execution(context.session_id or "", context.generation)
        if not valid:
            result = self._blocked_cycle(context, f"STALE_GENERATION_DISCARDED: {validation_reason}")
            result.status = "TIMEOUT_DISCARDED"
            self._correct_persisted_outcome(result)
            self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
            return result
        result = self._execute_model_decision(context, output, now=_as_utc(self.clock()))
        self._record_result(result, context=context, scheduled_at=scheduled_text, started_at=cycle_started)
        return result

    def _record_result(
        self,
        result: Any,
        *,
        context: AICycleContext | None = None,
        scheduled_at: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        completed = _as_utc(self.clock())
        started = started_at or (_parse_time(context.started_at) if context else completed) or completed
        duration_ms = max(0.0, (completed - started).total_seconds() * 1000.0)
        with self._lock:
            self._last_cycle_id = getattr(result, "cycle_id", None)
            self._last_cycle_status = getattr(result, "status", None)
            self._last_operational_state = getattr(result, "operational_state", None)
            self._last_cycle_at = _iso(completed)
            self._last_completed_at = _iso(completed)
            self._last_duration_ms = duration_ms
            self._last_scheduled_at = scheduled_at or self._last_scheduled_at
            self._last_reason = str(getattr(result, "reason", "cycle_complete"))
            if getattr(result, "status", "") in {"BLOCKED", "REJECTED", "TIMEOUT_DISCARDED"}:
                self._last_error = self._last_reason
            else:
                self._last_error = None
            account_id = self._account_id
            mode = self._mode.value if self._mode else None
            venue = self._venue
        if context is None or not account_id or account_id == "unconfigured":
            return
        scope = resolve_account_scope(self.store, account_id) or {}
        environment = str(scope.get("environment") or context.execution_environment or mode or "unknown").lower()
        output = getattr(result, "action_output", None)
        intent = getattr(result, "order_intent", None)
        candidate_id = getattr(intent, "candidate_id", None) if intent is not None else getattr(output, "candidate_id", None)
        symbol = getattr(output, "instrument_id", None)
        memory = None
        if str(getattr(result, "decision_origin", "MODEL")).upper() == "MODEL":
            try:
                memory = record_decision_memory(
                    self.store,
                    account_id=account_id,
                    provider=str(scope.get("provider") or context.provider or venue),
                    environment=environment,
                    cycle_id=str(getattr(result, "cycle_id", "")),
                    session_id=context.session_id,
            candidate_id=candidate_id,
            symbol=symbol,
            action=str(getattr(output, "action", "WAIT")),
            cycle_status=str(getattr(result, "status", "UNKNOWN")),
            decision_at=completed,
            reason=str(getattr(result, "reason", "")),
            payload={
                "authorization_id": context.authorization_id,
                "model_id": context.model_id,
                "model_digest_status": context.model_digest_status,
                "execution_intent_id": getattr(intent, "intent_id", None),
                "order_preference": getattr(output, "order_preference", "AUTO"),
                "strategy_template_id": str(context.strategy_instructions.get("template_id") or "custom")[:64],
                "strategy_name": str(context.strategy_instructions.get("name") or "")[:80],
                "strategy_style": str(context.strategy_instructions.get("style") or "CUSTOM")[:32],
            },
                    decision_origin="MODEL",
                )
            except Exception:
                logger.warning("Failed to persist AI decision memory", exc_info=True)
        else:
            try:
                self.store.record_runtime_diagnostic(
                    state=str(getattr(result, "operational_state", "PRECHECK_BLOCKED")),
                    code=str(getattr(result, "reason", "PRECHECK_BLOCKED")).split(":", 1)[0],
                    account_id=account_id,
                    session_id=context.session_id,
                    cycle_id=str(getattr(result, "cycle_id", "")),
                    payload={"reason": str(getattr(result, "reason", "")), "decision_origin": getattr(result, "decision_origin", None)},
                    occurred_at=completed,
                )
            except Exception:
                logger.warning("Failed to persist AI runtime diagnostic", exc_info=True)
        if hasattr(self.store, "_connect") and getattr(result, "cycle_id", None):
            try:
                with self.store._connect() as db:
                    db.execute(
                        """UPDATE ai_led_cycles SET completed_at=?, duration_ms=?, lag_ms=?,
                           scheduled_at=?, next_scan_at=?, decision_memory_id=? WHERE cycle_id=?""",
                        (_iso(completed), duration_ms, self._last_lag_ms, scheduled_at or self._last_scheduled_at, self._next_scan_at, memory.get("memory_id") if isinstance(memory, dict) else None, str(result.cycle_id)),
                    )
            except Exception:
                logger.warning("Failed to persist AI cycle timing", exc_info=True)

    def _correct_persisted_outcome(self, result: Any) -> None:
        """Keep the durable cycle row aligned with a coordinator discard.

        ``AILedDecisionEngine`` persists a safe WAIT before this coordinator
        knows whether the model future timed out or became stale.  The final
        lifecycle outcome is still a durable fact and must not remain
        misleadingly recorded as ``WAITING``.
        """
        cycle_id = getattr(result, "cycle_id", None)
        if not cycle_id or not hasattr(self.store, "_connect"):
            return
        try:
            with self.store._connect() as db:
                db.execute(
                    "UPDATE ai_led_cycles SET status=?, reason=? WHERE cycle_id=?",
                    (
                        str(getattr(result, "status", "UNKNOWN")),
                        str(getattr(result, "reason", "")),
                        str(cycle_id),
                    ),
                )
        except Exception:
            logger.warning("Failed to correct persisted AI cycle outcome", exc_info=True)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                account_id = self._account_id
            strategy = self._active_strategy(account_id)
            interval = self._strategy_scan_minutes(account_id)
            revision = int(strategy.get("revision") or 0)
            now = _as_utc(self.clock())
            next_boundary = self._next_aligned_scan(now, interval)
            with self._lock:
                self._next_scan_at = _iso(next_boundary)
            # The active strategy owns the cadence. Waking after a strategy
            # edit only recalculates the next boundary; it never triggers an
            # unscheduled order cycle.
            wait_seconds = max(0.0, (next_boundary - now).total_seconds())
            if self._wake_event.wait(wait_seconds):
                self._wake_event.clear()
                continue
            if self._stop_event.is_set():
                break
            latest_strategy = self._active_strategy(account_id)
            latest_interval = self._strategy_scan_minutes(account_id)
            if int(latest_strategy.get("revision") or 0) != revision or latest_interval != interval:
                continue
            schedule = getattr(self, "_schedule", None) or StrategySchedule(self.store)
            self._schedule = schedule
            if not account_id or not schedule.claim(account_id, next_boundary, revision):
                continue
            try:
                self.run_cycle_once(scheduled_at=next_boundary)
            except Exception as exc:
                logger.exception("AI cycle failed")
                with self._lock:
                    self._last_reason = "cycle_failed"
                    self._last_error = f"{type(exc).__name__}: {exc}"

    def wake(self) -> None:
        """Recompute the active strategy cadence without running an early cycle."""
        self._wake_event.set()

    def start(self, *, account_id: str | None = None) -> dict[str, Any]:
        account, error = self._configure_scope(account_id or self._account_id)
        if error:
            with self._lock:
                self._enabled = False
                self._last_reason = error
                self._last_error = error
            return self.status()
        del account
        with self._lock:
            session = self.session_manager.status()
            self._enabled = True
            self._stop_event.clear()
            self._wake_event.clear()
            if self._thread is not None and self._thread.is_alive():
                self._last_reason = "already_active"
                return self.status()
            self._last_reason = "explicit_start"
            self._last_error = None
            self._thread = threading.Thread(target=self._loop, name="aima-ai-led-coordinator", daemon=True)
            self._thread.start()
            return self.status()

    def pause(self, *, reason: str = "explicit_pause") -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
            self._enabled = False
            self._last_reason = reason
        self._cancel_active_generation()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._wake_event.set()
            thread = self._thread
            self._enabled = False
            self._last_reason = "explicit_terminate"
        self._cancel_active_generation()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return self.status()

    def close(self) -> None:
        self.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["AISessionCoordinator", "AI_COORDINATOR_CONTRACT_VERSION"]
