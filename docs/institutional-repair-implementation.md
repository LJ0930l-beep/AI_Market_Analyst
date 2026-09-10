# AI Market Analyst 机构量化审计修复实施记录

> **当前 main 权威复核（2026-09-10）**：本节及其后的“当前复核”内容优先于本文保留的历史快照。源码实现提交为 `c719d2d`（完整 SHA 见 Git 历史），已推送到 `origin/main`；仓库版本为 `2.0.0`（`pyproject.toml`、`core/config.py`、`web/package.json`、`src-tauri/tauri.conf.json` 一致）。V1.7 只做了版本核对，没有改版本元数据。

## 当前 main 的实施与验证结论

- 本轮保留工作区中已有的用户改动；没有 `reset`、`checkout`、`clean`，也没有运行 `scripts/direct_install.ps1`。按用户后续明确要求，当前交付清单已随实现提交 `c719d2d` 推送到 `origin/main`，未覆盖未知文件。
- Gate 账户底座已收口为 `gate_testnet` 唯一托管 TestNet 账户；`gate_paper` 仅作为输入兼容别名，不能再创建本地假成交账户。`gate_live` 独立保留，默认 `LOCKED`。远端权益、保证金、持仓、挂单与成交读取经 `GateAccountTruthService` 持久化为审计快照；不创建或更新 Gate 的本地 `simulated_positions`，历史旧行仅保留追溯，不能反向冒充 Gate 事实。
- 已接通账户作用域的凭证验证、只读连接测试、Gate TestNet 远端 adapter、订单回执/撤单/保护与独立 TestNet E2E 验收服务；E2E 必须显式确认、使用幂等键，且不复用 AI 授权或模型调用。本轮已使用用户提供的 TestNet 凭证完成一次真实 `ETHUSDT` 1 张开仓、保护单确认、reduce-only 平仓并确认远端仓位归零；未访问 Live、未触碰既有 BTC 仓位。
- AI 链路保存系统阻断与模型 WAIT 的不同来源、阶段流水、授权/租约/账户快照、真实模型响应、延迟、量化与 digest。发现本机 Ollama/Qwen3.5 的复杂 Schema grammar 兼容问题后，新增了“仅服务端明确拒绝 grammar 才降级为 JSON mode + 本地严格业务校验”的窄兼容路径；普通模型/网络错误仍失败关闭并保留原因。
- 页面新增远端账户事实卡、策略候选/阶段证据、按账户 Gate 凭证只读连接测试，以及带 `role=region`、键盘焦点和内部滚动的宏观日历框，长栏不再把整页无限推低。
- `gate-live` 凭证表单在旧数据或新安装尚未登记账户资料时，会先幂等登记默认双账户，再只调用 `/v2/gate/accounts/{account_id}/credentials/verify`；已移除错误的全局 `/gate/config` 写入回退。该路径由“缺失 profile 自动登记后仍走 scoped verify”的前端回归覆盖。

### 真实 Gate TestNet 开平回归（2026-09-10）

在隔离临时 SQLite 和独立幂等键下，选取远端没有已有仓位的 `ETHUSDT`，按 Gate 合约张数自动取最小可交易数量 `1`：

| 阶段 | 结果 | 事实边界 |
|---|---|---|
| 账户凭证/余额读取 | `VERIFIED_READ_ONLY` / `AVAILABLE` | 只使用 Gate 官方 TestNet 私有 API；凭证未写入代码、文档或日志 |
| 多单开仓 | `FILLED`，成交 `1` 张 | 真实 TestNet 订单；未写本地 `simulated_positions` |
| 止盈/止损保护 | `PROTECTED` | Gate 远端保护单已被接受并纳入回执复核 |
| reduce-only 平仓 | `FILLED`，成交 `1` 张 | 修复了清理单传输字段长度和错误码映射问题 |
| 平仓后对账 | 远端 `ETHUSDT` 仓位 `0`、挂单 `0` | 本次临时仓位已清理；账户中既有 BTC 仓位未触碰 |

该结果是 TestNet 真实私有 API/订单证据，不等于 Live 可用或策略收益证明；Live 仍保持发布锁定。

### 本轮实测结果

| 检查 | 实测结果 | 证据边界 |
|---|---|---|
| 后端完整 pytest | `374 passed, 1 skipped, 1 warning`，268.94s | warning 为 Starlette/httpx 弃用提示 |
| 定向 Gate/AI/证据/Ollama 集 | `22 passed, 1 warning` | 隔离 SQLite、确定性 adapter；不等于私有账户可用 |
| `python -m compileall -q apps core scripts tests` | PASS | 当前 Python 源码可编译 |
| `python scripts/institutional_acceptance.py` | `4/4 PASS` | disposable SQLite；不是公开 HTTP/Qwen/交易证明 |
| 前端 | `22 files / 99 tests`、typecheck PASS、lint PASS、Vite build PASS（86 modules） | 当前 React 源码与页面回归，含缺失 Gate profile 自动登记/账户范围验证回归 |
| Tauri Rust | `cargo check --manifest-path src-tauri/Cargo.toml` PASS | 只代表 Rust 检查通过 |
| sidecar 构建 | `scripts/build-tauri.ps1` PASS，PyInstaller 6.21.0 生成新 sidecar，并随当前 NSIS 包安装 | 安装后只读健康核验通过 |
| Tauri NSIS | PASS：使用 MSVC 目标生成当前 `2.0.0` 用户级安装器并成功安装 | 安装后只读健康核验为 `200/ready`；未做业务库迁移、私有账户访问或下单 |
| Gate 公共只读 HTTP | TestNet `/api/v4/futures/usdt/contracts`=`200`、63；Live=`200`、977；TestNet 样本字段完整 | 公共接口，不含私有鉴权、余额、订单或成交 |
| Ollama/Qwen | `qwen3.5:9b` health 可用，digest=`6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`；真实 JSON smoke 返回 `WAIT`，schema 标记 `json_mode_local_validation` | 仅无账户无交易 smoke；未运行前瞻影子账户、固定评测集或交易效果评估 |
| 当前安装版 | current-user 安装成功，版本 `2.0.0`、桌面快捷方式和 sidecar 哈希已核对；`/health=200/ready`、`/hydration/status=ready` | 只读核验；没有改用户配置、业务库、私有账户或交易状态 |

