# 「一直开不了单」根因诊断报告

- 诊断时间：2026-09-15 15:00–15:22（本地）
- 数据根：`D:\RJ\AI Market Analyst\data\market_analyst.sqlite3`
- 账户：`gate_testnet`（`mode=TESTNET`，远端权益 ~50172.78 USDT）
- 方法：只读查库 + 按生产代码路径复现（未改动任何代码、未改配置）

---

## 一、结论

**开不了单不是单点故障，是 6 层阻断叠加，其中最底层、也是真正的因，是「策略候选永远进不了 PROPOSAL」。**
AI 决策阶段（`AI_MODEL`）无论通过与否，都没有可执行候选可用 → 模型只能输出 WAIT；同时推理侧还在超时。

| # | 阻断点 | 严重度 | 状态 |
|---|---|---|---|
| 1 | 策略候选永远停在 `WARMING_UP`（K线身份污染） | **致命（根因）** | 持续 |
| 2 | `opening_range_breakout` 被订阅到加密标的 | 高 | 持续 |
| 3 | 模型连续 14+ 轮 WAIT（对"预埋限价单"理解错位） | 高 | 持续 |
| 4 | Ollama 推理贴着/超过 120s 预算 | 高 | 持续 |
| 5 | 授权记录 `mode=PAPER` 且已过期 5 天 | 中（潜伏，一旦真开仓就撞） | 未触发 |
| 6 | Gate 订单簿全量判为不可信 | 中 | 持续 |

---

## 二、逐条证据

### 1. 根因：K线身份污染 → 候选永远 `WARMING_UP`

用项目自身的 store 复现（`SQLiteStore.list_market_bars`）：

| 标的 | 15m 返回行数 | `instrument_key` 分布 | 过滤后 closed | **唯一时间戳** | warmup 门槛 |
|---|---|---|---|---|---|
| BTCUSDT | 375 | `legacy:unknown:BTCUSDT:UNKNOWN:last`=132 / `gate:perpetual:BTC_USDT:USDT:last`=124 / `gate:perpetual:BTC_USDT:USDT:mark`=119 | 241 | **123** | 60 |
| ETHUSDT | 375 | 同上（ETH） | 241 | **123** | 60 |
| SOLUSDT | 375 | 同上（SOL） | 241 | **123** | 60 |

同一根经济意义上的 15m K 线，被写成 **3 个不同 `instrument_key`**：
- `legacy:...:UNKNOWN:last`（遗留写入，来源不明、结算币 UNKNOWN）
- `gate:perpetual:...:USDT:last`（权威口径）
- `gate:perpetual:...:USDT:mark`（**标记价**，不是成交价）

`latest_bars` 只在 `(instrument_key, bar_start)` **内**去重，跨身份不去重 → 策略拿到 241 条、只有 123 个唯一时间戳 → 触发 `core/quant/strategies.py:393`：

```python
if len(closed) < self.warmup_bars or len({b.timestamp for b in closed}) != len(closed):
    self.last_status = "WARMING_UP"
```

**注意：这行日志是误导的。** 它固定打印 `Insufficient warmup bars ({len(closed)} < {warmup_bars})`，
所以库里看到的 `Insufficient warmup bars (112 < 60)` 里 112 并不小于 60 —— 真正触发的是后半段「去重后时间戳数 ≠ 总数」。这条日志建议一并修掉。

**候选表实际结果**（`ai_strategy_candidates`，共 1574 条）：

| status | 条数 | 最近一条 |
|---|---|---|
| NO_TRIGGER | 969 | 09-15 12:30 |
| UNSUPPORTED | 453 | 09-15 15:12 |
| WARMING_UP | 120 | 09-15 15:00 |
| IDLE | 21 | 09-14 16:05 |
| **PROPOSAL** | **9** | **09-15 12:15** |
| STALE_DATA | 5 | 09-13 18:08 |
| GAP_DETECTED | 3 | 09-15 12:05 |

**PROPOSAL 全期只出现过 9 次，最近一次在 12:15，此后 3 小时以上 0 条。** 没有 PROPOSAL，`ai_led_engine.py:646-656` 的候选匹配拿不到东西，模型也就没有任何可执行的进场依据。

### 2. `opening_range_breakout` 被订阅到加密标的

`core/quant/strategies.py:935`：

```python
class OpeningRangeBreakout(BaseStrategy):
    supported_markets = ("equity",)
```

而 `strategy_subscriptions` 里 `BTCUSDT / opening_range_breakout` 是 `enabled=1`。
结果（原始记录）：

```
rationale: "Market type crypto is not supported by opening_range_breakout"
status: UNSUPPORTED
```

它**永远不会**产出一个候选。另外两个订阅 `ETHUSDT/bollinger_squeeze`、`SOLUSDT/ema_trend` 的 `supported_markets` 含 crypto，是被第 1 条卡住。

### 3. 模型连续 WAIT

`ai_led_cycles` 最近 20 条：14 条 `WAITING`（`model_result=WAIT`、`decision_origin=MODEL`、`operational_state=MODEL_DECISION`），4 条超时/不可用，2 条 BLOCKED。WAIT 理由高度雷同：

> 当前 BTC/USDT 处于 5m 震荡整理，价格位于 EMA20 与布林带中轨之间。…当前无清晰方向性信号，且未触及关键支撑/阻力位，暂不执行市价单。

但策略手册 `core/trading/ai_strategy_book.py:35` 明确要求的是：

> 只要价格未发生瞬时放量突破，**严禁无休止 WAIT！必须优先在 5m EMA20 支撑位或 support20 预埋做多限价单**（OPEN_LONG）…TTL 设为 15 分钟

**模型把"预埋限价单"理解成了"等价格到达支撑位再挂单"**，于是永远在等一个"明确测试"信号 —— 而 `预埋` 的本意是"现在就把限价单挂在支撑位"。这是 prompt 语义错位导致的死循环。
`ai_strategy_book.py:106` 的注释自己也写着这个现象：「teaches the model to emit the same safe WAIT repeatedly」。

### 4. Ollama 推理超时

`ai_runtime_diagnostics` 聚合（全表）：

