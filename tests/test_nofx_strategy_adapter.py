from copy import deepcopy
import json

import pytest

from core.trading.ai_strategy_book import AIStrategyBook, TEMPLATES
from core.trading.autonomous_strategy import (
    STRATEGY_SECTION_CHAR_LIMITS,
    build_strategy_system_prompt,
)
from core.trading.nofx_strategy_adapter import adapt_nofx_strategy_config
from core.storage import SQLiteStore


def _current_strategy(template_id: str = "conservative_pullback") -> dict:
    template = next(item for item in TEMPLATES if item["id"] == template_id)
    return {
        "name": template["name"],
        "template_id": template["id"],
        "style": template["style"],
        "profile": deepcopy(template["profile"]),
        "sections": deepcopy(template["sections"]),
        "execution": deepcopy(template["execution_defaults"]),
        "revision": 0,
    }


def test_nofx_nested_export_maps_prompt_sections_and_primary_timeframe_only():
    current = _current_strategy()
    original_execution = deepcopy(current["execution"])
    adapted = adapt_nofx_strategy_config(
        {
            "strategy_type": "ai_trading",
            "ai_config": {
                "prompt_sections": {
                    "role_definition": "Trade price action with the selected strategy.",
                    "trading_frequency": "Assess each closed 5m candle.",
                    "entry_standards": "Look for a confirmed breakout and retest with sufficient liquidity.",
                    "decision_process": "Review positions first, compare candidates, then choose one setup.",
                },
                "custom_prompt": "Prefer passive limit entries.",
                "indicators": {"klines": {"primary_timeframe": "5m"}},
                "risk_control": {"btc_eth_max_leverage": 125},
                "coin_source": {"static_coins": ["BTCUSDT"]},
            },
        },
        current_strategy=current,
    )

    assert adapted["template_id"] == "aggressive_impulse"
    assert adapted["scan_interval_minutes"] == 5
    assert adapted["execution"] == original_execution
    assert adapted["sections"]["role"] == "Trade price action with the selected strategy."
    assert adapted["sections"]["frequency"] == "Assess each closed 5m candle."
    assert adapted["sections"]["custom_prompt"] == "Prefer passive limit entries."
    assert set(adapted["imported_fields"]) == {
        "role", "frequency", "entry_standards", "decision_process", "custom_prompt",
        "signal_timeframe", "context_timeframes", "indicator_switches", "gate_candidate_universe",
    }
    assert len(adapted["ignored_fields"]) == 4
    assert adapted["nofx_runtime"]["candidate_sources"] == ["gate_active_usdt_perpetuals"]


