# AI 做单闭环验收报告（2026-09-16）

> 承接 `docs/no-order-root-cause-2026-09-11.md`。上一份文档定的 6 层阻断已全部修完；
> 本份记录**修完之后的运行态验收结果**，以及随之暴露的、性质完全不同的新瓶颈。

---

## 一、结论（先看这段）

1. **A / B / C / E 四项改动全部在运行态验证通过**（证据见第三节），
   断开的三条回路（策略→模型、决策→结果、绩效→模型）**已闭合**。
2. **"开不了单"的原因已经换了一层，不再是断链。** 新的因果链是：
   - ~~模板声明的扫描标的里，BTCUSDT 与 `liquidity_sweep` 没有任何启用的订阅~~
     → **已更正（§四「发现 2 的更正」）**：`enabled=0` 不等于不被扫描。扫描器对
     「一条 enabled 都没有」的标的会**自动补齐全部策略**，所以 BTCUSDT 其实被 6 个策略
     扫到、模板点名的 3 个全覆盖。真正缺的是 **ETHUSDT / SOLUSDT**：
     它们各有 1 条 enabled，反而被**收窄**到只剩那 1 条，各缺 2 个策略。
   - 唯二启用的两条规则（`ETHUSDT:bollinger_squeeze`、`SOLUSDT:ema_trend`）
     **当前都是 `NO_TRIGGER`**；
   - 模型被要求「自己从原始 K 线authoring 入场」，而它**120 轮 100% 输出 WAIT**；
   - 其自评 `confidence` **111 轮里 89 轮恰好是 65.0**，而系统里**每一道 confidence 门槛都是 70**。
3. **所以现在不是"代码坏了"，是"模型从不commit"。** 继续堆 prompt 文案已验证无效
   （模型会在 `reason` 里先复述禁令再绕过它）。
4. 另有一条**运维缺口**：重建/重启客户端后 AI 自主会话**不会自动恢复**，
   必须手动 POST 一次启动。本次会话里我踩了两次。

---

## 二、验收时间线

| 时刻（UTC） | 事件 |
|---|---|
| 09-15 18:11:55 | 第一次重建安装完成，新 sidecar 起 |
| 09-15 18:23:13 | 发现 AI 会话 `STOPPED`（`last_reason=not_started`），手动拉起 |
| 09-15 18:23:40 | runtime `degraded / consecutive_failures=10` → 拉起后 `running / 0` |
| 09-15 18:31:46 | **第一轮重启后周期**：`performance_context` **不在** payload（原因见下） |
| 09-15 18:34–18:37 | 第二次重建（补 `ai_led_engine` 落库镜像） |
| 09-15 18:37:19 | 再次手动拉起会话，`next_scan_at=18:45` |
| 09-15 ~18:45 后 | 机器/客户端停止，**18:45 那轮从未执行** |
| 09-16 11:50:07 | 客户端重新启动（新 `instance_id`），AI 会话又是 `STOPPED` |
| 09-16 12:16 | 确认 17.5 小时内**零周期** |
| 09-16 12:17:08 | 手动拉起会话，`next_scan_at=12:30` |
| 09-16 12:31:50 | **验证周期落地** `cycle_20260916T123000015186Z_21824` → B 通过 |

---

## 三、A / B / C / E 运行态验收

验证周期：`cycle_20260916T123000015186Z_21824`（2026-09-16T12:31:50Z，
`action=WAIT`，`status=WAITING`，`block_stage=None`，`operational_state=MODEL_DECISION`）。

| 项 | 内容 | 判定 | 硬证据 |
|---|---|---|---|
| **A** | 决策结果回填接通 | ✅ 代码在位 / ⚠️ 无样本可证 | `reconcile_decision_outcomes` 调用点已植入 `_run_cycle_once`；`ai_decision_memory` 5 行**全为 WAIT**，`outcome_status` 非空数 = 0 —— WAIT 按设计永不回填，12 条单测覆盖 |
| **B** | 绩效反馈进 prompt | ✅ **端到端通过** | `payload_json['performance_context']` 存在且完整（见下） |
| **C** | 思维链开关真实化（不启用） | ✅ | `model_inference_settings`: `think=false`、`max_tokens=900`、`keep_alive=45m`、`context_length=32768` |
| **E** | 模板矛盾修正 | ✅ **逐字命中** | `strategy_instructions.revision=38`、`len=10`，`sections` 文本与改写完全一致 |

