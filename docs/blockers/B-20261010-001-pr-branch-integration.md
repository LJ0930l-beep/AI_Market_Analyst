# B-20261010-001 — PR 分叉集成与组合验收未完成

```yaml
id: B-20261010-001
severity: S1
status: FIX_IN_REVIEW
component: integration
mode: RESEARCH_OFFLINE
branch_sha: 732a77ee415461caa7c74b6faba2a9807b45ba77
trigger_signature: PR_COMBINATION_CANDIDATE_AWAITS_HUMAN_REVIEW
first_seen_at_utc: 2026-10-10T09:27:00Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: true
risk_to_funds: NONE
affected_sample_or_order_ids: PR-1-through-27; no orders
immediate_stop_or_isolation: No remote merge, force-push, TestNet request, or Live action. Preserve all source PR refs.
evidence_files_and_hashes: docs/audits/PR-INTEGRATION-MATRIX-20261010.json Git-blob SHA-256 8318066478e5d767452db920fca4b5eb9d35c37787c610e0f327660445466edc; docs/audits/PR-INTEGRATION-MATRIX-REFRESH-20261010.json SHA-256 B94397AE77DB87C0574C93709D1377103F630F9D76DE358DF8A82BA495BC27E4.
reproduction: gh pr list --state all --limit 100 --json number,title,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,updatedAt,mergeable,reviewDecision,statusCheckRollup; query check-runs by each exact head SHA
root_cause_status: CONFIRMED
safe_fallback: Preserve separate branches and perform only a local isolated integration candidate.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/26 and https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
negative_tests: Candidate must include Gate account isolation, Gate 4h pagination, reduce-only semantics, macro clock behavior, model response identity/parser failures, and full offline CI.
unblock_criteria: One candidate SHA includes PR-20/21/22 and PR-25 ancestry (including PR-23/24 fix); changed-file overlap reviewed; full quick and full hosted CI pass on that exact SHA; same-environment baseline has zero new failures; rollback point recorded; human review and explicit merge approval are complete.
needs_human_decision: true
candidate_branch: codex/v2-g0-integration-20261010
candidate_source_heads: origin/main@8642639a78ca9aa56fbad1e88aea4b82169ea433; PR20@43eb1835ef76b8c39f9b560ceaa3cac1b436d1bf; PR21@941e38372a0c0f96d1aaecd4a4ca2faf62dd3070; PR22@cb03f8e34cedca18d735187fdab41cda388eaa99; PR25@97d138fc3f8072668af1bbeada3475d612f55a7d
candidate_local_verification: targeted 38 passed; quick gate 222 passed; full 2517 passed, 1 skipped, 0 failed; baseline 2113 passed, 103 failed, 1 skipped; node-id comparison 0 new failures, 0 missing baseline nodes.
candidate_junit_sha256: baseline ec187fb87f395a5bb3949d358b13614b73dacfbfa873dc7d8a451ef54374ca9b; candidate dad101a071100b00d9071936e13233f1efcfa8813e82a1acd3d29fbbe9c0a733
remote_ci_status: PR26 exact head ffc265be9bdcb1f97aef5c80d572636ca9cef035 run 38043671492 passed quick/full; PR27 exact head 732a77ee415461caa7c74b6faba2a9807b45ba77 run 38046845072 passed quick/full; PR23 exact head remains failed on run 37963206552 and is not rewritten by green descendants.
next_owner_action: Human review PR26 and PR27; keep both Draft and unmerged; if review requests changes, patch the isolated candidate and rerun same-head CI.
```

## 故障原因与原始证据

25 个 PR 全部开放。PR #20 从 #19 分出，#21/#22 建立在 #20；PR #23 也从 #19 分出，#24/#25 建立在 #23。PR #25 当前 head CI 已绿，但 ancestry 不含 #20/#21/#22。PR #20 与 #23/#24/#25 存在 Gate E2E、macro calendar、测试或审计文档重叠。

PR #23 exact-head run `37963206552` 有 3 个 `tests/test_macro_actuals.py` 失败；PR #24 明确修复时钟/可用时间 fixture，run `37966405672` 通过。最新 PR #25 head `fcfc9ec...` 的 run `38040115559` 快速 222 passed、完整 2,513 passed，说明 PR #25 单支是绿色，不证明与 #20/#21/#22 组合后正确。

