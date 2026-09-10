# Trader Reliability v1.4 修复报告

日期：2026-09-09（Asia/Shanghai）  
开发者：Luna Max，单一连续上下文  
项目：`D:\RJ\codex\ai-market-analyst`  
合同：`docs/v1.4-audit-repair-contract.md`

## 结论

v1.4 合同 D01-D04 已在现有 dirty/untracked 工作区中实现，并完成本地隔离验证。最初保存的 D01 失败测试仍保留；同一测试已通过。当前后端全回归为 **303 passed、1 skipped、1 warning**，v14 合同专项为 **20 passed**，v14/v13/v12 聚焦回归为 **50 passed**；前端全套 **40 suites / 87 tests passed**，关键交易员 UI 副本 **4 suites / 5 tests passed**，typecheck 和 production build 均通过。

这不是外部交易或模型验收：本轮没有读取凭据、没有真实或 TESTNET 下单、没有解锁 LIVE、没有下载模型、没有改变真实账户/授权，也没有 reset、clean、commit 或 push。真实 Qwen 9B、外部 TESTNET 原生保护/成交/费用和干净 Windows 安装均按合同记录为 `NOT_RUN`，而不是 PASS。

## 失败→修复→通过证据

| 问题 | 修改路径 | 生产入口与效果 | 失败/通过证据 | 层级与限制 |
|---|---|---|---|---|
| D01：`reduce_fraction=.25` 被丢失并全平，`leverage` 被丢失 | `core/trading/trade_plan_contract.py`、`core/trading/trader_capabilities.py`、`core/trading/execution_gateway.py`、`core/trading/ledger.py`、`apps/api/v2.py` | `POST /v2/trade-plans` → canonical schema/storage → `POST /v2/trade-plans/{id}/execute` → Gateway → ledger CAS；`.25` 成交 `.25`、余 `.75`，CLOSE 才允许全平，杠杆往返保留 | 初始 `artifacts/junit-v14-d01-failing.xml`：`KeyError: reduce_fraction`；同一测试 `artifacts/junit-v14-d01-passing.xml`：1 passed；V14-01..03 | L1/L2 隔离 SQLite/PAPER；远端经济事实仍需 adapter 对账 |
| D02：条件字段只存不执行 | `core/trading/trade_plan_contract.py`、`core/trading/trader_capabilities.py`、`core/monitoring_runtime.py`、`core/trading/position_guardian.py`、`core/trading/execution_gateway.py` | 版本化 `condition_spec` 支持 fresh mark、价格方向、最大追价、过期和事件状态；runtime 只在闭合行情评估；不满足为 `WAITING_TRIGGER`/`EXPIRED`/`INVALIDATED`/`BLOCKED_DATA`；成交后 time exit、分批、追踪、事件更正进入 Guardian 合同 | `test_v14_d02_runtime_consumes_typed_trigger_and_blocks_untyped_prose`、`test_v14_d02_time_exit_partial_take_profit_and_trailing_survive_runtime_stop`、API/runtime E2E JUnit | L2 本地 runtime/API → Gateway → Guardian；未实现自然语言/未知事件不会激活 |
| D03：历史切分被误标无前视，扰动没有重放 | `core/trading/trader_capabilities.py`、`core/analysis/strategy_evaluator.py`、现有 `STRATEGIES` | `/v2/research/evaluations` 读取已存闭合 bars/predictions/outcomes，执行真实策略 replay、warmup、冻结 OOS、rolling、purge/embargo、同 K 线保守成交、成本压力和参数扰动；任意 trade 数组不再是证据 | `test_v14_d03_replay_and_parameter_perturbation_are_real`、`test_v14_d03_missing_bars_do_not_claim_no_lookahead`；旧 AT24/25/26/30 回归 | L1 存储回放；无足够 bars 为 `EVIDENCE_INSUFFICIENT`/`NOT_RUN_REPLAY_REQUIRED` |
| D04：调用者自填 expected gap/absorbed 升级高风险权限 | `core/news_revision.py`、`core/trading/trader_capabilities.py`、`apps/api/v2.py`、新闻前端 | 原文、来源、发布时间、首次获取、revision、事实和推断分开；只有共识/原始观测/价格对齐的有时间证据才形成推断；更正既阻断新计划又被 Guardian 消费；新闻永远不提高 RiskEngine 限额 | `test_v14_d04_news_inference_requires_independent_evidence`、`test_v14_d04_news_correction_reaches_existing_guardian`；旧新闻回归 | L1 本地 registry/修订链；外部来源真实性与消化程度未做外部验收 |
| v1.3 A：TESTNET 被 PAPER 本地撮合 | `core/trading/position_guardian.py`、`core/trading/execution_gateway.py`、`core/trading/ledger.py` | PAPER 才能本地经济成交；TESTNET/LIVE bar 触发只提交受保护 reduce-only 或返回 DEGRADED，待真实成交回报后落账 | `test_v13_a_guardian_only_paper_can_locally_close`、`test_v13_a_remote_protection_does_not_duplicate_unresolved_exit` | L1/L2；无 adapter 保留仓位，不伪造价格/PnL |
| v1.3 B：终止 A 后 B 丢失保护 | `core/monitoring_runtime.py`、`core/trading/session_manager.py`、`apps/api/v2.py`、`AITraderPanel.tsx` | A 仍有保护责任时 runtime 拒绝重绑 B；stop 终止策略/AI 发现但保留 Guardian/feed/lease；无 runtime 的 session API 返回 503 | `test_v13_b_stop_then_rebind_is_blocked_until_account_a_recovers` 及 API/Guardian 回归 | L2；需要在 A 上恢复/对账/退出后才能接管 |
| v1.3 C：租约失效仍可提交旧周期 | `core/trading/session_manager.py`、`core/monitoring_runtime.py`、`core/trading/ai_session_coordinator.py`、Gateway | 持久递增 fencing token、提交前 lease/session 校验、generation 取消和旧结果丢弃；保护任务不因新风险租约失败而关闭 | `test_v13_c_stale_fencing_token_cannot_reach_gateway`、`test_v13_c_runtime_lease_loss_cancels_ai_and_pauses_session` | L3 本地双 owner/故障注入；分布式/交易所网络窗口仍需部署验证 |