### B 的落库实测值

```json
{"status": "AVAILABLE", "stance": "INSUFFICIENT_SAMPLE", "closed_trades": 0,
 "winning_trades": 0, "losing_trades": 0, "win_rate_pct": 0.0, "profit_factor": 1.0,
 "max_drawdown_pct": 0.0025443870613252135, "net_pnl_usdt": 11.490449705197534,
 "current_equity_usdt": 50172.77943219892, "initial_capital_usdt": 50161.28898249372,
 "equity_basis": "GATE_TESTNET_REMOTE_ACCOUNT_TRUTH",
 "capital_source": "gate_testnet_remote_account_truth", "capital_stale": false,
 "guidance_zh": "已了结交易仅 0 笔，样本不足以判断策略优劣；按标准评估本轮证据即可，不要因为样本小就对同一形态给出两极化的置信度。"}
```

> **资金口径复核**：权益 50 172.78 / 起算 50 161.29 均来自远端快照，
> **不是本地种子 10 000** —— `GATE_TESTNET_REMOTE_ACCOUNT_TRUTH` 铁律成立。

### E 的逐字命中（模型看到的 sections）

- `entry_standards`：「动量敏捷触发规则（**本模板只做「价格已经到位」的即时进场，不预埋未来限价单**）：… 2.【价格尚未到位时】：… 应当输出 WAIT…」
- `decision_process`：含「**不得把尚未触发的价位描述成已成交**」
- `custom_prompt`：含「**本模板不预埋未来限价单**」

### 过程中修正的一个自己的错误

第一轮（18:30）验证时我判定「B 没生效」，**判定错了**。原因：
`performance_context` 我只加到了 `ai_session_coordinator._model_output()` 里的
**局部变量 `prompt_payload`**（喂模型用），而**落库 payload 是 `ai_led_engine.py` 里
按 `context` 字段另行构造的 dict**。两者互不影响。
已补 `ai_led_engine.py:309` 的镜像（与第 308 行的 `strategy_instructions` 同样写法），
重建后 12:30 周期落库命中。

---

## 四、新发现（本轮暴露，均未改）

### 发现 1（运维级）：重建/重启后 AI 会话静默停摆

```
ai_session.state        = STOPPED          （正常应为 RUNNING）
ai_session.worker_alive = false
ai_session.last_reason  = 'not_started'    ← __init__ 的初始默认值，说明 start() 从未被调用
ai_session.mode         = None             （正常 TESTNET）
ai_session.venue        = 'simulated'      （正常 gate）
schedule.next_scan_at   = null
```

**但顶层 `session.state` 仍是 `RUNNING`、`active=true`** —— 那是 session_manager 的
持久化会话，AI worker 死了它照样 RUNNING。**不能拿它判断 AI 是否在跑。**

根因（`apps/api/main.py::startup_monitoring`）：

```python
if resume_setting:                                   # app_settings['monitoring.resume'] = true
    runtime.start(resume=True, user_initiated=False)   # ← 没带 enable_ai=True
```

`core/monitoring_runtime.py` 里只有 `if enable_ai:` 才调 `self.ai_coordinator.start(...)`。
实测 `monitoring.resume=true` + `desktop.auto_start=true` 都已设置，**依然无效**。
前端也没有任何自动启动逻辑（只有 `AITraderPanel` 的按钮走
`POST /v2/ai-session/start`）。

**恢复动作（每次重建后必做）**：
`POST /v2/ai-session/start?account_id=gate_testnet`

### 发现 1 的连带症状：runtime 长时间 `degraded`

AI 关闭期间 runtime 会走遗留固定策略路径（`core/strategy_monitoring.py::run`），
该路径抛 `sqlite3.OperationalError`，被 `strategy_monitoring.py:344-351` 吞成
`error_code`（**异常消息被丢弃**），于是只看到类名：

```
state=backoff  consecutive_failures=17
last_error='monitoring cycle degraded: OperationalError,OperationalError'
```

