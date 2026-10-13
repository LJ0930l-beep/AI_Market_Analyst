# V2.0 G0/G1 实时基线刷新与复审记录 — 2026-10-10

## 1. 本轮目标与证据基线

重新核实 GitHub `main`、全部开放 PR、每个当前 PR head 的 check-runs 与 G0/G1 候选状态。没有把早先快照或绿色单支检查当作当前集成证明。

- V2.0 手册 SHA-256：`A31A70795C02B9F5BE3ED505361AA3133ADFE82DA09DDD18DE8FE5D482841B4D`。
- `main` 经 GitHub API、`git ls-remote` 和 `origin/main` 三方核实仍为 `8642639a78ca9aa56fbad1e88aea4b82169ea433`。
- 历史矩阵 `PR-INTEGRATION-MATRIX-20261010.json` 保持不变；以 Git blob 字节计算的 SHA-256 是 `8318066478e5d767452db920fca4b5eb9d35c37787c610e0f327660445466edc`。Windows 工作树换行形式的哈希不同，不代表 Git 中历史文件已改写。
- 本次刷新矩阵 `PR-INTEGRATION-MATRIX-REFRESH-20261010.json` 记录 2026-10-10T11:23:57Z 的 27 个 PR、base/head SHA、current-head checks 和依赖关系；SHA-256：`B94397AE77DB87C0574C93709D1377103F630F9D76DE358DF8A82BA495BC27E4`。

## 2. 开放 PR 与分支依赖

快照时全部 **27 个 PR 均 OPEN**。当前分支拓扑为：

- #1 指向 `main`；#2–#19 依次建立在前一 PR 分支上。
- #20 从 #19 分出；#21 和 #22 都以 #20 为 base。
- #23 也从 #19 分出；#24 建立在 #23 上，#25 建立在 #24 上。
- #26 是从 `main` 起步的人工审查用整合候选，包含 #20/#21/#22，并合入含 #23/#24 修复的 #25 ancestry。
- #27 以 #26 为 base，承载 G1 的 V38 Response Contract V2 离线实现。

PR #23 仍在自己的当前 head 上失败；#24/#25 以及整合候选的通过只证明这些后代 SHA，不改写 #23 的历史检查。PR #1–#11 在当前 head 上没有 check-runs；#12–#22、#24–#27 有成功检查。

| 当前 head 检查 | PR 数 | 结论 |
|---|---:|---|
| 无 check-runs | 11（#1–#11） | 不计为通过 |
| 成功 | 15（#12–#22、#24–#27） | 仅覆盖表中各自精确 SHA |
| 失败 | 1（#23） | 保留失败记录 |

### G0 候选

- PR #26：base `main@8642639a78ca9aa56fbad1e88aea4b82169ea433`，head `ffc265be9bdcb1f97aef5c80d572636ca9cef035`。
- 同 head GitHub run [38043671492](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38043671492) 成功：快速门禁 222 项通过；完整套件 2,518 项通过、25 个子测试通过、0 失败、1 条既有弃用警告。
- 同环境本地候选与 `main` 基线对比仍为 0 个新增失败节点；PR #26 是 Draft、未合并、尚无人工审查结论。

### G1 候选快照

- PR #27：base #26 的 head `ffc265be9bdcb1f97aef5c80d572636ca9cef035`；快照时 head `732a77ee415461caa7c74b6faba2a9807b45ba77`。
- 同 head GitHub run [38046845072](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38046845072) 成功：快速门禁 247 项通过；完整套件 2,543 项通过、25 个子测试通过、0 失败、1 条既有弃用警告。
- 该 head 的本地完整套件记录为 2,542 passed、1 skipped、0 failed。它与 hosted 汇总相差一项；两次结果分开保存，不推断差异原因。
- PR #27 是 Draft、未合并。该快照之后的本地复审发现并修正了 V2 evaluator 的两项证据表达边界，修订后的完整测试属于本报告提交所包含的后续 G1 更新，不能用旧 head CI 代替；新 head 的验证结果记录在 PR #27。

## 3. V38 研究证据与 G1 判定

- 冻结 72-intent 账本 SHA-256 仍为 `820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7`；原始 67 个 `INVALID_JSON`、4 个 `CALL_ERROR_NO_RETRY`、1 个 `AMBIGUOUS_NO_RESULT_NEVER_RETRY` 未重写、重标或重试。
- 离线分类确认 67 个内容都是完整单层 JSON fence；可解析性兼容诊断不并入旧分析结果或评分分母。完整 HTTP body、代理是否执行 `response_format`、真实费用和权重证明依然缺失/未知。
- G1 V2 离线契约明确区分 payload model 字段与适配器响应身份回执。报告的字段一致仅代表响应身份字段匹配，不是底层模型权重证明。
- 新响应解析、Schema/evidence_refs、空/截断/finish、身份、transport、超长与成本未知负例只在 fake fixture 上运行。能力始终为 `unknown_unverified`，prompt 远程调用额度为 0。
- G1 离线代码是否通过以 PR #27 **更新后的精确 head CI** 为准；真实代理能力仍为 `BLOCKED_WITH_EVIDENCE`，G2 保持 `NOT_AUTHORIZED`。

## 4. 本次工作交付（手册要求的八项）

1. **目标**：刷新 G0/G1 基线、纠正集成 blocker 的实时状态，并加固 V2 离线证据边界。
2. **改动文件**：新增本实时复审报告与刷新矩阵；更新 B-20261010-001 和 blocker 索引；修订 `response_contract_v2.py` 和对应测试；不改原始矩阵和 V38 账本。
3. **不变量**：`main`、PR 源分支、V1 runner/prompt、生产交易路径、V35 风控、72-intent 账本和 sealed split 保持不变。
4. **测试**：快照时 #26/#27 exact-head hosted CI 如上。后续 evaluator 补丁定向测试 `34 passed`、仓库 quick gate `256 passed`、本地全量 `2,551 passed, 1 skipped, 0 failed`（359.38s，1 条既有 warning）；Ruff check/format 与 `git diff --check` 通过。完整远端验证以 PR #27 更新后新 head CI 结果为准。
5. **失败情形**：缺响应身份回执、payload/adapter 字段不一致、非有限/非正 transport byte count、坏 JSON、错误/未来 evidence_refs 都 fail closed；PR #23 原失败仍单独保留。
6. **局限**：本地 fixture 不能证明 Antigravity 结构化输出能力、模型底层权重、提供方幂等、用量真实性或实际账单。
7. **授权/成本**：没有 Gemini、Gate/TestNet/Live API 请求、订单或新费用；本任务没有扩大已消耗授权。
8. **回滚**：只可 revert PR #27 中新增 V2 evaluator/测试/审计快照；不回写历史账本、不改 PR #20–#25、不动 `main`；PR #26/#27 均保持 Draft，禁止自动合并。

## 5. 阻塞与下一关

- **B-20261010-001**：组合候选及 exact-head hosted CI 已验证；状态更新为 `FIX_IN_REVIEW`，等待人工审查/明确合并决定。
- **B-20261010-002**：G1 离线契约修订可以本地验证；真实路由能力、实际账单及权重证明未知，仍 `BLOCKED_WITH_EVIDENCE`。
- **G2**：旧 72 intents 和格式 pilot 授权已耗尽。新受控调用仍需独立明确授权、全新冻结范围、请求上限和预算/停止规则；本阶段不调用。

下一动作是完成人工审查 PR #26/#27；任何代码调整后按同一候选 SHA 重跑 quick/full CI。未有单独 G2 授权前继续只做离线工作，不跨越未通过的 Gate。
