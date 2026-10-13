# B-20261010-001 — PR 分叉集成与组合验收未完成

```yaml
id: B-20261010-001
severity: S1
status: FIX_IN_REVIEW
component: integration
mode: RESEARCH_OFFLINE
branch_sha: 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1
trigger_signature: PR_COMBINATION_CANDIDATE_AWAITS_HUMAN_REVIEW
playbook_codes: B21,B22
first_seen_at_utc: 2026-10-10T09:27:00Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: true
risk_to_funds: NONE
affected_sample_or_order_ids: PR-1-through-27; no orders
immediate_stop_or_isolation: No remote merge, force-push, TestNet request, or Live action. Preserve all source PR refs.
evidence_files_and_hashes: docs/audits/PR-INTEGRATION-MATRIX-20261010.json Git-blob SHA-256 8318066478e5d767452db920fca4b5eb9d35c37787c610e0f327660445466edc; docs/audits/PR-INTEGRATION-MATRIX-REFRESH-20261010.json SHA-256 B94397AE77DB87C0574C93709D1377103F630F9D76DE358DF8A82BA495BC27E4; docs/audits/PR-INTEGRATION-MATRIX-LIVE-20261013T011953Z.json SHA-256 47EA3731D7E294F837CAE3468FA920ED7BDF92161DB96EDAE58D5863013D6F53.
reproduction: gh pr list --state all --limit 100 --json number,title,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,updatedAt,mergeable,reviewDecision,statusCheckRollup; query check-runs by each exact head SHA
root_cause_status: CONFIRMED
original_evidence: "The 2026-10-13T10:27:43Z GitHub snapshot records 27 OPEN PRs, PR #23 with a failing current-head check, PRs #1-#11 with no reported checks, and PR #27 Draft with no formal review or review request."
root_cause: "The PR set contains forked dependency branches and no accepted, fully reviewed combined candidate; a descendant's green CI does not clear source-head failures or missing checks."
temporary_mitigation: "Keep source PRs intact and PR #26/#27 Draft; use only the isolated main-based candidate and do not merge or force-push."
safe_fallback: Preserve separate branches and perform only a local isolated integration candidate.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/26 and https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_method: "Build and refresh one main-based candidate, verify branch ancestry and changed-file overlaps, run baseline and exact-head CI, then obtain independent review and a human G0 decision."
negative_tests: Candidate must include Gate account isolation, Gate 4h pagination, reduce-only semantics, macro clock behavior, model response identity/parser failures, and full offline CI.
unblock_criteria: One candidate SHA includes PR-20/21/22 and PR-25 ancestry (including PR-23/24 fix); changed-file overlap reviewed; full quick and full hosted CI pass on that exact SHA; same-environment baseline has zero new failures; rollback point recorded; human review and explicit merge approval are complete.
needs_human_decision: true
candidate_branch: codex/v2-g0-integration-20261010
candidate_source_heads: origin/main@8642639a78ca9aa56fbad1e88aea4b82169ea433; PR20@43eb1835ef76b8c39f9b560ceaa3cac1b436d1bf; PR21@941e38372a0c0f96d1aaecd4a4ca2faf62dd3070; PR22@cb03f8e34cedca18d735187fdab41cda388eaa99; PR25@97d138fc3f8072668af1bbeada3475d612f55a7d
candidate_local_verification: "Historical local comparison for PR26: targeted 38 passed; quick 222; full 2517 passed, 1 skipped, 0 failed; baseline 2113 passed, 103 failed, 1 skipped; node-id comparison 0 new failures, 0 missing baseline nodes. See latest exact-head hosted runs below for current counts."
candidate_junit_sha256: baseline ec187fb87f395a5bb3949d358b13614b73dacfbfa873dc7d8a451ef54374ca9b; candidate dad101a071100b00d9071936e13233f1efcfa8813e82a1acd3d29fbbe9c0a733
remote_ci_status: "Current exact-head runs: PR26 head 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1 run 38107795624 SUCCESS (quick 231; full 2538 passed; 25 subtests); PR27 head 5bb0cc34dfe93a1f4b6fa5b312894d5878150503 run 38328844757 SUCCESS (quick 289; full 2605 passed; 25 subtests; Web tests/lint/type-check/build SUCCESS). PR23 current-head failure and PR1-11 no-check statuses are retained."
next_owner_action: "As of 2026-10-13T01:21:27Z: PR #26/#27 are OPEN/Draft with zero reviews and requests; auto-merge is disabled. PR26 exact-head run 38107795624 and PR27 exact-head run 38328844757 passed. Current inventory has 27 OPEN PRs; the #1-25 heads are 25/25 ancestors of #26, and #26 is an ancestor of #27. Review the 173 changed-path overlap pairs semantically and obtain an independent human review/recorded G0 decision. Preserve #1-#11 NO_REPORTED_CHECKS and #23's three-test failure. Do not merge."
rollback: "Abandon or revert only the isolated PR #26/#27 candidate commits; preserve original PR refs, the main baseline, historic failure records, frozen ledgers, V35 risk controls, and all prior snapshots. Never force-push or auto-merge."
```