### R01–R11 当前对应矩阵

| 合同 | 当前实现路径 | 回归/验收 ID | 当前结果与外部依赖 |
|---|---|---|---|
| R01 数据身份/迁移 | `core/storage/sqlite.py`、`core/instruments.py`、`core/trading/account_aliases.py`、Gate account scope | `test_latest_bars_limit_one_returns_newest_and_cross_venue_identity_is_kept`、`test_gate_default_accounts_are_distinct_and_api_provision_is_idempotent` | `PASS_ISOLATED`；生产活动库迁移未执行 |
| R02 时间语义/查询 | `core/storage/sqlite.py` latest/range/PIT、closed-bar 传递 | `AT-DATA-IDENTITY-LATEST`、`test_v14_d03_closed_bar_replay_and_real_parameter_perturbation` | `PASS_ISOLATED`；真实历史可知性仍按数据源标记 |
| R03 六策略合同 | `core/quant/strategies`、`core/trading/candidate_scanner.py`、策略监控调用方 | full pytest、`test_v14_d02_api_round_trip_preserves_typed_conditions_and_protection_contract` | `PASS_FIXTURE`；未声称参数已优化或真实止损/资金费可见 |
| R04 新闻/宏观事实 | `core/events.py`、`core/news_revision.py`、`core/macro_calendar.py`、来源 registry | `test_source_registry_does_not_trust_path_or_source_text`、`test_news_negation_changes_direction` | `PARTIAL_EXTERNAL`；本轮未用私有/付费新闻源补数，缺失值保持未知 |
| R05 研究回放 | `core/analysis/strategy_replay.py`、`core/trading/trader_capabilities.py` | `test_v14_d03_closed_bar_replay_and_real_parameter_perturbation`、AT24–AT30 | `PASS_SCOPED`；不足数据不会伪装为零交易 EVALUATED |
| R06 指标/反事实 | `core/trading/ledger.py`、`core/analysis/ai_trade_analytics.py`、`core/trading/institutional_risk.py` | partial-exit、SHORT MAE/MFE、费用压力与 outbox 回归 | `PASS_ISOLATED`；真实成本/深度/资金费仍按可用性标记 |
| R07 统一账本/页面 | `core/trading/ai_led_engine.py`、`apps/api/v2.py`、`web/src/components/AITraderPanel.tsx`、`V2WorkspacePage.tsx` | `test_ai_system_block_is_not_persisted_as_model_wait`、98 frontend tests | `PASS_ISOLATED_UI`；GET 无副作用与账户作用域已覆盖 |
| R08 数据中心/研究资格 | `apps/api/v3.py`、`apps/api/v2.py`、`core/storage/sqlite.py` | `test_research_cancel_uses_authoritative_scope_and_does_not_500`、full pytest | `PASS_ISOLATED`；DSR/PBO 仍需足量真实试验输入 |
| R09 组合/执行风险 | `core/trading/execution_gateway.py`、`position_guardian.py`、`gate_account_truth.py`、`gate_testnet_e2e.py` | `test_gate_remote_truth_required_for_new_risk_but_not_reduce_only_boundary`、`test_gate_gateway_open_and_reduce_only_use_remote_position_without_local_mirror`、真实 TestNet E2E、RT01–RT16 | `PASS_ISOLATED_WITH_REAL_TESTNET_CYCLE`；Live 私有 API 未访问 |
| R10 AI 证据/治理 | `core/evidence.py`、`core/ai/ollama.py`、`core/trading/ai_session_coordinator.py`、`ai_cycle_trace.py` | `test_model_digest_is_selected_for_requested_smart_model`、Ollama provider contract tests、真实 Qwen smoke | `PASS_LOCAL_WITH_REAL_SMOKE`；无影子账户/评测集，不宣称交易效果 |
| R11 运维/安全 | `core/trading/institutional_schema.py`、`core/security/local_guard.py`、outbox、Tauri build scripts | full pytest、compileall、ruff F821、cargo check、MSVC NSIS、current-user install、`git diff --check` | `PASS_LOCAL_WITH_MSVC_BUILD`；本轮真实 TestNet 仅做一次隔离开平验收，生产运维未运行，LIVE 仍锁定 |

详细旧运行快照、历史基线和 Sol 独立签收要求继续保留在下文，但不得覆盖本节的当前结论。