一旦 AI 会话起来（`service.ai_only=True`），`_run_cycle` 直接短路成
`AI_COORDINATOR_OWNS_DECISIONS` → `consecutive_failures=0`、`state=running`。
**"AI 停摆"和"runtime 长绿变红"是同一个根因的两个症状**，不要分头去查。

### 发现 2（数据级）：模板要扫的标的/策略，和实际启用的订阅不匹配

`strategy_instructions.execution` / `profile` 声明：

```
execution.symbols                = [BTCUSDT, ETHUSDT, SOLUSDT]
execution.universe_mode          = CUSTOM
profile.candidate_strategy_ids   = [liquidity_sweep, ema_trend, bollinger_squeeze]
```

而 `strategy_subscriptions` 的实际状态：

| symbol | strategy | enabled |
|---|---|---|
| ETHUSDT | bollinger_squeeze | **1** |
| SOLUSDT | ema_trend | **1** |
| BTCUSDT | 全部 6 个 | 0 |
| ETHUSDT | 其余 5 个 | 0 |
| SOLUSDT | 其余 5 个 | 0 |
| NVDA | opening_range_breakout | 0 |

→ **BTCUSDT 一条都没开；`liquidity_sweep` 一条都没开。** 模板点名的三个策略里
只有一个 `bollinger_squeeze`（在 ETHUSDT 上）真正在跑。

### 发现 3（模型级）：候选 8/8 无触发，模型 120/120 全 WAIT

唯二启用的规则在 12:30 的扫描结果：

```
v2_strategy:ETHUSDT:bollinger_squeeze  NO_TRIGGER  bars=160  "Strategy rules not triggered on current bar"
v2_strategy:SOLUSDT:ema_trend          NO_TRIGGER  bars=160  "Strategy rules not triggered on current bar"
```

整轮 8 个候选全部 `NO_TRIGGER`（6 个）或 `UNSUPPORTED`（2 个，
`opening_range_breakout` / `funding_extreme` 对 crypto 不支持）：

```
{"candidate_id": "...", "symbol": "BTCUSDT", "strategy_id": "bollinger_squeeze",  "status": "NO_TRIGGER"}
{"candidate_id": "...", "symbol": "BTCUSDT", "strategy_id": "ema_trend",         "status": "NO_TRIGGER"}
{"candidate_id": "...", "symbol": "BTCUSDT", "strategy_id": "funding_extreme",   "status": "UNSUPPORTED"}
...
strategy_readiness = {"status": "READY", "candidate_count": 8}
```

模型侧（最近 120 轮 MODEL 周期统计）：

| 指标 | 值 |
|---|---|
| `action` 分布 | **WAIT 120 / 120** |
| `confidence` 有效样本 | 111 |
| min / median / max | 0.0 / **65.0** / 85.0 |
| 众数带 | **65–69 带 89 次**（0 带 7、45 带 5、60 带 4、70 带 1、85 带 5） |
| `>= 70` | **6** |
| `< 70` | **105** |
| `trigger_completion_pct` | 36 轮有值，median = max = **45.0** |

**模型确实"看懂"了策略**（这部分是好的）：

```json
"strategy_analysis": {
  "strategy_id": "aggressive_impulse",          ← 等于账户激活模板
  "matched_conditions": ["Price above EMA20 on 5m/15m", "Volume ratio < 1.5 (no breakout)"],
  "missing_conditions": ["Breakout of resistance20(2428.63)", "Pullback to support20(2381.43) with confirmation"],
  "trigger_completion_pct": 45.0
}
```

`reason` 里引用的都是真实指标（ETH 现价 2406.29 / EMA20 2405.90 /
resistance20 2428.63 / support20 2381.43），`evidence_refs` 也规范
（`technical_snapshot:*` + `market_snapshot:*` + 一条新鲜 `news_revision:*`）。

> **但 `strategy_id` 的自报仍有编造成分**（近 120 轮统计）：
> `aggressive_impulse` 28、`ema_trend` 2、`aggressive_breakout` 2、`Momentum_Reversal` 3，
> 其余缺失。`aggressive_breakout` 与 `Momentum_Reversal` **不是系统里任何模板的 id**，
> 属模型虚构。近期周期（策略接线后）已基本收敛到真值 `aggressive_impulse`。

