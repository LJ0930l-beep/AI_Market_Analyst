# B-20261010-002 — V38 结构化响应能力未验证

id: B-20261010-002
severity: S1
status: BLOCKED_WITH_EVIDENCE
component: model
mode: RESEARCH_OFFLINE
g0_base_sha: 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1
branch_sha: "f4461a72486b8aba132eb23045e03c9b1ffe0ba3"
g1_working_branch: codex/v2-g1-offline-20261010
trigger_signature: FENCED_JSON_RESPONSES_AND_RESPONSE_FORMAT_CAPABILITY_UNKNOWN
first_seen_at_utc: 2026-10-10T09:27:00Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: 67 immutable INVALID_JSON results and 1 separate consumed format pilot
immediate_stop_or_isolation: No new Gemini call, no retry, no rewriting/relabeling old result; validation and untouched-test remain sealed.
evidence_files_and_hashes: docs/audits/V38-invalid-json-classification-20261010.json SHA-256 4ba18a5bbb41ad8525e334cf44bcad98ab6d5d98ac76823b49f0f6c871785db7; source ledger SHA-256 820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7; offline V2 report docs/audits/V38-Prompt-Schema-V2-offline-contract-20261010.md; full-suite local JUnit SHA-256 C9359E2863F499657C922810B586CD8F5EC085C1593B2AC44C48B4953226A261 (Git-ignored)
reproduction: Local fixed response envelopes through evaluate_response_v2; targeted V2/parser/format-pilot/runner tests, no network path.
root_cause_status: HISTORICAL_LOCAL_ENVELOPE_GAP_CONFIRMED; REMOTE_RESPONSE_FORMAT_CAPABILITY_UNKNOWN
safe_fallback: Strict V2 local JSON/schema/evidence validation; actual route capability remains unknown_unverified; V2 prompt remote dispatch disabled.
patch_pr: Stacked G1 offline branch codex/v2-g1-offline-20261010 based on G0 candidate PR #26; no auto-merge.
negative_tests: Empty/partial/truncated body; finish_reason length/error/missing; missing/mismatched model ID with payload/adapter source separation; malformed/multiple fence; invalid/unknown/future top-level and nested evidence refs; offline fake provider ignores a submitted json_schema request and capability remains unknown; duplicate JSON key; usage mismatch; NaN/infinite/non-positive transport byte counts; duplicate intent in existing pilot tests.
unblock_criteria: Offline contract PR and same-head full CI pass; any G2 remote capability probe requires separate explicit user authorization, a new frozen scope, request ceiling, budget/stop rule, and provider/route identity evidence.
needs_human_decision: true
next_owner_action: "Keep provider dispatch disabled. PR #27 exact head f4461a72486b8aba132eb23045e03c9b1ffe0ba3 passed run 38298606199 (quick 289; full 2603 with 25 subtests; Web 161 plus lint/type-check/build). Remote response_format, authenticated model/weight identity, billing and quota remain unknown. G2 requires G0 human review and a fresh bounded authorization."

## 已证实与仍未知

只读复算显示，冻结账本的 67 个回包均为完整单层 JSON fence；离线 V2 兼容解析和 V38 schema/evidence_refs 验证可以解释其格式，但没有更改任何旧状态。冻结账本 SHA-256 仍为 820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7。

新增 Prompt/Schema V2 与 fake-provider 矩阵仅验证本地契约。成功的本地解析、HTTP 样例、模型字段或 fenced JSON 均不能证明 Antigravity 实际执行 response_format=json_schema。当前真实能力、provider receipt 和费用仍未知。

## 处置、验收与回滚

- 临时方案：只接受固定 V2 schema 与 decision-time evidence_refs；V2 无远程 dispatch 授权，能力固定为 unknown_unverified。
- 修复：保留 V1；新增最小/完整版 V2 schemas、离线 parser policy 和固定 envelope 验证。原文与规范化分析分离，错误响应 fail closed。
- 验收：V2/parser/format-pilot/runner 离线测试通过、候选完整 CI 同 head 通过、历史账本 hash 不变。真实代理 capability 仍需独立 G2 授权与有界试验。
- 回滚：只撤销堆叠 G1 PR 的 V2 新增文件和 parser V2 名称；V1、G0 候选、V35 风控、历史研究 ledger 与报告不变。

## 当前状态

G0 候选 PR #26 的同 head hosted quick/full CI 已通过，但候选仍 Draft、未合并。G1 复审补丁本地验证：V2 evaluator 34 passed、仓库 quick gate 256 passed、全量离线套件 2,551 passed / 1 skipped / 0 failed；Ruff check/format 与 git diff --check 通过。该结果来自本地工作树；复审补丁推送后必须以 PR #27 最新精确 head 的 hosted CI 确认。G1 远端能力仍 BLOCKED_WITH_EVIDENCE。未触发 Gemini、Gate/TestNet/Live API 或订单。费用状态保持未知。任何 G2 调用需用户另行明确授权。

