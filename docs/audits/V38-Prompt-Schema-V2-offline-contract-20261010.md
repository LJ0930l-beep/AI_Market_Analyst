# V38 Prompt/Schema V2 离线契约审计 — 2026-10-10

## 1. Gate、基线与决定

- 阶段：G1 模型响应可靠性，离线契约与测试修复。
- G0 基线：集成候选 PR #26，base main@8642639a78ca9aa56fbad1e88aea4b82169ea433，候选 head ffc265be9bdcb1f97aef5c80d572636ca9cef035。
- PR #26 的同 head hosted CI 已通过：quick gate 222 passed；全量 2,518 passed、0 failed。候选仍为 Draft、未合并，人工审查仍待完成。
- 本次 G1 分支从该候选 head 创建；G1 代码没有写入或改写 G0 提交。
- 决定：本地 V2 响应契约已可复现；真实 Antigravity response_format 能力仍为 unknown_unverified。因此 G1 离线部分可接受，G1 的远端能力结论保持 MODEL_OUTPUT_CONTRACT_BLOCKED；不得进入 G2 或发起新请求。

## 2. 已冻结的历史证据

旧活动账本保持原 SHA-256 820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7。冻结 V38 活动仍是 72 个 dispatch intents、71 个终态结果，其中 67 个 INVALID_JSON、4 个 CALL_ERROR_NO_RETRY、1 个 AMBIGUOUS_NO_RESULT_NEVER_RETRY，有效市场分析为 0。

只读分类器此前确认 67/67 个原始 message.content 是单层完整 JSON fence；脱离旧活动的新兼容解析诊断为 67/67 解析成功并通过 V38 Schema 与 evidence_refs 校验。该兼容诊断不改写旧状态、不进入旧活动分母，也不构成新模型调用质量结果。独立 one-intent pilot 复用了旧 optimization context，仍不具备质量样本资格，其授权已耗尽。

原始回包中结构化输出能力无法从语法结果推定；usage 67/67 的算术不一致，提供方费用仍未知。完整 HTTP envelope 不在旧账本内。旧证据不可修复、重标、重试或覆盖。

## 3. V2 离线实现

新增 V2 有两个明确阶梯：

1. MINIMAL：版本、OBSERVE/WAIT、market regime、bias 和因果 evidence_refs。
2. FULL：在同一意见链路中扩展 location、signal、candidate_setup、target_structure、counter_evidence、decision_rationale 和 wait_reason。

新增 parser policy RAW_OR_SINGLE_JSON_FENCE_V2。它只接受原始 JSON 对象或恰好一层完整 json fence；拒绝多重/混合 fence、前后文本、重复键、非 JSON 常量和非对象根节点。原始内容、原文哈希与规范化分析分开保存，不会清洗后覆盖原文，也不会写入 V1 ledger。

契约检查模型身份字段、请求 ID 与 transport receipt、finish_reason、字符上限、Schema、枚举和 evidence_refs。未知/未来 evidence ref 被拒绝。Usage 仅检查可报告的算术一致性；不一致时标记未验证，费用始终是 UNKNOWN_NO_VERIFIED_BILLING_RECEIPT。

两个新 Prompt 配置的远程调用授权均为 false，最大调用数为 0。响应格式偏好为 JSON Schema，但真实代理能力固定输出 unknown_unverified；纯 JSON 或 fenced JSON fixture 不能把能力升级成已验证。此代码路径没有模型或交易所客户端，也没有订单接口。

## 4. 修改文件与不变量

新增：

- core/replay/pa_decision_quality_v38/response_contract_v2.py
- configs/research/prompts/v38-market-only-minimal-v2.json
- configs/research/prompts/v38-market-only-full-v2.json
- tests/v38/test_response_contract_v2.py
- 本审计文件。

修改：

- core/replay/pa_decision_quality_v38/json_response.py：仅注册 V2 fence parser 名称；V1 解析规则保持原样。
- docs/blockers/B-20261010-002-model-response-capability.md 与 docs/blockers/index.yaml：将未验证的代理能力和离线修复状态准确登记。

初版文件哈希（第7节复审补丁前；最终复审代码哈希见第7节）：

| 文件 | SHA-256 |
|---|---|
| core/replay/pa_decision_quality_v38/response_contract_v2.py | 50F425ED6752A2EF90595B70EDC4B401672A697698ECEF666438F82B9A6910B2 |
| core/replay/pa_decision_quality_v38/json_response.py | 2D9308E401B2570C22B9253A9798610A1091CD2FA386CBF600F400169A13CED7 |
| tests/v38/test_response_contract_v2.py | C5C98ABDF2D4EE156761BD35AE039357E225BECF7BFFE0182E897BFBEC2CA7CC |
| configs/research/prompts/v38-market-only-minimal-v2.json | 04886B05617BD2B8882312819FAB4DB315FF5093FC7345E9C068237C382CB520 |
| configs/research/prompts/v38-market-only-full-v2.json | 5D897FF0539CDEADD7B88E845C4C916D5FEF145E059AA1820D928837FB0D7A2C |

不变：

- V1 runner、V1 prompts、已消耗授权、历史账本、67 个失败状态、one-intent pilot、V35 风控与生产交易路径。
- 没有新增模型请求、Gate/TestNet/Live API 访问、订单或额外费用；没有自动合并 PR。
- 验证与 untouched_test 样本没有被用于模型请求、调参或修复。

## 5. 测试

本次针对性测试：

