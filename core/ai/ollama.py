"""Ollama HTTP adapter with finite timeouts, retries, and raw-response tracing."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from ..context import MarketContext
from ..model_routing import DEFAULT_FAST_MODEL
from .contracts import LLMCallMetadata, LLMError, ModelSignalResponse, SignalPolicy
from .prompts import PROMPT_VERSION, build_prompt_messages, build_repair_messages
from ..trading.model_schemas import SIGNAL_SCHEMA


class OllamaProvider:
    provider_name = "ollama"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model_name: str | None = None,
        timeout: float | None = None,
        context_length: int | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int | None = None,
        quantization: str | None = None,
        think: bool | None = None,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.model_name = model_name or os.environ.get("OLLAMA_MODEL", DEFAULT_FAST_MODEL)
        self.default_model = self.model_name
        self.timeout = timeout if timeout is not None else float(os.environ.get("OLLAMA_TIMEOUT_SEC", "45"))
        self.context_length = context_length or int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "8192"))
        self.temperature = temperature if temperature is not None else float(os.environ.get("OLLAMA_TEMPERATURE", "0.2"))
        self.max_tokens = max_tokens or int(os.environ.get("OLLAMA_MAX_TOKENS", "700"))
        self.retries = max(0, min(retries if retries is not None else int(os.environ.get("OLLAMA_RETRIES", "1")), 2))
        self.quantization = quantization or os.environ.get("OLLAMA_QUANTIZATION", "Q4_K_M")
        think_value = os.environ.get("OLLAMA_THINK", "false") if think is None else str(think).lower()
        self.think = think_value in {"1", "true", "yes", "on"}
        self.prompt_version = prompt_version
        self.max_response_chars = int(os.environ.get("OLLAMA_MAX_RESPONSE_CHARS", "12000"))
        # Populated only from the provider's manifest/tag response.  The
        # model name and version are identity fields, never a weight digest.
        self.weight_digest: str | None = None

    def _request(self, method: str, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = Request(
                    f"{self.base_url}{path}",
                    data=body,
                    method=method,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                with urlopen(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise LLMError(f"Ollama returned HTTP {response.status}", code="model_http_error")
                    decoded = json.loads(response.read().decode("utf-8"))
                    if not isinstance(decoded, dict):
                        raise LLMError("Ollama response must be a JSON object", code="model_invalid_envelope")
                    return decoded
            except LLMError:
                raise
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise LLMError(f"Ollama unavailable: {last_error}", code="MODEL_UNAVAILABLE") from last_error

    def health(self) -> dict[str, object]:
        try:
            payload = self._request("GET", "/api/tags")
            models = payload.get("models", [])
            names = [item.get("name") for item in models if isinstance(item, dict) and isinstance(item.get("name"), str)]
            selected = next((item for item in models if isinstance(item, dict) and item.get("name") == self.model_name), {})
            raw_digest = selected.get("digest") if isinstance(selected, dict) else None
            digest = str(raw_digest or "").strip().lower()
            if digest.startswith("sha256:"):
                digest = digest[7:]
            self.weight_digest = digest if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest) else None
            return {
                "provider": self.provider_name,
                "available": True,
                "model_id": self.model_name,
                "model_available": self.model_name in names,
                "models": names,
                "weight_digest": self.weight_digest,
                "digest_status": "OBSERVED_PROVIDER_DIGEST" if self.weight_digest else "UNKNOWN_NOT_PROVIDED",
                "context_length": self.context_length,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "quantization": self.quantization,
                "think": self.think,
            }
        except LLMError as exc:
            return {
                "provider": self.provider_name,
                "available": False,
                "model_id": self.model_name,
                "model_available": False,
                "error_code": exc.code,
                "weight_digest": None,
                "digest_status": "UNKNOWN_NOT_PROVIDED",
                "quantization": self.quantization,
                "think": self.think,
            }

    @staticmethod
    def _content(payload: dict[str, object]) -> str:
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        if isinstance(payload.get("response"), str):
            return str(payload["response"])
        raise LLMError("Ollama response has no message content", code="model_invalid_envelope")

    def generate_json(
        self,
        messages: list[dict[str, str]],
        *,
        model_name: str,
        prompt_version: str,
        input_hash: str,
        temperature: float | None = None,
        schema: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        """Run a bounded structured call against an explicitly selected tier.

        This is intentionally separate from ``analyze_market``: V1.2 callers
        must name the Smart model and receive a hard error if that model is not
        installed.  The adapter never substitutes ``self.model_name``.
        """

        started = time.perf_counter()
        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            # Ollama accepts a JSON schema object here.  Plain JSON is kept
            # only for legacy callers that have no strict contract; all
            # institutional callers pass a closed schema.
            "format": schema or "json",
            "think": self.think,
            "options": {
                "temperature": self.temperature if temperature is None else max(0.0, min(float(temperature), 2.0)),
                "num_ctx": self.context_length,
                "num_predict": self.max_tokens,
            },
        }
        try:
            envelope = self._request("POST", "/api/chat", payload)
            raw = self._content(envelope)
            if len(raw) > self.max_response_chars:
                raise LLMError("Ollama response exceeds output limit", code="output_too_long", raw_response=raw[: self.max_response_chars])
            try:
                decoded = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise LLMError(f"invalid structured JSON: {exc}", code="parse_error", raw_response=raw) from exc
            if not isinstance(decoded, dict):
                raise LLMError("structured model output must be an object", code="parse_error", raw_response=raw)
            elapsed = (time.perf_counter() - started) * 1000.0
            metadata = {
                "model_id": model_name,
                "model_version": str(envelope.get("model") or model_name),
                "prompt_version": prompt_version,
                "input_hash": input_hash,
                "latency_ms": round(elapsed, 3),
                "raw_response": raw,
                "parse_status": "valid",
                "input_tokens_est": sum(len(item.get("content", "")) for item in messages) // 4,
                "output_chars": len(raw),
                "schema_version": "strict_json_schema" if schema else "json_object",
            }
            return decoded, raw, metadata
        except LLMError:
            raise

    def analyze_market(
        self,
        context: MarketContext,
        policy: SignalPolicy,
        *,
        repair: bool = False,
        repair_error: str | None = None,
    ) -> tuple[ModelSignalResponse, LLMCallMetadata]:
        started = time.perf_counter()
        messages = build_repair_messages(context, policy, repair_error or "previous output was invalid") if repair else build_prompt_messages(context, policy)
        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "format": SIGNAL_SCHEMA,
            "think": self.think,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.context_length,
                "num_predict": self.max_tokens,
            },
        }
        try:
            envelope = self._request("POST", "/api/chat", payload)
            raw = self._content(envelope)
            if len(raw) > self.max_response_chars:
                raise LLMError("Ollama response exceeds output limit", code="output_too_long", raw_response=raw[: self.max_response_chars])
            try:
                decoded = json.loads(raw)
                response = ModelSignalResponse.from_dict(decoded)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise LLMError(f"invalid structured JSON: {exc}", code="parse_error", raw_response=raw) from exc
            elapsed = (time.perf_counter() - started) * 1000.0
            metadata = LLMCallMetadata(
                model_id=self.model_name,
                model_version=str(envelope.get("model") or self.model_name),
                prompt_version=self.prompt_version,
                input_hash=context.input_hash(),
                latency_ms=round(elapsed, 3),
                raw_response=raw,
                parse_status="repair_valid" if repair else "valid",
                input_tokens_est=sum(len(item["content"]) for item in messages) // 4,
                output_chars=len(raw),
            )
            return response, metadata
        except LLMError:
            raise