def test_nofx_runtime_maps_supported_contexts_indicators_and_exclusions_without_credentials():
    current = _current_strategy()
    adapted = adapt_nofx_strategy_config(
        {
            "ai_config": {
                "prompt_sections": {"entry_standards": "Use the imported indicators as evidence."},
                "indicators": {
                    "klines": {
                        "primary_timeframe": "5m",
                        "enable_multi_timeframe": True,
                        "selected_timeframes": ["5m", "15m", "1h", "4h"],
                    },
                    "enable_ema": True,
                    "ema_periods": [20, 300, 50],
                    "enable_macd": True,
                    "enable_rsi": True,
                    "rsi_periods": [14, 101],
                    "enable_oi": True,
                    "enable_funding_rate": True,
                    "external_data_sources": [{"url": "https://private.invalid", "api_key": "NEVER_IMPORT"}],
                },
                "coin_source": {
                    "excluded_coins": ["BTC_USDT", "BAD?", "ETH/USDT:USDT"],
                    "use_oi_top": True,
                },
            }
        },
        current_strategy=current,
    )

    runtime = adapted["nofx_runtime"]
    assert runtime["signal_timeframe"] == "5m"
    assert runtime["context_timeframes"] == ["15m", "1h"]
    assert runtime["indicators"]["ema"] == {"enabled": True, "periods": [20, 50]}
    assert runtime["indicators"]["rsi"] == {"enabled": True, "periods": [14]}
    assert runtime["indicators"]["open_interest"] == {"enabled": True, "status": "CONFIGURED"}
    assert runtime["indicators"]["funding_rate"] == {"enabled": True, "status": "CONFIGURED"}
    assert runtime["candidate_sources"] == ["gate_active_usdt_perpetuals"]
    assert runtime["excluded_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert any(item.startswith("timeframe:4h") for item in runtime["unsupported_sources"])
    assert any(item.startswith("coin_source:OI_TOP") for item in runtime["unsupported_sources"])
    assert any(item.startswith("indicators.external_data_sources") for item in runtime["unsupported_sources"])
    assert "NEVER_IMPORT" not in json.dumps(adapted, ensure_ascii=False)


def test_nofx_legacy_flat_export_preserves_current_cadence_and_execution():
    current = _current_strategy("aggressive_breakout")
    adapted = adapt_nofx_strategy_config(
        {
            "prompt_sections": {"entry_standards": "Only use closed candles and explicit evidence."},
            "custom_prompt": "Do not use fixed leverage.",
        },
        current_strategy=current,
    )

    assert adapted["template_id"] == "aggressive_breakout"
    assert adapted["scan_interval_minutes"] == 15
    assert adapted["execution"] == current["execution"]
    assert adapted["sections"]["entry_standards"] == "Only use closed candles and explicit evidence."


def test_nofx_full_strategy_export_with_string_config_is_supported():
    current = _current_strategy()
    exported_config = {
        "strategy_type": "ai_trading",
        "ai_config": {
            "prompt_sections": {
                "entry_standards": "Wait for a closed-bar retest before entry.",
            },
            "indicators": {"klines": {"primary_timeframe": "15m"}},
        },
    }
    adapted = adapt_nofx_strategy_config(
        {"id": "strategy-id", "name": "NOFX 导出的趋势策略", "config": json.dumps(exported_config)},
        current_strategy=current,
    )

    assert adapted["name"] == "NOFX 导出的趋势策略"
    assert adapted["sections"]["entry_standards"] == "Wait for a closed-bar retest before entry."
    assert adapted["template_id"] == current["template_id"]


@pytest.mark.parametrize(
    ("configuration", "error"),
    [
        ({"strategy_type": "grid_trading", "grid_config": {}}, "NOFX_GRID_STRATEGY_UNSUPPORTED"),
        ({"ai_config": {"prompt_sections": {"entry_standards": "Use closed bars."}, "indicators": {"klines": {"primary_timeframe": "1h"}}}}, "NOFX_TIMEFRAME_UNSUPPORTED"),
        ({"ai_config": {"risk_control": {"max_positions": 2}}}, "NOFX_STRATEGY_PROMPT_EMPTY"),
    ],
)
def test_nofx_adapter_fails_closed_for_unsupported_config(configuration, error):
    with pytest.raises(ValueError, match=error):
        adapt_nofx_strategy_config(configuration, current_strategy=_current_strategy())


def test_long_strategy_text_is_retained_within_bounded_prompt_budget():
    current = _current_strategy("aggressive_breakout")
    source_text = "价格突破后回测并观察成交量变化。" * 200
    adapted = adapt_nofx_strategy_config(
        {"ai_config": {"prompt_sections": {"entry_standards": source_text}}},
        current_strategy=current,
    )
    prompt = build_strategy_system_prompt(
        {
            "name": adapted["name"],
            "template_id": adapted["template_id"],
            "style": "AGGRESSIVE",
            "profile": {"signal_timeframe": "15m"},
            "sections": adapted["sections"],
        }
    )

    expected = source_text[:STRATEGY_SECTION_CHAR_LIMITS["entry_standards"]]
    assert len(adapted["sections"]["entry_standards"]) == STRATEGY_SECTION_CHAR_LIMITS["entry_standards"]
    assert adapted["truncated_fields"] == ["entry_standards"]
    assert f"入场标准：{expected}" in prompt
    assert len(prompt) < 8000


def test_nofx_adapter_reports_each_truncated_prompt_section():
    current = _current_strategy("aggressive_breakout")
    adapted = adapt_nofx_strategy_config(
        {
            "ai_config": {
                "prompt_sections": {
                    "role_definition": "角色" * (STRATEGY_SECTION_CHAR_LIMITS["role"] + 1),
                    "entry_standards": "入场" * (STRATEGY_SECTION_CHAR_LIMITS["entry_standards"] + 1),
                },
                "custom_prompt": "补充" * (STRATEGY_SECTION_CHAR_LIMITS["custom_prompt"] + 1),
            }
        },
        current_strategy=current,
    )

    assert adapted["truncated_fields"] == ["role", "entry_standards", "custom_prompt"]
    for field in adapted["truncated_fields"]:
        assert len(adapted["sections"][field]) == STRATEGY_SECTION_CHAR_LIMITS[field]


def test_import_payload_keeps_account_risk_settings_when_saved(tmp_path):
    store = SQLiteStore(tmp_path / "nofx-import.sqlite3")
    store.initialize()
    book = AIStrategyBook(store)
    current = book.active("gate_testnet")
    current["execution"]["leverage"] = 17
    current["execution"]["max_notional_usdt"] = 777
    current["execution"]["fixed_notional_usdt"] = 500

    adapted = adapt_nofx_strategy_config(
        {
            "ai_config": {
                "prompt_sections": {"entry_standards": "Select a setup from closed bars."},
                "risk_control": {"btc_eth_max_leverage": 100},
                "indicators": {"klines": {"primary_timeframe": "15m"}},
            }
        },
        current_strategy=current,
    )
    saved = book.save(
        account_id="gate_testnet",
        name=adapted["name"],
        sections=adapted["sections"],
        expected_revision=0,
        execution=adapted["execution"],
        template_id=adapted["template_id"],
    )

    assert saved["execution"]["leverage"] == 17
    assert saved["execution"]["max_notional_usdt"] == 777
    assert saved["execution"]["scan_interval_minutes"] == 15
