# V2.0 G0/G1 离线核查与修复规划 — 2026-10-10

## 本轮范围

本报告落实《AI Market Analyst V2.0：开发执行总纲与阻塞处置手册》的 G0 集成基线和 G1 模型响应可靠性核查。只读检查 GitHub、当前 checkout、冻结 V38 账本和现有测试；未调用 Gemini、Gate API 或任何交易接口，未创建订单。

手册文件 SHA-256：`A31A70795C02B9F5BE3ED505361AA3133ADFE82DA09DDD18DE8FE5D482841B4D`。

## 当前事实与工作区边界

- 重新向远端查询并 fetch 后，GitHub `main` 仍为 `8642639a78ca9aa56fbad1e88aea4b82169ea433`，与手册记载一致。
- 默认 checkout 是 `D:\RJ\codex\ai-market-analyst`，分支 `codex/v35-feasibility`，HEAD `2a5d7b9992a7909a6a597917e409ecb0737f9c89`。其中 5 个已有未跟踪审计条目（含 `.audit-temp/`）未触碰。
- 本轮 V38 worktree 是 `C:\Users\baicha\.codex\worktrees\v38-current-gates\ai-market-analyst`，分支 `codex/v38-authorized-gemini-runner`，HEAD `fcfc9ec37102d0ec4a09fc777fec0e44d20291cc`；核查开始时 clean。
- PR #1–#25 当前全部 OPEN。完整 base/head SHA、逐 PR 文件清单、直接分支依赖和 GitHub 状态检查快照见 [`PR-INTEGRATION-MATRIX-20261010.json`](PR-INTEGRATION-MATRIX-20261010.json)。

## G0：集成与基线

### 已核实

1. 当前不是单链。PR #1–#19 形成共同历史；#20 从 #19 分出，#21/#22 再从 #20 分出；#23 则另从 #19 分出，随后 #24、#25 依次建立在这条分支线上。PR #25 当前 base 为 PR #24 的 head `5e9ef3781a06cdab1f70353242fdd8124bf2ad11`。
2. 因此，PR #25 的通过不能覆盖 PR #20/#21/#22 的改动。PR #25 相对其直接 base 改 23 个文件，但相对 `main` 是 195 个文件、28,286 行新增、467 行删除；其累计 CI 通过仍不等同于把两条分叉合并后的组合验收。
3. PR #23 的 exact-head CI run `37963206552` 失败：三个 `tests/test_macro_actuals.py` 用例因宏日历 writer/status 对可用时间和测试时钟处理错误失败。PR #24 修改了 `core/macro_calendar.py` 与对应测试；其 exact-head run `37966405672` 成功。PR #25 继承了 #24 的修复。
4. PR #25 当前 exact head `fcfc9ec37102d0ec4a09fc777fec0e44d20291cc` 的 GitHub Actions run `38040115559` 已完成：快速门禁 222 passed；完整套件 2,513 passed、1 warning、25 subtests，结论 SUCCESS。PR #25 当前 OPEN、`CLEAN`、auto-merge 未配置。该证据只覆盖 PR #25 这条分支线。
5. PR #12–#22 现有报告的检查为成功；PR #23 为失败；PR #24 和当前 PR #25 为成功。PR #1–#11 在 GitHub 当前 PR 查询中没有报告 `statusCheckRollup`，不能将其 `CLEAN` 误称为曾通过当前 CI。PR #20/#22 的单支绿色也没有覆盖与 #23–#25 的组合。
6. 已确认分叉间存在文件重叠：#20 与 #23 重叠 `core/trading/gate_testnet_e2e.py`、`tests/test_gate_testnet_ai_chain.py`；#20 与 #24 重叠 `core/macro_calendar.py`、`tests/test_macro_actuals.py`；#20/#25 与 #23/#25 还共享 E2E 测试文件；#21、#24、#25 共改主计划文档。细节已记录在机器矩阵。
7. PR 矩阵 SHA-256：`8318066478e5d767452db920fca4b5eb9d35c37787c610e0f327660445466edc`。该 JSON 在原始 CI 运行态快照后附加了 run `38040115559` 的最终 SUCCESS 和 7 组跨支文件重叠，不覆盖原快照时间点。

### G0 判定：未通过，不能进入“已集成”状态

