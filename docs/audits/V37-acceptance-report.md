# V37 验收报告

## 验收结论

**V37 的来源清点、因果行情重建、离线重放和回归控制达到本阶段验收条件。** 这表示已有可复现的独立研究 benchmark；不表示恢复了 V25 的 Gemini 原始决策，也不代表任何策略盈利或 Gate 可成交。

基线为 `codex/v36.1-research-integrity` / `23ca359082193033d9c40ee282c41a074eee53a2`。开发分支为 `codex/v37-historical-evidence`。Pull request 以 V36.1 为 base，保持未合并。

## 核心发现

- V25 脱敏样本有 100 次扫描摘要及 10 笔完整平仓摘要，样本 SHA256 为 `4193956555d7576a3a754fbeb80198ad5b4828079b574dedf71eb347a13353cb`。100 条均不具备完整 A0 身份；精确 A0 恢复数为 0。没有按时间、标的或方向拼接 recovery pilot 缓存。
- V25 的 13 条开仓提案因缺少时点账户、保证金、费用/滑点和合约规则证据而全部 `BLOCKED`；其余 87 条不是开仓提案。10 条平仓只按脱敏摘要计数，没有重新计算盈亏或推断网关接受、成交路径。
- 已冻结来自 V35 提交 `2a5d7b9992a7909a6a597917e409ecb0737f9c89` 的基础 A0 `AI_ACTION_SCHEMA`。规范化 SHA256 为 `d2e264052568f7e6bf661c1cc40beef2dd573178f93ca0a5103d7b4a23d29763`。V25 每次调用的输入约束 Schema 不存在，冻结的基础 Schema 不用于倒推历史有效 Schema。
- 构建了 3 个按预定日历锚点选取的 BTCUSDT 重建 benchmark，每个点具备 5m、15m、1h、4h 因果窗口和逐 bar 来源哈希。价格来自校验过的 Binance UM 官方月度归档，属于 `VERIFIED_ARCHIVE_RECONSTRUCTION`；`available_at = bar_end + 60s` 是明确的 `ASSUMED_PROXY`，不是历史 Gate 到达时间。
- A0/A1/A2 均维持 `NOT_RUN`。A3 只作为确定性研究候选运行，历史经济性检查全部阻断。离线 replay 的模型调用为 0；订单创建为 0；网关接受、成交和完整平仓均未观察到。

## 修改文件与原因

- `configs/research/schemas/a0-v35-action-schema.json`：固定 V35 来源、规范化哈希及基础 Schema 内容。
- `core/replay/pa_decision_quality_v36/validation.py`：V36 A0 改为加载并验证冻结 Schema，避免运行时生产 Schema 改动改变历史 A0 校验版本。
- `core/replay/pa_decision_quality_v37/`：新增只读来源清点、严格 A0 身份恢复分类、缺失经济证据阻断、月度归档校验、四周期聚合和因果可用时间检查。
- `scripts/reconstruct_historical_evidence_v37.py`：从冻结输入构建本地报告，并逐字节比较 V36 CLI、直接离线回放及独立 review 指标。
- `scripts/compare_v37_test_evidence.py`：记录并比较完整 pytest 失败 node ID 与失败阶段，不以失败总数代替基线核对。
- `tests/v37/test_historical_evidence.py`：覆盖冻结 Schema、只读 SQLite、OHLCV 质量/因果边界、缓存身份、经济阻断、离线零调用和生命周期指标。
- `docs/plans/V37-development-plan.md` 与 `docs/audits/V37-*.md`：记录执行计划、证据分级、审计发现、重建范围、接受条件和后续限制。

未修改生产交易执行逻辑、Gate 网关、生产资金/风险参数、V25/V33/V34/V35/V36/V36.1 历史结果或原始数据库。大体量行情、SQLite 和逐 bar 报告保存在 Git 忽略的本地 `reports/v37/`。

## 离线产物与复现

