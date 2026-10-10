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
