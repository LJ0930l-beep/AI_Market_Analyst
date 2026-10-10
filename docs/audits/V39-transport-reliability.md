# V39 Transport Reliability Audit

## Offline evidence

Command:

```text
python -m pytest tests/test_gemini_transport_diagnostics.py tests/test_model_client_stream.py tests/test_completion_stream.py tests/test_completion_connection_trace.py -q
```

Result: **91 passed** in 2.71 seconds. Tests use deterministic fixtures and loopback sockets; there were no external model requests.

Coverage includes SSE split frames and Unicode, multiline events/comments, EOF before terminal marker, incomplete/truncated events, malformed/error payloads, invalid finish reasons, missing/mismatched model identity, data after finish/trailing data, bounded memory/event/number handling, absolute deadline behavior under dribble and body stall, header-versus-body timeout provenance, connect/write phase distinction, TLS verification, sanitized transport traces, and configurable retry counts.

## Remaining transport risks

- No real Gemini endpoint was called, so TLS termination, provider-side cancellation, gateway behavior, and provider-specific event variations are not certified.
- A local EOF/incomplete-stream fixture is covered, but remote reset/disconnect behavior after the provider may have accepted a request is not equivalent to a provider integration test.
- Retry-count forwarding is covered; exactly-once inference is not. If the remote provider processed a request but the response was lost, a retry can repeat inference and billing. No provider idempotency key has been proven here.
- The suite rejects data after terminal completion but does not establish a provider-wide deduplication contract for repeated event IDs.

No retry or provider behavior was changed. **V39 status: PARTIAL** until the operator defines duplicate-cost handling and any future provider integration is separately authorized and bounded.
