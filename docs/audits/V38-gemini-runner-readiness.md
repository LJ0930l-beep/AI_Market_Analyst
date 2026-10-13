# V38 Gemini Market-Only Runner Readiness — 2026-10-10

## Decision

The V38 A1/A2 market-only research path is implemented and passes offline contract tests. This is an execution-readiness report, **not a Gemini result or a market-quality acceptance**. No model call was made while preparing this change; the model-decision count remains zero until a later explicit `--execute` invocation is observed in the local call ledger.

## Authorization and frozen scope

The user explicitly selected `gemini-3.8-flash-high`, the Antigravity Tools reverse proxy, and use of its weekly quota through exhaustion. The authorization is recorded in `configs/research/authorizations/v38-gemini-optimization-run-20261010-v1.json`. It selects only the frozen V3 primary dataset's 36 `optimization` contexts and the existing A1/A2 prompt templates. Their original bytes and hashes remain unchanged. The maximum scope is 72 request intents (36 contexts × 2 arms), with a per-invocation default of one context and a hard maximum of 36.

Validation remains excluded until the model identity, request wrapper, evaluation metrics, and stopping rules are frozen for a one-time use. The 18 untouched-test contexts remain hash-only and are never opened by this runner. The script accepts the frozen visible input file only when its byte hash matches `4fd074eafb73ecd113c8734c5417c05878b4ee6ad6afbcb75f736b74c173399d`; it verifies the canonical dataset manifest hash and rebuilds each optimization input against the frozen per-context digest and evidence-reference count.

The original A1/A2 prompt files still say model selection and calls are unset. They are retained as frozen prompt templates; the separately recorded authorization is the run permission. No frozen evaluation protocol, prompt, dataset, V25/V37 report, or original ledger was overwritten.

## Request controls

- The CLI defaults to offline dry-run. It does not import the model client, read its key, query `/health` or `/models`, or send a request unless `--execute` is supplied.
- Execution uses the existing `ModelClient` at the configured local loopback proxy, requires the pinned Gemini High model, sends one non-stream completion request per new context-arm, sets `retries=0`, disables syntax repair, and accepts only a response whose response-sourced model ID exactly matches `gemini-3.8-flash-high`.
- Every request intent, fixed input hash, prompt hash, and caller-generated request ID is durably appended before network dispatch. A per-ledger exclusive lock prevents concurrent runs. A call with a lost result remains `AMBIGUOUS_NO_RESULT_NEVER_RETRY`; a completed error or invalid response is also immutable and is not silently repaired or reissued.
- The executor revalidates the authorization contract itself and reloads the frozen prompt bytes before scheduling. Ledger events are sequence-numbered and hash-chained; review binds each intent and transport trace to the exact scheduled sample and request. `VALID_ANALYSIS` is rechecked against the stored response bytes, exact response model, completed HTTP 200 JSON trace, schema, and evidence references. A prior HTTP 402/429 latches a stop across later invocations of the shared ledger.
- The response schema permits only `OBSERVE` or `WAIT`. Account, execution, risk, proposal, order, fill, and close fields are forbidden. Reports separately retain request intents, terminal results, reported token counts, model identity, and each trade lifecycle stage. Provider cost remains null unless a verifiable cost receipt becomes available.
- Absolute timestamp fields and decision IDs are removed from the model-facing projection while bar order, prices, derived context, and opaque evidence references are retained. This reduces calendar leakage but cannot eliminate a model's possible prior knowledge of recognizable market sequences.

## Verification

- Actual frozen V3 visible input and manifest loaded successfully; exactly 36 optimization contexts rebuilt to their registered input hashes. The 18 validation rows were not sent to a model; the separate untouched-test payload was not opened.
- Offline CLI dry-run reported 36 authorized optimization contexts, a 72-attempt denominator, and 0 model requests / 0 orders.
- Targeted runner and client tests: **22 passed**; `tests/v38/`: **113 passed**.
- Full local suite: **2,479 passed, 1 skipped, 0 failed**, with one existing Starlette/httpx deprecation warning. Exact comparison with frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664` is `PASS_NO_NEW_FAILURES`: 0 missing baseline nodes, 0 new failures, 0 phase changes, all 100 inherited failures resolved, and 156 added test nodes passing.
- Full-suite machine evidence is local/Git-ignored: `reports/v38+/verification/v38-gemini-runner-final-20261010-v3.json`; exact comparison is `reports/v38+/verification/v38-gemini-runner-final-vs-baseline-20261010-v2.json`.
- Actual frozen V3 input dry-run: **36** optimization contexts, **72** planned intents, **0** model requests, **0** orders; validation and untouched-test remain unsent.
- Ruff passes for the new runner, CLI, and tests; `git diff --check` passes. Existing repository lint diagnostics remain outside this new code.
- These tests use injected fixtures only; they do not establish Antigravity provider identity, billing, deduplication, or model decision quality.

## Remaining limits

- Model response identity is response-sourced from the loopback relay, not an independently signed Google attestation.
- Antigravity's provider-side deduplication, replay behavior, weekly-quota amount, and per-request billing are unverified. Client retries and same-context-arm re-execution are disabled, but an internal provider duplicate or a lost response may still consume quota without a usable result. The user-authorized weekly-quota policy is not a numeric USDT ceiling and must not be presented as a measured cost cap.
- The durable intent ledger is placed under the common repository root's Git-ignored `reports/v38+` directory, so linked worktrees of this local clone share it. Separate Git clones do not share the ledger; they must not be used for the same authorized run.
- The local hash chain detects accidental or partial ledger edits, but it is not a remote signature or an externally anchored tamper-proof audit log.
- Human blind annotations remain zero. A1/A2 outputs cannot yet be called accurate or superior, and this run design makes no profitability claim.
- Binance UM archive reconstruction remains proxy evidence for a Gate-oriented project; historical Gate availability, news, funding, bid/ask, account margin, and executable samples remain absent.
- V39 remote semantics and V42 research acceptance remain `BLOCKED_WITH_EVIDENCE` / not accepted as applicable. This code does not enable execution or Live trading.
