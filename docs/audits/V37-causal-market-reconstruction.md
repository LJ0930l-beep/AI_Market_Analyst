# V37 因果行情重建

## 范围

构建器位于 `core/replay/pa_decision_quality_v37/evidence_builder.py`。它只读取已冻结的本地 Binance UM 研究数据库和同源归档 manifest，不调用网络、交易所私有接口、Gemini 或执行网关。

来源是 Binance 官方月度 1m K 线归档，经校验的归档数据库 SHA256 为 `c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2`；归档 manifest SHA256 为 `2f348e6a190690e173f0237b5f5614102bebeb8e0573a1266014c04f5b25012a`。两个 BTC/ETH 序列在全年窗口中均无分钟缺口或重复。

## 时间因果与来源字段

每根输出 bar 都保留 symbol、timeframe、`bar_start`、`bar_end`、`available_at`、`available_at_basis`、来源、原始月度 zip SHA256、manifest SHA256、数据库 SHA256、收盘状态、质量状态及 OHLCV。跨月 bar 同时保存组成该 bar 的归档哈希，并以其规范化哈希填充单一 `source_file_hash`。

`available_at` 被设置为 `bar_end + 60 秒`，并标记 `ASSUMED_PROXY_60S_AFTER_BAR_END_NOT_OBSERVED` / `ASSUMED_PROXY`。历史 Binance 归档能够证明价格时间，不证明数据曾在当时何时到达 AI Market Analyst。V36 只接收 `bar_end < decision_time` 且 `available_at < decision_time` 的 bar；因此重建在假设之下严格因果，但不属于 EXACT_OBSERVED 原始输入。

1m 数据按完整连续分钟聚合为 5m、15m、1h 和 4h。重复、乱序、缺失分钟、错误 OHLC 几何、来源哈希不符或无法验证的原始 zip 都会失败关闭；缺口不插值。未完成或在决策时尚不可用的周期 bar 不会进入输入。Swing 只在 V36 因果上下文已包含其确认窗口之后计算，并复核 `confirmed_at`、`available_at`、`known_at` 均早于决策时间。

构建器调用 V36 `build_context()`，每个周期要求至少 32 根，最多保留 48 根。其 V36 上下文哈希与包含归档来源、延迟假设和逐 bar 元数据的 V37 证据哈希分别保存，避免 V36 上下文哈希掩盖来源假设。

## 生成的工程样本

固定日历锚点位于三个既有 V25 recovery pilot 日期窗口的 12:00 UTC；锚点不按动作、盈亏或后续 A3 结果挑选，也不链接到任何 V25 `decision_id`。

| 本地 benchmark ID | 决策时间 UTC | 分区 | 5m / 15m / 1h / 4h bars | V37 证据输入 SHA256 |
|---|---|---|---|---|
| `v37-benchmark-btc-20251015-1200z` | 2025-10-15 12:00 | research | 48 / 48 / 48 / 47 | `bb4ba5af1d8aeffc770feff15abb05b4a36b8fca5ff26b2556e789cbba9fcca1` |
| `v37-benchmark-btc-20251214-1200z` | 2025-12-14 12:00 | validation | 48 / 48 / 48 / 47 | `05b690b60200edf88fc5556cb596cbb97549a65b25a122cb5a0a82782deaef53` |
| `v37-benchmark-btc-20260212-1200z` | 2026-02-12 12:00 | untouched_test | 48 / 48 / 48 / 47 | `1f5b8743085461ea870c2712ec855cae8c209a7376a2886e2fa3017d7589b846` |

每个点还记录独立的 V36 上下文哈希，详见本地 `reports/v37/market-reconstruction-manifest.json`。这三个点只用于数据因果性、哈希稳定性和离线运行器工程验收；每分区一个样本，不具备策略表现推断能力。它们均为 `RECONSTRUCTED_MARKET_BENCHMARK`，不能标为 V25 原始决策或历史 Gate 输入。

## 本地复现

```powershell
python scripts/reconstruct_historical_evidence_v37.py `
  --local-data-root D:\RJ\codex\ai-market-analyst `
  --output-dir D:\RJ\codex\ai-market-analyst\reports\v37-rebuild
```

脚本拒绝覆盖非空输出目录。全部逐 bar 行情与本地 source manifest 位于 Git 忽略的 `reports/v37/`，不会推送到 GitHub。
