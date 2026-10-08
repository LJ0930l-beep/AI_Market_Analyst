"""PA exit authority, ownership and truthful receipts; no private API calls."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from core.ai.transport_diagnostics import CompletionTransportTrace
from core.model_routing import DEFAULT_SMART_MODEL
from core.trading.ai_led_engine import (
    AICycleContext, AIActionOutput, AILedDecisionEngine, _verified_price_action_exit,
)
from core.trading.autonomous_strategy import CONTRACT
from core.trading.execution_gateway import GatewayError, TradingMode


def fixture_case(action='CLOSE_POSITION', price=100.2, *, mode=TradingMode.LIVE):
    now = datetime(2026, 10, 15, 5, 15, tzinfo=timezone.utc)
    position = {'position_id': 'fixture-position', 'symbol': 'BTCUSDT', 'side': 'SHORT',
                'contracts': 10, 'entry_price': 100, 'mark_price': price,
                'opened_at': (now-timedelta(minutes=45)).isoformat()}
    context = AICycleContext(cycle_id='fixture-cycle', account_id='fixture-account', generation=1,
        started_at=now.isoformat(), expires_at=(now+timedelta(seconds=20)).isoformat(),
        allowed_instruments=('BTCUSDT',), mode=mode, venue='gate', decision_contract=CONTRACT,
        strategy_instructions={'template_id': 'price_action_structure', 'profile': {}},
        account_truth={'status': 'AVAILABLE', 'source': 'isolated-fixture', 'positions': [position]},
        market_snapshots={'BTCUSDT': {'price': price, 'data_as_of': now.isoformat()}},
        model_id=DEFAULT_SMART_MODEL, model_version=DEFAULT_SMART_MODEL,
        model_call_attempted=True, model_call_completed=True,
        model_call_prompt_version='fixture-prompt', input_hash='a'*64)
    output = AIActionOutput(action=action, instrument_id='BTCUSDT', position_id='fixture-position',
                            reason='structure invalidation fixture', reduce_fraction=.5 if action=='REDUCE_POSITION' else None)
    raw = json.dumps({'action': action, 'instrument_id': output.instrument_id,
                     'position_id': output.position_id, 'reason': output.reason,
                     'reduce_fraction': output.reduce_fraction})
    context.model_raw_response = raw
    trace = CompletionTransportTrace([{'role': 'user', 'content': 'fixture'}], 1, 30)
    trace.data.update(http_status=200, transport_mode='SSE', stream_done=True, stream_finish_reason='stop')
    trace.completed()
    context.model_inference_settings = {'actual_model_id': DEFAULT_SMART_MODEL,
        'model_identity_source': 'completion_response', 'verified_manifest_model_id': DEFAULT_SMART_MODEL,
        'request_hash': context.input_hash, 'model_response_audit': {'attempts': [{
            'status': 'COMPLETED', 'validation_error': None, 'request_hash': context.input_hash,
            'prompt_version': context.model_call_prompt_version, 'raw_response': raw,
            'raw_response_chars': len(raw), 'raw_response_truncated': False,
            'transport_trace': trace.snapshot()}]}}
    return context, output, now


def fixture_engine(*, status='FILLED', ownership=True, gateway_reject=False):
    class Gateway:
        def __init__(self):
            self.intents = []
            self.ownership_checks = 0

        def _gate_remote_position_ownership_evidence(self, **kwargs):
            self.ownership_checks += 1
            if not ownership:
                raise GatewayError('OWNERSHIP', 'GATE_SYSTEM_POSITION_OWNERSHIP_UNVERIFIED:FIXTURE', 403)
            return {'status': 'VERIFIED', 'position_id': kwargs['position']['position_id']}

        def submit_intent(self, intent, **kwargs):
            self.intents.append(intent)
            if gateway_reject:
                raise GatewayError('AUTHORIZATION', 'FIXTURE_AUTHORIZATION_DENIED', 403)
            return {'status': status, 'order_id': 'fixture-order'}

    engine = object.__new__(AILedDecisionEngine)
    engine.gateway = Gateway()
    engine.store = SimpleNamespace()
    engine.ledger = SimpleNamespace(get_open_positions=lambda *a, **k: [])
    engine.agent_policy_id = 'fixture-policy'
    engine._persist_cycle = lambda *_: None
    engine._live_execution_quote = lambda *a, **k: None
    return engine


@pytest.mark.parametrize('action', ['CLOSE_POSITION', 'REDUCE_POSITION'])
@pytest.mark.parametrize('price', [100.2, 99.8])
@pytest.mark.parametrize('mode', [TradingMode.LIVE, TradingMode.TESTNET])
def test_verified_pa_risk_reduction_reaches_reduce_only_gateway_at_45_minutes(action, price, mode):
    context, output, now = fixture_case(action, price, mode=mode)
    engine = fixture_engine()
    result = engine.execute_cycle(context, now=now, model_output=output)
    assert result.status == 'EXECUTED', result.reason
    intent = engine.gateway.intents[0]
    assert engine.gateway.ownership_checks == 1
    assert intent.reduce_only is True and intent.side == 'BUY'
    assert intent.quantity == (10 if action=='CLOSE_POSITION' else 5)
    assert intent.position_id == 'fixture-position' and intent.account_id == 'fixture-account'
    assert output.extra_fields['exit_authority']['legacy_age_and_price_pnl_gate_applied'] is False


@pytest.mark.parametrize('mutation', ['incomplete', 'wrong_model', 'input_hash', 'partial_sse',
                                    'response_changed', 'instrument_changed', 'position_changed', 'missing_audit'])
def test_unverified_or_changed_model_exit_never_gets_the_time_gate_exception(mutation):
    context, output, now = fixture_case()
    attempt = context.model_inference_settings['model_response_audit']['attempts'][-1]
    if mutation=='incomplete': context.model_call_completed=False
    elif mutation=='wrong_model': context.model_inference_settings['actual_model_id']='gemini-3.8-flash-low'
    elif mutation=='input_hash': context.model_inference_settings['request_hash']='b'*64
    elif mutation=='partial_sse': attempt['transport_trace']['stream_done']=False
    elif mutation=='response_changed': attempt['raw_response']='{}'
    elif mutation=='instrument_changed': output.instrument_id='ETHUSDT'
    elif mutation=='position_changed': output.position_id='other-position'
    elif mutation=='missing_audit': context.model_inference_settings.pop('model_response_audit')
    engine=fixture_engine()
    result=engine.execute_cycle(context, now=now, model_output=output)
    assert result.status in {'BLOCKED','REJECTED'}
    assert engine.gateway.intents == []
    assert not _verified_price_action_exit(context, output)


def test_other_strategies_keep_existing_hold_throttle():
    context, output, now=fixture_case()
    context.strategy_instructions['template_id']='other-strategy'
    result=fixture_engine().execute_cycle(context,now=now,model_output=output)
    assert result.status=='BLOCKED' and result.reason.startswith('THROTTLE_MIN_HOLD_ACTIVE')


@pytest.mark.parametrize('scope', ['ownership','expiration','authorization','fraction'])
def test_pa_time_gate_exception_does_not_bypass_existing_hard_checks(scope):
    context, output, now=fixture_case('REDUCE_POSITION' if scope=='fraction' else 'CLOSE_POSITION')
    engine=fixture_engine(ownership=scope!='ownership',gateway_reject=scope=='authorization')
    if scope=='expiration': context.expires_at=(now-timedelta(seconds=1)).isoformat()
    if scope=='fraction': output.reduce_fraction=1.5
    result=engine.execute_cycle(context,now=now,model_output=output)
    assert result.status in {'REJECTED','TIMEOUT_DISCARDED'}
    if scope!='authorization': assert engine.gateway.intents==[]


@pytest.mark.parametrize('gateway_status,expected', [('ACKNOWLEDGED','SUBMITTED'),
    ('FILLED','EXECUTED'),('PARTIALLY_FILLED','EXECUTED'),('REJECTED','REJECTED'),('UNKNOWN','REJECTED')])
def test_exit_acceptance_is_not_reported_as_a_fill(gateway_status,expected):
    context,output,now=fixture_case()
    result=fixture_engine(status=gateway_status).execute_cycle(context,now=now,model_output=output)
    assert result.status==expected


def test_non_exit_output_cannot_acquire_exit_authority():
    context,output,_=fixture_case()
    output.action='OPEN_LONG'
    assert not _verified_price_action_exit(context,output)