## 行为字段流转矩阵

| 字段/事实 | API 输入 | 持久化/读回 | 消费者 | 可观察效果 | UI/测试 |
|---|---|---|---|---|---|
| `reduce_fraction` / `reduce_quantity` | `TradePlanBody`，禁止 null/非有限/越界 | `trader_trade_plans.payload_json` 的 `trade_plan_v1.4` | plan executor → scoped Gateway → ledger CAS | 按步长向下取整；缺失为 `NEEDS_RECONFIRMATION`；不默认全仓 | `AITraderPanel` 计划卡；V14-01/02 |
| `leverage` | `TradePlanBody`/AI output | canonical plan、intent、cycle/receipt | RiskEngine/Gateway | 保留用户值但不得扩大硬限额；缺失为 UNKNOWN/安全默认语义 | 计划/receipt；V14-01/03、Gate dry-run |
| `entry_trigger` + `condition_spec` | 说明文字与 typed spec 分离 | `conditions`、condition schema/version/hash | runtime closed-bar scheduler | 未触发不建单不占永久预算；unsupported 不激活 | 条件状态/原因；V14-04 |
| `abandon_chase_condition` | `MAX_CHASE_BPS` 等 typed 条件 | canonical `conditions.abandon_chase` | condition evaluator before Gateway | 超过追价界限保持 WAITING，不追单 | evidence/status；V14-04 |
| `entry_expires_at` | API timestamp | plan/condition contract | runtime/evaluator | 入场到期 `EXPIRED`；与持仓 `time_exit_at` 分开 | plan status；V14-04/08 |
| `time_exit_at` | API timestamp | position protection contract | Guardian | 不需要新 AI 结果，暂停/终止后仍 reduce-only 退出 | position card；V14-05 |
| `partial_take_profits` | price + fraction list | protection contract、exit event identity | Guardian + ledger CAS | 按原始/剩余数量执行，25%/25%/余量不超卖 | exit/protection history；V14-06 |
| `trailing_protection` | typed distance/percent | protection contract | Guardian | 只向有利方向收紧，乱序/重复不回退 | current stop/status；V14-06 |
| `event_invalidation` / `news_revision_ids` | revision/event facts | plan + position contract | revision gate + Guardian | 更正阻断新风险并进入确定保护路径；未知证据为 BLOCKED_DATA | news evidence badge；V14-07 |
| `evidence` | source/as-of/revision refs | plan/news/research provenance | plan gate、scorecard、UI | 可追溯但不授予资金权限 | evidence panel；V14-03/07 |
| market quote/bar | provider/runtime snapshot | `market_realtime_state`/`market_bars` | evaluator、RiskEngine、Gateway、replay | explicit fresh executable quote；缺失为 UNKNOWN/BLOCKED | freshness banner；V14-04/09 |