## 2026-10-10 independent offline review addendum — response size limit

**Sub-finding:** `V2_RESPONSE_SIZE_LIMIT_TYPECHECK_ORDER`; severity S1; status `PATCHED_LOCALLY_AWAITING_EXACT_HEAD_CI`. The parent B-002 remains `BLOCKED_WITH_EVIDENCE` because real provider capability, identity attestation, and billing are unknown.

- **Root cause:** `evaluate_response_v2()` compared `len(content)` with `max_raw_response_chars` before validating that the configured limit was a positive exact `int`.
- **Original reproduction evidence:** on base head `c56c54271834c9fd78fad623c3010d725bd70682`, a fixed local `_envelope(_minimal())` with `max_raw_response_chars=None` raised `TypeError: '<=' not supported between instances of 'int' and 'NoneType'` at `response_contract_v2.py:396`. Passing a string raised the corresponding int/string comparison error. No network or exchange client was involved.
- **Temporary containment:** keep G1 unaccepted and use no remote model path. An invalid caller-supplied size limit is an evaluator/configuration fault; it must never become a valid analysis.
- **Repair:** move strict positive-integer validation before envelope/body comparisons. Add parameterized negatives for `None`, string, bool, zero, negative, and float values. Only `response_contract_v2.py` and `tests/v38/test_response_contract_v2.py` change in code.
- **Local acceptance evidence:** `python -m pytest -q tests/v38/test_response_contract_v2.py` — 40 passed; the repository quick-gate command — 262 passed; Ruff check/format and `git diff --check` passed. The frozen call ledger remains 144 lines with SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`. These are local results only; the base `c56c542` hosted run does not cover the patch.
- **Unblock criteria:** new PR #27 exact head must pass quick and full hosted CI; rerun the response-contract test on that head; confirm the V38 ledger SHA remains unchanged. Provider capability remains blocked separately until an explicitly authorized G2 probe.

- **Rollback:** revert only this parameter-check move and its six-case test. Keep PR #27 open and G1 blocked if the new CI or review fails; no changes to V1, production execution, V35 risk controls, frozen samples, or historical ledger.

## 2026-10-10 exact-head CI acceptance addendum — 13:18Z

The `V2_RESPONSE_SIZE_LIMIT_TYPECHECK_ORDER` subfinding is now `PATCHED_AND_EXACT_HEAD_CI_VERIFIED`: PR #27 exact head `cd0de57dae6d6aa5368158ea678697c72688fc2e` passed hosted run `38053934404` (262 quick, 2,558 full, one Starlette deprecation warning, 25 subtests). The targeted response-contract suite rerun locally passed 40 tests. Invalid positive-size configuration cases fail closed before response-length comparison.

The parent B-002 remains `BLOCKED_WITH_EVIDENCE`: remote structured-response capability, actual response identity/weights, and billing remain unknown. This CI result does not authorize G2. No new model, exchange, TestNet, Live, or order request was made. The immutable 144-line ledger was read-only rechecked in the default checkout at SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`; the active isolated worktree does not carry that ignored artifact.

Keep G2 closed pending human review of PRs #26/#27 and separate explicit G2 authorization with fresh frozen optimization scope, a request ceiling, and a cost/stop rule. Roll back only the size-limit validation move and its six negative tests if a regression is found.

## 2026-10-10 independent offline review addendum — fractional transport byte count

**Subfinding:** `V2_TRANSPORT_REQUEST_BYTES_INTEGER_TYPE`; severity S1; status `PATCHED_AND_EXACT_HEAD_CI_VERIFIED`. Parent B-002 remains `BLOCKED_WITH_EVIDENCE` for external provider capability/identity/billing.

- **Root cause:** V2 accepted any finite positive float as `transport_trace.request_bytes_written`; a byte count is an integer quantity, so fractional values cannot be a valid transport receipt.
- **Source evidence:** `core/ai/transport_diagnostics.py::_TracedConnection.send()` calls `request_written(len(data), ...)` for bytes-like request data; `len(data)` is an integer. The trace accumulator preserves that byte count.
- **Original offline reproduction:** at PR #27 head `5314acd9e51a77a2984c58e3c45958eaf1b7278b`, `_evaluate(_envelope(_minimal(), request_bytes_written=1.5))` returned `VALID_LOCAL_CONTRACT` and `MATCHED_COMPLETED_JSON_HTTP_200`. Test-first parameter cases for `1.0` and `1.5` then failed (2 failed, 7 passed) while existing integer/invalid cases behaved as expected. No network, exchange, or provider path was involved.
- **Temporary containment:** the evaluator still has `remote_dispatch_allowed=false`; only fixed offline fixtures are accepted here. Keep G1 pending human review and G2 blocked while external provider capability remains unknown.
- **Repair:** require `type(request_bytes_written) is int and request_bytes_written > 0`; add rejection cases for whole and fractional floats and assert no normalized analysis is emitted. Scope is only `response_contract_v2.py` and its unit test; V1 runner and shared trace production are unchanged.
- **Local evidence:** response-contract suite `42 passed`; repository quick gate `264 passed`; Ruff check/format and `git diff --check` pass. These do not replace exact-head hosted CI.
- **Acceptance:** PR #27 exact head `4f07961861ff8f95147f75495fcaf78063cf3d2b` passed hosted run [38058300661](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38058300661): quick gate 264 passed; full offline suite 2,560 passed, 1 Starlette deprecation warning, and 25 subtests passed. This run verifies the integer-only transport receipt repair. The local review confirmed V1/shared trace production, `main`, V35 risk controls, the frozen model-call ledger, and production execution were unchanged.
- **Rollback:** revert only the integer-only V2 predicate and its added parameter cases/assertions. Preserve the earlier response-size fix and all original evidence; keep G1 blocked if exact-head CI fails.

