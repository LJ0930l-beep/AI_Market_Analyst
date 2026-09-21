from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pytest
from core.storage import SQLiteStore
from core.providers.gateio_provider import GatePublicProvider
from core.trading.strategy_execution import (
    DEFAULT_EXECUTION,
    normalize_execution,
    resolve_execution_leverage,
    size_position,
    venue_leverage_limit,
)


def sizing(config=None, **changes):
    values = dict(equity=10000, available=10000, used_margin=0, entry=100,
                  unit_risk=2, contract_size=1, step=.01, risk_fraction=.0025,
                  leverage=3, fee_rate=.0005)
    values.update(changes)
    return size_position(config or normalize_execution(), **values)


def test_stop_loss_budget_does_not_grow_with_leverage():
    one, three = sizing(leverage=1), sizing(leverage=3)
    assert one['notional_usdt'] == three['notional_usdt'] == 1250
    assert three['estimated_loss_usdt'] <= 25
    assert three['estimated_margin_usdt'] == pytest.approx(one['estimated_margin_usdt'] / 3)


def test_fixed_notional_is_reduced_for_wide_stop_and_margin_is_capped():
    config = {**normalize_execution(), 'sizing_mode': 'FIXED_NOTIONAL', 'fixed_notional_usdt': 1000}
    assert sizing(config)['notional_usdt'] == 1000
    assert sizing(config, unit_risk=10)['notional_usdt'] == 250
    result = sizing(config, available=25)
    assert result['notional_usdt'] < 75 and result['binding_limit'] == 'available_margin'
    assert sizing(config, used_margin=2100)['quantity'] == 0


def test_equity_percentage_and_model_request_only_reduce_the_budget():
    config = {**normalize_execution(), 'sizing_mode': 'EQUITY_PERCENT', 'equity_notional_pct': 2}
    assert sizing(config, requested_notional=1000)['notional_usdt'] == 200
    assert sizing(config, requested_notional=80)['notional_usdt'] == 80


def test_actual_leverage_uses_lowest_ai_user_venue_and_stop_risk_ceiling():
    config = {**normalize_execution(), 'leverage': 100}
    market = {
        'leverage_max': '75',
        'limits': {'leverage': {'min': 1, 'max': 75}},
    }
    resolved = resolve_execution_leverage(
        config,
        market=market,
        rule_leverage=30,
        requested_leverage=50,
    )
    assert resolved['leverage'] == 30
    assert resolved['binding_limit'] == 'risk_rule_cap'
    assert resolved['ceilings'] == {
        'strategy_user_cap': 100,
        'risk_rule_cap': 30,
        'venue_contract_cap': 75,
        'ai_requested_cap': 50,
    }


def test_venue_contract_cap_is_authoritative_and_missing_remote_limit_fails_closed():
    config = {**normalize_execution(), 'leverage': 100}
    assert venue_leverage_limit({'info': {'leverage_max': '20.9'}}) == 20
    resolved = resolve_execution_leverage(config, market={'leverage_max': 8}, rule_leverage=50)
    assert resolved['leverage'] == 8
    assert resolved['binding_limit'] == 'venue_contract_cap'
    with pytest.raises(ValueError, match='VENUE_LEVERAGE_LIMIT_UNAVAILABLE'):
        resolve_execution_leverage(config, market={}, rule_leverage=20)


def test_gate_native_contract_exposes_exchange_leverage_ceiling(monkeypatch):
    provider = GatePublicProvider(testnet=True)
    monkeypatch.setattr(provider, '_request', lambda *_args, **_kwargs: {
        'name': 'BTC_USDT', 'status': 'trading',
        'quanto_multiplier': '0.0001', 'order_price_round': '0.1',
        'order_size_min': '1', 'order_size_max': '1000000',
        'leverage_max': '100', 'taker_fee_rate': '0.0005',
        'maker_fee_rate': '-0.0001', 'mark_price': '80000',
        'index_price': '80001', 'funding_rate': '0.0001',
        'funding_next_apply': 1,
    })

    market = provider.market('BTCUSDT')

    assert market['leverage_max'] == 100
    assert market['limits']['leverage'] == {'min': 1, 'max': 100}


@pytest.mark.parametrize('change', [{'leverage': 101}, {'risk_per_trade_pct': .26}, {'max_positions': True}, {'max_margin_pct': float('nan')}, {'symbols': [{}]}, {'symbols': [], 'universe_mode': 'CUSTOM'}, {'scan_interval_minutes': 10}, {'fixed_notional_usdt': 6000}, {'direction': 'ANYTHING'}, {'atr_adaptive_sizing': 'yes'}])
def test_strategy_config_rejects_invalid_or_out_of_policy_values(change):
    with pytest.raises(ValueError):
        normalize_execution({**deepcopy(DEFAULT_EXECUTION), **change})


def test_latest_candles_are_not_truncated_by_old_history_or_downgraded_by_open_update(tmp_path):
    store = SQLiteStore(tmp_path / 'bars.sqlite3'); store.initialize()
    now = datetime(2026, 9, 13, 3, 30, tzinfo=timezone.utc)
    bars = [{'timestamp': now - timedelta(minutes=15 * (2500-i)), 'open': 100, 'high': 102,
             'low': 99, 'close': 101, 'volume': 10, 'is_closed': True} for i in range(2500)]
    identity = dict(provider='gate_test', venue='gate', market_type='perpetual', native_symbol='BTC_USDT', settle_currency='USDT', price_type='last')
    store.upsert_market_bars('BTCUSDT', '15m', bars, data_as_of=now, now=now, **identity)
    store.upsert_market_bars('BTCUSDT', '15m', [{**bars[-1], 'is_closed': False, 'close': 100}], data_as_of=now+timedelta(seconds=5), now=now+timedelta(seconds=5), **identity)
    recent = store.latest_bars('BTCUSDT', '15m', limit=64, venue='gate', market_type='perpetual', price_type='last')
    assert len(recent) == 64
    assert recent[-1]['bar_end'] == now.isoformat()
    assert recent[-1]['is_closed'] and recent[-1]['close'] == 101
    assert all(item['venue'] == 'gate' and item['price_type'] == 'last' for item in recent)