## 已具备的运行闭环与生产入口

1. 账户选择和模式/venue scope → `POST /v2/monitoring/sessions/start` → durable runtime lease/fence。
2. `POST /v2/trade-plans` 或 runtime closed-bar consumer → typed condition/evidence/news gate → authorization/session/risk checks。
3. `ExecutionGateway.submit_intent` 是 manual、fixed strategy、AI_LED、plan、Guardian 的共同入口；RiskEngine 原子预占预算，UNKNOWN 不释放，PAPER 才进入本地撮合。
4. 成交/挂单/未决状态 → scoped `AccountLedger` → protection contract → PositionGuardian → fee/PnL/reconciliation evidence → `/v2/accounts/{id}/risk-cockpit` 与 Workspace。
5. `POST /v2/research/evaluations` → 可重跑的已有策略闭合 bar replay；`/v2/news/{id}/impacts`/`research` → revision provenance；AI cycle 持久化 model digest、prompt、input、authorization、intent、receipt。
6. `AITraderPanel` 与 `V2WorkspacePage` 只通过统一 `web/src/api/client.ts` 读写，显示真实异步状态、保护身份、UNKNOWN/DEGRADED、模型不可用和授权/应用退出影响；暂停不停止已有保护。

## V14 验收矩阵

| 验收 | 本地结果 | 主要证据 |
|---|---|---|
| V14-01 | PASS_LOCAL | v14 D01 双向减仓、API/storage/read-back/fill/remainder |
| V14-02 | PASS_LOCAL | 缺失、0、负数、>1、NaN/Inf、旧计划、CLOSE |
| V14-03 | PASS_LOCAL | leverage、typed fields、unknown field、multi-position identity |
| V14-04 | PASS_LOCAL | trigger/chase/expiry runtime scheduling |
| V14-05 | PASS_LOCAL | stopped runtime 后 time exit / protection |
| V14-06 | PASS_LOCAL | staged exits、trailing、manual reduction、CAS、duplicate/out-of-order |
| V14-07 | PASS_LOCAL | missing inference、immutable revision、correction → Guardian |
| V14-08 | PASS_LOCAL | restart/armed plan/protection recovery/idempotent intent |
| V14-09 | PASS_LOCAL | existing strategy closed-bar production replay |
| V14-10 | PASS_LOCAL | actual parameter perturbation、purge/embargo、cost stress |
| V14-11 | PASS_LOCAL | deterministic rerun、insufficient-data boundary；旧 split 标 `RETROSPECTIVE_SPLIT` |
| V14-12 | PASS_LOCAL_API_RUNTIME_E2E + PASS_LOCAL_UI_BOUNDARY | `artifacts/junit-v14-api-runtime-e2e.xml`；key UI 4 suites/5 tests。UI mock 仅替代 API 边界，不替代风控/账本/Gateway |
| V14-13 | PASS_LOCAL_REGRESSION | v1.2/v1.3 focused + backend/frontend full |

