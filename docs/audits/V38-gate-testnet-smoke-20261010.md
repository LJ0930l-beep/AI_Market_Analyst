# V38 Gate TestNet 受控执行检查

日期：2026-10-10

环境：Gate 官方 TestNet (`api-testnet.gateapi.io`)

范围授权：用户明确授权使用已保存的 Gate TestNet 读写凭证运行模拟交易。

## 执行边界

- 仅使用账户 `gate_testnet`，适配器的 TestNet 标记和 API 主机均通过执行前断言；未构造或调用 `gate_live` 适配器。
- 本次是交易所传输、成交回执、保护单和清理链路检查，不是 Gemini 策略判断；`model_called=false`。
- 使用 BTCUSDT 永续合约最小数量 1 张，合约规模 `0.0001 BTC`，执行前最小名义金额估计约 `8.27 USDT`，杠杆 1x，止损 1%，止盈 2%，并启用自动 reduce-only 平仓。
- 执行前远端账户为 0 持仓、0 挂单。没有创建本地 `trade_fills` 或模拟成交镜像。
- 未访问 Live API，未修改生产资金或风险参数。TestNet BTC 杠杆设为 1x；本次未尝试恢复其他杠杆值。

## 结果与恢复

- Gate TestNet 入场订单与 reduce-only 平仓订单均返回 `FILLED`。
- E2E 服务将本次运行记为 `RECONCILIATION_REQUIRED`：Gate 返回的原生保护单带有 `initial.is_reduce_only=true`，没有 `initial.reduce_only=true` 字段；Gate 适配器已将其验证并归一化为顶层 `reduce_only=true`，但 E2E 清理器仍错误要求原始嵌套字段存在。
- 从本次运行回执取得的两个保护单，经过订单 ID、BTCUSDT 合约范围、适配器归一化的 reduce-only 属性及远端状态复核后，分别执行撤销。最终独立读取 Gate TestNet 账户事实为 0 持仓、0 挂单。
- 原 E2E 运行记录保持原样，没有把失败状态改写为成功。报告只保留脱敏摘要，不包含 API 凭证、账户权益、远端订单 ID 或原始账户响应。

## 修复与验证

清理器现在依赖 Gate 适配器已经校验过的顶层 `reduce_only=true` 和合约/订单身份，并保留原始 `initial` 载荷。新增覆盖 Gate `is_reduce_only` 原生字段的适配器与 E2E 回归用例。

验证结果：

- `python -m pytest tests/test_gate_testnet_ai_chain.py -q` — **22 passed**，1 条既有 Starlette/httpx 弃用警告。
- 全量候选：**2461 passed, 3 failed, 1 skipped**。3 个失败均位于既有 `tests/test_macro_actuals.py`。
- 在精确父提交 `ffb2b29397ee388583332e18b3002d2a20f5e30a` 上复跑这 3 项，结果仍是相同 **3 failed**；本次 Gate 改动没有新增失败节点。失败项保留，未修改或屏蔽。
- Ruff 相对基线无新增诊断（E2E 模块 34→34，测试模块 24→24）。

## 结论与限制

TestNet 下单、成交、平仓和远端归零均有本地审计记录；首次运行的程序级保护单清理结果并未通过，因此只能表述为“订单已平仓，保护单已单独核验清理”，不能把首次 E2E 服务结果称为 `COMPLETED`。本次没有调用 Gemini，也没有验证 AI 自主选币或策略收益。