尚无从当前 `main` SHA 构建、同时包含 PR #20/#21/#22 与 PR #25 最新 head、并通过同一套组合回归的集成候选。PR #23 的旧红色 head 仍开放；其修复可由 #24/#25 后代覆盖，但不能把旧 #23 检查改写为绿色。现有远端 CI 也没有覆盖 Gate 4h 分页、TestNet 原生 reduce-only、账户隔离、Gemini 响应身份和校准模式在同一 SHA 上的组合行为。

### G0 修复次序与回滚

1. 在隔离的本地候选分支从 `origin/main@8642639a78ca9aa56fbad1e88aea4b82169ea433` 起步；先纳入 PR #20，再纳入其子分支 #21、#22，最后合入 PR #25 head（由其 ancestry 带入 #23/#24 的修复）。禁止直接合并远端 PR、推送到 `main` 或改写源分支。
2. 对重叠文件逐段核对原始 diff；尤其检查 Gate E2E、`tests/test_gate_testnet_ai_chain.py`、宏日历时钟与 PR #25 的 Gemini 变更。PR #23 失败的三个测试只接受 #24 的明确修复和组合测试结果。
3. 候选 SHA 上运行 workflow 的快速门禁、完整 Python 套件和新增/现有组合回归；在相同 Python/OS/依赖条件下与固定 main 基线比较，要求 0 个新失败节点。还需记录候选 SHA、所有并入 head、测试命令、失败节点和回滚点。
4. 任一冲突或新失败无法解释时，停止候选；保留源分支与 PR，不删测试、不放宽风控。回滚方式为放弃隔离候选或对候选中的单个 merge commit 做 revert，不 force-push、不改远端源分支。

## G1：模型响应可靠性

### 只读重建结果