| state | code | 次数 | 最近 |
|---|---|---|---|
| DATA_BLOCKED | ORDER_BOOK_UNTRUSTED | 158,482 | 09-15 15:12:40 |
| SYSTEM_BLOCKED | **SMART_MODEL_UNAVAILABLE** | **30** | 09-15 15:07:15 |
| SYSTEM_BLOCKED | AUTHORIZATION_REQUIRED | 13 | 09-10 12:05 |
| SYSTEM_BLOCKED | SESSION_NOT_EXECUTABLE | 5 | 09-13 04:00 |
| SYSTEM_BLOCKED | MODEL_TIMEOUT_DISCARDED | 2 | 09-15 15:12:29 |
| SYSTEM_BLOCKED | AI_SESSION_NOT_ENABLED | 1 | 09-13 18:07 |
| SYSTEM_BLOCKED | MARKET_DATA_UNAVAILABLE | 1 | 09-15 13:02 |

实测（Ollama 0.34.0 正常在跑、`qwen3.5:9b` 已安装，模型卡原生上下文 262144）：

| 场景 | `prompt_eval_count` | 生成 tokens | 提示词求值 | **总耗时** | 预算 |
|---|---|---|---|---|---|
| 极小 prompt | 28 | 10 | — | 5.6s | 120s |
| 真实 prompt @ num_ctx 8192（冷启动） | **3185**（被截断） | 737 | 1747ms | 23.8s | 120s |
| 真实 prompt @ num_ctx 8192（驻留） | **3185**（被截断） | 737 | 61ms | 17.8s | 120s |
| 真实 prompt @ num_ctx 16384（冷启动） | 11906 | 599 | 8275ms | 38.1s | 120s |
| 真实 prompt @ num_ctx 16384（驻留） | 11906 | 599 | 84ms | 22.8s | 120s |
| **最坏 prompt @ num_ctx 32768** | **26759** | （截断答案预算） | 24765ms | 34.8s | 120s |

**配置沿革（更正初稿的表述）**：`git diff` 显示**上一次提交**（`6518566`）里这一行是
`timeout=55, context_length=16384, max_tokens=1800`，而本轮开始时**工作区**已被改成
`timeout=120, context_length=8192, max_tokens=900`（未提交）。也就是说有人把窗口从 16384 下调到 8192、
同时把超时从 55s 放宽到 120s —— **窗口变小、超时变长**，正好掩盖了截断（模型不报错，只是答非所问）。
这比初判更容易误诊，务必以「送入 token 数」而非「是否报错」判断窗口是否够。

两个放大器：
- **`OllamaProvider` 从不发 `keep_alive`** → 走 Ollama 默认 5 分钟；而 `DEFAULT_CYCLE_INTERVAL_SECONDS=900`（15 分钟）→ **每轮周期都要重新加载 6.1 GiB 权重**。`/api/ps` 实测为空（0 个驻留模型）。同一调用冷启动提示词求值 **8275ms** vs 驻留后 **84ms**（98 倍）。
- **两条路径上下文不一致**：交易路径曾被硬编码为 `8192`，而 `core/consult.py` 的 `OllamaProvider()` 走环境默认 `8192`；`scratch/audit_live_strategy.py` 与 `scripts/smoke-ai-news-strategy.py` 又都用 `16384`。Ollama 在 `num_ctx` 变化时重载模型 → 咨询/翻译与交易周期交替时反复抖动（`phase6_events` 近 48h 新增 1215 条，翻译调用很频繁）。

### 5. 授权记录失效（潜伏硬门）

`trading_authorizations` 全表只有 1 条：

| 字段 | 值 |
|---|---|
| authorization_id | `auth_7ca80af8f0e6` |
| account_id | `gate_testnet` |
| **mode** | **`PAPER`**（账户是 `TESTNET`） |
| decision_path | AI_LED |
| allowed_instruments | `["BTC_USDT"]` |
| max_leverage | 20 |
| valid_from | 2026-09-09T16:25:44Z |
| **expires_at** | **2026-09-10T16:25:44Z ← 已过期 5 天** |
| status | ACTIVE（陈旧状态，未随过期更新） |

且**最近所有周期的 `authorization_id` 都是 `null`** —— 当前没有任何有效授权绑定到周期。
目前它还没拦（`AUTHORIZATION_REQUIRED` 最近一次是 09-10），但 `ai_led_engine` 构造 `OrderIntent` 时会把 `authorization_id=context.authorization_id` 透传到 `execution_gateway`，**一旦模型真要开仓，就会撞这道门**。

### 6. 订单簿全量不可信

`ai_runtime_diagnostics` 里 `ORDER_BOOK_UNTRUSTED` **158,482 次且仍在持续**（最近 15:12:40），BTC/ETH/SOL 三个标的全是 `sequence_status=OUT_OF_ORDER`。写入点在 `core/monitoring_runtime.py:1144`（来自 `core/gate_stream.py` 的 Gate WS 序列校验）。

影响面：`ai_led_engine.py:805-809` 的 `AI_ORDER_EXCEEDS_OBSERVED_DEPTH` 依赖 `market_snap["depth_contracts"]`；`OrderSelectionPolicy` 选市价/限价也依赖盘口 bid/ask 与 `liquidity_ok`。订单簿不可信会持续污染这两个判断。

---

## 三、需要更正的两处初判（避免误伤）

| 现象 | 实际结论 |
|---|---|
| `gate_credentials.testnet=0, live_enabled=0` | **不是**阻断源。那是**遗留表**，secret 已写成 `[MIGRATED_TO_DPAPI]`。真实凭证在 `secure_account_credentials`：`account_id=gate_testnet`、`**testnet=1**`、`updated_at=2026-09-10T11:07:32Z`。 |
| `news_events` 最新只到 2026-09-08 | **不是**新闻过期。周期读的是 `store.list_event_evidence` → `phase6_events`，近 48h 有 **1215 条**，其中 **87 条**含 `BTCUSDT`。新闻管道是新鲜的。`news_events`(78 行) 是遗留表。 |

---

## 四、修复建议（按优先级）—— **已按此优先级执行，见第六节**

