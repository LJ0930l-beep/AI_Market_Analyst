"""Regression tests for the AI cycle's context guard and Bonsai route contract.

These lock in the two defects that made every cycle fail:

1. The Python session claimed a 32K window while the running llama-server was
   configured for 8192 tokens, so the model lost the JSON contract. The guard
   rejects prompts that do not fit the configured context budget.
2. The analysis adapter must use the single ModelClient route and verify the
   exact model manifest before inference; legacy Ollama settings cannot route it.
"""

from __future__ import annotations

import copy
import json

import pytest

from core.ai.ollama import OllamaProvider
from core.model_client import model_client
from core.model_routing import DEFAULT_SMART_MODEL
from core.trading.ai_session_coordinator import (
    DECISION_OUTPUT_TOKEN_BUDGET,
    INFERENCE_SOFT_CONTEXT_LIMIT,
    MIN_MODEL_CONTEXT_LENGTH,
    MODEL_CONTEXT_LENGTH,
    PROMPT_BUDGET_SAFETY_MARGIN_TOKENS,
    _assert_prompt_fits,
    _estimate_tokens,
    _fit_prompt_payload,
    _session_context_length,
)


# --------------------------------------------------------------------------
# token accounting
# --------------------------------------------------------------------------


def test_token_estimate_counts_cjk_per_glyph() -> None:
    """Chinese must not be charged at 4 characters per token."""
    text = "中文" * 500  # 1000 glyphs
    assert _estimate_tokens(text) == 1000
    # The old, flattering accounting would have reported 250.
    assert _estimate_tokens(text) > len(text) // 4


def test_token_estimate_is_calibrated_against_measured_json_payloads() -> None:
    """JSON is denser than prose: 1.75 chars/token, not 4.

    The largest saved prompt was measured at 26759 tokens over 49053
    characters.  The estimate must not fall below that, or the guard would let
    a truncating prompt through.
    """
    assert _estimate_tokens("a" * 400) == 229
    # A payload of the real worst-case size must estimate at or above the
    # observed model token count.
    assert _estimate_tokens("a" * 49053) >= 26759
    # ...and must not be wildly over either, or every cycle would be blocked.
    assert _estimate_tokens("a" * 49053) < 40000


# --------------------------------------------------------------------------
# context budget guard
# --------------------------------------------------------------------------


def test_guard_rejects_the_measured_real_prompt_at_8192() -> None:
    """The real prompt is ~8.7k tokens; 8192 must be a hard failure, not silence."""
    system_content = "系" * 2556  # measured system prompt size in tokens
    user_content = "用" * 6153  # measured user message size in tokens
    with pytest.raises(ValueError) as excinfo:
        _assert_prompt_fits(
            system_content=system_content,
            user_content=user_content,
            context_length=8192,
            reserve=900,
        )
    assert "AI_INPUT_BUDGET_EXCEEDED" in str(excinfo.value)
    assert "truncated" in str(excinfo.value)


def test_guard_accepts_the_same_prompt_at_a_verified_larger_window() -> None:
    system_content = "系" * 2556
    user_content = "用" * 6153
    measured = _assert_prompt_fits(
        system_content=system_content,
        user_content=user_content,
        context_length=16384,
        reserve=900,
    )
    assert measured == 8709


def test_guard_counts_a_news_spike_that_a_char_budget_would_miss() -> None:
    """A big news set is still only ~9.7k tokens, yet 36000 chars of CJK is ~30k."""
    system_content = "系" * 2556
    user_content = "新" * 7146
    # 36000 characters is the *old* budget and this payload is under it...
    assert len(user_content) < 36000
    # ...but it still does not fit the old 8192 window.
    with pytest.raises(ValueError):
        _assert_prompt_fits(
            system_content=system_content,
            user_content=user_content,
            context_length=8192,
            reserve=900,
        )
    assert _assert_prompt_fits(
        system_content=system_content,
        user_content=user_content,
        context_length=16384,
        reserve=900,
    ) == 9702


def test_guard_is_inert_when_the_window_is_unknown() -> None:
    """Unknown capacity must block rather than permit silent server truncation."""
    with pytest.raises(ValueError, match="MODEL_CONTEXT_UNKNOWN"):
        _assert_prompt_fits(
            system_content="a" * 20,
            user_content="b" * 20,
            context_length=0,
            reserve=900,
        )


def test_guard_accepts_a_large_prompt_only_when_the_larger_window_is_verified() -> None:
    """A large historical prompt must not be assumed to fit the 8K desktop server.

    This prompt was measured at 26759 tokens (49053 characters). Sizing
    the window below that would keep the guard raising on a legitimate cycle.
    """
    system_content = "系" * 2556
    user_content = "j" * 46301  # the real 49053 chars minus the system part
    measured = _assert_prompt_fits(
        system_content=system_content,
        user_content=user_content,
        context_length=32768,
        reserve=900,
    )
    assert measured + 900 <= 32768
    # The same prompt must be rejected when the verified runtime is only 8K.
    with pytest.raises(ValueError):
        _assert_prompt_fits(
            system_content=system_content,
            user_content=user_content,
            context_length=8192,
            reserve=900,
        )