## 故障原因与原始证据

在 2026-10-10 首次快照中，25 个 PR 全部开放。PR #20 从 #19 分出，#21/#22 建立在 #20；PR #23 也从 #19 分出，#24/#25 建立在 #23。PR #25 单支绿色不证明包含 #20/#21/#22 的组合正确。当前 27-PR 拓扑和完整 path overlap 见本文件开头记录的新机器快照；其中 PR #20 与 #23/#24/#25 存在 Gate E2E、macro calendar、测试或审计文档重叠。

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

## 2026-10-11 live exact-head refresh — 12:29Z

The read-only GitHub refresh at 2026-10-11T12:29:59Z still shows 27 OPEN PRs and 0 merged. Exact-head inventory: 15 successful, PR #23 failed, PR #1–#11 with no reported checks, and none pending. PR #1–#25 heads were individually verified as ancestors of PR #26 head 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1. PR #26 is based on main 8642639a78ca9aa56fbad1e88aea4b82169ea433; PR #27 head 230550b87abccb1530bc34e9997a8afee46732c4 is based exactly on #26.

PR #26 exact-head run 38107795624 passed its Python quick/full checks (231 / 2,538). PR #27 exact-head run 38136662102 passed Python quick/full (286 / 2,600) and Web tests (29 files / 161 tests), lint, and build. Same-host JUnit comparison against the frozen baseline found 103 baseline failure nodes now passing, zero new failures, and zero missing baseline nodes. The machine snapshot is docs/audits/PR-INTEGRATION-MATRIX-20261011.json; the readable report is docs/audits/V2-G0-G1-live-refresh-20261011T122959Z.md.

Both candidates remain OPEN/Draft with zero review events and auto-merge disabled. Status remains FIX_IN_REVIEW. Required owner action is independent human review and a recorded integration decision. Preserve PR #23's failed check and PR #1–#11's no-check state; do not auto-merge or modify main. No model, exchange, TestNet, Live, order, or account-setting operation occurred.

## 2026-10-12T18:07:29Z 当前状态刷新

- **严重度/状态：** S1 / `FIX_IN_REVIEW`；根因仍是 stacked PR 集成需要独立人工审查和明确决策。
- **最新证据：** [`V2-G0-G1-live-refresh-20261012T180729Z.md`](../audits/V2-G0-G1-live-refresh-20261012T180729Z.md) 与 [`V2-G0-G1-live-state-20261012T180729Z.json`](../audits/V2-G0-G1-live-state-20261012T180729Z.json)。GitHub 仍有 27 个 OPEN、0 个 merged PR；精确 head 检查为 15 成功、#23 失败、#1–#11 无报告检查、0 pending。main `8642639a78ca9aa56fbad1e88aea4b82169ea433` 未变；#26 head `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1`，#27 head `66ef1f707652c322eb5c33778485a529b52804e3`，#27 基于 #26；#1–#25 heads 均是 #26 的祖先。
- **验收证据：** PR #26 run 38107795624 在其精确 head 通过；PR #27 run [38286178147](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38286178147) 在 `66ef1f707652c322eb5c33778485a529b52804e3` 通过（Python quick 286；full 2,600、25 subtests；Web 161、lint、type-check/build 通过）。PR #26/#27 仍 OPEN/Draft、无 review/request、auto-merge 关闭，所以尚未达到 G0 ACCEPT。
- **临时方案：** 保持候选分支隔离、#26/#27 Draft/open、main 不变、不自动合并；保留 #23 红色结果和 #1–#11 NO_REPORTED_CHECKS。
- **修复/下一步：** 候选组合和测试证据已刷新；由独立人工 reviewer 检查集成和重叠文件，并记录明确的集成决定。若有意见，只在隔离候选追加普通修复提交并跑精确 head CI。
- **回滚：** 如本次记录事实错误，回退该次审计文档提交并新增更正快照；不得改写既有矩阵/历史记录或 main。
- **外部影响：** 无模型或交易所 API 调用、订单、费用、账户或生产参数变更。