| 优先级 | 动作 | 风险 |
|---|---|---|
| P0 | 让策略评估只吃**单一权威身份** K线（`gate:perpetual:*_USDT:USDT:last`），把 `legacy:*` 与 `:mark` 从评估输入里排除 | 改动行情读取语义，**需白茶确认后动** |
| P0 | 修 `core/quant/strategies.py:395` 的误导日志（区分"条数不足"与"时间戳重复"，并打印唯一数） | 低，纯日志 |
| P1 | 撤掉 `BTCUSDT / opening_range_breakout` 订阅（equity 策略不适用于加密） | 低，配置 |
| P1 | 修 prompt：把「预埋限价单」明确写成「**现在就**把限价单挂在 support20 / EMA20，TTL 15 分钟」，消除"等价格到位"的误读 | 中，需回归测试 |
| P1 | `OllamaProvider` 发 `keep_alive`（如 `"30m"`）+ 统一 `num_ctx`（消灭 8192/16384 抖动）+ 把 `timeout` 与周期预算对齐 | 中，需实测 |
| P2 | 重建失效授权（`mode=TESTNET`、未过期、标的与杠杆按当前意愿设定） | 中，涉及授权语义 |
| P2 | 修 Gate 订单簿 WS 的 sequence 校验（`OUT_OF_ORDER` 刷了 15.8 万次） | 中 |

---

## 五、验证方法（可复现）

```bash
# 复现根源：扫描器看到的 K 线身份污染
#   list_market_bars 返回 375 行 / 3 个 instrument_key / 唯一时间戳 123
# 复现推理耗时：按交易路径参数（16384/1800/120s）发一次请求
# 复现超时：在模型被驱逐后立即请求
```

详见本轮使用的只读诊断脚本 `scratch/diag_no_order.py`。

---

## 六、已执行的修复（2026-09-15，按 P0→P1 优先级）

### P0-1 行情身份收敛 —— 已修

新增 `core/instruments.py`：

```python
TRADING_BAR_VENUE = "gate"
TRADING_BAR_MARKET_TYPE = "perpetual"
TRADING_BAR_PRICE_TYPE = "last"      # 权威成交价，排除 :mark 与 legacy:*

def trading_bar_filters() -> dict[str, str]: ...
def read_trading_bars(store, symbol, timeframe, *, limit) -> list: ...
```

6 个交易路径读点全部改走 `read_trading_bars`：

| 文件 | 位置 |
|---|---|
| `core/trading/candidate_scanner.py` | `_bars()` —— 直接决定候选产出 |
| `core/trading/ai_session_coordinator.py` | `:413` 回退取价 |
| `core/trading/trader_capabilities.py` | `_read_window_bars` |
| `core/trading/ai_calibration.py` | `_closed_bars` |
| `core/trading/autonomous_strategy.py` | `technical_context` |
| `apps/api/main.py` | `_v12_cached_bundle` |

**实测效果（BTCUSDT 15m）**：

| | 修复前 | 修复后 |
|---|---|---|
| `closed` 行数 | 177 | 239 |
| 唯一时间戳 | **66** | **239** |
| 判重 | 必挂 → 永远 `WARMING_UP` | 通过 |

`CandidateScanner.scan()` 真跑已从「永远 WARMING_UP」变成真实规则判定：`UNSUPPORTED(BTCUSDT/opening_range_breakout)`、`NO_TRIGGER(ETHUSDT/SOLUSDT)`。

### P0-2 误导日志 —— 已修

`core/quant/strategies.py:393` 拆成两条独立判据，重复时间戳明确报
`Duplicate bar timestamps (N rows over M distinct bars): the series mixes more than one bar identity`。
新增回归 `tests/test_bar_identity_contract.py`（4 条，全过）。

### P1-3 撤销无效订阅 —— 已修（数据侧，可回滚）

`core/v2_store.py::set_strategy_subscription` 加市场类型守卫：
启用时校验 `strategy_cls.supported_markets` 是否包含 `market_type_for_symbol(symbol)`，不匹配即 `ValueError`。
`BTCUSDT/opening_range_breakout` 已置 `enabled=0`（**行保留，可回滚**）。守卫验证：拒绝重新启用，且不误伤 `BTCUSDT+ema_trend`、`NVDA+opening_range_breakout`。

### P1-4 「预埋限价单」prompt 语义 —— 已修

`core/trading/ai_strategy_book.py`：`DEFAULT_SECTIONS["custom_prompt"]` + 4 个 TEMPLATES 的 `entry_standards`
全部改写为明确的「**本轮立刻**把限价单挂在关键位上等待被动成交，不是等价格走到关键位再挂」。
校验：4 模板 keys 匹配、长度 ≤390、均含「现在就挂」语义。

### P1-5 Ollama 窗口与驻留 —— 已修（本轮新增，证据最硬）

**修了三处**：

**(a) 窗口从 8192 → 32768。** 生产代码硬编码 `context_length=8192`，而按 Ollama 自报的
`prompt_eval_count` 实测：真实 prompt 送入 **11906** token，最坏 **26759** token。

| num_ctx | 真实送入 | 被截掉 | 结论 |
|---|---|---|---|
| 8192（修复前） | **3185** | **11906 → 3185，丢 73%** | 模型在读一个被撕碎的 prompt |
| 16384（中途取值） | 11906 | 无 | 典型 prompt 够、最坏 prompt 溢出 −11275 |
| **32768（最终）** | 11906 / 26759 | 无 | 典型余量 20562，最坏余量 5109 |

**(b) `OllamaProvider` 新增可配置 `keep_alive`**（`core/ai/ollama.py`）：
- 新增入参 + `OLLAMA_KEEP_ALIVE` 环境变量；`None` 时**完全不发该字段**（咨询/翻译保持 Ollama 原驱逐策略，不占显存）。
- 交易 session 显式传 `"45m"`（> 15 分钟周期）。实测 `/api/ps`：`expires_at` 从 5 分钟的 `23:41` 延长到 `00:21`，权重常驻。
- 新增 `loaded_models()`（打 `/api/ps`）供验证驻留，纯诊断、任何传输失败降级为空列表，不会抛进交易周期。

**(c) 把形同虚设的字符预算换成 token 级上下文守卫。**
旧代码 `if len(messages[1]["content"]) > 36000: raise AI_INPUT_BUDGET_EXCEEDED`
在 8192 窗口下**永远不会触发**（36000 字符的 JSON 约 2 万 token）。新守卫按
`_estimate_tokens()`（CJK 逐字形、非 CJK 按 1.75 字符/token，经两次实测标定）比较
`prompt + reserve` 与 `provider.context_length`，超出即抛 `AI_INPUT_BUDGET_EXCEEDED` 并说明会被截断——
**宁可明确报错，也不再静默截断**。

> 标定依据：Ollama 自报 `prompt_eval_count` 实测 26759 token / 49053 字符（≈1.83 字符/token）、
> 11906 token / 23581 字符（≈1.98 字符/token）。朴素的 `len//4` 低估约 2 倍，所以旧的字符预算不可用。