> **以下为历史快照**：从“交付边界”开始的旧执行记录用于追溯，可能包含先前运行、安装或测试环境的结果；若与本文开头“当前 main 的实施与验证结论”冲突，以当前 main 复核和 `evidence/institutional-repair-result.json` 的 `current_main_revalidation` 为准。

## 交付边界

- 项目：`D:/RJ/codex/ai-market-analyst`
- 里程碑：F01–F15 与报告第 12–16 章功能包 A–D 第一版
- 执行身份：Luna Max；状态为 `DEVELOPER_COMPLETE_PENDING_SOL_ACCEPTANCE`
- 基线：`32596a3f1431a6646e3c5cb853a213fcc69a067c`，工作树原本已 dirty；本轮没有 reset、checkout、clean、commit、push，也没有在用户确认退出前触碰安装版。确认退出后仅用当前 2.0.0 NSIS 包更新了已核对的用户级安装目录和桌面快捷方式，没有执行业务库迁移、真实授权或订单操作。
- 版本核对：当前 checkout 的 pyproject、web package、Tauri 配置均为 `2.0.0`，HEAD 对应 `v2.0.0`。用户提到的 V1.7 仅作为“是否最新”的参考核对，本轮没有把项目改成 V1.7，也没有改版本元数据。

本记录严格区分“隔离确定性 fixture 通过”和“真实公开 HTTP、真实 Ollama/Qwen、Testnet 或实盘证据”。本轮额外对 Gate v4 公开合约 GET 做了只读网络抽测；没有凭证或外部服务的其他子能力返回 `NOT_CONFIGURED`、`UNKNOWN` 或 `PARTIAL`，不以状态标签代替事实。

## 独立审验与修复后复验（2026-09-09）

独立审验先在临时 SQLite 中发现两个未被原有测试覆盖的运行时错误，随后已在本里程碑内修复并补回归：

- `apps/api/v3.py:569` 的研究取消处理已先解析 account scope；`POST /v3/research/runs/{run_id}/cancel` 在临时库中由原先的 `500 Internal Server Error` 修复为正常状态转换。
- `core/trading/ledger.py:1613,1628` 的已有 `position_id` 加仓路径已使用本地保护状态常量，避免账本层循环依赖；同一账户连续记录第二笔加仓成交已不再抛出 `NameError`。

修复前的失败输出仍作为历史审验事实保留在 evidence JSON；修复后的 `AUDIT-001`、`AUDIT-002` 均为 `PASS_AFTER_REPAIR`。相关隔离回归、Gate 账户链路回归与完整 pytest 均已重跑；本文件不替 Sol 做最终签收。

## 运行态审验与页面修复（2026-09-10）

### 审验结论

对当前用户级安装版做了只读 API/进程/数据库审验：版本仍为 `2.0.0`，`/health` 为 ready、local-only，公开行情流显示 connected；首次快照监控处于 `backoff`（`run_count=18`、连续失败 18 次、三个 `ValueError`），随后最新快照已转为 `degraded`（`worker_alive=false`、`run_count=32`、连续失败 32 次、`execution_blocked=true`），最后错误为 `Runtime lease renewal failed; new risk is fenced and in-flight discovery was cancelled while protection remains active`。AI 会话为 `STOPPED`，底层 session 为 `IDLE`；按响应明细读取，`gate_paper` 作用域下交易计划、订单、持仓、成交均为 0，因此不能判定为正在做单。早先运行库检查发现 SQLite journal 和外部已提交视图/进程 API 订阅视图不一致，后续只读文件检查时 journal 已不存在；本轮没有碰该库，不删除 journal，也不做生产迁移。

### 根因、修复与回归

在临时 SQLite 和真实 Gate 公开行情（只读 GET）组合的隔离复现中，`StrategyMonitoringService` 没有把 Gate 原生合约 `BTC_USDT` 生成的 canonical `instrument_key` 传入存储层；随后存储层又用未经规范化的字符串比较拒绝了 `BTCUSDT` 请求与 `BTC_USDT` 原生标识之间合法的 provider 分隔符差异。修复分成两层：

- `core/strategy_monitoring.py` 现在保存完整的 `gate:perpetual:BTC_USDT:USDT:last` 身份，原生符号不被改写；
- `core/storage/sqlite.py` 仅在请求符号到身份符号的校验中压缩字母数字分隔符，仍把完整原生身份写入数据库，避免跨市场/跨 venue 覆盖。

新增 `test_strategy_monitoring_persists_gate_native_identity_without_degrading`：隔离 Gate fixture 通过；真实公开 Gate 三标的临时库周期返回 `COMPLETED`，结果为 BTC `UNSUPPORTED`（显式 crypto ORB policy）、ETH/SOL `NO_TRIGGER`，不再出现 degraded `ValueError`。这些结果证明当前接口/存储链路的身份合同，不等于私有凭证、Qwen 或实盘成交证明。

### 页面优化

`web/src/pages/V2WorkspacePage.tsx` 将宏观日历列表标成可聚焦的 `role=region`，`web/src/v2.css` 为其增加响应式 `max-height`、内部 `overflow-y` 滚动、边框背景、滚动条留槽、overscroll 隔离和 `:focus-visible` 样式；长列表因此限制在信息框内，不再把整页无限向下推。组件回归覆盖区域语义、键盘焦点和事件可见性。