## 2026-10-12T18:38:22Z 当前状态刷新

- **严重度/状态：** S1 / `FIX_IN_REVIEW`；根因仍是 stacked PR 集成等待独立人工审查与明确决定。
- **原始证据：** [`V2-G0-G1-live-refresh-20261012T183822Z.md`](../audits/V2-G0-G1-live-refresh-20261012T183822Z.md) 与 [`V2-G0-G1-live-state-20261012T183822Z.json`](../audits/V2-G0-G1-live-state-20261012T183822Z.json)；27 个 PR head、检查、review/request 已刷新；PR #26/#27 reviews 与 requests 为空。
- **临时遏制：** PR #26/#27 保持 OPEN/Draft，main 不变、auto-merge 关闭；保留 #23 failure 与 #1–#11 NO_REPORTED_CHECKS。
- **候选修复与验收：** #1–#25 heads 为 #26 head 的祖先；#26 exact-head run 38107795624 成功，#27 exact head `72dd60b19b1b3a75baed47f44a029e1382d87c38` run 38289819059 成功（quick 286/full 2600、25 subtests、Web 161、lint/type-check/build）。以上不替代 reviewer 与集成批准。
- **回滚：** 如本快照字段不准确，撤销仅本次审计文档变更并新增更正快照；不改写历史结果、旧快照、来源 PR 或 main。
- **外部影响：** 无模型、交易所、TestNet/Live 请求或订单；无账户参数和研究样本变更。

## 2026-10-12T19:21:18Z G1 本地修复待远端验证

- **只读状态刷新：** PR #26/#27 仍 OPEN/Draft、无 reviews/requests、未启用 auto-merge；PR #27 exact head `5f70d186d169261f0f337232c70e246a2e88b4e6` 的 run `38293103442` 成功。该 run 早于本地多 choice 修复，不能作为修复验收证据。
- **新增独立问题：** 在 #27 基线复现 V38 evaluator 对多个 provider choices 静默读取第一条并返回有效；本地现要求 choices 恰为一个对象，修复与回归测试见 B-20261012-005。推送后的精确 head hosted CI 待验证。
- **G0 状态：** 仍待独立人工集成审查与明确决策。保留 #23 failure、#1–#11 `NO_REPORTED_CHECKS`，不得自动合并。
- **安全边界：** 本轮未调用 Gemini、Gate/TestNet/Live API，未提交订单，未改生产配置、历史账本或样本。

## 2026-10-12T19:54:50Z G1 修复精确 head CI 完成

- **精确提交：** PR #27 head `f4461a72486b8aba132eb23045e03c9b1ffe0ba3`；run [38298606199](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38298606199) 全部成功。
- **测试结果：** Python quick 289 passed；full 2,603 passed、25 subtests、1 Starlette/httpx deprecation warning；Web 29 个文件/161 tests、lint、type-check 与 build 通过。远端 full pytest 用时 1,033.02s。
- **结论：** B-20261012-005 修复在精确 head 的自动化 CI 中通过。PR #26/#27 仍 OPEN/Draft、无 reviews/requests；G0 仍需独立人工 review 和集成决定。
- **安全边界：** 未调用模型或交易所 API、未下单、未更改真实账户配置；历史样本和 ledger 未修改。保留 PR #23 failure 与 #1–#11 `NO_REPORTED_CHECKS`。

## 2026-10-12T21:46:04Z live refresh