**新增可观测性**：`generate_json` 的 metadata 增加 `prompt_eval_count`/`eval_count`/
`prompt_eval_duration_ms`/`load_duration_ms`；周期记录 `model_inference_settings` 写回这些计数，
并给出 `prompt_truncated` 布尔（Ollama 自报的求值数显著低于我方估算即视为截断）。
从此每一轮 cycle 都自带「有没有被截断」的证据。

> **注意别误判**：`GET /v2/ai-session/status` **按设计**不发起 provider health 调用
> （`_health(force=False)` 直接返回 `NOT_CHECKED`，避免读端点做无界外部调用），
> 所以在那里**看不到** `context_length` / `keep_alive` / `prompt_eval_count`。
> 这些字段只在**真跑过一轮 cycle** 之后才会写进周期记录。别据此判断"代码没更新"。

**端到端验证**（`scratch/verify_p15_end_to_end.py`，走真实 provider 构造路径 + 真 prompt + 真 schema）：

| 场景 | 耗时 | `prompt_eval_count` | 截断 | 驻留 | 判定 |
|---|---|---|---|---|---|
| 典型 prompt 冷启动 | 54.9s | 15577 | 无 | 是 | PASS |
| 典型 prompt 驻留 | 24.6s | 15577 | 无 | 是 | PASS |
| **最坏 prompt 冷启动** | 74.0s | **26775** | 无 | 是 | PASS |
| **最坏 prompt 驻留** | 48.7s | **26775** | 无 | 是 | PASS |

最坏情况 74s 仍在 120s 预算内（修复前是「两轮生成各 60s+ → 必然超时」）。

**回归测试**：`tests/test_model_context_budget.py`（18 条）全过。

### P1-5 附带修复：`read_trading_bars` 过严（本轮发现并修，重要）

P0-1 的收敛一开始写成了**硬性要求** `venue=gate, market_type=perpetual, price_type=last`。
实测（`scratch/probe_fixture_bar_identity.py`）：

| 写入 provider | 实际落库身份 | Gate 过滤可见 |
|---|---|---|
| `local_repair_fixture` | `legacy:unknown:BTCUSDT:UNKNOWN:last` | **0 行** |
| `local-paper-fixture` | `legacy:unknown:BTCUSDT:UNKNOWN:last` | **0 行** |
| `local-replay-fixture` | `legacy:unknown:BTCUSDT:UNKNOWN:last` | **0 行** |
| `gate_testnet` | `legacy:unknown:BTCUSDT:UNKNOWN:last` | **0 行** |

**`upsert_market_bars` 无论传什么 provider，只要不显式给身份参数，都写 legacy 身份。**
所以硬过滤会让 **paper/模拟账户和全部测试夹具的 K 线彻底不可见** → 校准与候选扫描在模型被调用前就被掐断。
全量测试证实：6 个失败中 **4 个**由此引起（`test_repair_v12_luna`、`test_trader_reliability_v13`、
`test_trader_reliability_v14`、`test_v12_api`）。

修法（`core/instruments.py`）：**优先 Gate `last`；若该身份无数据，则收敛到「单一主导身份」**
（`_dominant_identity_rows` 按 `instrument_key` 计数取最多者，只保留它）。
这保留了「不混身份」这个真正的修复，同时不再硬性要求一个 paper 账户根本不可能有的身份。
**没有**退化成「不过滤读取」——那会把重复时间戳的 bug 放回来。

新增回归：`test_legacy_only_store_is_still_visible_to_the_trading_path`、
`test_identity_collapse_still_refuses_to_interleave`。

### 附带发现（本轮新增，未修）

1. **原生 JSON Schema 一直被 Ollama 拒绝。** `qwen3.5:9b` + Ollama 0.34.0 对 `AI_ACTION_SCHEMA`
   返回 `HTTP 400 Failed to initialize samplers: failed to parse grammar`，
   所以生产**每一轮**都在走 `_request_json_with_schema_compat` 的降级路径（`json_mode_local_validation`），
   而该路径会把 schema 全文**再加一条 system 消息**塞进 prompt（实测约 +826 token）。
   `model_inference_settings.schema_enforcement` 初始写的是 `REQUESTED_NATIVE_JSON_SCHEMA`，
   实际永远是降级的那个值。代码对降级处理是正确的，故不阻断；但可考虑精简 schema 使其可被语法解析。
2. **截断会诱导乱开单。** 同一 prompt：8192（截断 73%）时模型输出 **`OPEN_LONG`**，
   16384（完整上下文）时输出 **`WAIT`**。即修复前的配置不只是"开不了单"，
   而是**在模型看不到完整风控规则时更容易直接下单**——这是安全性问题，不只是可用性问题。
3. **prompt 体量根因未治**：`MAX_CYCLE_SYMBOLS=5` 且 `news_revisions` 无上限，
   最坏 prompt 达 49053 字符 / 26759 token。把窗口撑到 32768 是止血；
   真正的治本是裁剪 prompt（限制标的数与新闻条数）。

---

## 七、仍待白茶裁定 / 未执行

| 优先级 | 事项 | 为什么需要你点头 |
|---|---|---|
| **P1-4 收尾** | 账户 `gate_testnet` 当前生效指令是 **revision 34**（今天 15:10 写入的**手写**「必须立即开单」指令）：`template_id = ai_autonomous_alpha`（**该模板 id 不存在于内置模板表**）、`entry_price 76430 / stop 75800 / tp 77800`（**塞死的假价格**，与当时真实报价不符）。模型无法满足 → 连续 WAIT。建议通过 `AIStrategyBook.save()` 切回正规内置模板（带版本、旧版保留、可回滚），但这是**改动已存储指令语义**，未擅自执行。 | 需确认是否切回内置模板，以及切哪个 |
| P2-6 | 重建失效授权（`auth_7ca80af8f0e6` 为 `mode=PAPER` 且已过期 5 天） | 涉及授权语义，须走正规创建入口 |
| P2-7 | 修 Gate WS 订单簿 sequence 校验（`ORDER_BOOK_UNTRUSTED` 已 158,482 次） | 涉及行情可信度语义 |
| — | 重建客户端（`bash scripts/build-client.sh`） | 本轮改了后端，按约定需同步 |

---

## 八、客户端同步（已完成，2026-09-15 23:53–23:57）

按项目硬约定，后端改动后重建客户端：

