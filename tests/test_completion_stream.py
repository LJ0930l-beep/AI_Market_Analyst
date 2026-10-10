import io
import json
import time

import pytest

from core.completion_stream import CompletionStreamError, collect_completion_stream
from core.model_routing import DEFAULT_MODEL, is_configured_model_identity


def event(content=None, *, model=DEFAULT_MODEL, finish=None, index=0, **extra):
    value = {"model": model, "choices": [{"index": index, "delta": {}, "finish_reason": finish}], **extra}
    if content is not None:
        value["choices"][0]["delta"]["content"] = content
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"


DONE = b"data: [DONE]\n\n"


class Response(io.BytesIO):
    headers = {"Content-Type": "text/event-stream; charset=utf-8"}


def collect(data):
    return collect_completion_stream(Response(data), deadline=time.monotonic() + 2,
                                     identity_check=is_configured_model_identity, bounded_ttl_text=True)[0]


@pytest.mark.parametrize('value', ['60', '777', '1800'])
def test_ttl_wire_string_across_every_content_boundary(value):
    content = json.dumps({'action': 'WAIT', 'ttl_seconds': value})
    raw = b''.join(event(character) for character in content) + event(finish='stop') + DONE
    assert collect(raw)['choices'][0]['message']['content'] == content


def test_runaway_ttl_text_is_rejected_without_reading_the_rest():
    first = event('{"ttl_seconds":"900')
    second = event('00')
    class BoundedResponse(Response):
        reads = 0
        def read1(self, size):
            self.reads += 1
            if self.reads == 1: return first
            if self.reads == 2: return second
            raise AssertionError('runaway must fail before a third read')
    with pytest.raises(CompletionStreamError, match='TTL_TEXT_INVALID'):
        collect_completion_stream(BoundedResponse(b''), deadline=time.monotonic() + 2,
                                  identity_check=is_configured_model_identity, bounded_ttl_text=True)


def test_ttl_guard_ignores_nested_analysis_and_literal_key_text():
    content = json.dumps({'reason': 'ttl_seconds:' + '9' * 3000,
                         'analysis': {'ttl_seconds': '9' * 3000}, 'ttl_seconds': '900'})
    assert collect(event(content, finish='stop') + DONE)['choices'][0]['message']['content'] == content


@pytest.mark.parametrize('value', ['59', '1801', '0900', '900.0', '9e2', '+900', '', '９００'])
def test_ttl_wire_string_invalid_forms_fail_in_stream(value):
    with pytest.raises(CompletionStreamError, match='TTL_TEXT_INVALID'):
        collect(event(json.dumps({'ttl_seconds': value}), finish='stop') + DONE)


def test_escaped_ttl_key_still_gets_the_bounded_value_check():
    with pytest.raises(CompletionStreamError, match='TTL_TEXT_INVALID'):
        collect(event('{"ttl_\\u0073econds":"90000"}', finish='stop') + DONE)


def test_ttl_digit_object_across_every_delta_keeps_only_complete_content():
    content = json.dumps({'reason':'fixture','ttl_seconds':{'d3':'0','d2':'9','d1':'0','d0':'0'}})
    raw = b''.join(event(char) for char in content) + event(finish='stop') + DONE
    assert collect(raw)['choices'][0]['message']['content'] == content


@pytest.mark.parametrize('value', ['00', '9 prose', '９', '', '\\u0039'])
def test_digit_string_cannot_turn_into_freeform_prose(value):
    content = json.dumps({'ttl_seconds':{'d3':'0','d2':value,'d1':'0','d0':'0'}})
    with pytest.raises(CompletionStreamError, match='TTL_TEXT_INVALID'):
        collect(event(content, finish='stop') + DONE)


def test_split_unicode_and_sse_frames_return_only_one_complete_original_content():
    raw = event('{"action":"') + event('WAIT","reason":"等待"}') + event(finish="stop") + DONE
    class SplitResponse(Response):
        def read1(self, size):
            return super().read1(1)
    response, count = collect_completion_stream(SplitResponse(raw), deadline=time.monotonic() + 2,
                                                identity_check=is_configured_model_identity)
    assert count == len(raw)
    assert response["choices"][0]["message"]["content"] == '{"action":"WAIT","reason":"等待"}'
    assert response["model"] == DEFAULT_MODEL
    assert response["choices"][0]["finish_reason"] == "stop"


