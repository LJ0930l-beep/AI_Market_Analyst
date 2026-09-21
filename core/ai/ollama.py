"""Compatibility provider adapter pinned to the local Bonsai ModelClient."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from ..context import MarketContext
from ..model_routing import DEFAULT_SMART_MODEL, bonsai_manifest_entry_matches, is_bonsai_model_identity
from .contracts import LLMCallMetadata, LLMError, ModelSignalResponse, SignalPolicy
from .prompts import PROMPT_VERSION, build_prompt_messages, build_repair_messages
from ..trading.model_schemas import SIGNAL_SCHEMA
from ..model_client import model_client


_MODEL_DIGEST_LOCK = threading.Lock()
_MODEL_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


def _local_model_digest(path: str) -> str | None:
    """Hash the exact local GGUF artifact once per stable file identity.

    llama-server's current ``/v1/models`` response omits a digest. Only hash a
    concrete local file path returned by that manifest; never hash an alias,
    model name, URL, or empty payload as a stand-in for model weights.
    """
    normalized = os.path.abspath(os.path.expanduser(path))
    if not os.path.isabs(normalized) or not os.path.isfile(normalized):
        return None
    try:
        before = os.stat(normalized)
    except OSError:
        return None
    key = (os.path.normcase(normalized), int(before.st_size), int(before.st_mtime_ns))
    with _MODEL_DIGEST_LOCK:
        cached = _MODEL_DIGEST_CACHE.get(key)
        if cached:
            return cached
        digest = hashlib.sha256()
        try:
            with open(normalized, "rb") as source:
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            after = os.stat(normalized)
        except OSError:
            return None
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return None
        value = digest.hexdigest()
        _MODEL_DIGEST_CACHE.clear()
        _MODEL_DIGEST_CACHE[key] = value
        return value


def _manifest_model_identity(model: dict[str, object]) -> str:
    for field in ("id", "name", "model"):
        value = model.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _model_alias_matches(target: str, model: dict[str, object]) -> bool:
    """Match the configured alias to one manifest entry without substitution."""
    return bonsai_manifest_entry_matches(model, requested_model=target)


def _positive_context_length(model: dict[str, object]) -> int | None:
    meta = model.get("meta")
    value = meta.get("n_ctx") if isinstance(meta, dict) else model.get("context_length")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_keep_alive(value: object) -> str | int | float | None:
    """Return an Ollama ``keep_alive`` value, or ``None`` to omit the field.

    Ollama accepts a duration string (``"30m"``, ``"1h"``), the literal
    ``-1``/``"-1"`` for "never unload", or a number of seconds.  ``None``
    leaves the server default (about five minutes) in place, which is the
    right answer for call paths that must not pin a multi-gigabyte model.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    # Numeric strings are seconds in Ollama's protocol.  Keeping them numeric
    # matters for "-1", which would otherwise be read as a malformed duration.
    if text.lstrip("-").isdigit():
        return int(text)
    return text


