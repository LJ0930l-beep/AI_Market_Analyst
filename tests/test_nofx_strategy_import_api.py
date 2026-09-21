from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.v2 import router_for
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import AIStrategyBook


def _client(tmp_path):
    store = SQLiteStore(tmp_path / "nofx-import-api.sqlite3")
    store.initialize()
    app = FastAPI()
    app.include_router(router_for(lambda: store, lambda: None, lambda: None))
    client = TestClient(app, headers={"Host": "localhost:8000", "Origin": "http://localhost:5173"})
    return store, client


def test_import_nofx_strategy_saves_prompt_and_cadence_without_importing_executor_config(tmp_path):
    store, client = _client(tmp_path)
    initial = AIStrategyBook(store).active("gate_testnet")
    execution = dict(initial["execution"])
    execution["leverage"] = 21
    execution["max_notional_usdt"] = 620
    execution["fixed_notional_usdt"] = 500
    AIStrategyBook(store).save(
        "gate_testnet",
        name=initial["name"],
        sections=initial["sections"],
        expected_revision=0,
        execution=execution,
        template_id=initial["template_id"],
    )

    response = client.post(
        "/v2/ai-strategy/import-nofx?account_id=gate_testnet",
        json={
            "expected_revision": 1,
            "configuration": {
                "strategy_type": "ai_trading",
                "ai_config": {
                    "prompt_sections": {
                        "role_definition": "Trade both directions from closed market evidence.",
                        "trading_frequency": "Assess on each closed 5m signal candle.",
                        "entry_standards": "Compare trend, liquidity and news before choosing one setup.",
                        "decision_process": "Review open positions, compare the allowed market candidates, then act.",
                    },
                    "custom_prompt": "Prefer passive limit orders.",
                    "indicators": {"klines": {"primary_timeframe": "5m"}},
                    "risk_control": {"btc_eth_max_leverage": 100},
                    "coin_source": {"static_coins": ["BTCUSDT"]},
                    "indicators_secret_fixture": "NOFX_PRIVATE_CREDENTIAL_DO_NOT_ECHO",
                },
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active"]["revision"] == 2
    assert payload["active"]["template_id"] == "aggressive_impulse"
    assert payload["active"]["execution"]["scan_interval_minutes"] == 5
    assert payload["active"]["nofx_runtime"]["signal_timeframe"] == "5m"
    assert payload["active"]["nofx_runtime"]["candidate_sources"] == ["gate_active_usdt_perpetuals"]
    assert payload["active"]["profile"]["signal_timeframe"] == "5m"
    assert payload["active"]["execution"]["leverage"] == 21
    assert payload["active"]["execution"]["max_notional_usdt"] == 620
    assert payload["active"]["sections"]["entry_standards"].startswith("Compare trend")
    assert payload["truncated_fields"] == []
    assert "risk_control" not in payload["imported_fields"]
    assert any("杠杆" in item for item in payload["ignored_fields"])
    assert "NOFX_PRIVATE_CREDENTIAL_DO_NOT_ECHO" not in response.text


def test_import_nofx_strategy_reports_prompt_fields_truncated_to_bonsai_budget(tmp_path):
    _, client = _client(tmp_path)
    response = client.post(
        "/v2/ai-strategy/import-nofx?account_id=gate_testnet",
        json={
            "expected_revision": 0,
            "configuration": {
                "ai_config": {
                    "prompt_sections": {
                        "entry_standards": "入场条件" * 500,
                    }
                }
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["truncated_fields"] == ["entry_standards"]
    assert len(payload["active"]["sections"]["entry_standards"]) == 1500


def test_import_nofx_strategy_rejects_stale_revision_and_does_not_overwrite(tmp_path):
    store, client = _client(tmp_path)
    original = AIStrategyBook(store).active("gate_testnet")
    response = client.post(
        "/v2/ai-strategy/import-nofx?account_id=gate_testnet",
        json={
            "expected_revision": 4,
            "configuration": {"ai_config": {"prompt_sections": {"entry_standards": "Closed bars only."}}},
        },
    )

    assert response.status_code == 400
    assert "STRATEGY_REVISION_CONFLICT" in response.text
    assert AIStrategyBook(store).active("gate_testnet")["revision"] == original["revision"]


def test_import_nofx_strategy_rejects_large_payload_before_mapping(tmp_path):
    _, client = _client(tmp_path)
    response = client.post(
        "/v2/ai-strategy/import-nofx?account_id=gate_testnet",
        json={
            "expected_revision": 0,
            "configuration": {"ai_config": {"custom_prompt": "x" * 200_001}},
        },
    )

    assert response.status_code == 413
    assert "NOFX_CONFIG_TOO_LARGE" in response.text


def test_import_nofx_strategy_rejects_unsupported_grid_config(tmp_path):
    store, client = _client(tmp_path)
    response = client.post(
        "/v2/ai-strategy/import-nofx?account_id=gate_testnet",
        json={"expected_revision": 0, "configuration": {"strategy_type": "grid_trading"}},
    )

    assert response.status_code == 400
    assert "NOFX_GRID_STRATEGY_UNSUPPORTED" in response.text
    assert AIStrategyBook(store).active("gate_testnet")["revision"] == 0
