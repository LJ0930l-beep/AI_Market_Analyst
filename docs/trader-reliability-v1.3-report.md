# Trader Reliability v1.3 修复与交付报告

日期：2026-09-08（Asia/Shanghai）  
开发者：Luna Max（单一连续上下文）  
项目：`D:\RJ\codex\ai-market-analyst`

## 结论

本轮已完成三项独立审阅阻塞问题的本地实现修复，并在隔离 SQLite/PAPER/API/runtime 入口上完成回归。交易员辅助与 AI 量化能力已落到现有 v2 架构的 API、运行时、统一 Gateway、账本、Guardian 和前端风险台；没有用静态页面、样例业绩或 mock 结果冒充真实场所证据。

本地后端最终回归为 **283 passed、1 skipped、1 warning**（JUnit 共 284 testcase）；v1.3 专项为 **15 passed**。前端为 **19 个测试文件、85 tests passed**，typecheck 与 production build 通过。真实 Qwen 9B、外部 TESTNET 原生保护/成交/费用、LIVE、干净 Windows 安装均未执行，全部保持 `NOT_RUN`/`LOCKED`。

未执行任何真实或 TESTNET 订单，未读取或修改真实账户、私钥、授权，也没有解锁 LIVE；没有 reset、clean、commit 或 push。

## 问题/能力 → 修改路径 → 生产入口 → 测试 → 证据层级 → 限制

