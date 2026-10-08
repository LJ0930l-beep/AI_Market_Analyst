"""Bounded model repair preserves all trading facts and frozen style."""
import ast
from copy import deepcopy
import json

import pytest

import core.trading.ai_session_coordinator as coordinator
import core.trading.model_schemas as schemas
from tests.active_gemini_sources import active_sources as proposed_sources


def helpers():
    _, sources = proposed_sources()
    namespace = {**vars(coordinator), **vars(schemas)}
    names = {'gate_wait_text_repair_fields', 'assert_wait_text_repair_preserves_decision',
             '_gate_repair_system_prompt', '_wait_text_repair_inputs', '_gate_wait_text_repair_system_prompt'}
    for source in sources.values():
        nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     '<prepared-bounded-repair>', 'exec'), namespace)
    return namespace


def waiting():
    return {'action': 'WAIT', 'instrument_id': 'BTCUSDT', 'reason': 'Wait for confirmation',
            'next_trigger_price': 100, 'confidence': 70, 'evidence_refs': ['technical_snapshot:BTCUSDT:fixture'],
            'strategy_analysis': {'missing_conditions': ['volume'], 'matched_conditions': ['trend']},
            'timeframe_analysis': {'5m': 'Original evidence. ' * 100, '1h': 'unchanged background'},
            'stop_price': None, 'requested_leverage': None}


def test_only_authorized_wait_overlong_timeframe_has_one_mutable_path():
    fn = helpers()['gate_wait_text_repair_fields']
    value = waiting()
    assert fn(value, 'INVALID_ACTION_SCHEMA:output.timeframe_analysis.5m:length', ['BTCUSDT']) == {'timeframe_analysis.5m'}
    for error in ('INVALID_ACTION_SCHEMA:output.stop_price:exclusive_range',
                  'INVALID_ACTION_SCHEMA:output.reason:length', 'MODEL_TIMEOUT'):
        assert not fn(value, error, ['BTCUSDT'])
    assert not fn(value, 'INVALID_ACTION_SCHEMA:output.timeframe_analysis.5m:length', ['ETHUSDT'])
    value['action'] = 'OPEN_LONG'
    assert not fn(value, 'INVALID_ACTION_SCHEMA:output.timeframe_analysis.5m:length', ['BTCUSDT'])


@pytest.mark.parametrize('change', ['action', 'instrument_id', 'next_trigger_price', 'reason', 'confidence',
                                  'evidence_refs', 'strategy_analysis', 'requested_leverage', 'other_frame', 'remove', 'add'])
def test_wait_text_repair_cannot_change_or_remove_any_other_fact(change):
    original = waiting()
    revised = deepcopy(original)
    revised['timeframe_analysis']['5m'] = 'Concise original explanation'
    if change == 'other_frame':
        revised['timeframe_analysis']['1h'] = 'different'
    elif change == 'remove':
        revised.pop('next_trigger_price')
    elif change == 'add':
        revised['entry_price'] = 100
    else:
        revised[change] = 'changed'
    with pytest.raises(ValueError, match='CHANGED_WAIT_DECISION'):
        helpers()['assert_wait_text_repair_preserves_decision'](original, revised, {'timeframe_analysis.5m'})


def test_bounded_wait_explanation_can_change_and_all_original_fields_stay():
    original = waiting()
    revised = deepcopy(original)
    revised['timeframe_analysis']['5m'] = 'Concise original explanation'
    helpers()['assert_wait_text_repair_preserves_decision'](original, revised, {'timeframe_analysis.5m'})
    assert len(original['timeframe_analysis']['5m']) > 800


def test_repair_system_retains_exact_style_sections_contract_breakout_semantics_and_ownership():
    frozen = ('Unneeded decision loop\nJSON字段规则：fixed2000 margin18% leverage_max\n'
              '策略：Original family\n信号周期：5m\nsupport20/resistance20 original semantics\n'
              '当前策略的具体指令：\nrole original\nentry standards original\ncustom original\n'
              '只返回一个 JSON 对象。\nGate仅管理VERIFIED_SYSTEM rows')
    fn = helpers()['_gate_repair_system_prompt']
    revised = fn(frozen, 'repair only missing fields')
    for text in frozen.splitlines()[1:8]:
        assert text in revised
    assert 'Gate仅管理VERIFIED_SYSTEM rows' in revised
    assert 'Unneeded decision loop' not in revised
    assert fn('unknown layout', ' task') == 'unknown layout task'


def test_wait_text_inputs_keep_full_account_all_quote_values_identity_and_references_without_mutation():
    payload = {'account_id': 'fixture', 'mode': 'TESTNET', 'allowed_instruments': ['BTCUSDT', 'ETHUSDT'],
               'account_truth': {'owned_entry_orders': [{'order_id': 'fixture', 'price': 101.2345}]},
               'market_snapshots': {'BTCUSDT': {'price': 101.2345}, 'ETHUSDT': {'price': 202.9876}},
               'evidence_refs': ['technical_snapshot:BTCUSDT:fixture'], 'active_strategy': {'name': 'fixture'},
               'technical_context': {'fixture': 'raw history'}, 'news_revisions': []}
    before = deepcopy(payload)
    projected = helpers()['_wait_text_repair_inputs'](payload)
    for key in ('account_truth', 'market_snapshots', 'allowed_instruments', 'active_strategy', 'evidence_refs'):
        assert projected[key] == payload[key]
    projected['account_truth']['owned_entry_orders'].clear()
    assert payload == before


def test_wait_text_system_is_explicit_rewriting_not_a_new_strategy_decision():
    fn = helpers()['_gate_wait_text_repair_system_prompt']
    old = 'Original decision loop\n策略：frozen focus\n信号周期：5m\n当前策略的具体指令：frozen policy'
    text = fn(old, 'repair task')
    assert '不重新分析行情或策略' in text and '不改变决策' in text
    assert '策略：frozen focus' in text and '信号周期：5m' in text
    assert 'Original decision loop' not in text
    assert fn('unknown', ' task') == 'unknown task'