```
run tag        : 20260915-235328
frontend       : tsc --noEmit && vite build OK (2.33s)
sidecar        : SIDECAR_OK 59987543 bytes
tauri          : release 编译 1m21s -> ai-market-analyst.exe 12090880 bytes
install        : sha256 校验通过
```

| 产物 | 大小 | SHA-256 |
|---|---|---|
| `ai-market-analyst.exe` | 12 090 880 | `c6687a78d7d93f6b7863536b2debd6b11781c6ea11234cf23807e65bba2e8efa` |
| `ai-market-analyst-backend.exe` | 59 987 543 | `a25d9e9f951ee7b76f3945afb9359549932aae54b0ee32c483f14cb8e418bd7f` |

**独立核验**（不是采信脚本自报）：安装目录两个文件的 SHA-256 由我另行计算，与构建产物一致；
mtime `23:57:11/12`；运行中的 backend `pid=40440` 启动于安装之后；`/health` 返回 `status=ok`、
`port=18765`、`pid=40440`；仅监听 18765（单实例正确）。exe 体积 12 062 208 → **12 090 880**
（+28 672 字节），与新增代码量相符。

> 沙箱坑：`bash scripts/build-client.sh` 里的 `bash` 会被解析成 **WSL 的 bash**
> （报 `No such file or directory`，输出带 `wsl: 检测到 localhost 代理配置`，且 WSL 看不到 `D:`）。
> 必须显式用 Git Bash 绝对路径：
> `"C:/Users/baicha/.workbuddy/binaries/PortableGit/versions/1.2.0/bin/bash.exe" -lc '/d/RJ/codex/ai-market-analyst/scripts/build-client.sh'`

**`/health` 的 `ownership_verified=False`** 属预期：该字段是拿请求头里的
`X-AIMA-Ownership-Token` 与实例 token 做 `hmac.compare_digest` 得到的，裸查询（不带该头）
必然是 False，不代表后端异常。

### 全量测试现状（诚实记录）

`pytest tests` → **474 passed, 3 failed**。归属证据：`git diff -U0 core/trading/ai_session_coordinator.py`
的 hunk 行号清单（18/39/51/65-154/214-217/505-509/721/785-793/825-839/957/1000）
既不包含 `_news_revisions`（1155 行）也不包含 `_allowed_symbols`（471 行）。三条都不由本轮引入：

| 失败用例 | 原因 | 性质 |
|---|---|---|
| `test_autonomous_news_strategy::test_news_loader_adds_bounded_market_context_for_discovered_altcoin` | 断言 `item['scope'] == 'MARKET_WIDE'`，但 `_news_revisions` 产出的字典**根本没有 `scope` 字段**（读代码即可证伪） | **未实现的功能**：「给非直接相关标的补全市场级新闻背景」。测试先于实现写成 |
| `test_strategy_cadence_universe::test_ai_coordinator_uses_exchange_universe_for_unrestricted_gate_account` | 调用 `_allowed_symbols(None, scheduled_at=..., positions=...)`，真实签名是 `_allowed_symbols(self, authorization=None)` | **未跟踪的新文件**，属在途的「策略周期与交易所可选范围」特性，接口尚未接上 |
| `test_v12_monitoring_runtime::test_fastapi_startup_resumes_only_an_authorized_prior_runtime` | `wait_for(active)` 一返回就断言 `last_reason`，此刻可能仍是中间态 `stream_connected` | **竞态偶发**：单独连跑 3 次 **3/3 通过**；首轮全量未失败、次轮才失败 |

前两项**未擅自实现**：一个会改变 prompt 里的新闻内容，一个属他人在途特性的接口约定，都需先确认意图。

**已排掉的本轮回归**：P0-1 的身份收敛一开始写成硬性要求 Gate 身份，导致
`test_repair_v12_luna` / `test_trader_reliability_v13` / `test_trader_reliability_v14` / `test_v12_api`
共 **4 个**失败（paper 账户与测试夹具的 K 线全部不可见）。已由 `read_trading_bars` 的
「优先 Gate、缺失时收敛到单一主导身份」修好，4 个全部转绿。

---

## 九、界面「千问不可用」的真相（2026-09-16 00:25 白茶截图）

### 9.1 截图那 5 条 = 旧构建的遗留记录，逐条对齐

界面上「最近决策」列表按时间倒序渲染 `ai_led_cycles`，截图里 5 条与库中记录一一对应：

| 界面文案 | `reason` / `human_message` | 本地时间 |
|---|---|---|
| `SYSTEM_BLOCKED` + 「Qwen3.5-9B 当前不可用，本轮未生成交易动作。」 | `SMART_MODEL_UNAVAILABLE: LLMError: Ollama unavailable: timed out` | **23:17:17** |
| `SYSTEM_BLOCKED` + 「模型推理超过本轮有效期，结果已丢弃。」 | `MODEL_TIMEOUT_DISCARDED` | 23:14:33 |
| 同上 | `MODEL_TIMEOUT_DISCARDED` | 23:12:29 |
| 「Qwen3.5-9B 当前不可用」 | `SMART_MODEL_UNAVAILABLE` | 23:07:15 |
| 「Qwen3.5-9B 当前不可用」 | `SMART_MODEL_UNAVAILABLE` | 23:02:33 |

截图拍摄于 **00:25:24**，而 **重建客户端完成于 23:57**。**这 5 条全部产生于重建之前**，
用的还是旧配置（`context_length=16384`、`max_tokens=1800`、**无 `keep_alive`**，
见每条 cycle 的 `payload_json.model_inference_settings`）。

也就是说：界面**没有失效**，它只是**没有新数据可显示**。

### 9.2 重建后的真实时间线（硬证据）

| 本地时间 | 事件 | 证据 |
|---|---|---|
| 23:57:16/17/19 | 桌面壳与 sidecar 重启 | `Get-Process StartTime`；`runtime.json.started_at_utc=15:57:21Z` |
| 00:17:53 | 监控运行时取得租约（AI 会话开始调度） | `runtime_leases.acquired_at`，`fencing_token=93` |
| 00:25:24 | 白茶截图 | 截图文件名 `clipboard-2026-09-15T16-25-24-978Z` |
| 00:30:00 | **重建后第一轮对齐周期启动** | `schedule.last_started_at` |
| 00:32:44 | 结果：`TIMEOUT_DISCARDED`，模型 latency **122 649 ms** | `ai_led_cycles` |
| 00:45:00 → 00:47:25 | 第二轮：同样 `TIMEOUT_DISCARDED`，latency **125 508 ms** | `ai_led_cycles` |