| 问题或能力 | 修改路径 | 生产入口/链路 | 测试与结果 | 证据层级 | 残留限制 |
|---|---|---|---|---|---|
| A：TESTNET 仓位被 PAPER 本地止损关闭 | `core/trading/position_guardian.py`、`core/trading/execution_gateway.py`、`core/trading/ledger.py` | `MonitoringRuntime` 持有的 Guardian → scoped reduce-only Gateway/adapter；只有 `PAPER` 进入本地撮合 | `test_v13_a_guardian_only_paper_can_locally_close`；`test_v13_a_remote_protection_does_not_duplicate_unresolved_exit` | L1 本地隔离 SQLite；真实场所未运行 | TESTNET/LIVE 没有 adapter 时保留原仓位并标 `DEGRADED`，需要另行授权的真实 adapter 对账才能证明远端成交 |
| B：终止 A 后启动 B 丢失 A 保护 | `core/monitoring_runtime.py`、`core/trading/position_guardian.py`、`core/trading/session_manager.py`、`apps/api/v2.py`、`web/src/components/AITraderPanel.tsx` | `POST /v2/ai-session/{start|pause|resume|terminate}` → runtime；runtime stop 终止策略/AI 会话，但有保护责任时保留 Guardian/feed/lease | `test_v13_b_stop_then_rebind_is_blocked_until_account_a_recovers`；旧 runtime terminate 保护回归；API 无 runtime 503 回归 | L2 本地真实 runtime/API 入口 | A 有保护责任时仍需先在 A 上恢复/对账/退出，旧 runtime 不释放唯一保护租约；新 runtime 接管需当前租约成功 |
| C：租约续期失败只 degraded、旧周期仍可能提交 | `core/trading/session_manager.py`、`core/monitoring_runtime.py`、`core/trading/ai_session_coordinator.py`、`core/trading/execution_gateway.py` | 持久 `runtime_leases.fencing_token` → heartbeat → generation invalidation → provider cancel → Gateway side-effect fence | `test_v13_c_stale_fencing_token_cannot_reach_gateway`；`test_v13_c_runtime_lease_loss_cancels_ai_and_pauses_session` | L3 本地双 owner/故障注入，含在途推理 | provider 无法被线程强杀时依靠取消 hook、generation 和 Gateway 边界丢弃晚返回；分布式数据库/交易所网络窗口仍需部署验证 |
| P0 统一风控、预算、行情和费用 | `core/trading/execution_gateway.py`、`core/trading/risk_engine.py`、`core/trading/ledger.py`、`core/trading/authorization.py` | API、固定策略、AI_LED、计划、Guardian 共享 runtime-owned Gateway；打开新风险前要求新鲜可执行行情、费用、滑点、合约大小/步长、单笔/组合/cluster/日亏损和原子 reservation | v1.2 Gateway/risk/authorization 回归；v1.3 组合 UNKNOWN 回归；全后端 JUnit | L1/L2 本地账本与 API | 外部报价/费用/成交事实未验证；兼容低层 Gateway 仍保留给旧测试，但生产 API/runtime 入口加 lease/session fence |
| P0 reduce-only 方向、身份、剩余量、并发幂等 | `core/trading/execution_gateway.py`、`core/trading/ledger.py`、`core/trading/position_guardian.py` | 所有减仓动作必须带 account/venue/mode/position_id（多仓位时不可省略），由 ledger CAS 控制剩余数量 | `test_v13_b_reduce_only_buy_cannot_open_or_reverse`；旧 RT/AT reduce-only、部分成交、并发退出回归 | L1 本地并发 CAS | `legacy_unverified` 旧仓位禁止自动管理，需显式人工迁移/确认 |
| 账户/场所/模式隔离与遗留数据安全 | `core/trading/ledger.py`、`core/v2_store.py`、`core/trading/position_guardian.py`、`core/monitoring_runtime.py`、`apps/api/v2.py` | `GET /v2/accounts/{id}/risk-cockpit`、`/v2/orders`、`/v2/positions`、workspace 均按 account + authoritative mode/venue 筛选 | `test_v13_d_cockpit_and_api_queries_are_account_scoped`；`test_v13_d_legacy_position_is_unknown_and_blocks_runtime_recovery`；两账户同品种回归 | L2 本地 API/账本 | 缺失 owner 的旧记录永不猜归属；accountless 旧订阅表仍是有界迁移限制 |
| 可信账户风险台 | `core/trading/trader_capabilities.py`、`apps/api/v2.py`、`web/src/pages/V2WorkspacePage.tsx`、`web/src/components/AITraderPanel.tsx`、`web/src/v2.css` | `GET /v2/accounts/{account_id}/risk-cockpit` 和 `GET /v2/workspace?account_id=...` | v1.3 account scope/API E2E、前端 Vitest | L2 本地 API → workspace | 无新鲜 mark、对账、模型或 remote evidence 时显示 `UNKNOWN`/`DEGRADED`；不会填充权益或盈利样例 |
| 组合风险与资金竞争解释 | `core/trading/ledger.py`、`core/trading/risk_engine.py`、`core/trading/trader_capabilities.py` | 单笔 capacity、组合 capacity、open position、pending reservation、UNKNOWN order 同一快照；展示方向/品种 concentration，并对 correlation/event/strategy ownership 缺失给出保守政策 | `test_v13_f_portfolio_budget_includes_unknown_orders`；旧 RT14 原子预算/UNKNOWN 回归 | L1 本地 SQLite 原子事务 | 没有权威相关性、事件暴露或策略归属时这些维度保持 `UNKNOWN`，不虚构相关系数；未知暴露可阻断新风险 |
| 有证据的策略有效性研究 | `core/trading/trader_capabilities.py`、`core/analysis/strategy_evaluator.py`、`apps/api/v2.py` | `POST /v2/research/evaluations` → stored predictions/outcomes → 六策略/evaluator；`GET /v2/research/evaluations/{task_id}` | `test_v13_e_research_is_stored_costed_and_no_lookahead`；旧 AT24/25/26/30 回归 | L1 存储数据管道 | 无足够历史返回 `EVIDENCE_INSUFFICIENT`；参数扰动要求真实 replay（没有时为 `NOT_RUN_REPLAY_REQUIRED`）；AI_LED 不继承固定策略成绩 |
| 完整交易计划与正式 WAIT | `core/trading/trader_capabilities.py`、`apps/api/v2.py`、`core/trading/ai_led_engine.py`、`core/trading/execution_gateway.py` | `POST/GET /v2/trade-plans`、`POST /v2/trade-plans/{id}/execute`；计划状态持久化后经 runtime/Gateway/ledger/Guardian | `test_v13_g_trade_plan_runs_through_api_runtime_gateway_and_guardian`；WAIT/减仓/保护旧回归 | L2 本地 PAPER API → runtime → fill → protection → ledger | TESTNET/LIVE 执行明确 `NOT_RUN_EXTERNAL`；时间/事件规则先持久化和展示，外部事件事实仍需数据源 |
| 新闻事件 provenance、修订和未知预期差 | `core/news_revision.py`、`core/trading/trader_capabilities.py`、`apps/api/v2.py`、既有 `core/market_intelligence.py`/前端新闻组件 | 新闻修订 registry → `POST/GET /v2/news/{id}/impacts|research`；原文、发布时间、首次获取、修订、事实/推断/冲突/验证状态分离 | `test_v13_f_wait_news_and_unknown_facts_remain_explicit`；既有 news/revision 回归 | L1 本地来源 fixture | 未验证文本不能独立触发高风险；无法证实 expected gap/absorbed 时保持 `UNKNOWN`；外部文本不是工具或授权指令 |
| AI 成绩单、授权和错误归因 | `core/trading/ai_session_coordinator.py`、`core/trading/ai_led_engine.py`、`core/trading/trader_capabilities.py`、`core/analysis/ai_trade_analytics.py` | coordinator 将 input hash、model digest、prompt version、strategy version、authorization scope、intent/execution receipt 串到 `ai_led_cycles`/scorecard | `test_v13_g_ai_scorecard_links_receipts_but_not_profit_claims`；本地 Qwen-shaped coordinator/两周期持久化回归；旧 AT31-35 | L1 mock-provider trace + L2 本地持久化 | 本地 mock 只证明协议/边界，不证明真实模型收益；confidence 不转化为胜率/盈利保证；model/prompt digest 变更需要新 evidence |
| 动态 TESTNET 能力和证据等级 | `core/trading/testnet_capabilities.py`、`apps/api/v2.py`、`core/manifest.py` | `GET /v2/testnet/capabilities?account_id=...` → account scoped adapter probe；无 adapter 为 `NOT_RUN_NO_ADAPTER`；费用仅 `UNVERIFIED` | `test_rt23_testnet_capabilities_dynamic_probing_and_auth_boundary`；旧 RT24 manifest mapping | L1 状态机与权限边界 | 无外部 adapter/授权，本轮不宣称 native TPSL、成交、费率或 funding 已验证 |
| 前端工作台和真实异步状态 | `web/src/pages/V2WorkspacePage.tsx`、`web/src/components/AITraderPanel.tsx`、`web/src/api/client.ts`、`web/src/i18n.tsx`、`web/src/v2.css` | 已有 workspace 优先风险异常 → 仓位保护 → 新机会 → 归因；AI panel 使用 account 选择与 API lifecycle 状态 | Vitest 19 files/85 tests；typecheck/build | L2 本地 frontend + API contract | 未完成外部请求时保留加载/未知/错误态；没有新增交易所、策略市场、跟单、云多账户或模型竞赛 |