## 2026-10-10 independent offline review addendum — V38 runner fractional byte acceptance

**Subfinding:** `V38_RUNNER_ACCEPTS_NONINTEGER_REQUEST_BYTES`; severity S1; status `PATCHED_AND_EXACT_HEAD_CI_VERIFIED`. Parent B-002 remains `BLOCKED_WITH_EVIDENCE` for external provider capability/identity/billing.

- **Root cause:** The V38 Gemini runner accepted `request_bytes_written` when it was any positive `int` or `float` in three places: initial `VALID_ANALYSIS` classification, review-time provenance verification, and the reviewed transport-byte metric. A positive fractional number is not a byte count, so malformed evidence could be labeled valid and counted.
- **Source evidence:** `core/ai/transport_diagnostics.py::_TracedConnection.send()` records `len(data)`, an integer. A read-only check of the frozen V38 ledger found 144 lines, 69 recorded byte counts all of type `int`, and the unchanged SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`.
- **Original offline reproduction:** On base head `132b2387e65bfe5eae8ed09ac6f533606f35aca8`, failure-first tests for `1.0`, `1.5`, and a correctly rehashed claimed-valid ledger produced 3 failures / 21 deselected: the execution path returned `VALID_ANALYSIS` for both floats and review did not reject the rehashed float trace. Only local fake-provider fixtures and temporary ledgers were used.
- **Temporary containment:** V2 remote dispatch remains disabled; no new model request is made. Research acceptance stays blocked while this shared runner/reviewer invariant is repaired and verified.
- **Repair:** Add one V38 runner predicate requiring an exact positive integer byte count and use it in initial response validation, review-time provenance validation, and the transport-byte metric. Add execution/review tests for whole and fractional floats and a rehashed ledger tampering case. The transport producer and V1 prompt/schema are unchanged.
- **Local evidence:** At the time of the patch, the new failure-first cases passed (`3 passed`); V38 runner plus V2 response-contract modules passed (`66 passed`); repository quick gate passed (`267 passed`). Exact-head hosted acceptance is recorded below.
- **Acceptance:** PR #27 exact head `f374589d9de79b29f9347cde630135263934c6e5` passed hosted run [38062787101, attempt 2](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38062787101): quick gate 267 passed in 37.23s; full suite 2,563 passed, 0 failed, one Starlette deprecation warning, and 25 subtests passed in 1,169.81s. The V38 runner and V2 response-contract suites passed in this full run. The frozen ledger remains unchanged at 144 lines / 69 integer byte counts with SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`.
- **First-attempt CI note:** The same SHA's attempt 1 passed the 267-test quick gate, then reported 2,562 passed / 1 failed in the full suite. The sole failure was `tests/test_phase5_scheduler.py::SchedulerCoreTests::test_cooperative_stop_interrupts_remaining_items_and_releases_lease`: Windows raised `WinError 32` while `TemporaryDirectory` cleanup unlinked `stop.sqlite3`. The targeted test passed locally, and hosted attempt 2 passed the full suite. This shows an intermittent Windows cleanup/handle risk; its underlying handle owner is not established, so the first failure is retained as a reliability caveat rather than erased.
- **Rollback:** Revert only the V38 runner byte-count predicate and its regression tests if a regression appears. Preserve V2 response-contract fixes, all historical reports, V35 risk controls, and the immutable ledger.

The next owner action is human review of Draft PRs #26 and #27. Keep remote provider capability, identity, and billing marked unknown; no model calls are authorized by this CI result.

## 2026-10-10 offline negative-matrix evidence refresh — 16:27Z

**Subfinding:** `V2_G1_OFFLINE_NEGATIVE_MATRIX_COVERAGE`; severity S1; status `COVERED_AND_EXACT_HEAD_CI_VERIFIED`. Parent B-002 remains `BLOCKED_WITH_EVIDENCE` for remote provider capability, identity attestation, and billing.

