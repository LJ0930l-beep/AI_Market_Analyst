# Trader Reliability v1.5 修复报告

日期：2026-09-09（Asia/Shanghai）  
开发者：Luna Max，单一连续上下文  
项目：`D:\RJ\codex\ai-market-analyst`  
范围：`docs/trader-reliability-v1.5-plan.md`；承接 v1.2/v1.3/v1.4

## 结论

本轮两个独立缺陷已经在生产入口修复，并完成隔离 SQLite/PAPER 的失败→通过回归。PositionGuardian 不再把开仓/保护生效前的累计 OHLC 当作有效止损证据；新闻修订和时间期限不再被相同报价去重跳过，也不使用旧 bar 开盘价伪造成交。AI 做单分析现在以 `trade_fills` 为权威经济事实、以 `order_intents` 为订单状态、以 `simulated_positions` 为持仓生命周期，并通过实际 API/runtime/Gateway/ledger 链路展示策略开仓、部分成交、部分平仓、已平仓和未成交订单。

本地最终验证：后端 **316 passed、1 skipped、1 warning**；v1.5/Guardian/v1.4/分析聚焦 **36 passed**；前端 **20 files / 88 tests passed**，typecheck 和 production build 均通过。没有读取私有凭据、没有发送真实或 TESTNET 订单、没有解锁 LIVE、没有 reset/clean/commit/push，也没有写入用户业务数据库。

## 问题→修改→生产效果→证据

