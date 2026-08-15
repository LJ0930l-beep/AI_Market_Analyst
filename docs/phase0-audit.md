# Candidate base audit

审计对象：`mmovsesyan/stock_signal_analyzer`，默认分支 `main`，审计日期：2026-08-14。

## 结论

候选底座具备可复用的技术指标、交易计划、行情抓取和 Outcome 代码，但它不是一个适合直接进入 AI Market Analyst V1 的精简底座。主链路同时承载 Telegram、用户订阅、Celery/Redis、PostgreSQL、多家行情供应商、Kronos 预测模型和 LLM 学习，违反白皮书要求的“先闭环、后智能”和“本地优先、禁止真实执行”。本阶段不直接移植这些耦合模块，而是建立清晰的 Phase 1 边界。

## 实际能力与差距

| 领域 | 候选底座现状 | 对本项目的判断 |
|---|---|---|
| 技术分析 | `stock_signal_analyzer/technical.py`、`levels.py`、`regime.py`、`trade_plan.py` | 可借鉴算法；需要迁移到纯 Provider -> Quant 边界 |
| 行情 Provider | Yahoo、Polygon、Finnhub、MOEX、Tinkoff 等分散在多个模块 | 过宽；Phase 1 只保留公开 Provider 接口和离线 Fixture |
| 新闻/LLM | RSS、Finnhub 新闻、VADER/FinBERT、Ollama Cloud/Local | 接口可复用，LLM 调用延后到 Phase 2 |
| Signal / Outcome | 有交易计划、信号日志、`outcome_tracker.py` | 需要把 Prediction 与 PaperTrade 显式分离 |
| 后台任务 | `tasks.py` + Celery/Redis + scheduler | Phase 1 删除运行时依赖，先用同步可重放流程 |
| 用户/通知 | `telegram_bot.py`、订阅、告警、MAX/Telegram | 非目标；不进入精简底座 |
| 预测模型 | Kronos、ML scoring、backtest 组件 | 非 Phase 1；避免显存和样本量约束进入主链路 |
| 数据库 | PostgreSQL/SQLAlchemy，另有 SQLite 数据文件 | 单机 MVP 先使用标准库 SQLite |

## 风险与阻塞项

- 仓库根目录目录树未发现可识别的 `LICENSE` 文件，不能把候选代码当作已确认可再分发资产；当前实现只借鉴公开架构，不复制其源码。
- 候选仓库的 Docker 编排要求 PostgreSQL、Redis、Celery，并暴露 Telegram/Tinkoff/LLM Cloud 配置；这些依赖不适合作为本阶段启动门槛。
- 真实行情 Provider 需要网络和数据源稳定性；Phase 1 使用 Fixture 证明接口和生命周期，Phase 2 再接入真实行情并做回放。

## 复用 / 删除 / 重写矩阵

| 决策 | 内容 |
|---|---|
| 复用思路 | Technical / levels / regime / trade-plan / outcome 的职责划分 |
| 删除出主链路 | Telegram、MAX、订阅、用户管理、Celery、Redis、Tinkoff/MOEX 私有接口、Kronos、ML |
| 重写边界 | Instrument、MarketProvider、NewsProvider、QuantSnapshot、SignalProposal、Outcome、SQLiteStore |

