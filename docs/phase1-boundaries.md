# Phase 1 boundaries

## Data Provider

`core/providers/base.py` 只负责 `Quote`、`Bar` 和 `MarketProvider` 协议。股票和 Crypto 统一进入 `Instrument`；Provider 不做方向判断、不写交易记录、不持有私钥。

`FixtureProvider` 是默认验收 Provider，保证没有网络时仍可重放 AAPL/NVDA/TSLA/AMD/BTCUSDT/ETHUSDT。`YFinanceProvider` 是可选公共行情适配器，必须显式安装额外依赖。

## News Provider

`core/providers/news.py` 只定义 `NewsEvent` 与 `NewsProvider`。事件的来源、时间、符号、分类、情绪和重要度是结构化数据；Phase 1 不让 LLM 参与数据抓取或数值计算。

## Quant Engine

`core/quant/engine.py` 生成可序列化的 `QuantSnapshot`，包含 EMA20/EMA50、RSI14、MACD、ATR14、成交量比、支撑/阻力和市场状态。所有价格与指标由 Python 计算。

## Signal

`core/signals/schema.py` 的 `SignalProposal` 是 Prediction 契约。LONG/SHORT 必须包含 Entry/Stop/TP1/TP2、时间边界、失效条件和置信度；WAIT 是合法结果且不携带可执行价位。`model_id` 和 `input_hash` 为后续模型审计保留。

## PaperTrade

SQLite 的 `paper_trades` 表只在用户明确跟随 Prediction 时写入。系统没有 Broker/Exchange order client，也没有自动下单路径。

## Outcome

`core/outcomes/engine.py` 只读取 Prediction 和后续 Bar，结算 TP1/TP2/STOP/TIMEOUT，并记录 MFE/MAE/R。若单根 K 线同时触及止损和目标，采用止损优先的保守规则，避免伪造盘中顺序。

