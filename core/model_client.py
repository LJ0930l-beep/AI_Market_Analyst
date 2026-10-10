"""Unified ModelClient for AI Market Analyst V2.

All Agents and sub-systems interact with the reasoning model solely via ModelClient.
Provides:
- OpenAI-compatible /v1/chat/completions protocol
- Mode A (ANALYSIS, thinking enabled) vs Mode B (FAST, thinking disabled)
- Tool calling roundtrips
- Single-attempt requests; retries are rejected until provider idempotency is verified
- JSON output validation and repair
- Token usage tracking and latency metrics
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from typing import Any, Callable, Dict, Generator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen as _stdlib_urlopen
from urllib.parse import urlparse

from .config import config, GenerationPreset
from .logging import get_logger
from .model_routing import DEFAULT_MODEL, is_configured_model_identity
from .completion_stream import CompletionStreamError, collect_completion_stream

logger = get_logger("model_client")


def urlopen(request, *, timeout):
    """Keep the injectable opener contract; trace only completion requests."""
    trace = getattr(request, "_completion_transport_trace", None)
    if trace is None:
        return _stdlib_urlopen(request, timeout=timeout)
    # Deferred import preserves the standalone model_client entry point.
    from .ai.transport_diagnostics import open_traced_request
    return open_traced_request(request, timeout=timeout, trace=trace)


class ModelClientError(Exception):
    """Base error for ModelClient operations."""
    pass


class ModelTimeoutError(ModelClientError):
    pass


class ModelSchemaError(ModelClientError):
    def __init__(self, message: str, *, raw_response: str | None = None, finish_reason: str | None = None):
        super().__init__(message)
        self.raw_response = raw_response
        self.finish_reason = finish_reason


class ModelClient:
    """Authoritative client for Gemini 3.8 Flash OpenAI-compatible inference server."""

    def __init__(
        self,
        base_url: str | None = None,
        model_name: str | None = None,
        timeout_sec: float | None = None,
        retries: int = 0,
    ) -> None:
        self.base_url = (base_url or config.base_url).rstrip("/")
        self.model_name = model_name or config.model_name
        self.timeout_sec = timeout_sec or config.timeout_sec
        self.retries = retries
        self._endpoint = f"{self.base_url}/chat/completions"
        self._response_state = threading.local()

    @property
    def last_response_model(self) -> str | None:
        """Actual model identity returned by this thread's last completion."""
        value = getattr(self._response_state, "model", None)
        return str(value) if isinstance(value, str) and value.strip() else None

    @property
    def last_schema_enforcement(self) -> str | None:
        return getattr(self._response_state, "schema_enforcement", None)

    @property
    def last_transport_trace(self) -> dict[str, Any] | None:
        trace = getattr(self._response_state, "transport_trace", None)
        return trace.snapshot() if trace is not None else None

    def _configuration_error(self, requested_model: str | None = None) -> str | None:
        if self.model_name != DEFAULT_MODEL:
            return "MODEL_CONFIGURATION_MISMATCH"
        if requested_model is not None and requested_model != DEFAULT_MODEL:
            return "MODEL_NOT_ALLOWED"
        if requested_model is not None and requested_model != self.model_name:
            return "MODEL_CONFIGURATION_MISMATCH"
        try:
            parsed = urlparse(self.base_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port != 8045
                or parsed.path.rstrip("/") != "/v1"
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                return "MODEL_ENDPOINT_NOT_ALLOWED"
        except ValueError:
            return "MODEL_ENDPOINT_NOT_ALLOWED"
        return None

    def _assert_model_route(self, requested_model: str | None = None) -> str:
        error = self._configuration_error(requested_model)
        if error:
            raise ModelClientError(error)
        return DEFAULT_MODEL

    def count_tokens(self, content: str, *, timeout_sec: float = 5.0) -> int:
        """The relay has no verified tokenizer; callers use their labeled estimator."""
        self._assert_model_route()
        raise ModelClientError("MODEL_TOKENIZER_UNAVAILABLE_REMOTE_PROVIDER")

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        *,
        mode: str = "ANALYSIS",
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str | Dict[str, Any]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        stream: bool = False,
        temperature_override: Optional[float] = None,
        max_tokens_override: Optional[int] = None,
        max_tokens: Optional[int] = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        timeout_sec: float | None = None,
        retries: int | None = None,
        request_id: str | None = None,
    ) -> Dict[str, Any]:
        """Execute a standard chat completion call."""
        # Providers import this module; load their package after client initialization.
        from .ai.transport_diagnostics import CompletionTransportTrace

        self._response_state.model = None
        selected_model = self._assert_model_route(model_name)
        if not isinstance(stream, bool):
            raise ModelClientError("MODEL_STREAM_FLAG_INVALID")
        preset = config.get_preset(mode)
        actual_max_tokens = max_tokens if max_tokens is not None else max_tokens_override
        if actual_max_tokens is None:
            actual_max_tokens = preset.max_tokens
        
        payload: Dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature_override if temperature_override is not None else preset.temperature,
            "top_p": preset.top_p,
            "max_tokens": actual_max_tokens,
        }
        # Preserve older callers while keeping the operator-selected High tier.
        if reasoning_effort is not None:
            normalized_effort = str(reasoning_effort).strip().lower()
            if normalized_effort in {"none", "minimal"}:
                normalized_effort = "high"
            if normalized_effort not in {"low", "medium", "high"}:
                raise ModelClientError("MODEL_REASONING_EFFORT_INVALID")
            payload["reasoning_effort"] = "high"  # The selected release is explicitly High.

        payload["reasoning_effort"] = "high"

        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        if response_format:
            payload["response_format"] = response_format

        request_timeout = self.timeout_sec if timeout_sec is None else timeout_sec
        if (
            isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(request_timeout)
            or request_timeout <= 0
        ):
            raise ModelClientError("MODEL_TIMEOUT_INVALID")
        request_retries = self.retries if retries is None else retries
        if isinstance(request_retries, bool) or not isinstance(request_retries, int) or request_retries < 0:
            raise ModelClientError("MODEL_RETRIES_INVALID")
        if request_retries != 0:
            raise ModelClientError("MODEL_RETRY_UNSAFE_WITHOUT_IDEMPOTENCY")

        if request_id is None:
            request_id = uuid.uuid4().hex
        elif (not isinstance(request_id, str) or len(request_id) != 32
              or any(character not in "0123456789abcdef" for character in request_id)):
            raise ModelClientError("MODEL_REQUEST_ID_INVALID")
        last_exception = None
        for attempt in range(request_retries + 1):
            start_t = time.time()
            trace = CompletionTransportTrace(messages, attempt + 1, request_timeout,
                                             request_id=request_id)
            trace.data["transport_mode"] = "SSE" if stream else "JSON"
            self._response_state.transport_trace = trace
            try:
                body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = Request(
                    self._endpoint,
                    data=body_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream" if stream else "application/json",
                        "Authorization": f"Bearer {config.api_key}",
                        "User-Agent": "AI-Market-Analyst-V2/Gemini",
                        "X-AI-Correlation-ID": trace.data["correlation_id"],
                        "X-AI-Request-ID": trace.data["request_id"],
                        "X-AI-Attempt-ID": trace.data["attempt_id"],
                    },
                    method="POST",
                )
                req._completion_transport_trace = trace
                trace.awaiting_headers()
                with urlopen(req, timeout=request_timeout) as resp:
                    trace.headers_received(getattr(resp, "status", None))
                    if stream:
                        try:
                            parsed, response_size = collect_completion_stream(
                                resp, deadline=trace.started + request_timeout,
                                identity_check=is_configured_model_identity, trace=trace,
                                bounded_ttl_text=bool(response_format and
                                    response_format.get("json_schema", {}).get("schema", {}).get(
                                        "properties", {}).get("ttl_seconds", {}).get("type") in ("string", "object")),
                            )
                        except CompletionStreamError as exc:
                            raise ModelSchemaError(str(exc)) from exc
                        trace.body_received(response_size)
                    else:
                        response_bytes = resp.read()
                        trace.body_received(len(response_bytes))
                        raw_data = response_bytes.decode("utf-8")
                        parsed = json.loads(raw_data)
                    latency_ms = (time.time() - start_t) * 1000.0
                    if not isinstance(parsed, dict):
                        raise ModelSchemaError("Model response must be a JSON object")
                    response_model = parsed.get("model")
                    self._response_state.model = None
                    if not is_configured_model_identity(response_model):
                        raise ModelClientError("MODEL_RESPONSE_IDENTITY_MISMATCH")
                    self._response_state.model = response_model.strip()
                    logger.debug(
                        "Chat completion succeeded in %.2fms (mode=%s, tokens=%s)",
                        latency_ms, mode, parsed.get("usage", {}).get("total_tokens")
                    )
                    # Inject convenient top-level accessors for callers
                    choices = parsed.get("choices") or []
                    if choices:
                        msg = choices[0].get("message", {})
                        parsed["content"] = msg.get("content", "")
                        parsed["thinking"] = msg.get("reasoning_content", "")
                    else:
                        parsed["content"] = ""
                        parsed["thinking"] = ""
                    trace.completed()
                    return parsed
            except ModelClientError as exc:
                trace.failed(exc)
                exc.transport_trace = trace.snapshot()
                raise
            except HTTPError as exc:
                trace.failed(exc)
                try:
                    remote = json.loads(exc.read(16384).decode("utf-8"))
                    message = str((remote.get("error") or {}).get("message") or "")
                except (ValueError, UnicodeError, AttributeError):
                    message = ""
                if "location is not supported" in message.lower():
                    raise ModelClientError("MODEL_UPSTREAM_REGION_UNSUPPORTED") from exc
                if exc.code in {400, 401, 403, 404}:
                    raise ModelClientError(f"MODEL_UPSTREAM_HTTP_{exc.code}") from exc
                last_exception = exc
                if attempt < request_retries:
                    time.sleep(1.0 * (2 ** attempt))
            except (URLError, TimeoutError) as exc:
                trace.failed(exc)
                last_exception = exc
                latency_ms = (time.time() - start_t) * 1000.0
                logger.warning(
                    "Model request attempt %d/%d failed in %.2fms: %s",
                    attempt + 1, request_retries + 1, latency_ms, exc
                )
                if attempt < request_retries:
                    time.sleep(1.0 * (2 ** attempt))
            except json.JSONDecodeError as exc:
                trace.failed(exc)
                raise ModelSchemaError(f"Model returned invalid JSON: {exc}") from exc
            except Exception as exc:
                trace.failed(exc)
                failure = ModelClientError(f"Unexpected error calling model: {exc}")
                failure.transport_trace = trace.snapshot()
                raise failure from exc

        failure = ModelTimeoutError(
            f"Failed to communicate with model at {self._endpoint} after {request_retries + 1} attempts: {last_exception}"
        )
        failure.transport_trace = self.last_transport_trace
        raise failure from last_exception

    def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        *,
        mode: str = "ANALYSIS",
        temperature_override: Optional[float] = None,
        model_name: str | None = None,
        timeout_sec: float | None = None,
        max_tokens: int | None = None,
        cancel_event: threading.Event | None = None,
        on_response_open: Callable[[Any], None] | None = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Stream chunks from OpenAI-compatible SSE stream."""
        self._response_state.model = None
        selected_model = self._assert_model_route(model_name)
        preset = config.get_preset(mode)
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 2048
        ):
            raise ModelClientError("MODEL_MAX_TOKENS_INVALID")
        payload = {
            "model": selected_model,
            "messages": messages,
            "stream": True,
            "temperature": temperature_override if temperature_override is not None else preset.temperature,
            "top_p": preset.top_p,
            "reasoning_effort": "high",
            "max_tokens": max_tokens if max_tokens is not None else preset.max_tokens,
        }
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self._endpoint,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {config.api_key}",
            },
            method="POST",
        )
        try:
            finish_seen = False
            done_seen = False
            data_lines: list[str] = []

            def parse_event(data: str) -> dict[str, Any] | None:
                nonlocal finish_seen
                if data == "[DONE]":
                    if not finish_seen:
                        raise ModelClientError("MODEL_STREAM_FINISH_MISSING")
                    return None
                if done_seen:
                    raise ModelClientError("MODEL_STREAM_DATA_AFTER_DONE")
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ModelClientError("MODEL_STREAM_EVENT_INVALID") from exc
                if not isinstance(chunk, dict):
                    raise ModelClientError("MODEL_STREAM_EVENT_INVALID")
                if chunk.get("error") is not None:
                    raise ModelClientError("MODEL_STREAM_PROVIDER_ERROR")

                choices = chunk.get("choices")
                if choices is not None:
                    if not isinstance(choices, list):
                        raise ModelClientError("MODEL_STREAM_EVENT_INVALID")
                    if finish_seen and choices:
                        raise ModelClientError("MODEL_STREAM_DATA_AFTER_FINISH")
                    for choice in choices:
                        if not isinstance(choice, dict):
                            raise ModelClientError("MODEL_STREAM_EVENT_INVALID")
                        finish_reason = choice.get("finish_reason")
                        if finish_reason is not None:
                            if finish_reason != "stop":
                                raise ModelClientError("MODEL_STREAM_FINISH_INVALID")
                            if finish_seen:
                                raise ModelClientError("MODEL_STREAM_FINISH_INVALID")
                            finish_seen = True

                if "model" in chunk:
                    response_model = chunk.get("model")
                    if (
                        not isinstance(response_model, str)
                        or not response_model.strip()
                        or not is_configured_model_identity(response_model)
                    ):
                        raise ModelClientError("MODEL_RESPONSE_IDENTITY_MISMATCH")
                    self._response_state.model = response_model.strip()
                return chunk

            with urlopen(req, timeout=timeout_sec or self.timeout_sec) as resp:
                if on_response_open is not None:
                    on_response_open(resp)
                for line_bytes in resp:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    try:
                        lines = line_bytes.decode("utf-8").splitlines()
                    except UnicodeDecodeError as exc:
                        raise ModelClientError("MODEL_STREAM_EVENT_INVALID") from exc
                    for line in lines:
                        if not line:
                            if not data_lines:
                                continue
                            data = "\n".join(data_lines)
                            data_lines.clear()
                            if data == "[DONE]":
                                if not finish_seen:
                                    raise ModelClientError("MODEL_STREAM_FINISH_MISSING")
                                if done_seen:
                                    raise ModelClientError("MODEL_STREAM_DATA_AFTER_DONE")
                                done_seen = True
                                continue
                            chunk = parse_event(data)
                            if chunk is not None:
                                yield chunk
                            continue
                        if line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            value = line[5:]
                            data_lines.append(value.removeprefix(" "))
                if cancel_event is not None and cancel_event.is_set():
                    return
                if not done_seen:
                    raise ModelClientError("MODEL_STREAM_EOF_BEFORE_DONE")
        except ModelClientError:
            raise
        except Exception as exc:
            raise ModelClientError(f"Stream interrupted: {exc}") from exc

    def structured_analysis(
        self,
        messages: List[Dict[str, Any]],
        *,
        schema: Optional[Dict[str, Any]] = None,
        mode: str = "FAST",
        max_tokens: Optional[int] = None,
        temperature_override: Optional[float] = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        timeout_sec: float | None = None,
        retries: int | None = None,
        allow_syntax_repair: bool = True,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """Request a JSON object; callers remain responsible for schema validation."""
        self._assert_model_route(model_name)
        if not isinstance(allow_syntax_repair, bool):
            raise ModelClientError("MODEL_SYNTAX_REPAIR_FLAG_INVALID")
        # Generation and execution representations differ only for canonical
        # TTL digits. Include the generation schema in uncompressed prompts too.
        frozen_schema = bool(schema and schema.get("$id") == "aima:frozen-decision:v1")
        from .trading.model_schemas import gemini_response_format
        response_format = (gemini_response_format(schema)
                           if frozen_schema else {"type": "json_object"})
        req_messages = [dict(m) for m in messages]
        if schema:
            has_compact_contract = any(
                item.get("role") == "system" and "JSON字段规则：" in str(item.get("content") or "")
                for item in req_messages
            )
            if not has_compact_contract:
                schema_instruction = (
                    "JSON Schema (return one matching JSON object):\n"
                    + json.dumps(response_format["json_schema"]["schema"] if frozen_schema else schema,
                                 ensure_ascii=False, separators=(",", ":"))
                )
                if req_messages and req_messages[0].get("role") == "system":
                    req_messages[0]["content"] = req_messages[0]["content"] + "\n\n" + schema_instruction
                else:
                    req_messages.insert(0, {"role": "system", "content": schema_instruction})

        # Keep the relay-compatible schema separate from local conditional
        # validation; the relay normalizes bounds and cannot attest a grammar.
        self._response_state.schema_enforcement = None

        response = self.chat_completion(
            messages=req_messages,
            mode=mode,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature_override=temperature_override,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            timeout_sec=timeout_sec,
            retries=retries,
            stream=stream,
        )
        self._response_state.schema_enforcement = response_format["type"]
        
        choices = response.get("choices") or []
        if not choices:
            raise ModelSchemaError("Model returned empty choices array")
            
        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        message = first_choice.get("message", {})
        message = message if isinstance(message, dict) else {}
        content = message.get("content", "")
        if not isinstance(content, str):
            raise ModelSchemaError("Model returned non-text completion content")
        # Clean potential markdown fences ```json ... ```
        cleaned = content.strip()
        if not cleaned:
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            reasoning_content = message.get("reasoning_content")
            logger.error(
                "Gemini returned empty final content (finish_reason=%s, reasoning_chars=%s, completion_tokens=%s)",
                first_choice.get("finish_reason"),
                len(reasoning_content) if isinstance(reasoning_content, str) else 0,
                usage.get("completion_tokens"),
            )
            raise ModelSchemaError("Model returned empty completion content")
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
            
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            if not allow_syntax_repair:
                raise ModelSchemaError(f"Model returned invalid JSON: {exc}", raw_response=content,
                                       finish_reason=first_choice.get("finish_reason")) from exc
            # Self-repair attempt using FAST mode
            logger.warning("JSON decode failed (%s). Triggering self-repair...", exc)
            repair_messages = [
                {"role": "system", "content": "You are a JSON repair specialist. Output ONLY the valid JSON object fixing syntax errors."},
                {"role": "user", "content": f"Fix the syntax error in this JSON string:\n{content[:2000]}"},
            ]
            repair_resp = self.chat_completion(
                repair_messages,
                mode="FAST",
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                temperature_override=temperature_override,
                model_name=model_name,
                timeout_sec=timeout_sec,
                retries=retries,
                stream=stream,
            )
            repair_choices = repair_resp.get("choices") or []
            if not repair_choices or not isinstance(repair_choices[0], dict):
                raise ModelSchemaError("Model JSON self-repair returned empty choices")
            repair_message = repair_choices[0].get("message", {})
            repair_message = repair_message if isinstance(repair_message, dict) else {}
            repaired_content = repair_message.get("content", "")
            if not isinstance(repaired_content, str) or not repaired_content.strip():
                raise ModelSchemaError("Model JSON self-repair returned empty completion content")
            try:
                return json.loads(repaired_content.strip())
            except json.JSONDecodeError as repair_exc:
                raise ModelSchemaError(f"Model JSON self-repair returned invalid JSON: {repair_exc}") from repair_exc

    def is_healthy(self, timeout: float = 2.0) -> bool:
        """Probe the server's /health endpoint."""
        if self._configuration_error():
            return False
        try:
            health_url = f"{self.base_url.rsplit('/v1', 1)[0]}/health"
            req = Request(health_url, headers={"User-Agent": "AI-Market-Analyst-V2/HealthCheck"})
            with urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_models(self, timeout: float = 2.0) -> List[Dict[str, Any]]:
        """Read the inference server's actual OpenAI-compatible model manifest.

        A successful ``/health`` probe proves only that the server process is
        alive. Trading readiness also needs the loaded model identity and its
        effective (not training) context window, so callers can fail closed
        when those facts are absent.
        """
        self._assert_model_route()
        req = Request(
            f"{self.base_url}/models",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.api_key}",
                "User-Agent": "AI-Market-Analyst-V2/ModelManifest",
            },
            method="GET",
        )
        with urlopen(req, timeout=timeout or self.timeout_sec) as resp:
            if resp.status != 200:
                raise ModelClientError(f"Model manifest returned HTTP {resp.status}")
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ModelSchemaError("Model manifest must be a JSON object")
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
        if not isinstance(rows, list):
            raise ModelSchemaError("Model manifest has no data/models list")
        return [dict(row) for row in rows if isinstance(row, dict)]


# Shared default client instance
model_client = ModelClient()


def get_model_client() -> ModelClient:
    return model_client