- **Finding:** The 10 October offline review's then-current gap list became stale after later V2 negative cases landed. Reinspection of the actual current head found coverage for finish-length/missing/non-stop, empty and partial content, identity missing/mismatch, evidence-ref rejection, byte/transport validation, and a fake-provider `json_schema` request that remains unverified.
- **Evidence:** `python -m pytest -q tests/v38/test_response_contract_v2.py tests/v38/test_gemini_runner.py` passed **66 tests** at head `986e2f27fb3cb20957ab0b3a4413a58445b50a69`. Current-head hosted run [38066089278](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38066089278) passed the 267-test quick gate and 2,563-test full suite. The fake-provider test asserts `response_format_capability=unknown_unverified`, `NOT_PRESENT_NO_REMOTE_CAPABILITY_PROBE`, and `remote_dispatch_allowed=false`.
- **Root cause of stale statement:** The prior timestamped audit captured an earlier negative-matrix state and was not rewritten when later tests were added. Its original statement remains as historical context; the current-state correction is recorded in [`V2-G0-G1-live-refresh-20261010T162737Z.md`](../audits/V2-G0-G1-live-refresh-20261010T162737Z.md).
- **Temporary containment:** Keep all real provider dispatch disabled. Historical ledger, old response labels and denominators remain unchanged; no old or ambiguous intent is retried.
- **Acceptance:** Offline negative-matrix tests and current exact-head CI pass. This does not establish provider behavior. Remote schema capability, actual underlying model/weights, replay/billing behavior, and usage receipts remain unknown, so G1's external capability gate stays open and G2 remains unauthorized.
- **Rollback:** Revert only this additive status note if its evidence reference is invalidated. Preserve the dated old review, test cases, historical ledger, and prior CI results.

## 2026-10-10 independent offline review addendum — evidence allowlist input validation — 17:07Z

**Subfinding:** `V2_EVIDENCE_ALLOWLIST_CONTEXT_TYPE`; severity S2; status `PATCHED_LOCALLY_AWAITING_EXACT_HEAD_CI`. Parent B-002 remains `BLOCKED_WITH_EVIDENCE` for remote provider capability, identity attestation, and billing.

- **Root cause:** The V2 evaluator validated model-supplied evidence references against `valid_evidence_refs` without validating the allowlist container or entries. `None` raised an uncaught `TypeError`; list and malformed set inputs were accepted as lookup containers; a blank reference could be certified when the corrupt allowlist also contained `""`.
- **Original offline reproduction:** On base head `3ad669db6b00ae66c725a1198da5d84e70c7365e`, five failure-first cases produced 5 failures / 42 deselected. One raised `TypeError: argument of type 'NoneType' is not iterable`; four showed that unsupported/malformed allowlists or a blank reference could fail to receive the expected context rejection, including cases labeled `VALID_LOCAL_CONTRACT`. Only fixed local envelopes were used.
- **Temporary containment:** No V2 remote dispatch is enabled, and the evaluator is not connected to a production or exchange route. Keep G1 and G2 blocked at their existing gates while validating the offline context boundary.
- **Repair:** Validate that the allowlist is an ordinary `set` or `frozenset` of non-empty, trimmed strings before reference membership checks. Invalid context returns `INVALID_EVIDENCE` / `EVIDENCE_ALLOWLIST_INVALID` and never emits normalized analysis. Changes are limited to `response_contract_v2.py`, `tests/v38/test_response_contract_v2.py`, and this record; V1 and production execution paths are untouched.
- **Local evidence:** The five new negative cases pass and the immutable valid-allowlist control passes; `python -m pytest -q tests/v38/test_response_contract_v2.py tests/v38/test_gemini_runner.py` passes 72 tests. Exact-head hosted CI is still required after push.
- **Rollback:** Revert only the allowlist validator and its five tests. Preserve prior response-contract fixes, historical ledgers/reports, and the G2 authorization boundary.

## 2026-10-10 exact-head CI acceptance addendum — evidence allowlist input validation — 17:38Z

The `V2_EVIDENCE_ALLOWLIST_CONTEXT_TYPE` subfinding is `PATCHED_AND_EXACT_HEAD_CI_VERIFIED`. PR #27 exact head `22a137ec16776457959b44c225f191ac465c5065` passed hosted run [38070919856](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38070919856): quick gate 273 passed; full offline suite 2,569 passed, with one Starlette deprecation warning and 25 subtests.

The response-contract and Gemini runner tests passed 72 cases locally. Five failure-first malformed-context cases had reproduced the defect; the repair now accepts only a `set` or `frozenset` containing non-empty trimmed strings and rejects invalid context as `INVALID_EVIDENCE` / `EVIDENCE_ALLOWLIST_INVALID` without normalized analysis. A valid `frozenset` control passes. No V1 contract, production execution path, V35 risk control, historical sample, or ledger was modified.

The parent B-002 remains `BLOCKED_WITH_EVIDENCE` for remote structured-response capability, actual response identity/weights, and billing. PR #27 remains OPEN/Draft without human review. No Gemini, Gate/TestNet/Live request or order was made; this exact-head CI does not authorize G2.

## 2026-10-10 independent offline review addendum — stored-input evidence refs — 19:00Z

