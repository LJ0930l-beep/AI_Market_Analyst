# AI Market Analyst 机构量化审计修复验收记录

> **当前 main 权威复核（2026-09-10）**：本节优先于本文保留的历史验收快照。当前 HEAD 为 `3700bc63d4e35df1f0e2b2ec86beeb5d889a2889`，`main` 与 `origin/main` 同步；四个版本来源均为 `2.0.0`。V1.7 仅被核对，没有改版本号。

## 本轮复核摘要

本轮以当前 `main` 的源码和工作区为基线直接复核，保留用户既有改动，不做 reset/checkout/clean，不运行 `scripts/direct_install.ps1`；在原程序已停止的前提下，用当前 MSVC NSIS 包完成 current-user 安装，并按用户后续明确要求将交付清单提交/推送到 `origin/main`。未访问业务数据库、私有 Gate 凭证或真实订单，安装后未启动应用。

| 项目 | 结果 |
|---|---|
| 后端完整 pytest | `370 passed, 1 skipped, 1 warning` |
| 前端 | `22 files / 98 tests`、typecheck/lint/build 全部 PASS |
| 隔离机构验收脚本 | `4/4 PASS` |
| Gate 公共 HTTP | TestNet 200/63 contracts；Live 200/977 contracts；只读公开接口 |
| 本机 Qwen | `qwen3.5:9b` 可用；真实 digest `6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7`；无账户 WAIT smoke PASS |
| 当前安装版 | current-user 安装成功，版本 `2.0.0`、桌面快捷方式和 sidecar 哈希已核对；`127.0.0.1:18765` 安装后未启动 |
| 新 sidecar | 构建 PASS，并随当前 NSIS 包安装 |
| NSIS | MSVC 目标生成并成功完成 current-user 安装，安装器退出码 `0`；安装后未启动应用，不访问业务库或账户 |

### 当前验收边界

- `gate_testnet` 是唯一权威 Gate TestNet 账户；`gate_paper` 只接受为历史输入别名，不再指向本地 PAPER 撮合。`gate_live` 独立保存元数据，发布锁保持 `LOCKED`。
- Gate 远端余额/保证金/持仓/挂单/成交由账户作用域 adapter 和 `GateAccountTruthService` 读取、记录、镜像；镜像不生成 `trade_fills`，不把本地初始资金当成远端权益。
- 独立 TestNet E2E 已实现“确认→真实 entry→远端回执→持仓/保护→可选 reduce-only 清理→远端归零→撤保护”的步骤、超时、幂等与故障关闭，但因没有用户凭证，本轮只通过隔离 fake adapter，真实私有端点和真实订单为 `NOT_ATTEMPTED`。
- AI 运行阶段区分 `SYSTEM_BLOCKED`、`MODEL WAIT`、风险拒绝和执行结果；真实 Ollama 9B digest/响应/延迟可被保存。Qwen smoke 不是固定评测集、影子账户或真实交易表现证明。
- Ollama 兼容修复只对服务端明确 `failed to parse grammar` 的 Schema 拒绝使用 JSON mode + 本地严格校验；普通错误仍失败关闭，证据带 `schema_enforcement`。

### R01–R11 独立验收索引

