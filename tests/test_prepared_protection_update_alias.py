from copy import deepcopy

import pytest

from scripts.prepare_protection_update_alias import prepared_function, proposed_sources


def test_actual_model_shape_copies_prices_without_erasing_original_fields():
    proposal={'action':'UPDATE_PROTECTION','instrument_id':'ETHUSDT','position_id':'fixture-position',
              'stop_price':4005.0,'take_profit':3900.0,'new_stop_price':None,'new_take_profit':None}
    old=deepcopy(proposal)
    proof=prepared_function()(proposal)
    assert proposal['new_stop_price']==4005.0 and proposal['new_take_profit']==3900.0
    for key,value in old.items():
        if key not in ('new_stop_price','new_take_profit'): assert proposal[key]==value
    assert proof['execution_prices_generated'] is False
    assert proof['field_sources']['new_stop_price']['source_field']=='stop_price'


@pytest.mark.parametrize('action',['WAIT','HOLD','OPEN_LONG','OPEN_SHORT','CLOSE_POSITION','TIGHTEN_STOP'])
def test_bridge_does_not_apply_to_other_actions(action):
    data={'action':action,'stop_price':100}
    before=deepcopy(data)
    assert prepared_function()(data) is None and data==before


@pytest.mark.parametrize('bad',[0,-1,True,float('nan'),float('inf'),'100'])
def test_invalid_second_leg_is_atomic(bad):
    data={'action':'UPDATE_PROTECTION','stop_price':90,'take_profit':bad}
    before=deepcopy(data)
    with pytest.raises(ValueError,match='PRICE_INVALID'): prepared_function()(data)
    assert set(data)==set(before) and 'new_stop_price' not in data


def test_conflicting_canonical_and_alias_rejected():
    data={'action':'UPDATE_PROTECTION','stop_price':90,'new_stop_price':91}
    before=deepcopy(data)
    with pytest.raises(ValueError,match='FIELDS_CONFLICT'): prepared_function()(data)
    assert data==before


def test_equal_or_canonical_only_is_unchanged():
    for data in [{'action':'UPDATE_PROTECTION','new_stop_price':90},
                 {'action':'UPDATE_PROTECTION','stop_price':90,'new_stop_price':90}]:
        before=deepcopy(data)
        assert prepared_function()(data) is None and data==before


def test_prepared_pipeline_has_both_normalization_sites_no_source_deployment():
    from scripts.prepare_protection_update_alias import ROOT
    old,new=proposed_sources()
    assert new['core/trading/ai_session_coordinator.py'].count('projection = normalize_protection_update_aliases(')==2
    assert all((ROOT/name).read_text(encoding='utf-8')==value for name,value in old.items())


@pytest.mark.parametrize('owned',[True,False])
def test_prepared_prices_reach_engine_gateway_and_gateway_rejection_propagates(owned):
    # Full production engine with isolated gateway: not an exchange acceptance test.
    import runpy
    from pathlib import Path
    fixtures=runpy.run_path(str(Path(__file__).with_name('test_price_action_verified_exit.py')))
    fixture_case,fixture_engine=fixtures['fixture_case'],fixtures['fixture_engine']
    from core.trading.ai_led_engine import AIActionOutput
    from core.trading.execution_gateway import GatewayError
    context,_,now=fixture_case('UPDATE_PROTECTION',price=80)
    engine=fixture_engine(ownership=owned)
    captured=[]
    attempts=[]
    def update(**kwargs):
        attempts.append(kwargs)
        if not owned:
            raise GatewayError('OWNERSHIP','FIXTURE_GATEWAY_POSITION_UNVERIFIED',403)
        captured.append(kwargs)
        return {'status':'FIXTURE_VERIFIED','simulation':True}
    engine.gateway.update_gate_protection=update
    data={'action':'UPDATE_PROTECTION','instrument_id':'BTCUSDT','position_id':'fixture-position',
          'stop_price':95,'take_profit':70,'reason':'fixture structure update'}
    prepared_function()(data)
    result=engine.execute_cycle(context,now=now,model_output=AIActionOutput(**data))
    if owned:
        assert result.status=='EXECUTED',result.reason
        assert captured[0]['new_stop_price']==95 and captured[0]['new_take_profit']==70
        assert captured[0]['position_id']=='fixture-position'
    else:
        assert result.status=='REJECTED' and captured==[]
    assert len(attempts)==1