## 三个阻塞问题的关键安全语义

### A：环境隔离

`PositionGuardian.process_bar` 只有 `PAPER` 分支会计算本地成交价并写入本地 economic fill。`TESTNET`/`LIVE` 触发时只构造带 `account_id`、`venue`、`mode`、`position_id`、退出方向和 reduce-only 的 Gateway intent；同一保护退出已有未决 order 时不重复提交。没有客户端、没有确定远端成交、或对账失败时，原仓位和剩余数量保持事实，保护状态为 `DEGRADED`/待对账，价格不被 bar 穿越伪造。

### B：保护责任不可被 session terminate 带走

`MonitoringRuntime.stop` 仍停止发现/策略/AI session，但只要 A 还有 open/protected position、未决 order、pending reservation 或 `legacy_unverified` 责任，就不停止 Guardian/feed、不释放 runtime lease，并拒绝同一 runtime 改绑 B。API 将运行时缺失、账户不一致、恢复责任冲突分别返回可识别的 409/503；前端把失败显示为事实错误，而不是把会话状态改成成功。

### C：租约续期是 fencing，不是状态提示

`runtime_leases.fencing_token` 持久递增；旧 holder 在过期、被接管或本地 invalidation 后不能 renew 或 reacquire 复活。续期失败会停止新发现、pause coordinator、取消 provider generation、递增 SessionManager generation、阻断 Gateway 新风险；Gateway 在 risk/reservation 前和 side-effect 前都重新校验 holder/token。保护 feed 不因唯一保护责任而被顺手关闭，安全接管仍需要当前 owner 的独立 recovery。

## 本地闭环

以下链路已由 API/runtime 入口测试，而不是只手工调用低层类：

`API start/account scope → MonitoringRuntime → fresh market/authorization → OrderIntent → unified ExecutionGateway → PAPER fill → scoped protection → AccountLedger position/fill/fee → Guardian bar exit → attribution/risk cockpit`。

代表性测试是 `test_v13_h_api_runtime_to_gateway_fill_guardian_ledger_attribution` 和 `test_v13_g_trade_plan_runs_through_api_runtime_gateway_and_guardian`。AI coordinator 的本地 provider trace 同样进入持久周期表并验证模型名、prompt/input digest、generation、authorization 和 market snapshot hash；真实 Qwen 只保留严格模型名 `qwen3.5:9b`，不可用时记录明确 unavailable，不静默替换。

## 最终验证命令与结果

| 命令 | 结果 | 机器证据 |
|---|---|---|
| `pytest -q tests/test_trader_reliability_v13.py --junitxml=artifacts/junit-v13-focused-final.xml` | PASS：15 passed、0 skipped、0 failed；含 v1.3 blocker/API/E2E 回归 | `artifacts/junit-v13-focused-final.xml` |
| `pytest -q --junitxml=artifacts/junit-backend-v13-final.xml` | PASS：283 passed、1 skipped、0 failed、1 FastAPI/Starlette httpx deprecation warning；JUnit 284 testcase | `artifacts/junit-backend-v13-final.xml` |
| `cd web; npm test -- --run` | PASS：19 files、85 tests | `artifacts/vitest-v13-final.json` |
| `cd web; npm run typecheck` | PASS：`tsc --noEmit` exit 0 | evidence JSON |
| `cd web; npm run build` | PASS：Vite production build，82 modules transformed | evidence JSON |
| `python -m compileall -q core apps tests` | PASS | evidence JSON |
| `git diff --check` | exit 0；保留既有 `core/providers/gateio_provider.py:144 new blank line at EOF`，其余为 Windows LF/CRLF 提示 | evidence JSON |

