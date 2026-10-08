"""Every-scan transport/request coverage, fixtures never count as real calls."""
from copy import deepcopy
import hashlib
import json

import pytest

from core.model_routing import DEFAULT_MODEL
from scripts.audit_gemini_full_execution import audit_model_attempts, schedule


def test_registered_budget_rejects_receipts_from_other_adapter_capacity():
    from scripts.audit_gemini_full_execution import audit_registered_input_budget
    audit_registered_input_budget({'model_inference_settings':{'context_length':12288}},
        {'application_input_budget':12288})
    for actual in (8192, None, True, '12288'):
        with pytest.raises(ValueError, match='REGISTERED_INPUT_BUDGET_CHANGED'):
            audit_registered_input_budget({'model_inference_settings':{'context_length':actual}},
                {'application_input_budget':12288})


def fixture():
    messages = [{'role':'system','content':'synthetic test only'}, {'role':'user','content':'中文输入'}]
    request_hash = hashlib.sha256(json.dumps({'messages':messages},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    raw = '{"action":"WAIT"}'
    trace = {'version':'gemini_transport_trace_v1','correlation_id':'a'*32,'wire_messages_sha256':request_hash,
        'phase':'COMPLETED','http_status':200,'transport_mode':'SSE','stream_done':True,
        'stream_finish_reason':'stop','stream_event_count':4,'elapsed_ms':100,
        'first_stream_event_ms':10,'first_content_ms':20}
    attempt = {'status':'COMPLETED','request_hash':request_hash,'raw_response':raw,
        'raw_response_chars':len(raw),'raw_response_truncated':False,'transport_trace':trace}
    context = {'model_id':DEFAULT_MODEL,'model_version':DEFAULT_MODEL,'model_call_completed':True,
        'model_inference_settings':{'model_response_audit':{'attempts':[attempt]}}}
    return context,{request_hash:{'messages':messages}},attempt


def test_real_utf8_request_hash_and_every_original_repair_call_are_independently_bound():
    context,requests,original = fixture()
    repair = deepcopy(original);repair['transport_trace']['correlation_id']='b'*32
    context['model_inference_settings']['model_response_audit']['attempts'].append(repair)
    before = deepcopy((context,requests))
    seen=set();proofs=audit_model_attempts(context,requests,seen)
    assert len(proofs)==2 and len(seen)==2
    assert (context,requests)==before
    assert proofs[0]['raw_response_sha256']==hashlib.sha256(original['raw_response'].encode()).hexdigest()


@pytest.mark.parametrize('defect',['no_attempts','no_completed','model','version','http','failed_phase',
    'non_sse','not_done','finish_reason','event_count','timing','missing_request','wire_hash',
    'changed_messages','truncated_raw','raw_count','raw_missing','reused_correlation'])
def test_any_unverifiable_attempt_prevents_full_protocol_pass(defect):
    context,requests,attempt=fixture();trace=attempt['transport_trace'];seen=set()
    if defect=='no_attempts': context['model_inference_settings']['model_response_audit']['attempts']=[]
    elif defect=='no_completed': context['model_call_completed']=False
    elif defect=='model': context['model_id']='other'
    elif defect=='version': context['model_version']='other'
    elif defect=='http': trace['http_status']=500
    elif defect=='failed_phase': trace['failed_phase']='READING_RESPONSE_BODY'
    elif defect=='non_sse': trace['transport_mode']='JSON'
    elif defect=='not_done': trace['stream_done']=False
    elif defect=='finish_reason': trace['stream_finish_reason']='length'
    elif defect=='event_count': trace['stream_event_count']=False
    elif defect=='timing': trace['first_content_ms']=101
    elif defect=='missing_request': requests={}
    elif defect=='wire_hash': trace['wire_messages_sha256']='b'*64
    elif defect=='changed_messages': requests[attempt['request_hash']]['messages'][0]['content']='changed'
    elif defect=='truncated_raw': attempt['raw_response_truncated']=True
    elif defect=='raw_count': attempt['raw_response_chars']-=1
    elif defect=='raw_missing': attempt['raw_response']=''
    else: seen.add(trace['correlation_id'])
    with pytest.raises(ValueError): audit_model_attempts(context,requests,seen)


def test_native_calendar_counts_include_each_template_and_every_half_open_window_scan():
    window={'start':'2025-10-15T00:00:00Z','end':'2025-10-17T00:00:00Z'}
    pa={'template_id':'price_action_structure','execution':{'scan_interval_minutes':15}}
    rows=schedule(window,[pa])
    assert len(rows)==192 and rows[0]==('price_action_structure','2025-10-15T00:00:00+00:00')
    assert rows[-1][1]=='2025-10-16T23:45:00+00:00'
    other={'template_id':'synthetic','execution':{'scan_interval_minutes':5}}
    assert len(schedule(window,[pa,other]))==192+576


@pytest.mark.parametrize('interval',[True,0,-5,7,15.0])
def test_invalid_cadence_cannot_silently_change_the_expected_denominator(interval):
    with pytest.raises(ValueError,match='INTERVAL_INVALID'):
        schedule({'start':'2025-10-15T00:00:00Z','end':'2025-10-17T00:00:00Z'},[
            {'template_id':'synthetic','execution':{'scan_interval_minutes':interval}}])