权威快照：[`PR-INTEGRATION-MATRIX-20261010.json`](../audits/PR-INTEGRATION-MATRIX-20261010.json)。

## 临时方案、修复、验收与回滚

- 临时方案：不合并任何远端 PR；以 branch head SHA 冻结矩阵，保持每支独立且不触碰 main。
- 修复：本地隔离候选以 `origin/main@8642639...` 起步，按 #20 → #21/#22 → #25 顺序合并；#25 负责携带 #23/#24/#25 线，不能把 #23 原红色 head 当作独立绿线。对矩阵列出的共享文件审查所有两支改动。
- 验收：同一候选 SHA 的完整测试、重点组合回归、相同环境 baseline 比较，0 个新失败；PR 来源、合并点、测试证据及变更回滚点逐一记录。人工审查前不合入 main。
- 回滚：只放弃隔离候选或 revert 候选内部的具体 merge commit；原分支、远端 PR、冻结数据和主线保持不变。不得 force-push。

## 当前状态

`FIX_IN_REVIEW`。PR #26 组合候选与 PR #27 G1 堆叠候选均已在各自精确 SHA 通过 hosted quick/full CI，但两者仍 Draft、未合并，且没有人工审查决定。PR #23 自身的失败检查保持失败；后代修复不改写其历史结论。

## 2026-10-10 live snapshot addendum

The original incident narrative above preserves the discovery-time branch evidence. At `2026-10-10T11:23:57Z`, the refreshed GitHub snapshot recorded 27 open PRs. PR #26 exact head `ffc265be9bdcb1f97aef5c80d572636ca9cef035` passed run `38043671492`; stacked PR #27 exact head `732a77ee415461caa7c74b6faba2a9807b45ba77` passed run `38046845072`. PR #23 exact head remains failed on run `37963206552`; PR #24/#25 descendant passes do not rewrite it. PRs #1–#11 have no check runs attached to their current heads; #12–#22 and #24–#27 have successful current-head checks.

Refreshed machine snapshot: [`PR-INTEGRATION-MATRIX-REFRESH-20261010.json`](../audits/PR-INTEGRATION-MATRIX-REFRESH-20261010.json), SHA-256 `B94397AE77DB87C0574C93709D1377103F630F9D76DE358DF8A82BA495BC27E4`. The original 25-PR matrix remains unchanged. Status is `FIX_IN_REVIEW`, not `VERIFIED`: both candidate PRs still need human review and explicit merge approval.

## 2026-10-10 exact-head CI refresh — 13:18Z

PR #26 remains at `ffc265be9bdcb1f97aef5c80d572636ca9cef035`; exact-head run `38043671492` passed (222 quick, 2,518 full). PR #27 now points to `cd0de57dae6d6aa5368158ea678697c72688fc2e`; exact-head run `38053934404` passed (262 quick, 2,558 full, one Starlette deprecation warning, 25 subtests). PR #23 head still has failed run `37963206552`; PRs #1–#11 have no current-head check runs. The live matrix is `docs/audits/PR-INTEGRATION-MATRIX-LIVE-20261010T131858Z.json`, SHA-256 `5F3E6C2F4D8E43D9823218853CA1F305625920AC41352E4A0327BBF1C9C90068`.

Both review candidates remain OPEN, Draft, with no reviews and no merge. Status remains `FIX_IN_REVIEW`; CI does not replace the required human review/approval. Keep source branches and old CI outcomes unchanged.

## 2026-10-10 exact-head refresh — 15:05Z

