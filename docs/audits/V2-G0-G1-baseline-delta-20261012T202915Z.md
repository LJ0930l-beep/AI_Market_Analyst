# V2.0 G0/G1 基线差异与当前状态

**观察时间：** 2026-10-12 20:29:15 UTC
**仓库：** `LJ0930l-beep/AI_Market_Analyst`
**分支：** `codex/v2-g1-offline-20261010`
**本次观察到的 PR #27 head：** `ae746242e604e8a33ee231e51e0937323d8dd219`
**上游开发手册 SHA-256：** `a31a70795c02b9f5be3ed505361aa3133adfe82da09ddd18de8fe5d482841b4d`
**机器可读快照：** [PR-INTEGRATION-MATRIX-20261012T202915Z.json](PR-INTEGRATION-MATRIX-20261012T202915Z.json)，SHA-256 `F7694E9609011EB04E86E417548E79A3EDD1EAB51EDC837501E1F1FDB6156E31`。

本报告是该时点的独立快照；旧审计、原始账本、样本和先前矩阵均保留。手册基线历史记录与当前 GitHub 状态分开陈述。

## 手册基线与当前 GitHub 状态

| 项目 | 手册中的历史基线 | 2026-10-12 当前观察 |
|---|---|---|
| main | `8642639a78ca9aa56fbad1e88aea4b82169ea433` | 同一 SHA，未变 |
| PR 数量 | 25 个 open | 27 个 open，0 个 merged |
| PR #25 | head `fdaa380918…` | PR #25 head 已前进；完整当前 head 见机器快照 |
| 报告测试 | 约 2,488 passed、1 skipped | PR #27 的当时精确 head 检查通过：Python quick 289；Python full 2,603 passed、25 subtests；Web 29 files / 161 tests；lint、type-check、production build 均通过 |

手册数字是历史基线，不代表本次验证结果。当前 27 个 PR 的 exact-head 检查分布为：15 个全通过、PR #23 有失败检查、PR #1–#11 没有报告检查、0 个 pending。PR #23 的失败仍记录为失败；后代分支通过不能改写它的结果。

## G0 集成门

GitHub main 仍为基线 SHA。当前集成候选 [PR #26](https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/26) 的 head 为 `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1`，其父分支组合包含 PR #1–#25；本地 Git 对这 25 个当前 head 逐一执行 ancestry 验证，25/25 均为候选祖先。PR 依赖边、并行分支和文件重叠见机器快照。

PR #26 和堆叠的 [PR #27](https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27) 均保持 OPEN / Draft；截至观察时两者都没有 review、review request 或 auto-merge。#26 的当前检查通过；#27 在当时精确 head `ae746242e604e8a33ee231e51e0937323d8dd219` 的运行 [38301747891](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38301747891) 全部通过。

**G0 结论：等待独立人工审查，尚未验收。** 不进行合并。PR #23 的既有红色检查和 PR #1–#11 无检查记录继续作为集成审查材料。

## G1 离线响应契约

B-20261012-005 的离线修复要求响应 `choices` 必须是恰好包含一个对象的列表；多项、空列表或非对象内容均 fail closed。回归覆盖冲突的多 choice、空 choice 和非对象 choice。它只改变离线解析与验证，不会因匹配模型 ID 或检查缓存而触发模型调用。

当时 PR #27 精确 head 的 quick/full/Web、lint、type-check 和构建检查均通过。B-005 状态仍为 `FIX_IN_REVIEW`，等待独立审查；离线 CI 不证明上游 Gemini 的 structured-output 能力或真实响应身份。

## V38 冻结研究证据

已有分类结果保持冻结，未重试或重新标注任何请求：

- 72 个冻结 intent；71 个 terminal，另有 1 个 unmatched ambiguous。
- 分类合计：67 个 `INVALID_JSON`、4 个 `CALL_ERROR_NO_RETRY`、1 个 `AMBIGUOUS_NO_RESULT_NEVER_RETRY`。
- 有效分析为 0，质量样本为 0。
- 单独的 1-call format pilot 不属于这 72 个 intent，也不具备质量样本资格。
- 原始账本先前只读核验为 144 行、512,044 bytes，SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`；独立 pilot 账本先前只读核验 SHA-256 `4852F2F893F3F68FCA325D5FF07621958C42336861DC3CED2D59DA423AEBDF19`。

忽略目录中的原始账本不在当前 managed worktree 中。本轮未读取其内容；上列账本哈希和计数承接先前从 primary checkout 做的只读核验。当前分类报告的 worktree 哈希记录在机器快照中。历史报告和原始分类均未修改。

## PR #27 修复增量

从修复前 head `5f70d186d169261f0f337232c70e246a2e88b4e6` 到观察时 head 的 8 个路径为：

- `core/replay/pa_decision_quality_v38/response_contract_v2.py`
- `tests/v38/test_response_contract_v2.py`
- `docs/audits/V2-G0-G1-live-refresh-20261012T191510Z.md`
- `docs/audits/V2-G0-G1-live-state-20261012T191510Z.json`
- `docs/blockers/B-20261010-001-pr-branch-integration.md`
- `docs/blockers/B-20261010-002-model-response-capability.md`
- `docs/blockers/B-20261012-005-v38-multiple-choice-ambiguity.md`
- `docs/blockers/index.yaml`

其中，与未变更 head 的 PR #1–#25 相比，本增量只与 PR #25 的 blocker 记录有文本重叠：B-001、B-002 和 `docs/blockers/index.yaml`。这是追加的状态与审计记录；未改动它们的生产逻辑。上一次提交 `f4461a7` 至观察 head 的 4 个文档路径也列在机器快照中。

## 阻塞与影响

- B-001 / G0：仍需独立人工审查和明确集成验收。
- B-002：provider 原生 structured output、远端模型/权重身份、账单和周额度结算仍未证实。
- B-005：已修复并通过离线 exact-head CI，仍待独立审查。
- `remote_dispatch_allowed=false`、`max_calls=0`；G2 为 `NOT_AUTHORIZED`。
- 本次刷新没有 Gemini/Antigravity 或交易所 API 调用、没有订单，也没有生产资金或风险配置改动；没有自动合并。

## 当前验收判断

G1 的离线多 choice 缺陷已由测试和当时精确 head CI 验证。G0 因没有独立人工审查仍未验收；G2 继续关闭。整个 V2.0 目标尚未完成。后续需先完成人工 G0 审查及明确批准，再按手册继续门禁；外部模型调用必须另有新的明确授权和边界。