class OllamaProvider:
    provider_name = "bonsai_llama_server"

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
        keep_alive: str | int | float | None = None,
        auto_start: bool = False,
    ) -> None:
        # OLLAMA_* variables are compatibility leftovers only. They are never
        # used as a route or model override; any stale values are surfaced in
        # health while inference stays pinned to Bonsai's configured client.
        default_base_url = os.environ.get("BONSAI_BASE_URL") or model_client.base_url
        default_base_url = default_base_url.rstrip("/")
        default_model_name = os.environ.get("BONSAI_MODEL_NAME") or DEFAULT_SMART_MODEL
        self.base_url = (base_url or default_base_url).rstrip("/")
        self.model_name = model_name or default_model_name
        self.default_model = self.model_name
        self.timeout = timeout if timeout is not None else float(os.environ.get("OLLAMA_TIMEOUT_SEC", "120"))
        self.context_length = context_length or int(os.environ.get("OLLAMA_CONTEXT_LENGTH", "8192"))
        self.temperature = temperature if temperature is not None else float(os.environ.get("OLLAMA_TEMPERATURE", "0.2"))
        self.max_tokens = max_tokens or int(os.environ.get("OLLAMA_MAX_TOKENS", "700"))
        self.retries = max(0, min(retries if retries is not None else int(os.environ.get("OLLAMA_RETRIES", "1")), 2))
        self.quantization = quantization or os.environ.get("OLLAMA_QUANTIZATION", "Q4_K_M")
        think_value = os.environ.get("OLLAMA_THINK", "false") if think is None else str(think).lower()
        self.think = think_value in {"1", "true", "yes", "on"}
        self.prompt_version = prompt_version
        self.max_response_chars = int(os.environ.get("OLLAMA_MAX_RESPONSE_CHARS", "12000"))
        # Retained as an adapter option for older call signatures. The active
        # Bonsai OpenAI-compatible route does not send Ollama keep_alive.
        self.keep_alive = _normalize_keep_alive(
            keep_alive if keep_alive is not None else os.environ.get("OLLAMA_KEEP_ALIVE")
        )
        self.auto_start = bool(auto_start)
        self.rejected_legacy_overrides = [
            name
            for name, value, expected in (
                ("OLLAMA_BASE_URL", os.environ.get("OLLAMA_BASE_URL"), model_client.base_url),
                ("OLLAMA_MODEL", os.environ.get("OLLAMA_MODEL"), DEFAULT_SMART_MODEL),
            )
            if value and value.strip() != expected
        ]
        # Populated only from a provider manifest or the SHA-256 of the exact
        # local model artifact returned by the inference server.
        self.weight_digest: str | None = None

    def _route_error(self, requested_model: str | None = None) -> str | None:
        if self.model_name != DEFAULT_SMART_MODEL or (requested_model is not None and requested_model != DEFAULT_SMART_MODEL):
            return "MODEL_NOT_ALLOWED"
        if self.base_url.rstrip("/") != model_client.base_url.rstrip("/"):
            return "MODEL_ENDPOINT_NOT_ALLOWED"
        return model_client._configuration_error(requested_model or self.model_name)

    def _uses_model_client_bridge(self) -> bool:
        """Whether this adapter is pinned to the configured Bonsai API."""
        return self._route_error() is None

    def _local_server_reachable(self, *, timeout: float = 1.0) -> bool:
        try:
            return self._uses_model_client_bridge() and model_client.is_healthy(timeout=timeout)
        except Exception:
            return False

    def loaded_models(self) -> list[dict[str, object]]:
        """The llama.cpp Bonsai server has no Ollama residency endpoint."""
        return []

    def health(self, *, model_name: str | None = None) -> dict[str, object]:
        target_model = model_name or self.model_name
        route_error = self._route_error(target_model)
        if route_error:
            self.weight_digest = None
            return {
                "provider": "bonsai_llama_server",
                "available": False,
                "model_id": target_model,
                "actual_model_id": None,
                "model_identity_source": None,
                "model_available": False,
                "error_code": route_error,
                "weight_digest": None,
                "digest_status": "UNKNOWN_NOT_PROVIDED",
                "context_length": None,
                "models": [],
                "rejected_legacy_overrides": list(self.rejected_legacy_overrides),
            }
        if not model_client.is_healthy():
            self.weight_digest = None
            return {
                "provider": "bonsai_llama_server",
                "available": False,
                "model_id": target_model,
                "model_available": False,
                "models": [],
                "weight_digest": None,
                "digest_status": "UNKNOWN_NOT_PROVIDED",
                "context_length": None,
                "error_code": "MODEL_UNAVAILABLE",
                "rejected_legacy_overrides": list(self.rejected_legacy_overrides),
            }
        try:
            manifest = model_client.list_models(timeout=2.0)
        except Exception as exc:
            self.weight_digest = None
            return {
                "provider": "bonsai_llama_server",
                "available": True,
                "model_id": target_model,
                "model_available": False,
                "models": [],
                "weight_digest": None,
                "digest_status": "UNKNOWN_NOT_PROVIDED",
                "context_length": None,
                "error_code": "MODEL_MANIFEST_UNAVAILABLE",
                "manifest_error": f"{type(exc).__name__}: {exc}"[:240],
                "rejected_legacy_overrides": list(self.rejected_legacy_overrides),
            }
        rows = [row for row in manifest if isinstance(row, dict)]
        names = list(dict.fromkeys(identity for row in rows if (identity := _manifest_model_identity(row))))
        matching = [row for row in rows if _model_alias_matches(target_model, row)]
        selected = matching[0] if len(matching) == 1 else {}
        context_length = _positive_context_length(selected) if selected else None
        actual_model_id = _manifest_model_identity(selected) if selected else None
        digest = None
        if selected:
            supplied_digest = str(selected.get("digest") or "").strip().lower()
            if supplied_digest.startswith("sha256:"):
                supplied_digest = supplied_digest[7:]
            if len(supplied_digest) == 64 and all(char in "0123456789abcdef" for char in supplied_digest):
                digest = supplied_digest
            elif actual_model_id and actual_model_id.lower().endswith(".gguf"):
                digest = _local_model_digest(actual_model_id)
        self.weight_digest = digest
        return {
            "provider": "bonsai_llama_server",
            "available": True,
            "model_id": target_model,
            "actual_model_id": actual_model_id,
            "model_identity_source": "verified_manifest",
            "model_available": len(matching) == 1,
            "models": names,
            "weight_digest": digest,
            "digest_status": "OBSERVED_LOCAL_ARTIFACT_SHA256" if digest else "UNKNOWN_NOT_PROVIDED",
            "context_length": context_length,
            "configured_context_length": self.context_length,
            "rejected_legacy_overrides": list(self.rejected_legacy_overrides),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "quantization": (
                str((selected.get("meta") or {}).get("ftype"))
                if isinstance(selected.get("meta"), dict) and (selected.get("meta") or {}).get("ftype")
                else "UNKNOWN_NOT_PROVIDED"
            ),
            "think": self.think,
            "server_recovered": False,
        }

    def generate_json(
        self,
        messages: list[dict[str, str]],
        *,
        model_name: str,
        prompt_version: str,
        input_hash: str,
        temperature: float | None = None,
        schema: dict[str, object] | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        """Run one structured call through the verified Bonsai ModelClient.

        The manifest is checked immediately before inference. If the server
        omits a completion model id, metadata explicitly binds the requested
        alias to that verified manifest; it never claims the completion itself
        returned the identity.
        """
        route_error = self._route_error(model_name)
        if route_error:
            raise LLMError(route_error, code=route_error)
        health = self.health(model_name=model_name)
        if not health.get("available") or not health.get("model_available"):
            raise LLMError(
                str(health.get("error_code") or "MODEL_UNAVAILABLE"),
                code=str(health.get("error_code") or "MODEL_UNAVAILABLE"),
            )
        output_tokens = self.max_tokens if max_tokens is None else max_tokens
        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or not 1 <= output_tokens <= 2048:
            raise LLMError("MODEL_MAX_TOKENS_INVALID", code="MODEL_MAX_TOKENS_INVALID")
        started = time.perf_counter()
        try:
            decoded = model_client.structured_analysis(
                messages,
                schema=schema,
                mode="FAST",
                max_tokens=output_tokens,
                temperature_override=(
                    self.temperature
                    if temperature is None
                    else max(0.0, min(float(temperature), 2.0))
                ),
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                timeout_sec=self.timeout,
                retries=self.retries,
            )
        except Exception as exc:
            raise LLMError(f"Bonsai inference error: {exc}", code="MODEL_INFERENCE_ERROR") from exc
        if not isinstance(decoded, dict):
            raise LLMError("structured model output must be an object", code="parse_error")
        response_model = model_client.last_response_model
        if response_model is not None and not is_bonsai_model_identity(response_model):
            raise LLMError("MODEL_RESPONSE_IDENTITY_MISMATCH", code="MODEL_RESPONSE_IDENTITY_MISMATCH")
        raw = json.dumps(decoded, ensure_ascii=False)
        elapsed = (time.perf_counter() - started) * 1000.0
        actual_model_id = response_model or str(health.get("actual_model_id") or "")
        metadata = {
            "model_id": DEFAULT_SMART_MODEL,
            "model_version": actual_model_id or None,
            "actual_model_id": actual_model_id or None,
            "model_identity_source": "completion_response" if response_model else "request_bound_to_verified_manifest",
            "verified_manifest_model_id": health.get("actual_model_id"),
            "prompt_version": prompt_version,
            "input_hash": input_hash,
            "latency_ms": round(elapsed, 3),
            "raw_response": raw,
            "parse_status": "valid",
            "input_tokens_est": sum(len(item.get("content", "")) for item in messages) // 4,
            "output_chars": len(raw),
            "reasoning_effort": reasoning_effort,
            # The ModelClient uses the OpenAI-compatible JSON-object mode;
            # schema contracts are still validated by domain callers.
            "schema_version": "json_object",
            "schema_enforcement": "json_object",
        }
        return decoded, raw, metadata

    def analyze_market(
        self,
        context: MarketContext,
        policy: SignalPolicy,
        *,
        repair: bool = False,
        repair_error: str | None = None,
    ) -> tuple[ModelSignalResponse, LLMCallMetadata]:
        messages = build_repair_messages(context, policy, repair_error or "previous output was invalid") if repair else build_prompt_messages(context, policy)
        decoded, raw, receipt = self.generate_json(
            messages,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            input_hash=context.input_hash(),
            temperature=self.temperature,
            schema=SIGNAL_SCHEMA,
        )
        try:
            response = ModelSignalResponse.from_dict(decoded)
        except (TypeError, ValueError) as exc:
            raise LLMError(f"invalid structured JSON: {exc}", code="parse_error", raw_response=raw) from exc
        metadata = LLMCallMetadata(
            model_id=DEFAULT_SMART_MODEL,
            model_version=str(receipt.get("actual_model_id") or "") or None,
            prompt_version=self.prompt_version,
            input_hash=context.input_hash(),
            latency_ms=float(receipt["latency_ms"]),
            raw_response=raw,
            parse_status="repair_valid" if repair else "valid",
            input_tokens_est=int(receipt.get("input_tokens_est") or 0),
            output_chars=len(raw),
            schema_enforcement=str(receipt.get("schema_enforcement") or "json_object"),
            actual_model_id=str(receipt.get("actual_model_id") or "") or None,
            model_identity_source=str(receipt.get("model_identity_source") or "") or None,
            verified_manifest_model_id=str(receipt.get("verified_manifest_model_id") or "") or None,
        )
        return response, metadata
