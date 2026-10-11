"""Bounded metadata only; no headers, credentials, prompts or body contents."""
from copy import deepcopy
from functools import partial
import hashlib
import http.client
import ipaddress
import json
import math
import re
import time
import uuid
from urllib.request import HTTPHandler, HTTPSHandler, build_opener

PHASES = {'BUILD_REQUEST','AWAITING_RESPONSE_HEADERS','READING_RESPONSE_BODY',
          'CONNECTING_TRANSPORT','WRITING_REQUEST',
          'VALIDATING_RESPONSE','HTTP_STATUS_REJECTED','COMPLETED','FAILED'}
ERRORS = {'TimeoutError','URLError','HTTPError','JSONDecodeError','UnicodeDecodeError',
          'ModelClientError','ModelSchemaError','OSError','ConnectionError',
          'ConnectionResetError','ConnectionAbortedError','BrokenPipeError','RemoteDisconnected'}


def safe_transport_trace(raw):
    if not isinstance(raw, dict) or raw.get('version') != 'gemini_transport_trace_v1':
        return None
    if (not re.fullmatch('[0-9a-f]{32}', str(raw.get('correlation_id') or ''))
            or not re.fullmatch('[0-9a-f]{64}', str(raw.get('wire_messages_sha256') or ''))):
        return None
    result = {k:raw[k] for k in ('version','correlation_id','wire_messages_sha256')}
    request_id = raw.get('request_id')
    if request_id is not None:
        if not re.fullmatch('[0-9a-f]{32}', str(request_id)) or request_id != raw.get('correlation_id'):
            return None
        result['request_id'] = request_id
    attempt_id = raw.get('attempt_id')
    if attempt_id is not None:
        if not re.fullmatch('[0-9a-f]{32}', str(attempt_id)):
            return None
        result['attempt_id'] = attempt_id
    for key in ('phase','failed_phase'):
        if isinstance(raw.get(key),str) and raw[key] in PHASES:
            result[key] = raw[key]
    for key in ('attempt','configured_timeout_seconds','elapsed_ms','headers_wait_ms','body_read_ms','response_bytes',
                'stream_event_count','first_stream_event_ms','first_content_ms',
                'connect_ms','request_write_ms','request_bytes_written'):
        value = raw.get(key)
        if type(value) in (int,float) and 0 <= value <= 1e12 and math.isfinite(value):
            result[key] = value
    for key in ('connection_detail_available','connection_established','peer_is_loopback'):
        if type(raw.get(key)) is bool:
            result[key] = raw[key]
    port = raw.get('connected_peer_port')
    if type(port) is int and 1 <= port <= 65535:
        result['connected_peer_port'] = port
    status = raw.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        result['http_status'] = status
    if isinstance(raw.get('error_type'),str) and raw['error_type']:
        result['error_type'] = raw['error_type'] if raw['error_type'] in ERRORS else 'OTHER_ERROR'
    failure_code = raw.get('failure_code')
    if isinstance(failure_code, str) and re.fullmatch(r'MODEL_[A-Z0-9_]{1,96}', failure_code):
        result['failure_code'] = failure_code
    if raw.get('transport_mode') in ('JSON', 'SSE'):
        result['transport_mode'] = raw['transport_mode']
    if type(raw.get('stream_done')) is bool:
        result['stream_done'] = raw['stream_done']
    if raw.get('stream_finish_reason') == 'stop':
        result['stream_finish_reason'] = 'stop'
    return result


