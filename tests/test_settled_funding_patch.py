"""Exercise proposed functions in memory; frozen source is never overwritten."""
import ast
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

import core.replay.ai_template_runner as runner
import core.trading.ai_session_coordinator as coordinator
from tests.active_gemini_sources import active_sources as proposed_sources
from scripts.prepare_settled_funding_patch import replace_once


def proposed_function(source, name, original_module):
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = dict(vars(original_module))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])), '<offline-proposed-funding>', 'exec'), namespace)
    return namespace[name]


def test_research_quote_and_compaction_preserve_exact_venue_and_time_labeled_funding_without_modifying_source():
    before = runner.frozen_source_fingerprint()
    original, changed = proposed_sources()
    market = {'price':100,'symbol':'BTCUSDT','market':{'contractSize':.0001,'leverage_max':200}}
    history = SimpleNamespace(payload={'assumptions':{'data_venue':'binance'},'funding':[
        {'instrument_id':'BTCUSDT','payment_time':'2025-10-15T00:00:00Z', 'rate':-.00005425,
         'source':'binance_official_funding_archive'}]}, market_snapshot=lambda *_: deepcopy(market))
    fn = proposed_function(changed['core/replay/ai_template_runner.py'], '_market_snapshot', runner)
    point = datetime(2025,10,15,0,5,tzinfo=timezone.utc)
    result = fn(history,'BTCUSDT',point)
    compact = proposed_function(changed['core/trading/ai_session_coordinator.py'], '_compact_market_snapshot', coordinator)
    assert compact(result)['historical_settled_funding'] == result['historical_settled_funding']
    rate = result['historical_settled_funding']
    assert rate['venue']=='binance' and rate['rate_fraction']=='-0.00005425'
    assert rate['availability_basis']=='PAYMENT_PLUS_RESEARCH_LAG_ASSUMPTION'
    assert 'fundingRate' not in result
    without_rate = SimpleNamespace(payload={**history.payload,'funding':[]},market_snapshot=history.market_snapshot)
    baseline = fn(without_rate,'BTCUSDT',point)
    assert {k:v for k,v in result.items() if k!='historical_settled_funding'} == {k:v for k,v in baseline.items() if k!='historical_settled_funding'}
    assert before == runner.frozen_source_fingerprint()


def test_budget_compaction_retains_fact_and_fails_if_required_inputs_cannot_fit():
    _, changed = proposed_sources()
    fn = proposed_function(changed['core/trading/ai_session_coordinator.py'], '_fit_prompt_payload', coordinator)
    rate = {'status':'HISTORICAL_SETTLED_RATE','venue':'binance','rate_fraction':'-.00005425',
            'payment_time':'2025-10-15T00:00:00Z','live_or_next_predicted_rate':False}
    payload={'allowed_instruments':['BTCUSDT'],'evidence_refs':[], 'account_truth':{},
        'market_snapshots':{'BTCUSDT':{'symbol':'BTCUSDT','price':100,'historical_settled_funding':rate,
                                     'unrelated_volume_history':'x'*5000}}, 'technical_context':{}}
    original = deepcopy(payload)
    result, metadata = fn(payload, 'system', context_length=8192, reserve=7300, required_symbols=('BTCUSDT',))
    assert result['market_snapshots']['BTCUSDT']['historical_settled_funding'] == rate
    assert 'compact_market_snapshots' in metadata['steps']
    assert payload==original
    with pytest.raises(ValueError,match='AI_INPUT_BUDGET_EXCEEDED'):
        fn(payload, 'x'*15000, context_length=8192, reserve=7300, required_symbols=('BTCUSDT',))


def test_dependency_freeze_and_past_funding_export_are_in_the_patch():
    _, changed = proposed_sources()
    source = changed['core/replay/ai_template_runner.py']
    tree = ast.parse(source)
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target,ast.Name) and target.id=='SOURCE_FILES' for target in node.targets))
    assert 'core/replay/settled_funding.py' in ast.literal_eval(assignment.value)
    assert 'start - timedelta(days=2)' in changed['core/replay/gemini_research.py']
    assert 'PAYMENT_PLUS_60S_RESEARCH_PUBLICATION_LAG_NOT_GATE_CURRENT_RATE' in changed['core/replay/gemini_research.py']


def test_drifted_preimage_cannot_produce_a_silent_partial_patch():
    with pytest.raises(ValueError,match='PREIMAGE_CHANGED'): replace_once('wrong source','anchor','new')