| 合同 | 结果 | 关键实现/测试 | 依赖与限制 |
|---|---|---|---|
| R01 | `PASS_ISOLATED` | instrument identity、account alias migration、latest/range；`AT-DATA-IDENTITY-LATEST` | 生产活动库迁移未执行 |
| R02 | `PASS_ISOLATED` | closed-bar/PIT/latest 相关 full pytest 与 RT 回归 | 真实源的历史可知性依赖数据提供方 |
| R03 | `PASS_FIXTURE` | 六策略边界、typed candidate、退出/非法参数测试 | 不把默认参数当优化结果 |
| R04 | `PARTIAL_EXTERNAL` | 精确 hostname、重定向安全、否定句方向测试 | 真实付费/私有新闻与 FF actual 仍未配置 |
| R05 | `PASS_SCOPED` | 回放时钟、版本/hash、缺数据不做零交易假评估 | 足量真实试验才可运行 DSR/PBO |
| R06 | `PASS_ISOLATED` | TradeLifecycle、SHORT MAE/MFE、费用守恒、反事实 | 真实深度/资金费/费用源按字段标记 |
| R07 | `PASS_ISOLATED_UI` | 统一账本投影、AI stage trace、GET 纯读、98 前端测试 | UI 已构建，未安装到当前用户程序 |
| R08 | `PASS_ISOLATED` | v3 research cancel、资格与持久化全量回归 | 未接入付费数据不阻断其他实现 |
| R09 | `PASS_ISOLATED` | Decimal/Guardian/租约/风险预留、TestNet E2E fake chain | 私有 Gate 凭证缺失，真实订单未尝试 |
| R10 | `PASS_LOCAL_WITH_REAL_SMOKE` | digest 目标选择、Ollama grammar fallback、stage/model provenance | 未做固定 eval、forward shadow、效果结论 |
| R11 | `PASS_LOCAL_WITH_MSVC_BUILD` | 脱敏、Origin、outbox、compileall、ruff F821、cargo check、MSVC NSIS、current-user install | 生产/真实 TestNet 运维未运行；LIVE 仍锁定 |

Sol 仍需独立复核并决定里程碑签收；本文件不把开发者验证写成生产发布批准。

> **以下为历史快照**：从“验收结论边界”开始的旧执行记录用于追溯，可能包含先前运行、安装或测试环境的结果；若与本文开头“当前 main 权威复核”冲突，以当前 main 复核和 `evidence/institutional-repair-result.json` 的 `current_main_revalidation` 为准。

## 验收结论边界

开发执行状态：`DEVELOPER_COMPLETE_PENDING_SOL_ACCEPTANCE`。

本文件是交给 Sol 的独立验收输入，不是 Luna 自行签收。通过项只表示在当前 checkout、隔离数据库和确定性测试夹具中满足对应合同；不把 fixture 结果升级为真实 Qwen、公开 HTTP、Testnet 或实盘能力。

版本核对结果：当前源码与前端包版本均为 `2.0.0`，HEAD 为 `32596a3f1431a6646e3c5cb853a213fcc69a067c` / `v2.0.0`。V1.7 只作为用户提出的版本参考被核对，没有修改版本号或版本文件。

## 本轮独立审验与修复后复验（2026-09-09）

审验先在隔离临时库发现两个未覆盖的高风险运行时错误；本轮已完成修复、回归覆盖和全量重跑。当前开发交付状态为 `PASS_AFTER_REPAIR / DEVELOPER_COMPLETE_PENDING_SOL_ACCEPTANCE`，仍由 Sol 独立验收，不由 Luna 自行签收：

| 编号 | 严重度 | 影响合同 | 复现与根因 | 结果 |
|---|---|---|---|---|
| AUDIT-001 | HIGH | R08、R11 | 修复前：临时 SQLite 创建研究任务后调用 `POST /v3/research/runs/{run_id}/cancel` 返回 `500 Internal Server Error`；根因是 `apps/api/v3.py:569` 使用了未定义的 `scope`。修复后先解析 account scope，状态转换回归通过。 | PASS_AFTER_REPAIR |
| AUDIT-002 | HIGH | R06、R07、R09、R11 | 修复前：临时 SQLite 对同一 `position_id` 连续记录第二笔加仓成交返回 `NameError: ProtectionStatus is not defined`；修复后账本使用本地保护状态常量，已有仓位加仓回归通过。 | PASS_AFTER_REPAIR |

两项均在隔离临时库中完成“修复前失败、修复后通过”的闭环，未访问业务数据库、账户或交易接口。历史失败输出保留在 evidence JSON；修复后的回归、Gate 账户链路和完整 pytest 结果见下文。

## 运行态与页面审验（2026-09-10）