def test_multiline_sse_data_and_comments_are_parsed_as_one_json_event():
    body = {"model": DEFAULT_MODEL, "choices": [{"index": 0, "delta": {"content": "{}"}, "finish_reason": "stop"}]}
    lines = json.dumps(body, indent=2).splitlines()
    raw = b": keepalive\r\n\r\n" + b"\r\n".join(b"data: " + line.encode() for line in lines) + b"\r\n\r\n" + DONE
    assert collect(raw)["choices"][0]["message"]["content"] == "{}"


def test_exact_duplicate_explicit_sse_event_is_rejected_without_returning_partial_content():
    repeated = b"id: relay-event-1\n" + event('{"action":"WAIT"}')
    with pytest.raises(CompletionStreamError, match="DUPLICATE_EVENT"):
        collect(repeated + repeated + event(finish="stop") + DONE)


def test_identical_deltas_without_ids_and_reused_ids_with_different_data_are_preserved():
    no_ids = event(" ") + event(" ") + event(finish="stop") + DONE
    assert collect(no_ids)["choices"][0]["message"]["content"] == "  "

    same_id_different_data = (b"id: relay-event-2\n" + event("a")
                              + b"id: relay-event-2\n" + event("b")
                              + event(finish="stop") + DONE)
    assert collect(same_id_different_data)["choices"][0]["message"]["content"] == "ab"


@pytest.mark.parametrize("data,code", [
    (event("{}"), "EOF_BEFORE_DONE"),
    (event("{}") + DONE, "INCOMPLETE"),
    (event(finish="stop") + DONE, "INCOMPLETE"),
    (event("{}", finish="length") + DONE, "FINISH_INVALID"),
    (event("{}", finish="content_filter") + DONE, "FINISH_INVALID"),
    (event("{}", finish="tool_calls") + DONE, "FINISH_INVALID"),
    (event("{}", model="gemini-3.8-flash") + event(finish="stop") + DONE, "IDENTITY_MISMATCH"),
    (event("{}", model=None) + event(finish="stop") + DONE, "IDENTITY_MISMATCH"),
    (event("{}", index=1) + event(finish="stop") + DONE, "CHOICE_INVALID"),
    (event(12) + event(finish="stop") + DONE, "TEXT_INVALID"),
    (b"data: broken-secret-json\n\n", "EVENT_INVALID"),
    (b'data: {"error":{"message":"private-secret"}}\n\n', "ERROR_EVENT"),
    (event("{}", finish="stop") + event("extra") + DONE, "DATA_AFTER_FINISH"),
    (event("{}", finish="stop") + DONE + b"data: extra\n\n", "TRAILING_DATA"),
])
def test_partial_or_rejected_stream_never_returns_a_completion(data, code):
    with pytest.raises(CompletionStreamError, match=code) as failure:
        collect(data)
    assert "private-secret" not in str(failure.value)
    assert "broken-secret" not in str(failure.value)


def test_usage_and_reasoning_are_preserved_but_reasoning_cannot_be_final_content():
    first = {"model": DEFAULT_MODEL, "choices": [{"index": 0,
        "delta": {"reasoning_content": "thinking"}, "finish_reason": None}]}
    usage = {"model": DEFAULT_MODEL, "choices": [], "usage": {"total_tokens": 12}}
    raw = b"data: " + json.dumps(first).encode() + b"\n\n"
    raw += event("{}", finish="stop") + b"data: " + json.dumps(usage).encode() + b"\n\n" + DONE
    response = collect(raw)
    assert response["choices"][0]["message"]["reasoning_content"] == "thinking"
    assert response["choices"][0]["message"]["content"] == "{}"
    assert response["usage"] == {"total_tokens": 12}


def test_stream_without_any_model_identity_is_rejected_even_if_manifest_is_valid():
    raw = b'data: {"choices":[{"index":0,"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n' + DONE
    with pytest.raises(CompletionStreamError, match="INCOMPLETE"):
        collect(raw)


