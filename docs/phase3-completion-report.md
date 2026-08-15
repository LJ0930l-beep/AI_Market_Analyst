# AI Market Analyst Phase 3 完成报告

## 1. 最终判定

**PHASE 3 PASS（功能与数据完整性通过，真实 Qwen 输出保留已记录错误）**

Phase 3 的回放、绩效、校准、API、CLI、断点续跑和数据完整性要求均已实现并验收。正式真实模型回放以 `COMPLETED_WITH_ERRORS` 完成：300 个计划样本中有 45 个样本最终无法通过严格 JSON/Schema 修复，因此本报告不宣称“零模型错误”。这 45 个样本被保留、计入错误统计并排除出有效绩效样本。

本阶段只实现 Phase 3，不包含 Phase 4。

## 2. 正式回放凭证

| 项目 | 值 |
| --- | --- |
| 验收日期 | 2026-08-15 |
| Replay run | `replay-20260815042457-71ec330d` |
| Model | `qwen3.5:4b` |
| Prompt | `phase2-json-v8` |
| Source type | `replay` |
| Symbols | `AAPL, NVDA, TSLA, AMD, BTCUSDT, ETHUSDT` |
| Timeframes | `1h, 4h` |
| Samples | `300` |
| Seed | `42` |
| Manifest hash | `517ae96c02f19e3e58720d793b927a06cf771154ef23a956806d0962759a5` |
| Context | `technical_only` |
| Historical news | unavailable；没有伪造 neutral news |

回放严格按 `as_of` 构造输入，未来 K 线只用于之后的 Outcome 结算；每个样本具有稳定且唯一的 replay Prediction ID。

## 3. 正式结果

### 3.1 数据完整性

| 指标 | 结果 |
| --- | ---: |
| 计划样本 | 300 |
| Replay sample rows | 300 |
| Predictions | 300 |
| 有效模型结果（COMPLETED + WAIT） | 255 |
| WAIT | 64 |
| 最终错误样本 | 45 |
| 可行动预测 | 191 |
| 已结算可行动预测 | 191 |
| Outcomes | 191 |
| 重复 sample Prediction ID | 0 |

### 3.2 绩效指标

以下为校准脚本完成后生成的当前汇总，绩效状态按规格保持为 `PRELIMINARY`：

| 指标 | 值 |
| --- | ---: |
| Win Rate | 36.65%（70 胜 / 121 负） |
| Avg R / Expectancy | 0.3716 R |
| Profit Factor | 1.5957 |
| Max Drawdown | -19.4996 R |
| MFE 平均 / 中位数 / P90 | 1.7662 / 1.2807 / 3.6200 R |
| MAE 平均 / 中位数 / P90 | -1.3332 / -1.1779 / 0 R |
| Timeout | 6（3.14%） |
| Coverage / Action Rate | 74.90% |
| WAIT Rate | 25.10% |

Coverage 的分母是 255 条有效模型结果；45 条最终错误记录单独计入 `invalid_count`，不进入有效绩效分母。

### 3.3 置信度校准

当前校准结果为全局经验 Beta 收缩：

- Calibration ID：`739a53eb90155f876e512900`
- 方法：`empirical_beta_shrinkage`
- 先验：Beta(5,5)
- 最小样本阈值：100
- 校准样本：191 条已结算可行动预测
- 状态：`ACTIVE`
- Brier raw / calibrated：`0.297835 / 0.247882`
- ECE raw / calibrated：`0.242376 / 0.033322`

主要 bucket：

| Raw confidence bucket | n | Empirical win rate | Shrunk rate |
| --- | ---: | ---: | ---: |
| 0.50–0.60 | 61 | 44.26% | 45.07% |
| 0.60–0.70 | 104 | 30.77% | 32.46% |
| 0.70–1.00 | 0 | — | 50.00% prior |

Raw confidence 没有被覆盖；校准值、版本、scope、样本数和 fallback 均单独持久化。当前样本不足以对高置信度 bucket 或 symbol/timeframe 做稳定的独立校准，因此继续使用全局校准。