**但它每一轮都在 `reason` 里先复述 prompt 禁令、紧接着绕过禁令去 WAIT**，
并用 `confidence=65` 给自己盖章"不够格"。

### 发现 2 的更正（2026-09-16 晚）：订阅表不是唯一真相，且"启用"会**收窄**扫描

发现 2 写的是「模板要扫的标的/策略与实际启用的订阅不匹配」——现象对，
但**机制被我说反了**，据此给出的建议 #1 因此不可执行。查原文后的真实机制：

**（1）`set_strategy_subscription` 强制「一标的一策略」**（`core/v2_store.py:137-141`）：
```python
if enabled:
    db.execute("UPDATE strategy_subscriptions SET enabled=0 WHERE symbol=?", (symbol,))
```
所以「给 BTCUSDT 同时开 liquidity_sweep + ema_trend + bollinger_squeeze」**在写入口就被抹掉**，
只剩最后一条。建议 #1 字面不可执行。

**（2）真正的补齐发生在扫描器**（`core/trading/candidate_scanner.py:90-99`）：

```python
for row in rows:                       # 只收 enabled=1 且命中 allowed_strategies 的行
    ...
for symbol in symbols:                 # ← 该标的**一条 enabled 都没有**时
    if not any(s == symbol for s, _ in result):
        for strategy_id in sorted(allowed_strategies):
            result[(symbol, strategy_id)] = {...}   # 自动补齐
```

而且生产侧**从不传** `strategy_ids`（唯一调用点 `ai_session_coordinator.py:1517`
只给 `symbols` / `calibration_profile`）→ `allowed_strategies` 退化成**全部 6 个 STRATEGIES**。

→ **反直觉的结论：某个标的启用了 1 条订阅，它的扫描面就只剩这 1 条；
反而"一条都不启用"会让它被全部策略扫到。** 用库里数据验算：

| symbol | enabled 条数 | 实际被扫的策略 | 模板点名的 3 个策略覆盖 |
|---|---|---|---|
| BTCUSDT | **0** | 全部 6 个（含 2 个 UNSUPPORTED） | ✅ 3/3 |
| ETHUSDT | 1（bollinger_squeeze） | **只剩 bollinger_squeeze** | ❌ 1/3 |
| SOLUSDT | 1（ema_trend） | **只剩 ema_trend** | ❌ 1/3 |

候选数 6+1+1 = **8**，与 §四发现 3 实测的 8 个候选**逐条吻合** —— 机制确认无误。

**（3）顺带查出两个死配置字段**（同类"接了线但没人读"）：

| 字段 | 定义处 | 生产读取点 |
|---|---|---|
| `profile.candidate_strategy_ids` | `ai_strategy_book.py` 四模板都声明了 | **0 处** |
| `CandidateScanner.scan(strategy_ids=...)` | `candidate_scanner.py:264` | **0 处**（仅测试/scratch 用） |

即：模板声明的「候选策略宇宙」目前是**装饰性的**，运行时完全不采信。

**（4）所以要让模板声明真正生效，只有两条路**：

- **A（纯配置，2 条 UPDATE）**：把 `ETHUSDT.bollinger_squeeze` / `SOLUSDT.ema_trend`
  置 `enabled=0` → 三个标的全部走自动补齐 → 每个标的被 6 个策略扫到，
  模板点名的 3 个全部覆盖。代价：候选 8 → 18，其中 6 个是必然 UNSUPPORTED 的噪声
  （每标的 `opening_range_breakout` + `funding_extreme`），prompt 体积随之上涨。
- **B（改代码，接线死字段）**：把 `candidate_strategy_ids` 传进 `scan(strategy_ids=...)`，
  并让它成为自动补齐的**上界**。A+B 同时做才是「每个标的恰好 3 个声明策略、无噪声」
  （候选 9）。**只做 B 会更差**：BTCUSDT 从 6 降到 3，候选 8 → 5，反而更难出触发。

> 两条路都改变实际交易扫描面，**需要用户点头**，本轮未执行。

---

## 五、硬约束表（想调门槛前必读）

`core/trading/strategy_execution.py` 的 `LIMITS` 是**硬校验**，
`normalize_execution()` 越界直接抛 `STRATEGY_EXECUTION_INVALID_*`：