两轮的 `model_inference_settings` 都已是新值：
`{"context_length": 32768, "max_tokens": 900, "keep_alive": "45m", ...}` —— **改动确实生效了**。

`/v2/ai-session/status` 的 `model` 字段在 00:30:44 返回过
`status=READY / model_available=true / context_length=32768 / keep_alive=45m / quantization=Q4_K_M`
—— 健康检查层面**「千问不可用」这条已经消除**。

### 9.3 但周期仍然超时：真正的瓶颈是**显存**，不是窗口大小

`num_ctx` 修好了截断，却换来**灾难性的生成速度**。Ollama 自身日志
（`%LOCALAPPDATA%\Ollama\server.log`）给出的事实：

```
load_tensors:  CUDA_Host model buffer size =  1424.85 MiB      <- 权重落在主机内存
llama_context: n_ctx = 32768
llama_kv_cache: size = 1024.00 MiB ( 32768 cells, 8 layers)
sched_reserve: graph splits = 117 (with bs=512), 14 (with bs=1)

slot print_timing: prompt processing, n_tokens = 17764, progress = 1.00, t = 43.57 s / 407.68 tokens per second
slot print_timing: n_gen = 100, tg = 3.33 t/s      <- 生成只有 3 tok/s
slot print_timing: n_gen = 228, tg = 2.99 t/s      <- 跑到 120s 被砍
```

| 观测量 | 实测值 |
|---|---|
| 真实 prompt（runtime，含 schema 降级追加的 system 消息） | **17 768 token** |
| 提示词处理耗时 | 43.6 s（≈408 tok/s） |
| 生成速度 | **2.99–4.54 t/s**（对照：早前小窗口任务有 26 t/s） |
| `max_tokens` | 900 |
| 900 token 在 3 t/s 下所需时间 | **≈300 s** → 必然撞爆 120 s 预算 |

**为什么权重会掉到 CPU**：

```
nvidia-smi --query-gpu=memory.total,memory.used,memory.free
NVIDIA GeForce RTX 4060, 8188 MiB, 7526 MiB, 432 MiB
```

`/api/ps` 同时显示 `size = 7 242 220 826` 而 `size_vram = 5 514 796 726`
—— **约 1.73 GB 未进显存**。这张 8 GB 卡同时被 Wallpaper Engine、Chrome、
微信、TradingView、Steam、Doubao、剪映托盘、ToDesk、WorkBuddy 等常驻程序占用，
只剩 432 MiB 空闲；`qwen3.5:9b` 还是**带视觉编码器**的 Qwen3-VL（日志里
`clip_ctx: CLIP using CUDA0 backend`），CLIP 也要显存。于是 `n_ctx=32768`
需要 1 GiB KV + 239 MiB compute 时，模型被挤掉一部分 → 生成速度塌到 3 tok/s。

**结论**：`num_ctx=32768` 在这台机器上是**用截断换超时**。
`P1-5` 的价值（消除静默截断、保住风控规则可见性）成立，但**必须在能容下它的显存预算里用**。

附带发现（同一条日志）：
`forcing full prompt re-processing due to lack of cache data (likely due to SWA or hybrid/recurrent memory)`
—— 每轮请求都会**全量重算 17k token 的 prompt**（无前缀缓存复用），这也是 43.6 s 的来源。

### 9.4 尚未解决 / 待裁定

| 方案 | 做法 | 代价 | 预期 |
|---|---|---|---|
| **A（推荐）** | 把交易路径 prompt 压到 ≤14k token：`MAX_CYCLE_SYMBOLS` 5→3、给 `news_revisions` 设上限，同时把 `MODEL_CONTEXT_LENGTH` 调回 20480 或 16384 | 每轮少看 2 个标的 | KV 从 1 GiB 降到 0.5–0.65 GiB，权重可全部驻留显存，恢复 13–26 t/s |
| B | 把模型预算 `model_budget_seconds` / `MODEL_TIMEOUT_SECONDS` 从 120 s 提到 240–300 s | 单轮最长 5 分钟（周期 900 s，放得下） | 3 t/s 也能跑完 900 token，但每轮会明显变慢 |
| C | 交易路径改用 `qwen3.5:4b`（本机已存在） | 决策质量下降 | 体积小，可完全驻留显存 |
| D | 释放显存：关掉 Wallpaper Engine / 浏览器 / 剪映托盘等常驻 GPU 程序 | 需白茶侧操作 | 立刻见效，但不可控 |

以上 A/B 属**改动运行策略与 prompt 语义**，按项目约定**须白茶点头后才执行**。

---

## 十、策略层与决策层**断链**（2026-09-16 00:58 查实，本轮最重要的结构性问题）

白茶问「目前项目的策略和 ai 思考是不是一致」。**结论：不一致，而且不是偏差，是断链 ——
界面上配置的账户策略，AI 从未看到过。**

### 10.1 证据链（五条独立证据，互相印证）

1. **`AIStrategyBook` 在 `core/` 里只有类定义，没有任何调用方。**
   `grep -rn "AIStrategyBook" core/` → 只命中 `core/trading/ai_strategy_book.py:55`（`class AIStrategyBook:`）。
   全仓库的消费方只有 `apps/api/v2.py:2321/2327`（界面读 / 写）+ 测试 + `scratch/`。

2. **生产路径构造 `AICycleContext` 的三处都没传 `strategy_instructions`**：
   `core/trading/ai_session_coordinator.py:623 / 1282 / 1299`。
   该字段只在 `ai_led_engine.py:122` 声明为 `field(default_factory=dict)`。

3. **全仓库唯一给 `.strategy_instructions` 赋值的地方是测试**：
   `tests/test_autonomous_news_strategy.py:102/127/255`。
   生产代码里**一次赋值都没有**。

4. **唯一读取点**：`ai_session_coordinator.py:785`
   `system_content = build_strategy_system_prompt(getattr(context, "strategy_instructions", None))`
   —— 读到的恒为 `{}`。

5. **实测 prompt 长度对比**：

   | 输入 | 渲染出的 system prompt | 含策略段 |
   |---|---|---|
   | 生产实际 `{}` | **3 351 字符** | **否** |
   | 账户真正生效的指令（rev 36） | 5 863 字符 | 是 |

   因为 `autonomous_strategy.py:75` 是 `if sections or profile:`，
   空字典时 `strategy_block = ""`，直接 `return SYSTEM_PROMPT`。
   **被吞掉的那 2 512 字符，就是整段「当前已激活的执行策略」+ profile + 6 条策略纪律。**

