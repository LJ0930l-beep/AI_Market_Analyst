"""Health failure provenance; isolated fixtures, no relay or exchange calls."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from core.ai import ollama
from core.ai.contracts import LLMError
from core.ai.transport_diagnostics import CompletionTransportTrace
from core.model_client import ModelTimeoutError
from core.model_routing import DEFAULT_SMART_MODEL
from core.replay.ai_template_runner import (
    ReplayModelUnavailable, _production_model_pin, _record_health_failure,
)


def failure_proof(phase):
    trace = CompletionTransportTrace([{'role': 'user', 'content': 'private fixture'}], 1, 30)
    trace.awaiting_headers()
    if phase == 'READING_RESPONSE_BODY':
        trace.headers_received(200)
    trace.failed(TimeoutError('private fixture error'))
    return trace.snapshot()


@pytest.fixture(autouse=True)
def isolated_cache():
    with ollama._GEMINI_HEALTH_LOCK:
        before = deepcopy(ollama._GEMINI_HEALTH_CACHE)
        ollama._GEMINI_HEALTH_CACHE.clear()
    yield
    with ollama._GEMINI_HEALTH_LOCK:
        ollama._GEMINI_HEALTH_CACHE.clear()
        ollama._GEMINI_HEALTH_CACHE.update(before)


@pytest.mark.parametrize('phase', ['AWAITING_RESPONSE_HEADERS', 'READING_RESPONSE_BODY'])
def test_health_keeps_this_probe_trace_through_cache_pin_and_durable_context(monkeypatch, phase):
    proof = failure_proof(phase)
    failure = ModelTimeoutError('fixture timeout')
    failure.transport_trace = {**proof, 'Authorization': 'fixture secret', 'body': 'private fixture'}
    calls = []
    monkeypatch.setattr(ollama.model_client, 'list_models', lambda **_: [{'id': DEFAULT_SMART_MODEL}])

    def fail(messages, **options):
        calls.append(options)
        raise failure

    monkeypatch.setattr(ollama.model_client, 'structured_analysis', fail)
    provider = ollama.OllamaProvider()
    result = provider.health(timeout_sec=30)
    assert result['available'] is False and result['error_type'] == 'ModelTimeoutError'
    assert result['transport_trace']['correlation_id'] == proof['correlation_id']
    assert result['transport_trace']['failed_phase'] == phase
    result['transport_trace']['failed_phase'] = 'WRITING_REQUEST'
    cached = provider.health(timeout_sec=30)
    assert cached['transport_trace']['failed_phase'] == phase
    cached['transport_trace']['phase'] = 'WRITING_REQUEST'
    with pytest.raises(ReplayModelUnavailable) as error:
        _production_model_pin(provider)
    assert error.value.transport_trace['failed_phase'] == phase
    context = SimpleNamespace(model_inference_settings={'existing': {'keep': True}},
                              model_call_attempted=False, model_call_completed=False)
    _record_health_failure(context, error.value, 'BEFORE_DECISION')
    saved = json.loads(json.dumps(context.model_inference_settings))
    assert saved['existing'] == {'keep': True}
    assert saved['replay_health_failure']['stage'] == 'BEFORE_DECISION'
    assert saved['replay_health_failure']['transport_trace']['correlation_id'] == proof['correlation_id']
    assert 'model_response_audit' not in saved
    assert not context.model_call_attempted and not context.model_call_completed
    assert 'private fixture' not in json.dumps(saved) and 'fixture secret' not in json.dumps(saved)
    assert len(calls) == 1 and calls[0]['retries'] == 0 and calls[0]['stream'] is True
    assert calls[0]['reasoning_effort'] == 'high' and 0 < calls[0]['timeout_sec'] <= 30


def test_health_probe_timeout_never_exceeds_configured_budget(monkeypatch):
    calls = []
    # Model the sub-microsecond clock-sampling drift seen on Windows CI.
    monotonic_values = iter([1.0, 1.0 - 5.684341886080802e-14, 1.0])
    monkeypatch.setattr(ollama.time, 'monotonic', lambda: next(monotonic_values))
    monkeypatch.setattr(ollama.model_client, 'list_models',
                        lambda **_: [{'id': DEFAULT_SMART_MODEL}])

    def fail_probe(_messages, **options):
        calls.append(options)
        raise TimeoutError('fixture timeout')

    monkeypatch.setattr(ollama.model_client, 'structured_analysis', fail_probe)
    result = ollama.OllamaProvider().health(timeout_sec=30)

    assert result['available'] is False
    assert len(calls) == 1
    assert calls[0]['timeout_sec'] == 30


def test_manifest_failure_cannot_steal_previous_completion_trace(monkeypatch):
    calls = []
    monkeypatch.setattr(ollama.model_client._response_state, 'transport_trace',
                        failure_proof('AWAITING_RESPONSE_HEADERS'), raising=False)

    def fail_manifest(**_):
        raise TimeoutError('fixture manifest timeout')

    monkeypatch.setattr(ollama.model_client, 'list_models', fail_manifest)
    monkeypatch.setattr(ollama.model_client, 'structured_analysis', lambda *a, **k: calls.append(k))
    provider = ollama.OllamaProvider()
    health = provider.health()
    assert 'transport_trace' not in health and calls == []
    with pytest.raises(ReplayModelUnavailable) as error:
        _production_model_pin(provider)
    assert error.value.transport_trace is None


def test_provider_health_rejection_carries_trace_without_attempting_decision(monkeypatch):
    proof = failure_proof('READING_RESPONSE_BODY')
    provider = ollama.OllamaProvider()
    monkeypatch.setattr(provider, 'health', lambda **_: {
        'available': False, 'model_available': False, 'error_code': 'MODEL_UNAVAILABLE',
        'transport_trace': {**proof, 'body': 'private fixture'},
    })
    calls = []
    monkeypatch.setattr(ollama.model_client, 'structured_analysis', lambda *a, **k: calls.append(k))
    with pytest.raises(LLMError) as error:
        provider.generate_json([{'role': 'user', 'content': 'fixture'}],
            model_name=DEFAULT_SMART_MODEL, prompt_version='fixture', input_hash='a' * 64)
    assert error.value.transport_trace == proof
    assert calls == []


def test_direct_provider_exception_keeps_its_bound_failure_evidence():
    proof = failure_proof('AWAITING_RESPONSE_HEADERS')
    failure = ModelTimeoutError('fixture timeout')
    failure.transport_trace = proof

    def fail(**_):
        raise failure

    with pytest.raises(ReplayModelUnavailable) as error:
        _production_model_pin(SimpleNamespace(health=fail))
    assert error.value.transport_trace == proof
    assert error.value.__cause__ is failure


@pytest.mark.parametrize('stage', ['BEFORE_DECISION', 'AFTER_DECISION'])
def test_probe_failure_never_overwrites_an_existing_completed_decision_receipt(stage):
    original = {'model_response_audit': {'attempts': [{'status': 'COMPLETED', 'request_hash': 'b' * 64}]}}
    context = SimpleNamespace(model_inference_settings=deepcopy(original))
    _record_health_failure(context, ReplayModelUnavailable('fixture',
                          transport_trace=failure_proof('AWAITING_RESPONSE_HEADERS')), stage)
    assert context.model_inference_settings['model_response_audit'] == original['model_response_audit']
    assert context.model_inference_settings['replay_health_failure']['stage'] == stage


def test_unrelated_error_and_malformed_trace_do_not_fabricate_health_proof():
    context = SimpleNamespace(model_inference_settings={'keep': 1})
    _record_health_failure(context, ValueError('fixture'), 'BEFORE_DECISION')
    assert context.model_inference_settings == {'keep': 1}
    _record_health_failure(context, ReplayModelUnavailable('fixture', transport_trace={'body': 'secret'}),
                           'BEFORE_DECISION')
    assert 'transport_trace' not in context.model_inference_settings['replay_health_failure']


def test_replay_context_serialization_keeps_health_failure_without_promoting_model_flags():
    from core.trading.ai_led_engine import AICycleContext
    from core.replay.ai_template_runner import _context_dict
    from inspect import signature
    # Use the normal context constructor; every required identity is fixture-only.
    required = {k: 'fixture' for k, v in signature(AICycleContext).parameters.items()
                if v.default is v.empty}
    context = AICycleContext(**required)
    _record_health_failure(context, ReplayModelUnavailable('fixture',
        transport_trace=failure_proof('AWAITING_RESPONSE_HEADERS')), 'BEFORE_DECISION')
    saved = json.loads(json.dumps(_context_dict(context), default=str))
    assert saved['model_inference_settings']['replay_health_failure']['stage'] == 'BEFORE_DECISION'
    assert saved['model_call_attempted'] is False and saved['model_call_completed'] is False


@pytest.mark.parametrize('phase', ['AWAITING_RESPONSE_HEADERS', 'READING_RESPONSE_BODY'])
def test_real_isolated_probe_timeout_survives_provider_and_replay_wrapper(monkeypatch, phase):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.request import Request
    import threading
    from core.ai.transport_diagnostics import open_traced_request
    from core.model_client import ModelClient
    from core.model_client import config

    release, received = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            received.set()
            if phase == 'READING_RESPONSE_BODY':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.flush()
            release.wait(2)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = ModelClient(retries=0)
    monkeypatch.setattr(config, 'api_key', 'isolated-fixture-only')
    monkeypatch.setattr(ollama, 'model_client', client)
    monkeypatch.setattr(client, 'list_models', lambda **_: [{'id': DEFAULT_SMART_MODEL}])

    def local_transport(request, *, timeout):
        # Only this fixture redirects the wire; production routing stays intact.
        redirected = Request(f'http://127.0.0.1:{server.server_port}/fixture',
                             data=request.data, headers=dict(request.headers))
        return open_traced_request(redirected, trace=request._completion_transport_trace, timeout=.1)

    monkeypatch.setattr('core.model_client.urlopen', local_transport)
    try:
        provider = ollama.OllamaProvider()
        health = provider.health(timeout_sec=.1)
        assert health.get('error_type') == 'ModelTimeoutError', health.get('error_code')
        with pytest.raises(ReplayModelUnavailable) as error:
            _production_model_pin(provider)
        proof = error.value.transport_trace
        assert received.is_set()
        assert proof['failed_phase'] == phase
        assert proof['connection_established'] is True and proof['peer_is_loopback'] is True
        assert proof['request_bytes_written'] > 0 and proof['connected_peer_port'] == server.server_port
        assert proof['attempt'] == 1 and proof.get('stream_done') is not True
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(2)
