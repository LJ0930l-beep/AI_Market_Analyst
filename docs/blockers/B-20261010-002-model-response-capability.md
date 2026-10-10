# B-20261010-002 — V38 结构化响应能力未验证

id: B-20261010-002
severity: S1
status: BLOCKED_WITH_EVIDENCE
component: model
mode: RESEARCH_OFFLINE
g0_base_sha: ffc265be9bdcb1f97aef5c80d572636ca9cef035
branch_sha: 732a77ee415461caa7c74b6faba2a9807b45ba77
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
next_owner_action: Keep provider capability blocked and do not issue model requests; await independent G2 authorization after human review of G0/G1 PRs.

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
