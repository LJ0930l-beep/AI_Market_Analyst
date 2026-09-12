# AI 做单分析资金口径迁移到 Gate 模拟盘远端事实

日期：2026-09-11
账户：`gate_testnet`（`account_type=GATE_TESTNET`、`execution_mode=TESTNET`）

## 1. 问题

「AI 做单审计看板」显示 `初始资本 10000 / 当前权益 10000 / 净盈亏 +0.00 / 累计费用 0.00`，
而 Gate 官方 TestNet 账户的实际权益是 5 万多 USDT。看板在用**本地种子存款**冒充账户资金。

## 2. 根因

`accounts.initial_deposit = 10000` 是本地兼容行里的种子值，不是交易所事实。
它从两个出口泄漏到了界面：

| 出口 | 端点 | 泄漏点 |
| --- | --- | --- |
| AI 做单审计看板 | `GET /v2/ai-analysis/dashboard` | `core/analysis/institutional_dashboard.py` 用 `initial_deposit` 算 `initial_capital_usdt` 与 `current_equity_usdt`，并把 `data_quality.source` 标成 `authoritative_local_execution_ledger` |
| AI 做单分析 KPI 卡 | `GET /v2/ai-analysis` | `core/analysis/ai_trade_analytics.py` 把 `initial_deposit` 直接写进 `account.initial_capital_usdt` |

值得注意的是，项目自己在 `apps/api/v2.py` 的 `/accounts` 接口里**已经写明了正确原则**：

> The persisted compatibility row may contain an old local seed (for example 10000).
> It is not an exchange fact and must never be displayed as TestNet equity or starting capital.

也就是说这不是设计缺失，而是这条原则没有推广到上述两个分析端点。本次改动就是把口径统一过来。

## 3. 改动

### 3.1 新增远端资金基准解析器

`core/trading/gate_account_truth.py`

```python
resolve_remote_capital_basis(store, account_id) -> dict
```

口径定义：

- **起算权益** = 首个 `equity` 有限且非负的远端快照
- **当前权益** = 最后一个可信远端快照
- **净盈亏** = 两者之差
- **累计费用** = 远端 `fills_json` 中 `fee_cost` 的合计（证据标识 `OBSERVED_REMOTE_FILLS`）
- **权益曲线 / 最大回撤** = 由可信快照全序列现算
- 无远端快照 → 返回 `UNAVAILABLE`，**绝不回落本地种子**
- 观测超过 300 秒 → `stale = true`

**必须跳过负 equity 的早期快照。** 库内最早的几条 AVAILABLE 快照长这样：

```
2026-09-10T16:47:36Z  equity=-200.073125   available_margin=50160.970996
2026-09-10T16:52:21Z  equity=-198.708331   available_margin=50162.676453
```

早期投影把 `equity` 算成了 `realized + unrealized`，于是出现「负权益配正常保证金」的行。
不跳过就会在权益曲线上画出一根不存在的悬崖。

### 3.2 消费方改造

- `core/analysis/institutional_dashboard.py`：Gate 账户（`scope.provider == "gate"`）的
  `account` 区块、权益曲线、最大回撤全部改读远端；`data_quality.source` 改为
  `gate_testnet_remote_account_truth`；新增 `capital_basis_status` / `capital_observed_at` /
  `capital_stale`。**Gate 账户路径完全不再读 `initial_deposit`。**
- `core/analysis/ai_trade_analytics.py`：以 `account_type == "GATE_TESTNET" or mode == "TESTNET"`
  判定托管账户，账户区块与 `equity_curve` 远端化；未同步时资金字段全为 `null`。
- 前端：文案「初始模拟本金」→「起算权益（远端）」；KPI 副标题区分「已同步 / 已过期 / 未同步」；
  看板新增「可用保证金」KPI 与未同步/过期提示条；
  「刷新模拟盘对账」按钮现在同时刷新风险面板与两个分析视图。

### 3.3 一个刻意的克制

空账户的看板 `status` **没有**从 `EMPTY` 升级为 `DEGRADED`。
`tests/test_institutional_gate_testnet.py` 断言了全新 Gate 账户的看板为 `EMPTY`。
缺远端资金改由 `data_quality` 与 `empty_state` 文案表达，不动已存契约。

## 4. 真实链路验收（Gate 官方 TestNet）

### 4.1 市价单

`GateTestnetE2EService`（即 `POST /v2/gate/testnet/order-test` 的实现），8 阶段全部 COMPLETED：

