# Trader Reliability v1.5 修复范围与验收矩阵

日期：2026-09-09（Asia/Shanghai）  
范围：承接 v1.2/v1.3/v1.4，仅修复本轮 Guardian 时间语义和 AI 做单分析可见性，不新增交易所、策略、账户、云服务或付费依赖。

## 安全边界

- 只使用隔离临时 SQLite/PAPER 数据；不访问私有交易接口，不发送真实/TESTNET 订单，不解锁 LIVE。
- 保留所有用户 dirty/untracked 修改；不 reset、clean、commit、push，不覆盖业务数据。
- PAPER 才允许本地经济撮合；TESTNET/LIVE 保护只能由 adapter 回执和对账证明，缺 adapter 保留仓位并标记 DEGRADED/UNKNOWN。
- 新闻修订、时间 deadline、市场观测是独立保护驱动；缺少新鲜可执行价格不得用旧 bar 或旧 close 伪造成交。
- AI 分析使用权威 order/fill/position/execution 数据的只读投影；查询同步不得重放订单或重复写成交。

## 本轮修复范围

| ID | 根因/目标 | 生产接线 | 必须保留的行为 |
|---|---|---|---|
| V15-A | 首次累计 K 线的低/高可能早于开仓或保护生效，不能直接触发保护 | `MonitoringRuntime._on_stream_bar` → `PositionGuardian.process_bar` → scoped reduce-only Gateway/ledger | 根据 `bar_start`、事件时间/序列、fill 时间、保护生效时间判断可用观测；跨生效点的累计 OHLC 不可证明生效后触发；当前 bar 内和后续新鲜事件仍可触发 |
| V15-B | 相同报价去重提前返回，跳过新闻失效和时间退出 | `PositionGuardian` 独立 observation/deadline/revision 驱动；runtime 真实 callback 接线 | 市场价格相同不影响事件/时间检查；新闻修订不可用旧 bar 开盘价成交；无新鲜价为 PAPER pending/degraded，远端等待 adapter 回执 |
| V15-C | AI 做单分析漏掉策略/量化库产生的权威开仓/成交记录 | 现有订单、fills、positions、execution、AI analysis API → 统一只读关联投影 → 现有 UI | 同一开仓只出现一次；策略单不冒充 AI_LED；ACK 不算成交；部分成交/平仓/restart/refresh/account+venue+mode 隔离 |

## 字段流转矩阵

| 字段/事实 | 来源/API | 存储 | 查询消费者 | 效果/UI | 验收 |
|---|---|---|---|---|---|
| `account_id`/`venue`/`mode` | 账户与 intent scope | orders/fills/positions/execution | Guardian、投影、AI analysis | 不跨账户/环境串单 | V15-07/08 |
| `position_id`/`intent_id`/`order_id`/`trade_id` | Gateway/ledger 回执 | authoritative rows + payload | 关联去重；成交表与持仓生命周期表分开呈现 | 同一经济开仓只显示一次，OPEN/PARTIALLY_CLOSED/CLOSED 可追溯 | V15-06/07/08 |
| `bar_start`/`bar_end`/event seq | runtime/provider event | market bars/observation watermark | Guardian temporal gate | 旧累计极值不越过保护生效时间 | V15-01/02/03 |
| `fill_at`/`protection_effective_at` | fill/protection receipt | position/protection contract | Guardian | 当前 bar 内按事件顺序处理 | V15-03/04 |
| news revision state | `NewsRevisionRegistry` | revision chain | Guardian event invalidation | 相同报价也能触发失效；无新鲜价不成交 | V15-04/05 |
| time deadline | protection contract/clock | position contract + watermark | Guardian | 无新 K 线也检查时间退出 | V15-04/05 |
| strategy/version/path/cycle | intent/fill/AI cycle | order/fill/decision payload | read-only AI projection | `STRATEGY_DRIVEN`、`AI_FILTERED`、`AI_LED`、`MANUAL`、legacy 明确展示 | V15-06/07/08 |
| execution status | order/fill/reconciliation | authoritative order/fill rows | analysis/API/UI | ACK/pending/rejected/cancelled 不计作成交/开仓 | V15-06/07 |

## 验收矩阵

| 编号 | 必须证明的行为 | 证据层级 |
|---|---|---|
| V15-01 | 首次接入开仓前累计 K 线不触发错误 STOP；不跳过整根后仍能识别当前 bar 内/后续有效止损 | L1 isolated Guardian |
| V15-02 | bar start/end、fill/protection 生效时间和 observation seq 水位可追溯；旧/乱序/重复事件不越界 | L1/L3 fault injection |
| V15-03 | 当前 bar 内开仓后不利极值按可证明事件触发；多空方向正确；保护收紧不被旧累计极值触发 | L1 Guardian + runtime path |
| V15-04 | 同报价仍检查 time exit/news revision；无新 K 线可处理 deadline/revision；无新鲜价不伪造 PAPER/remote fill | L1/L2 |
| V15-05 | 新闻重复、重启、并发止损、AI discovery stop 后存量保护仍幂等 | L2/L3 |
| V15-06 | 策略/量化真实 API/runtime 产生 PAPER open/partial/close，权威记录能关联到 AI analysis；查询不写交易 | L2 API/runtime/E2E |
| V15-07 | 开仓、部分成交、持仓、部分平仓、已平仓、ACK/pending/rejected/cancelled 的分析投影正确且去重 | L2 API + projection |
| V15-08 | 两账户同 symbol、PAPER/TESTNET、restart/refresh、分页/时间筛选隔离；策略路径不冒充 AI_LED，AI 专属业绩不混入 | L2 API/UI |
| V15-09 | v1.2/v1.3/v1.4 Guardian、lease fencing、UNKNOWN budget、reduce-only、费用一次、typed plan 回归保持 | L1/L2 regression |
| V15-10 | 后端全回归、前端相关 tests/typecheck/build；JUnit、manifest、报告记录真实结果和 NOT_RUN 外部边界 | L1/L2 evidence |

## 失败优先与交付规则

先运行本轮失败回归并保存 JUnit，再修复同一测试。不得删除/放宽既有断言；允许为新的时间/状态契约补充明确 fixture 字段。需要的最小临时变异必须证明“重新绑定报价去重”“把时间检查绑回价格变化”“把策略记录冒充 AI_LED”能被测试抓住。

本地实现缺失记 `NOT_IMPLEMENTED`，外部条件缺失记 `NOT_RUN`；mock 只替代行情/新闻/adapter/model 边界，不替代 Guardian、投影、Gateway、账本和 API 消费者。
