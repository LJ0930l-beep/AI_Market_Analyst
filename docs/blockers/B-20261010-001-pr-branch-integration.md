# B-20261010-001 — PR 分叉集成与组合验收未完成

```yaml
id: B-20261010-001
severity: S1
status: FIX_IN_REVIEW
component: integration
mode: RESEARCH_OFFLINE
branch_sha: 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1
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
remote_ci_status: PR26 exact head 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1 run 38107795624 passed (quick 231; full 2538); PR27 exact head e30551fa22125413afe364de3932b9ddf5cfe880 run 38125994063 passed (quick 286; full 2600); PR23 exact head remains failed on run 37963206552 and is not rewritten by green descendants.
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

## 2026-10-10 exact current-head refresh — 21:35Z

The additive snapshot [`PR-INTEGRATION-MATRIX-LIVE-20261010T213530Z.json`](../audits/PR-INTEGRATION-MATRIX-LIVE-20261010T213530Z.json), SHA-256 `A2F87BC0C6B82C6AA053D5E5D28BF30C842D7F2FDB53D87BAA28D7E1F75E41AF`, rechecked all 27 current PR heads, bases, check runs and review counts. GitHub `main` remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`; 27 PRs are OPEN, 0 merged, and all have zero reviews. Current heads classify as 15 ALL_SUCCESS, #23 FAILURE, #1–#11 NO_CHECKS, and 0 in progress.

PR #26 head `fa5832f7f81c8cb5438aecc5b8bead9e02ce9cfc` is based on main, contains all #1–#25 current head commits as ancestors, and has 211 changed files. Its exact-head run [38086165883](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38086165883) passed 222 quick tests and 2,520 full tests, with 25 subtests and one existing warning. PR #27 head `58430c72b3c5e989cc77be86145527de7b0f9ef7` is based on #26 and has 29 changed files; exact-head run [38086177948](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38086177948) passed 275 quick tests and 2,573 full tests, with 25 subtests and one existing warning.

The immediately preceding #26 run timed out at the then-configured 30-minute job limit after 1,971 tests passed; the workflow limit is now 45 minutes, and the exact new head passed. A preceding #27 run recorded one Windows SQLite temporary-file cleanup error after 2,572 tests passed; the isolated local test and the latest full hosted run passed. Preserve that earlier run as an intermittent reliability caveat. Neither result is rewritten.

Status remains `FIX_IN_REVIEW`: no human review or merge decision exists. Keep both candidate PRs Draft and unmerged; no model, exchange, TestNet, Live, order, or account-setting operation was performed.

## 2026-10-10 exact current-head refresh — 19:41Z

The immutable live snapshot [`PR-INTEGRATION-MATRIX-LIVE-20261010T194115Z.json`](../audits/PR-INTEGRATION-MATRIX-LIVE-20261010T194115Z.json), SHA-256 `F933C0E4FF3C21BDA8E806BF1D4FAEE865AF80664FA975B332C01C640FBBC4EC`, captured 27 open PRs and 0 merged PRs. `main` remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`. PR #26 is still based on main and includes PR #1–#25 current-head ancestry; PR #27 is stacked on #26.

PR #27 exact head `9eb55ac19b7e8739b7500cca03b0c9d2a253c299` passed hosted run [38078606379](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38078606379): quick gate 275 passed and full suite 2,571 passed with one Starlette deprecation warning and 25 subtests. The current-head matrix still has 15 all-success heads, PR #23's original failure, 11 heads without checks (#1–#11), and none in progress. PR #26/#27 remain OPEN/Draft, with no reviews, merge, or auto-merge.

This refresh updates evidence only. Status remains `FIX_IN_REVIEW` pending human review and explicit merge decisions; the historical PR #23 failure remains unchanged. No source PR, `main`, model route, exchange, account, or order was changed or contacted.

## 2026-10-11 full candidate refresh — 04:02Z

The new point-in-time snapshot is [`V2-G0-G1-live-refresh-20261011T040254Z.md`](../audits/V2-G0-G1-live-refresh-20261011T040254Z.md), with the complete current 27-PR inventory in [`PR-INTEGRATION-MATRIX-LIVE-20261011T040254Z.json`](../audits/PR-INTEGRATION-MATRIX-LIVE-20261011T040254Z.json). Main remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`; PRs #1–25 retain their prior head/base SHAs. All 25 current heads are ancestors of PR #26, and PR #27 is based exactly on PR #26 head.

