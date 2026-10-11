# V39 Progressive Consultation Stream Integrity Follow-up

Date: 2026-10-09
Disposition: **LOCAL PROTOCOL PASS / REMOTE PROVIDER BEHAVIOR UNVERIFIED**

## Finding and repair

The progressive consultation path used `ModelClient.chat_completion_stream()`. Before this follow-up, the iterator returned normally at EOF even when the server omitted `[DONE]`, omitted `finish_reason="stop"`, sent a non-stop finish reason, or sent malformed JSON. `OllamaConsultTransport` could then record a model receipt and emit a `done` event after a truncated stream.

The client now parses SSE data events through their blank-line boundaries and only returns normally after both a valid `finish_reason="stop"` and `[DONE]`. It fails closed on EOF before `[DONE]`, missing or invalid finish status, provider error events, malformed JSON, invalid UTF-8, repeated completion status, and content after finish. Consumer cancellation still ends without converting the intentional cancellation into a protocol failure. The consultation endpoint can stream partial `delta` events before a later failure, but the failure path does not emit a `done` event or a model receipt.

The repair changes only the progressive client and its local tests. It does not alter the structured-completion parser, model-selection policy, retry policy, trading decisions, Gate execution path, or production risk settings.

## Tests and verification

- Test-first reproduction: six terminal/protocol cases failed before the repair, confirming that EOF, missing stop, non-stop finish, malformed JSON, provider errors, and post-finish content were accepted silently.
- Focused Gate 3 suite: **154 passed**; one existing Starlette/httpx `TestClient` deprecation warning.

  ```text
  python -m pytest -q tests/test_gemini_transport_diagnostics.py tests/test_model_client_stream.py tests/test_completion_stream.py tests/test_completion_connection_trace.py tests/test_gemini_relay.py tests/test_post_v1_consult.py tests/test_gemini_structured_stream.py tests/test_v39_sse_fault_injection.py
  ```

- Full suite with the frozen evidence plugin: **2,418 passed, 1 skipped, 0 failed** (2,419 collected); the same existing deprecation warning remains.
- Same-environment comparison to frozen V37.1 baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**. All 100 inherited failure nodes remain resolved; 95 post-baseline nodes pass; zero missing baseline nodes, new failure nodes, or phase changes.
- Ruff diagnostic differential across the three changed Python files: **0 added diagnostics**. The pre-existing `UP017` diagnostic in `tests/test_post_v1_consult.py` remains. `compileall` and `git diff --check` pass.
- Git-ignored local evidence:
  - `reports/v38+/verification/v39-progressive-stream-integrity-20261009.json` — SHA-256 `cc61f72d684a022de29a5fc075524be7500e539df097c425af96478aebb1ee72`
  - `reports/v38+/verification/v39-progressive-stream-integrity-20261009-vs-baseline.json` — SHA-256 `243154441da4c45a54b8fa310cc2cf43529e6b27c86ae8eb8cd45d5f23b6426a`

## Safety and remaining limits

- No Gemini or other external model call, exchange request, TestNet/Live session, order, saved production setting, or account permission changed.
- The `/consult/stream` contract is intentionally strict. A real provider that omits the OpenAI-compatible `finish_reason="stop"` or `[DONE]` will now yield an error instead of a completed consultation. Provider compatibility still needs separately authorized integration evidence.
- A partial `delta` may already have reached the client before an error. Consumers must treat only the terminal `done` event as completion; the incomplete path produces `error` and no receipt.
- Provider-side buffering, actual cancellation behavior, identity consistency, request replay, billing, and idempotency remain unverified. This repair does not authorize retries or claim exactly-once inference.

## Acceptance

The local progressive-stream integrity defect is repaired and covered by repeatable offline tests. This follow-up passes its local V39 acceptance target. Overall V39 and V38–V42 remain **PARTIAL** because remote provider semantics and the separately authorized V42 research run are still gated.