本轮只更新源码、测试和交付证据，没有重启当前安装版、没有改业务数据库或用户配置；在用户明确授权的 Gate TestNet 模拟账户上完成了一次隔离真实开平，未访问 Live。源代码修复须在下一次构建包中激活。

## A. 事实、策略与量化计算修复

### F01–F04：可执行策略合同

`core/quant/strategies.py` 增加 `StrategySpec`、`ParameterSpec`、参数 canonicalization/hash 和六策略注册。统一策略输入为 5 分钟信号数据，同时显式区分 15 分钟结构与 1 小时环境上下文；缺少必需上下文返回 `UNSUPPORTED`/缺口，而不是伪造 `NO_TRIGGER`。

- F01：15m 行情不再直接冒充 5m 信号；策略运行时按 strategy spec 的周期分组并传递闭合 bar。
- F02：`session_vwap` 使用会话锚点，`opening_range_breakout` 要求完整开盘窗口；Crypto 的非交易日历语义不再悄悄套股票规则。
- F03：价格与数量用 `Decimal` 和 tick/step quantization，低价资产量化后重新做正数和边界校验，不再固定 `round(..., 4)` 导致归零。
- F04：EMA 策略读取 1h 环境并应用 RSI 过滤；规则 `score` 与经验 `calibrated_probability` 分开，概率不足为 `null` 并保留样本量，不把规则分数伪称胜率。

相关代码：`core/quant/strategies.py`、`core/strategy_monitoring.py`、`core/providers/base.py`、`core/instruments.py`。

### F05–F07：身份、双时间与回放

`core/instruments.py` 提供规范 `instrument_key = venue:market_type:native_symbol:settle_currency:price_type`，`Instrument` 载荷可恢复并校验；`core/storage/sqlite.py` 增加 `market_bar_versions`、`institutional_migration_runs`、`data_catalog` 等增量表。旧行情会进入明确的 legacy identity，无法确认来源时标记 `LEGACY_UNVERIFIED`，不猜成 Gate 或某个市场。

行情版本同时保留：

- `event_time`：市场事件发生时间；
- `first_received_at`、`available_at`、`fetched_at`：系统何时首次观测、何时可供研究、何时抓取；
- `revision_id`、`raw_hash`、`quality_status`：修订与质量血缘。

`latest_bars` 先按最新版本取数再按时间升序返回；`range_bars` 先过滤区间再应用 limit，并保留游标参数。`core/analysis/strategy_replay.py` 按 `available_at`/`data_as_of` 建虚拟时钟，缺 funding/OI/5m/session 时返回明确不支持或 `SIGNAL_RESEARCH_ONLY`，不返回“已评价但零交易”。

### F08–F11：反事实、生命周期、研究来源与新闻

- `core/analysis/strategy_evaluator.py` 以完整 `TradeLifecycle` 处理部分退出、剩余数量、持仓有效期间的高低点、盯市回撤和损失序列 ES；SHORT 的 MAE 使用高点，MFE 使用低点；缺失字段保持 `null`。
- 费用压力在独立 counterfactual 分支中重新计算，不再线性缩放最终盈亏；成本翻倍不会让固定成交路径的净收益变好；未评审分支不默认 `PASS`。
- `core/analysis/ai_trade_analytics.py` 和 `core/trading/trader_capabilities.py` 以 `trade_fills`/生命周期为权威样本，旧 prediction 仅作单独信号研究；缺失 fee 不再在投影中隐式变成零。
- `core/events.py`、`core/news_engine.py`、`core/security/local_guard.py` 使用解析后 hostname 的精确/子域白名单和逐跳 redirect 检查；新闻区分事实、来源、修订与影响假说，并处理否定句/转引，不用 substring 命中制造方向。

## B. 数据证据中心、研究资格与 API

### 第 12 章 / F05、F06、F11、F15

`apps/api/v3.py` 提供数据目录、最新/区间行情、质量、数据集详情、执行时间线、研究任务和资格查询。读取接口直接读取持久化数据；不会因为 GET 创建 task、触发外部抓取或写研究结果。`POST /v3/data/backfills` 具备 account/venue/mode 范围、幂等键和终态错误，但当前没有配置真实 provider 时返回 `NOT_CONFIGURED`，不伪造补数成功。

数据/身份、质量、运行身份和模型治理数据分别落在 `data_catalog`、`market_bar_versions`、`research_runs`、`qualification_history`、`model_evaluations`、`evidence_bundles` 等增量表；旧接口通过兼容读路径保留。

### 第 13 章 / F07、F08、F10

`POST /v3/research/runs` 记录完整策略版本、参数、数据范围、成本配置、账户环境和实验 trial；同一 idempotency key 返回同一 run，配置冲突返回 409。研究入口预先检查指定 account 的权威 `trade_fills`，没有真实成交时返回 `NOT_RUN_NO_AUTHORITATIVE_FILLS`，不会读取旧预测表凑样本。基线和参数扰动 trial 会真实落库并标记注册状态。

资格结果绑定 strategy/version/params、资产池、数据清单和成本场景。DSR/PBO 在样本、试验矩阵或统计依赖不足时明确 `NOT_CONFIGURED`/`NOT_QUALIFIED_*`；本轮没有填演示值，也没有把“收益为正”当作资格。

### 第 16 章：模块联动

主链路已经贯通为：