- PR #26 `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1`: exact-head run [38107795624](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38107795624) passed quick (231) and full (2,538), plus 25 subtests; 217 changed files.
- PR #27 `2cfc8f0ecdac401fc102202d8d3b3f9efa3f53ad`: exact-head run [38108665836](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38108665836) passed quick (284) and full (2,591), plus 25 subtests; 37 changed files.
- The 9 shared paths are sequential follow-up edits on the exact PR #26 base. Both candidates remain Draft, have zero reviews, and have auto-merge disabled.
- PRs #1–11 still have no checks and PR #23 still fails its current check. Preserve these gaps in the G0 decision; green candidate CI alone is not G0 acceptance.

The full local suite on the PR #27 worktree passed **2,590 / skipped 1 / failed 0**. Same-host node-ID comparison with the baseline at main `8642639a78ca9aa56fbad1e88aea4b82169ea433` found all 103 baseline-failing IDs now passing, with zero missing baseline failures, zero regressions from baseline passes, and zero new failures. This is an integrated-candidate comparison; it does not attribute those resolved failures only to PR #26/#27. The detailed command, XML hashes, and test inventory are in the linked live refresh report.

Status remains `FIX_IN_REVIEW`; do not merge until human review and an explicit integration decision are recorded.

## 2026-10-11 PR #23 failure root-cause closure — 04:34Z

The earlier 04:02Z snapshot correctly recorded PR #23's red check as a historical branch status. Follow-up inspection of run [37963206552](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/37963206552) found three failures in `tests/test_macro_actuals.py`, all caused by test fixtures using an injected historical `now` while leaving the scheduler clock and status reads on host wall time. The fixed 2026-10-02 rows had aged beyond the seven-day lookback by the 2026-10-09 run. The exact failing node IDs, evidence, PR #24 patch, rollback, and boundary are recorded in [`V2-G0-PR23-CI-failure-resolution-20261011T043402Z.md`](../audits/V2-G0-PR23-CI-failure-resolution-20261011T043402Z.md).

PR #24 commit `5e9ef37` threads the effective clock into calendar status and fixes all three test fixtures. Current local macro-calendar tests pass 16/16; the three original failure nodes pass 3/3; PR #24 exact-head CI passed. Both integrated candidates also have exact-head full CI success, including PR #27 head `5346f0375a649e78123221ab57d1e55b759bb1ab` run [38111025018](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38111025018).

PRs #1–11 remain `NO_CHECKS` as individual historical PR statuses. Fresh ancestry checks confirm all PR #1–25 heads are included in PR #26, and the integrated full CI passed; retain the individual status in the matrix without treating it as uncovered candidate code. The original PR #23 failed run remains unchanged. G0 remains `FIX_IN_REVIEW` pending human review and an explicit integration decision.


## 2026-10-10 live exact-head refresh — 20261010T221231Z

Current read-only state: [`V2-G0-G1-live-state-20261010T221231Z.json`](../audits/V2-G0-G1-live-state-20261010T221231Z.json) and [`V2-G0-G1-live-refresh-20261010T221231Z.md`](../audits/V2-G0-G1-live-refresh-20261010T221231Z.md). PR #26 head `fa5832f7f81c8cb5438aecc5b8bead9e02ce9cfc` and stacked PR #27 head `d124c1597fcc4ce8655845eb7d68afc4c6552002` pass exact-head hosted quick/full CI. Current PR #1–#25 heads are ancestors of #26; #26 is an ancestor of #27. GitHub reports 27 open PRs, zero submitted reviews, no auto-merge. Status remains `FIX_IN_REVIEW`; next owner action is human review and explicit merge decision. Keep both candidates Draft and unmerged. PR #23 exact-head failure remains unchanged.

