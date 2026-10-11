"""Collect one complete OpenAI SSE response under an absolute deadline.

No partial output escapes this boundary. This module has no provider imports,
credentials, retries, or order operations.
"""
from __future__ import annotations

import json
import time
from typing import Callable


class CompletionStreamError(ValueError):
    """Protocol failure; messages contain stable codes, never response content."""


MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_STREAM_EVENTS = 65536
MAX_JSON_NUMBER_CHARACTERS = 1024


class _JsonNumberBoundary:
    """Bound numeric literals across deltas without inspecting quoted text.

    Financial values are finite machine numbers. A kilobyte numeric literal
    is already far beyond their precision; unbounded model digit generation
    must fail before consuming the entire transport deadline.
    """
    def __init__(self):
        self.in_string = False
        self.escaped = False
        self.number_characters = 0

    def feed(self, text):
        for character in text:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif character == "\\":
                    self.escaped = True
                elif character == '"':
                    self.in_string = False
                continue
            if self.number_characters:
                if character in "0123456789.eE+-":
                    self.number_characters += 1
                    if self.number_characters > MAX_JSON_NUMBER_CHARACTERS:
                        raise CompletionStreamError("MODEL_STREAM_JSON_NUMBER_LIMIT")
                    continue
                self.number_characters = 0
            if character == '"':
                self.in_string = True
            elif character in "0123456789-":
                self.number_characters = 1


class _TtlStringBoundary:
    """Bound only top-level TTL text across deltas, never analysis strings.

    This prevents the bounded wire representation from becoming a new long
    string loop. It only rejects; complete JSON/identity/schema validation is
    still required before any response can leave the collector.
    """
    def __init__(self):
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.expect_key = False
        self.key_string = False
        self.ttl_string = False
        self.ttl_value = False
        self.ttl_object_depth = None
        self.ttl_text_limit = 4
        self.key = ""
        self.value = ""

    def feed(self, text):
        for character in text:
            if self.in_string:
                if self.ttl_string:
                    if character == '"':
                        valid = (len(self.value) == 1 if self.ttl_text_limit == 1 else
                                 2 <= len(self.value) <= 4 and self.value[0] != "0"
                                 and 60 <= int(self.value) <= 1800)
                        if not valid:
                            raise CompletionStreamError("MODEL_STREAM_TTL_TEXT_INVALID")
                    elif character not in "0123456789" or len(self.value) >= self.ttl_text_limit:
                        raise CompletionStreamError("MODEL_STREAM_TTL_TEXT_INVALID")
                    else:
                        self.value += character
                if self.escaped:
                    self.escaped = False
                elif character == "\\":
                    self.escaped = True
                elif character == '"':
                    self.in_string = False
                    if self.key_string:
                        try:
                            name = json.loads('"' + self.key + '"')
                        except ValueError:
                            name = None
                        self.ttl_value = name in (("ttl_seconds", "limit_ttl_seconds") if self.depth == 1
                                                 else ("d3", "d2", "d1", "d0"))
                        self.expect_key = False
                    self.key_string = self.ttl_string = False
                    continue
                if self.key_string and len(self.key) < 96:
                    self.key += character
                continue
            if character == '"':
                self.in_string = True
                watched = self.depth == 1 or self.depth == self.ttl_object_depth
                self.key_string = watched and self.expect_key
                self.ttl_string = watched and not self.expect_key and self.ttl_value
                self.ttl_text_limit = 1 if self.depth == self.ttl_object_depth else 4
                self.key = self.value = ""
                if self.ttl_string:
                    self.ttl_value = False
            elif character in "{[":
                ttl_object = self.depth == 1 and self.ttl_value and character == "{"
                self.depth += 1
                if ttl_object:
                    self.ttl_object_depth = self.depth
                    self.expect_key = True
                if self.depth == 1 and character == "{":
                    self.expect_key = True
                else:
                    self.ttl_value = False
            elif character in "}]":
                if self.depth == self.ttl_object_depth:
                    self.ttl_object_depth = None
                    self.expect_key = False
                self.depth -= 1
                self.ttl_value = False
            elif character == "," and (self.depth == 1 or self.depth == self.ttl_object_depth):
                self.expect_key = True
                self.ttl_value = False
            elif character not in " \t\r\n:" and self.depth == 1:
                self.ttl_value = False


def _remaining_timeout(response, deadline, clock):
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("MODEL_TOTAL_DEADLINE_EXCEEDED")
    # urllib HTTPResponse uses this socket. Memory fixtures do not block.
    socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if socket is not None:
        socket.settimeout(remaining)
    return remaining


