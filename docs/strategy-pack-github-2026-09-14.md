# AI 新闻技术策略包（2026-09-14）

本策略包把 AI 放在“比较候选、解释新闻、选择一笔交易”的位置，把周期、仓位、杠杆、止损、TTL 和冷却交给代码。它借鉴了 NOFX 的连续决策链路和 Strategy Studio 的配置方式，但没有复制 NOFX 的代码。

## 外部参考与许可证边界

- [NOFX Strategy Module](https://github.com/NoFxAiOS/nofx/blob/dev/docs/architecture/STRATEGY_MODULE.md)：候选币筛选 → 多周期数据组装 → 提示词 → JSON 解析 → 风控/执行/回执，是本项目的 AI 运行链路参考。
- [NOFX Prompt Guide](https://github.com/NoFxAiOS/nofx/blob/dev/docs/prompt-guide.zh-CN.md)：把策略写成数值化的触发条件、退出条件和频率约束。NOFX 仓库声明为 AGPL-3.0，不能直接把其实现整包拷入本项目而不做许可证决策。
- [Freqtrade Strategy Customization](https://github.com/freqtrade/freqtrade/blob/develop/docs/strategy-customization.md)：采用已收盘 K 线、多周期指标、回测 → dry-run，并检查 lookahead/recursive bias。
- [Jesse example-strategies](https://github.com/jesse-ai/example-strategies)：Donchian、Dual Thrust、Turtle、Bollinger 等规则适合作为信号组件；仓库明确说明示例不是可直接保证盈利的成品，MIT 许可证只解决代码使用边界，不解决交易风险。

## 四套编译策略

### A1 · 5m 动量突破 + 流动性扫荡（激进）

- 信号周期：5m；背景：15m、1h；每 5 分钟对齐扫描。
- 多头：收盘价突破过去 20 根 5m 高点，或扫过支撑后收回；成交量 ≥ 20 根均值的 1.20 倍；15m 方向不反向；至少通过 2 个独立确认。
- 空头：对称处理，允许跌破低点和反弹受阻做空。
- 进场：突破位附近 ≤ 0.3% 用市价，否则只使用有 TTL 的限价；TTL 180 秒。
- 保护：止损 1.20 ATR；TP1 1.8R、TP2 2.6R；单小时最多 3 次新进场，单品种冷却 15 分钟。

### A2 · 15m 波动扩张 + 回踩续航（激进）

- 信号周期：15m；背景：1h；每 15 分钟对齐扫描。
- 多头：布林带/Keltner 挤压结束，15m 收盘突破整理高点，EMA20 > EMA50，成交量 ≥ 1.30 倍；回踩突破位不失守。
- 空头：对称跌破；资金费率极端且与方向相反时否决。
- 进场：主用限价回踩单，允许未来关键位限价但 TTL 240 秒；已确认突破且距触发位 ≤ 0.4% 才允许市价。
- 保护：止损 1.30 ATR；TP1 2R、TP2 3R；单小时最多 2 次，冷却 30 分钟。

### C1 · 1h 趋势 + 15m 回踩确认（保守）

- 信号周期：15m；背景：1h；每 15 分钟扫描。
- 多头：1h EMA20 > EMA50；15m 回踩 EMA20/前高后重新收回；RSI 45–65；CVD/OI 不出现反向背离；重大反向新闻否决。
- 空头：对称处理。
- 进场：只挂关键位限价，TTL 300 秒；不追市价。
- 保护：止损 1.50 ATR；TP1 2.2R、TP2 3R；每小时最多 1 次，冷却 60 分钟。

### C2 · VWAP + 资金费率极值均值回归（保守）

- 信号周期：15m；背景：1h；每 15 分钟扫描。
- 多头：价格位于会话 VWAP 下方 2σ/1.4 ATR 附近，RSI ≤ 32，资金费率/OI 显示空头拥挤，CVD 出现底背离并收回 VWAP 内侧。
- 空头：对称处理，禁止在趋势加速时逆势摸顶摸底。
- 进场：只挂 VWAP/极值区限价，TTL 300 秒；重大反向新闻直接否决。
- 保护：止损 1.40 ATR；TP1 2R、TP2 2.6R；每小时最多 1 次，冷却 90 分钟。

## 运行约束

1. AI 每轮只选择一笔最优机会，也可以 WAIT；WAIT 必须给出数据缺失、条件未满足或风控冷却的原因。
2. 置信度不是胜率。当前账户的代码硬上限仍控制单笔风险、最大名义金额、最大持仓数、杠杆、连续亏损冷却和美盘防御锁。
3. 新闻是风险过滤器和方向增强项；中性或全市场新闻不再自动否决技术面合格的交易。
4. 不用远期假价格伪造“已触发”。限价策略可以提交有 TTL 的未来关键位，但必须通过价格方向、tick、深度、费用和滑点验证。
5. 策略上线前必须用 Gate 已收盘真实 K 线做回放，随后在 Gate TestNet dry-run/小额测试观察信号、成交、撤单和保护单；不能把模板说明当成收益保证。