def _production_sized_prompt_payload() -> dict[str, object]:
    """Build a deterministic three-symbol prompt with production-shaped evidence."""
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    timeframe_candles = {
        "5m": [[100.0 + i, 101.2 + i, 99.4 + i, 100.8 + i, 1234.5 + i] for i in range(4)],
        "15m": [[100.0 + i, 102.0 + i, 98.5 + i, 101.1 + i, 3456.7 + i] for i in range(2)],
        "1h": [[100.0 + i, 104.0 + i, 97.0 + i, 101.8 + i, 9876.5 + i] for i in range(2)],
    }
    technical_context: dict[str, object] = {
        "indicator_columns": ["ema20", "rsi14_simple", "atr14_simple", "volume_ratio20", "support20", "resistance20"],
        "candle_columns": ["open", "high", "low", "close", "volume"],
    }
    for symbol_index, symbol in enumerate(symbols):
        technical_context[symbol] = {
            "status": "READY",
            "timeframes": {
                timeframe: {
                    "status": "READY",
                    "last_closed_at": f"2026-09-21T12:{(symbol_index * 5 + 5) % 60:02d}:00+00:00",
                    "indicators": [
                        61234.12 + symbol_index * 100,
                        54.2 + symbol_index,
                        145.8 + symbol_index * 2,
                        1.18 + symbol_index / 10,
                        60700.0 + symbol_index * 100,
                        61600.0 + symbol_index * 100,
                    ],
                    "candles": candles,
                }
                for timeframe, candles in timeframe_candles.items()
            },
        }

    # The three samples per symbol preserve the same trend information as the
    # live compact radar. Long source/source-status strings model the verbose
    # metadata seen in real feeds without relying on external services.
    radar_series = {
        symbol: [
            [-(3 - index) * 5, 61200.0 + symbol_index * 100 + index * 12.5,
             220.0 + index, 180.0 + index, 40.0, 100.0 + index * 40.0, 84 + index]
            for index in range(3)
        ]
        for symbol_index, symbol in enumerate(symbols)
    }
    oi_series = [
        [venue, symbol, -(3 - index) * 5, 123456.789 + index * 1200,
         7654321.12 + index * 9000, 61200.0 + index * 12.5, "contracts"]
        for symbol in symbols
        for venue in ("gate", "binance")
        for index in range(3)
    ]
    # Optional historical OI samples model a high-volume public snapshot. The
    # decision matrix already carries current per-venue OI/funding changes,
    # so these redundant samples are safe to remove from the model projection.
    oi_series.extend(
        ["gate", symbol, -(6 - index) * 5, 121000.0 + index * 1100,
         7410000.0 + index * 8500, 61000.0 + index * 10, "contracts"]
        for symbol in symbols
        for index in range(3)
    )
    market_radar = {
        "status": "AVAILABLE",
        "cvd": {
            "status": "AVAILABLE", "source": "gate_public_trades_agg", "as_of": "2026-09-21T12:30:00+00:00",
            "unit": "contracts", "synthetic": False,
            "series_columns": "min,price,buy,sell,delta,cvd,trades", "series": radar_series,
        },
        "open_interest": {
            "status": "AVAILABLE", "source": "gate+binance_open_interest", "as_of": "2026-09-21T12:30:00+00:00",
            "synthetic": False, "binance_status": "AVAILABLE", "series_columns": "venue,symbol,min,oi,oi_usdt,price,unit",
            "series": oi_series,
        },
        "derivatives_matrix_columns": "symbol,gate_status,gate_oi_pct,gate_funding_pct,binance_status,binance_oi_pct,binance_funding_pct,crowding_score,label,price_pct",
        "derivatives_matrix": [
            {"symbol": symbol,
             "gate": {"status": "AVAILABLE", "oi_change_pct": 1.28, "funding_rate_pct": 0.0062},
             "binance": {"status": "AVAILABLE", "oi_change_pct": 0.97, "funding_rate_pct": 0.0054},
             "crowding_score": 41 + index, "crowding_label": "BALANCED", "price_change_pct": 0.8}
            for index, symbol in enumerate(symbols)
        ],
        "liquidations": {
            "status": "AVAILABLE", "source": "gate_public_websocket", "as_of": "2026-09-21T12:30:00+00:00",
            "synthetic": False, "window_hours": 24,
            "counts": {"LONG": 1234, "SHORT": 987, "UNKNOWN": 2},
            "estimated_notional": {"LONG": 1234567.89, "SHORT": 987654.32, "UNKNOWN": 100.0},
            "recent": [[symbol, "2026-09-21T12:29:00+00:00", "LONG", 3.2, 61200.0, 195840.0] for symbol in symbols[:2]],
            "recent_columns": "symbol,time,direction,size,price,notional",
        },
        "onchain": {
            "status": "AVAILABLE", "source": "whale_alert_webhook", "as_of": "2026-09-21T12:30:00+00:00",
            "synthetic": False,
            "events": [["Arkham", symbol.removesuffix("USDT"), "2026-09-21T12:21:00+00:00", "EXCHANGE_INFLOW", 12345678.9] for symbol in symbols[:2]],
            "event_columns": "provider,asset,time,direction,amount",
        },
        "cross_market": {
            "status": "AVAILABLE", "source": "macro_feed", "as_of": "2026-09-21T12:30:00+00:00",
            "synthetic": False,
            "items": [["DXY", 104.22, 0.31, "READY"], ["US10Y", 4.23, -0.02, "READY"], ["NQ", 19765.2, 0.42, "READY"], ["BTC.D", 58.2, 0.1, "READY"]],
            "item_columns": "symbol,value,change_pct,status",
        },
    }
    news_revisions = [
        {
            "revision_id": f"news-{symbol}", "scope": "SYMBOL", "symbol": symbol,
            "published_at": "2026-09-21T12:10:00+00:00", "known_at": "2026-09-21T12:11:00+00:00",
            "source": "Verified public newswire", "impact": "NEUTRAL",
            "title": f"{symbol}资金流与市场快讯变化",
            "summary": ("交易所资金流、行业政策和市场参与者行为摘要" * 5)[:32],
        }
        for symbol in symbols
    ] + [{
        "revision_id": "news-MACRO", "scope": "MARKET_WIDE", "symbol": None,
        "symbols": symbols, "published_at": "2026-09-21T12:05:00+00:00", "known_at": "2026-09-21T12:06:00+00:00",
        "source": "Verified macro calendar", "impact": "UNKNOWN",
        "title": "宏观经济与风险事件日历",
        "summary": "相关资产波动，等待事实确认与价格验证。",
    }]
    candidates = [
        {
            "candidate_id": f"c-{index}", "symbol": symbol, "signal_timeframe": "5m",
            "status": "PROPOSAL", "direction_bias": "LONG", "rr": 2.25,
            "trigger_completion_pct": 91 + index,
            "entry_zone": {"low": 61100.0 + index * 100, "high": 61300.0 + index * 100},
            "invalidation": "结构位失效",
            "proposal": {
                "side": "LONG", "entry": 61200.0 + index * 100, "stop": 60700.0 + index * 100,
                "targets": [62300.0 + index * 100, 63500.0 + index * 100],
            },
        }
        for index, symbol in enumerate(symbols)
    ]

    # These fields approximate the remaining account, strategy and market
    # context from a live cycle so the evidence fields above cross the 8K cap.
    payload: dict[str, object] = {
        "allowed_instruments": symbols,
        "active_strategy": {
            "name": "激进趋势共振策略", "revision": 12,
            "execution": {"order_preference": "LIMIT", "risk_per_trade_pct": 0.25,
                          "max_positions": 2, "scan_interval_minutes": 5,
                          "atr_adaptive_sizing": True, "consecutive_loss_lock_enabled": True,
                          "us_open_defense_enabled": True},
        },
        "market_universe": {"status": "READY", "environment": "TESTNET", "selected_symbols": symbols},
        "technical_context": technical_context,
        "market_snapshots": {
            symbol: {"symbol": symbol, "price": 61200.0 + index * 100, "bid": 61199.5 + index * 100,
                     "ask": 61200.5 + index * 100, "baseVolume": 12345.6789,
                     "slippage": 0.0012, "market": {"taker": "0.00075"},
                     "quoteVolume": 765432100.12, "fundingRate": 0.000062,
                     "openInterest": 123456.789, "data_as_of": "2026-09-21T12:30:00+00:00",
                     "received_at": "2026-09-21T12:30:00.100+00:00", "source": "gate_native_rest",
                     "freshness_status": "fresh", "fresh": True, "environment": "TESTNET",
                     "last": 61200.0 + index * 100, "open": 60800.0 + index * 100,
                     "high": 61600.0 + index * 100, "low": 60400.0 + index * 100,
                     "change": 400.0, "percentage": 0.66, "volume": 12345.6789,
                     "market_data_environment": "TESTNET"}
            for index, symbol in enumerate(symbols)
        },
        "market_radar": market_radar,
        "news_coverage_status": "RECENT_REVISIONS_INCLUDED",
        "news_revisions": news_revisions,
        "candidates": candidates,
        "account_truth": {"status": "AVAILABLE", "source": "GATE_TESTNET", "observed_at": "2026-09-21T12:30:00+00:00",
                          "snapshot_id": "account-snapshot-test", "equity": 10000.0, "available_margin": 8000.0,
                          "used_margin": 2000.0, "unrealized_pnl": 42.12,
                          "positions": [], "pending_orders": []},
        "calibration": {"status": "READY", "profile_id": "calibration-test", "sample_size": 640,
                        "expires_at": "2026-09-21T13:00:00+00:00",
                        "profile": {"confidence_bias": 0.01, "entry_style": "LIMIT", "max_concurrent_positions": 2,
                                    "order_preference": "LIMIT", "risk_regime": "NORMAL",
                                    "replay": {"sample_size": 640, "return_observations": 639,
                                               "positive_fraction": 0.53, "mean_return": 0.0003,
                                               "first_bar_end": "2026-08-01T00:00:00+00:00",
                                               "last_bar_end": "2026-09-21T12:00:00+00:00",
                                               "recent_observations": [0.001 * (i - 10) for i in range(24)]},
                                    "diagnostic_bins": [{"range": f"{i * 5}-{i * 5 + 5}", "count": i + 1,
                                                         "realized_return": (i - 5) / 1000} for i in range(18)]}},
        "market_data_environment": "TESTNET",
        "data_quality": {"status": "READY", "source": "gate_native_rest", "environment": "TESTNET"},
        "strategy_readiness": {"status": "READY", "candidate_count": 3},
        "indicator_snapshot_id": "indicators-test-20260921-1230",
        "decision_memory": [{"action": "WAIT", "symbol": "BTCUSDT", "strategy_template_id": "aggressive_5m"}],
        "strategy_experience": {"status": "AVAILABLE", "sample_size": 14, "wins": 8, "losses": 5, "flats": 1,
                                "win_rate_pct": 61.5, "net_realized_pnl_usdt": 47.21,
                                "by_strategy": [{"strategy_template_id": "aggressive_5m", "settled_count": 14,
                                                 "win_rate_pct": 61.5, "net_realized_pnl_usdt": 47.21}]},
        "dynamic_risk": {"status": "BLOCKED", "entry_allowed": False,
                         "blocked_until": "2026-09-21T14:00:00+00:00",
                         "reasons": ["CONSECUTIVE_LOSS_COOLDOWN"], "evidence_status": "AUTHORITATIVE_EXIT_EVIDENCE",
                         "atr_adaptive_sizing": {"enabled": True, "status": "ENFORCED_BY_STOP_DISTANCE_RISK_SIZING"}},
        "generation": 17,
    }
    return payload