| 问题/能力 | 根因 | 修改路径 | 实际生产入口与效果 | 测试与证据层级 | 限制 |
|---|---|---|---|---|---|
| A：首次累计 K 线在开仓前已出现低点，却被判定 STOP | `Bar.timestamp` 只有 bar 开盘时间，Guardian 直接消费整根 OHLC，没有和 `entry_filled_at`/保护生效时间对齐 | `core/providers/base.py`、`core/realtime.py`、`core/gate_stream.py`、`core/trading/ledger.py`、`core/trading/position_guardian.py` | `MonitoringRuntime._on_stream_bar`/`process_market_event` → `PositionGuardian.update_market_event/process_bar`；增加 `bar_end`、`event_at`、`sequence`，用 later activation boundary；跨界累计 OHLC 只允许有时间依据的 fresh executable point，完整生效后 bar 才可使用 high/low | `test_v15_old_cumulative_bar_cannot_trigger_before_position_activation`、`test_v15_current_bar_crossing_activation_uses_timestamped_point_not_old_open`、runtime 路径测试；L1 isolated Guardian/L2 runtime | 没有时间戳的历史 OHLC 不能证明盘中顺序；必须等 fresh point/provider event，不把缺证据升级成成交 |
| A：当前 bar 内开仓后不能因“跳过整根”漏掉有效保护 | 旧实现只能二选一：全根消费或整根跳过 | `PositionGuardian._market_observation_context/_fresh_executable_price` | current bar 携带 post-activation `event_at` 时，按 close/last/mark executable point 触发；后续完整 bar 按 OHLC；多空方向和 trailing CAS 保持 | `tests/test_guardian_intrabar.py` 5 项旧修复全部保留并通过；L1 | 无法从无时间顺序的 aggregate low/high 推断低点发生在激活后 |
| B：相同报价去重跳过新闻修订和时间退出 | market observation 去重在保护条件前 `continue`，且旧逻辑使用 bar 开盘时间/旧 close | `core/trading/position_guardian.py`、`core/monitoring_runtime.py` | Guardian 每次调用独立检查 news revision 和 wall-clock deadline；`ACTIVE_OR_CORRECTED` 字符串正确解释；有 fresh executable point 才 PAPER 平仓/提交 remote reduce-only；无 fresh quote 为 `PENDING` + `DEGRADED`，持仓仍 OPEN | `test_v15_news_correction_is_independent_from_same_quote_deduplication`、`test_v15_news_trigger_without_fresh_quote_is_pending_and_degraded`、旧 v1.3/v1.4 event/time 回归；L1/L2 | 远端仍需 adapter receipt 和成交对账；本地 stale bar 不会被重新解释为新报价 |
| B：无新 K 线也要到期检查；AI 停止不能停止存量保护 | 保护检查依赖行情变化/策略 worker 生命周期 | `core/trading/position_guardian.py`、`core/monitoring_runtime.py` | Guardian loop 重复观察已接收 feed，deadline 使用 wall clock/observation/bar end；runtime 停止发现后保留 Guardian/feed/保护责任 | v1.4 stopped-runtime time-exit/protection 回归、v1.5 runtime callback；L2 | 没有任何 feed/quote 时只能记录待处理保护事实，不能产生经济成交 |
| B：量化/策略库开仓在 AI 做单分析不可见 | 分析只读 `agent_trade_decisions`/旧 simulation view，没有把权威 fill 与 intent/position/plan 关联 | `core/analysis/ai_trade_analytics.py`、`apps/api/v2.py`、`web/src/pages/V2WorkspacePage.tsx`、`web/src/pages/V2WorkspacePage.test.tsx` | `POST /v2/trade-plans` → `trader_trade_plans` → `MonitoringRuntime.process_market_event`/armed-plan consumer → `ExecutionGateway.submit_intent` → `AccountLedger` → `trade_fills/simulated_positions` → `GET /v2/ai-analysis` → Workspace；真实 entry/partial/exit 一条 authoritative fill 一行，持仓生命周期另表显示 | `test_v15_strategy_runtime_fill_is_visible_once_in_ai_analysis_projection`；`test_v15_guardian_exit_is_projected_as_exit_not_a_second_entry`；API/runtime E2E；L2 | UI boundary 使用 fixture API response；核心 Gateway/ledger/projection 使用真实本地链路，未宣称真实交易所验证 |
| B：ACK/pending 被误认为成交或开仓 | 订单状态与经济事实没有分离 | `core/analysis/ai_trade_analytics.py`、Workspace UI | `trade_fills` 是唯一 concrete economic fill source；ACK/CREATED/SUBMITTING/CANCEL_PENDING 显示为 `NOT_FILLED_PENDING`，REJECTED/CANCELED/EXPIRED 不生成 fill；FILLED 无 fill 显示 `RECONCILIATION_REQUIRED` | `test_v15_ai_analysis_keeps_resting_ack_out_of_economic_fills`；L1/L2 | 远端状态为 FILLED 但没有 fill report 仍必须人工/adapter 对账 |
| B：归属、环境和历史映射不安全 | 旧 position/fill 查询缺少统一 account/venue/mode 过滤，旧记录来源不明确 | `core/analysis/ai_trade_analytics.py`、`core/trading/ledger.py`、`core/trading/execution_gateway.py` | 投影严格绑定 account + mode + venue + position/intent/order/trade；`STRATEGY_DRIVEN`、`AI_FILTERED`、`AI_LED`、`MANUAL`、`LEGACY_UNCONFIRMED` 显式展示；不确定 legacy 不分配给账户；exit 继承 entry 归属但不冒充新的 AI_LED 决策 | v1.5 两账户同 symbol、Guardian exit、projection API；v1.3/v1.4 account isolation 回归；L2 | 旧的未分配记录保持 UNKNOWN/legacy，不会为了图表强行映射 |

## 字段流转矩阵