## Addendum 2026-10-11 — 已耗尽研究授权可在空克隆重放

- 严重度：S1；状态仍为 `OPEN`，因为修复后的 PR head hosted CI 和组合审查尚未完成。
- 根因：campaign 与 format-pilot 的去重账本按本地 Git clone 隔离，旧授权文件没有跨 clone 的已耗尽状态。
- 影响：V38 A1/A2 的 72 个调用意图（71 个终态）及已完成的单次格式 pilot，在新的空账本克隆中可重新进入执行路径。没有实际重放；原文件和 ledger 摘要见 [`V2-G0-authorization-replay-review-20261011.md`](../audits/V2-G0-authorization-replay-review-20261011.md)。
- 临时方案：不再执行两个旧授权；不重试任何模型调用，不修改原始账本。
- 修复：candidate 新增 fail-closed 撤销登记，覆盖两个执行库和两个 CLI；登记/代码缺失或损坏时拒绝执行。新回归测试用 fake provider 与空临时 ledger 验证拒绝发生在 provider 初始化/调用前。
- 验收：`tests/v38` 154 passed；提交钩子全仓 pytest 为 2528 passed、1 skipped、1 warning；ruff 与 diff 检查通过。仍要求修复后 exact-head hosted quick/full CI 及组合候选审查。
- 回滚：允许撤回执行 guard 代码以恢复其他功能时，必须保留两个撤销记录；不得恢复旧 ID 的可执行状态。不得改写旧研究结果。
- 外部效果：本次没有模型/API、交易所、订单或实盘操作。

## 2026-10-11 current-head refresh and Gate trigger classification review — 06:20Z

The additive live snapshot is [`V2-G0-G1-live-refresh-20261011T062037Z.md`](../audits/V2-G0-G1-live-refresh-20261011T062037Z.md), with per-PR exact head/base/check/review data in [`V2-G0-G1-live-state-20261011T062037Z.json`](../audits/V2-G0-G1-live-state-20261011T062037Z.json). GitHub currently reports 27 open PRs, 0 merged, 0 submitted reviews, and no pending checks. PRs #12–#22 and #24–#27 have exact-head successful checks; PR #23's exact current head remains failed on run `37963206552`; #1–#11 have no checks on their own heads. All PR #1–#25 heads are ancestors of PR #26, and PR #27 is based exactly on #26.

PR #26 head `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1` passed hosted run `38107795624` (quick 231, full 2,538). PR #27 head `ae6ca628d1238be7eaf8e8e5fe49996a746e7460` passed hosted run `38115375371` (quick 286, full 2,593). Both remain Draft and without reviews. The current remote #27 head does not contain the new local patch described below.

Offline code review found that `GateLiveTrader.get_open_trigger_orders()` used a `"close-"` substring and Python truthiness to classify account trigger orders. This could mark an undocumented order type or the string `"false"` as a confirmed protection order. Failure-first fixtures reproduced two misclassifications. The local fix maps sides only from the six exact Gate protection order enums, parses native flags explicitly, and reports malformed, conflicting, or unknown evidence as degraded. Targeted Gate regressions pass 66 tests; the full local suite passes 2,598 with 1 skipped and 0 failures. `git diff --check` passes. Ruff still reports file-level pre-existing diagnostics; none point to the modified lines.

**Status remains `FIX_IN_REVIEW`.** The local patch is uncommitted and has no hosted run. Incorporate it into the candidate, run exact-head hosted quick/full CI, and obtain human review before considering acceptance. No model or Gate account API was called, no order was sent, Live remained untouched, and no historical ledger or frozen sample was modified.

