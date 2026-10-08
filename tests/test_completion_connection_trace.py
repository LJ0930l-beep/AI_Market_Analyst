"""Real isolated HTTP transport only; never relay8045 or a trading account."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import Request
from urllib.error import URLError
import http.client
import json
import ssl
import threading

import pytest

from core.ai.transport_diagnostics import (CompletionTransportTrace,open_traced_request,
    safe_transport_trace,_TracedHTTPConnection,_TracedHTTPSConnection)


def trace():return CompletionTransportTrace([{'role':'user','content':'isolated fixture'}],1,.15)


@pytest.mark.parametrize('scenario',['success','headers_timeout','body_timeout','proxy_route'])
def test_real_socket_phases_and_unchanged_wire(scenario,monkeypatch):
    release=threading.Event();received=[]
    request_body=json.dumps({'messages':[{'role':'user','content':'中文 fixture'}],
        'stream':True,'model':'gemini-3.8-flash-high'},ensure_ascii=False).encode()
    response_body=b'{"action":"WAIT"}'
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*_):pass
        def do_POST(self):
            received.append((self.path,self.rfile.read(int(self.headers['Content-Length'])),
                             self.headers.get('Authorization')))
            if scenario=='headers_timeout':release.wait(2)
            try:
                self.send_response(200);self.send_header('Content-Length',str(len(response_body)))
                self.end_headers();self.wfile.flush()
                if scenario=='body_timeout':release.wait(2)
                self.wfile.write(response_body)
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    worker.start();actual=trace()
    endpoint=f'http://127.0.0.1:{server.server_port}/fixture'
    if scenario=='proxy_route':
        monkeypatch.setattr('urllib.request.getproxies',lambda:{'http':f'http://127.0.0.1:{server.server_port}'})
        monkeypatch.setattr('urllib.request.proxy_bypass',lambda _:False)
        endpoint='http://127.0.0.1:42/v1/chat/completions'
    req=Request(endpoint,data=request_body,headers={'Content-Type':'application/json',
        'Authorization':'Bearer fixture-private-secret','X-AI-Correlation-ID':actual.data['correlation_id']},method='POST')
    try:
        try:
            with open_traced_request(req,timeout=.15,trace=actual) as response:
                actual.headers_received(response.status)
                result=response.read();actual.body_received(len(result));actual.completed()
        except (TimeoutError,URLError) as failure:
            actual.failed(failure)
        proof=actual.snapshot()
        assert proof['connection_detail_available'] is True
        assert proof['connection_established'] is True
        assert proof['peer_is_loopback'] is True
        assert proof['connected_peer_port']==server.server_port
        assert proof['request_bytes_written']>len(request_body)
        assert proof['connect_ms']>=0 and proof['request_write_ms']>=0
        assert len(received)==1 and received[0][1]==request_body
        assert received[0][2]=='Bearer fixture-private-secret'
        if scenario=='headers_timeout':assert proof['failed_phase']=='AWAITING_RESPONSE_HEADERS'
        elif scenario=='body_timeout':assert proof['failed_phase']=='READING_RESPONSE_BODY'
        else:
            assert proof['phase']=='COMPLETED' and result==response_body
            assert proof['headers_wait_ms']>=0
        if scenario=='proxy_route':assert received[0][0]==endpoint
        exported=json.dumps(proof)
        for secret in ('fixture-private-secret','Authorization','中文','request_body','peer_address'):
            assert secret not in exported
    finally:
        release.set();server.shutdown();server.server_close();worker.join(2)
        assert not worker.is_alive()


def test_connect_timeout_is_not_reported_as_headers_wait(monkeypatch):
    actual=trace();connection=_TracedHTTPConnection('127.0.0.1',transport_trace=actual)
    def fail(_):raise TimeoutError('sensitive fixture text')
    monkeypatch.setattr(http.client.HTTPConnection,'connect',fail)
    with pytest.raises(TimeoutError) as error:connection.send(b'fixture bytes')
    actual.failed(error.value)
    proof=actual.snapshot()
    assert proof['failed_phase']=='CONNECTING_TRANSPORT'
    assert 'request_bytes_written' not in proof and 'connection_established' not in proof
    assert 'sensitive' not in json.dumps(proof)


def test_request_write_timeout_is_not_reported_as_headers_wait(monkeypatch):
    actual=trace();connection=_TracedHTTPConnection('127.0.0.1',transport_trace=actual)
    connection.sock=object()
    def fail(*_):raise TimeoutError('private header/body')
    monkeypatch.setattr(http.client.HTTPConnection,'send',fail)
    with pytest.raises(TimeoutError) as error:connection.send(b'fixture bytes')
    actual.failed(error.value)
    assert actual.snapshot()['failed_phase']=='WRITING_REQUEST'
    assert 'request_bytes_written' not in actual.snapshot()


def test_tls_certificate_and_hostname_verification_remain_enabled():
    connection=_TracedHTTPSConnection('example.invalid',transport_trace=trace())
    assert connection._context.check_hostname is True
    assert connection._context.verify_mode==ssl.CERT_REQUIRED


def test_safe_trace_rejects_private_fields_and_invalid_connection_metadata():
    raw=trace().snapshot();raw.update(connected_peer_port=True,connection_established='yes',
        peer_is_loopback=1,connection_detail_available=[],connect_ms=float('nan'),
        request_bytes_written=-1,Authorization='private',body='private',peer_address='private')
    result=safe_transport_trace(raw)
    assert not set(raw).difference(trace().snapshot()).intersection(result)


def test_untraced_model_opener_preserves_stdlib_path(monkeypatch):
    from core import model_client as module
    observed=[];sentinel=object()
    monkeypatch.setattr(module,'_stdlib_urlopen',lambda req,timeout:(observed.append((req,timeout)) or sentinel))
    request=Request('http://127.0.0.1:42/models')
    assert module.urlopen(request,timeout=2)==sentinel
    assert observed==[(request,2)]


def test_completion_model_opener_passes_the_same_request_object(monkeypatch):
    from core import model_client as module
    actual=trace();request=Request('http://127.0.0.1:42/v1/chat/completions',data=b'unchanged')
    request._completion_transport_trace=actual;observed=[];sentinel=object()
    monkeypatch.setattr('core.ai.transport_diagnostics.open_traced_request',
        lambda req,timeout,trace:(observed.append((req,timeout,trace)) or sentinel))
    assert module.urlopen(request,timeout=2)==sentinel
    assert observed==[(request,2,actual)] and request.data==b'unchanged'