V14-09..11 的 `no_lookahead` 兼容字段不再被单独当作证明；只有冻结输入、闭合 bars、future invariance 和 purge/embargo 的 replay 结果才提升证据等级。

## 测试、证据和哈希

| 命令/文件 | 结果 | SHA-256 |
|---|---:|---|
| `python -m pytest -q tests/test_trader_reliability_v14.py --junitxml=artifacts/junit-v14-contract-final.xml` | 20 passed | `E6CDF8FBCA5ED0A9056491EADFA6B6539B02EB55671C7FEB76F7E462BBCF28F6` |
| `python -m pytest -q tests/test_trader_reliability_v14.py tests/test_trader_reliability_v13.py tests/test_repair_v12_luna.py --junitxml=artifacts/junit-v14-focused-final.xml` | 50 passed | `2A864269568CAE4BC44DB9DA034EB6A9F6B389889E878C035E25436ABE2E167A` |
| `python -m pytest -q --junitxml=artifacts/junit-backend-v14-final.xml` | 303 passed, 1 skipped | `98722D56ABB99223BB65404B69D1038A527107992D9012E208E18297779576C4` |
| `artifacts/junit-v14-api-runtime-e2e.xml` | 3 passed | `8EE29579C858B00AED15AEFD6976B0458F221124ED13EB626CBDB7F7F77115DE` |
| `npm test -- --run --reporter=json --outputFile=../artifacts/vitest-v14-final.json` | 40 suites / 87 passed | `8FD7181084C76262A9B6798D17854F6AAC10973E5AA24522B8F2A18A3FD146B9` |
| `npx vitest run src/components/AITraderPanel.test.tsx src/pages/V2WorkspacePage.test.tsx ...` | 4 suites / 5 passed | `BFC2CDEAA288DDE67DBE3EF144163C666C872DB65EE2AF54BF2A84CFE79BE6FC` |
| `npm run typecheck` | exit 0 | build input |
| `npm run build` | 82 modules, exit 0 | build output |
| `python -m compileall -q core apps tests` | exit 0 | static check |

The first-failure artifact is `artifacts/junit-v14-d01-failing.xml` (`5DF4D0EE3053A62B1771C990E5AE8C381BA8DF4BCD21E4AC9CCCEA3B303FE28B`); the repaired same-test artifact is `artifacts/junit-v14-d01-passing.xml` (`FDF4C96AB06D837F58A3E28E026E8088486E678218AF222511C897AAACD22114`).

Independent temporary mutation checks also caught both important classes: deleting `reduce_fraction` from the service result produced `MUTATION_CAUGHT KeyError 'reduce_fraction'`; making the condition evaluator unconditional produced `MUTATION_CAUGHT AssertionError`. These ran in a separate Python process and did not modify the checkout.

`evidence/manifest.json` is generated from the final dirty source set with JUnit report metadata and critical source/test SHA-256 entries. Its final SHA-256 is `A129860FB325B78D87F99EDCC00D3BF97E483423295C3163DD8CE3565D230BBF`. The companion machine-readable v1.4 record is `artifacts/trader-reliability-v1.4-evidence.json`, final SHA-256 `A8FE2F8A50255DD0D6FD51FCAF3F087B3C735DDED04F4A8A9B80B4B210DE5A25`; its artifact hashes are the values above. The manifest's own hash is not embedded in itself, avoiding a self-hash cycle.

## 旧回归与测试变更清单