| 审验项 | 观察 | 结论 |
|---|---|---|
| 当前安装版健康与执行状态 | `2.0.0`、ready/local-only；首次只读快照为 `backoff`（18 次失败），最新快照为 `degraded`、worker 已停止、执行已阻断（32 次失败，租约续期失败）；AI `STOPPED`/底层 session `IDLE`；按响应明细读取交易计划、订单、持仓、成交均为 0 | 安装版在线，但当前没有做单 |
| 运行库安全边界 | 早先活动 SQLite 检查存在 journal 且外部已提交视图与进程 API 订阅视图不一致；后续只读文件检查时 journal 已不存在 | 只记录，不修复、不迁移；本轮未删除 journal 或写库 |
| Gate 身份根因 | `BTCUSDT` 请求 → provider 原生 `BTC_USDT`，监控漏 canonical key，存储严格字符串校验再次拒绝 | `core/strategy_monitoring.py` 补全 canonical key；`core/storage/sqlite.py` 接受仅分隔符差异并保留原生身份 |
| 隔离公开行情周期 | 临时库 + Gate 公开行情 GET：BTC `UNSUPPORTED`（显式 crypto ORB policy），ETH/SOL `NO_TRIGGER`，整体 `COMPLETED`，无 degraded error | PASS；证明身份/存储运行路径，不证明私有权限或成交 |
| 宏观日历 UI | 长列表限制为响应式高度的带框 `role=region`，内部滚动并支持键盘 focus | PASS；页面不会被长栏无限推低 |

本节的安装版观察来自只读检查，且本轮未重启或替换正在运行的安装版；因此源代码修复尚未被宣称为当前安装版已激活。没有访问私有 Gate 账户、没有下单，也没有改变业务库或用户配置。

## Gate 双账户链路验收

本轮在不接触私有账户、不下单的边界内增加了 Gate 账户级写入与路由：

- `gate_paper`：保留历史兼容 ID/行模式 `PAPER`，但权威 `account_type=GATE_TESTNET`、effective mode 为 `TESTNET`；使用 Gate 官方 TestNet 的账户、订单、成交和持仓链路，不使用本地模拟成交或本地假填充。
- `gate_live`：独立 `LIVE` 账户，保存实盘环境元数据和按账户加密凭证能力，但默认保持 `LOCKED`；未在页面显式触发验证时不会读取私有余额、成交或提交订单。
- 账户列表、幂等注册、详情和凭证写入均按 `account_id` 作用域；Gate TestNet 交易计划必须走账户专属远端 adapter，缺凭证时失败关闭，LIVE 交易计划在发布锁处 `NOT_RUN`。本地账本/撮合仅适用于明确的非 Gate `simulated/PAPER` 账户。
- `/gate-live` 页面支持按所选账户输入 API Key/Secret 并点击“Verify & save”；后端只对该账户明确的环境发起一次只读余额鉴权，只有 `valid=true` 才写入该账户的加密凭证槽，失败不会覆盖旧凭证。
- AI/Guardian、策略、交易计划、恢复和 API 共用 account-scoped `ExecutionGateway` 解析；缺凭证或缺外部 adapter 时失败关闭，不退回另一个账户或旧的全局 Gate client。

直接回归：`tests/test_gate_account_chain.py` 11 项与 `tests/test_institutional_gate_testnet.py` 4 项共 15 passed，覆盖账户隔离、加密凭证、只读验证后持久化/失败保留、TestNet 远端回执/撤单/符号映射、LIVE 锁、跨账户 adapter、取消 500 修复和已有仓位加仓修复。

## F01–F15 验收矩阵

