"""Proposed transport metadata and isolated loopback HTTP fixtures; no relay calls."""
import ast
from copy import deepcopy
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from core.ai.transport_diagnostics import CompletionTransportTrace, safe_transport_trace
from core.model_routing import DEFAULT_MODEL
import core.model_client as original_client
import core.ai.ollama as original_provider
import core.trading.ai_session_coordinator as original_coordinator
from tests.active_gemini_sources import active_sources as proposed_sources


def trace():
    return CompletionTransportTrace([{'role':'user','content':'fixture'}],1,5)


def test_trace_is_bounded_metadata_with_monotonic_phase_timings_and_no_prompt():
    times = iter([1,2,3,4,5])
    obj = CompletionTransportTrace([{'role':'user','content':'secret fixture'}],1,10,clock=lambda:next(times))
    obj.awaiting_headers()
    obj.headers_received(200)
    obj.body_received(20)
    obj.completed()
    result = obj.snapshot()
    assert result['headers_wait_ms']==1000 and result['body_read_ms']==1000 and result['elapsed_ms']==4000
    assert result['response_bytes']==20 and result['phase']=='COMPLETED'
    assert 'secret fixture' not in json.dumps(result)


def test_header_and_body_timeout_are_distinguishable_without_claiming_an_upstream_cause():
    first, second = trace(), trace()
    first.awaiting_headers()
    first.failed(TimeoutError('private error text'))
    second.awaiting_headers()
    second.headers_received(200)
    second.failed(TimeoutError('private error text'))
    assert first.snapshot()['failed_phase']=='AWAITING_RESPONSE_HEADERS'
    assert second.snapshot()['failed_phase']=='READING_RESPONSE_BODY'
    assert first.snapshot()['correlation_id'] != second.snapshot()['correlation_id']
    assert 'private error text' not in json.dumps(second.snapshot())


def test_safe_trace_drops_arbitrary_headers_response_account_and_nonfinite_fields():
    original = trace().snapshot()
    raw = {**original,'Authorization':'Bearer fixture-secret','response_body':'fixture-secret',
           'account_email':'fixture-account','elapsed_ms':float('nan'),'response_bytes':True,
           'error_type':'private-error-text','phase':'fixture-secret'}
    result = safe_transport_trace(raw)
    assert 'fixture-secret' not in json.dumps(result) and 'fixture-account' not in json.dumps(result)
    assert 'elapsed_ms' not in result and 'response_bytes' not in result and 'phase' not in result
    assert result['error_type']=='OTHER_ERROR'


def test_malformed_metadata_cannot_crash_the_bounded_audit_or_smuggle_objects():
    raw={**trace().snapshot(),'phase':[], 'failed_phase':{}, 'error_type':[], 'elapsed_ms':10**1000}
    result=safe_transport_trace(raw)
    assert not {'phase','failed_phase','error_type','elapsed_ms'} & set(result)


@pytest.mark.parametrize('key,value',[('version','unknown'),('correlation_id','not-hash'),('wire_messages_sha256','not-hash')])
def test_unbound_trace_is_not_accepted_into_a_model_audit(key,value):
    raw=trace().snapshot()
    raw[key]=value
    assert safe_transport_trace(raw) is None


def compile_node(source,name,module,extras=None):
    tree=ast.parse(source)
    node=next(n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name==name)
    namespace=dict(vars(module))
    namespace.update(extras or {})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])), '<proposed-transport>', 'exec'),namespace)
    return namespace[name], namespace


def client_for(fake_open):
    _,sources=proposed_sources()
    cfg=SimpleNamespace(api_key='fixture-only',get_preset=lambda _:SimpleNamespace(max_tokens=512,temperature=.2,top_p=.9))
    cls,namespace=compile_node(sources['core/model_client.py'],'ModelClient',original_client,
        {'CompletionTransportTrace':CompletionTransportTrace,'config':cfg,'urlopen':fake_open})
    return cls(base_url='http://127.0.0.1:8045/v1',model_name=DEFAULT_MODEL,timeout_sec=5,retries=0)


class Response(io.BytesIO):
    status=200