def _add_production_named_technical_evidence(payload: dict[str, object]) -> None:
    """Give readiness tests the same named indicators and provenance as production."""
    technical = payload["technical_context"]
    for symbol, instrument in technical.items():
        if symbol in {"candle_columns", "indicator_columns"} or not isinstance(instrument, dict):
            continue
        for timeframe, frame in instrument["timeframes"].items():
            values = frame.get("indicators")
            if isinstance(values, list):
                names = (
                    "ema20", "rsi14_simple", "atr14_simple", "volume_ratio20",
                    "support20", "resistance20",
                )
                frame["indicators"] = dict(zip(names, values))
            frame["nofx_indicator_snapshot"] = {
                "schema_version": "nofx-indicator-snapshot-v1",
                "status": "READY",
                "source": "gate_native_rest",
                "timeframe": timeframe,
                "as_of": frame["last_closed_at"],
                "bar_count": 32,
                "reason": None,
                "diagnostics": {"display_only": "remove from model projection"},
            }


def test_prompt_payload_is_compacted_to_verified_budget_without_losing_decision_evidence() -> None:
    system_content = "系统规则" * 507 + "。" * 3
    payload = _production_sized_prompt_payload()
    payload["decision_memory"] = [
        {"action": "WAIT", "symbol": "BTCUSDT", "status": "WAITING", "reason": "已过期等待锚点" * 12},
        {
            "action": "OPEN_LONG", "symbol": "ETHUSDT", "status": "CLOSED",
            "outcome_status": "WIN", "outcome_pnl": 12.3,
            "summary_zh": "15m 收盘突破前高后按止损风险限额入场，达到首个目标后保护剩余仓位。" * 2,
        },
    ]
    original = copy.deepcopy(payload)
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    raw_user_tokens = _estimate_tokens(serialized)
    assert raw_user_tokens >= 7505
    assert _estimate_tokens(system_content) + raw_user_tokens + 1024 > 8192

    projected, metadata = _fit_prompt_payload(
        payload,
        system_content,
        context_length=8192,
        reserve=1024,
        signal_timeframe="5m",
    )

    projected_json = json.dumps(projected, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert (
        _estimate_tokens(system_content)
        + _estimate_tokens(projected_json)
        + metadata["reserve_tokens"]
        + metadata["safety_margin_tokens"]
        <= 8192
    )
    assert metadata["compacted"] is True
    assert metadata["steps"]
    assert metadata["estimated_input_tokens"] == _estimate_tokens(system_content) + _estimate_tokens(projected_json)
    assert metadata["context_length"] == 8192
    assert metadata["reserve_tokens"] == 1024
    assert metadata["safety_margin_tokens"] == 256

    symbols = original["allowed_instruments"]
    assert projected["allowed_instruments"] == symbols
    assert {item["symbol"] for item in projected["candidates"]} == set(symbols)
    assert {item["symbol"] for item in projected["news_revisions"] if item.get("scope") == "SYMBOL"} == set(symbols)
    assert {item["revision_id"] for item in projected["news_revisions"]} == {
        item["revision_id"] for item in original["news_revisions"]
    }
    for symbol in symbols:
        headline = next(item for item in projected["news_revisions"] if item.get("symbol") == symbol)
        assert headline["title"].startswith(f"{symbol}资金流")
        assert len(headline["summary"]) >= 20
    assert len(projected["decision_memory"]) == 1
    assert projected["decision_memory"][0]["outcome_status"] == "WIN"
    assert projected["decision_memory"][0]["summary_zh"].startswith("15m 收盘突破前高")
    assert len(projected["decision_memory"][0]["summary_zh"]) <= 96
    for symbol in symbols:
        assert projected["technical_context"][symbol]["timeframes"]["5m"]["candles"][-2:] == original["technical_context"][symbol]["timeframes"]["5m"]["candles"][-2:]
        assert projected["market_snapshots"][symbol]["slippage"] == 0.0012
        assert projected["market_snapshots"][symbol]["fee_rate"] == 0.00075
    for key in ("status", "entry_allowed", "blocked_until", "reasons", "evidence_status", "atr_adaptive_sizing"):
        assert projected["dynamic_risk"][key] == original["dynamic_risk"][key]
    assert payload == original, "compaction must not mutate the frozen cycle input"


def test_latency_target_keeps_all_owned_rows_and_account_wide_margin_facts() -> None:
    payload = _production_sized_prompt_payload()
    account = payload["account_truth"]
    account.update({
        "equity": 10000.0,
        "available_margin": 7500.0,
        "used_margin": 2500.0,
        "managed_state": {
            "status": "AVAILABLE", "account_id": "acct", "environment": "TESTNET",
            "venue": "gate", "managed_position_count": 1, "owned_entry_order_count": 1,
            "owned_protection_order_count": 1, "owned_reduction_order_count": 1,
        },
        "managed_positions": [{
            "position_id": "position-btc", "symbol": "BTCUSDT", "side": "LONG",
            "quantity": 0.01, "entry_price": 61000.0, "mark_price": 61200.0,
            "notional": 612.0, "margin": 30.6, "leverage": 20,
            "stop_price": 60500.0, "ownership": "VERIFIED_SYSTEM",
        }],
        "owned_entry_orders": [{
            "order_id": "entry-eth", "symbol": "ETHUSDT", "side": "LONG",
            "type": "LIMIT", "price": 3000.0, "amount": 0.2, "remaining": 0.2,
            "status": "open", "ownership": "VERIFIED_SYSTEM",
        }],
        "owned_protection_orders": [{
            "order_id": "stop-btc", "symbol": "BTCUSDT", "side": "SELL",
            "type": "STOP", "trigger_price": 60500.0, "amount": 0.01,
            "status": "open", "reduce_only": True, "ownership": "VERIFIED_SYSTEM",
        }],
        "owned_reduction_orders": [{
            "order_id": "reduce-sol", "symbol": "SOLUSDT", "side": "SELL",
            "type": "LIMIT", "price": 140.0, "amount": 1.0, "remaining": 1.0,
            "status": "open", "reduce_only": True, "ownership": "VERIFIED_SYSTEM",
        }],
    })

    projected, metadata = _fit_prompt_payload(
        payload,
        "system",
        context_length=INFERENCE_SOFT_CONTEXT_LIMIT,
        reserve=DECISION_OUTPUT_TOKEN_BUDGET,
        signal_timeframe="5m",
    )

    compacted_account = projected["account_truth"]
    assert metadata["estimated_input_tokens"] + DECISION_OUTPUT_TOKEN_BUDGET + metadata["safety_margin_tokens"] <= INFERENCE_SOFT_CONTEXT_LIMIT
    assert projected["allowed_instruments"] == payload["allowed_instruments"]
    assert compacted_account["managed_positions"][0]["position_id"] == "position-btc"
    assert compacted_account["managed_positions"][0]["stop_price"] == 60500.0
    assert compacted_account["owned_entry_orders"][0]["order_id"] == "entry-eth"
    assert compacted_account["owned_entry_orders"][0]["price"] == 3000.0
    assert compacted_account["owned_entry_orders"][0]["remaining"] == 0.2
    assert compacted_account["owned_protection_orders"][0]["order_id"] == "stop-btc"
    assert compacted_account["owned_protection_orders"][0]["trigger_price"] == 60500.0
    assert compacted_account["owned_reduction_orders"][0]["order_id"] == "reduce-sol"
    assert compacted_account["owned_reduction_orders"][0]["remaining"] == 1.0
    for field in ("equity", "available_margin", "used_margin"):
        assert compacted_account[field] == account[field]


def test_empty_trade_history_and_unavailable_radar_do_not_displace_closed_candles() -> None:
    payload = _production_sized_prompt_payload()
    _add_production_named_technical_evidence(payload)
    payload["performance_context"] = {
        "status": "AVAILABLE", "stance": "INSUFFICIENT_SAMPLE", "closed_trades": 0,
        "guidance_zh": "没有已结算交易" * 20, "win_rate_pct": 0,
        "profit_factor": 1, "current_equity_usdt": 10000,
    }
    payload["decision_memory"] = [{"action": "WAIT", "reason": "没有触发" * 20}]
    payload["market_radar"]["cross_market"] = {
        "status": "CONFIG_REQUIRED", "source": "UNKNOWN", "missing_symbols": ["DXY", "NQ"],
    }
    payload["market_radar"]["onchain"] = {"status": "CONFIG_REQUIRED", "source": "UNKNOWN"}
    payload["market_radar"]["derivatives_matrix"] = [["BTCUSDT", "AVAILABLE", 1.2, 0.01]]

    projected, metadata = _fit_prompt_payload(
        payload, "系统规则" * 620, context_length=8192, reserve=1024,
        signal_timeframe="5m",
    )

    assert metadata["estimated_input_tokens"] + metadata["reserve_tokens"] + metadata["safety_margin_tokens"] <= 8192
    assert "keep_named_primary_indicators" in metadata["steps"]
    for symbol in payload["allowed_instruments"]:
        frames = projected["technical_context"][symbol]["timeframes"]
        signal_bars = frames["5m"]["candles"]
        assert signal_bars == payload["technical_context"][symbol]["timeframes"]["5m"]["candles"][-4:]
        for timeframe in ("15m", "1h"):
            source_frame = payload["technical_context"][symbol]["timeframes"][timeframe]
            actual_frame = frames[timeframe]
            assert actual_frame["status"] == "READY"
            assert actual_frame["last_closed_at"] == source_frame["last_closed_at"]
            assert actual_frame["candles"] == source_frame["candles"][-1:]
            assert actual_frame["indicators"] == {
                "ema20": source_frame["indicators"]["ema20"],
                "atr14_simple": source_frame["indicators"]["atr14_simple"],
            }
            snapshot = actual_frame["nofx_indicator_snapshot"]
            assert snapshot["source"] == "gate_native_rest"
            assert snapshot["timeframe"] == timeframe
            assert snapshot["as_of"] == source_frame["last_closed_at"]
            assert "diagnostics" not in snapshot
    assert projected["decision_memory"] == []
    assert projected["performance_context"]["closed_trades"] == 0


def test_prompt_payload_fails_closed_when_required_decision_contract_cannot_fit() -> None:
    payload = _production_sized_prompt_payload()
    # Required risk-gate evidence cannot be silently dropped to fit. This
    # oversized value models a corrupted/unbounded upstream source; a safe
    # helper must reject the prompt rather than trim away a risk gate.
    payload["dynamic_risk"]["evidence_status"] = "AUTHORITATIVE_EXIT_EVIDENCE" + "证据" * 10000
    original = copy.deepcopy(payload)

    with pytest.raises(ValueError, match="AI_INPUT_BUDGET_EXCEEDED"):
        _fit_prompt_payload(
            payload,
            "固定交易规则和JSON决策合同。",
            context_length=8192,
            reserve=1024,
            signal_timeframe="5m",
        )

    assert payload == original


def test_prompt_payload_uses_an_injected_runtime_token_counter() -> None:
    payload = _production_sized_prompt_payload()
    system_content = "系统规则" * 507 + "。" * 3
    exact_counter = lambda text: (len(text.encode("utf-8")) + 5) // 6

    projected, metadata = _fit_prompt_payload(
        payload,
        system_content,
        context_length=8192,
        reserve=1024,
        signal_timeframe="5m",
        token_counter=exact_counter,
    )
    projected_json = json.dumps(projected, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    exact_input_tokens = exact_counter(system_content) + exact_counter(projected_json)

    assert metadata["tokenizer"] == "PROVIDER_TOKENIZER"
    assert metadata["estimated_input_tokens"] == exact_input_tokens
    assert exact_input_tokens + metadata["reserve_tokens"] + metadata["safety_margin_tokens"] <= 8192


def test_extended_scan_defers_lower_ranked_symbols_but_keeps_held_position() -> None:
    payload = _production_sized_prompt_payload()
    _add_production_named_technical_evidence(payload)
    template = copy.deepcopy(payload["technical_context"]["BTCUSDT"])
    snapshot = copy.deepcopy(payload["market_snapshots"]["BTCUSDT"])
    for index in range(5):
        symbol = f"EXTRA{index}USDT"
        payload["allowed_instruments"].append(symbol)
        payload["technical_context"][symbol] = copy.deepcopy(template)
        payload["market_snapshots"][symbol] = copy.deepcopy(snapshot)
    owned_alias = "EXTRA4/USDT:USDT"
    payload["account_truth"]["positions"] = [{
        "symbol": owned_alias, "side": "SHORT", "quantity": 1,
        "ownership": "VERIFIED_SYSTEM", "ownership_role": "MANAGED_SYSTEM_POSITION",
    }]
    payload["account_truth"]["managed_positions"] = [{
        "position_id": "managed-extra4", "symbol": owned_alias, "side": "SHORT",
        "quantity": 1, "ownership": "VERIFIED_SYSTEM",
        "ownership_role": "MANAGED_SYSTEM_POSITION",
    }]
    payload["account_truth"]["pending_orders"] = [{
        "order_id": "entry-extra4", "symbol": owned_alias, "side": "SHORT",
        "amount": 1, "remaining": 1, "status": "open",
        "ownership": "VERIFIED_SYSTEM", "ownership_role": "OWNED_ENTRY_ORDER",
    }]
    payload["account_truth"]["owned_entry_orders"] = [{
        "order_id": "entry-extra4", "symbol": owned_alias, "side": "SHORT",
        "amount": 1, "remaining": 1, "status": "open",
        "ownership": "VERIFIED_SYSTEM", "ownership_role": "OWNED_ENTRY_ORDER",
    }]
    payload["account_truth"]["owned_protection_orders"] = [{
        "order_id": "stop-extra4", "symbol": owned_alias, "side": "BUY",
        "amount": 1, "remaining": 1, "status": "open", "reduce_only": True,
        "ownership": "VERIFIED_SYSTEM", "ownership_role": "OWNED_PROTECTION_ORDER",
    }]
    payload["account_truth"]["owned_reduction_orders"] = [{
        "order_id": "reduce-extra4", "symbol": owned_alias, "side": "BUY",
        "amount": 1, "remaining": 1, "status": "open", "reduce_only": True,
        "ownership": "VERIFIED_SYSTEM", "ownership_role": "OWNED_REDUCTION_ORDER",
    }]
    original = copy.deepcopy(payload)

    projected, metadata = _fit_prompt_payload(
        payload, "系统规则" * 620, context_length=8192, reserve=1024,
        signal_timeframe="5m",
    )

    assert any(step.startswith("defer_symbol_for_model_window:") for step in metadata["steps"])
    assert metadata["selected_symbols"] == original["allowed_instruments"]
    assert metadata["visible_symbols"] == projected["allowed_instruments"]
    assert metadata["deferred_symbols"] == [symbol for symbol in original["allowed_instruments"]
                                            if symbol not in projected["allowed_instruments"]]
    assert metadata["visibility_reason"] == "MODEL_CONTEXT_BUDGET"
    assert "EXTRA4USDT" in projected["allowed_instruments"]
    account = projected["account_truth"]
    assert account["positions"][0]["symbol"] == owned_alias
    assert account["managed_positions"][0]["position_id"] == "managed-extra4"
    assert account["managed_positions"][0]["ownership_role"] == "MANAGED_SYSTEM_POSITION"
    assert account["managed_positions"][0]["quantity"] == 1
    for collection, order_id, role in (
        ("pending_orders", "entry-extra4", "OWNED_ENTRY_ORDER"),
        ("owned_entry_orders", "entry-extra4", "OWNED_ENTRY_ORDER"),
        ("owned_protection_orders", "stop-extra4", "OWNED_PROTECTION_ORDER"),
        ("owned_reduction_orders", "reduce-extra4", "OWNED_REDUCTION_ORDER"),
    ):
        order = account[collection][0]
        assert order["order_id"] == order_id
        assert order["ownership_role"] == role
        assert order["remaining"] == 1
    assert len(projected["allowed_instruments"]) < len(original["allowed_instruments"])
    assert set(projected["market_snapshots"]) == set(projected["allowed_instruments"])
    assert set(projected["technical_context"]) - {"candle_columns", "indicator_columns"} == set(projected["allowed_instruments"])
    for symbol in projected["allowed_instruments"]:
        for timeframe in ("15m", "1h"):
            frame = projected["technical_context"][symbol]["timeframes"][timeframe]
            if frame["status"] == "READY":
                assert frame["candles"]
                assert frame["last_closed_at"]
                assert frame["indicators"]["ema20"] > 0
                assert frame["indicators"]["atr14_simple"] > 0
                assert frame["nofx_indicator_snapshot"]["source"] == "gate_native_rest"
    assert payload == original
    assert projected["news_revisions"][0]["summary"]


@pytest.mark.parametrize(
    "owned_collection",
    (
        "managed_positions", "positions", "pending_orders",
        "owned_entry_orders", "owned_protection_orders", "owned_reduction_orders",
    ),
)
@pytest.mark.parametrize("native_alias", ("EXTRA4/USDT:USDT", "EXTRA4_USDT"))
def test_8k_symbol_deferral_preserves_owned_gate_alias_context(
    owned_collection: str,
    native_alias: str,
) -> None:
    payload = _production_sized_prompt_payload()
    _add_production_named_technical_evidence(payload)
    template = copy.deepcopy(payload["technical_context"]["BTCUSDT"])
    snapshot_template = copy.deepcopy(payload["market_snapshots"]["BTCUSDT"])
    for index in range(5):
        symbol = f"EXTRA{index}USDT"
        payload["allowed_instruments"].append(symbol)
        payload["technical_context"][symbol] = copy.deepcopy(template)
        payload["market_snapshots"][symbol] = copy.deepcopy(snapshot_template)
        payload["market_snapshots"][symbol]["symbol"] = symbol

    ownership_role = (
        "MANAGED_SYSTEM_POSITION"
        if owned_collection in {"managed_positions", "positions"}
        else {
            "pending_orders": "OWNED_ENTRY_ORDER",
            "owned_entry_orders": "OWNED_ENTRY_ORDER",
            "owned_protection_orders": "OWNED_PROTECTION_ORDER",
            "owned_reduction_orders": "OWNED_REDUCTION_ORDER",
        }[owned_collection]
    )
    owned_row = {
        "symbol": native_alias,
        "side": "SHORT",
        "quantity": 1.0,
        "ownership": "VERIFIED_SYSTEM",
        "ownership_role": ownership_role,
    }
    if owned_collection in {"managed_positions", "positions"}:
        owned_row.update(position_id=f"position-{owned_collection}", status="OPEN")
    else:
        owned_row.update(
            order_id=f"order-{owned_collection}", amount=1.0, remaining=1.0,
            status="open", reduce_only=owned_collection != "owned_entry_orders",
        )
    payload["account_truth"][owned_collection] = [owned_row]
    original = copy.deepcopy(payload)

    projected, metadata = _fit_prompt_payload(
        payload,
        "系统规则" * 620,
        context_length=8192,
        reserve=1024,
        signal_timeframe="5m",
    )

    assert any(step.startswith("defer_symbol_for_model_window:") for step in metadata["steps"])
    assert len(projected["allowed_instruments"]) < len(original["allowed_instruments"])
    assert "EXTRA4USDT" in projected["allowed_instruments"]
    assert "EXTRA4USDT" in projected["market_snapshots"]
    assert projected["market_snapshots"]["EXTRA4USDT"]["symbol"] == "EXTRA4USDT"
    assert "EXTRA4USDT" in projected["technical_context"]
    context_1h = projected["technical_context"]["EXTRA4USDT"]["timeframes"]["1h"]
    assert context_1h["status"] == "READY"
    assert context_1h["candles"]
    assert context_1h["indicators"]["ema20"] > 0
    assert context_1h["indicators"]["atr14_simple"] > 0

    preserved = projected["account_truth"][owned_collection][0]
    assert preserved["symbol"] == native_alias
    assert preserved["ownership_role"] == ownership_role
    if owned_collection in {"managed_positions", "positions"}:
        assert preserved["position_id"] == f"position-{owned_collection}"
        assert preserved["quantity"] == 1.0
    else:
        assert preserved["order_id"] == f"order-{owned_collection}"
        assert preserved["amount"] == 1.0
        assert preserved["remaining"] == 1.0
    assert (
        metadata["estimated_input_tokens"]
        + metadata["reserve_tokens"]
        + metadata["safety_margin_tokens"]
        <= 8192
    )
    assert payload == original, "symbol deferral must not mutate source ownership or evidence"


# --------------------------------------------------------------------------
# session window / residency configuration
# --------------------------------------------------------------------------


def test_default_session_window_matches_safe_desktop_runtime() -> None:
    window = _session_context_length()
    assert window == MODEL_CONTEXT_LENGTH == MIN_MODEL_CONTEXT_LENGTH == 8192


def test_session_decision_output_budget_leaves_room_inside_verified_context() -> None:
    """Observed decisions fit a 1024-token cap while keeping the schema unchanged."""
    assert DECISION_OUTPUT_TOKEN_BUDGET == 1024
    assert DECISION_OUTPUT_TOKEN_BUDGET < MODEL_CONTEXT_LENGTH


def test_latency_soft_window_reserves_output_and_safety_margin() -> None:
    assert INFERENCE_SOFT_CONTEXT_LIMIT == 5500
    assert (
        INFERENCE_SOFT_CONTEXT_LIMIT
        - DECISION_OUTPUT_TOKEN_BUDGET
        - PROMPT_BUDGET_SAFETY_MARGIN_TOKENS
        == 4220
    )


def test_session_window_honors_a_smaller_preflight_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "2048")
    assert _session_context_length() == 2048


def test_session_window_honors_a_larger_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "32768")
    assert _session_context_length() == 32768