`market_bar_versions/data_catalog → StrategySpec/EvidenceBundle → Decision/TradePlan → RiskEngine → ExecutionGateway → trade_fills/ledger → Guardian → execution projection/research`。

`core/trading/trader_capabilities.py` 将 Decision→Plan→Intent→Fill→Position→Exit 的关系带入实际分析；`build_execution_ledger_projection` 是交易分析和页面的统一读模型。`apps/api/v2.py` 的 `/ai-analysis` GET 改为纯读，并与 v3 复用权威投影，而不是在查询时创建任务或重复下单。

### Gate 双账户写入与全链路

`core/trading/gate_accounts.py` 为 Gate 注册了两个相互独立的默认账户槽位：`gate_paper` 保留历史兼容 ID/行模式 `PAPER`，但其权威 `account_type=GATE_TESTNET`、effective mode 为 `TESTNET`，所有私有读取、下单、撤单、成交/持仓查询都指向 Gate 官方 TestNet；`gate_live` 为 `LIVE`，只保存实盘环境元数据并继续受 `RELEASE_POLICY_LOCK_M0_TO_M3` 锁定。两者的 account id、账户配置、账本 scope、凭证引用和执行 adapter 均不共享；初始化可重复调用，不覆盖既有余额、事件或配置。Gate TestNet 不使用本地假成交；本地撮合仅保留给明确的非 Gate `simulated/PAPER` 账户。

`core/security/credentials.py` 增加按 `account_id` 隔离的加密凭证表。Gate API 的 `POST /v2/gate/accounts/{account_id}/credentials`（兼容旧客户端）和显式 `/credentials/verify` 都先用该账户明确的环境做只读余额鉴权，只有 `valid=true` 才保存候选凭证；失败或网络不可用时保留旧凭证。未带 `account_id` 的 `/gate/config` 拒绝重新写入旧全局凭证槽。`apps/api/v2.py` 提供账户列表、默认双账户幂等注册、账户详情、凭证写入和验证；账户读取与交易读取按账户 scope 分流，Gate TestNet 读取官方 TestNet，LIVE 不自动访问私有接口。

交易计划、AI/Guardian 和恢复路径共用 `core/trading/execution_gateway.py` 的 account-scoped adapter 解析：已注册 Gate TESTNET 账户只解析自身凭证，缺失时失败关闭且不会退回旧全局 adapter；只有显式非 Gate `simulated/PAPER` 计划进入本地持仓撮合，Gate TestNet 计划保持远端订单/回执/对账语义，成交审计不创建本地 Gate 持仓镜像，LIVE 计划在发布锁处停止并不创建订单。`web/src/pages/V2WorkspacePage.tsx` 的 `/gate-live` 页面展示两个账户、环境、脱敏凭证状态和 LIVE 锁定状态，输入后以“验证并保存”触发上述只读检查，并显示验证结果。本轮已完成一次真实 Gate TestNet 私有验证、开仓、保护、reduce-only 平仓和远端归零；Live 未访问。

### Gate 公网只读抽测（2026-09-09）

对当前源码登记的两个公开合约 endpoint 做了真实 HTTP GET：`https://api-testnet.gateapi.io/api/v4/futures/usdt/contracts` 返回 HTTP 200、63 条 JSON 合约且包含 `name`/`quanto_multiplier`；`https://api.gateio.ws/api/v4/futures/usdt/contracts` 返回 HTTP 200、973 条 JSON 合约且包含同样字段（复验时间 2026-09-10）。该抽测只证明基址可达和公开响应形态，不能证明 API key 权限、账户余额、签名、下单或成交；本轮没有访问私有 endpoint。

## C. 组合风险、执行成本与 AI 证据

### 第 14 章 / F08、F09、F13

`core/trading/institutional_risk.py` 增加 `RiskCluster`、`ExposureSnapshot`、`TCAResult`、保守 cluster fallback、敞口、风险压力、capacity 和 TCA 计算。默认保守 Crypto 风险簇按 stop risk amount 聚合；相关性/深度不足时返回保守或 `NOT_CONFIGURED`，不会因为 BTC/ETH 名称不同而自动放大杠杆。TCA 使用 Decimal 记录 arrival/decision/fill、费用、滑点、成交量及部分成交状态。

`core/trading/execution_gateway.py` 保留既有单笔/组合/日亏/保证金/费用/授权/幂等/Guardian 边界，并将确定性回放时钟贯通 freshness、RiskEngine 与明确非 Gate `simulated/PAPER` 撮合；Gate TestNet 走账户专属远端 adapter，实时调用仍使用默认墙上时钟。`core/trading/ledger.py` 写入 `institutional_outbox` 与账本事件同一 SQLite 事务，`INSERT OR IGNORE` 保证重放不重复发布。

### 第 15 章 / F12、F15

`core/evidence.py` 提供冻结、canonical input hash、引用校验、不可变持久化和 digest 校验。`core/trading/ai_session_coordinator.py` 在模型调用前冻结多周期快照、质量、风险、新闻/宏观与持仓引用；调用后保存 prompt hash、响应、模型标识和 evidence 状态。`core/ai/ollama.py` 只有在 Ollama manifest/tag 提供真实 digest 时才填 weight digest；模型名字符串不会被 hash 冒充权重指纹，缺失即 `UNKNOWN_NOT_PROVIDED`。