## 2026-10-11 Gate patch hosted acceptance — 06:44Z

The current live snapshot is [`V2-G0-G1-live-refresh-20261011T064456Z.md`](../audits/V2-G0-G1-live-refresh-20261011T064456Z.md), with all 27 PR heads, bases, checks, and review counts in [`V2-G0-G1-live-state-20261011T064456Z.json`](../audits/V2-G0-G1-live-state-20261011T064456Z.json). PR #27 now includes commit `6d4278066fd2516077972bd6ba6a3f2683f8de6b`; exact-head run `38118351684` passed quick 286 and full 2,599 tests, with 25 subtests and one existing warning. PR #26's exact-head run `38107795624` remains green. There are still 27 open PRs, no merged PRs, no submitted reviews, 11 individual heads without checks (#1–#11), and PR #23's historical exact-head failure remains recorded. All #1–#25 heads remain ancestors of #26; #27 remains based exactly on #26.

The Gate trigger-order classification fix is now on the remote Draft PR #27 head and covered by its full hosted run. The local pre-commit suite passed 2,598 with 1 skipped; focused Gate regressions passed 66. PR #26/#27 still require human review and an explicit merge decision. G0 remains `FIX_IN_REVIEW`, not accepted. No Gemini, Gate account or TestNet API, order, Live action, historical ledger, or frozen sample was touched.

## 2026-10-11 live refresh and Gate close-side correction — 07:44Z

Fresh GitHub and local DAG checks record 27 OPEN PRs, 0 merged: 15 current heads have successful checks, PR #23 retains its failed exact-head run 37963206552, PRs #1–#11 still have no checks on their individual heads, and 0 checks are pending. Every current PR #1–#25 head is an ancestor of the G0 candidate. PR #26 remains based on main 8642639a78ca9aa56fbad1e88aea4b82169ea433 at head 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1; PR #27 is based exactly on that head.

Independent API-contract review found that Gate price-trigger initial.size is a closing quantity, not necessarily the action side. The official close-long-order example uses size=100; the previous reader reported BUY even though the order_type closes a long position. A failure-first test reproduced 1 failure and 6 passes. Commit 0ceb0f038a23da7879276769a386f4b2918b6b92 now derives side from the exact documented protection order type; unknown types stay degraded. Full details and rollback scope are in V2-G0-order-type-enum-review-20261011.md.

The failure-first regression now passes. Gate account/TestNet/protection targeted tests passed 67/67. The local hard gate passed 2,599 with 1 skipped; exact PR #27 head CI run 38121493732 passed quick 286 and full 2,600, including 25 subtests and one existing deprecation warning. The original PR #23 failure, individual #1–#11 check gaps, and all historical snapshots remain unchanged.

Current owner action: obtain independent human review of Draft PR #26/#27 and record an explicit integration decision. Do not auto-merge. G0 remains FIX_IN_REVIEW. No model/provider call, Gate account/TestNet/Live request, order, historical ledger mutation, or production account-setting change occurred.
## 2026-10-11 current-head verification update — 08:38Z

- **Severity/status:** S1 / `FIX_IN_REVIEW`.
- **Trigger and cause:** 27 open PRs form a dependent integration stack; missing checks on #1–#11, the red exact-head run for #23, and no independent review on #26/#27 require an explicit integration decision.
- **Original/current evidence:** immutable prior snapshots remain intact. Current snapshot [`V2-G0-G1-live-state-20261011T083833Z.json`](../audits/V2-G0-G1-live-state-20261011T083833Z.json), SHA-256 `ae90124b1f2cb3f4184eefc64c91509fc280f65cc6ae8bc23d81650ccfbb2247`; PR #26 run 38107795624 passed quick/full, and PR #27 exact head `bf265d27d01ec75bb7dcc94c80bd86ddcf132989` run 38122906585 passed quick 286 and full 2,600.
- **Temporary containment:** leave #26/#27 OPEN/Draft, keep auto-merge off and main unchanged; do not erase #1–#11 NO_CHECKS or #23 failure.
- **Repair / acceptance:** maintain PR #26 as the integration candidate and #27 as its child. All #1–#25 current heads were rechecked as ancestors of #26. Require independent review of both candidates and a recorded integration decision before G0 ACCEPT.
- **Rollback:** if review finds a defect or omitted branch, keep main untouched and revise or close the candidate PRs with ordinary commits; preserve this and all earlier snapshots.
- **External effects:** none. No model, private exchange, TestNet/Live, order, or account-settings operation occurred.

