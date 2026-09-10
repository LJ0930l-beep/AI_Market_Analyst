# Trader Reliability v1.6 修复契约与验收矩阵

日期：2026-09-09（Asia/Shanghai）  
范围：承接 v1.5，只修复本轮四项 Guardian/分析边界；不新增交易所、策略、账户、云服务或付费依赖。

## 不可突破的安全契约

- 仅使用隔离临时 SQLite/PAPER 测试；不读取私钥，不发送真实/TESTNET 订单，不解锁 LIVE。
- 保留当前所有 dirty/untracked 用户成果；不 reset、clean、commit、push。
- 实时墙钟只能由真实已到达的观测推进；未收盘 bar 的 `bar_end` 不是已发生事件。回放必须显式使用虚拟时钟，不能用未来 fixture 放宽实时语义。
- 跨开仓/保护生效点的累计 OHLC 只有在保护生效后明确观测到的新极值或逐笔/报价证据可用；首次旧极值不得误平，已证明的新极值必须触发。
- Guardian 从持久化 scoped positions 开始检查，即使 `_price_feed` 为空；无 fresh executable quote 只能记录 `PENDING/DEGRADED`，不能伪造成交、PnL 或远端回执。
- AI 分析先在完整同账户/环境生命周期证据上解析归属，再应用事件时间窗口；窗口内绩效只统计窗口内经济事件，不能把全生命周期持仓 PnL 冒充区间收益。

## 四项修复与生产接线

| ID | 缺陷根因 | 生产接线 | 必须达到的效果 |
|---|---|---|---|
| V16-01 | `_deadline_reached` 将未收盘 `bar_end` 当作当前时间 | `MonitoringRuntime` → `PositionGuardian.process_bar/_run_guardian_loop` | 实时 deadline 只由 wall clock、已到达 event/quote 或已结束 bar 推进；未来 `bar_end/event_at` 不提前退出；回放虚拟时钟显式隔离 |
| V16-02 | 跨 activation bar 只看 close，丢失生效后累计新不利极值 | `PositionGuardian` temporal observation state/CAS | 首次旧 OHLC 不触发；同 bar 后续可信新极值跨 stop 触发；LONG/SHORT、乱序、重复、重启、trailing 收紧安全 |
| V16-03 | Guardian loop 只遍历 `_price_feed` | `PositionGuardian._run_guardian_loop` → scoped durable positions | 空 feed/重启/断流/暂停发现仍检查期限与新闻；无报价持久 PENDING/DEGRADED，恢复报价按共享幂等 reduce-only 继续 |
| V16-04 | 日期过滤后才建立 `position_attribution` | `build_execution_ledger_projection/analyze_ai_trading_ledger` → API/UI | 先构建完整生命周期归属，再过滤成交事件/快照；区间不改 attribution；区间 PnL 只计窗口内 fills 的净实现结果；分页不改变归属/汇总 |

## 字段流转矩阵

| 字段/事实 | 来源 | 持久化/状态 | 消费者与效果 | 验收 |
|---|---|---|---|---|
| `bar_start`/`bar_end`/`event_at`/`sequence` | provider/runtime/回放事件 | Bar、Guardian watermark | 识别已结束区间、当前事件、乱序和累计新极值 | V16-01/02 |
| `clock_now`/`replay_now`/`is_replay` | runtime clock 或显式 replay context | 本次评估上下文 | 未来 fixture 不可推进实时期限；回放只能用注入虚拟时钟 | V16-01 |
| `entry_filled_at`/`protection_effective_at` | Ledger/Gateway protection receipt | position payload | activation boundary 与后续观察基线 | V16-02 |
| `protection_last_bar_observation`/`...at`/sequence | Guardian | position payload CAS | 同 bar 新极值可证明，重复/旧 revision 幂等拒绝 | V16-02/03 |
| durable scoped position | accounts/positions | `simulated_positions` | 空 feed 仍执行 deadline/news；账户/venue/mode 隔离 | V16-03 |
| trigger + fresh quote status | market/revision/adapter | protection evidence/status | 无 fresh quote 为 PENDING/DEGRADED，不写 economic fill | V16-03 |
| full lifecycle attribution | all scoped fills/orders/positions/plans/cycles | projection context | 窗口外 entry 仍为策略/AI/人工真实归属 | V16-04 |
| event window / snapshot window | API `from_at/to_at` | response metadata | fills/PnL 口径可追溯，不隐藏历史归属 | V16-04 |

## 验收矩阵

| 编号 | 必须证明的行为 | 证据层级 |
|---|---|---|
| V16-01 | 期限前不退出；恰好/墙钟到期退出；未来 `bar_end`/异常未来 `event_at` 不提前；重启/重复 tick 与显式回放时钟可控；新闻仍可独立触发 | L1/L2 isolated Guardian/runtime |
| V16-02 | activation 前累计极值不误平；activation 后同 bar 新极值 LONG/SHORT 均保护；旧更坏极值、trailing 新基线、乱序、重复、重启、price-only 边界明确 | L1/L2 Guardian + runtime |
| V16-03 | 空 feed loop 从持久化 scoped positions 工作；断流/暂停/移除自选仍检查；无报价 PENDING/DEGRADED；恢复幂等；TESTNET 无 adapter 不本地撮合 | L1/L2/L3 fault injection |
| V16-04 | 窗口外开仓+窗口内部分/全平归属不变；区间 PnL 仅计窗口 fills；Guardian/AI_LED/策略/人工/legacy 分离；分页、账户、环境、UI 筛选一致且只读 | L2 API/projection/UI |
| V16-05 | v1.2-v1.5 全部安全回归保持：remote no-adapter、runtime rebind、lease fencing、UNKNOWN budget、reduce-only、费用一次、typed plan、intrabar | L1/L2 regression |
| V16-06 | 失败 JUnit、通过 JUnit、源码/证据哈希、报告、NOT_RUN/LIVE LOCKED 真实记录；后端全量和前端 tests/typecheck/build | L1/L2 evidence |

## 失败优先与状态规则

先保存四项当前失败测试，再修改同一入口。不得删除或放宽既有断言；只允许补充显式时间、观测和窗口口径 fixture。外部条件缺失记 `NOT_RUN`，本地能力缺失记 `NOT_IMPLEMENTED`，不能互相混淆。mock 只替代行情/新闻/adapter/时钟边界，不替代 Guardian、Gateway、Ledger、projection 或 API 消费者。