**Subfinding:** `V38_STORED_INPUT_EVIDENCE_REF_SELF_AUTHORIZATION`; severity S1; status `PATCHED_LOCALLY_AWAITING_EXACT_HEAD_CI`. Parent B-002 remains `BLOCKED_WITH_EVIDENCE` for remote provider capability, response identity/weights, and billing.

- **Root cause:** `_valid_stored_input()` used the request's own `evidence_refs` list as its validation set. A persisted input containing `['']` or a fabricated reference could pass after recomputing the ordinary `market_input_sha256`; the analysis validator then accepted the same reference as grounded.
- **Failure-first evidence:** At PR #27 base head `ca9f525aa895e6957570e0cb693d9b6d32c88202`, the two new tests failed because both the blank and fabricated top-level references were accepted. A fixed local fake provider then returned two `VALID_ANALYSIS` records for the blank-ref input. No real model or exchange client was used.
- **Repair:** Validate the V36 context schema/version and ready timeframe frames; require sorted, unique, non-empty `bar:<timeframe>:<16 lowercase hex>` references; and require the top-level list to equal the union of the frame reference lists. The executor test asserts invalid input is rejected before the fake provider is called or a ledger is written. Changes are limited to `market_only.py`, `tests/v38/test_gemini_runner.py`, and this additive blocker note.
- **Local verification:** The failure-first cases now pass. `python -m pytest tests/v38/test_market_only.py tests/v38/test_gemini_runner.py -q` passed 45 tests; `python -m pytest tests/v38 -q` passed 198 tests; Ruff and `git diff --check` passed. Exact-head hosted CI is pending after push.
- **Evidence limitation:** The local SHA-256 is an unkeyed consistency digest, not source authentication. This validator checks schema, formatting, uniqueness, and consistency with the embedded causal context; it does not independently prove that a hostile editor did not fabricate the entire context and recompute its hashes. The authorized optimization loader still rebuilds inputs from the frozen, hash-pinned source file and manifest. Historical ledgers and research outputs were not modified.
- **Containment and rollback:** Keep provider dispatch and G2 blocked; do not reinterpret prior results. If needed, revert only this stored-input evidence-ref validation and its regression tests. Preserve V1/V2 response contracts, V35 risk controls, all frozen datasets, reports, and ledgers.

## 2026-10-10 exact-head CI acceptance addendum — stored-input evidence refs — 19:41Z

The `V38_STORED_INPUT_EVIDENCE_REF_SELF_AUTHORIZATION` subfinding is now `PATCHED_AND_EXACT_HEAD_CI_VERIFIED`. PR #27 exact head `9eb55ac19b7e8739b7500cca03b0c9d2a253c299` passed hosted run [38078606379](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38078606379): quick gate 275 passed; full suite 2,571 passed, one Starlette deprecation warning, and 25 subtests. Local evidence recorded for this head includes 45 targeted runner/market-only tests, 198 V38 tests, and the commit hook full suite (2,570 passed, 1 skipped, 0 failed).

The repair validates stored context/version and timeframe frames, requires sorted unique non-empty bar references, and checks that top-level evidence references equal the frame-reference union. Invalid executor input is rejected before calling the fixed fake provider or writing a ledger. The frozen historical ledger remains 144 lines / 512,044 bytes with SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`; no historical response was relabeled or retried.

G1 offline response-contract verification passes. Parent B-002 remains `BLOCKED_WITH_EVIDENCE` for provider-native structured-output capability, underlying model weights/identity attestation, and actual billing. Hosted CI is not remote provider evidence and does not authorize G2. No Gemini, Gate/TestNet/Live call or order was made by this repair.

## 2026-10-10 latest exact-head verification — 21:35Z

PR #27 current head `58430c72b3c5e989cc77be86145527de7b0f9ef7` passed exact-head hosted run [38086177948](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38086177948): quick gate 275 passed; full suite 2,573 passed, 25 subtests passed, one existing Starlette/httpx warning. This verifies the offline contract and regression suite on the current candidate SHA only.

Keep the prior exact-head run [38083801386](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38083801386) in the record: 2,572 tests passed before `test_explicit_start_is_singleton_and_stop_leaves_no_thread` encountered `WinError 32` while deleting its temporary SQLite file. The focused local test passed, and the current full hosted rerun passed; classify the lock as not reproduced, with residual intermittent test-environment risk. No test or historical failure was deleted.

The original ledger remains at SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`; the 67 `INVALID_JSON` outcomes remain unchanged. Provider `response_format` support, underlying model weights/identity attestation, and billing remain UNKNOWN/BLOCKED_WITH_EVIDENCE. No Gemini request was made. G2 remains locked pending the manual's fresh explicit scope and stop-rule requirements.


## 2026-10-10 G1 exact-head refresh — 20261010T221231Z