def test_session_window_ignores_a_malformed_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "plenty")
    assert _session_context_length() == MODEL_CONTEXT_LENGTH


# --------------------------------------------------------------------------
# Bonsai ModelClient route and manifest contract
# --------------------------------------------------------------------------


def test_provider_default_is_pinned_to_modelclient_and_reports_legacy_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.5:9b")
    provider = OllamaProvider()
    assert provider.model_name == DEFAULT_SMART_MODEL
    assert provider.base_url == model_client.base_url
    assert provider._route_error(DEFAULT_SMART_MODEL) is None
    assert set(provider.rejected_legacy_overrides) == {"OLLAMA_BASE_URL", "OLLAMA_MODEL"}


def test_provider_inference_requires_verified_modelclient_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai.ollama import _GEMINI_HEALTH_CACHE
    _GEMINI_HEALTH_CACHE.clear()
    provider = OllamaProvider(base_url=model_client.base_url, model_name=DEFAULT_SMART_MODEL)
    monkeypatch.setattr(model_client, "is_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(
        model_client,
        "list_models",
        lambda **_kwargs: [{"id": DEFAULT_SMART_MODEL}],
    )
    captured: dict[str, object] = {}

    def fake_analysis(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        model_client._response_state.model = DEFAULT_SMART_MODEL
        return {"ok": True} if "Connection test only" in messages[0]["content"] else {"action": "WAIT"}

    monkeypatch.setattr(model_client, "structured_analysis", fake_analysis)
    model_client._response_state.model = None
    answer, _raw, receipt = provider.generate_json(
        [{"role": "user", "content": "hi"}],
        model_name=DEFAULT_SMART_MODEL,
        prompt_version="v",
        input_hash="h",
        temperature=0.0,
        max_tokens=900,
        reasoning_effort="none",
    )
    assert answer == {"action": "WAIT"}
    assert captured["model_name"] == DEFAULT_SMART_MODEL
    assert captured["temperature_override"] == 0.0
    assert captured["max_tokens"] == 900
    assert captured["reasoning_effort"] == "high"
    assert receipt["actual_model_id"] == DEFAULT_SMART_MODEL
    assert receipt["model_identity_source"] == "completion_response"


def test_provider_does_not_fallback_when_manifest_identity_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.ai.contracts import LLMError

    from core.ai.ollama import _GEMINI_HEALTH_CACHE
    _GEMINI_HEALTH_CACHE.clear()
    provider = OllamaProvider(base_url=model_client.base_url, model_name=DEFAULT_SMART_MODEL)
    monkeypatch.setattr(model_client, "is_healthy", lambda **_kwargs: True)
    monkeypatch.setattr(model_client, "list_models", lambda **_kwargs: [{"id": "Other-Bonsai.gguf"}])
    with pytest.raises(LLMError, match="MODEL_MANIFEST_IDENTITY_MISMATCH"):
        provider.generate_json([], model_name=DEFAULT_SMART_MODEL, prompt_version="v", input_hash="h")


def test_8k_prompt_compaction_preserves_selected_price_action_structure_evidence() -> None:
    payload = _production_sized_prompt_payload()
    _add_production_named_technical_evidence(payload)
    for symbol in payload["allowed_instruments"]:
        frames = payload["technical_context"][symbol]["timeframes"]
        for timeframe in ("15m", "1h"):
            frames[timeframe]["price_action"] = {
                "status": "READY",
                "as_of": frames[timeframe]["last_closed_at"],
                "source": "gate_native_rest:last",
                "closed_bar_count": 240,
                "confirmed_swings": [{
                    "side": "HIGH", "price": 61250.0,
                    "pivot_at": "2026-09-21T11:15:00Z",
                    "confirmed_at": "2026-09-21T11:45:00Z",
                }, {
                    "side": "LOW", "price": 60800.0,
                    "pivot_at": "2026-09-21T10:15:00Z",
                    "confirmed_at": "2026-09-21T10:45:00Z",
                }],
                "prior_range": {"lookback_bars": 20, "high": 61300.0, "low": 60700.0,
                                "excludes_latest_close": True},
                "bos": {"side": "LONG", "level": 61250.0,
                        "pivot_confirmed_at": "2026-09-21T11:45:00Z",
                        "confirmed_at": "2026-09-21T12:00:00Z"},
                "sweep_reclaim": None,
                "breakout_retest": {"side": "LONG", "level": 61250.0,
                                     "bos_at": "2026-09-21T12:00:00Z",
                                     "confirmed_at": "2026-09-21T12:15:00Z"},
            }
    original = copy.deepcopy(payload)

    projected, metadata = _fit_prompt_payload(
        payload,
        "系统规则" * 620,
        context_length=8192,
        reserve=1024,
        signal_timeframe="15m",
    )

    assert metadata["compacted"] is True
    assert metadata["estimated_input_tokens"] + metadata["reserve_tokens"] + metadata["safety_margin_tokens"] <= 8192
    for symbol in projected["allowed_instruments"]:
        for timeframe in ("15m", "1h"):
            frame = projected["technical_context"][symbol]["timeframes"][timeframe]
            assert frame["status"] == "READY"
            assert frame["candles"]
            from tests.prompt_evidence_helpers import decode_price_action
            assert decode_price_action(projected["technical_context"], frame["price_action"]) == original["technical_context"][symbol]["timeframes"][timeframe]["price_action"]
    assert payload == original, "prompt compaction must not mutate the frozen source evidence"
