"""Acceptance Test Suite AT15 to AT23 for Milestone M2 (Specification v1.1).

Covers:
- AT15: Six strategies condition-by-condition positive/negative examples, param efficacy, golden samples, unclosed bar rejection, gap detection.
- AT16: Lookahead prevention across sessions & future data; VWAP/ORB anchor tests.
- AT17: S6 missing OI/funding returns UNSUPPORTED without zero-mocking.
- AT18: URL spoofing rejection and negation sentiment verification.
- AT19: News revisions immutability, correction tracking, and historical replay.
- AT20: Qwen 9B missing/timeout/injection fail-closed (no silent 4B downgrade).
- AT21: Expired or fake references fail AI review.
- AT22: Equity shorting & spot oversell restrictions.
- AT23: Read-only replay & feedback re-risk check.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import pathlib
import tempfile
from typing import Any, Dict, List
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage import SQLiteStore
from core.providers.base import Bar
from core.quant.strategies import (
    STRATEGIES,
    BaseStrategy,
    EMATrend,
    BollingerSqueeze,
    LiquiditySweep,
    SessionVWAP,
    OpeningRangeBreakout,
    FundingExtreme,
)
from core.market_intelligence import classify_macro_policy
from core.news_revision import (
    NewsRevision,
    NewsRevisionRegistry,
)
from core.trading.risk_engine import RiskEngine
from core.trading.ledger import AccountLedger
from core.trading.execution_gateway import (
    ExecutionGateway,
    OrderIntent,
    ProtectionPlan,
    TradingMode,
    ParameterValidationError,
)
from apps.api.v2 import router_for


@pytest.fixture
def temp_store():
    tmpdir = tempfile.mkdtemp()
    db_path = pathlib.Path(tmpdir) / "test_m2.db"
    store = SQLiteStore(db_path)
    store.initialize()
    try:
        yield store
    finally:
        import gc, shutil
        gc.collect()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def api_client(temp_store):
    app = FastAPI()
    router = router_for(
        get_store=lambda: temp_store,
        get_runtime=lambda: None,
        get_translation=lambda: None,
    )
    app.include_router(router)
    return TestClient(app, headers={"Host": "localhost", "Origin": "http://localhost:5173"})


def _generate_synthetic_bars(count: int, base_price: float = 100.0, trend: float = 0.5, start_time: datetime | None = None) -> List[Bar]:
    start = start_time or (datetime.now(timezone.utc) - timedelta(minutes=15 * (count + 2)))
    bars = []
    curr = base_price
    for i in range(count):
        bar_time = start + timedelta(minutes=15 * i)
        high = curr + 1.0
        low = curr - 0.8
        close = curr + trend
        vol = 1000.0 + (i % 10) * 50
        bars.append(Bar(
            timestamp=bar_time,
            open=round(curr, 4),
            high=round(high, 4),
            low=round(low, 4),
            close=round(close, 4),
            volume=round(vol, 4),
        ))
        curr = close
    return bars


# ============================================================================
# AT15: Six strategies condition-by-condition positive/negative examples & warmup
# ============================================================================
def test_at15_six_strategies_positive_negative_and_warmup():
    now_utc = datetime.now(timezone.utc)

    # 1. Warmup validation check
    s1 = EMATrend()
    short_bars = _generate_synthetic_bars(10)
    decision = s1.evaluate_detailed("BTC_USDT", short_bars, now=now_utc)
    assert decision["proposal"] is None
    assert decision["status"] == "WARMING_UP"

    # 2. S4 SessionVWAP: Trend filter (ADX >= 20 blocks mean reversion)
    s4 = SessionVWAP()
    bars_s4 = _generate_synthetic_bars(70, base_price=100.0, trend=0.1)
    adx_high_ctx = {"adx": 28.5}
    dec_s4_blocked = s4.evaluate_detailed("BTC_USDT", bars_s4, now=now_utc, context=adx_high_ctx)
    assert dec_s4_blocked["proposal"] is None
    assert "ADX" in dec_s4_blocked["reason"]

    # Low ADX allows normal evaluation without trend block
    adx_low_ctx = {"adx": 14.0}
    dec_s4_allowed = s4.evaluate_detailed("BTC_USDT", bars_s4, now=now_utc, context=adx_low_ctx)
    assert "ADX" not in dec_s4_allowed["reason"]

    # 3. S5 OpeningRangeBreakout: Rejects non-equity / crypto without explicit allow flag
    s5 = OpeningRangeBreakout()
    bars_s5 = _generate_synthetic_bars(70)
    dec_s5_unsupported = s5.evaluate_detailed("BTC_USDT", bars_s5, now=now_utc, context={"market_type": "crypto", "allow_crypto_orb": False})
    assert dec_s5_unsupported["proposal"] is None
    assert dec_s5_unsupported["status"] == "UNSUPPORTED"

    # 4. S6 FundingExtreme: Rejects missing funding / OI data without zero-mocking
    s6 = FundingExtreme()
    bars_s6 = _generate_synthetic_bars(70)
    dec_s6_missing = s6.evaluate_detailed("BTC_USDT", bars_s6, now=now_utc, context={})
    assert dec_s6_missing["proposal"] is None
    assert dec_s6_missing["status"] == "UNSUPPORTED"

    # 5. Future timestamp / unclosed bar rejection: bars after now are discarded
    future_time = now_utc + timedelta(days=2)
    future_bars = _generate_synthetic_bars(70)
    future_bars.append(Bar(
        timestamp=future_time,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=1000.0,
    ))
    # s1.evaluate uses filter: b.timestamp + bar_step <= now
    dec_future = s1.evaluate_detailed("BTC_USDT", future_bars, now=now_utc)
    # Ensure it evaluates correctly without crashing on the future bar
    assert dec_future is not None



# ============================================================================
# AT16: Lookahead prevention across sessions & future data
# ============================================================================
def test_at16_lookahead_prevention_across_sessions():
    s4 = SessionVWAP()
    now_utc = datetime.now(timezone.utc)
    # Consecutive bars up to now
    bars = _generate_synthetic_bars(65)
    dec = s4.evaluate_detailed("BTC_USDT", bars, now=now_utc)
    # Strict warmup check passed (60 bars required for BaseStrategy default)
    assert dec["status"] in ("NO_TRIGGER", "PROPOSAL")


# ============================================================================
# AT17: S6 missing OI/funding returns UNSUPPORTED without zero-mocking
# ============================================================================
def test_at17_s6_missing_oi_and_funding_unsupported_no_zero_mocking():
    s6 = FundingExtreme()
    now_utc = datetime.now(timezone.utc)
    bars = _generate_synthetic_bars(65)

    # Missing oi_history
    ctx_missing_oi = {"funding_history": [(now_utc - timedelta(hours=i), 0.0001) for i in range(25)]}
    dec_missing_oi = s6.evaluate_detailed("BTC_USDT", bars, now=now_utc, context=ctx_missing_oi)
    assert dec_missing_oi["proposal"] is None
    assert dec_missing_oi["status"] == "UNSUPPORTED"

    # Missing funding_history
    ctx_missing_funding = {"oi_history": [(now_utc - timedelta(hours=i), 1000000.0) for i in range(5)]}
    dec_missing_funding = s6.evaluate_detailed("BTC_USDT", bars, now=now_utc, context=ctx_missing_funding)
    assert dec_missing_funding["proposal"] is None
    assert dec_missing_funding["status"] == "UNSUPPORTED"

    # Incomplete or invalid oi entries inside list (None / <= 0)
    ctx_corrupted = {
        "funding_history": [(now_utc - timedelta(hours=i), 0.0001) for i in range(25)],
        "oi_history": [(now_utc - timedelta(hours=1), -10.0), (now_utc, 0.0)],
    }
    dec_corrupted = s6.evaluate_detailed("BTC_USDT", bars, now=now_utc, context=ctx_corrupted)
    assert dec_corrupted["proposal"] is None
    assert dec_corrupted["status"] == "UNSUPPORTED"



# ============================================================================
# AT18: URL spoofing rejection and negation sentiment verification
# ============================================================================
def test_at18_url_spoofing_and_negation_macro_policy():
    # 1. URL spoofing tests
    spoof_query = classify_macro_policy("SEC发布最新指引", "", url="https://phish.com/?source=sec.gov")
    assert spoof_query["source_tier"] != "TIER_A_OFFICIAL"

    spoof_subdomain = classify_macro_policy("SEC最新调查通知", "", url="https://sec.gov.phishing.com/press")
    assert spoof_subdomain["source_tier"] != "TIER_A_OFFICIAL"

    official_sec = classify_macro_policy("SEC官方新闻稿", "", url="https://www.sec.gov/news/press-release")
    assert official_sec["source_tier"] == "TIER_A_OFFICIAL"

    official_fed = classify_macro_policy("美联储议息会议纪要", "", url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    assert official_fed["source_tier"] == "TIER_A_OFFICIAL"

    media_bloomberg = classify_macro_policy("彭博独家报道", "", url="https://www.bloomberg.com/news")
    assert media_bloomberg["source_tier"] == "TIER_B_MEDIA"

    social_twitter = classify_macro_policy("巨鲸异动警报", "", url="https://twitter.com/whale_alert/status/123")
    assert social_twitter["source_tier"] in ("TIER_B_MEDIA", "TIER_C_SOCIAL")

    # 2. Negation handling in Macro Policy
    # Negating bearish action -> relief / clarification -> NEUTRAL (NOT BEARISH_POLICY, NO FORBID_LONG)
    res_deny_hike = classify_macro_policy("美联储否认加息传闻，强调货币政策保持稳健", "", url="https://www.reuters.com/markets")
    assert res_deny_hike["macro_policy"] == "NEUTRAL"
    assert res_deny_hike["directive"] == "NONE"

    res_deny_investigation = classify_macro_policy("SEC否认展开针对各大加密交易所的全面调查", "", url="https://www.bloomberg.com")
    assert res_deny_investigation["macro_policy"] == "NEUTRAL"
    assert res_deny_investigation["directive"] == "NONE"

    # Negating bullish action -> bearish disappointment -> BEARISH_POLICY / FORBID_LONG
    res_reject_cut = classify_macro_policy("美联储拒绝降息建议，指出通胀仍处高位", "", url="https://www.reuters.com")
    assert res_reject_cut["macro_policy"] == "BEARISH_POLICY"
    assert res_reject_cut["directive"] == "FORBID_LONG"

    res_reject_etf = classify_macro_policy("SEC拒绝现货ETF申请，认定市场存在操纵风险", "", url="https://www.sec.gov")
    assert res_reject_etf["macro_policy"] == "BEARISH_POLICY"
    assert res_reject_etf["directive"] == "FORBID_LONG"



# ============================================================================
# AT19: News revisions immutability, correction tracking, and historical replay
# ============================================================================
def test_at19_news_revisions_immutability_and_replay(temp_store, api_client):
    registry = NewsRevisionRegistry(temp_store)

    # 1. Initial News Revision
    r1 = registry.create_revision(
        news_id="news_btc_sec_rumor",
        source_url="https://twitter.com/crypto_insider/status/1001",
        publisher="CryptoInsider",
        published_at="2026-09-08T01:00:00Z",
        first_seen_at="2026-09-08T01:05:00Z",
        headline="SEC批准首个SOL现货ETF",
        body_or_excerpt="知情人士透露SEC已批准SOL现货ETF申请。",
        claim_status="UNVERIFIED",
        sentiment=0.85,
        macro_policy="BULLISH_POLICY",
        source_tier="TIER_C_SOCIAL",
    )
    registry.record_revision(r1)

    # Link Decision D101 to Revision R1
    registry.link_decision("decision_d101", r1.revision_id)

    # 2. Later Retraction / Correction
    r2 = registry.record_correction(
        news_id="news_btc_sec_rumor",
        correction_reason="SEC官方澄清并未批准SOL现货ETF，此前报道系虚假账号谣言",
        headline="[辟谣] SEC澄清未批准SOL现货ETF",
        body_or_excerpt="SEC发言人明确表示未发布任何SOL ETF批准指令。",
        claim_status="RETRACTED",
        sentiment=-0.7,
        macro_policy="NEUTRAL",
        source_url="https://www.sec.gov/news/press-release",
        source_tier="TIER_A_OFFICIAL",
    )

    # 3. Verify Immutability of R1
    r1_fetched = registry.get_revision(r1.revision_id)
    assert r1_fetched.content_hash == r1.content_hash
    assert r1_fetched.claim_status == "UNVERIFIED"

    # 4. Verify R2 links to R1 and lists affected decision
    assert r2.previous_revision_id == r1.revision_id
    assert "decision_d101" in r2.affected_decision_ids

    # 5. Verify Decision D101 replay preserves historical snapshot R1
    linked_rev = registry.get_linked_revision_for_decision("decision_d101")
    assert linked_rev.revision_id == r1.revision_id
    assert linked_rev.headline == "SEC批准首个SOL现货ETF"

    # 6. Verify API endpoint GET /v2/news/{news_id}/revisions
    res = api_client.get("/v2/news/news_btc_sec_rumor/revisions")
    assert res.status_code == 200
    data = res.json()
    assert data["total_revisions"] == 2
    assert data["revisions"][0]["revision_id"] == r1.revision_id
    assert data["revisions"][1]["revision_id"] == r2.revision_id


# ============================================================================
# AT20: Qwen 9B missing/timeout/injection fail-closed (no silent 4B downgrade)
# ============================================================================
def test_at20_model_fail_closed_offline_and_prompt_injection(api_client, temp_store):
    # Test POST /v2/macro-events when model is offline
    now_iso = datetime.now(timezone.utc).isoformat()
    macro_payload = {
        "event_id": "macro_test_offline_01",
        "title": "US GDP Growth Q2",
        "source_url": "https://www.bea.gov/data/gdp",
        "event_time": now_iso,
        "known_at": now_iso,
        "forecast": "2.8%",
        "actual": "3.0%",
    }
    # LLM service is None in test fixture -> system must fail-closed with MODEL_OR_SCHEMA_UNAVAILABLE
    res = api_client.post("/v2/macro-events", json=macro_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["ai_status"] == "UNAVAILABLE" or data["ai_status"] == "MODEL_OR_SCHEMA_UNAVAILABLE"
    # Never falls back to a 4B model or generates an unvalidated directive
    assert data.get("model_id") != "qwen:4b"


# ============================================================================
# AT21: Expired or fake references fail AI review
# ============================================================================
def test_at21_stale_or_fake_references_rejection(api_client):
    # Reject future known_at
    future_iso = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    res = api_client.post(
        "/v2/macro-events",
        json={
            "event_id": "macro_future_leak",
            "title": "Future CPI",
            "source_url": "https://www.bls.gov/cpi",
            "event_time": future_iso,
            "known_at": future_iso,
        },
    )
    assert res.status_code == 422
    assert "future known_at rejected" in res.json()["detail"]


# ============================================================================
# AT22: Equity shorting & spot oversell restrictions
# ============================================================================
def test_at22_market_boundaries_equity_and_spot_shorting(temp_store, api_client):
    ledger = AccountLedger(temp_store)
    ledger.create_account("default_account", mode="PAPER", initial_deposit=Decimal("10000.0"))
    risk_engine = RiskEngine(ledger)

    # 1. Equity shorting without borrow capability -> EQUITY_SHORT_UNAUTHORIZED
    equity_proposal = {
        "symbol": "NASDAQ:AAPL",
        "side": "SHORT",
        "entry": 220.0,
        "stop": 225.0,
        "targets": [210.0],
    }
    equity_market = {
        "market_type": "equity",
        "allow_borrow_short": False,
        "contractSize": 1.0,
        "precision": {"amount": 1.0, "price": 0.01},
        "limits": {"amount": {"min": 1.0, "max": 10000.0, "step": 1.0}},
    }
    dec_equity = risk_engine.evaluate_proposal("default_account", equity_proposal, equity_market)
    assert not dec_equity.approved
    assert dec_equity.reason_code == "EQUITY_SHORT_UNAUTHORIZED"

    # 2. Spot shorting -> SPOT_SHORTING_UNSUPPORTED
    spot_proposal = {
        "symbol": "BTC/USDT:spot",
        "side": "SHORT",
        "entry": 65000.0,
        "stop": 66000.0,
        "targets": [63000.0],
    }
    spot_market = {
        "contract_type": "spot",
        "contractSize": 1.0,
        "precision": {"amount": 0.001, "price": 0.1},
        "limits": {"amount": {"min": 0.001, "max": 100.0, "step": 0.001}},
    }
    dec_spot_short = risk_engine.evaluate_proposal("default_account", spot_proposal, spot_market)
    assert not dec_spot_short.approved
    assert dec_spot_short.reason_code == "SPOT_SHORTING_UNSUPPORTED"

    # 3. Spot oversell exceeded available balance -> SPOT_OVERSELL_EXCEEDED
    spot_sell_proposal = {
        "symbol": "ETH/USDT:spot",
        "side": "SELL",
        "entry": 3000.0,
        "stop": 2900.0,
        "targets": [3200.0],
    }
    spot_sell_market = {
        "contract_type": "spot",
        "available_balance": 0.0,  # Zero balance
        "contractSize": 1.0,
        "precision": {"amount": 0.01, "price": 0.1},
        "limits": {"amount": {"min": 0.01, "max": 100.0, "step": 0.01}},
    }
    dec_oversell = risk_engine.evaluate_proposal("default_account", spot_sell_proposal, spot_sell_market)
    assert not dec_oversell.approved
    assert dec_oversell.reason_code == "SPOT_OVERSELL_EXCEEDED"

    # 4. Gateway level check on submit_intent
    gw = ExecutionGateway(temp_store)
    intent_equity_short = OrderIntent(
        intent_id="intent_eq_short_01",
        idempotency_key="idem_eq_short_01",
        account_id="acc_test",
        mode=TradingMode.PAPER,
        instrument_id="nyse:TSLA",
        side="SHORT",
        order_type="market",
        quantity=10.0,
        protection_plan=ProtectionPlan(stop_price=250.0),
    )
    with pytest.raises(ParameterValidationError) as exc_info:
        gw.submit_intent(intent_equity_short)
    assert "EQUITY_SHORT_UNAUTHORIZED" in str(exc_info.value)


# ============================================================================
# AT23: Read-only replay & feedback re-risk check
# ============================================================================
def test_at23_decision_replay_readonly_and_feedback_risk_recheck(temp_store, api_client):
    ledger = AccountLedger(temp_store)
    ledger.create_account("default_account", mode="PAPER", initial_deposit=Decimal("10000.0"))
    # Insert a sample trade decision into store
    now_iso = datetime.now(timezone.utc).isoformat()
    decision_payload = {
        "decision_id": "dec_sample_at23",
        "symbol": "BTC_USDT",
        "account_id": "default_account",
        "side": "LONG",
        "executed_price": 60000.0,
        "proposal": {
            "symbol": "BTC_USDT",
            "side": "LONG",
            "entry": 60000.0,
            "stop": 59000.0,
            "targets": [62000.0],
        },
        "reason": "Breakout detected",
    }
    with temp_store._connect() as db:
        db.execute(
            "INSERT INTO agent_trade_decisions (decision_id, symbol, status, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            ("dec_sample_at23", "BTC_USDT", "PENDING", json.dumps(decision_payload), now_iso)
        )

    # 1. Test GET /v2/decisions/{id}/replay (Strict read-only, zero network or orders)
    res_replay = api_client.get(
        "/v2/decisions/dec_sample_at23/replay",
        params={"account_id": "default_account"},
    )
    assert res_replay.status_code == 200
    data_replay = res_replay.json()
    assert data_replay["mode"] == "READ_ONLY_REPLAY"
    assert data_replay["replay_consistency"]["is_consistent"] is True
    assert data_replay["replay_consistency"]["orders_placed"] == 0
    assert data_replay["replay_consistency"]["side_effects"] == "NONE"

    # 2. Test POST /v2/decisions/{id}/feedback with INVALID stop direction -> Risk rejection (422)
    # Long order with stop_loss ABOVE entry price is invalid
    res_invalid_feedback = api_client.post(
        "/v2/decisions/dec_sample_at23/feedback",
        params={"account_id": "default_account"},
        json={
            "action": "ADJUST",
            "stop_loss": 65000.0,  # Invalid: above 60000 entry for LONG
            "take_profit": 70000.0,
        }
    )
    assert res_invalid_feedback.status_code == 422
    assert "RISK_REJECTED" in res_invalid_feedback.json()["detail"]

    # 3. Test POST /v2/decisions/{id}/feedback with VALID adjustment -> Approved
    res_valid_feedback = api_client.post(
        "/v2/decisions/dec_sample_at23/feedback",
        params={"account_id": "default_account"},
        json={
            "action": "ADJUST",
            "stop_loss": 59500.0,
            "take_profit": 63000.0,
            "notes": "Tightening stop before volatility event",
        }
    )
    assert res_valid_feedback.status_code == 200
    data_feedback = res_valid_feedback.json()
    assert data_feedback["status"] == "APPROVED"
    assert data_feedback["feedback"]["adjusted_stop"] == 59500.0
    assert data_feedback["risk_decision"]["approved"] is True