| 参数 | 当前值 | **允许区间** | 备注 |
|---|---|---|---|
| `min_confidence` | 70 | **(70, 100)** | ⚠️ **不允许低于 70**，想下调必须同时改 `LIMITS` |
| `min_net_rr` | 2 | (2, 10) | 不能低于 2 |
| `risk_per_trade_pct` | 0.25 | (0.01, 0.25) | 已在上限 |
| `leverage` | 5 | (1, 100) | |
| `max_positions` | 3 | (1, 5) | |
| `max_margin_pct` | 20 | (1, 80) | |
| `scan_interval_minutes` | 15 | (5, 15) | 模板曾声明 5；2026-09-16 起 `profile.signal_timeframe` 统一为 `15m`，落库值同步为 15，与运行时固定 15 分钟节奏**对齐** |

另有 `POLICY`（`core/trading/autonomous_strategy.py`）独立一份：
`min_confidence=70`、`min_net_reward_risk=2.0`、`max_leverage=100`、`daily_loss_limit_fraction=0.015`。

> **修正一条旧记录**：项目记忆里写过「executive 的 leverage / max_notional /
> risk_per_trade / max_positions 都没被读取，`POLICY` 是唯一权威」——**这条已不准确**。
> 实测模板的 `execution` 块（`leverage=5`、`min_confidence=70`、`min_net_rr=2`、
> `max_positions=3`、`risk_per_trade_pct=0.25`、`max_notional_usdt=1000`）
> **确实进了 prompt，并被 `ai_led_engine.py` 的 `normalize_execution` / `size_position`
> 用于仓位计算**。是否每一条都在最终准入上生效，尚未逐条查实（未验证，别当成已确认）。

### D（周期对齐）精确落点

- 模板：`execution.scan_interval_minutes`（前值 5，2026-09-16 起落库为 15）。
- 运行时：`cycle_interval_seconds = 900.0`、`scan_interval_seconds = 900.0`，
  调度 `alignment = "minute % 15 == 0"`，`catch_up = false`。
- 结论（更正后）：`AIStrategyBook` 的 `_profile_signal_interval` 会用
  `profile.signal_timeframe` **覆盖** `execution.scan_interval_minutes`（读/写两侧都覆盖）。
  把模板 `signal_timeframe` 从 `5m` 改成 `15m` 之后，声明值与实际节奏不再打架。

---

## 六、下一步建议（按性价比排序，均需用户点头）

> **执行状态（2026-09-16）**：第 1/2/3 项**已落地**，并且用户追加了一条：
> 策略正文有问题，可以重写。重写与落地细节见 **§八**。第 4 项经用户明确
> **确认不做**（理由见下）。第 5/6 项未动。

| 优先级 | 动作 | 影响面 | 状态 |
|---|---|---|---|
| 1 | **A+B（替代字面方案）**：A = 把 `ETHUSDT.bollinger_squeeze`、`SOLUSDT.ema_trend` 置 `enabled=0`；B = 把 `profile.candidate_strategy_ids` 接进扫描调用 | A 改订阅表 2 行；B 改 coordinator 1 处调用 + 1 个新方法 | ✅ **已完成**，候选 18 → 9（剔除 3 个策略 × 3 标的的噪声，零未知策略） |
| 2 | 修发现 1：自动恢复时带上 `enable_ai`（持久化一个布尔 `ai.autonomous_resume`） | `apps/api/main.py` + `apps/api/v2.py` + `sqlite.py` | ✅ **已完成**，实测重启后无人工干预即 `state=RUNNING` |
| 3 | 给 `strategy_monitoring.py` 的 `except` 补上 `str(exc)` | 1 行日志 | ✅ **已完成** |
| 4 | 放宽 `min_confidence` | — | ❌ **用户确认不做**。代码级证明：`validate_entry` 仅在 `action ∈ {OPEN_LONG, OPEN_SHORT}` 时才被调用（`ai_led_engine.py:610-613`），120/120 WAIT ⇒ 它从未被调用 ⇒ 降门槛产生**零订单** |
| 5 | D：让运行时读 `execution.scan_interval_minutes` | 改动调度语义 | 部分收敛：模板 `profile.signal_timeframe` 统一为 `15m`，与运行时 15 分钟节奏**对齐**，歧义消除 |
| 6 | 提升模型（9B Q4 on 8 GB → 更大/更高精度，或换更强 provider） | 硬件受限，~8 tok/s | 未动，仍是"模型从不 commit"的根本手段 |