6. **实证**：00:45 那轮周期落库的 `payload_json.strategy_instructions` **就是 `{}`**。

### 10.2 连带失效的三件事（静默，没有任何报错）

| 失效项 | 机制 | 实测 |
|---|---|---|
| **订单偏好契约失效** | `resolve_order_preference()` 在空指令下只能回落到模型自己给的值 | `resolve_order_preference({}, 'MARKET')` → **`MARKET`**。内置模板若是 LIMIT 型，网关**不会**拦住市价单（`autonomous_strategy.py:113-130`） |
| **界面风控参数不生效** | 界面 `execution` 里的 `leverage / max_notional_usdt / risk_per_trade_pct / max_positions` 无人读取；运行时用的是 `autonomous_strategy.POLICY` 硬编码那份 | 界面写 `leverage: 5`、`max_notional_usdt: 1000`、`risk_per_trade_pct: 0.25`；实际生效 `max_leverage: 100`、`max_single_risk_fraction: 0.0025`。**数字碰巧在 `min_confidence=70` / `min_net_rr=2` 上一致，但机制上互不通气** |
| **周期不一致** | 界面模板声明 `signal_timeframe: 5m` / `scan_interval_minutes: 5`；运行时固定 `cycle_interval_seconds = 900.0`、对齐 `minute % 15 == 0` | 界面说 5 分钟，实际 15 分钟 |

### 10.3 模型之外还有 4 道门（叠加在同一轮周期上）

| 层 | 现状 | 数据 |
|---|---|---|
| 候选层 | 8 条候选**全是** `NO_TRIGGER` / `UNSUPPORTED` | `funding_extreme`、`opening_range_breakout` = `UNSUPPORTED` |
| ~~授权层~~ | **【已更正，不是门】** 协调器 `ai_session_coordinator.py:1318` 把 `authorization = None` **写死**，注释明说「账户的 Gate 凭据与下游风控引擎接管授权；这里没有本地 scope 记录需要发放/过期/撤销」。`trading_authorizations` 全仓只被 `core/trading/authorization.py` 自己读写，`execution_gateway.py` 里 `authorization_id` 只作元数据落库与回显，**无任何校验分支** | 所以 `auth_7ca80af8f0e6`（`mode=PAPER`、已过期 6 天）是**死数据**，不构成阻断 |
| 行情层 | 订单簿序列校验失败 | `ORDER_BOOK_UNTRUSTED` **161 779** 次，仍在刷 |
| 算力层 | 生成 2.99 t/s，900 token 需 ≈300 s | 预算 120 s，见第九节 |

### 10.4 结果：模型从未开过一单

```
decision_origin = MODEL 的决策：164 条，全部是 WAIT
SYSTEM_BLOCKED：56 条
OPEN_LONG / OPEN_SHORT：0 条
```

### 10.5 账户策略正在被反复更换（说明白茶一直在试图解决）

`ai_strategy_instructions` 修订历史（`gate_testnet`）：rev 25 → **36**，一天内换了 12 次。
最近两次 **rev 35 / 36 写于 2026-09-15T16:54 UTC = 00:54 本地**（就在提问前两分钟），
都是 `aggressive_impulse`；rev 34 是手写的 `ai_autonomous_alpha`（`template_id` 不在内置表里）。
**但无论换哪一个，模型都看不到。**

### 10.6 修复顺序建议（均需白茶点头）

| 序 | 事项 | 说明 |
|---|---|---|
| 1 | **接上断链**：协调器构造 context 时 `AIStrategyBook(store).active(account_id)` 填入 `strategy_instructions` | 一行级改动，但会**改变 prompt 语义**（模型会突然看到 2 512 字符的策略与「严禁 WAIT」纪律） |
| 2 | 让 `execution` 的 `leverage / max_notional / risk_per_trade_pct / max_positions` 真正进网关校验 | 否则界面风控是摆设 |
| 3 | 周期对齐（5m/15m 跟随 profile） | 需与 evidence 帧、`strategy_frames(interval)` 一起改 |
| 4 | 先解决算力/显存（第九节 A/B 方案） | 否则接线后 prompt 更大，超时概率更高 |

**注意顺序**：第 1 项会让 prompt 从 3 351 涨到 5 863 字符（system 侧），
若第 4 项不先解决，接线后大概率**还是**超时 —— 但至少模型终于能看到策略了。

---

## 11. 已实施的修复（白茶批准 0+1+2）

### 11.1 核实结论：断链只有一处，下游全部已接线

逐处读过之后，修复面比第十节预估的小得多 —— 消费侧**早就写好了**，只差上游一传：

| 位置 | 状态 |
|---|---|
| `ai_led_engine.py:675` `getattr(context,"strategy_instructions",None) or {}` | 已接线 |
| `ai_led_engine.py:676-686` `execution` / `profile` / `configured_ttl` | 已接线 |
| `ai_led_engine.py:681` `resolve_order_preference(...)` → `:687` `OrderSelectionPolicy.select` | 已接线 |
| `autonomous_strategy.validate_entry():221-237` 方向/杠杆/风险/`scan_interval_minutes`→`strategy_frames` | 已接线 |
| `autonomous_strategy.py:271-274` `allow_market_entry` / LIMIT 契约 | 已接线 |
| `ai_strategy_book.active()` | 返回 `name/template_id/style/profile/sections/execution/digest`，可直接用 |
| **三处 `AICycleContext(...)`（原 `:623` / `:1282` / `:1299`）** | **唯一缺口** |

`AICycleContext.strategy_instructions` 默认 `field(default_factory=dict)`（`ai_led_engine.py:122`），
所以下游所有 `getattr(context, ...)` 恒得 `{}`。

### 11.2 改动清单（`core/trading/ai_session_coordinator.py`）

| # | 位置 | 改动 |
|---|---|---|
| 1 | 常量区 | `MODEL_BUDGET_SECONDS` 120.0 → **300.0**（附实测注释） |
| 2 | 常量区 | `MODEL_TIMEOUT_SECONDS` 120.0 → **360.0**（HTTP 层必须大于生成预算） |
| 3 | 常量区 | `INTENT_TTL_SECONDS` 240.0 → **320.0**（必须大于 budget，见 11.6） |
| 4 | import 区 | 新增 `from .ai_strategy_book import AIStrategyBook`（原先未导入） |
| 5 | `_build_context` 之前 | 新增 `_active_strategy(account_id)`，fail-open 回落 `{}`，异常记 `STRATEGY_BOOK_UNAVAILABLE` |
| 6 | `_build_context` 尾部 | `strategy_instructions=self._active_strategy(account_id)` |
| 7 | 两处 blocked 路径 | 同上（审计记录里也要体现用的是哪版策略） |
| 8 | `core/monitoring_runtime.py` | **删除**该处 `model_budget_seconds=120.0` 硬编码入参 |