| 功能 | 目标 | 证据测试 | 当前结果 |
|---|---|---|---|
| F01 | 15m 结构与 5m 策略周期分离 | `test_spec_at15_at23.py::test_at15_six_strategies_positive_negative_and_warmup`; v14 D03 | PASS（隔离 fixture） |
| F02 | Session VWAP / ORB 会话语义 | AT15、v14 D02 typed conditions | PASS（会话窗口缺失会显式缺数据） |
| F03 | tick/step Decimal 量化，低价不归零 | `test_institutional_repair_regressions.py::test_low_price_quantization_is_positive_and_revalidated`; AT13/RT16 | PASS |
| F04 | EMA 1h 环境、RSI 过滤，score 与 probability 分离 | regression EMA；AT15/AT17 | PASS；校准样本不足时 probability 为 null |
| F05 | 规范 instrument identity 与增量迁移 | regression identity；AT26；RT25 | PASS（临时/复制库）；生产活动库迁移未运行 |
| F06 | event/available/fetched 双时间及 latest/range/cursor | regression latest；AT16；v16 window/deadline | PASS |
| F07 | PIT 回放与上下文门槛 | regression missing context；AT23；v13 research | PASS；仅信号路径明确 `SIGNAL_RESEARCH_ONLY` |
| F08 | 独立反事实、费用/机会成本重算 | regression counterfactual；AT24/AT25；RT07 | PASS；缺成本时不伪造 complete |
| F09 | 生命周期、部分退出、SHORT MAE/MFE、MTM DD/ES | regression partial/short；AT24；v16 window | PASS |
| F10 | 研究以成交账本为输入、GET 纯读、POST 任务 | v3 research；v13 E；AT23 | PASS；无权威 fills 明确 `NOT_RUN_NO_AUTHORITATIVE_FILLS` |
| F11 | SourceRegistry、redirect SSRF、新闻否定与事实/影响分离 | regression source/news；AT18/AT19/AT21；v14 news | PASS（确定性来源/文本）；真实公开来源依赖未配置 |
| F12 | EvidenceBundle、AI 输入证据与真实 digest 规则 | evidence test；RT17–RT20；coordinator test | PASS（证据路径）；本次真实 Ollama digest 未配置 |
| F13 | 风险簇、敞口、保守 fallback、TCA/capacity | risk/TCA test；AT12；RT06/RT13/RT14 | PASS（Decimal/保守簇）；真实盘口容量未配置 |
| F14 | exact Origin 校验 | regression Origin；v3 spoof test；AT07 | PASS |
| F15 | 运行身份、schema、数据根、模型/租约/保护状态 | v3 catalog/quality；RT24；manifest/diagnostics tests；安装后版本/快捷方式核验 | PASS（本地诊断路径和当前 2.0.0 安装文件已核验）；真实账户运行态未核验 |

## R01–R11 验收矩阵

完整实现路径和逐条测试映射见 [institutional-repair-implementation.md](institutional-repair-implementation.md)。下面记录验收时必须区别的状态：

| 合同 | 本轮可判定结果 | 未覆盖或依赖 |
|---|---|---|
| R01 | 隔离迁移、canonical identity、legacy 标记 PASS | 生产库备份/回退/实际迁移未执行 |
| R02 | 双时间、PIT 门槛、最新/区间查询 PASS | 历史首次可知性需真实源血缘 |
| R03 | 六策略边界、参数校验、上下文状态 PASS | 参数仍是研究默认值，不是已优化结果 |
| R04 | hostname/redirect/否定/修订合同 PASS | 公开 provider 与一致预期为 NOT_CONFIGURED |
| R05 | 权威 fills 门槛、确定性回放与独立时钟 PASS | 无 fills/无外部历史时不运行研究 |
| R06 | 生命周期、成本守恒、指标 null 语义和已有仓位加仓 PASS_AFTER_REPAIR | 真实费用/资金费覆盖取决于数据源 |
| R07 | 统一账本投影、Gate 双账户隔离、交易计划 scope、GET 无副作用 PASS_AFTER_REPAIR | 旧 legacy 行需后续人工确认归属 |
| R08 | v3 目录/质量/任务/研究/资格/时间线及取消状态转换 PASS_AFTER_REPAIR | 真实补数、足够实验矩阵后的 DSR/PBO 为 PARTIAL/NOT_CONFIGURED |
| R09 | 风险簇、Decimal TCA、outbox、账户级 adapter 解析、LIVE lock PASS_AFTER_REPAIR | 真实深度/参与率/交易所保证金合同未接入 |
| R10 | 冻结证据、模型治理、digest 不伪造 PASS | 真实 Qwen 9B 权重、前瞻影子账户未运行 |
| R11 | exact Origin、账户级加密凭证、脱敏诊断、事务 outbox、LIVE lock、取消和已有仓位加仓 PASS_AFTER_REPAIR | 当前用户级安装覆盖已核验；跨进程真实 Testnet/生产运维未运行 |

