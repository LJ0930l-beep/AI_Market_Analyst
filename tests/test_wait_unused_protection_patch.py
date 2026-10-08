"""Verify the next-freeze WAIT projection without changing frozen source."""
import ast
import copy
from copy import deepcopy

import pytest

from core.replay.ai_template_runner import frozen_source_fingerprint
from core.trading.model_schemas import AI_ACTION_SCHEMA, validate_schema
from core.trading.model_schemas import pin_gate_repair_trade_fields
from tests.active_gemini_sources import active_sources as proposed_sources


def proposed_normalizer():
    _, sources = proposed_sources()
    tree = ast.parse(sources['core/trading/model_schemas.py'])
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == 'normalize_wait_unused_protection')
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 '<prepared-wait-projection>', 'exec'), namespace)
    return namespace[node.name]


def test_wait_zero_unused_protection_has_explicit_delta_and_preserves_every_other_field():
    decoded = {'action': 'WAIT', 'instrument_id': 'BTCUSDT', 'stop_price': 0,
               'take_profit': 0.0, 'reason': 'Wait for volume', 'next_trigger_price': 100,
               'entry_price': None, 'quantity': None, 'requested_leverage': 1,
               'evidence_refs': ['fixture'], 'strategy_analysis': {'missing_conditions': ['volume']}}
    original = deepcopy(decoded)
    proof = proposed_normalizer()(decoded)
    assert decoded == {**original, 'stop_price': None, 'take_profit': None}
    assert proof['original_values'] == {'stop_price': 0, 'take_profit': 0.0}
    assert not proof['action_changed'] and not proof['execution_prices_generated']
    assert proposed_normalizer()(decoded) is None


@pytest.mark.parametrize('action', ['OPEN_LONG', 'OPEN_SHORT', 'HOLD', 'CLOSE', 'UPDATE_PROTECTION'])
def test_execution_and_other_actions_are_never_projected(action):
    decoded = {'action': action, 'stop_price': 0, 'take_profit': 0}
    before = deepcopy(decoded)
    assert proposed_normalizer()(decoded) is None
    assert decoded == before


@pytest.mark.parametrize('value', [False, '0', -1, 10, None])
def test_wait_nonzero_or_non_numeric_prices_are_unchanged(value):
    decoded = {'action': 'WAIT', 'stop_price': value, 'take_profit': value}
    before = deepcopy(decoded)
    assert proposed_normalizer()(decoded) is None
    assert decoded == before


def test_compiled_patch_has_both_coordinator_audit_points_and_keeps_live_freeze():
    before = frozen_source_fingerprint()
    _, sources = proposed_sources()
    for source in sources.values():
        ast.parse(source)
    coordinator = sources['core/trading/ai_session_coordinator.py']
    for variable in ('candidate_decoded', 'decoded'):
        assert 'projection = normalize_wait_unused_protection('+variable+')' in coordinator
    assert coordinator.count('normalize_wait_unused_protection(') == 2
    assert before == frozen_source_fingerprint()


@pytest.mark.parametrize('action', ['OPEN_LONG', 'OPEN_SHORT'])
def test_actual_repair_block_preserves_nested_base_contract_and_future_wait(action):
    original, proposed = proposed_sources()
    def run(source, legacy_shallow_fixture=False):
        tree = ast.parse(source)
        # Select the real first repair block, including its native constraints.
        method = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and any(isinstance(c, ast.Name) and c.id == 'gate_repair_fields' for c in ast.walk(n)))
        block = next(n for n in ast.walk(method) if isinstance(n, ast.If)
                     and ast.unparse(n.test) == 'proposed_action in ALLOWED_AI_ACTIONS')
        assignments = [n for n in ast.walk(method) if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'repair_schema' for t in n.targets)
                       and n.lineno < block.lineno]
        assignment = max(assignments, key=lambda n: n.lineno)
        if legacy_shallow_fixture:
            assignment.value = ast.parse('{**AI_ACTION_SCHEMA, "required":list(AI_ACTION_SCHEMA.get("required") or ()), "properties":dict(AI_ACTION_SCHEMA.get("properties") or {})}',mode='eval').body
        base = deepcopy(AI_ACTION_SCHEMA)
        before = deepcopy(base)
        namespace = {'AI_ACTION_SCHEMA': base, 'copy': copy, 'proposed_action': action,
                     'ALLOWED_AI_ACTIONS': {action}, 'candidate_instrument': 'BTCUSDT',
                     'nofx_gate': True, 'previous_decision': {'action': action, 'instrument_id': 'BTCUSDT'},
                     'gate_repair_fields': {'entry_price', 'stop_price', 'take_profit'},
                     'pin_gate_repair_trade_fields': pin_gate_repair_trade_fields}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[assignment, block], type_ignores=[])),
                     '<actual-repair-schema-block>', 'exec'), namespace)
        return before, base, namespace['repair_schema']
    before, polluted, _ = run(original['core/trading/ai_session_coordinator.py'], legacy_shallow_fixture=True)
    assert polluted != before  # Explicit fixture of the archived shallow-copy defect.
    assert polluted['properties']['stop_price']['type'] == 'number'
    before, after, repair = run(proposed['core/trading/ai_session_coordinator.py'])
    assert after == before
    assert after['properties']['stop_price']['type'] == ['number', 'null']
    assert repair['properties']['stop_price']['type'] == 'number'
    assert repair['properties']['stop_price']['exclusiveMinimum'] == 0
    assert repair['properties']['action']['enum'] == [action]
