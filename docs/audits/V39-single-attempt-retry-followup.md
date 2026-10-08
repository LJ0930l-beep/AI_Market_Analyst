# V39 Single-Attempt Retry Safety Follow-up

Date: 2026-10-09
Status: **LOCAL CLIENT RETRIES DISABLED / REMOTE IDEMPOTENCY BLOCKED**

## Scope and change

This follow-up addresses duplicate inference risk after ambiguous POST failures. It supplements, and does not rewrite, the original [`V39-transport-reliability.md`](V39-transport-reliability.md) checkpoint.

- `ModelClient` now defaults to zero retries and rejects any nonzero per-call or client-level retry count with `MODEL_RETRY_UNSAFE_WITHOUT_IDEMPOTENCY` before opening a network request.
- The legacy `OllamaProvider` adapter now defaults `OLLAMA_RETRIES` to `0`; the central `ModelClient` guard rejects any explicit nonzero value before an inference POST.
- `ConsultConfig` rejects nonzero retries; `QWEN_CONSULT_RETRIES` is bounded to zero and a nonzero environment value fails configuration. The consultation stream performs exactly one `_stream_once` attempt.

This is a fail-closed availability trade-off: a transient transport failure can end the analysis rather than automatically repeat a possibly accepted, billable POST.

## Test-first evidence

Before the implementation change, the injected ambiguous `URLError` caused the default client to call the opener three times. The explicit retry test also reached the opener instead of refusing the request. The new tests now establish one attempt for an ambiguous failure and zero network attempts for nonzero retry configuration.

## Verification

- Focused V39 transport and consultation compatibility command: **128 passed**. It covers transport diagnostics, completion stream/connection tracing, client routing, and consultation configuration.

  ```text
  python -m pytest -q tests/test_gemini_transport_diagnostics.py tests/test_model_client_stream.py tests/test_completion_stream.py tests/test_completion_connection_trace.py tests/test_gemini_relay.py tests/test_post_v1_consult.py
  ```

- Final full suite with immutable local evidence recording: **2,354 passed, 0 failed, 1 skipped** across **2,355** nodes; one existing Starlette/httpx deprecation warning.

  ```text
  python -m pytest -q -p scripts.v36_test_evidence --v36-report=reports/v38+/verification/v39-single-attempt-retries-final-20261009.json
  python scripts/compare_v37_test_evidence.py reports/v38+/verification/full-suite-test-evidence-baseline-64a5.json reports/v38+/verification/v39-single-attempt-retries-final-20261009.json --output reports/v38+/verification/v39-single-attempt-retries-final-20261009-vs-baseline.json --baseline-commit 64a5c4206a5073e0c44e9d5cc4178705ffa24664
  ```
- Exact comparison to frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**; all **100** baseline failure nodes resolved, **31** new test nodes passed, **0** missing baseline nodes, **0** new failures, and **0** phase changes.
- Machine evidence is local and Git-ignored: `reports/v38+/verification/v39-single-attempt-retries-final-20261009.json` and `reports/v38+/verification/v39-single-attempt-retries-final-20261009-vs-baseline.json`.
- `py_compile` and `git diff --check` passed. Ruff differential over the six modified Python files: **65 baseline diagnostics, 64 current, 0 added, 1 removed**.
- No Gemini/provider call, private exchange request, TestNet/Live session, order, or production risk-setting change occurred.

## Remaining limits

- The local tests prove that this client path does not automatically replay a completion POST. They do not prove the provider's acceptance, deduplication, billing, or cancellation behavior after a disconnect.
- A separately initiated request can still repeat analysis; provider idempotency and duplicate-cost handling remain unverified. Do not claim exactly-once inference.
- V39 therefore remains **PARTIAL** at the remote provider boundary. Any future retry or unattended model operation requires an authoritative provider idempotency contract or an explicitly approved duplicate-cost policy, plus separate model/spend authorization.