```
METADATA              已读取合约最小数量、精度和费率
PROTECTION_DERIVED    止损/止盈已按价格精度与方向约束推导
LEVERAGE              杠杆设置已确认
ORDER_SUBMITTED       订单已发送
FILL_RECONCILED       成交数量已确认
POSITION_RECONCILED   远端持仓已确认
PROTECTION_RECONCILED 原生 reduce-only 止损/止盈已确认
CLEANUP               reduce-only 清理完成并复核无剩余持仓
```

| 项 | 值 |
| --- | --- |
| run_id | `gate_e2e_595154a3ce8565028d94b2d0` |
| status | `COMPLETED` |
| orders_sent | 2 |
| model_called / local_fill_created | `false` / `false` |
| 入场 | FILLED @ 77255.3，1 张（contractSize 0.0001） |
| 原生保护单 | `PROTECTED`，stop 75672.1 / tp 79533.0 |
| 平仓 | FILLED @ 77221.7（reduce-only） |
| 终态 | positions = 0 |
| 权益 | 50172.7905 → 50172.7794 |

> 该链路刻意不写本地账本（`local_fill_created=false`），`trade_fills` / `order_intents` 不会新增行。
> 判断成功只能看 `stages` 与远端对账，不能看本地表。

### 4.2 限价单

仓库内**没有**限价链路服务（`GateTestnetE2EService` 只发市价单），按 6 步自建并对账：

| 步骤 | 远端实测结果 |
| --- | --- |
| 0 前态 | 1 笔遗留 OPEN 挂单 `144115188078845537` BUY 65640.1 |
| 1 撤遗留单 | 撤单受理 |
| 2 复核消失 | `get_open_orders()` → `[]`；used_margin 3.2919 → **0.0** |
| 3 挂新限价单 | last 77158.2 → 限价 **69442.3**（低 10%，永不成交通），1 张，`ACKNOWLEDGED` / `remote_status=open` |
| 4 复核 OPEN | 按 `client_order_id` 命中 1 笔，`status=OPEN` |
| 5 撤单 | `finish_as=cancelled` |
| 6 终态 | `open_orders=0`、`positions=0`、`used_margin=0.0`、equity 50172.7794 |

遗留挂单的处置**事先征求了用户裁定**（用户选择「先撤掉再跑链路」）。

证据文件：

- `evidence/gate_testnet_market_e2e_20260911T080238Z.json`
- `evidence/gate_testnet_limit_e2e_20260911T081036Z.json`

## 5. 切换前后对照

| 指标 | 切换前（本地种子口径） | 切换后（远端事实口径） |
| --- | --- | --- |
| 起算权益 | 10000.00 | **50161.29**（首个可信远端快照） |
| 当前权益 | 10000.00 | **50172.78** |
| 净盈亏 | +0.00 | **+11.49（+0.0229%）** |
| 已实现盈亏 | — | **295.45** |
| 可用保证金 | — | **50172.78**（已用 0.00） |
| 累计费用 | 0.00 | **0.7249**（`OBSERVED_REMOTE_FILLS`） |
| 最大回撤 | 0.00% | **0.0025%**（远端权益曲线现算） |
| 权益曲线 | 单点 | **37 个远端快照点** |

> 费用这一项最能说明换源的必要：本地 `trade_fills` 那 3 条的 `fee_amount` **全是 0**，
> 而远端成交里是真实费率。本地口径下「累计费用 0.00」永远不可能对。

## 6. 验证

| 项目 | 结果 |
| --- | --- |
| 后端完整 pytest | **391 passed** |
| 前端 `tsc --noEmit` | 0 错误 |
| 前端 `eslint --max-warnings=0` | 0 警告 |
| 前端 `vitest src` | **99 passed（22 文件）** |
| 客户端重建 | app 12 062 720 B / sha256 `256f355f…8bce`；sidecar 59 916 076 B / sha256 `62b682b4…c06f` |
| 安装校验 | 覆盖后哈希一致 |
| 端到端实测 | `/v2/ai-analysis/dashboard` 与 `/v2/ai-analysis` 均返回远端权益（非 10000）；`capital_stale=false` |
| 客户端同步 | `/v2/gate/account/refresh` 经运行中的客户端执行成功：equity 50172.779、positions 0、pending_orders 0、fills 14 |

## 7. 遗留

- 看板的 `details.fills` / `details.orders` 仍来自账户作用域执行账本（本地 `trade_fills`、`order_intents`），
  与远端费用 KPI 的出处不同。两者已在界面上分别标注来源，但若要完全对齐需另做一次投影改造。
- `scripts/sync-client.ps1` 在沙箱 PowerShell 通道内无法端到端跑通（该通道解析不到 `cmd`，
  会在 `build-tauri.ps1` 处中断）。本机重建一律走 `bash scripts/build-client.sh`。