The immutable machine and prose refresh are docs/audits/V2-G0-G1-live-state-20261012T214604Z.json and docs/audits/V2-G0-G1-live-refresh-20261012T214604Z.md. The matrix comparison found only PR #27 head changed since the 20:29 snapshot; all other PR base/head SHAs stayed the same. Fresh inventory remains 27 open / 0 merged: 15 current heads pass, #23 fails, #1–#11 have no reported checks, and 0 are pending. PR #1–#25 ancestry into #26 was rechecked at 25/25; #26 remains an ancestor of #27.

PR #26 exact head 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1 passed run 38107795624. PR #27 exact head 952d38a8e4d6aa800c507f6c54ee9477d744181b passed run 38309564947: quick 289, full 2,605, 25 subtests, one existing deprecation warning; Web/lint/type-check/build passed. Both PRs remain OPEN/Draft with zero reviews and requests and no auto-merge.

The latest offline increment adds Gate 4h pagination coverage and a calibration missing-digest/no-model-call regression. It does not cover the required positive calibration remote mode. The G0/G4 dependency conflict is tracked as B-20261013-006. G0 remains not accepted; do not merge, open G2, or rewrite PR #23's failure. Rollback remains limited to the test-only commit 952d38a if it causes a regression; retain all prior snapshots and ledgers.

## 2026-10-12T23:38:26Z exact-head and all-PR refresh