class CompletionTransportTrace:
    def __init__(self, messages, attempt, timeout_seconds, *, request_id=None, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.headers_started = self.started
        self.body_started = None
        self.connect_started = None
        request_id = request_id or uuid.uuid4().hex
        if not re.fullmatch('[0-9a-f]{32}', str(request_id)):
            raise ValueError('TRANSPORT_REQUEST_ID_INVALID')
        self.data = {'version':'gemini_transport_trace_v1','correlation_id':request_id,
            'request_id':request_id,'attempt_id':uuid.uuid4().hex,
            'wire_messages_sha256':hashlib.sha256(json.dumps({'messages':messages},sort_keys=True,
                separators=(',',':')).encode()).hexdigest(),
            'attempt':attempt,'configured_timeout_seconds':timeout_seconds,'phase':'BUILD_REQUEST'}

    def connecting_transport(self):
        self.connect_started = self.clock()
        self.data.update(phase='CONNECTING_TRANSPORT',connection_detail_available=True)

    def transport_connected(self, peer):
        self.data.update(connection_established=True,
                         connect_ms=round(max(0,self.clock()-self.connect_started)*1000,3))
        # Peer address never leaves memory. A proxy's port can differ from the
        # requested relay port; metadata alone is not handler acceptance proof.
        try:
            self.data['peer_is_loopback'] = ipaddress.ip_address(peer[0]).is_loopback
            self.data['connected_peer_port'] = int(peer[1])
        except (ValueError,TypeError,IndexError):
            pass

    def request_writing(self):
        self.data['phase'] = 'WRITING_REQUEST'

    def request_written(self, size, elapsed):
        self.data['request_bytes_written'] = self.data.get('request_bytes_written',0) + size
        self.data['request_write_ms'] = round(self.data.get('request_write_ms',0) + max(0,elapsed)*1000,3)

    def awaiting_headers(self):
        self.headers_started = self.clock()
        self.data['phase'] = 'AWAITING_RESPONSE_HEADERS'

    def headers_received(self, status):
        now = self.clock()
        self.data.update(http_status=status,headers_wait_ms=round(max(0,now-self.headers_started)*1000,3),
                         phase='READING_RESPONSE_BODY')
        self.body_started = now

    def body_received(self, size):
        now = self.clock()
        self.data.update(body_read_ms=round(max(0,now-self.body_started)*1000,3),response_bytes=size,
                         phase='VALIDATING_RESPONSE')

    def body_progress(self, size):
        if type(size) is not int or size < 0:
            return
        now = self.clock()
        self.data['response_bytes'] = self.data.get('response_bytes', 0) + size
        if self.body_started is not None:
            self.data['body_read_ms'] = round(max(0,now-self.body_started)*1000,3)

    def completed(self):
        self.data['phase'] = 'COMPLETED'

    def stream_event(self, *, has_content):
        elapsed = round(max(0, self.clock() - self.started) * 1000, 3)
        self.data['stream_event_count'] = self.data.get('stream_event_count', 0) + 1
        self.data.setdefault('first_stream_event_ms', elapsed)
        if has_content:
            self.data.setdefault('first_content_ms', elapsed)

    def failed(self, error):
        self.data.update(failed_phase=self.data['phase'],phase='FAILED',error_type=type(error).__name__)
        message = getattr(error, 'code', None)
        if not isinstance(message, str):
            message = str(error)
        if re.fullmatch(r'MODEL_[A-Z0-9_]{1,96}', message):
            self.data['failure_code'] = message
        elif type(error).__name__ in {'TimeoutError', 'socket.timeout'}:
            self.data['failure_code'] = 'MODEL_TRANSPORT_TIMEOUT'
        elif type(error).__name__ in {'ConnectionResetError', 'ConnectionAbortedError',
                                      'BrokenPipeError', 'RemoteDisconnected'}:
            self.data['failure_code'] = 'MODEL_TRANSPORT_DISCONNECTED'
        status = getattr(error,'code',None)
        if type(status) is int:
            self.data['http_status'] = status
            if 400 <= status <= 599:
                self.data['failed_phase'] = 'HTTP_STATUS_REJECTED'

    def snapshot(self):
        result = deepcopy(self.data)
        result['elapsed_ms'] = round(max(0,self.clock()-self.started)*1000,3)
        return safe_transport_trace(result)


class _TracedConnection:
    """Instrument stdlib I/O without changing routing, payload or retries."""
    def __init__(self, *args, transport_trace, **kwargs):
        self._completion_trace = transport_trace
        super().__init__(*args, **kwargs)

    def connect(self):
        trace = self._completion_trace
        trace.connecting_transport()
        try:
            super().connect()  # includes DNS, proxy tunnel and TLS where applicable
        except Exception:
            # A CONNECT proxy tunnel can write bytes inside connect(); TLS or
            # tunnel failure still belongs to transport establishment.
            trace.data['phase'] = 'CONNECTING_TRANSPORT'
            raise
        trace.transport_connected(self.sock.getpeername())

    def send(self, data):
        # Mirror HTTPConnection.send's automatic connection handling before
        # setting the write phase, so failed connects remain distinguishable.
        if self.sock is None:
            if not self.auto_open:
                raise http.client.NotConnected()
            self.connect()
        trace = self._completion_trace
        trace.request_writing()
        started = trace.clock()
        super().send(data)
        # Our completion requests carry byte headers and a byte JSON body.
        # Never consume or alter a file/iterator for diagnostics.
        if isinstance(data, (bytes,bytearray,memoryview)):
            trace.request_written(len(data), trace.clock()-started)

    def getresponse(self):
        self._completion_trace.awaiting_headers()
        return super().getresponse()


class _TracedHTTPConnection(_TracedConnection, http.client.HTTPConnection):
    pass


class _TracedHTTPSConnection(_TracedConnection, http.client.HTTPSConnection):
    pass


class _TraceHTTPHandler(HTTPHandler):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def http_open(self, request):
        return self.do_open(partial(_TracedHTTPConnection, transport_trace=self.trace),request)


class _TraceHTTPSHandler(HTTPSHandler):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def https_open(self, request):
        return self.do_open(partial(_TracedHTTPSConnection, transport_trace=self.trace),request,
                            context=self._context)


def open_traced_request(request, *, timeout, trace):
    # Default proxy, redirect, authentication, and certificate policy remain
    # stdlib defaults. Each request has independent connections and trace.
    return build_opener(_TraceHTTPHandler(trace),_TraceHTTPSHandler(trace)).open(request,timeout=timeout)
