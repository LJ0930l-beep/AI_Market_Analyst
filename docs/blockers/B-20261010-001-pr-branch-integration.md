# B-20261010-001 — PR 分叉集成与组合验收未完成

```yaml
id: B-20261010-001
severity: S1
status: OPEN
component: integration
mode: RESEARCH_OFFLINE
branch_sha: fcfc9ec37102d0ec4a09fc777fec0e44d20291cc
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
unblock_criteria: One candidate SHA includes PR-20/21/22 and PR-25 ancestry (including PR-23/24 fix); changed-file overlap reviewed; full quick and full CI pass; same-environment baseline has zero new failures; rollback point recorded.
needs_human_decision: true
next_owner_action: Build a local candidate from origin/main and merge source heads in documented order without changing remote PRs.
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

`OPEN`。PR #25 本身已通过当前 head CI；阻塞是尚无合并 #20/#21/#22 和 #25 线的组合候选，不是当前 PR #25 CI 失败。