- **Severity/status:** S1 / `FIX_IN_REVIEW`; integrated PR review and recorded G0 acceptance are still absent.
- **Original evidence:** [current prose audit](../audits/V2-G0-G1-live-refresh-20261012T233826Z.md) and [machine snapshot](../audits/V2-G0-G1-live-state-20261012T233826Z.json), SHA-256 `A2200D337DCFDDAAF07183F74CD1E1D875AD7D5977FED9621D46EE3F2AA66EB6`. Handbook SHA-256 remains `A31A70795C02B9F5BE3ED505361AA3133ADFE82DA09DDD18DE8FE5D482841B4D`; `main` remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`.
- **Current inventory:** 27 OPEN, 0 merged; 15 current heads pass, PR #23 fails, PR #1–#11 have no reported checks, 0 pending. Only PR #27 head changed since the 21:46 snapshot; its base and PR #1–#26 base/head SHAs did not change.
- **Candidate evidence:** PR #26 exact head `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1` remains green. PR #27 exact head `86ab6fe124b2541fd7f73482dbabe2dd23f63b48` passed run `38320355179` (quick 289, full 2,605 / 25 subtests, Web/lint/type-check/build). Both remain OPEN/Draft with no review requests and auto-merge off.
- **Root cause and containment:** Historical #23 failure and #1–#11 no-check state are retained as evidence, not edited away. Keep both candidate PRs unmerged and remote dispatch disabled. The B-006 ordering decision is resolved; G0 remains unaccepted pending independent review and remaining G0 criteria.
- **Repair/acceptance:** PR #26/#27 remain the review candidate; acceptance still requires an independent review and a recorded G0 decision on the integrated SHA. No direct merge or `main` update is authorized.
- **Rollback:** If this snapshot is inaccurate, revert only the new audit/blocker documentation and add a correction snapshot; preserve source PRs, prior matrices, code, ledgers, and `main`.
- **External effects:** No Gemini/Antigravity/Gate request, order, production setting change, or automatic merge occurred. Ledger files were only rehashed; contents were not read or changed.

## Live refresh 2026-10-13T04:13:54Z
- Machine snapshot: docs/audits/PR-INTEGRATION-MATRIX-LIVE-20261013T041354Z.json SHA-256 F6AEFE5AB90CAE32FA12998410784A6B474634C28ABBD96CF1964BC0243119F4; prose report docs/audits/V2-G0-G1-current-state-20261013T041354Z.md SHA-256 B45C5F84C3ABE25FEEBA0196C820B0DAC775EA0AB0D3ACF6D2DB7B882122987B.
- Live inventory: 27 OPEN, 0 merged; 15 PASS, #23 FAILURE, #1-#11 NO_REPORTED_CHECKS, 0 pending.
- PR #26 00d1c4405a513b9e5cdee9f6fa15808b4386d8b1 passed run 38107795624. PR #27 4abdcdfe8917dbc3527196765369f73b4913f6c9 passed run 38343135615: quick 289, full 2616, 25 subtests, Web 29 files/161 tests, lint/typecheck/build.
- PR #27 is main-based and contains all PR #1-#26 heads in its ancestry. The current exact path matrix does not claim a complete semantic review.
- Both candidates are OPEN/Draft with zero reviews/requests. G0 remains NOT_ACCEPTED pending independent review and a recorded human decision. No merge or external calls occurred.

## Live inventory and exact-head result — 2026-10-13T09:47:51Z

- Fresh GitHub inventory: 27 OPEN, 0 merged, 0 closed-unmerged. Current-head checks: 15 SUCCESS, PR #23 FAILURE, PR #1–#11 have no reported checks, 0 pending.
- `main` remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`. PR #26 remains Draft at `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1`; PR #27 remains Draft, directly based on `main`, at `90344019d20a7db1464ddd7e82f3378bd3e791de`.
- Exact PR #27 run [38374291874](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38374291874) succeeded: Python quick 289 passed; full suite 2,683 passed, 25 subtests passed, one existing Starlette/httpx deprecation warning; Web tests, lint, type-check, and build passed.
- PR #27 has no submitted reviews or review requests. G0 remains NOT_ACCEPTED; this CI pass is not independent approval. No merge, model/provider call, Gate/TestNet request, or order occurred.
- Frozen V38 intent ledger remains 512,044 bytes with SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`. Both V2 prompt configs remain remote-disabled and unauthorized.

## Additional S1 finding 2026-10-13T04:41:39Z

B-20261013-007 was detected during independent code review. Exact pre-entry versus final-account equality was not enforced for positions and pending-order IDs, allowing two fixture cases to return COMPLETED after baseline state changed. Failure-first reproduction: 5 passed / 2 failed. The local repair requires exact equality and returns CLEANUP_BASELINE_CHANGED for lost baseline state. See docs/blockers/B-20261013-007-gate-cleanup-baseline-preservation.md and docs/audits/V2-G0-G1-cleanup-baseline-followup-20261013T044139Z.md. The existing PR #27 hosted run on 166b5841 does not verify this uncommitted repair. G0 remains NOT_ACCEPTED; do not merge.

## 2026-10-13T11:16:41Z current candidate refresh

- **Original evidence:** [`V2-G0-G1-live-refresh-20261013T111641Z.md`](../audits/V2-G0-G1-live-refresh-20261013T111641Z.md) and [`PR-INTEGRATION-MATRIX-LIVE-20261013T111641Z.json`](../audits/PR-INTEGRATION-MATRIX-LIVE-20261013T111641Z.json), matrix SHA-256 `6A9D5DE04B1C6267F4B6AF89BBE308E518BC101C27D6A9768061D783CA57C780`.
- **Current repository state:** `main` remains `8642639a78ca9aa56fbad1e88aea4b82169ea433`; all 27 PRs are OPEN, none merged. Current-head checks: 15 PASS, #23 FAILURE, #1–#11 NO_REPORTED_CHECKS. PR #26/#27 remain Draft.
- **Candidate integration:** PR #27 head `928b35844d2f03397c611a29aa13fe710aff4d37` is based on `main`; all current PR #1–#26 heads were verified as ancestors. This ancestry proof is not a complete semantic review. PR #27 remains at 305 changed files (+105,888/−560), with no submitted review or review request.
- **Exact-head verification:** run [38386090835](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38386090835) passed on exact head `928b35844d2f03397c611a29aa13fe710aff4d37`: quick 295 passed; full suite 2,704 passed / 25 subtests / one existing deprecation warning; Web tests, lint, type-check, and build passed.
- **Disposition:** G0 remains NOT_ACCEPTED pending complete independent review and recorded human decision. Preserve #23's failure and #1–#11 no-check evidence. Keep PRs Draft/unmerged, remote dispatch disabled, and main unchanged. No model/exchange request or order occurred.
- **Next action and rollback:** obtain a formal independent review of the exact current candidate; if the review identifies omissions, fix only on the isolated candidate and rerun exact-head CI. If this snapshot is inaccurate, append a correction snapshot; do not rewrite prior matrices or source PRs.
