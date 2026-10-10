"""End-to-end completion transport faults against loopback only."""
import json
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import model_client as model_client_module
from core.model_client import ModelClient, ModelClientError
from core.model_routing import DEFAULT_MODEL


def _chunk(*, content=None, finish=None):
    delta = {} if content is None else {"content": content}
    return {"model": DEFAULT_MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _frame(value, *, event_id=None):
    prefix = b"" if event_id is None else f"id: {event_id}\n".encode()
    return prefix + b"data: " + json.dumps(value, separators=(",", ":")).encode() + b"\n\n"


@pytest.mark.parametrize("scenario", [
    "disconnect", "truncated_json", "duplicate_event", "provider_termination",
    "body_read_stall", "success",
])
def test_http_200_sse_faults_never_yield_partial_completion_and_are_single_attempt(scenario, monkeypatch):
    release = threading.Event()
    received = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            received.append({
                "body": body,
                "request_id": self.headers.get("X-AI-Request-ID"),
                "attempt_id": self.headers.get("X-AI-Attempt-ID"),
                "correlation_id": self.headers.get("X-AI-Correlation-ID"),
                "authorization": self.headers.get("Authorization"),
            })
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                if scenario == "truncated_json":
                    self.wfile.write(b'data: {"model":"' + DEFAULT_MODEL.encode() + b'","choices":[\n\n')
                elif scenario == "duplicate_event":
                    frame = _frame(_chunk(content='{"action":"WAIT"}'), event_id="relay-event-1")
                    self.wfile.write(frame)
                    self.wfile.write(frame)
                elif scenario == "provider_termination":
                    self.wfile.write(_frame(_chunk(content="partial", finish="length")))
                elif scenario == "disconnect":
                    self.wfile.write(_frame(_chunk(content="private-partial-output")))
                    self.wfile.flush()
                    time.sleep(.03)
                    linger_layout = "hh" if sys.platform == "win32" else "ii"
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                                struct.pack(linger_layout, 1, 0))
                    self.connection.close()
                    return
                elif scenario == "body_read_stall":
                    self.wfile.write(_frame(_chunk(content="private-partial-output")))
                    self.wfile.flush()
                    release.wait(1.5)
                    self.wfile.write(_frame(_chunk(finish="stop")) + b"data: [DONE]\n\n")
                else:
                    self.wfile.write(_frame(_chunk(content='{"action":"WAIT"}'))
                                     + _frame(_chunk(finish="stop"))
                                     + b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    worker.start()
    monkeypatch.setattr(model_client_module.config, "api_key", "offline-fixture-token")
    client = ModelClient(base_url="http://127.0.0.1:8045/v1", model_name=DEFAULT_MODEL, retries=0)
    # Keep the production route guard intact while redirecting this one call to
    # the isolated, ephemeral loopback server.
    client._endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        if scenario == "success":
            result = client.chat_completion(
                [{"role": "user", "content": "offline fixture"}],
                model_name=DEFAULT_MODEL, stream=True, timeout_sec=.25,
            )
            assert result["choices"][0]["message"]["content"] == '{"action":"WAIT"}'
            trace = client.last_transport_trace
            assert trace["phase"] == "COMPLETED" and trace["stream_done"] is True
        else:
            with pytest.raises(ModelClientError) as failure:
                client.chat_completion(
                    [{"role": "user", "content": "offline fixture"}],
                    # Leave enough time for the loopback server's abortive close
                    # to reach a loaded Windows runner. The body-stall fixture
                    # intentionally waits beyond this deadline.
                    model_name=DEFAULT_MODEL, stream=True, timeout_sec=1.0,
                )
            trace = failure.value.transport_trace
            assert trace["phase"] == "FAILED"
            assert trace["failed_phase"] == "READING_RESPONSE_BODY"
            assert trace["stream_done"] is not True
            assert trace["response_bytes"] > 0 and trace["body_read_ms"] >= 0
            if scenario == "truncated_json":
                assert trace["failure_code"] == "MODEL_STREAM_EVENT_INVALID"
            elif scenario == "duplicate_event":
                assert trace["failure_code"] == "MODEL_STREAM_DUPLICATE_EVENT"
            elif scenario == "provider_termination":
                assert trace["failure_code"] == "MODEL_STREAM_FINISH_INVALID"
            elif scenario == "disconnect":
                assert trace["failure_code"] == "MODEL_TRANSPORT_DISCONNECTED"
            else:
                assert trace["failure_code"] == "MODEL_TRANSPORT_TIMEOUT"
        assert trace["http_status"] == 200
        assert trace["attempt"] == 1
        assert len(received) == 1
        assert trace["request_id"] == trace["correlation_id"] == received[0]["request_id"]
        assert trace["attempt_id"] == received[0]["attempt_id"]
        assert trace["attempt_id"] != trace["request_id"]
        assert received[0]["authorization"] == "Bearer offline-fixture-token"
        assert "private-partial-output" not in json.dumps(trace)
        assert "offline-fixture-token" not in json.dumps(trace)
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()