Latest #27 exact-head run [38088943367](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38088943367) is on `d124c1597fcc4ce8655845eb7d68afc4c6552002`: quick 275 passed; full 2573 passed, 25 subtests passed, 1 existing warning. Original ledger remains 512,044 bytes / SHA-256 `820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7` and historical classifications are unchanged. G1 offline contract verification passes; provider-native `response_format` capability, remote weight identity, and verified billing remain unknown. No remote request was made. Keep G2 blocked pending fresh bounded authorization.

## 2026-10-11 exact-head refresh — 04:02Z

PR #27 current head `2cfc8f0ecdac401fc102202d8d3b3f9efa3f53ad` passed exact-head offline CI [38108665836](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38108665836): quick 284 passed, full 2,591 passed, 25 subtests, and one existing warning. The additive candidate state and full PR inventory are recorded in [`V2-G0-G1-live-refresh-20261011T040254Z.md`](../audits/V2-G0-G1-live-refresh-20261011T040254Z.md) and its linked matrix.

This evidence validates only the local/offline response contract and its tests. Provider-native `response_format` behavior, actual underlying model identity or weights, provider billing, and quota settlement remain unverified. The immutable 72-call ledger and separate format-pilot ledger remain unchanged; no call was made by this refresh. Keep dispatch disabled and G2 locked until human G0 review and a fresh explicit bounded authorization.

## 2026-10-11 independent offline review addendum — duplicate JSON keys in runner — 05:00Z

**Subfinding:** `V38_RUNNER_ACCEPTS_DUPLICATE_JSON_KEYS`; severity S1; status `PATCHED_LOCAL_SUITE_PASS_AWAITING_EXACT_HEAD_CI`. Parent B-002 remains `BLOCKED_WITH_EVIDENCE` for remote response-format capability, model identity/weights, and billing.

- **Root cause:** The V38 runner classified completion text with permissive `json.loads`, and ledger review repeated that parse. Duplicate JSON object keys were collapsed to their last value, allowing ambiguous raw completions to be recorded as `VALID_ANALYSIS` and replay-verified.
- **Failure-first reproduction:** On candidate base `92dec3992f0429f8883458ea21bf1fc7ddb6c994`, two offline fake-provider tests failed: both duplicate-key A1/A2 responses were counted valid, and a rehashed ledger with the same ambiguous raw completion was not rejected (`2 failed, 26 deselected`).
- **Repair:** Both parser call sites now reuse the explicit `RAW_JSON_ONLY_V1` parser, preserving the raw-only envelope contract while rejecting duplicate keys and non-JSON constants. Added execution and ledger-review regressions. No network, provider, exchange, or account path was used.
- **Focused evidence:** After repair, the two new tests passed; `tests/v38/test_gemini_runner.py tests/v38/test_response_contract_v2.py` passed 76 tests; full local suite passed 2,592 with 1 skipped, 0 failed, and one existing warning. Ruff lint and `git diff --check` passed. Exact-head hosted CI remains pending; evidence is tracked in [`V2-G1-legacy-runner-strict-json-20261011T050014Z.md`](../audits/V2-G1-legacy-runner-strict-json-20261011T050014Z.md).
- **Containment and rollback:** Keep provider dispatch disabled. If regression appears, revert only these two parser call-site changes and their tests; preserve strict V2 parsing, source ledgers, production risk code, and all historical results.

## 2026-10-11 exact-head acceptance — duplicate JSON keys — 05:30Z

The `V38_RUNNER_ACCEPTS_DUPLICATE_JSON_KEYS` repair is now `PATCHED_AND_EXACT_HEAD_CI_VERIFIED` at PR #27 SHA `cf1f1d881e6868e46219be58789eddf164a20332`. Hosted run [38114167230](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38114167230) passed quick (286 tests) and full (2,593 passed, one existing warning, 25 subtests). The exact run, local duplicate-key reproductions, and rollback scope are in [`V2-G1-legacy-runner-strict-json-20261011T050014Z.md`](../audits/V2-G1-legacy-runner-strict-json-20261011T050014Z.md).

This closes only the offline parser subfinding. Parent B-002 remains `BLOCKED_WITH_EVIDENCE` because provider-native structured output, remote model identity/weights, and billing are unverified. PR #27 remains Draft without review; keep model dispatch disabled until G0 review and separate explicit bounded G2 authorization.

## 2026-10-11 current-head verification update — 08:38Z

