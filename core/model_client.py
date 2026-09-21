"""Unified ModelClient for AI Market Analyst V2.

All Agents and sub-systems interact with the reasoning model solely via ModelClient.
Provides:
- OpenAI-compatible /v1/chat/completions protocol
- Mode A (ANALYSIS, thinking enabled) vs Mode B (FAST, thinking disabled)
- Tool calling roundtrips
- Robust retries with exponential backoff
- JSON output validation and repair
- Token usage tracking and latency metrics
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, Generator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from .config import config, GenerationPreset
from .logging import get_logger
from .model_routing import DEFAULT_MODEL, is_bonsai_model_identity

logger = get_logger("model_client")


class ModelClientError(Exception):
    """Base error for ModelClient operations."""
    pass


class ModelTimeoutError(ModelClientError):
    pass


class ModelSchemaError(ModelClientError):
    pass


class ModelClient:
    """Authoritative client for Bonsai 2 27B OpenAI-compatible inference server."""

    def __init__(
        self,
        base_url: str | None = None,
        model_name: str | None = None,
        timeout_sec: float | None = None,
        retries: int = 2,
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
                or parsed.port != 8080
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

    def _assert_bonsai_route(self, requested_model: str | None = None) -> str:
        error = self._configuration_error(requested_model)
        if error:
            raise ModelClientError(error)
        return DEFAULT_MODEL

    def count_tokens(self, content: str, *, timeout_sec: float = 5.0) -> int:
        """Count text with the tokenizer of the pinned local Bonsai runtime.

        The /tokenize endpoint is read-only and is constrained to the same
        loopback Bonsai route as inference. Callers may fall back to a
        conservative estimator when the endpoint is unavailable.
        """
        self._assert_bonsai_route()
        if not isinstance(content, str):
            raise ModelClientError("MODEL_TOKENIZER_INPUT_INVALID")
        if len(content) > 250_000:
            raise ModelClientError("MODEL_TOKENIZER_INPUT_TOO_LARGE")
        if (
            isinstance(timeout_sec, bool)
            or not isinstance(timeout_sec, (int, float))
            or not math.isfinite(timeout_sec)
            or timeout_sec <= 0
        ):
            raise ModelClientError("MODEL_TOKENIZER_TIMEOUT_INVALID")
        tokenizer_url = f"{self.base_url.rsplit('/v1', 1)[0]}/tokenize"
        body = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
        request = Request(
            tokenizer_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AI-Market-Analyst-V2/Tokenizer",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=float(timeout_sec)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ModelClientError("MODEL_TOKENIZER_UNAVAILABLE") from exc
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        if not isinstance(tokens, list):
            raise ModelClientError("MODEL_TOKENIZER_RESPONSE_INVALID")
        return len(tokens)

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
    ) -> Dict[str, Any]:
        """Execute a standard chat completion call."""
        self._response_state.model = None
        selected_model = self._assert_bonsai_route(model_name)
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
            "top_k": preset.top_k,  # llama.cpp standard sampling param
            "max_tokens": actual_max_tokens,
        }
        # The pinned PrismML llama.cpp build advertises reasoning-effort support
        # in /props; keep the option opt-in so only scoped callers change it.
        if reasoning_effort is not None:
            normalized_effort = str(reasoning_effort).strip().lower()
            if normalized_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
                raise ModelClientError("MODEL_REASONING_EFFORT_INVALID")
            payload["reasoning_effort"] = normalized_effort

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
        
        last_exception = None
        for attempt in range(request_retries + 1):
            start_t = time.time()
            try:
                body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = Request(
                    self._endpoint,
                    data=body_bytes,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Authorization": f"Bearer {config.api_key}",
                        "User-Agent": "AI-Market-Analyst-V2/Bonsai",
                    },
                    method="POST",
                )
                with urlopen(req, timeout=request_timeout) as resp:
                    raw_data = resp.read().decode("utf-8")
                    latency_ms = (time.time() - start_t) * 1000.0
                    parsed = json.loads(raw_data)
                    if not isinstance(parsed, dict):
                        raise ModelSchemaError("Model response must be a JSON object")
                    response_model = parsed.get("model")
                    self._response_state.model = None
                    if "model" in parsed:
                        if not isinstance(response_model, str) or not response_model.strip() or not is_bonsai_model_identity(response_model):
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
                    return parsed
            except ModelClientError:
                raise
            except (HTTPError, URLError, TimeoutError) as exc:
                last_exception = exc
                latency_ms = (time.time() - start_t) * 1000.0
                logger.warning(
                    "Model request attempt %d/%d failed in %.2fms: %s",
                    attempt + 1, request_retries + 1, latency_ms, exc
                )
                if attempt < request_retries:
                    time.sleep(1.0 * (2 ** attempt))
            except json.JSONDecodeError as exc:
                raise ModelSchemaError(f"Model returned invalid JSON: {exc}") from exc
            except Exception as exc:
                raise ModelClientError(f"Unexpected error calling model: {exc}") from exc

        raise ModelTimeoutError(
            f"Failed to communicate with model at {self._endpoint} after {request_retries + 1} attempts: {last_exception}"
        ) from last_exception

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
        selected_model = self._assert_bonsai_route(model_name)
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
            "top_k": preset.top_k,
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
            with urlopen(req, timeout=timeout_sec or self.timeout_sec) as resp:
                if on_response_open is not None:
                    on_response_open(resp)
                for line_bytes in resp:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    line = line_bytes.decode("utf-8").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        if isinstance(chunk, dict) and "model" in chunk:
                            response_model = chunk.get("model")
                            if not isinstance(response_model, str) or not response_model.strip() or not is_bonsai_model_identity(response_model):
                                raise ModelClientError("MODEL_RESPONSE_IDENTITY_MISMATCH")
                            self._response_state.model = response_model.strip()
                        yield chunk
                    except json.JSONDecodeError:
                        continue
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
    ) -> Dict[str, Any]:
        """Request a JSON object; callers remain responsible for schema validation."""
        self._assert_bonsai_route(model_name)
        req_messages = [dict(m) for m in messages]
        if schema:
            has_compact_contract = any(
                item.get("role") == "system" and "JSON字段规则：" in str(item.get("content") or "")
                for item in req_messages
            )
            if not has_compact_contract:
                schema_instruction = (
                    "JSON Schema (return one matching JSON object):\n"
                    + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                )
                if req_messages and req_messages[0].get("role") == "system":
                    req_messages[0]["content"] = req_messages[0]["content"] + "\n\n" + schema_instruction
                else:
                    req_messages.insert(0, {"role": "system", "content": schema_instruction})

        # Use universal json_object format to avoid llama.cpp grammar parser 400 errors
        response_format = {"type": "json_object"}

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
        )
        
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
                "Bonsai returned empty final content (finish_reason=%s, reasoning_chars=%s, completion_tokens=%s)",
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
        self._assert_bonsai_route()
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
