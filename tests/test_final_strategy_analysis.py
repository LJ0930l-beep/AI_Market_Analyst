"""Production strategy decision -> gateway -> ledger -> analysis, offline PAPER only."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.agent_execution import AgentDecisionService
from core.ai.ollama import OllamaProvider, model_client
from core.analysis.ai_trade_analytics import analyze_ai_trading_ledger
from core.model_routing import DEFAULT_MODEL
from core.storage import SQLiteStore
from core.trading.ledger import AccountLedger


def test_strategy_open_appears_once_in_analysis_without_replaying_orders(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "strategy.db")
    store.initialize()
    ledger = AccountLedger(store)
    ledger.create_account("strategy_account", mode="PAPER", initial_deposit=Decimal("10000"), config={"venue":"simulated"})
    # Explicit fixture-only simulation setting; never applied to user accounts.
    store.upsert_app_setting("simulation.allow_unknown_macro", True)
    answer = {"decision":"EXECUTE_TRADE", "summary":"本地测试满足规则", "counterevidence":[]}
    artifact = r"D:\models\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    model = OllamaProvider(base_url=model_client.base_url, model_name=DEFAULT_MODEL)
    monkeypatch.setattr(model, "health", lambda **_kwargs: {
        "available": True,
        "model_available": True,
        "model_id": DEFAULT_MODEL,
        "actual_model_id": artifact,
        "model_identity_source": "verified_manifest",
    })
    monkeypatch.setattr(model_client, "structured_analysis", lambda *_args, **_kwargs: answer)
    model_client._response_state.model = None
    now = datetime.now(timezone.utc)
    proposal = {"account_id":"strategy_account", "symbol":"BTCUSDT", "strategy_id":"ema_trend",
        "strategy_version":"2.0.0", "side":"LONG", "entry":100, "stop":95,
        "targets":[110,115], "target_fractions":[0.5,0.5], "generated_at":now.isoformat(),
        "expires_at":(now+timedelta(minutes=15)).isoformat(),
        "source_bar_at":(now-timedelta(minutes=15)).isoformat(), "rationale":"本地规则测试"}
    market = {"active":True,"linear":True,"settle":"USDT","contractSize":0.01,
        "precision":{"amount":1,"price":0.1},"limits":{"amount":{"min":1,"max":1000000}}}
    facts = {"freshness":"fresh","source":"isolated_fixture","as_of":now.isoformat(),"price":100}
    service = AgentDecisionService(store, model)
    result = service.decide(proposal, market, facts, now=now)
    assert result["status"] == "SIMULATED", result.get("reason")
    for _ in range(2):
        output = analyze_ai_trading_ledger(store, "strategy_account")
        entries = [r for r in output["execution_records"] if r["economic_role"] == "ENTRY"]
        assert len(entries) == 1
        assert entries[0]["decision_path"] == "STRATEGY_DRIVEN"
        assert entries[0]["intent_id"] == result["order_intent_id"]
        assert output["ai_led_performance"]["position_count"] == 0
    assert service.decide(proposal, market, facts, now=now)["status"] == "DUPLICATE"
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM trade_fills").fetchone()[0] == 1
    ledger.close()
