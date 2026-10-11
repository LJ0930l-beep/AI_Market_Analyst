# V36 价格行为数据输入审计

审计基线：V35 提交 `2a5d7b9992a7909a6a597917e409ecb0737f9c89`。PR #1 核验为 OPEN、未合并；`main` 仍为 `8642639a78ca9aa56fbad1e88aea4b82169ea433`。V36 分支从 V35 派生并保留其依赖。本审计只读生产路径，不修改生产 Prompt、`AI_ACTION_SCHEMA`、Gemini 路由、账户资金设置或交易执行。

## 已证实的调用路径

1. `core/trading/ai_session_coordinator.py` 中的 `AISessionCoordinator._strategy_scan_contract()` 对 `price_action_structure` 固定信号周期为 15m；配置了 5m 入场周期时保留 5m，并总是附加 1h、4h 背景周期。该逻辑覆盖旧 NOFX runtime 的帧配置，避免旧配置静默丢失价格行为背景。
2. `AISessionCoordinator._build_context()` 以该帧列表调用 `core/trading/autonomous_strategy.py::technical_context(..., include_price_action=True)`。上下文生成会从存储读取每个标的、每个周期最多 240 根 K 线，校验显式收盘状态、周期长度、`bar_end`、`available_at` 不晚于决策时刻、OHLCV 几何和数据质量；至少 32 根、最新数据足够新、全段连续时才标记 READY。失败或缺失数据不能补成确定的结构结论。
3. `technical_context()` 对 READY 帧计算 EMA、RSI、ATR、成交量比和近 20 根高低值。开启价格行为后，它进一步调用 `core/trading/price_action_structure.py::build_price_action_structure()`。后者要求可信 Gate 来源/身份、最多 240 根、至少 32 根、连续且未过期的收盘数据；无效数据返回明确的 `UNAVAILABLE` 原因。Swing 使用左右各两根 K 线确认，输出拐点时间、确认 K 线时间、确认可知时间及可用时间；突破/扫荡仅引用在事件 K 线之前已经可用的摆动结构。
4. 模型输入由 `compact_technical()` 和 `ai_session_coordinator.py::_fit_prompt_payload()` 再次压缩。价格行为策略正常压缩时保留 15m 8 根、5m 6 根及其它背景周期各 4 根 candle，并附压缩后的结构摘要和 `last_closed_at`；上下文预算更紧时会先减少明细、保留跨标的结构摘要，最终预算仍不满足则不应发送。原始 240 根用于本地计算，不等于模型看到 240 根原始 K 线。模型负责市场环境、位置、方向、入场与持仓解释；Python 结构输出是有来源的事实，不是自动开仓信号。
5. `core/replay/ai_template_runner.py::run_ai_template_replay()` 另有历史路径：先将历史时钟固定到决策点，再使用同一 `technical_context()` 生成上下文。但它写入自身 SQLite 回放表并调用模型 Provider，不能直接充当 V36 A1/A2/A3 的共享离线缓存或默认运行器。V36 使用独立冻结输入、记录及输出目录，防止写回旧实验账本。

## 时间因果及数据边界

- 生产上下文拒绝 `bar_end > now` 或 `available_at > now` 的行；高级别周期以其完整帧时长校验，未完成 1h/4h 帧不能作为 READY 结构事实。
- `price_action_structure.py` 不仅存储摆动拐点时间，还等待两根右侧 K 线完成，再以确认时间和源数据可用时间的较晚者定义 `known_at`。已确认的事件不会因随后数据而回写到更早时点。
- 生产检查允许 `bar_end == now` 且 `available_at == now`。在时间戳精度不足以判定同刻事件顺序时，这类边界的先后无法仅凭时间戳证明。V36 研究切片采用严格早于决策时刻的收盘和可用时间；同刻记录只有存在冻结的事件顺序证据才可纳入。
- `technical_context()` 以 `bar_end` 为键收集行，重复键会被后读到的记录覆盖。调用点传入结构构造器的也是该映射的值，因此结构构造器的 `DUPLICATE_BAR_END` 检查无法看到上游已折叠的重复。是否由底层存储唯一约束完全消除此情况尚未在本次窄范围审计中证明。V36 离线输入在生成摘要前显式拒绝重复结束时间，不改变生产行为。
- 生产 Prompt 中 candle 以 OHLCV 数组传递，周期的 `last_closed_at` 作为时间锚点；压缩后的每根 candle 不附独立时间和可用时间。审计判断依赖生成前的冻结证据，而不是要求 Gemini 从无时间戳数组重建精确因果序列。
- `technical_context()` 对存储读取异常采用空数据继续构造上下文；由 READY 条件和 `UNAVAILABLE` 结果限制其使用。V36 会保留输入缺失/异常分类，不能把异常静默转成中性或可交易判断。

## 冻结历史证据与可回答范围

当前可提交的 V25 脱敏样本为 `docs/research/v25-price-action-sample-20261008.json`，SHA256 `4193956555d7576a3a754fbeb80198ad5b4828079b574dedf71eb347a13353cb`，来源历史 SHA256 `c712c0486244627584c4a85ba6773cbf179da22e47176e6bd9950a7284848d56`。其中 100 次扫描为 WAIT 63、HOLD 24、OPEN_LONG 5、OPEN_SHORT 8；另有 10 笔完整平仓，3 盈、7 亏，净收益 `-4.77884509450 USDT`。样本排除了原始 Prompt、模型响应、决策叙述和账户/委托身份；也没有这些扫描时点的 K 线输入、完整 Gate 合约快照或私有成交证明。

另行发现的 192 决策缓存窗口与上述 100 次扫描的数量、来源范围不相符。没有逐项证明模型、Prompt、冻结输入、决策时间和状态快照完全匹配之前，不能将其重标为 V25 A0 或混入此样本。因此，历史描述统计可以报告，Context/Location/Signal 质量、WAIT 漏失机会、目标结构支持度及 A0 对照质量目前不可由脱敏样本判定；A1/A2/A3 的历史性能对照应标记 NOT_RUN。V35 对 10 笔交易的代理成本审查也只说明其假设下的经济性，不是 Gemini 市场判断能力结论。

## 结论

生产系统已有四周期选择、闭合/可用性过滤、因果 Swing 证据、结构事实与模型解释分层以及预算化 Prompt 压缩；本次没有证据支持“Gemini 已看到过未来 K 线”的结论。可进一步明确审计的边界是：同刻时间顺序、上游重复结束时间折叠、缺失历史原始模型输入、压缩 candle 的逐根时间戳不可见。V36 在隔离研究路径中对这些边界采用严格时间切片、重复拒绝、输入哈希和证据引用；生产风险参数及 V35 2.0 净盈亏比/止损约束保持不变。