**关键提醒**：第 1 项（A+B）和第 6 项才是真正指向"能开单"的杠杆。
第 4 项看起来很诱人（就差 5 分），但已被证明**不产生订单** —— 别把它当成解法；用户据此确认不做。

---

## 七、复现命令

```bash
# 直连 sidecar（端口固定 18765）
curl -s http://127.0.0.1:18765/health

# AI 会话真实状态（只看 ai_session，别信顶层 session）
curl -s 'http://127.0.0.1:18765/v2/ai-session/status?account_id=gate_testnet'

# 手动恢复 AI 会话（每次重建后必做）
curl -s -X POST 'http://127.0.0.1:18765/v2/ai-session/start?account_id=gate_testnet'

# 等一个周期落地并逐项判定
python scratch/verify_post_cycle.py 2026-09-16T12:29 900

# confidence 分布 / action 分布统计
# （见本报告第三节数据，脚本 scratch/verify_perf_and_confidence.py）
```

数据库：`D:\RJ\AI Market Analyst\data\market_analyst.sqlite3`（只读用
`file:...?mode=ro`）。

---

## 八、A+B + 策略重写（2026-09-16 落地）

### A — 清掉会造成误判的订阅行

`strategy_subscriptions` 里 `ETHUSDT.bollinger_squeeze` 与 `SOLUSDT.ema_trend`
曾被置为 `enabled=1`，经 `set_strategy_subscription` 的
**"一标的一策略"**语义（`UPDATE ... SET enabled=0 WHERE symbol=?`）会顺带压掉同标的
其他策略。已用正确的 store API 全部置 `enabled=0`。现在该表 **14 行全部 `enabled=0`**。

> **反直觉要点**：`enabled=0` 不等于"不扫描"。扫描器 `_subscriptions()` 对
> **零启用订阅**的标的会**自动补齐**允许策略集。所以在 B 落地前，
> 全部置 0 得到的是 `3 标的 × 6 个注册策略 = 18 对`（含噪声）。

### B — 把 `profile.candidate_strategy_ids` 接进扫描

- `ai_session_coordinator.py` 新增 `_declared_candidate_strategies(account_id)`，
  返回 `profile.candidate_strategy_ids`（缺失返回 `None`，即保持旧行为）。
- 扫描调用改为 `strategy_ids=self._declared_candidate_strategies(account_id)`。

**实测收敛（真实调用 `CandidateScanner._subscriptions()`，对生产库副本）**：

| 口径 | 对数 | 明细 |
|---|---|---|
| BEFORE（`strategy_ids=None`） | **18** | 3 标的 × 全部 6 个注册策略 |
| AFTER（`strategy_ids=profile 声明`） | **9** | 3 标的 × `{bollinger_squeeze, ema_trend, liquidity_sweep}` |

未知策略 before/after 均为 `∅`。剔除掉的噪声：`funding_extreme`、
`opening_range_breakout`、`session_vwap` —— 模板从未声明它们，
旧口径下它们稳定产出 UNSUPPORTED 条目，纯占 token。

### 策略重写（用户追加要求）

重写前策略正文的**结构性缺陷**：sections 从未描述 `validate_entry` 的
11 道机器闸门，模型只能靠猜；且 `5m` 声明与实际 15 分钟节奏冲突；
`conservative_defense` 的 `target_r_multiples=[2.0,2.6]` **低于它自己的
`minimum_net_rr=2.2`**（自相矛盾）。

改动：

1. `TEMPLATES[0]`（aggressive_impulse）：`signal_timeframe 5m → 15m`、
   `context_timeframes ["15m","1h"] → ["1h"]`、`target_r_multiples [1.8,2.6] → [2.2,3.0]`。