JUnit SHA-256（最终文件）：

- `artifacts/junit-v13-focused-final.xml`：`7961D8498179AE1A23EA151692C0375FF7C68E4D50748453BD16893DCC737FBB`
- `artifacts/junit-backend-v13-final.xml`：`ED42C5DF4331D5BEF5785FAA29D91276BE962C46033AF4E4A5AD214A0E837D45`
- `artifacts/vitest-v13-final.json`：`03044CC8D7F5809263582FE9000F41BEEEA91A52DDC1FD3EE66D72B61E2DE4F5`

源码 dirty 哈希、测试分层、矩阵状态、限制和不执行门槛见 `artifacts/trader-reliability-v1.3-evidence.json` 与 `evidence/manifest.json`。最终 manifest SHA-256：`2A1EDFAFCAE75ECA3D967316EB046F029290EBAE8F4F4C9A8EE24B7EE5585D4A`；machine evidence bundle SHA-256：`62996B5E4CE4E8756D9CD98DDF6E7CC2C0AC0A64BA0447274A9358C75DCEF48A`。

## 可用闭环与仅能展示 UNKNOWN 的能力

已具备本地运行闭环：

- PAPER 账户的 scoped equity/cash/fees/positions/orders/reservations、统一 fresh quote 风控、费用滑点和原子预算。
- PAPER 限价/市价模拟撮合、部分成交/止盈/止损、Guardian 与 AI/手工 reduce-only 的 CAS 竞争，以及 account/venue/mode/position identity。
- runtime start/pause/terminate/recovery 的保护责任保留、租约 fencing、在途 AI generation cancel/discard、持久周期和 API 错误状态。
- stored evidence 研究任务、正式 WAIT、交易计划持久化、新闻 revision/provenance、AI receipt/scorecard 和 workspace 风险台。

只能展示 `UNKNOWN`/`NOT_RUN`，不构成事实证据：

- 无新鲜行情或远端对账时的 mark、权益/保护/成交状态。
- 没有 correlation/event/strategy ownership snapshot 时的组合组暴露；代码采用保守限制，不填固定相关系数。
- 历史样本不足、没有 out-of-sample/replay 或成本输入不全时的策略有效性和参数扰动。
- 无 adapter 时 TESTNET native TPSL、原生订单、成交、真实 fee/funding；固定费率只作为明确 `UNVERIFIED` 的计算参照。
- 真实 Qwen 9B 的可用性、两连续真实周期、模型收益/胜率；mock-provider 仅是协议追踪证据。

## NOT_RUN / LOCKED 清单

1. **真实 Qwen 9B**：本轮未探测/下载/付费调用真实模型服务；`qwen3.5:9b` mock trace 不晋级为真实 E2E。
2. **外部 TESTNET**：未连接 adapter、未探测 native protection、未提交/查询订单、未验证成交、费用或 funding。
3. **真实账户/私钥/LIVE**：未读取、修改或发送订单；LIVE 继续 `LOCKED`。
4. **干净 Windows 安装/RT25**：当前 checkout 有既有 dirty/untracked 用户成果，不能代表全新安装；该验收为 `NOT_RUN`。

## 迁移与回滚

- 迁移是 additive/idempotent：lease fencing、AI cycle provenance、trade plan、research task、news impact 和 protection 字段均有安全默认值，不猜旧数据归属。
- 旧仓位/订单缺失可证明的 account/venue/mode/position identity 时标为 `legacy_unverified`，从自动管理和可用预算中排除并阻断新风险；恢复需显式人工确认/迁移，不把旧事实改写成当前账户。
- 回滚流程：停止本地 runtime；保留已有迁移备份；按正常版本审查回退本轮代码/证据；用 schema/integrity 检查重新打开备份。禁止用 reset/clean 删除其他 dirty work。
- 本轮没有自动 commit/push，也没有覆盖或回退用户既有修改；旧 v1.2 报告保留为历史记录，未改写成 v1.3 通过证据。

## 交付文件

- 实施方案与验收矩阵：`docs/trader-reliability-v1.3-plan.md`
- 本报告：`docs/trader-reliability-v1.3-report.md`
- 机器可读证据：`artifacts/trader-reliability-v1.3-evidence.json`
- 现有 manifest 的 v1.3 增量绑定：`evidence/manifest.json`
- 最终 JUnit/Vitest：`artifacts/junit-backend-v13-final.xml`、`artifacts/junit-v13-focused-final.xml`、`artifacts/vitest-v13-final.json`

本报告交给后续主管/独立审阅；它是本地实现与验证报告，不是外部交易或产品全面验收声明。