- 没有删除、skip、降低既有业务断言。
- 为新鲜行情合同，仅给旧 AT37 remote protective fixture 增加了明确的 `fresh: true`；这使旧测试输入满足现在的显式 freshness contract，不改变期望结果。
- 为兼容旧 AI cycle 直接调用，`AILedDecisionEngine` 仅在 cycle 自有时间边界内为未标记的 PAPER snapshot 补齐 application-owned freshness 标记，Gateway 仍作最终新鲜度校验；生产 coordinator 使用 provider snapshot 和市场规则。
- 既有 v12/v13/v14 fresh-market fixture 的 `slippage`/fresh market metadata 是明确测试输入，不是放宽断言；remote opening 缺规则仍不会调用 adapter。
- `git diff --check` 仅保留既有 `core/providers/gateio_provider.py:144` EOF 空行警告和 LF/CRLF normalization notices，未改动该用户文件。

## 数据迁移、回滚和并发边界

SQLite 迁移是 additive/idempotent：增加 account/venue/mode/position/protection/fill/economic evidence 字段和索引；旧无法证明归属的记录标为 `legacy_unverified`，不会被 Guardian、预算或自动恢复猜测管理。新增计划 schema 带版本/hash，不原地解释旧 REDUCE 为全平。

回滚前必须停止受管 runtime/sidecar、确认没有 WAL/SHM 和在途远端对账，先用现有备份/restore 流程恢复到新文件名并重新 `SQLiteStore.initialize()`、完整性检查和 scope 对账，再切换；不要在当前工作区使用 git reset/clean，也不要删除用户 dirty/untracked 成果。若回退代码，旧 v1.4 plan/protection rows 应先标为 review/unknown，禁止旧代码直接执行未知 reduction 或远端经济事实。

fencing token 单调递增，旧 holder 即使恢复也不能续租或提交；Gateway 在副作用前重查 account/mode/venue/session/lease/token。Guardian 接管只允许绑定同一保护责任，不能用启动 B 或释放 A 来“解决”冲突。

## 产品能力状态与实际限制

已闭环的是本地 PAPER 计划执行、账户隔离、条件状态机、持仓退出合同、六策略之一及其真实有界 replay、参数扰动、成本压力、新闻 revision gate、AI cycle provenance、风险 cockpit 和中英文异步 UI。所有权益、风险、保护和费用优先来自同一 scoped ledger snapshot；缺 mark、fee、contract、correlation、event exposure 或 strategy ownership 时显示 `UNKNOWN`，采用保守限制，不填图表或样例数字。

仍然明确受限：

- 相关性、同事件暴露和多策略资金归属没有权威输入时只能显示 UNKNOWN/保守组限制，不声称有真实相关系数。
- `strategy_subscriptions` 是历史 accountless 全局策略偏好表；安全写入口在无法证明账户范围时拒绝全局变更，并把它与账户执行事实分离，不能当作账户持仓/权益事实。
- 外部 remote fill 缺 fee/contract 时保留成交状态但经济 reconciliation 为 `UNVERIFIED`，兼容账本数值不被当作真实费用证明。
- 模型 confidence 不是胜率、收益保证或授权；模型/提示词/策略版本变化会形成新 provenance，不能静默继承旧成绩或权限。
- 未验证新闻不能独立触发高风险交易；自然语言条件、未知事件事实、缺失历史 bar 和过期行情都 fail closed。

## 外部验收门槛

| 项目 | 状态 | 原因 |
|---|---|---|
| 真实 Qwen 9B E2E | `NOT_RUN` | 本轮不下载/不调用外部或本地付费模型；provider-shaped boundary 只证明取消、超时、持久周期和权限链路 |
| 外部 TESTNET native TPSL、远端成交、真实费用 | `NOT_RUN` | 没有独立授权、客户端或私有凭据；本地 adapter boundary 不升级为真实证据 |
| LIVE | `LOCKED` | 未解锁、未使用账户或私钥 |
| 干净 Windows 安装/RT25 | `NOT_RUN` | 当前 checkout 按要求保留 dirty/untracked 用户成果，未伪造安装验收 |

这些限制不会把已完成的本地实现标作外部通过，也不会把缺失数据掩盖成产品成功。