2. `TEMPLATES[3]`：`target_r_multiples [2.0,2.6] → [2.4,3.0]`，修复自相矛盾。
3. 重写 `DEFAULT_SECTIONS` 与 `TEMPLATES[0]` 的 role / frequency /
   entry_standards / decision_process / custom_prompt：
   - 把"价格是否已到位"锚定到**客观字段** `candidates[].status`（TRIGGERED /
     NO_TRIGGER / UNSUPPORTED），不再依赖主观描述；
   - 补上 11 道机器闸门的**输出前自检清单**；
   - 统一盈亏比语义：**名义 ≥ 2.2 ⇒ 扣费后净 ≥ 2.0**；
   - 置信度纪律：合格入场落 70~85，禁止用 65 之类固定值占位。
4. `autonomous_strategy.py` SYSTEM_PROMPT 第 8 条同步澄清（名义 RR ≥ 2.2、
   `requested_risk_fraction` 必须填 0.002）。

**四模板一致性复核**（全部通过）：

| 模板 | signal_tf | ctx | target_r | net_rr | decision 含「不得」 |
|---|---|---|---|---|---|
| aggressive_impulse | 15m | ['1h'] | [2.2, 3.0] | 2.0 | ✅ |
| aggressive_breakout | 15m | ['1h'] | [2.0, 3.0] | 2.0 | ✅ |
| conservative_pullback | 15m | ['1h'] | [2.2, 3.0] | 2.2 | ✅ |
| conservative_defense | 15m | ['1h'] | [2.4, 3.0] | 2.2 | ✅ |

**落库**：`ai_strategy_instructions`（gate_testnet）最高修订 = **rev 41**，
`sections` 的 SHA1 指纹 `d7bf843b6dd8` **与源码 `TEMPLATES[0]` 完全一致**
（role 109 / frequency 119 / entry_standards 787 / decision_process 670 /
custom_prompt 481 字符，逐段比对 OK）。

> **为什么必须落库**：`_sections_for_template` 在频率短语冲突时用内建段落，
> 但**会用存储的 `custom_prompt` 覆盖内建版**。只改内建定义不落库，
> 重写的 custom_prompt 会被旧文本还原。
>
> **修订历史里有冗余**：rev 39 是中间态（新 profile + 旧 decision/custom），
> rev 40 与 rev 41 **内容完全相同** —— 那是 persist 脚本连跑两次的产物，
> 不是失控写入。`AIStrategyBook.save()` 才是唯一写修订的路径（`active()` 只读），
> 每调用一次只 +1，不存在自我放大。

### 重建后的端到端实测（2026-09-16 21:24 构建 / 21:31 首个周期）

| 检查项 | 期望 | 实测 |
|---|---|---|
| 自动恢复（未手动 start） | `RUNNING` | ✅ `state=RUNNING` / `generation=3` |
| 安装产物 = 构建产物 | 哈希一致 | ✅ exe `d2402481…`、backend `12fb9d1a…` 逐位一致 |
| 进程起于新构建 | 启动时刻 = 构建时刻 | ✅ 21:24:51 / 21:24:53 |
| 落库策略 | rev ≥ 41 / 15m | ✅ rev 41、`signal_tf=15m`、`scan_interval=15` |
| 候选池 | 9 条、零噪声 | ✅ `candidate_count=9`，`{bollinger 3, ema 3, sweep 3}`，噪声 ∅ |
| 周期对齐 | 下一个 15 分钟整点 | ✅ `next_scan_at=13:45:00Z` |
| 策略进模型 | 周期内含 rev 41 | ✅ `strategy_instructions.revision=41` |
| prompt 未截断 | `prompt_truncated=false` | ✅ `prompt_tokens_estimated=20098` |

**模型服从度确有改善**（与重写前对比）：

- `strategy_analysis.strategy_id = "aggressive_impulse"` —— **真值**（此前会瞎填
  `Momentum_Reversal` 之类不存在的 id）；
- `trigger_completion_pct = 0.0`、`missing_conditions` 明写
  "所有候选策略状态为 NO_TRIGGER" → 证明它真的在读**客观 `status` 字段**；
- `reason` / `human_message` 同时引用了新 custom_prompt 原句与具体价位
  （EMA20 / resistance20 / support20）。

**9 条候选全部 `NO_TRIGGER`，所以本轮 WAIT 是正确结论**，不是新的阻断。

