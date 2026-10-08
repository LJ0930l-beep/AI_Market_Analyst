"""Synthetic deterministic prompt budgets; no live exchange evidence."""
from copy import deepcopy
import json

import pytest

from core.trading.ai_session_coordinator import _fit_prompt_payload
from core.trading.model_schemas import AI_ACTION_SCHEMA, frozen_decision_schema


def payload():
    return {'allowed_instruments': ['ETHUSDT', 'BTCUSDT'],
            'market_snapshots': {'ETHUSDT': {'price': 100}, 'BTCUSDT': {'price': 200}},
            'technical_context': {},
            'evidence_refs': ['market_snapshot:ETHUSDT:e', 'market_snapshot:BTCUSDT:b'],
            'account_truth': {'equity': 1000, 'positions': [], 'pending_orders': []}}


def counter(content):
    if not content.startswith('{'):
        return 0
    return 2000 if len(json.loads(content).get('allowed_instruments', [])) > 1 else 500


def test_baseline_reproduces_second_candidate_disappearing_before_pinned_repair():
    fitted, meta = _fit_prompt_payload(payload(), '', 1000, reserve=0, token_counter=counter)
    assert fitted['allowed_instruments'] == ['ETHUSDT']
    schema = deepcopy(AI_ACTION_SCHEMA)
    schema['properties']['instrument_id']['enum'] = ['BTCUSDT']
    with pytest.raises(ValueError, match='MODEL_VISIBLE_INSTRUMENTS_OUTSIDE_PINNED_SCHEMA'):
        frozen_decision_schema(fitted, schema)
    assert meta['deferred_symbols'] == ['BTCUSDT']


def test_repair_keeps_selected_second_symbol_and_full_account_without_reordering_facts():
    original = payload()
    fitted, meta = _fit_prompt_payload(original, '', 1000, reserve=0, token_counter=counter,
                                      required_symbols=('BTCUSDT',))
    assert fitted['allowed_instruments'] == ['BTCUSDT']
    assert fitted['market_snapshots'] == {'BTCUSDT': {'price': 200}}
    assert fitted['account_truth'] == original['account_truth']
    assert fitted['evidence_refs'] == ['market_snapshot:BTCUSDT:b']
    assert original == payload()
    schema = deepcopy(AI_ACTION_SCHEMA)
    schema['properties']['instrument_id']['enum'] = ['BTCUSDT']
    assert frozen_decision_schema(fitted, schema)['properties']['instrument_id']['enum'] == ['BTCUSDT']
    assert meta['required_symbols'] == ['BTCUSDT']


def test_required_symbol_plus_owned_position_cannot_be_dropped_to_force_a_fit():
    source = payload()
    source['account_truth']['positions'] = [{'symbol': 'ETHUSDT', 'position_id': 'owned'}]
    with pytest.raises(ValueError, match='AI_INPUT_BUDGET_EXCEEDED'):
        _fit_prompt_payload(source, '', 1000, reserve=0, token_counter=counter,
                            required_symbols=('BTCUSDT',))


def test_cross_asset_cited_evidence_cannot_be_silently_dropped():
    with pytest.raises(ValueError, match='AI_INPUT_BUDGET_EXCEEDED'):
        _fit_prompt_payload(payload(), '', 1000, reserve=0, token_counter=counter,
                            required_symbols=('ETHUSDT', 'BTCUSDT'))


def test_required_symbol_must_already_be_visible_not_injected():
    with pytest.raises(ValueError, match='AI_REQUIRED_SYMBOL_NOT_VISIBLE'):
        _fit_prompt_payload(payload(), '', 1000, reserve=0, token_counter=counter,
                            required_symbols=('SOLUSDT',))


def test_real_coordinator_pins_second_asset_through_a_smaller_repair_window(tmp_path, monkeypatch):
    from core.model_client import model_client
    from core.replay import ai_template_runner as runner
    from core.replay.ai_history import ReplayHistory, manifest_hash
    from tests.test_ai_template_runner import FixtureProvider, history, rows

    source = deepcopy(history(5).payload)
    source['symbols'].append('BTCUSDT')
    contract = deepcopy(source['contracts']['ETHUSDT'])
    contract.update(instrument_id='BTCUSDT', native_symbol='BTC_USDT')
    contract['market'].update(id='BTC_USDT', symbol='BTCUSDT')
    source['contracts']['BTCUSDT'] = contract
    additions = []
    for original in source['bars']:
        bar = deepcopy(original)
        bar.update(symbol='BTCUSDT', native_symbol='BTC_USDT',
                   instrument_key='gate:perpetual:BTCUSDT:USDT:last')
        additions.append(bar)
    source['bars'].extend(additions)
    source['manifest_sha256'] = manifest_hash(source)

    def tokens(content, **_kwargs):
        if not content.startswith('{'):
            return 0
        item = json.loads(content)
        if 'previous_decision' in item:
            return 600
        return 1000 if len(item.get('allowed_instruments', [])) > 1 else 200
    monkeypatch.setattr(model_client, 'count_tokens', tokens)

    class Provider(FixtureProvider):
        context_length = 2200
        def generate_json(self, messages, **options):
            wrapper = json.loads(messages[1]['content'])
            repair = 'previous_decision' in wrapper
            inputs = wrapper['inputs'] if repair else wrapper
            self.payloads.append(inputs)
            assert 'BTCUSDT' in inputs['allowed_instruments']
            decision = {'action': 'OPEN_LONG', 'instrument_id': 'BTCUSDT', 'reason': 'unit second candidate',
                'confidence': 80, 'entry_price': 100, 'stop_price': 90, 'take_profit': 200,
                'evidence_refs': [ref for ref in inputs['evidence_refs'] if ref.startswith('market_snapshot:BTCUSDT:')]}
            if repair:
                assert options['schema']['properties']['instrument_id']['enum'] == ['BTCUSDT']
                assert inputs['allowed_instruments'] == ['BTCUSDT']
                assert inputs['account_truth'] == self.payloads[0]['account_truth']
                decision.update(position_size_usdt=2000, requested_leverage=40, order_preference='MARKET')
            raw = json.dumps(decision)
            return decision, raw, {'model_id': self.model_id, 'model_version': self.model_id,
                'actual_model_id': self.model_id, 'model_identity_source': 'completion_response',
                'verified_manifest_model_id': self.model_id}

    provider = Provider()
    database = tmp_path / 'replay.sqlite3'
    result = runner.run_ai_template_replay(ReplayHistory(source), db_path=database,
        model_provider=provider, priority_guard=lambda: None, max_decisions=1)
    assert not result['errors'] and len(provider.payloads) == 2
    record = rows(database)[0]
    assert record['status'] == 'COMPLETED'
    ctx = json.loads(record['context_json'])
    assert ctx['model_inference_settings']['repair_prompt_compaction']['required_symbols'] == ['BTCUSDT']
    assert json.loads(record['decision_json'])['instrument_id'] == 'BTCUSDT'
    assert json.loads(record['result_json'])['private_exchange_calls'] == 0
