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
- 精确候选 SHA 的 GitHub Actions 验证待本次变更推送后完成；在此之前 G0 仍为待验收状态。