def collect_completion_stream(response, *, deadline: float,
                              identity_check: Callable[[object], bool], trace=None,
                              clock=time.monotonic, bounded_ttl_text=False):
    if trace is not None:
        trace.data["stream_done"] = False
    if not str(getattr(response, "headers", {}).get("Content-Type", "")).lower().startswith("text/event-stream"):
        raise CompletionStreamError("MODEL_STREAM_CONTENT_TYPE_INVALID")
    if not callable(getattr(response, "read1", None)):
        raise CompletionStreamError("MODEL_STREAM_READER_UNSUPPORTED")
    pending = bytearray()
    data_lines = []
    event_id = None
    seen_event_frames = set()
    event_bytes = 0
    total_bytes = 0
    events = 0
    model = None
    finished = False
    done = False
    content = []
    reasoning = []
    usage = None
    number_boundary = _JsonNumberBoundary()
    ttl_boundary = _TtlStringBoundary()
    response_id, created = None, None

    def dispatch():
        nonlocal events, model, finished, done, usage, response_id, created
        if not data_lines:
            return
        data = b"\n".join(data_lines)
        if data.strip() == b"[DONE]":
            if not finished or not model or not content:
                raise CompletionStreamError("MODEL_STREAM_INCOMPLETE")
            done = True
            return
        if done:
            raise CompletionStreamError("MODEL_STREAM_DATA_AFTER_DONE")
        # The client never resumes a stream, but a relay can still replay a
        # frame inside one response. Reject only an exact replay with the same
        # explicit SSE id; identical deltas without IDs can be legitimate.
        if event_id not in (None, b""):
            identity = (event_id, data)
            if identity in seen_event_frames:
                raise CompletionStreamError("MODEL_STREAM_DUPLICATE_EVENT")
            seen_event_frames.add(identity)
        events += 1
        if events > MAX_STREAM_EVENTS:
            raise CompletionStreamError("MODEL_STREAM_EVENT_LIMIT")
        try:
            event = json.loads(data)
        except (ValueError, UnicodeDecodeError) as error:
            raise CompletionStreamError("MODEL_STREAM_EVENT_INVALID") from error
        if not isinstance(event, dict) or "error" in event:
            raise CompletionStreamError("MODEL_STREAM_ERROR_EVENT")
        if "model" in event:
            candidate = event["model"]
            if not identity_check(candidate) or model is not None and candidate != model:
                raise CompletionStreamError("MODEL_RESPONSE_IDENTITY_MISMATCH")
            model = candidate
        choices = event.get("choices")
        if not isinstance(choices, list):
            raise CompletionStreamError("MODEL_STREAM_CHOICES_INVALID")
        if len(choices) > 1:
            raise CompletionStreamError("MODEL_STREAM_CHOICES_INVALID")
        has_content = False
        for choice in choices:
            if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0:
                raise CompletionStreamError("MODEL_STREAM_CHOICE_INVALID")
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("function_call"):
                raise CompletionStreamError("MODEL_STREAM_DELTA_INVALID")
            if delta.get("role") not in (None, "assistant"):
                raise CompletionStreamError("MODEL_STREAM_ROLE_INVALID")
            for key, destination in (("content", content), ("reasoning_content", reasoning)):
                value = delta.get(key)
                if value is not None and not isinstance(value, str):
                    raise CompletionStreamError("MODEL_STREAM_TEXT_INVALID")
                if value:
                    if finished:
                        raise CompletionStreamError("MODEL_STREAM_DATA_AFTER_FINISH")
                    if key == "content":
                        number_boundary.feed(value)
                        if bounded_ttl_text:
                            ttl_boundary.feed(value)
                    destination.append(value)
                    has_content |= key == "content"
            reason = choice.get("finish_reason")
            if reason is not None:
                if finished or reason != "stop":
                    raise CompletionStreamError("MODEL_STREAM_FINISH_INVALID")
                finished = True
        if event.get("usage") is not None:
            if not isinstance(event["usage"], dict):
                raise CompletionStreamError("MODEL_STREAM_USAGE_INVALID")
            usage = event["usage"]
        if isinstance(event.get("id"), str):
            response_id = event["id"]
        if type(event.get("created")) is int:
            created = event["created"]
        if trace is not None:
            trace.stream_event(has_content=has_content)

    while not done:
        _remaining_timeout(response, deadline, clock)
        # read1 makes one buffered/raw read; readline can reset socket timeouts
        # indefinitely when a peer slowly dribbles an unterminated line.
        chunk = response.read1(16384)
        _remaining_timeout(response, deadline, clock)
        if not chunk:
            raise CompletionStreamError("MODEL_STREAM_EOF_BEFORE_DONE")
        total_bytes += len(chunk)
        if total_bytes > MAX_STREAM_BYTES:
            raise CompletionStreamError("MODEL_STREAM_SIZE_LIMIT")
        if trace is not None:
            trace.body_progress(len(chunk))
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, remaining = pending.partition(b"\n")
            pending[:] = remaining
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                dispatch()
                data_lines.clear()
                event_bytes = 0
                if done:
                    break
            elif line.startswith(b"data:"):
                value = bytes(line[5:])
                if value.startswith(b" "):
                    value = value[1:]
                event_bytes += len(value)
                if event_bytes > MAX_EVENT_BYTES:
                    raise CompletionStreamError("MODEL_STREAM_EVENT_SIZE_LIMIT")
                data_lines.append(value)
            elif line.startswith(b"id:") or line == b"id":
                value = bytes(line[3:]) if line.startswith(b"id:") else b""
                if value.startswith(b" "):
                    value = value[1:]
                # SSE ignores IDs containing NUL and an empty ID resets the
                # reconnect cursor. Neither is a usable duplicate-frame key.
                if b"\x00" not in value:
                    event_id = value
            # SSE event/retry fields and comments carry no response content.
            if not line and not done:
                event_id = None
        if len(pending) + event_bytes > MAX_EVENT_BYTES:
            raise CompletionStreamError("MODEL_STREAM_EVENT_SIZE_LIMIT")
    _remaining_timeout(response, deadline, clock)
    if pending.strip():
        raise CompletionStreamError("MODEL_STREAM_TRAILING_DATA")
    if trace is not None:
        trace.data.update(stream_done=True, stream_finish_reason="stop")
    result = {"model": model, "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "".join(content),
                    "reasoning_content": "".join(reasoning)}}]}
    if usage is not None:
        result["usage"] = usage
    if response_id is not None:
        result["id"] = response_id
    if created is not None:
        result["created"] = created
    return result, total_bytes