AI 自主做单仍保留在同一 `ExecutionGateway` 和现有授权、租约、日亏、Guardian 边界之内；AI 不能选择执行客户端、改授权版本或提高风险预算。未配置模型/外部数据时不产生假 cycle、假成交或假收益。

## D. 用户界面与运维安全

- `web/src/components/InstitutionalEvidencePanel.tsx` 和 `web/src/pages/V2WorkspacePage.tsx` 接入 v3 数据质量、风险、AI evidence 和账户选择；界面展示 `UNKNOWN`、`NOT_CONFIGURED`、来源、时效、账户范围和缺失原因，不用空数组/0 默认冒充事实。
- `web/src/api/client.ts` 增加 v3 typed 请求入口；现有中文/英文 UI、策略/监控/账本/分析页面继续使用同一 account-scoped client。
- `core/security/local_guard.py` 与 v3 router 精确校验 local Origin；相似域名和异常端口被拒绝。
- `core/manifest.py`、`core/diagnostics.py`、`core/ai/ollama.py` 形成实例、版本、schema、数据根、模型 digest、租约/保护/待对账状态的可核对路径；诊断导出保持用户显式触发和脱敏。
- 默认 `LIVE` 仍为 `LOCKED`，由 `scripts/institutional_acceptance.py` 提供隔离自检。本轮仅访问用户明确授权的 Gate TestNet 模拟账户并完成一次开平；没有执行生产迁移，当前安装包仅完成用户级程序覆盖和快捷方式核验。

## R01–R11 实施对照