## 命令与结果

以下命令均针对当前工作树执行；后端使用 pytest 临时目录 fixture，未连接用户业务数据库。

| 命令 | 结果 |
|---|---|
| `python -m pytest -q` | PASS — `362 passed, 1 skipped, 1 warning in 219.01s (0:03:39)` |
| `python -m pytest -q tests/test_institutional_repair_regressions.py`（修复前基线） | 修复前 `10 failed`；同一回归集修复后 PASS，具体以最终 full pytest 为准 |
| `python -m pytest -q tests/test_institutional_repair_regressions.py tests/test_spec_at24_at30.py tests/test_trader_reliability_v14.py::test_v14_d03_closed_bar_replay_and_real_parameter_perturbation` | PASS — `18 passed, 1 warning` |
| `python -m pytest -q tests/test_institutional_v3_api.py tests/test_institutional_evidence_risk.py tests/test_institutional_repair_regressions.py` | PASS — 现有审验相关集 `14 passed, 1 warning`；Gate 合并集见下一行 |
| `python -m pytest -q tests/test_gate_account_chain.py tests/test_institutional_gate_testnet.py` | PASS — `15 passed, 1 warning in 9.51s` |
| `python -m pytest -q tests/test_gate_account_chain.py tests/test_institutional_v3_api.py tests/test_institutional_repair_regressions.py` | PASS — `26 passed, 1 warning in 9.84s` |
| `python -m pytest -q tests/test_gate_account_chain.py` | PASS — `11 passed, 1 warning in 5.95s` |
| `python -m compileall -q core apps scripts/institutional_acceptance.py tests/test_gate_account_chain.py tests/test_institutional_repair_regressions.py` | PASS — exit 0 |
| `python scripts/institutional_acceptance.py` | PASS — 4/4 隔离检查；未覆盖 evidence JSON |
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-tauri.ps1` | PASS — 当前 React production assets 与 PyInstaller sidecar 重建 |
| `cargo +stable-x86_64-pc-windows-gnu tauri build --target x86_64-pc-windows-gnu --config '{"build":{"beforeBuildCommand":""}}'` | PASS — 当前 2.0.0 NSIS installer；inline override 仅绕过现有相对路径前置命令问题，未修改用户配置 |
| 当前 installer `/S` user-level install | PASS — exit code 0；主程序为 2.0.0，安装 sidecar 哈希与刚重建的 source sidecar 一致，桌面快捷方式指向核对过的安装目录，安装后无残留进程 |
| `cd web; npm run build` | PASS — Vite production build，86 modules transformed |
| `cd web; npm test -- --run` | PASS — `22 test files / 96 tests passed`，含宏观日历滚动区域和 TestNet 文案回归 |
| `cd web; npm run typecheck` | PASS |
| `cd web; npm run lint` | PASS — `--max-warnings=0` |

pytest 的唯一 warning 是当前安装环境的 Starlette/httpx TestClient 弃用提示，不影响本轮断言结果；此前隔离清理竞态造成的未处理监控线程异常已修复，针对性回归无 warning。

## Gate 公网只读验证

用户指出“未做网络验证不能知道 API 是否有效”后，本轮补做了不带凭证的公开合约接口抽测。官方文档入口为 [Gate API v4](https://www.gate.com/docs/developers/apiv4/en/)。结果如下：

| 配置中的公开 endpoint | HTTP 结果 | 返回形态 | 结论边界 |
|---|---:|---|---|
| `https://api-testnet.gateapi.io/api/v4/futures/usdt/contracts` | 200 | JSON array，63 条，包含 `name` / `quanto_multiplier` | TestNet 公共基址可达且响应形态符合预期 |
| `https://api.gateio.ws/api/v4/futures/usdt/contracts` | 200 | JSON array，973 条，包含 `name` / `quanto_multiplier` | 当前源码登记的 Live futures 公共基址可达且响应形态符合预期 |

