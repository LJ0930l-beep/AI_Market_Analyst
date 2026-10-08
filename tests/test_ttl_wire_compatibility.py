"""Actual coordinator and independent auditor; synthetic data is not profit evidence."""
from copy import deepcopy
import json

import pytest

from core.trading.model_schemas import (
    AI_ACTION_SCHEMA, gemini_response_format, normalize_limit_ttl_alias,
    pin_gate_repair_trade_fields, validate_schema,
)
from scripts.verify_ai_template_replay import audit_effective_request, audit_ttl_wire_value
from tests.test_gate_open_generation_repair import RepairingProvider, PROPOSAL
from tests.test_replay_repair_audit import repair_fixture


@pytest.mark.parametrize('value', ['60', '777', '900', '1800'])
def test_canonical_wire_text_is_exact_and_auditable(value):
    proposal = {**PROPOSAL, 'ttl_seconds': value}
    original = deepcopy(proposal)
    evidence = normalize_limit_ttl_alias(proposal)
    assert evidence['raw'] == value and evidence['normalized'] == int(value)
    assert proposal == {**original, 'ttl_seconds': int(value)}
    assert audit_ttl_wire_value(original) == proposal
    validate_schema(proposal['ttl_seconds'], AI_ACTION_SCHEMA['properties']['ttl_seconds'])


@pytest.mark.parametrize('value', ['0', '59', '1801', '0900', '900.0', '9e2', '+900', '-900',
                                  ' 900', '900 ', '９００', '9' * 10000, 'null', ''])
def test_invalid_text_is_not_coerced_or_truncated(value):
    proposal = {'ttl_seconds': value}
    assert normalize_limit_ttl_alias(proposal) is None
    assert proposal == {'ttl_seconds': value}
    with pytest.raises(ValueError):
        validate_schema(proposal['ttl_seconds'], AI_ACTION_SCHEMA['properties']['ttl_seconds'])
    with pytest.raises(ValueError, match='AUDIT_TTL_WIRE_INVALID'):
        audit_ttl_wire_value(proposal)


def test_wire_pin_keeps_the_previously_selected_duration_and_local_schema():
    original = deepcopy(AI_ACTION_SCHEMA)
    pinned = pin_gate_repair_trade_fields(original, {'ttl_seconds': 777}, set())
    wire = gemini_response_format(pinned)['json_schema']['schema']['properties']['ttl_seconds']
    assert wire['type'] == 'object'
    assert [wire['properties'][key]['enum'] for key in ('d3', 'd2', 'd1', 'd0')] == [['0'], ['7'], ['7'], ['7']]
    assert pinned['properties']['ttl_seconds']['minimum'] == 777
    assert pinned['properties']['ttl_seconds']['maximum'] == 777
    assert original == AI_ACTION_SCHEMA


def test_alias_conflicts_and_boolean_are_still_rejected():
    for proposal in ({'ttl_seconds': '900', 'limit_ttl_seconds': 900},
                     {'limit_ttl_seconds': True}, {'ttl_seconds': True}):
        before = deepcopy(proposal)
        assert normalize_limit_ttl_alias(proposal) is None and proposal == before


class WireProvider(RepairingProvider):
    def __init__(self, *, drift=False):
        super().__init__()
        self.ttl_drift = drift

    def generate_json(self, messages, **options):
        decision, _, metadata = super().generate_json(messages, **options)
        decision['ttl_seconds'] = '901' if self.ttl_drift and len(self.payloads) == 2 else '900'
        return decision, json.dumps(decision), metadata


class DigitProvider(RepairingProvider):
    def generate_json(self, messages, **options):
        decision, _, metadata = super().generate_json(messages, **options)
        decision['ttl_seconds'] = {'d3': '0', 'd2': '9', 'd1': '0', 'd0': '0'}
        return decision, json.dumps(decision), metadata


def test_all_allowed_durations_have_exact_digit_representation_without_narrowing_choice():
    wire = gemini_response_format(AI_ACTION_SCHEMA)['json_schema']['schema']['properties']['ttl_seconds']
    for value in range(60, 1801):
        digits = dict(zip(('d3', 'd2', 'd1', 'd0'), f'{value:04d}'))
        assert all(character in wire['properties'][key]['enum'] for key, character in digits.items())
        raw = {'ttl_seconds': digits}
        proposal = deepcopy(raw)
        assert normalize_limit_ttl_alias(proposal)['normalized'] == value
        assert proposal == audit_ttl_wire_value(raw) == {'ttl_seconds': value}


def test_unused_wait_zero_digits_have_no_order_duration_and_preserve_raw():
    raw = {'action': 'WAIT', 'instrument_id': 'ETHUSDT', 'reason': 'Await confirmation',
           'ttl_seconds': dict.fromkeys(('d3', 'd2', 'd1', 'd0'), '0'),
           'next_trigger_price': 4105.89}
    before = deepcopy(raw)
    decoded = deepcopy(raw)
    evidence = normalize_limit_ttl_alias(decoded)
    assert raw == before and evidence['raw'] == raw['ttl_seconds']
    assert evidence['source'] == 'WAIT_UNUSED_TTL_ZERO_TO_NULL'
    assert decoded == audit_ttl_wire_value(raw) == {**raw, 'ttl_seconds': None}
    validate_schema(decoded['ttl_seconds'], AI_ACTION_SCHEMA['properties']['ttl_seconds'])


@pytest.mark.parametrize('action', ['OPEN_LONG', 'OPEN_SHORT', 'HOLD', 'CANCEL_ORDER',
                                    'UPDATE_PROTECTION', 'CLOSE_POSITION', None])
