# V39 Offline SSE Fault-Injection Follow-up

Date: 2026-10-09
Status: **LOCAL STRUCTURED-COMPLETION FAULT TESTS PASS / REMOTE PROVIDER SEMANTICS BLOCKED**

## Scope and safety

This follow-up hardens and exercises the bounded structured-completion SSE path used by research calls. Tests use in-memory fixtures and an ephemeral `127.0.0.1` HTTP server only. They make no Gemini/provider request, private exchange request, TestNet/Live session, order, or production risk-setting change.

The earlier V39 retry-safety boundary remains in force: completion POSTs use one attempt, and any nonzero retry configuration is rejected before opening a request. Request and attempt identifiers are correlation metadata, **not** provider idempotency keys and not proof of exactly-once inference.

## Changes

- Every structured completion invocation has a 32-character `request_id` (also retained as `correlation_id`). Every transport attempt receives a distinct `attempt_id`; both are sent as headers and exported only after format/binding validation. Existing correlation consumers remain compatible.
- The stream collector rejects a repeated frame only when it has the same explicit, nonempty SSE event ID and byte-identical `data` payload. It does not collapse identical text deltas without IDs or distinct payloads that reuse an ID.
- Transport evidence records bytes read and elapsed body-read time as chunks arrive. `stream_done` is explicitly `false` until the complete terminal marker is validated; partial and failed streams never yield a completion result. Safe stable failure codes are retained without response content.
- Unexpected socket errors now carry the sanitized transport trace through the client error envelope.
- The structured-stream test now asserts `stream_done is False` for rejected/incomplete responses, strengthening its previous assertion that the field was absent.

## Verification

- Focused Gate 3 command: **146 passed**, one existing Starlette/httpx deprecation warning.

  ```text
  python -m pytest -q tests/test_gemini_transport_diagnostics.py tests/test_model_client_stream.py tests/test_completion_stream.py tests/test_completion_connection_trace.py tests/test_gemini_relay.py tests/test_post_v1_consult.py tests/test_gemini_structured_stream.py tests/test_v39_sse_fault_injection.py
  ```

- Loopback fault injection covers: TCP reset after HTTP 200 and partial content; truncated JSON; exact duplicate explicit SSE event; provider finish with a non-`stop` reason; body-read stall/timeout; and a complete successful stream. Failed cases retain HTTP 200, attempt 1, byte/time evidence, and a safe failure code; no case returns partial content or triggers a second request.
- Full suite: **2,410 passed, 1 skipped, 0 failed**. The one warning is the existing Starlette/httpx `TestClient` deprecation.
- Exact same-environment comparison against frozen V37.1 test evidence at `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**. All 100 inherited failure nodes remain resolved; 87 post-anchor test nodes pass (including prior V38–V41 additions and 11 nodes from this follow-up); there are no missing baseline nodes, new failures, or phase changes.
- `ruff check tests/test_v39_sse_fault_injection.py`, `compileall` for changed Python files, and `git diff --check` pass. A Ruff differential over six changed tracked Python files reports 49 diagnostics at the parent and 49 after the patch: 0 new and 0 removed. Existing lint debt was not mass-formatted as part of this fix.
- Git-ignored machine evidence:
  - `reports/v38+/verification/v39-offline-fault-injection-final2-20261009.json` — SHA-256 `053258a559fe5598cd59844e4caea2213026945feab1047d1c870a7a2950e39a`
  - `reports/v38+/verification/v39-offline-fault-injection-final2-20261009-vs-baseline.json` — SHA-256 `d677626a4417b5052cca6b8d89669dd13df09e72aff3111d5a7b670b1f328f12`

## Remaining limits

- No real Gemini endpoint was contacted. Provider-side acceptance, event replay behavior, cancellation, relay buffering, TLS termination, billing, and deduplication after the provider accepted a request but its response was lost remain unverified.
- The explicit-ID duplicate guard only identifies byte-identical repeats with an explicit SSE ID. When no ID exists, identical deltas can be legitimate and are preserved; no generic content-based deduplication is claimed.
- `chat_completion_stream`, used by the separate progressive consultation endpoint, remains a distinct incremental API with its own consultation timeout/cancellation tests; this follow-up does not change its user-visible chunk semantics.
- V39 therefore remains **PARTIAL** at the remote provider boundary. Before any retry or unattended model operation, require an authoritative provider idempotency contract or an explicitly approved duplicate-cost policy, as well as separate model/spend authorization.