这只是公共 GET 的可达性与字段形态验证，不证明私有 API key 权限、账户余额、签名、下单、成交或资金状态。页面上的 `Verify & save` 才会在用户主动输入后，对所选账户做一次只读私有余额校验；本轮没有凭证，因此该私有验证仍为 `NOT_ACCESSED`，也没有 TestNet 下单或实盘读取。

## 隔离验收脚本

`scripts/institutional_acceptance.py` 的四项检查为：

1. `AT-DATA-IDENTITY-LATEST`：临时 SQLite 中 canonical identity、5m bars 与 latest=最新一根；
2. `AT-EVIDENCE-IDEMPOTENCY-DIGEST`：EvidenceBundle 重复持久化只成功一次，模型名称不当作 digest；
3. `AT-COST-TCA-DECIMAL`：Decimal TCA 的成交量、费用、滑点守恒；
4. `AT-LIVE-DEFAULT-LOCK`：LIVE 默认保持 `LOCKED`。

脚本明确记录：`public_http=NOT_CONFIGURED`、`ollama_qwen_weights=NOT_CONFIGURED`、`private_trading_account=NOT_ACCESSED`、`real_orders=NOT_ATTEMPTED`。它不读取当前配置的数据库、不调用私有账户、不启动订单 adapter。Gate 双账户回归另使用临时 SQLite；没有执行真实 Gate 私有 API、Testnet 下单/成交或实盘读取。

## 公开 HTTP 与 fixture 的区别

| 证据类别 | 本轮状态 | 允许得出的结论 |
|---|---|---|
| 确定性 Python/SQLite/HTTP TestClient fixture | PASS | 证明代码合同、隔离数据流、失败关闭和幂等断言成立 |
| 固定公开样本/历史审计 evidence | 仅作输入来源说明 | 不能证明当时真实可知或当前在线可用 |
| Gate 配置中的公开 futures contracts GET | `PASS_READ_ONLY` | 仅证明两个配置基址当前可达、响应为预期 JSON 形态 |
| 其他真实公开 HTTP provider 抽测 | `NOT_CONFIGURED` / 未运行 | 不能声称当前外部行情、新闻或一致预期可用 |
| Gate 私有 API key/secret 验证 | `NOT_ACCESSED`（等待页面输入） | 不能声称当前账户凭证有效；需用户在 `/gate-live` 主动提交 |
| 真实 Ollama `qwen3.5:9b` 权重 digest 与模型 E2E | `NOT_CONFIGURED` / 未运行 | fixture model trace 不等于真实 Qwen 效果 |
| Testnet/private account/fill/fee reconciliation | 未访问 | 不声称连接交易所或完成外部成交对账 |
| 当前用户级安装覆盖与快捷方式核验 | PASS（一次用户确认退出后的覆盖） | 仅验证程序文件、版本和快捷方式；生产用户库迁移及跨进程升级回归未执行 |

## 未完成项与剩余风险