- **Severity/status:** S1 / `BLOCKED_WITH_EVIDENCE`; G1 offline contract acceptance does not establish provider capability.
- **Root cause:** the frozen V1 runner passed `message.content` directly to raw `json.loads`; all 67 frozen HTTP 200 completions contain one complete JSON Markdown fence. Parsing compatibility is an offline explanation only, not historical repair or a change to the valid-analysis denominator.
- **Original/current evidence:** the source ledger remains 144 lines / 512,044 bytes with SHA-256 `820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7`; the immutable classification report is SHA-256 `4ba18a5bbb41ad8525e334cf44bcad98ab6d5d98ac76823b49f0f6c871785db7`. Current integration/candidate evidence is in [`V2-G0-G1-live-state-20261011T083833Z.json`](../audits/V2-G0-G1-live-state-20261011T083833Z.json), SHA-256 `ae90124b1f2cb3f4184eefc64c91509fc280f65cc6ae8bc23d81650ccfbb2247`; PR #27 exact head `bf265d27d01ec75bb7dcc94c80bd86ddcf132989` passed run 38122906585 (quick 286; full 2,600; 25 subtests).
- **Temporary solution:** keep remote dispatch disabled; do not retry any old/ambiguous intent or relabel historical results. Use only the strict offline V2 schema/evidence validator and fixed fake-provider envelopes.
- **Repair:** preserve the old V1 record, keep strict JSON and duplicate-key checks, and use the new offline V2 parser/schema/evidence tests. No historical ledger or production execution path was changed.
- **Acceptance/unblock criteria:** offline contract is verified at the exact candidate head. Remote `response_format`, authenticated model/weight identity, billing and quota remain unknown. G2 requires G0 review plus a newly frozen 2–4 request scope, request ceiling, cost/quota limit and stop rule.
- **Rollback:** revert only the additive offline parser/validator implementation and its regression tests if a regression is found; preserve V1, historical statuses/denominators, all frozen datasets, V35 risk controls, and production defaults.
- **External effects:** no Gemini/Antigravity or Gate request, TestNet/Live order, or charge was attempted by this refresh.

## 2026-10-11 baseline and offline regression refresh — 09:14Z

The handbook's 2026-10-10 baseline of 25 open PRs is historical: current GitHub state is 27 open and 0 merged, with `main` unchanged at `8642639a78ca9aa56fbad1e88aea4b82169ea433`. PR #27 is now at exact head `e30551fa22125413afe364de3932b9ddf5cfe880`; hosted run [38125994063](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38125994063) passed quick 286 and full 2,600 tests. Local `tests/v38/test_response_contract_v2.py`, `tests/v38/test_authorization_revocation.py`, and `tests/test_gate_account_chain.py` passed 81 tests with one existing Starlette/httpx deprecation warning.

The ledger remains 144 lines / 512,044 bytes / SHA-256 `820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7`. The two reported classification JSON hashes are reconciled: `4ba18a5bbb41ad8525e334cf44bcad98ab6d5d98ac76823b49f0f6c871785db7` is the canonical LF Git blob; `c02a1d3eccff68b3cb1a8ac57125c82cd8bd7beba1636183c2de6daf6254b2cc` is the Windows CRLF worktree byte hash with `core.autocrlf=true`. LF-normalizing the worktree reproduces the Git blob SHA exactly. No report or source ledger was changed.

The full refreshed PR/head/check and ledger record is [`V2-G0-G1-live-state-20261011T091403Z.json`](../audits/V2-G0-G1-live-state-20261011T091403Z.json), Git blob SHA-256 `8dfe75e1afad571e763de2b5353c76c2ae9b23537d964609cc3257b4e31fb302` (Windows CRLF worktree SHA-256 `3e6dd6b0dc874c58cc883d3bd917ce50e908b31f04d602d3733bca11225b2594`); the readable report is [`V2-G0-G1-live-refresh-20261011T091403Z.md`](../audits/V2-G0-G1-live-refresh-20261011T091403Z.md).

This verifies the offline response contract only. Remote provider-native `response_format`, authenticated underlying model/weights, billing and quota remain UNKNOWN. Keep dispatch disabled; G2 remains `NOT_AUTHORIZED` until G0 review and a fresh bounded authorization.

## 2026-10-11 offline exact-head refresh — 12:29Z

PR #27 exact head 230550b87abccb1530bc34e9997a8afee46732c4 passed hosted run 38136662102: Python quick 286, full 2,600, Web 29 files / 161 tests, lint, and build. A same-host JUnit node-ID comparison against the frozen baseline recorded 2,599 passed, 1 Windows symlink-capability skip, and zero failures on the candidate; 103 baseline failures now pass, no baseline nodes are missing, and no new failures appear. This is local/offline evidence only; details and report hashes are in docs/audits/V2-G0-G1-live-refresh-20261011T122959Z.md and docs/audits/PR-INTEGRATION-MATRIX-20261011.json.

The V2 dispatch flag remains false and max_calls remains zero. Provider response_format behavior, authenticated response model/weight identity, billing and quota settlement are still unknown. Keep G2 NOT_AUTHORIZED until independent G0 human review and a fresh bounded authorization. No provider, exchange, TestNet, Live, order, ledger, or production account configuration was used or changed.

## 2026-10-12T18:07:29Z 当前离线证据刷新

