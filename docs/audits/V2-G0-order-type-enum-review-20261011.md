# V2 G0 Gate 保护单字段与类型复核 — 2026-10-11

## 范围与结论

复核组合候选的 `GateLiveTrader.fetch_protection_order()`。Gate 官方 Futures API 将 `reduce_only`（请求字段）、`is_reduce_only`（响应属性）和 `is_close`（是否为平仓订单）分别定义；它们不能被当作同一个布尔字段。官方文档也枚举了六种条件止盈止损 `order_type`。

因此，`is_close=false` 本身不否定 `is_reduce_only=true`：部分减仓可以是 reduce-only，但不是全仓平仓。适配器仍会拒绝互相冲突的 `reduce_only` / `is_reduce_only`，以及只有 false 的 reduce-only 标志。

发现并修复一处过宽匹配：原代码以 `"close-"` 子串判断订单类型，任意含该子串的未知类型也可能通过。现在只接受 Gate 文档列出的六个精确枚举值；未知类型继续拒绝。

## 实现与测试

- 仅收紧保护单读取校验，不改请求构造、下单、风险预算、账户配置或生产默认值。
- 新增对六个文档枚举的接受用例、两个含 `close-` 但不属于枚举的拒绝用例，以及 `is_reduce_only=true` / `is_close=false` 部分减仓语义用例。
- 现有 native flag 冲突和 false-only 拒绝用例保留。

## 证据与限制

- 字段及枚举依据：[Gate API v4 Futures — Futures price-triggered order details](https://www.gate.com/docs/developers/apiv4/en/futures/)。
- 本次只使用本地模拟响应 fixture；未访问 Gate TestNet/Live、未发送交易请求、未调用 Gemini。
- 这是对 API 契约和本地代码的复核，不证明当前 Gate TestNet 实际返回的所有兼容变体。远端环境的无订单只读复核仍未完成。
- 首次修补后的精确候选 SHA `6d4278066fd2516077972bd6ba6a3f2683f8de6b` 已由 run `38118351684` 验证；后续新增审查发现见下方追加记录。

## 2026-10-11 addendum — price-trigger side uses order-type semantics

### 发现与原始证据

- 范围：`GateLiveTrader.get_open_trigger_orders()` 的保护单方向字段及其账户对账消费者。
- Gate 官方 Futures API 的创建示例使用 `order_type="close-long-order"` 且 `initial.size=100`；同一接口将 `close-long-order` 定义为平多，将 `close-short-order` 定义为平空。该 `size` 是待平数量，不能直接按普通 futures order 的正负号推断保护单执行方向。
- failure-first 回归先加入该官方示例：预期保护方向 `SELL`，当前实现输出 `BUY`；结果为 1 failed、6 passed。下游保护单归属校验会比较 side，因此错误方向可能让已存在的保护单无法匹配到自有持仓/订单证据。

### 临时方案与修复

- 未访问任何 Gate 账户，也未发起 TestNet/Live 请求；保留现有远端 fail-closed 与保护状态降级策略。
- 当 `order_type` 是六种已知保护单枚举时，按其关闭的持仓方向映射 `BUY`/`SELL`；只有未知枚举才回退到 size 符号用于展示，且仍保持 `DEGRADED`、不把未知类型当保护单。
- 新回归固定 `close-long-order`、正数 size → `SELL`，防止将平仓数量误作交易方向。

### 验收、限制与回滚

- 该案例修复后，Gate account chain、TestNet AI chain 与 institutional protection 定向测试合计 67/67 通过；精确新 commit 的本地完整门禁与 GitHub full CI 尚待运行。
- 原始 V38 样本、研究 ledger、Gate 账户配置、V35 风控和生产下单代码未改。
- 若回滚，撤销 side 映射与对应 fixture 即可；必须保留此前精确枚举/原生布尔解析修复，不能恢复 `"close-"` 子串识别。
- API 依据：[Gate API v4 Futures — Futures price-triggered order details](https://www.gate.com/docs/developers/apiv4/en/futures/)。此离线证据不证明当前 TestNet 的实际响应兼容性。