- Gate 公共 futures contracts 已按代码配置的 TestNet/Live 基址完成只读网络抽测；其他真实公开行情/新闻/一致预期和外部 provider 仍需要配置后再抽测，失败应保留 `NOT_CONFIGURED`/`UNKNOWN`。
- 真实 Ollama manifest digest、固定评测集和前瞻影子账户尚未运行；当前 coordinator 测试使用确定性 provider。
- DSR/PBO 只有在足够真实实验 trial、OOS 矩阵、样本长度、偏度/峰度等输入齐全后才能计算；当前不填演示统计值。
- 生产活动库迁移未执行；后续必须先备份，在复制库/临时库验证升级与回退，再由独立运维操作。
- 真实盘口深度、参与率、资金费和交易所保证金合同未配置；PAPER capacity 保持 `NOT_CONFIGURED` 或采用明确保守假设。
- `LIVE` 仍受 release policy 锁定；本轮没有解锁、没有私有凭证、没有真实下单。
- Gate `gate_paper` / `gate_live` 的账户注册、凭证隔离、TestNet 远端 adapter 回执/撤单 fixture、页面只读验证路径和 LIVE 发布锁已通过隔离 fixture；真实私有凭证验证需要用户在 `/gate-live` 输入，真实 TestNet 下单/成交和实盘订单仍未访问。
- 当前 checkout 有大量先存 dirty/untracked 用户变化；本轮保留，Sol 验收前请以最终 `git status` 和文件哈希为准。

Sol 需要独立复核源码、结果 JSON、完整 pytest 和前端命令后决定是否接受；本记录不把“开发完成”写成“生产上线通过”。

## 代码与隔离复验快照（2026-09-10）

## 最终桌面安装与启动证据（2026-09-10）

正式 Tauri NSIS 包已从当前源码重建并以 current-user 模式覆盖安装：

- 安装器：`D:/RJ/codex/ai-market-analyst/src-tauri/target/release/bundle/nsis/AI Market Analyst_2.0.0_x64-setup.exe`；SHA-256 `C155D7F0BB8AA1A2EC2342FA43962A281BACD4082973BEA8EA289E39E0ED41BC`；`/S` exit code `0`。
- 桌面快捷方式：`C:/Users/baicha/Desktop/AI Market Analyst.lnk`，目标为 `C:/Users/baicha/AppData/Local/Programs/AI Market Analyst/ai-market-analyst.exe`。
- 安装前备份：`D:/RJ/codex/ai-market-analyst/artifacts/desktop-backup-20260910-125219`；活动数据库、旧备份和运行时配置均逐文件 SHA-256 对照一致。
- 安装后的只读启动：`/health=200 ready`、API `2.0.0`、`real_orders=false`、`private_keys=false`；`/hydration/status=ready`；监控 `stopped`、AI `IDLE`、无 Gate 账户/订单/成交。
- sidecar 安装哈希为 `AD77CE4015627247E3430663A6005DB48D92A2269C713FC41182B6B802697032`，与本轮构建产物一致。主程序与构建产物只差安装器写入的 3 个 PE 标记字节，因此证据不宣称主程序 SHA-256 完全相同。

这些安装证据不扩大真实外部验收范围：Gate 私有凭证和 TestNet 订单仍未访问，Qwen 权重 digest 未配置，生产库迁移未执行，LIVE 继续受发布锁保护。工作树的 102 个 dirty/untracked 状态项均保留。

## 既有验收快照补充

当前源码仍是 `2.0.0` / `v2.0.0`，没有改成 V1.7。完整后端为 `362 passed, 1 skipped, 1 warning`（219.01s）；Gate TestNet 专项为 `15 passed`；前端为 `22` 个测试文件、`96` 项通过，build/typecheck/lint 与 `cargo check` 均通过。公开 Gate TestNet/Live 合约 GET 均 HTTP 200，但私有凭证、TestNet 真实订单/成交、Qwen 权重 digest 和生产库迁移均未执行。正式包启动后观察到监控默认 `STOPPED`、AI session `IDLE`、无账户/订单/成交；`LIVE` 仍为发布锁定。

因此本轮交付结论是 `DEVELOPER_COMPLETE_PENDING_SOL_ACCEPTANCE`：代码、隔离 fixture 和安全边界可交给 Sol 独立验收，不能据此宣称真实交易链路或生产上线已验收。