| 合同 | 实现路径 | 直接测试 ID | 结果与依赖 |
|---|---|---|---|
| R01 数据身份和迁移 | `core/instruments.py`, `core/storage/sqlite.py`, `core/providers/base.py`, `core/strategy_monitoring.py` | `test_institutional_repair_regressions.py::test_latest_bars_limit_one_returns_newest_and_cross_venue_identity_is_kept`; `test_institutional_repair_regressions.py::test_strategy_monitoring_persists_gate_native_identity_without_degrading`; `test_spec_at24_at30.py::test_at26_data_migration_idempotency_and_unverified_flagging`; `test_spec_rt17_rt26.py::test_rt25_clean_migration_and_credential_sanitization` | 隔离复制库与公开 Gate 三标的周期 PASS；生产活动库迁移 NOT_RUN（权限/安全边界）。 |
| R02 时间语义和查询 | `core/storage/sqlite.py`, `core/analysis/strategy_replay.py`, `core/trading/execution_gateway.py` | `test_institutional_repair_regressions.py::test_replay_missing_required_context_is_not_zero_trade_evaluated`; `test_spec_at15_at23.py::test_at16_lookahead_prevention_across_sessions`; `test_trader_reliability_v16.py` window/deadline tests | 隔离 deterministic/PIT 与实时时钟路径 PASS；真实历史可知性仍依赖数据源血缘。 |
| R03 策略合同及六策略 | `core/quant/strategies.py`, `core/strategy_monitoring.py` | `test_spec_at15_at23.py::test_at15_six_strategies_positive_negative_and_warmup`; `test_spec_at15_at23.py::test_at17_s6_missing_oi_and_funding_unsupported_no_zero_mocking`; `test_institutional_repair_regressions.py::test_strategy_monitoring_persists_gate_native_identity_without_degrading`; `test_v2_execution.py` strategy tests; `test_trader_reliability_v14.py` D01–D03 | 隔离边界与 Gate native identity 运行路径 PASS；默认参数是研究起点，不是优化或收益保证。 |
| R04 新闻事实和宏观 | `core/events.py`, `core/news_engine.py`, `core/security/local_guard.py` | `test_institutional_repair_regressions.py::test_source_registry_does_not_trust_path_or_source_text`; `::test_news_negation_changes_direction`; `test_spec_at15_at23.py::test_at18_url_spoofing_and_negation_macro_policy`; `test_trader_reliability_v14.py` news revision tests | 解析/修订/否定 fixture PASS；真实公开 provider/一致预期 NOT_CONFIGURED。 |
| R05 真正的研究回放 | `core/analysis/strategy_replay.py`, `core/trading/trader_capabilities.py`, `apps/api/v3.py` | `test_institutional_v3_api.py::test_v3_research_without_authoritative_fills_does_not_read_old_predictions`; `test_spec_at15_at23.py::test_at23_decision_replay_readonly_and_feedback_risk_recheck`; `test_trader_reliability_v13.py::test_v13_e_research_is_stored_costed_and_no_lookahead` | 权威 fills 门槛、成本/版本/时钟隔离 PASS；无 fills 时明确不运行。 |
| R06 指标及反事实 | `core/analysis/strategy_evaluator.py`, `core/analysis/ai_trade_analytics.py`, `core/trading/ledger.py` | `test_institutional_repair_regressions.py` partial/short/cost 三项；`test_spec_at24_at30.py::test_at24_partial_tp_aggregated_into_single_closed_trade`; `::test_at25_counterfactual_comparison_with_opportunity_cost`; `test_gate_account_chain.py::test_scale_in_existing_position_preserves_active_protection_without_name_error` | 固定路径成本守恒、部分退出、SHORT MAE/MFE 和已有仓位加仓 PASS_AFTER_REPAIR；不完整字段保留 UNKNOWN/null。 |
| R07 统一账本与全部页面 | `core/analysis/ai_trade_analytics.py`, `core/analysis/institutional_dashboard.py`, `core/trading/trader_capabilities.py`, `core/trading/execution_gateway.py`, `core/trading/gate_accounts.py`, `apps/api/v2.py`, `web/src/components/InstitutionalAnalysisDashboard.tsx`, `web/src/pages/V2WorkspacePage.tsx` | `test_institutional_v3_api.py::test_v3_read_endpoints_are_pure_and_preserve_identity`; `test_trader_reliability_v15.py` projection tests; `test_trader_reliability_v16.py` window tests; `test_gate_account_chain.py::test_gate_testnet_trade_plan_stays_remote_and_preserves_scope`; `test_gate_account_chain.py::test_gate_credential_verify_is_read_only_before_scoped_persist`; `tests/test_institutional_gate_testnet.py::test_gate_dashboard_is_read_only_and_uses_testnet_scope`; frontend full suite | Fill 权威投影、账户/venue/mode/date scope、Gate 双账户隔离、TestNet 远端回执/撤单 fixture、凭证验证写入/失败保留和 GET 无副作用 PASS_AFTER_REPAIR。 |
| R08 数据中心与研究资格接口 | `apps/api/v3.py`, `core/storage/sqlite.py`, `web/src/components/InstitutionalEvidencePanel.tsx` | `test_institutional_v3_api.py` 全部；`test_gate_account_chain.py::test_research_cancel_uses_authoritative_scope_and_does_not_500`; `test_spec_at24_at30.py::test_at30_manifest_evidence_completeness`; `test_spec_rt17_rt26.py::test_rt24_manifest_junit_driven_transitions` | 数据持久化/幂等/查询与取消状态转换 PASS_AFTER_REPAIR；真实补数与 DSR/PBO 统计因外部数据/样本不足为 PARTIAL/NOT_CONFIGURED。 |
| R09 组合和执行风险 | `core/trading/institutional_risk.py`, `core/trading/execution_gateway.py`, `core/trading/gate_accounts.py`, `core/trading/ledger.py` | `test_institutional_evidence_risk.py` risk/TCA/outbox 三项；`test_spec_at08_at14.py::test_at12_atomic_risk_reservation_and_daily_loss_persistence`; `test_spec_rt01_rt16.py` risk/concurrency/TCA tests; `test_gate_account_chain.py::test_gate_remote_adapter_resolution_is_account_scoped_and_never_falls_back`; `tests/test_institutional_gate_testnet.py::test_gate_testnet_remote_receipt_reconcile_cancel_and_symbol_mapping` | Decimal、原子预留、cluster fallback、TCA/outbox、账户级 adapter 解析和已有仓位路径 PASS_AFTER_REPAIR；真实盘口深度/容量 NOT_CONFIGURED。 |
| R10 AI 证据与模型治理 | `core/evidence.py`, `core/trading/ai_session_coordinator.py`, `core/ai/ollama.py`, `core/trading/ai_led_engine.py` | `test_institutional_evidence_risk.py::test_evidence_bundle_is_frozen_idempotent_and_digest_does_not_hash_model_name`; `test_repair_v12_luna.py::test_production_ai_coordinator_uses_qwen9b_and_persists_cycles`; `test_spec_rt17_rt26.py` RT17–RT20 | Evidence/digest 规则与 fixture coordinator PASS；真实 Qwen 权重/前瞻影子账户 NOT_CONFIGURED。 |
| R11 运维与安全 | `core/manifest.py`, `core/diagnostics.py`, `core/security/local_guard.py`, `core/security/credentials.py`, `core/trading/gate_accounts.py`, `core/trading/gate_live_client.py`, `core/trading/execution_gateway.py`, `core/trading/ledger.py`, `apps/api/v2.py`, `scripts/institutional_acceptance.py` | `test_institutional_v3_api.py::test_v3_rejects_spoofed_origin`; `test_spec_at01_at07.py::test_at07_credentials_zero_leakage_and_cross_origin_blocked`; `test_gate_account_chain.py::test_gate_credentials_are_encrypted_and_scoped_per_account`; `test_gate_account_chain.py::test_gate_credential_verify_is_read_only_before_scoped_persist`; `test_gate_account_chain.py::test_gate_credential_verify_failure_does_not_replace_existing_slot_or_leak_secret`; `tests/test_institutional_gate_testnet.py` TestNet schema/receipt/dashboard cases; Live-lock/cancel/scale-in cases; `test_institutional_evidence_risk.py::test_ledger_events_and_fills_publish_one_transactional_outbox_event_each`; isolated acceptance 4 checks; `REAL_GATE_TESTNET_ETHUSDT_OPEN_PROTECTED_CLOSE` | 本地服务控制、账户级加密凭证、只读验证后持久化、失败保留、脱敏、TestNet 远端 fixture、LIVE lock、事务 outbox、取消和已有仓位加仓 PASS_AFTER_REPAIR；真实 TestNet 一次开平 PASS，生产迁移和安装版重启未运行。 |

## 兼容与迁移说明