- **严重度/状态：** S1 / `BLOCKED_WITH_EVIDENCE`；离线契约通过不能证明远端 provider 能力、模型身份或实际结算。
- **证据：** 快照 [`V2-G0-G1-live-refresh-20261012T180729Z.md`](../audits/V2-G0-G1-live-refresh-20261012T180729Z.md) 与 [`V2-G0-G1-live-state-20261012T180729Z.json`](../audits/V2-G0-G1-live-state-20261012T180729Z.json)；PR #27 exact head `66ef1f707652c322eb5c33778485a529b52804e3` 的 run [38286178147](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38286178147) 通过。冻结 ledger 和 67 个 `INVALID_JSON` 分类未读取或改写；既有 SHA 保持冻结。
- **临时遏制：** `remote_dispatch_allowed=false`、`max_calls=0`；不重试任何旧 intent，不把 green CI 当作 provider 能力证据。
- **修复/离线验收：** 当前 head Python quick 286、full 2,600 / 25 subtests；Web 161、lint、类型检查与构建通过。只证明离线 schema/parser/evidence contract 及回归。
- **未解除原因：** provider 原生 `response_format`、响应模型/权重真实性、计费和 quota 仍 UNKNOWN。G2 需要 G0 独立审查及新的明确 bounded scope、调用上限、费用/额度上限和 stop rules。
- **回滚：** 本轮为文档证据刷新；如事实有误，回退本次文档提交并新增更正记录，不改写历史 ledger 或旧审计。代码行为异常时仍保持 dispatch 关闭并按对应代码 PR 的回滚范围处理。
- **外部影响：** 无模型或交易所 API 调用、订单、费用、生产账户设置变化。


## 2026-10-12T18:38:22Z 当前离线证据刷新

- **严重度/状态：** S1 / `BLOCKED_WITH_EVIDENCE`；离线契约通过不证明远端 provider 能力、模型身份或账单结算。
- **证据：** [`V2-G0-G1-live-refresh-20261012T183822Z.md`](../audits/V2-G0-G1-live-refresh-20261012T183822Z.md) 与 [`V2-G0-G1-live-state-20261012T183822Z.json`](../audits/V2-G0-G1-live-state-20261012T183822Z.json)；PR #27 exact-head run 38289819059 全绿。历史 67 个 `INVALID_JSON` 仍未重试、重标或改写；ledger 哈希不变。
- **临时遏制：** `remote_dispatch_allowed=false`、`max_calls=0`；不重试旧 intent，不用 hosted CI 推断 response_format 支持。
- **修复/验收：** 当前 head Python quick 286，full 2600 / 25 subtests；Web 161，lint、type-check/build 通过。它只验证离线 V2 schema/parser/evidence 与回归。
- **未解除原因：** provider 原生 response_format、响应模型/权重身份、计费和 quota 仍未知。G2 仍需要 G0 review 及新的明确有界调用、费用/额度上限和停止规则授权。
- **回滚：** 本轮只有审计文档变更；如状态字段错误，撤销本次文档提交并新增更正，不改历史 ledger/报告；运行时仍保持 remote dispatch 关闭。
- **外部影响：** 无模型/交易所/TestNet/Live 请求、订单、费用或账户设置变化。

## 2026-10-12T19:21:18Z 当前依赖状态

PR #27 exact head `5f70d186d169261f0f337232c70e246a2e88b4e6` run `38293103442` passed, but predates the local B-20261012-005 multiple-choice repair. Provider dispatch stays disabled until the repair passes new exact-head CI. Remote structured-output capability, authenticated model/weight identity, billing, and quota remain unknown; G2 still requires G0 human review and separate bounded authorization.

## 2026-10-12T19:54:50Z 精确 head CI 更新

PR #27 exact head `f4461a72486b8aba132eb23045e03c9b1ffe0ba3` run `38298606199` passed: Python quick 289, full 2,603 / 25 subtests, Web 161, lint, type-check, and build. This verifies the offline code at that head only. Remote provider structured-output support, authenticated model/weight identity, billing, and quota remain unknown; dispatch stays disabled and G2 remains unauthorized.

## 2026-10-12T21:46:04Z offline state refresh

The V2 prompt configurations still set remote_dispatch_enabled=false, call_authorization.authorized=false, and maximum_calls=0. The new exact-head PR #27 run 38309564947 verifies only offline fixtures: quick 289 passed, full 2,605 passed with 25 subtests and one existing deprecation warning; Web/lint/type-check/build passed. No provider request was made.

Antigravity/Gemini structured response_format support, authenticated actual response identity/weights, billing, and quota settlement remain unknown. Keep BLOCKED_WITH_EVIDENCE, provider dispatch disabled, and G2 unauthorized until the manual's earlier gates and a fresh bounded authorization are satisfied. See docs/audits/V2-G0-G1-live-refresh-20261012T214604Z.md.

## 2026-10-12T23:38:26Z current exact-head refresh

PR #27 head `86ab6fe124b2541fd7f73482dbabe2dd23f63b48` passed hosted run `38320355179` (quick 289; full 2,605, 25 subtests; Web/lint/type-check/build). This is offline verification only. The V2 prompt files still set `remote_dispatch_enabled=false`, `authorized=false`, and `maximum_calls=0`. No provider request was made; `response_format` support, authenticated response/weight evidence, billing, and quota remain unverified. Keep this S1 blocker open with its existing safe fallback.
