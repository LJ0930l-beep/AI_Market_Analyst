from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import TEMPLATES, AIStrategyBook
from core.trading.autonomous_strategy import build_strategy_system_prompt


def _client(tmp_path):
    store = SQLiteStore(tmp_path / "strategy-preview.sqlite3")
    store.initialize()
    app = FastAPI()
    def forbidden_runtime_access():
        pytest.fail("strategy preview must not resolve the trading runtime")

    app.include_router(router_for(lambda: store, forbidden_runtime_access, lambda: None))
    client = TestClient(app, headers={"Host": "localhost:8000", "Origin": "http://localhost:5173"})
    return store, client


def test_strategy_preview_reuses_production_strategy_prompt_without_calling_model_or_execution(tmp_path):
    store, client = _client(tmp_path)
    template = next(item for item in TEMPLATES if item["id"] == "aggressive_impulse")
    response = client.post(
        "/v2/ai-strategy/preview?account_id=gate_testnet",
        json={
            "name": template["name"],
            "template_id": template["id"],
            "sections": deepcopy(template["sections"]),
            "execution": deepcopy(template["execution_defaults"]),
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["preview_kind"] == "STRATEGY_SYSTEM_INSTRUCTION_ONLY"
    assert preview["valid"] is True
    assert preview["model_called"] is False
    assert preview["execution_called"] is False
    assert preview["summary"]["signal_timeframe"] == "5m"
    assert preview["summary"]["scan_interval_minutes"] == 5
    assert preview["strategy_system_instruction"] == build_strategy_system_prompt(preview["effective_strategy"])
    assert any("不是本轮运行时完整模型请求" in item for item in preview["limitations"])
    assert AIStrategyBook(store).active("gate_testnet")["revision"] == 0


def test_strategy_preview_reports_static_validation_errors_without_persisting(tmp_path):
    store, client = _client(tmp_path)
    response = client.post(
        "/v2/ai-strategy/preview?account_id=gate_testnet",
        json={
            "template_id": "not-a-template",
            "execution": {"risk_per_trade_pct": 99},
        },
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["valid"] is False
    assert "STRATEGY_TEMPLATE_INVALID" in preview["validation_errors"]
    assert "STRATEGY_EXECUTION_INVALID_RISK_PER_TRADE_PCT" in preview["validation_errors"]
    assert preview["model_called"] is False
    assert preview["execution_called"] is False
    assert AIStrategyBook(store).active("gate_testnet")["revision"] == 0


def test_strategy_preview_rejects_market_input_fields(tmp_path):
    _, client = _client(tmp_path)
    response = client.post(
        "/v2/ai-strategy/preview?account_id=gate_testnet",
        json={"market_snapshots": {"BTCUSDT": {"price": 1}}},
    )
    assert response.status_code == 422