### 3.4 模型延迟与错误

255 条成功完成模型调用的延迟统计：

- Mean：`4269.33 ms`
- Median：`4343.49 ms`
- P95：`6404.91 ms`

最终样本错误为 `45 × repair_failed`。由于重试会产生多条 `model_runs`，底层尝试记录中的 `repair_failed` 为 67 次；其中一部分随后成功，不应与最终样本错误数混淆。正式回放未观察到 `MODEL_UNAVAILABLE`、超时或 GPU OOM。

## 4. 验收矩阵

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| Phase 1/2 回归测试 | PASS | `41 tests, OK` |
| Python 编译检查 | PASS | `python -m compileall -q core apps scripts tests` |
| 60 条真实 Qwen smoke | PASS with recorded errors | `data/phase3-real-qwen-smoke-v8.json` |
| 300 条正式真实 Qwen 回放 | PASS target / completed with errors | `data/phase3-real-qwen-formal-v2.json` |
| Resume / checkpoint | PASS | v2 从中断 checkpoint 继续完成 |
| as-of 无未来数据泄漏 | PASS | replay provider、outcome 边界和单元测试 |
| Prediction ID 唯一性 | PASS | 300 predictions，重复数 0 |
| Performance snapshot | PASS | global / symbol / timeframe / action / regime 等 scope |
| Calibration | PASS | ACTIVE，Beta(5,5)，raw/calibrated 分开 |
| Phase 3 API / CLI | PASS（编译与接口检查） | `apps/api/main.py` 与四个 Phase 3 脚本 |
| Phase 4 | PASS | 本阶段未实现 Phase 4 |

当前 Python 环境没有安装 FastAPI/Uvicorn，因此 API 进程级启动未在本环境执行；模块已通过编译检查。安装 API 依赖后可按 README 启动服务。

## 5. 已交付内容

- `core/performance/`：绩效指标、回撤、MFE/MAE、coverage、Brier/ECE、confidence buckets。
- `core/replay/`：确定性采样、manifest、as-of 数据边界、真实 Qwen 顺序回放、resume、错误记录和 latency。
- `core/storage/sqlite.py`：schema v4 增量迁移及 replay、snapshot、calibration 表；保留旧表和 Phase 1/2 数据。
- `apps/api/main.py`：绩效、校准、回放和 Prediction calibration API。
- `scripts/run_phase3_replay.py`：回放 CLI。
- `scripts/build_performance_snapshot.py`：绩效快照 CLI。
- `scripts/fit_confidence_calibration.py`：置信度校准 CLI。
- `scripts/report_phase3_metrics.py`：JSON/Markdown 报告 CLI。
- `scripts/refresh_phase3_output.py`：从 SQLite 刷新可复核的回放摘要。
- `data/phase3-real-qwen-formal-v2.manifest.json`：不可变回放清单。
- `data/phase3-formal-v2-report.json`：完整指标报告。
- `data/phase3-formal-v2-calibration.json`：当前校准结果。
- `data/phase3-formal-v2-snapshots.json`：绩效快照结果。
- `docs/phase3-formal-v2-metrics.md`：简版指标报告。

## 6. 已知限制与下一步

1. 历史新闻数据源尚未提供可按历史时间点查询的能力，本次回放明确标记为 `technical_only`，没有把缺失新闻伪装成中性新闻。
2. Qwen 严格输出仍有 45/300 个最终 `repair_failed`。如需零模型错误的硬门槛，需要继续优化 prompt/repair 或增加受控重试，并重新生成一组新的正式 run。
3. 当前全局性能报告仍是初步结果；校准已达到 191 个已结算可行动样本的 ACTIVE 阈值，但不应据此宣称跨 symbol/timeframe 的稳定泛化。
4. Phase 3 源规格书的 DOCX 已完成结构化读取；由于当前环境没有 LibreOffice/soffice，未完成 DOCX 的渲染级视觉检查。

