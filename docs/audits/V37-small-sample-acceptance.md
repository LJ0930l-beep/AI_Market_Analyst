# V37 小样本离线验收

## 数据集分层

本地 `reports/v37/decision-points.json` 只包含 Binance 重建 benchmark，不含 V25 原始 A0/cache 行：

| 分区 | 样本数 | 使用范围 |
|---|---:|---|
| research | 1 | 数据与运行器工程检查 |
| validation | 1 | 输入哈希、因果边界和 review 一致性检查 |
| untouched_test | 1 | 独立重复运行与哈希检查 |

每个点具备 5m、15m、1h、4h 的完整因果窗口、逐 bar 来源哈希、可用时间假设、输入哈希和 V36 Context 哈希。三点均使用 Binance 官方历史归档重建价格，`available_at` 使用明确的 60 秒延迟假设，因此市场价格等级为 `VERIFIED_ARCHIVE_RECONSTRUCTION`，到达时间等级为 `ASSUMED_PROXY`。

## 离线运行结果

现有 V36 CLI 和 review CLI 均从冻结的本地 JSON 输入运行。生成器还以同一输入直接重复运行 V36 库两次，并与 CLI 输出逐字节比较；review CLI 指标与独立 `summarize()` 结果完全一致。

| 实验组 | 状态 | 样本数 | 说明 |
|---|---|---:|---|
| A0 | `NOT_RUN_NO_EXACT_CACHE` | 3 | 没有导入或重标任何 V25 缓存。 |
| A1 | `NOT_RUN_STATE_SNAPSHOT_MISSING` | 3 | benchmark 不含真实账户/持仓状态，未生成模型调用。 |
| A2 | `NOT_RUN_STATE_SNAPSHOT_MISSING` | 3 | 同上；不得把未运行写成成功。 |
| A3 | `RESEARCH_CANDIDATE` | 3 | 仅运行确定性候选规则；每条风险预检因历史经济输入缺失而 `BLOCKED`。 |

回放 manifest 为 `OFFLINE_DEFAULT`，`model_calls_used=0`。所有候选的网关接受、成交和平仓状态均为 `NOT_OBSERVED`；没有创建订单。A3 候选不等于实际提案、委托、成交或获利交易。

## 验收边界

- V36.1 CLI 输出 SHA256：`d0eee6baa9244a51eedae1f37a6d14a0fc8cf77c6c397455175a1b7f1347398f`；重复库运行输出哈希一致。
- Review CLI 输出 SHA256：`85af68b0f2ee3ba649984eac9bb06039f68d885868bdc4a2c455fa1993d13a1c`；指标与独立总结一致。
- V25 原始样本和 212,893,696 字节本地 SQLite 的 SHA256 在生成前后保持一致。
- 本小样本只验证来源、时间因果、哈希、离线 CLI/review 和样本分区，不评估胜率、收益、盈利能力或 Gate 可成交性。
- V25 原始 A0 对照仍不可运行；V38 不能把这些 benchmark 当作 V25 Gemini 实际看到的 Gate 行情。