| 行为字段/事实 | API 或事件输入 | 持久化 | 消费者与效果 | UI/测试 |
|---|---|---|---|---|
| `bar_start`/`bar_end`/`event_at`/`sequence` | Provider `Bar`、realtime kline、Gate stream | `Bar`/runtime feed 与 position observation watermark | Guardian temporal gate；旧序列拒绝 market revision，跨 activation 仅用 fresh point | V15-01/02/03、intrabar 回归 |
| `entry_filled_at`/`protection_effective_at` | Ledger fill/protection confirmation | `simulated_positions.payload_json` | later activation boundary；前置累计 low/high 不可触发 | V15-01/03 |
| `event_invalidation`/`invalid_if` | Trade plan/protection contract + `NewsRevisionRegistry` | plan/position contract、revision chain | revision 与 market change 解耦；缺 quote 为 pending/degraded | V15-04/05 |
| `time_exit_at` | typed plan contract | position protection contract | wall-clock + observation/bar-end 检查；停策略仍保护 | v1.4 time-exit + V15-04 |
| `account_id`/`venue`/`mode` | registered account、intent、fill | accounts/order/fill/position rows | Gateway、Ledger、Guardian、projection 全部 scope check | V15-06/08、v1.3/v1.4 isolation |
| `position_id`/`intent_id`/`order_id`/`trade_id`/`fill_id` | Gateway/ledger receipts | authoritative rows/payload | alias 关联且以 fill_id 去重；不写补偿成交 | V15-06/07 |
| `strategy_id`/`strategy_version`/`decision_path`/`cycle_id` | plan/intent/decision/AI cycle | `trader_trade_plans`、`order_intents`、fill payload、cycle | 路径归属与 AI 专属绩效分离；不把策略单改称 AI_LED | V15-06/08 |
| order status / fill status | Gateway receipt、adapter reconciliation | `order_intents`/`trade_fills` | ACK/pending/reject/cancel 与 concrete fill 分开 | V15-07 |
| position status / protection status | Ledger/Guardian | `simulated_positions` | OPEN/PARTIALLY_CLOSED/CLOSED 与 ACTIVE/DEGRADED/UNKNOWN 展示 | V15-07/UI lifecycle table |

## 失败→通过证据

本轮先保存了当前失败行为，再修改同一实现入口：

- `artifacts/junit-v15-failing.xml`：旧累计 K 线错误 STOP 的失败证据。
- `artifacts/junit-v15-failing-b.xml`：策略权威成交投影缺 `execution_records` 的失败证据。
- `artifacts/junit-v15-final3.xml`：v1.5 核心 8 passed。
- `artifacts/junit-v15-focused-final4.xml`：v1.5 + Guardian intrabar + v1.4 + gate/analytics 36 passed。
- `artifacts/junit-backend-v15-final4.xml`：后端完整套件 316 passed、1 skipped。
- 前端 `npm test -- --run`：20 files / 88 tests passed；关键 `V2WorkspacePage` 4 passed；`npm run typecheck`、`npm run build` 退出码 0，build 转换 82 modules。

本轮证据文件 SHA-256（大写十六进制）：

| 文件 | SHA-256 |
|---|---|
| `artifacts/junit-v15-failing.xml` | `538D4D7A39BC52AF4EDDC336B774D0F30B2B5EA046A305174BF0B55E62E01357` |
| `artifacts/junit-v15-failing-b.xml` | `19CE8708360607B10934103BDEB430D539E63933569E97C164C9EB53F8872831` |
| `artifacts/junit-v15-final3.xml` | `167605239E81B239597DBC43D336F6B723B289752E625DDF635A16E2B35A7439` |
| `artifacts/junit-v15-focused-final4.xml` | `F7842AEE80C7368B1354E99F48CAB57A1A57EF0B7E846C80129F0D389A174D06` |
| `artifacts/junit-backend-v15-final4.xml` | `1A5CE5355F2B574DFA3964D357959B78B41F8DDF014A404AEDCD23C13EADCA57` |
| `artifacts/vitest-v15-final.json` | `FBDAF6D8CCAA4CAA9D0204F87D36DA3D7171D917352D7E7119BF87F758DB7817` |
| `evidence/manifest.json` | `CF0EA3E8847F8ACF7B3859DFF88043A3908011A3D724F343D9F79B68CAFF7C4A` |

`artifacts/trader-reliability-v1.5-evidence.json` 是配套机器可读记录；其中的 report/plan/source/JUnit hashes 与本报告对应。manifest 不嵌入自身 hash，也不把报告 hash写回 manifest，以避免循环哈希；交付时以上是冻结 manifest 的实际 hash。

没有删除或放宽旧断言。`tests/test_guardian_intrabar.py` 保留并通过：同 timestamp 新不利极值、trailing 收紧不消费旧极值、重复观测幂等、低 volume/高低范围旧 revision 拒绝、无 volume 的旧事件兼容、以及实际 runtime callback 保护路径。v1.2/v1.3/v1.4 的 reduce-only、UNKNOWN 预算、lease fencing、TESTNET 无 adapter 不本地平仓、费用一次和 typed plan 回归包含在完整套件中。

