"""Actual coordinator repair and an explicit archived-defect fixture; no orders."""
import ast
from copy import deepcopy
import inspect
import json
import sqlite3

import pytest

from core.replay import ai_template_runner as runner
from core.trading import ai_session_coordinator as coordinator
from core.trading.model_schemas import normalize_wait_unused_protection
from tests.test_ai_template_runner import FixtureProvider, history, rows


def legacy_normalizer_fixture():
    source = inspect.getsource(normalize_wait_unused_protection)
    before = '("stop_price", "take_profit", "take_profit_1", "take_profit_2")'
    assert source.count(before) == 1
    source = source.replace(before,'("stop_price", "take_profit")')
    namespace = {}
    exec(compile(ast.parse(source),'<prepared-optional-wait-targets>','exec'),namespace)
    return namespace['normalize_wait_unused_protection']


class OptionalTargetProvider(FixtureProvider):
    def __init__(self,action):
        super().__init__()
        self.action = action

    def generate_json(self,messages,**options):
        output,_,metadata = super().generate_json(messages,**options)
        if self.action == 'WAIT':
            output = {'action':'WAIT','instrument_id':'ETHUSDT','reason':'等待回踩确认',
                'confidence':35,'entry_condition':'等待已收盘确认','next_trigger_price':101,
                'strategy_analysis':{'matched_conditions':[],'missing_conditions':['回踩确认']}}
        else:
            output['action'] = self.action
            if self.action == 'OPEN_SHORT':
                output['stop_price'],output['take_profit'] = 110,80
        output.update(take_profit_1=0.0,take_profit_2=0.0)
        return output,json.dumps(output,ensure_ascii=False),metadata


@pytest.mark.parametrize('prepared',[False,True])
def test_actual_wait_optional_zero_target_failure_and_bounded_projection(tmp_path,monkeypatch,prepared):
    source_before = runner.frozen_source_fingerprint()
    if not prepared:
        monkeypatch.setattr(coordinator,'normalize_wait_unused_protection',legacy_normalizer_fixture())
    path = tmp_path/'wait.sqlite3'
    report = runner.run_ai_template_replay(history(5),db_path=path,model_provider=OptionalTargetProvider('WAIT'),
        priority_guard=lambda:None,max_decisions=1)
    row = rows(path)[0]
    if prepared:
        assert not report['errors'] and row['status'] == 'COMPLETED'
        output = json.loads(row['decision_json'])
        assert output['action'] == 'WAIT' and output['next_trigger_price'] == 101
        context = json.loads(row['context_json'])
        proof = next(x for x in context['model_inference_settings']['model_output_normalizations']
                     if x['normalization'] == 'WAIT_UNUSED_PROTECTION_ZERO_TO_NULL')
        assert proof['original_values'] == {'take_profit_1':0.0,'take_profit_2':0.0}
        assert not proof['action_changed'] and not proof['execution_prices_generated']
        assert json.loads(context['model_raw_response'])['take_profit_1'] == 0.0
        from scripts.verify_ai_template_replay import audit_effective_request
        with sqlite3.connect(path) as db:
            bundles = {key:json.loads(value) for key,value in db.execute('SELECT bundle_id,payload_json FROM evidence_bundles')}
        audit_effective_request(context,bundles[context['evidence_bundle_id']],bundles.__getitem__)
    else:
        assert 'INVALID_TAKE_PROFIT_1' in report['errors'][0]['error']
    assert all(item['fills'] == 0 for item in report['results'])
    assert runner.frozen_source_fingerprint() == source_before


@pytest.mark.parametrize('action',['OPEN_LONG','OPEN_SHORT'])
def test_prepared_wait_projection_never_relaxes_actual_open_target_validation(tmp_path,monkeypatch,action):
    report = runner.run_ai_template_replay(history(5),db_path=tmp_path/'open.sqlite3',
        model_provider=OptionalTargetProvider(action),priority_guard=lambda:None,max_decisions=1)
    assert 'INVALID_TAKE_PROFIT_1' in report['errors'][0]['error']
    assert all(item['fills'] == 0 for item in report['results'])
