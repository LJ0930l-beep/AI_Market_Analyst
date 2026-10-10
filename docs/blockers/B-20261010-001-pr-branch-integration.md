# B-20261010-001 — PR 分叉集成与组合验收未完成

```yaml
id: B-20261010-001
severity: S1
status: OPEN
component: integration
mode: RESEARCH_OFFLINE
branch_sha: 97d138fc3f8072668af1bbeada3475d612f55a7d
trigger_signature: PR_BRANCHES_DIVERGE_WITHOUT_SINGLE_COMBINATION_CANDIDATE
first_seen_at_utc: 2026-10-10T09:27:00Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: true
risk_to_funds: NONE
affected_sample_or_order_ids: PR-1-through-25; no orders
immediate_stop_or_isolation: No remote merge, force-push, TestNet request, or Live action. Preserve all source PR refs.
evidence_files_and_hashes: docs/audits/PR-INTEGRATION-MATRIX-20261010.json SHA-256 8318066478e5d767452db920fca4b5eb9d35c37787c610e0f327660445466edc.
reproduction: gh pr list --state all --limit 100 --json number,headRefName,headRefOid,baseRefName,baseRefOid,files,statusCheckRollup
root_cause_status: CONFIRMED
safe_fallback: Preserve separate branches and perform only a local isolated integration candidate.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/25
negative_tests: Candidate must include Gate account isolation, Gate 4h pagination, reduce-only semantics, macro clock behavior, model response identity/parser failures, and full offline CI.
unblock_criteria: One candidate SHA includes PR-20/21/22 and PR-25 ancestry (including PR-23/24 fix); changed-file overlap reviewed; full quick and full hosted CI pass on that exact SHA; same-environment baseline has zero new failures; rollback point recorded.
needs_human_decision: true
candidate_branch: codex/v2-g0-integration-20261010
candidate_source_heads: origin/main@8642639a78ca9aa56fbad1e88aea4b82169ea433; PR20@43eb1835ef76b8c39f9b560ceaa3cac1b436d1bf; PR21@941e38372a0c0f96d1aaecd4a4ca2faf62dd3070; PR22@cb03f8e34cedca18d735187fdab41cda388eaa99; PR25@97d138fc3f8072668af1bbeada3475d612f55a7d
candidate_local_verification: targeted 38 passed; quick gate 222 passed; full 2517 passed, 1 skipped, 0 failed; baseline 2113 passed, 103 failed, 1 skipped; node-id comparison 0 new failures, 0 missing baseline nodes.
candidate_junit_sha256: baseline ec187fb87f395a5bb3949d358b13614b73dacfbfa873dc7d8a451ef54374ca9b; candidate dad101a071100b00d9071936e13233f1efcfa8813e82a1acd3d29fbbe9c0a733
remote_ci_status: PR25 documentation head run 38042151994 passed quick gate and full suite was in progress at last observation; no hosted run yet for candidate branch.
next_owner_action: Publish the verified candidate as a draft integration PR; verify same-head hosted quick/full checks and wait for human review; do not merge.
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

`OPEN`。本地候选已合并 #20/#21/#22/#25 并通过同环境完整测试和基线差集；仍待 draft PR 上相同 candidate SHA 的 hosted quick/full CI 与人工审查。PR #25 自身的旧 head 绿灯不替代组合候选验收。
