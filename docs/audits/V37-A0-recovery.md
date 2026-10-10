# V37 V25 原始 A0 恢复审计

## 严格身份条件

V36.1 的 A0 匹配从保留的原始 `prompt`、`model_input`、`state_snapshot`、`model_id`、`prompt_version`、`analysis_schema_version` 重新计算身份。精确缓存键包括 `experiment_id`、`model_id`、`prompt_sha256`、`data_sha256`、`decision_time`、`state_sha256`、`prompt_version` 和 `analysis_schema_version`。仅时间、标的、动作方向或相邻扫描相同不构成匹配。

V37 将 V35 提交 `2a5d7b9992a7909a6a597917e409ecb0737f9c89` 的最终 `AI_ACTION_SCHEMA` 固定到 `configs/research/schemas/a0-v35-action-schema.json`。规范化 SHA256 为 `d2e264052568f7e6bf661c1cc40beef2dd573178f93ca0a5103d7b4a23d29763`，版本为 `v35-ai-action-schema/sha256:d2e264052568f7e6bf661c1cc40beef2dd573178f93ca0a5103d7b4a23d29763`。V36 A0 版本读取该文件并验证来源与哈希，不再从当前生产 `AI_ACTION_SCHEMA` 复制内容。

此冻结只证明 V35 基础 Schema 内容。历史调用的 `frozen_decision_schema(inputs, gate_wait=...)` 会根据当次模型可见标的、证据引用和 WAIT 条件继续收紧 Schema；V25 记录没有保存每次调用的有效 Schema，不能由基础 Schema 倒推。

## V25 证据结果

- 已跟踪 V25 脱敏样本包含 100 条扫描摘要和 10 条已平仓交易摘要；扫描行没有原始 Prompt、完整 model input、点时 state snapshot、模型身份、Prompt 哈希、state 哈希或 A0 Schema 版本。
- 本机 V25 recovery pilot 的三个 SQLite 各有 192 条 `COMPLETED` 行。它们可以证明各自试验存在 `input_hash`、模型身份/请求字段及 `prompt_version` 等部分字段；数据库行结构不是 V36.1 A0 精确 cache export。没有已证明的逐行映射到原始 100 条 V25 扫描。
- V31 的 100 行、V32/V33 同时段记录及 192 行 pilot 缓存未导入 V36 A0 matcher。没有按时间、标的或方向做近似 join。
- 原始 V25 sample SHA256 为 `4193956555d7576a3a754fbeb80198ad5b4828079b574dedf71eb347a13353cb`；检查前后哈希一致。

| 状态 | 数量 | 含义 |
|---|---:|---|
| `EXACT_A0_RECOVERED` | 0 | 没有任何记录具备可重新计算且完整匹配的 V36.1 A0 身份。 |
| `PARTIAL_EVIDENCE_ONLY` | 100 | 仅能从脱敏摘要读取历史扫描字段；每条均缺 Prompt、model input、状态快照、模型身份/Prompt 版本和有效 Schema 证据。 |
| `CACHE_IDENTITY_MISMATCH` | 0 | 不声称 100 条候选逐条身份不匹配；没有完整身份就无法执行该判断。 |
| `SCHEMA_UNVERIFIED` | 100 | V25 每次调用的有效 Schema 版本没有保存。 |
| `ORIGINAL_PROMPT_UNAVAILABLE` | 100 | 原始 Prompt 不在脱敏样本中。 |
| `ORIGINAL_STATE_UNAVAILABLE` | 100 | 原始账户/持仓/挂单状态快照不在脱敏样本中。 |

以上 blocker 计数互相重叠。可复现的完整 blocker 和字段计数在本地 `reports/v37/a0-recovery-coverage.json`。结论为 **`UNRECOVERABLE_FOR_EXACT_A0`**：V25 事实摘要仍保留，但不能声称完成 V25 原始决策质量重放。没有生成替代 Gemini 响应。