The latest read-only GitHub capture covers **27** open PRs (the handbook's earlier 25-PR count is historical and stale). `main` remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`. Current exact-head checks are 15 all-success, 1 failure (#23), 11 with no check runs (#1–#11), and 0 in progress. The new snapshot [`PR-INTEGRATION-MATRIX-LIVE-20261010T150501Z.json`](../audits/PR-INTEGRATION-MATRIX-LIVE-20261010T150501Z.json) has SHA-256 `6325C97D395222026BAEFE5F557ABCD5CE88BA61DB4C37D5AC7C2889213B5E6D` and captures 2026-10-10T15:05:01Z–15:05:11Z.

PR #26 exact head `ffc265be9bdcb1f97aef5c80d572636ca9cef035` remains green on run `38043671492` (222 quick, 2,518 full). PR #27 exact head `132b2387e65bfe5eae8ed09ac6f533606f35aca8` is green on run `38059970482` (264 quick, 2,560 full, 1 deprecation warning, 25 subtests; 24m51s). PR #23 exact head remains failed on `37963206552`; descendants do not rewrite that result. PR #26/#27 remain OPEN, Draft, without review or merge.

This snapshot is the remote state **before** the new local runner-byte validation patch is pushed. The working branch is adding an offline S1 repair for a second fractional-byte acceptance path; its failure-first reproduction and local tests are recorded in B-002. Next step: finish local validation, push the isolated PR #27 patch, and require exact-head hosted quick/full CI. G0 remains `FIX_IN_REVIEW`; no remote merge or external model/exchange request is authorized by this refresh.

## 2026-10-10 exact current-head refresh — 16:27Z

The live matrix now records 27 open PRs: 15 current heads with all checks successful, 1 failed current head (#23), 11 current heads with no check runs (#1–#11), and 0 in progress. GitHub `main` remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`. The new immutable snapshot [`PR-INTEGRATION-MATRIX-LIVE-20261010T162737Z.json`](../audits/PR-INTEGRATION-MATRIX-LIVE-20261010T162737Z.json) has SHA-256 `71323319cf288e7bfd244abb19f8ba2364ffdffc19b99b1366e1803bc577bde6`.

PR #26 remains at exact head `ffc265be9bdcb1f97aef5c80d572636ca9cef035`, with hosted run `38043671492` successful (222 quick / 2,518 full). Stacked PR #27 now points to exact head `986e2f27fb3cb20957ab0b3a4413a58445b50a69`; hosted run [38066089278](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38066089278) passed (267 quick / 2,563 full, 1 Starlette deprecation warning, 25 subtests). Both candidates remain OPEN, Draft, without review decisions or auto-merge.

PR #23 exact head `5831f9785e5f8b09d6a819c562949beceb6db4b0` remains failed on run `37963206552` with three `tests/test_macro_actuals.py` failures. PR #24/#25 descendant fixes and green #26/#27 candidate checks do not rewrite that historical red check. PR #1–#11 still have no current-head check runs. Status stays `FIX_IN_REVIEW`; next action is human review of #26/#27 and explicit approval before any merge. No source branch or `main` was modified.

## 2026-10-10 exact current-head refresh — 17:45Z

The live snapshot at 17:45:21Z contains 27 open PRs: 15 current heads with all checks successful, one failed head (#23), 11 current heads with no check runs (#1–#11), and none in progress. GitHub `main` and PR #26's base remain `8642639a78ca9aa56fbad1e88aea4b82169ea433`. The immutable machine snapshot [`PR-INTEGRATION-MATRIX-LIVE-20261010174521Z.json`](../audits/PR-INTEGRATION-MATRIX-LIVE-20261010174521Z.json) has SHA-256 `02B85C0CFFA0C3AD53CEDEEB5E46684F5D099D97055ACD5B1C129278BFF6D110`.

PR #26 remains OPEN/Draft at exact head `ffc265be9bdcb1f97aef5c80d572636ca9cef035`; hosted run `38043671492` completed successfully (222 quick / 2,518 full). Stacked PR #27 remains OPEN/Draft at exact head `22a137ec16776457959b44c225f191ac465c5065`; hosted run [38070919856](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38070919856) completed successfully (273 quick / 2,569 full, one Starlette deprecation warning, 25 subtests). Both have zero reviews/comments and no auto-merge. The `ALL_SUCCESS` classification is based on completed exact-head check runs; an aggregate legacy status of `pending` with zero status contexts is not treated as an in-progress run.

No descendant success rewrites PR #23's failed exact-head result. The review candidates remain `FIX_IN_REVIEW`; human review and explicit merge approval are still required. No branch was merged and no model, exchange, TestNet, Live, or order request was made.