@pytest.mark.parametrize('phase',['headers','body'])
def test_proposed_client_timeout_keeps_request_policy_and_binds_error_to_correct_stage(phase):
    requests=[]
    class BodyTimeout(Response):
        def read(self,*_): raise TimeoutError('fixture body timeout')
    def fake_open(request,*,timeout):
        requests.append(request)
        assert timeout==5
        if phase=='headers': raise TimeoutError('fixture header timeout')
        return BodyTimeout(b'')
    client=client_for(fake_open)
    with pytest.raises(original_client.ModelTimeoutError) as failure:
        client.chat_completion([{'role':'user','content':'fixture'}],model_name=DEFAULT_MODEL,retries=0,timeout_sec=5)
    proof=failure.value.transport_trace
    assert proof['failed_phase']==('AWAITING_RESPONSE_HEADERS' if phase=='headers' else 'READING_RESPONSE_BODY')
    assert len(requests)==1 and proof['attempt']==1 and proof['configured_timeout_seconds']==5
    assert requests[0].get_header('X-ai-correlation-id')==proof['correlation_id']
    assert 'fixture body timeout' not in json.dumps(proof)


def test_proposed_success_has_same_payload_and_model_identity_plus_private_content_free_trace():
    seen=[]
    body={'model':DEFAULT_MODEL,'choices':[{'message':{'content':'{"action":"WAIT"}'}}]}
    def fake_open(request,*,timeout):
        seen.append(json.loads(request.data))
        return Response(json.dumps(body).encode())
    client=client_for(fake_open)
    messages=[{'role':'user','content':'fixture'}]
    before=deepcopy(messages)
    response=client.chat_completion(messages,model_name=DEFAULT_MODEL,max_tokens=1024,reasoning_effort='high')
    assert messages==before and seen[0]['messages']==before
    assert seen[0]['model']==DEFAULT_MODEL and seen[0]['max_tokens']==1024
    assert seen[0]['reasoning_effort']=='high' and seen[0]['stream'] is False
    assert response['model']==client.last_response_model==DEFAULT_MODEL
    proof=client.last_transport_trace
    assert proof['phase']=='COMPLETED' and proof['response_bytes']==len(json.dumps(body).encode())


def test_http_rejection_is_not_mislabeled_as_a_missing_response_header():
    def fake_open(request,**_):
        raise HTTPError(request.full_url,401,'fixture refusal',{},io.BytesIO(b'{}'))
    client=client_for(fake_open)
    with pytest.raises(original_client.ModelClientError,match='MODEL_UPSTREAM_HTTP_401'):
        client.chat_completion([{'role':'user','content':'fixture'}],model_name=DEFAULT_MODEL,retries=0)
    result=client.last_transport_trace
    assert result['http_status']==401 and result['failed_phase']=='HTTP_STATUS_REJECTED'


def test_provider_success_and_request_audit_keep_transport_proof_without_altering_model_output():
    _,sources=proposed_sources()
    body={'model':DEFAULT_MODEL,'choices':[{'index':0,'delta':{'content':'{"action":"WAIT"}'},'finish_reason':'stop'}]}
    class StreamResponse(Response):
        headers={'Content-Type':'text/event-stream'}
    client=client_for(lambda *_,**__:StreamResponse(b'data: '+json.dumps(body).encode()+b'\n\ndata: [DONE]\n\n'))
    cls,_=compile_node(sources['core/ai/ollama.py'],'OllamaProvider',original_provider,{'model_client':client})
    provider=cls(base_url=client.base_url,model_name=DEFAULT_MODEL,retries=0)
    provider.health=lambda **_: {'available':True,'model_available':True,'actual_model_id':DEFAULT_MODEL}
    result=provider.generate_json([{'role':'user','content':'fixture'}],model_name=DEFAULT_MODEL,
        prompt_version='fixture',input_hash='a'*64,allow_syntax_repair=False)
    assert result[0]=={'action':'WAIT'} and result[2]['parse_status']=='valid'
    assert result[2]['input_hash']=='a'*64 and result[2]['model_identity_source']=='completion_response'
    append,_=compile_node(sources['core/trading/ai_session_coordinator.py'],
        '_append_model_response_audit',original_coordinator)
    context=SimpleNamespace(model_inference_settings={},model_raw_response=None)
    row=append(context,prompt_version='fixture',request_hash='a'*64,result=result)
    assert row['transport_trace']['phase']=='COMPLETED' and row['status']=='COMPLETED'