def test_expired_total_deadline_fails_before_reading_any_partial_content():
    class ForbiddenResponse(Response):
        def read1(self, size):
            pytest.fail("expired budget must not read")
    with pytest.raises(TimeoutError, match="TOTAL_DEADLINE"):
        collect_completion_stream(ForbiddenResponse(b""), deadline=1, clock=lambda: 2,
                                  identity_check=is_configured_model_identity)


@pytest.mark.parametrize("limit,value", [("MAX_STREAM_BYTES", 50), ("MAX_EVENT_BYTES", 50), ("MAX_STREAM_EVENTS", 0)])
def test_memory_and_event_limits_reject_without_exposing_data(monkeypatch, limit, value):
    import core.completion_stream as module
    monkeypatch.setattr(module, limit, value)
    with pytest.raises(CompletionStreamError, match="LIMIT"):
        collect(event("private response content" * 100) + event(finish="stop") + DONE)


@pytest.mark.parametrize("mode", ["dribble", "stall", "complete"])
def test_actual_loopback_http_enforces_absolute_deadline_and_complete_boundary(mode):
    from contextlib import contextmanager
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.request import urlopen

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                if mode == "dribble":
                    for part in b"data: " + b"x" * 30:
                        self.wfile.write(bytes([part]))
                        self.wfile.flush()
                        time.sleep(.025)
                elif mode == "stall":
                    self.wfile.write(event('{"action":"WAIT"}'))
                    self.wfile.flush()
                    time.sleep(.5)
                else:
                    self.wfile.write(event('{"action":"WAIT"}', finish="stop") + DONE)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    worker.start()
    started = time.monotonic()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=1) as response:
            if mode == "complete":
                result, _ = collect_completion_stream(response, deadline=started + 1,
                    identity_check=is_configured_model_identity)
                assert result["choices"][0]["message"]["content"] == '{"action":"WAIT"}'
            else:
                with pytest.raises(TimeoutError):
                    collect_completion_stream(response, deadline=started + .12,
                        identity_check=is_configured_model_identity)
                assert time.monotonic() - started < .4
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def test_numeric_runaway_is_rejected_across_stream_deltas_before_terminal_read():
    from core.completion_stream import MAX_JSON_NUMBER_CHARACTERS
    chunks = [event('{"action":"WAIT","ttl_seconds":5')]
    chunks += [event('0' * 64) for _ in range(MAX_JSON_NUMBER_CHARACTERS // 64 + 1)]
    class EndlessDigits(Response):
        def read1(self, size):
            if not chunks:
                pytest.fail('numeric runaway must stop before more network reads')
            return chunks.pop(0)
    with pytest.raises(CompletionStreamError, match='JSON_NUMBER_LIMIT'):
        collect_completion_stream(EndlessDigits(b''), deadline=time.monotonic() + 2,
                                  identity_check=is_configured_model_identity)


def test_quoted_numbers_escaped_quotes_and_reasoning_do_not_trip_numeric_guard():
    from core.completion_stream import MAX_JSON_NUMBER_CHARACTERS
    content = json.dumps({'reason': 'embedded "quote" ' + '0' * (MAX_JSON_NUMBER_CHARACTERS + 10),
                          'entry_price': 4.1234e-10, 'ttl_seconds': 1800})
    raw = b''
    for character in content:
        raw += event(character)
    thinking = {'model': DEFAULT_MODEL, 'choices': [{'index': 0,
        'delta': {'reasoning_content': '9' * (MAX_JSON_NUMBER_CHARACTERS + 10)}, 'finish_reason': None}]}
    raw += b'data: ' + json.dumps(thinking).encode() + b'\n\n' + event(finish='stop') + DONE
    assert collect(raw)['choices'][0]['message']['content'] == content


def test_number_tokens_are_counted_separately_and_boundary_is_inclusive(monkeypatch):
    import core.completion_stream as module
    monkeypatch.setattr(module, 'MAX_JSON_NUMBER_CHARACTERS', 5)
    content = '{"a":12345,"b":-1.25,"c":1e+03,"d":12345}'
    raw = b''.join(event(char) for char in content) + event(finish='stop') + DONE
    assert collect(raw)['choices'][0]['message']['content'] == content
    with pytest.raises(CompletionStreamError, match='JSON_NUMBER_LIMIT'):
        collect(event('{"ttl_seconds":') + event('123') + event('456}') + event(finish='stop') + DONE)