- python -m pytest tests/v38/test_response_contract_v2.py tests/v38/test_json_response.py tests/v38/test_format_pilot.py tests/v38/test_gemini_runner.py -q — 70 passed。
- python -m pytest tests/v38 -q — 170 passed。
- python -m pytest -q --junitxml=reports/v38+/verification/v2-g1-offline-final-20261010.xml — 2,542 passed, 1 skipped, 1 Starlette/httpx deprecation warning in 359.31s。
- 本地 JUnit SHA-256：C9359E2863F499657C922810B586CD8F5EC085C1593B2AC44C48B4953226A261；该文件位于 Git 忽略的 verification 目录，不上传。
- ruff check core/replay/pa_decision_quality_v38/response_contract_v2.py core/replay/pa_decision_quality_v38/json_response.py tests/v38/test_response_contract_v2.py — passed。

覆盖合法最小/完整版、原文与规范化分离、fence parser、空/部分/截断/缺 finish_reason、响应模型缺失和错配、请求/transport 错配、超限文本、未知枚举、未来/未知 evidence_refs、执行字段、usage 算术不一致、费用未知、忽略 JSON Schema 请求的 fake fixture，以及 Prompt 禁止远程调用的冻结检查。

这些测试只证明本地 V2 合同；不证明真实代理执行了 JSON Schema。

## 6. 失败处理、回滚与剩余风险

若解析、身份、transport 或 Schema 验证不通过，响应不得成为有效本地分析；原文哈希保留在结果对象，错误分类 fail closed。若响应超过字符上限，只保留哈希和长度，不保留超限正文。

若 V2 回归，回滚仅撤销新增 V2 文件和 parser V2 注册；V1 runner/prompt、G0 候选、原始账本和历史报告均不变。PR #26 仍是独立 G0 候选。

剩余风险：Antigravity 对 response_format=json_schema、json_object 的实际接受/拒绝、能力回执、计费和账单行为均没有本阶段新证据。真实能力保持 unknown_unverified，route 未解锁。任何后续受控模型试验属于 G2，需单独新的明确授权、全新冻结范围、有限请求数和费用停止规则。旧 72 intents 与已消耗 pilot authorization 不可复用。

## 7. 2026-10-10 离线复审补充

代码复审补上两处证据边界：

- 不再把 payload 内的 `model` 字段挪填到适配器 `response_model_id`。两种来源单独保留；缺一项、来源字段冲突或请求模型不匹配都拒绝本地分析。即使字段相等，`model_identity_evidence_level` 也明确写为 `REPORTED_FIELDS_MATCH_ONLY_NO_WEIGHT_ATTESTATION`。
- `request_bytes_written` 仅接受正整数或有限正浮点值；拒绝布尔值、0、负数、NaN 和正负 Infinity，避免非有限传输元数据通过比较判断。
- fake provider 负例现在明确提交 `response_format.type=json_schema` 请求，再让本地 fake 返回 fenced JSON；结果仍固定为 capability `unknown_unverified`。这只演示本地 evaluator 不提升能力，不声称真实代理支持或忽略该参数。
- FULL tier 新增 candidate_setup 嵌套未来 evidence ref 拒绝测试。

追加验证：

- `python -m pytest -q tests/v38/test_response_contract_v2.py`：**34 passed**。
- 仓库 quick gate：**256 passed**（32.07s）。
- 仓库全量离线套件：**2,551 passed, 1 skipped, 0 failed**；1 条既有 Starlette/httpx 弃用警告；359.38s。
- JUnit：`reports/v38+/verification/v2-g1-refined-20261010.xml`，SHA-256 `1312059A617766EC26B146D51239C6A1E556CC7220EBB3F40140EE50054C267A`，本地 Git-ignored。
- Ruff check、Ruff format check、`git diff --check`：通过。

复审后的代码 SHA-256：`core/replay/pa_decision_quality_v38/response_contract_v2.py`=`31A0DAFCCA9103ABC6B50356168F3F18BF75EC3A9AAA6938613EBF6594E3E8BC`；`tests/v38/test_response_contract_v2.py`=`E3D8E62A3A1F4BB693C14B953C25415A6EAA42AA6DA62A50F4A41E6D942B3EFD`。这次补丁不接入校准器或 production runner；远端 capability 仍为 unknown，G2 未启动。该补充所在更新后的 PR #27 还需同一新 head 的 GitHub CI，不能引用旧 head CI 代替。

## 2026-10-10 correction — V2 transport byte-count type

A follow-up offline review found that the earlier Section 7 description of positive finite floats as accepted was too permissive. `request_bytes_written` is sourced from `len(data)` on bytes-like requests; it must be a positive exact integer. The V2 evaluator now rejects both `1.0` and `1.5` as `INVALID_TRANSPORT` and emits no normalized analysis. This is a correction to the new V2 offline contract only; it does not rewrite historical response outcomes or change the V1 runner/shared transport producer. At the time of this correction, the related V38 runner fix was local and awaited exact-head PR #27 hosted CI; the later verification is recorded below and in the corresponding additive subfinding in B-20261010-002.

### Exact-head follow-up — 2026-10-10

The V38 runner's corresponding integer-byte repair was pushed in PR #27 head `f374589d9de79b29f9347cde630135263934c6e5`. Hosted run [38062787101, attempt 2](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38062787101) passed: quick gate 267 passed; full suite 2,563 passed, 0 failed, one existing Starlette deprecation warning, and 25 subtests passed. Attempt 1 on the same SHA had one Windows `WinError 32` while cleaning a temporary SQLite database after 2,562 tests passed; the targeted test passed locally and attempt 2 passed. The intermittent cleanup risk remains documented in [`B-20261010-002`](../blockers/B-20261010-002-model-response-capability.md). This verifies offline evaluator/runner behavior only; it does not verify external provider capability, response identity, or billing.