## 2026-10-11 baseline refresh — 09:14Z

GitHub REST/API and current-head checks were re-read. `main` remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`; all 27 PRs remain open, none are merged. Exact-head CI inventory is 15 success (#12–#22, #24–#27), #23 failure, #1–#11 with no current-head checks, and none in progress. PR #1–#25 current heads are all verified ancestors of PR #26 head `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1`; PR #27 is based on that exact head.

PR #26 run [38107795624](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38107795624) passed quick 231 and full 2,538 tests. PR #27 current head `e30551fa22125413afe364de3932b9ddf5cfe880` run [38125994063](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38125994063) passed quick 286 and full 2,600 tests. Both are still OPEN/Draft with zero reviews and no auto-merge. The exact machine snapshot is [`V2-G0-G1-live-state-20261011T091403Z.json`](../audits/V2-G0-G1-live-state-20261011T091403Z.json), Git blob SHA-256 `8dfe75e1afad571e763de2b5353c76c2ae9b23537d964609cc3257b4e31fb302` (Windows CRLF worktree SHA-256 `3e6dd6b0dc874c58cc883d3bd917ce50e908b31f04d602d3733bca11225b2594`); the readable refresh is [`V2-G0-G1-live-refresh-20261011T091403Z.md`](../audits/V2-G0-G1-live-refresh-20261011T091403Z.md).

G0 remains `FIX_IN_REVIEW`; CI does not replace independent human review or an explicit integration decision. Keep both candidates unmerged and preserve PR #23's failure and PR #1–#11's missing-check state.

## 2026-10-11 frontend hosted-coverage gap — 09:44Z

Reviewing the V2 acceptance matrix found that `.github/workflows/offline-tests.yml` ran only Python quick/full suites. It did not run the web unit tests, ESLint, or TypeScript/Vite production build, although the V2 integration gate calls for frontend type/unit/build coverage. This is a CI coverage gap, not a newly observed product-code failure.

The local repair adds a Windows `web` job using locked Node 24 dependencies, `npm test -- --run`, `npm run lint`, and `npm run build`. Lint and build are separate steps so a lint failure cannot be masked by a successful build. Its timeout is 45 minutes because the existing serialized 31-file Vitest suite was still actively consuming CPU after 16 minutes locally; a 20-minute hosted limit would risk terminating a valid run. The workflow YAML parses with the repository's PyYAML runtime and `git diff --check` is clean.

At capture time, local `npm ci --no-audit --no-fund` had installed the lockfile dependencies; `npm run lint` and `npm run build` passed. The build emitted the existing large-chunk advisory. `npm test -- --run` remained in progress with no final result yet, so this addendum does not claim frontend-suite acceptance. PR #27 head `e983df58b93157a75adc29ac709eb7a927f50513` run `38128269910` had its Python quick gate pass while the full Python suite remained in progress. The added workflow job has not yet run on a hosted exact head.

**Containment and rollback:** PRs #26/#27 remain Draft and unmerged; no auto-merge is enabled. Revert only the workflow/audit commit if the hosted web job is invalid or unstable; preserve the earlier audit snapshots, historical PR outcomes, and source PR refs. No product/runtime code, trading configuration, research sample, provider, exchange API, or order was changed or called. G0 stays `FIX_IN_REVIEW` pending exact-head CI and independent human review.
