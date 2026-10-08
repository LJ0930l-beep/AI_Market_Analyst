"""Isolated coordinator regression; no real market or trading acceptance claims."""
from copy import deepcopy
import json

import pytest

from core.replay import ai_template_runner as runner
from core.trading.model_schemas import AI_ACTION_SCHEMA
from tests.test_ai_template_runner import FixtureProvider, history, rows


class WaitTextProvider(FixtureProvider):
    def __init__(self, mutation=None):
        super().__init__()
        self.mutation = mutation
        self.initial = None

    def generate_json(self, messages, **options):
        payload = json.loads(messages[1]['content'])
        self.payloads.append(payload)
        if 'previous_decision' in payload:
            assert payload['previous_decision'] == self.initial
            assert payload['mutable_fields'] == ['timeframe_analysis.5m']
            assert options['schema']['properties']['action']['enum'] == ['WAIT']
            assert payload['inputs']['account_truth'] == self.payloads[0]['account_truth']
            assert payload['inputs']['market_snapshots'] == self.payloads[0]['market_snapshots']
            decision = deepcopy(self.initial)
            decision['timeframe_analysis']['5m'] = '等待已收盘回踩确认，不重复分析原轮行情。'
            if self.mutation == 'trigger':
                decision['next_trigger_price'] = 102
            elif self.mutation == 'condition':
                decision['strategy_analysis']['missing_conditions'] = ['改动原缺失条件']
            elif self.mutation == 'other_timeframe':
                decision['timeframe_analysis']['15m'] = '重新判断趋势'
            elif self.mutation == 'still_long':
                decision['timeframe_analysis']['5m'] = '原始说明' * 300
        else:
            decision = {'action':'WAIT','instrument_id':'ETHUSDT',
                'reason':'等待明确回踩确认','confidence':None,
                'next_trigger_price':101,
                'strategy_analysis':{'missing_conditions':['已收盘回踩确认尚未成立'],'matched_conditions':[]},
                'timeframe_analysis':{'5m':'原始说明' * 300,'15m':'趋势尚待确认'},
                'evidence_refs':[ref for ref in payload['evidence_refs'] if ref.startswith('market_snapshot:')]}
            self.initial = deepcopy(decision)
        raw = json.dumps(decision,ensure_ascii=False)
        self.raw_responses.append(raw)
        return decision,raw,{'model_id':self.model_id,'model_version':self.model_id,
            'actual_model_id':self.model_id,'model_identity_source':'completion_response',
            'verified_manifest_model_id':self.model_id}


@pytest.mark.parametrize('mutation',[None,'trigger','condition','other_timeframe','still_long'])
def test_actual_wait_repair_has_one_call_and_cannot_change_decision(tmp_path,monkeypatch,mutation):
    from core.model_client import model_client
    monkeypatch.setattr(model_client,'count_tokens',lambda content,**kwargs:max(1,len(content)//3))
    schema_before = deepcopy(AI_ACTION_SCHEMA)
    provider = WaitTextProvider(mutation)
    path = tmp_path/'wait.sqlite3'
    report = runner.run_ai_template_replay(history(5),db_path=path,model_provider=provider,
        priority_guard=lambda:None,max_decisions=1)
    assert len(provider.payloads) == 2
    assert AI_ACTION_SCHEMA == schema_before
    row = rows(path)[0]
    context = json.loads(row['context_json'])
    attempts = context['model_inference_settings']['model_response_audit']['attempts']
    assert len(attempts) == 2
    assert context['model_inference_settings']['repair_input_scope']['scope'] == 'WAIT_TEXT_ONLY_NO_MARKET_REEVALUATION'
    if mutation is None:
        assert not report['errors'] and row['status'] == 'COMPLETED'
        output = json.loads(row['decision_json'])
        assert output['action'] == 'WAIT' and output['next_trigger_price'] == 101
        assert output['strategy_analysis'] == provider.initial['strategy_analysis']
        assert output['timeframe_analysis']['15m'] == provider.initial['timeframe_analysis']['15m']
        assert len(output['timeframe_analysis']['5m']) <= 800
    else:
        assert row['status'] == 'ERROR'
        expected = 'timeframe_analysis.5m:length' if mutation == 'still_long' else 'MODEL_REPAIR_CHANGED_WAIT_DECISION'
        assert expected in report['errors'][0]['error']
    assert all(item['fills'] == 0 for item in report['results'])
    assert report['private_exchange_calls'] == 0