原始 ignored JSONL 账本 `reports/v38+/runs/v38-gemini-call-intent-ledger-v1.jsonl`：144 行，SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`。核查前后哈希相同；现有 `_read_ledger` 的序列、哈希链、事件配对及 `review_gemini_ledger` 校验通过。分类报告只保存聚合结果，不含回包原文，见 [`V38-invalid-json-classification-20261010.json`](V38-invalid-json-classification-20261010.json)。

分类 JSON SHA-256：`4ba18a5bbb41ad8525e334cf44bcad98ab6d5d98ac76823b49f0f6c871785db7`。

- 固定分母：72 dispatch intents；71 terminal results；另有 1 个无结果 intent，仍为 `AMBIGUOUS_NO_RESULT_NEVER_RETRY`。
- 71 个结果：67 `INVALID_JSON`、4 `CALL_ERROR_NO_RETRY`；历史有效分析仍为 **0**。
- 67 个 `INVALID_JSON` 全部是完整、单层 `json` Markdown fence 内的 JSON object。新版本 `RAW_OR_SINGLE_JSON_FENCE_V1` 在只读兼容诊断中解析 67/67；原 V38 analysis Schema 与各自 intent 的 evidence_refs 校验 67/67 通过。67 个 raw content hash 全部匹配 ledger 记录，长度为 1,645–2,763 字符。
- 67 条均记录 HTTP 200、`finish_reason=stop` 和响应字段 `gemini-3.8-flash-high`。这说明没有 ledger 证据支持“这 67 条因 length 截断而失败”；它们不是合格旧 V38 分析，也没有被回填或改标签。
- 其余 4 个调用错误中，2 个有 HTTP 400，2 个没有 HTTP 状态/响应文本；1 个 intent 仍 ambiguous。失败历史不可重发。
- 67 个 usage receipt 的 prompt/completion/total 字段全部不相等：报告合计分别为 720,668、56,359、901,797，前两项之和 777,027，与 total 相差 124,770。67 条的 `provider_cost_usdt` 都为空；费用与周配额消耗均未知。
- 本地 ledger 保存了 `message.content`、内容哈希及传输状态/字节数，但没有完整原始 HTTP 响应 body。因而无法从现有证据分类代理外层包装或证明服务端是否真正执行了 `response_format={"type":"json_object"}`。代码传入该参数不等同于提供方能力已验证。

### 已证实、未证实与现有修复覆盖

**已证实**：历史代码将 `message.content` 直接交给 `json.loads`；67 个完整对象外包一层 Markdown fence，因此失败的是旧响应 envelope 与严格 parser 的不兼容。严格 JSON/schema 内容本身可在新的独立兼容诊断里解析并通过引用校验。不能将该诊断回写成旧结果成功。

**未证实**：为什么中转链路在请求 `json_object` 后仍返回 fence；Antigravity 是否支持或透传 native JSON mode/schema；响应模型字段是否代表真实底层模型/权重；provider 是否重复处理/计费；历史使用量与费用。现有 transport trace 不含响应 body，也无签名权重证明。

**已有新路径**：PR #25 增加独立 `RAW_OR_SINGLE_JSON_FENCE_V1` 解析和一次性格式试验。当前 exact-head 解析器/格式试验专项测试复跑为 **24 passed**；GitHub current head 全量 CI 通过。先前单次格式试验使用已跑过的 optimization context，模型字段匹配且 JSON/schema/evidence_refs 有效，但其 `quality_sample_eligible=false`，授权已消耗且 token 用量不一致。它只证明该新 envelope 在该单次回包上可解析，不证明稳定 provider capability 或市场判断质量。

**尚存离线测试缺口**：V2 专项测试覆盖 raw object、单层 fence、额外文本/错误 fence、重复键、非 JSON 常量、错误根类型、版本拒绝、dry-run 零调用、重复 intent 不重发及模型错配；当前 V2 专项测试没有 `finish_reason=length`、空内容/部分内容、missing finish、invalid evidence_refs 和 provider `response_format` capability negotiation 的完整负例矩阵。历史 67 条都 `stop` 不替代这些故障注入测试。

### G1 修复规划与判定

1. 保留原 72-intent ledger、V1 prompt/schema 和既有报告逐字节不变。兼容解析仅作为新的版本化路径；不追溯修改任何状态、评分分母或旧响应。
2. 冻结新的 Prompt/Schema V2：最小 parseability 层仅包括 schema version、`OBSERVE|WAIT`、市场环境/方向和 evidence_refs；稳定后再分层扩展 location、signal、candidate_setup、counter-evidence 与 rationale。明确 raw/fence envelope 政策和每层 parser/schema 版本。
3. 加入机器可读 provider capability 状态：`native_schema_verified`、`json_object_only`、`prompt_only_unverified`。当前路由默认为 `prompt_only_unverified`，除非有可信的能力回执；单次 fence/有效 JSON 不能提升能力级别。分别记录请求参数哈希、响应身份字段来源、原文哈希、结束原因、transport trace 和 UNKNOWN 成本，不伪造权重 digest。
4. 全部使用固定 fake provider 做离线正负测试：raw object/单 fence、前后说明、多重 fence、空/部分/截断、length/error finish、包装缺字段、重复键、NaN/Infinity、wrong root、未知版本/枚举/字段、missing/wrong/disagreeing model ID、非法/未来 refs、超限、transport ambiguity、重复 intent 不重试及 429 stop latch。失败时保留原文并 fail closed。
5. G1 的**离线历史分类已完成**，parser 专项测试和当前分支 full CI 通过；但完整 G1 capability/negative-matrix 验收仍未通过。只有该负例矩阵和新的版本化契约在合并候选上通过，才可讨论 G1 解锁；任何 G2 付费调用仍须新的明确授权、独立样本、有限请求数及费用/停止规则。当前不进入 G2。
6. 回滚：V1 固定入口和历史状态不变；新契约若失败，只在新版本分支禁用该 parser/capability 声明并 revert 新变更，保留原始 evidence 与本次诊断报告。

## Gate 状态汇总

| Gate | 本轮状态 | 未满足的验收 |
|---|---|---|
| G0 集成与基线 | **NOT_ACCEPTED** | 尚无包含 #20/#21/#22 与 #25 线的单一候选 SHA、全 diff 审查及组合 CI |
| G1 模型响应可靠性 | **OFFLINE_ROOT_CAUSE_CONFIRMED; GATE_OPEN** | capability negotiation 与所列完整离线负例矩阵未完成；V2 尚未冻结 |
| G2 受控解析性实验 | **NOT_AUTHORIZED** | 需要新的明确授权；旧 72-intent 与单次格式试验额度均已消耗 |

本报告和矩阵为 PR #25 的新增审计文件；不修改生产代码、风控、原始研究数据、V25/V37 历史报告或 TestNet/Live 权限。