校验：`AICycleContext(` 全文件 3 处，注入语句 3 处，`ast.parse` 通过。

注意构造处是 `max(0.1, min(float(x), MODEL_BUDGET_SECONDS))` —— 传参会被上限 clamp，
但**反方向不受保护**：`monitoring_runtime` 传 120 时，上限改成 300 也救不回来。
所以第 1、2、3、8 项是**一件事**，只改常量定义无效（这是第一轮踩的坑，见 11.6）。

### 11.3 第 0 层的"窗口压到 20 480"被否，理由

`ai_session_coordinator.py:66-71` 的注释记着本机实测：最大 prompt **26 759 token**（49 053 字符）。
把 `num_ctx` 压到 20 480，`_assert_prompt_fits()`（`:146`）会判定超窗并**抛错终止整轮** ——
比超时更糟（超时至少留下 `TIMEOUT_DISCARDED` 记录，撞窗是硬失败）。

`32 768` 是当前的**硬下限**，除非同时压缩 prompt 体积（例如候选标的数 `MAX_CYCLE_SYMBOLS` 5→3）。
`MIN_MODEL_CONTEXT_LENGTH = 16 384` 只是 `_session_context_length()` 的下限护栏，**不是安全值**。

故第 0 层只保留"腾显存"这半边，需手动执行（见 11.5）。

### 11.4 回归验证

系统 Python 3.12.10 + pytest 8.4.2，8 个靶向套件 10.53s：

```
2 failed, 89 passed
```

两个失败与历史记录一致：

- `test_strategy_cadence_universe.py::test_ai_coordinator_uses_exchange_universe_for_unrestricted_gate_account`
  —— `TypeError: AISessionCoordinator._allowed_symbols() got an unexpected keyword argument 'scheduled_at'`
- `test_autonomous_news_strategy.py::test_news_loader_adds_bounded_market_context_for_discovered_altcoin`
  —— `_news_revisions` 未产出 `scope == 'MARKET_WIDE'`

**已证与本轮接线无关**：把本轮 6 处编辑在内存中反向剥离后重跑，两者以**完全相同的错误**失败。
脚本 `scratch/prove_preexisting_failures.py`，输出 `PYTEST_EXIT_WITHOUT_WIRING 1` 且
`restored byte-for-byte: True`。根因是工作区里前几轮**未提交**的改动（`git status` 显示 46 个文件在途）。

### 11.5 仍未生效的部分

| 事项 | 状态 |
|---|---|
| 模型真正看到策略 | **已完成**：两轮重建（tag `20260916-012706`、`20260916-013201`），安装产物 SHA-256 已与构建产物比对一致 |
| 腾显存 | **需手动**关闭常驻 GPU 程序（Wallpaper Engine / Chrome / 豆包 / 剪映 / TradingView / Steam / ToDesk），代码侧无法代劳 |
| 周期对齐（第十节第 3 项） | `self.cycle_interval_seconds` 仍不读 `execution.scan_interval_minutes`，5m 模板仍按 15 分钟跑 |
| `fixed_policy` 与 profile 并存 | `_model_output` 的 payload 同时给 `POLICY` 与账户 profile，措辞上未区分"平台硬上限"与"账户策略" |

### 11.6 第一轮重建后发现的坑：超时链三处架空

第一轮重建（tag `20260916-012706`）之后，`GET /v2/ai-session/status` **仍报 `model_budget_seconds: 120.0`**。
即：改了常量定义，运行态一点没变。追下去是三层叠加：

| 环节 | 位置 | 实际值 |
|---|---|---|
| 构造覆盖 | `core/monitoring_runtime.py:140` `model_budget_seconds=120.0` | 显式入参架空常量默认值 |
| TTL 天花板 | `INTENT_TTL_SECONDS = 240.0` | 即使 budget 改 300 也会被截到 240 |
| worker 等待 | `future.result(timeout=min(self.model_budget_seconds, INTENT_TTL_SECONDS))` | `min(120,240)` = **120 s** |

结论：第一轮的"超时抬到 300s"**一秒都没抬**。第二轮（tag `20260916-013201`）修正
上述四处之后才真正生效，运行态回显 `model_budget_seconds: 300.0`。

**教训**：改"上限型常量"必须同时做三件事 ——
① `grep -rn <CONST>` 覆盖定义与**所有入参点**（找显式传值）；
② 向下找 `min()` / `max()` 的二次 clamp；
③ 读**运行态接口**确认新值生效，不要靠读代码推断。

已写入 skill `aima-no-order-blocker-triage` 第 8.6 节。

### 11.7 验证抓手：prompt 规模是可量化的

`_model_output` 会把 `prompt_tokens_estimated` 落进周期的 `model_inference_settings`
（`ai_session_coordinator.py:864`），所以"策略到底有没有进 prompt"不需要猜：

| 周期 | 时间(UTC) | `context_length` | `prompt_tokens_estimated` |
|---|---|---|---|
| `…T170000019322Z` | 17:00 | 32 768 | **16 365**（接线前） |
| `…T171500009382Z` | 17:15 | 32 768 | **16 631**（接线前） |
| 17:45 及以后 | — | 32 768 | 预期 **≥ 17 800**（+ 策略段约 1.4k token） |

新周期的估计值只要明显越过 17k，就证明策略段真的进了 system prompt。
取数脚本：`scratch/diag_prompt_tokens.py`、`scratch/read_session_status.py`。

接线后**新增的行为风险**（首次激活，需盯第一轮真跑）：

1. `validate_entry` 的 `if config:` 由恒假变恒真 → `STRATEGY_DIRECTION_OR_SYMBOL_BLOCKED` /
   `STRATEGY_LEVERAGE_EXCEEDED` / `STRATEGY_RISK_EXCEEDED` 三门首次启用。
2. `resolve_order_preference` 由恒 `AUTO` 变模板真值 → LIMIT 型模板的
   `allow_future_limit` / `AI_LIMIT_WOULD_CROSS_QUOTE` 分支首次生效。