def test_zero_digits_never_become_valid_order_duration(action):
    raw = {'action': action, 'ttl_seconds': dict.fromkeys(('d3', 'd2', 'd1', 'd0'), '0')}
    before = deepcopy(raw)
    assert normalize_limit_ttl_alias(raw) is None and raw == before
    with pytest.raises(ValueError, match='AUDIT_TTL_WIRE_INVALID'):
        audit_ttl_wire_value(raw)


@pytest.mark.parametrize('value', [1, 59, 1801])
def test_wait_nonzero_invalid_duration_is_not_erased(value):
    raw = {'action': 'WAIT', 'ttl_seconds': dict(zip(('d3', 'd2', 'd1', 'd0'), f'{value:04d}'))}
    before = deepcopy(raw)
    assert normalize_limit_ttl_alias(raw) is None and raw == before
    with pytest.raises(ValueError, match='AUDIT_TTL_WIRE_INVALID'):
        audit_ttl_wire_value(raw)


class ZeroWaitProvider(RepairingProvider):
    model_id = 'gemini-3.8-flash-high'

    def generate_json(self, messages, **options):
        output = {'action': 'WAIT', 'instrument_id': 'ETHUSDT',
                  'confidence': 45,
                  'reason': 'Wait for a confirmed structure break',
                  'entry_condition': 'Closed 15m bar breaks the structure',
                  'strategy_analysis': {'missing_conditions': ['confirmed structure break']},
                  'ttl_seconds': dict.fromkeys(('d3', 'd2', 'd1', 'd0'), '0')}
        return output, json.dumps(output), {
            'model_id': self.model_id, 'model_version': self.model_id,
            'actual_model_id': self.model_id, 'model_identity_source': 'completion_response',
            'verified_manifest_model_id': self.model_id}


def test_wait_zero_digits_cross_actual_coordinator_and_independent_audit_without_order(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch, ZeroWaitProvider())
    assert json.loads(ctx['model_raw_response'])['ttl_seconds'] == dict.fromkeys(('d3', 'd2', 'd1', 'd0'), '0')
    evidence = ctx['model_inference_settings']['model_output_normalizations']
    assert any(item.get('source') == 'WAIT_UNUSED_TTL_ZERO_TO_NULL' for item in evidence)
    audit_effective_request(ctx, original, bundles.__getitem__)
    from tests.test_ai_template_runner import rows
    row = rows(tmp_path / 'audit.sqlite3')[0]
    assert row['status'] == 'COMPLETED'
    assert json.loads(row['decision_json'])['action'] == 'WAIT'
    assert json.loads(row['result_json'])['private_exchange_calls'] == 0


@pytest.mark.parametrize('digits', [
    {'d3':'0','d2':'0','d1':'5','d0':'9'},
    {'d3':'1','d2':'8','d1':'0','d0':'1'},
    {'d3':'0','d2':'9','d1':'0','d0':'0','extra':'0'},
    {'d3':'0','d2':'9','d1':'0'},
    {'d3':'0','d2':'9 prose','d1':'0','d0':'0'},
    {'d3':'0','d2':9,'d1':'0','d0':'0'},
    {'d3':'0','d2':'９','d1':'0','d0':'0'},
])
def test_bad_digits_are_not_guessed_or_repaired_locally(digits):
    raw = {'ttl_seconds': digits}
    before = deepcopy(raw)
    assert normalize_limit_ttl_alias(raw) is None and raw == before
    with pytest.raises(ValueError, match='AUDIT_TTL_WIRE_INVALID'):
        audit_ttl_wire_value(raw)


def test_actual_coordinator_digit_repair_preserves_original_and_is_independently_auditable(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch, DigitProvider())
    assert json.loads(ctx['model_raw_response'])['ttl_seconds'] == {'d3':'0','d2':'9','d1':'0','d0':'0'}
    request = audit_effective_request(ctx, original, bundles.__getitem__)
    assert request['local_validation_schema']['properties']['ttl_seconds']['minimum'] == 900


def test_actual_repair_preserves_raw_text_and_executes_identical_ttl(tmp_path, monkeypatch):
    ctx, original, bundles = repair_fixture(tmp_path, monkeypatch, WireProvider())
    assert json.loads(ctx['model_raw_response'])['ttl_seconds'] == '900'
    assert ctx['model_inference_settings']['model_output_normalizations'][0]['normalized'] == 900
    request = audit_effective_request(ctx, original, bundles.__getitem__)
    assert request['local_validation_schema']['properties']['ttl_seconds']['minimum'] == 900
    attempts = ctx['model_inference_settings']['model_response_audit']['attempts']
    assert all(json.loads(attempt['raw_response'])['ttl_seconds'] == '900' for attempt in attempts)


def test_changed_ttl_cannot_pass_either_production_or_independent_audit(tmp_path, monkeypatch):
    from core.replay.ai_template_runner import run_ai_template_replay
    from tests.test_ai_template_runner import history, rows
    from core.model_client import model_client
    monkeypatch.setattr(model_client, 'count_tokens', lambda text, **kw: max(1, len(text) // 3))
    path = tmp_path / 'drift.sqlite3'
    report = run_ai_template_replay(history(5), db_path=path, model_provider=WireProvider(drift=True),
                                   priority_guard=lambda: None, max_decisions=1)
    assert rows(path)[0]['status'] == 'ERROR' and report['errors']
    ctx, original, bundles = repair_fixture(tmp_path / 'good', monkeypatch, WireProvider())
    raw = json.loads(ctx['model_raw_response']); raw['ttl_seconds'] = '901'
    ctx['model_raw_response'] = json.dumps(raw)
    with pytest.raises(ValueError, match='REPAIR_CHANGED_TRADE'):
        audit_effective_request(ctx, original, bundles.__getitem__)