1. 迁移是 additive：旧表和旧 API 保留，机构表及索引通过 `CREATE TABLE IF NOT EXISTS` 与列补齐；旧行情复制到可识别的 legacy identity，重复执行不会新增同一版本。
2. 生产用户活动库不在本轮范围；所有迁移验证使用临时库或复制库。上线迁移仍需要备份、回退和可见状态，由 Sol/运维独立决定。
3. 旧 prediction/outcome 数据不删除、不冒充真实成交；统一账本只把 `trade_fills` 当作经济成交来源，旧数据缺 account/venue/mode 时保持 `LEGACY_UNCONFIRMED`。
4. v2 路由保持兼容，v3 只增加机构查询/任务合同；GET 不产生 side effect，POST 才创建补数/研究任务。

## 交付文件

机器可读结果、命令原始结果、源文件哈希、未完成项和完整路径清单见 [institutional-repair-result.json](../evidence/institutional-repair-result.json)。隔离验收入口为 [institutional_acceptance.py](../scripts/institutional_acceptance.py)，正式验收矩阵见 [institutional-repair-acceptance.md](institutional-repair-acceptance.md)。

## 代码与隔离复验记录（2026-09-10）

## 最终打包、覆盖安装与桌面启动复验（2026-09-10）

- 版本确认：`pyproject.toml`、`web/package.json`、Tauri 配置和安装文件均为 `2.0.0` / `v2.0.0`；没有改成 V1.7，也没有改版本元数据。
- 正式构建：`cargo tauri build --bundles nsis --ci --no-sign`，前置执行当前 React production build 与 PyInstaller sidecar，结果 PASS。
- 安装器：`D:/RJ/codex/ai-market-analyst/src-tauri/target/release/bundle/nsis/AI Market Analyst_2.0.0_x64-setup.exe`；SHA-256 为 `C155D7F0BB8AA1A2EC2342FA43962A281BACD4082973BEA8EA289E39E0ED41BC`，大小 `62,834,442` bytes；`/S` current-user 覆盖安装退出码 `0`。
- 数据保护：安装前已将当前用户级 `market_analyst.sqlite3`、旧备份和 `runtime.json` 复制到 `D:/RJ/codex/ai-market-analyst/artifacts/desktop-backup-20260910-125219`，三份源/备份 SHA-256 一致；未做生产迁移。
- 安装核对：快捷方式 `C:/Users/baicha/Desktop/AI Market Analyst.lnk` 指向 `C:/Users/baicha/AppData/Local/Programs/AI Market Analyst/ai-market-analyst.exe`，安装版本为 `2.0.0`；安装 sidecar SHA-256 `AD77CE4015627247E3430663A6005DB48D92A2269C713FC41182B6B802697032` 与本轮重建 source sidecar 一致。主程序与构建目录仅有安装器处理导致的 3 个 PE 标记字节差异，未把它写成完全同哈希。
- 启动后只读验收：`/health` 为 `200/ready`、`api_version=2.0.0`、`real_orders=false`、`private_keys=false`；`/hydration/status` 为 `ready`；监控 `stopped/active=false`；AI session `IDLE/active=false`；Gate 账户列表为空；没有创建订单或成交。
- 安全边界：3 个此前遗留的同名无窗口进程因 Windows 拒绝终止而未强杀；没有按模糊进程名清理，也没有触碰其他进程。当前新安装窗口和其自有 sidecar 已由快捷方式启动，仅用于上述健康检查。

最终安装只证明程序包、快捷方式和本地只读启动链路可用；本轮另行完成了一次用户授权的 Gate TestNet 真实开平，但它不扩大为生产库迁移、Live 权限或策略收益证明；Ollama Qwen 权重 digest、生产库迁移和 Live 仍分别受各自 `NOT_CONFIGURED`、`NOT_RUN` 或 `LOCKED` 边界约束。

## 隔离门禁结果摘要（2026-09-10）

- 当前版本核对为 `2.0.0` / `v2.0.0`，没有改版本号；V1.7 只完成过参考核对。
- `python -m pytest -q`：`362 passed, 1 skipped, 1 warning`（219.01s）；唯一 warning 是 Starlette/httpx TestClient 弃用提示。监控流清理竞态已加防护，`test_rt05_stale_market_data_degrades_and_blocks_opening` 单测通过且无未处理线程异常。
- Gate TestNet 相关隔离回归：`15 passed`；覆盖官方 TestNet URL/响应 schema、符号 canonicalization、远端回执/撤单、账户凭证验证失败保留、账户级 adapter 和 dashboard GET 无副作用；另有一次真实 `ETHUSDT` 1 张开仓、保护、reduce-only 平仓并远端归零。
- `web`：`22` 个测试文件、`96` 项通过；`npm run build`（86 modules）、`npm run typecheck`、`npm run lint` 和 `cargo check` 均通过。
- 公开网络抽测只使用无凭证 GET：TestNet `https://api-testnet.gateapi.io/api/v4/futures/usdt/contracts` 与 Live `https://api.gateio.ws/api/v4/futures/usdt/contracts` 均 HTTP 200。它们不证明私有鉴权、余额、下单或成交。
- 正式包激活前的安装版只读观察到 AI `STOPPED`、session `IDLE`、执行阻断且订单/持仓/成交为 0，因此不能声称正在做单；随后已按本节完成最终包覆盖与启动复验。`LIVE` 继续默认锁定。
- 最终交付状态：`DEVELOPER_COMPLETE_PENDING_SOL_ACCEPTANCE`。可选套利/HFT/期权不在本包；真实 Ollama/Qwen digest、生产库迁移和真实 TestNet 私有链路保留为未完成依赖。