### 真正的根因：术语断裂（PROPOSAL vs TRIGGERED，已修复 rev 42）

深入 `ai_strategy_candidates`（1695 行，09-13~09-16）后发现，**之前"候选无触发"的结论是错的**：

- 候选状态机里，"策略已触发、价格已到位"的最高状态叫 **`PROPOSAL`**
  （`strategies.py:505` / `candidate_scanner.py:314`）。**`TRIGGERED` 根本不在候选状态机里**，
  它是 trade plan 阶段的状态（`trade_plan_contract.py:525`）。
- 历史 1695 条候选里 **`PROPOSAL` 有 13 条**（都带 `direction_bias`=方向、`rr=2.0`），
  `TRIGGERED` 0 条。策略**确实算出了信号**，不是"没触发"。
- 但上一轮重写的正文（rev 41）教模型认 `status=TRIGGERED`，对 `PROPOSAL` 只字未提；
  且 SYSTEM_PROMPT 写 "candidates are optional reference evidence, not entry prerequisites"。
- **铁证**：13:15 那轮候选有 2 条 PROPOSAL（BTC/SOL ema_trend，SHORT，rr=2.0），
  模型 reason 却无视它们、还在找 LONG。120/120 WAIT 正是模型不认 PROPOSAL。

**已修复（落库 rev 42 + 重建）**：
1. sections 的 `TRIGGERED` → `PROPOSAL`，补 `UNSUPPORTED/WARMING_UP/STALE_DATA/GAP_DETECTED`=忽略；
2. SYSTEM_PROMPT 第一段改为 "status=PROPOSAL is an objectively triggered signal, adopt its
   direction_bias and rr"。
3. 离线验证：`build_strategy_system_prompt()` 输出含 PROPOSAL、无 TRIGGERED，PASS。

> **结论修正**：开不了单**不是模型能力问题（9B Q4 不是主因）**，是文案术语 bug。
> 换更强模型从"第一优先级"降为"上限手段"。

### 仍存在的阻断（下一档要治，不能靠降门槛）

**`confidence` 仍是 `null`。** 新 custom_prompt 已明确要求"必须给出具体数值"，模型仍返回
`raw.confidence = null`（`think=False`、`eval_count=520`、`schema_enforcement` 走
`json_mode_local_validation` 降级路径 —— 语法未被服务端强制）。

这不是"模型没填"的观感问题，而是**代码级硬拒**：

```python
# core/trading/autonomous_strategy.py:258-259
confidence = number(extra.get("confidence"))
if confidence is None or not config.get("min_confidence", POLICY["min_confidence"]) <= confidence <= 100:
    return "AI_CONFIDENCE_BELOW_POLICY"
```

→ 即使某轮候选 `PROPOSAL` 且模型给出 `OPEN_LONG`，只要 `confidence=null` 就会被拒。
**这与"降低门槛"无关**（`min_confidence` 只影响比较下界，`None` 分支先短路）。
可选的治法是让 `AI_ACTION_SCHEMA` 里 `confidence` 成为必需字段并在本地降级路径上也强制补全，
或让 `ai_led_engine` 在 `action ∈ OPEN_*` 且 confidence 缺失时触发一次定向 repair —— 需用户点头。

### 已知红灯（与本次改动无关，未修）

- `tests/test_autonomous_news_strategy.py::test_news_loader_adds_bounded_market_context_for_discovered_altcoin`
  —— 断言 `_news_revisions()` 会产出 `scope == 'MARKET_WIDE'`，但该字段**从未实现**。
  这正是"发现 3"里说的 `/health` 长绿变红的隐患源之一：`news_refresh` 与
  `ai_session` 的 scope 语义没有统一。该路径只在 `universe_mode=ALL`
  且出现新发现标的时触发；当前策略是 `CUSTOM` 三大主流币，不触发。
- `tests/test_strategy_cadence_universe.py` 里原先有一条断言
  `coordinator._market_universe` 接线的测试，但该属性**在 coordinator 中不存在**
  （`MarketUniverse` 是独立组件，从未接进会话）。已改写为验证
  `_allowed_symbols` 的真实契约（授权约束 + 监控策略 + 三大主流币回落 + 上限 5）。