## 实际生产接线路径

```text
量化/策略 UI
  -> POST /v2/trade-plans (canonical typed plan)
  -> TraderCapabilityService.create_trade_plan / trader_trade_plans
  -> MonitoringRuntime.process_market_event 或 _on_stream_bar
  -> process_armed_trade_plans
  -> ExecutionGateway.submit_intent (scope/risk/idempotency)
  -> AccountLedger.record_trade_fill
  -> trade_fills + simulated_positions
  -> GET /v2/ai-analysis?account_id=...&mode=...&venue=...&from_at=...&to_at=...&page=...
  -> V2WorkspacePage：权威成交、未成交订单、持仓生命周期、归属绩效
```

Guardian 保护路径独立于策略/AI discovery：`stream/runtime callback → update_market_event → PositionGuardian.process_bar → PAPER scoped CAS/fill`；TESTNET/LIVE 只进入 `ExecutionGateway` remote reduce-only adapter 边界，缺 receipt 不改 CLOSED/PnL。

## 数据迁移、回滚和恢复

本轮 schema 变化是 additive/idempotent：`Bar` 增加可选时间/序列字段；ledger 既有 position/fill payload 增加 entry/protection timing 与 scope metadata；order intents 增加策略/路径/session/signal metadata。初始化会为缺失列补列，不删除或重写经济 fill。已有未证明归属的 position 继续 `legacy_unverified`，Guardian 和投影都禁止猜测管理；不确定历史不会被写入当前账户。

回滚或升级前先停止 runtime/sidecar，确认没有 WAL/SHM 写入和远端对账在途，备份 SQLite 文件并用新文件名恢复，重新执行 `SQLiteStore.initialize()`、完整性检查和 account/venue/mode 对账。不要删除或回放 `trade_fills`；若回退到不识别 v1.5 payload 的旧代码，先将对应 position/protection 标为 review/UNKNOWN，禁止旧路径直接执行 reduction。代码回滚不会自动撤销已经发生的本地事实；UI 在投影缺失时只回到旧只读视图，不补写订单或成交。

## 产品能力状态与限制

已具备本地运行闭环：

- 策略/量化 API/runtime 产生 PAPER opening fill，经过 Gateway 与 Ledger 后在 AI analysis 中一次出现；部分减仓、Guardian exit、持仓生命周期、pending order 和两个账户/环境 scope 可追溯。
- 旧累计行情、乱序/重复 observation、新闻 revision、时间 deadline、保护收紧和 runtime 停止后的存量保护具有明确安全边界。
- attribution/performance 表只统计权威记录；AI_LED 专属绩效不混入 strategy-driven；缺失来源、费用、相关性或事件证据显示 UNKNOWN/保守状态，不以 confidence 代替胜率或盈利保证。

仍不能在本地证实或属于外部门槛：

| 项目 | 状态 | 原因 |
|---|---|---|
| 真实 Qwen 9B 推理/E2E | `NOT_RUN` | 本轮未下载、未调用真实模型；provider-shaped/mock boundary 不是模型验收 |
| 外部 TESTNET native protection、远端成交、真实费用 | `NOT_RUN` | 无独立授权、私有客户端或凭据；没有发送订单 |
| 干净 Windows 安装/RT25 | `NOT_RUN` | 按要求保留当前 dirty/untracked 工作区，未伪造安装结果 |
| LIVE | `LOCKED` | 未解锁、未访问账户、未使用私钥 |
| 无权威历史归属/相关性/事件消化程度 | `UNKNOWN` | 不猜测，不把样例或静态图表当作证据 |

## 旧回归放宽清单

无。旧断言未删除、未 skip、未降低；本轮只为时间字段、真实 authority projection 和持仓生命周期增加测试输入/视图。工作区原有 `core/providers/gateio_provider.py:144` EOF 空行警告仍由用户 dirty 文件保留，未为本轮改动。
