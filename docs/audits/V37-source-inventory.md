# V37 历史证据来源清点

审计日期：2026-10-09
基线：`codex/v36.1-research-integrity` / `23ca359082193033d9c40ee282c41a074eee53a2`
审计范围：V25/V31/V32/V33 本地研究资产、脱敏 V25 样本、应用本地 SQLite、Binance 公共归档及 V36.1 离线研究代码。

## 方法与保护边界

- SQLite 使用 `mode=ro` 并设置 `PRAGMA query_only=ON`。私有账户、订单、成交和响应正文没有输出到报告。
- 对研究数据库只读取表结构、回放表的行数/状态/时间范围，以及身份字段名是否出现；字段值不输出。
- 不运行 Gemini 年度研究脚本和响应导出脚本；它们可能调用模型或导出原始响应。只检查源码、路径和摘要元数据。
- 每个本地数据文件的完整 SHA256、字节数、归档成员清单和 SQLite 元数据记录在被 Git 忽略的 `reports/v37/source-inventory.json`。本地 SQLite 文件不提交公共仓库。

## 实际发现

| 来源 | 实际覆盖与完整性 | 敏感性 | 可支持的实验 |
|---|---|---|---|
| `docs/research/v25-price-action-sample-20261008.json` | 文件 SHA256 `4193956555d7576a3a754fbeb80198ad5b4828079b574dedf71eb347a13353cb`；100 条扫描摘要、10 条已平仓摘要。扫描行有时间、动作、标的和提案字段；没有 `decision_id`、原始 Prompt、完整模型输入、账户状态快照或每次调用的 Schema 版本。 | 已跟踪的脱敏摘要；仍含历史交易摘要，不复制到新数据文件。 | 只支持既有汇总计数与缺失证据审计，不支持原始 A0 重放。 |
| `reports/gemini-year-research-v25-recovery-2000-20261007/pilot-{1,2,3}/results.sqlite3` | 每库 192 条 `COMPLETED` 回放行，分别覆盖 2025-10-15 至 16、2025-12-14 至 15、2026-02-12 至 13。回放 JSON 中可见 `actual_model_id`、`model_id`、`input_hash` 和 `prompt_version` 键；没有与 V36.1 A0 身份一致的完整身份对象。 | 私有本地 SQLite；含账户/订单相关表。完整文件哈希仅在本地清单。 | 只能作为各自 pilot 的独立运行资产；没有逐行链接证据，不拼接到 V25 的 100 条扫描。 |
| V31/V32/V33 本地 pilot SQLite | V31 为 100 条、2025-10-15 至 2025-10-16；V32 为 50 条、2025-10-15；V33 为 50 条，其中 47 `COMPLETED`、3 `ERROR`。时间重合不能证明实验输入、状态、Prompt、Schema 或响应身份相同。 | 私有本地 SQLite；完整哈希仅本地保存。 | 只支持原 pilot 的运行完整性核对，不能重标为 V25。 |
| `data/market_analyst.sqlite3` | 212,893,696 字节。Gate-like 15m 数据最早 2026-09-05；Gate native WS 的 5m/15m/1h 数据只覆盖 2026-09-10 附近。质量状态同时包含 `LEGACY_UNVERIFIED`、`RECONSTRUCTED_LATE` 与少量 `VALID`。没有 V25 的 2025-10、2025-12、2026-02 决策时段行情。 | 私有本地 SQLite；存在账户快照、交易、成交和 TestNet 相关表。本次只查表名、行情元数据和分组区间，未读取这些敏感表的行值。 | 可证明本机 Gate 行情不覆盖 V25；不可重建 V25 原始市场输入。 |
| `reports/btc-eth-year-proxy-20261004/` | Binance UM 官方月度公共 1m K 线与 funding 归档。历史覆盖 2025-10-01 至 2026-10-01，另有 2025-09-21 起的 warm-up。BTCUSDT、ETHUSDT 各 540,000 根 1m bar；两个标的均为 0 缺口、0 重复。50 个本地 zip 均与 manifest 的 SHA256/字节数相符；独立代理审计复核 50 个归档，`PASS`、0 failures。 | 公共市场代理研究数据；较大归档与数据库只保留本地。 | 可重建 Binance 价格历史并构建独立代理 benchmark；不是历史 Gate 行情，也不能证明过去何时收到数据。 |
| `evidence/gemini-price-action-v33-first50-20261008/` | 已跟踪的 V33 脱敏归档；manifest SHA256 `6cbfd21cb49f187f636eeb4e18bd7c6bb6c84498fc6ef42f521f921991e1cbd3`，列出 51 个归档文件。 | 独立于原始账户数据库的 V33 脱敏资产；本次未读取模型响应正文。 | 仅能支持 V33 的响应/研究审计，不能作为 V25 A0 响应缓存。 |

脚本盘点确认以下文件在 V37 工作树中存在：`core/replay/gemini_research.py`、`core/replay/ai_template_runner.py`、`core/replay/ai_history.py`、年度研究脚本、响应导出脚本，以及 V36.1 的运行与 review CLI。它们的路径、SHA256 和“未执行”标记在本地清单中。

## 证据分级

- **EXACT_OBSERVED**：脱敏 V25 扫描/平仓摘要中的已保存字段，以及本地数据库中的可核验元数据；不扩展为原始 Prompt 或 Gate 成交证明。
- **VERIFIED_ARCHIVE_RECONSTRUCTION**：从 manifest 验证过的 Binance 官方月度归档重建的 OHLCV 价格。它重建的是 Binance 价格历史，不是 Gemini 当时看到的 Gate 行情。
- **ASSUMED_PROXY**：本次因果重放把 `available_at` 设为 `bar_end + 60s`。本地归档没有历史到达时间，因此该时间只是显式保守假设。
- **UNAVAILABLE**：V25 原始模型输入、点时账户/保证金、V25 时点 Gate 合约规则及 V25 决策时的真实行情可用时间。

## 结论

本地存在足够完整的 Binance 公共历史归档，可为 V36 的四周期上下文生成因果、可重复的重建行情 benchmark。没有足够证据恢复 V25 的 100 个原始 A0 身份，也没有覆盖这些决策日期的本地 Gate 行情。V33 响应归档、V25 recovery pilot 缓存和 V31/V32 同时段样本均保持各自来源身份，不用于近似拼接。
