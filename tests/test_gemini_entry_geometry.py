"""Offline arithmetic/provenance tests; synthetic cases are not trading proof."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from core.replay.ai_history import digest
from core.trading.ai_session_coordinator import _estimate_tokens
from scripts.review_gemini_entry_geometry import geometry_review, summary
from scripts.review_gemini_research_cases import review_request, OUTPUT_TOKENS

IDENTITY = 'price_action_structure'


def fixture():
    cases = []
    for index, (side, net) in enumerate([('LONG', '3'), ('SHORT', '-2'), ('LONG', '0')]):
        scenario = {'entry_fill_price': '100', 'model_reference_entry_price': '99' if side == 'LONG' else '101',
            'initial_stop': '98' if side == 'LONG' else '102',
            'initial_target': '105' if side == 'LONG' else '95',
            'target_net_usdt': '4', 'stop_loss_including_fees_usdt': '3',
            'net_reward_to_loss': str(Decimal(4) / Decimal(3)),
            'entry_price_change_cost_including_latency_usdt': '.1'}
        cases.append({'case_id': f'synthetic-{index}', 'template_id': IDENTITY, 'window': 'pilot-1',
            'side': side, 'net_pnl_usdt': net, 'fees_usdt': '1', 'gross_pnl_usdt': str(Decimal(net) + 1),
            'funding_pnl_usdt': '0', 'net_outcome': 'WIN' if Decimal(net)>0 else 'LOSS' if Decimal(net)<0 else 'BREAKEVEN',
            'opened_at': '2025-10-15T01:00:00Z', 'closed_at': '2025-10-15T01:01:00.500000Z',
            'entry_fills': [{'order_id': 'entry', 'fill_price': '100', 'netted_quantity': 0, 'fee_type': 'TAKER'}],
            'entry_model_proposals': {'entry': {'decision': {'entry_price': scenario['model_reference_entry_price'],
                'order_preference': 'LIMIT', 'reason': 'Synthetic test only'},
                'effective_model_visible_entry_facts': {'technical_context': {'timeframes': {
                    '15m': {'indicators': {'atr14_simple': '8'}}, '1h': {'indicators': {'atr14_simple': '40'}}}}}}},
            'exit_ledger_triggers': [{'action': 'STOP_LOSS' if net == '-2' else 'TAKE_PROFIT'}],
            'initial_plan_scenario': scenario})
    return {'plan_sha256': 'synthetic-plan', 'cases': cases, 'case_count': len(cases)}


def test_every_outcome_is_accounted_with_exact_decimal_ratios_and_aware_time():
    book = fixture()
    original = deepcopy(book)
    review = geometry_review(book, {IDENTITY: '15m'})
    assert book == original
    assert review['casebook_sha256'] == digest(book) and review['case_count'] == 3
    assert {r['case_id'] for r in review['case_rows']} == {c['case_id'] for c in book['cases']}
    for row in review['case_rows']:
        assert row['initial_stop_distance_over_visible_signal_atr'] == '0.25'
        assert row['initial_stop_width_pct'] == '2.00'
        assert row['hold_seconds'] == '60.5'
        assert row['unavailable_reason'] is None
    assert sum(r[2] for r in review['aggregate_rows']) == 3
    assert sum(Decimal(r[3]) for r in review['aggregate_rows']) == 1
    assert review['private_exchange_calls'] == review['production_strategy_writes'] == 0


def test_missing_visible_signal_atr_does_not_borrow_higher_timeframe_or_future_values():
    book = fixture()
    facts = book['cases'][0]['entry_model_proposals']['entry']['effective_model_visible_entry_facts']
    del facts['technical_context']['timeframes']['15m']['indicators']['atr14_simple']
    review = geometry_review(book, {IDENTITY: '15m'})
    row = review['case_rows'][0]
    assert row['initial_stop_distance_over_visible_signal_atr'] is None
    assert row['initial_fee_only_reward_to_loss'] is not None
    assert row['unavailable_reason'] == 'MODEL_VISIBLE_SIGNAL_ATR_UNAVAILABLE'
    assert review['aggregate_rows'][0][-1] == {'MODEL_VISIBLE_SIGNAL_ATR_UNAVAILABLE': 1}


def test_scaled_or_netted_cases_remain_in_closed_and_profit_denominators():
    book = fixture()
    book['cases'][0]['initial_plan_scenario'] = None
    book['cases'][0]['entry_fills'] *= 2
    review = geometry_review(book, {IDENTITY: '15m'})
    assert review['case_count'] == 3
    assert review['aggregate_rows'][0][2:5] == [1, '3', 0]
    assert review['case_rows'][0]['initial_stop_width_pct'] is None
    assert review['case_rows'][0]['hold_seconds'] == '60.5'


@pytest.mark.parametrize('defect', ['duplicate', 'count', 'side', 'outcome', 'naive', 'negative_time',
    'fill', 'reference', 'order', 'netted', 'scenario_multiple', 'wrong_stop', 'wrong_target',
    'bad_rr', 'zero_atr', 'nan_atr', 'infinite_net'])
def test_inconsistent_case_or_source_is_refused(defect):
    book = fixture()
    c = book['cases'][0]
    if defect == 'duplicate': book['cases'][1]['case_id'] = c['case_id']
    elif defect == 'count': book['case_count'] = 2
    elif defect == 'side': c['side'] = 'UNKNOWN'
    elif defect == 'outcome': c['net_outcome'] = 'LOSS'
    elif defect == 'naive': c['opened_at'] = '2025-10-15T01:00:00'
    elif defect == 'negative_time': c['closed_at'] = '2025-10-15T00:00:00Z'
    elif defect == 'fill': c['entry_fills'][0]['fill_price'] = '101'
    elif defect == 'reference': c['entry_model_proposals']['entry']['decision']['entry_price'] = '100'
    elif defect == 'order': c['entry_fills'][0]['order_id'] = 'other'
    elif defect == 'netted': c['entry_fills'][0]['netted_quantity'] = 1
    elif defect == 'scenario_multiple': c['entry_fills'] *= 2
    elif defect == 'wrong_stop': c['initial_plan_scenario']['initial_stop'] = '102'
    elif defect == 'wrong_target': c['initial_plan_scenario']['initial_target'] = '95'
    elif defect == 'bad_rr': c['initial_plan_scenario']['net_reward_to_loss'] = '2'
    elif defect in ('zero_atr', 'nan_atr'):
        c['entry_model_proposals']['entry']['effective_model_visible_entry_facts']['technical_context']['timeframes']['15m']['indicators']['atr14_simple'] = 0 if defect == 'zero_atr' else 'NaN'
    else: c['net_pnl_usdt'] = 'Infinity'
    with pytest.raises(ValueError):
        geometry_review(book, {IDENTITY: '15m'})


def test_zero_sample_explicit_null_summaries_and_zero_counts():
    book = {'plan_sha256': 'synthetic', 'cases': [], 'case_count': 0}
    review = geometry_review(book, {IDENTITY: '15m'})
    assert len(review['aggregate_rows']) == 3
    assert all(r[2] == 0 and r[5] == {'count': 0, 'min': None, 'median': None, 'max': None} for r in review['aggregate_rows'])
    assert summary([Decimal('1.00000000001'), Decimal('1.00000000003')])['median'] == '1.00000000002'


def test_candidate_input_binds_all_outcomes_and_full_casebook_with_budget():
    book = fixture()
    evidence = {'plan_sha256': book['plan_sha256'], 'styles': [{'template_id': IDENTITY,
        'name': 'PA', 'profile': {'signal_timeframe': '15m'}}], 'windows': [{'window': 'pilot-1',
        'errors': [], 'strategies': [{'template_id': IDENTITY, 'closed_trade_count': 3}]}]}
    geom = geometry_review(book, {IDENTITY: '15m'})
    messages, _ = review_request(evidence, book, entry_geometry=geom)
    data = json.loads(messages[1]['content'])
    assert data['entry_geometry_sha256'] == digest(geom)
    assert [r[1:5] for r in data['entry_geometry_rows']] == [r[1:5] for r in geom['aggregate_rows']]
    assert sum(r[2] for r in data['entry_geometry_rows']) == book['case_count']
    assert sum(_estimate_tokens(m['content']) for m in messages) + OUTPUT_TOKENS + 256 <= 8192
    altered = deepcopy(geom)
    altered['aggregate_rows'][0][3] = '999'
    with pytest.raises(ValueError, match='GEOMETRY_BINDING_INVALID'):
        review_request(evidence, book, entry_geometry=altered)