def test_provider_error_and_model_audit_preserve_sanitized_transport_provenance():
    _,sources=proposed_sources()
    def fake_open(*_,**__): raise TimeoutError('fixture timeout')
    client=client_for(fake_open)
    cls,_=compile_node(sources['core/ai/ollama.py'],'OllamaProvider',original_provider,{'model_client':client})
    provider=cls(base_url=client.base_url,model_name=DEFAULT_MODEL,retries=0)
    provider.health=lambda **_: {'available':True,'model_available':True,'actual_model_id':DEFAULT_MODEL}
    with pytest.raises(original_provider.LLMError) as failure:
        provider.generate_json([{'role':'user','content':'fixture'}],model_name=DEFAULT_MODEL,
            prompt_version='fixture',input_hash='a'*64,allow_syntax_repair=False)
    append,_=compile_node(sources['core/trading/ai_session_coordinator.py'],
        '_append_model_response_audit',original_coordinator)
    context=SimpleNamespace(model_inference_settings={},model_raw_response=None)
    result=append(context,prompt_version='fixture',request_hash='a'*64,error=failure.value)
    assert result['status']=='PROVIDER_ERROR' and result['transport_trace']['failed_phase']=='AWAITING_RESPONSE_HEADERS'
    assert result['transport_trace']['correlation_id']==failure.value.transport_trace['correlation_id']


def test_combined_patch_freezes_both_new_dependencies_and_leaves_running_source_unchanged():
    from core.replay.ai_template_runner import frozen_source_fingerprint
    before=frozen_source_fingerprint()
    original,sources=proposed_sources()
    for name,source in sources.items():
        ast.parse(source)
    assert 'historical_settled_funding' in sources['core/trading/ai_session_coordinator.py']
    assert '"core/ai/transport_diagnostics.py"' in sources['core/replay/ai_template_runner.py']
    assert '"core/replay/settled_funding.py"' in sources['core/replay/ai_template_runner.py']
    assert before==frozen_source_fingerprint()


@pytest.mark.parametrize('scenario', ['headers_timeout', 'body_timeout', 'success'])
def test_proposed_trace_matches_real_urllib_loopback_transport(scenario):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import time
    from urllib.request import Request, ProxyHandler, build_opener
    recorded = []
    release = threading.Event()
    payload = json.dumps({'model':DEFAULT_MODEL,
        'choices':[{'message':{'content':'{"action":"WAIT"}'}}]}).encode()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            recorded.append({'id':self.headers.get('X-AI-Correlation-ID'), 'body':json.loads(body)})
            if scenario == 'headers_timeout':
                release.wait(2)
            self.send_response(200)
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.flush()
            if scenario == 'body_timeout':
                release.wait(2)
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    thread.start()
    # Redirect only this isolated fixture's opener. The compiled production
    # route guard remains unchanged and no request reaches port 8045.
    opener = build_opener(ProxyHandler({}))
    def fixture_open(request, *, timeout):
        return opener.open(Request(f'http://127.0.0.1:{server.server_port}/fixture',
            data=request.data,headers=dict(request.header_items()),method='POST'),timeout=timeout)
    client = client_for(fixture_open)
    try:
        if scenario == 'success':
            result = client.chat_completion([{'role':'user','content':'fixture'}],
                model_name=DEFAULT_MODEL,retries=0,timeout_sec=.3)
            assert result['model'] == DEFAULT_MODEL
            proof = client.last_transport_trace
            assert proof['phase'] == 'COMPLETED' and proof['response_bytes'] == len(payload)
        else:
            with pytest.raises(original_client.ModelTimeoutError) as failure:
                client.chat_completion([{'role':'user','content':'fixture'}],
                    model_name=DEFAULT_MODEL,retries=0,timeout_sec=.3)
            proof = failure.value.transport_trace
            expected = 'AWAITING_RESPONSE_HEADERS' if scenario == 'headers_timeout' else 'READING_RESPONSE_BODY'
            assert proof['failed_phase'] == expected
            if scenario == 'body_timeout':
                assert proof['http_status'] == 200
        assert len(recorded) == 1 and recorded[0]['id'] == proof['correlation_id']
        assert proof['configured_timeout_seconds'] == .3 and proof['attempt'] == 1
        assert recorded[0]['body']['messages'] == [{'role':'user','content':'fixture'}]
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