本机报告目录：`D:\RJ\codex\ai-market-analyst\reports\v37\`。其中包括 `source-inventory.json`、`a0-recovery-coverage.json`、`economic-evidence-coverage.json`、`market-reconstruction-manifest.json`、`decision-points.json`、`offline-replay-reference.json`、`offline-replay.json`、`offline-review.json`、`replay-readiness.json` 和全量测试证据。该目录由 `.gitignore` 忽略，不会提交到公共 GitHub。

三点的 V37 证据输入哈希依次为：

- `v37-benchmark-btc-20251015-1200z`：`bb4ba5af1d8aeffc770feff15abb05b4a36b8fca5ff26b2556e789cbba9fcca1`
- `v37-benchmark-btc-20251214-1200z`：`05b690b60200edf88fc5556cb596cbb97549a65b25a122cb5a0a82782deaef53`
- `v37-benchmark-btc-20260212-1200z`：`1f5b8743085461ea870c2712ec855cae8c209a7376a2886e2fa3017d7589b846`

V36 CLI 输出 SHA256 为 `d0eee6baa9244a51eedae1f37a6d14a0fc8cf77c6c397455175a1b7f1347398f`，直接运行与 CLI 字节完全一致；review 输出 SHA256 为 `85af68b0f2ee3ba649984eac9bb06039f68d885868bdc4a2c455fa1993d13a1c`，指标与独立 `summarize()` 完全一致。

复现命令：

```powershell
python scripts/reconstruct_historical_evidence_v37.py `
  --local-data-root D:\RJ\codex\ai-market-analyst `
  --output-dir D:\RJ\codex\ai-market-analyst\reports\v37-rebuild
```

测试证据复现时，先在精确 V36.1 基线工作树和 V37 工作树分别运行 `python -m pytest -q -p scripts.v36_test_evidence --v36-report=<本地新 JSON 路径>`，再以两份证据调用 `scripts/compare_v37_test_evidence.py`。不要覆盖已生成报告。

## 测试结果

- `python -m pytest -q tests/v37 tests/v36`：**64 passed**。
- V35 风控/费用/固定名义金额回归：`python -m pytest -q tests/test_v35_trade_feasibility.py tests/test_gate_entry_economics.py tests/test_fixed_notional_ai_execution.py`：**51 passed**。
- 全仓 V36.1 基线：**2190 passed, 100 failed, 1 skipped**；共 2291 个收集项。
- 全仓 V37：**2201 passed, 100 failed, 1 skipped**；共 2302 个收集项。
- 全量逐项比较：环境一致，基线失败 node ID 与阶段完全相同；缺失基线测试 0、新增失败 0、失败阶段变化 0，11 个新增 V37 用例全部通过。结论 `PASS_NO_NEW_FAILURES`。原有 100 项失败仍保留并作为继承失败，不宣称全仓全绿。
- 仓库的 `pre-commit` 钩子会无条件重跑 pytest，并将继承失败作为硬阻断。基于上述已完成的同环境完整差异证据，提交使用 `--no-verify` 避免重复运行；V37 定向测试和完整基线对照均已实际执行。
- Ruff 覆盖新增模块、脚本和 V36 验证改动：通过。目标文件 `compileall`：通过。`git diff --check`：通过。
- V25 样本、应用主数据库和三个 V25 recovery pilot SQLite 的哈希在检查前后相同；来源数据未被修改。

## 未解决问题与残余风险

1. V25 原始 Prompt、模型输入、点时状态、逐次有效 Schema 和可验证响应身份仍缺失，因此不能做 V25 A0 历史决策质量比较。
2. 本地 Gate 行情没有覆盖三个 V25 pilot 日期；重建价格为 Binance 代理。60 秒 `available_at` 延迟也没有历史到达记录支持。
3. V25 提案缺少点时权益、保证金、费用/滑点和合约规则，经济可行性结论保持阻断；平仓摘要不等同于完整委托与成交证据。
4. benchmark 每个预定分区只有一个工程样本，不能用于胜率、收益或市场状态效果结论。
5. 冻结文件只覆盖 V35 基础 Schema。研究验证仍调用 V36/V35 中通用 Schema 验证 helper；后续若要求长期独立重放，应同时冻结 helper 语义或将其纳入版本化研究验证器并添加等价性测试。

## V38 前置条件

要声称进行 V25 历史 A0 对照，必须取得并校验原始 Prompt、完整 model input、状态快照、逐调用有效 Schema、原始响应、精确身份字段与实际模型 ID 的证据。要提出 Gate 专属可行性或执行结论，还需恢复决策时的 Gate 行情/到达时刻、账户权益/保证金、合约规则和费用证据。

如果只开展新的 Gemini 研究，须作为独立新实验取得明确授权，明确区分重建 Binance benchmark 与真实 Gate 历史，并继续保持零生产订单及零自动应用策略结果。
