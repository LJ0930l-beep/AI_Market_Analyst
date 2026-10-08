# V36 开发计划

## 基线与边界

- 分支 codex/v36-pa-decision-quality 从 V35 提交 2a5d7b9992a7909a6a597917e409ecb0737f9c89 派生。GitHub 检查显示 PR #1 未合并；V36 保留对 V35 的依赖，不从旧 main 覆盖。
- 架构审查已确定隔离设计。获批的 unsandboxed worker 在实现期因账户额度停止；剩余实现与独立验证由当前执行上下文完成，没有把中断的 worker 结果记为通过。
- 新增实现限制在 V36 专用研究包、脚本、research 配置、测试、文档和独立 evidence 目录。生产 Prompt、AI_ACTION_SCHEMA、Gemini 路由、Gate 执行、真实账户资金配置、仓位默认值以及 V25/V33/V34/V35 历史资产均保持不变。
- CLI 默认离线。启用新模型调用必须同时给出 --run、正整数 --max-decisions、显式 --caller module:function 和 --model-id。当前阶段未加载调用器、未发起 Gemini 调用、未开启 Live、未提交订单。

## 设计

V36 专用包提供冻结输入、因果 Context/Location/Signal 事实、A0/A1/A2/A3 实验定义、显式模型调用预算、精确缓存来源、只读复盘和阶段指标。15m/5m/1h/4h 的每根 bar 必须在决策时刻前完成收盘且数据已可用；同刻、重复、乱序、缺口、错误 OHLCV、过期或来源未知的数据 fail closed。

模型输入包括有时间戳和证据引用的近期 candles（15m 8 根、5m 6 根、1h/4h 各 4 根）及从最多 240 根历史计算的有界事实；A1/A2 序列化提示上限为 32 KB。Python 不替 Gemini 决定市场环境或方向。模型需分别报告 Context、Location、Signal、信号质量、理由、反向证据和未来假设；未来假设不会被标为已确认事实。

OPEN 结果必须列出触发、结构失效、入场、止损、目标结构及证据、净盈亏比、名义金额、杠杆和订单类型。模型提案不被静默缩小；净盈亏比 2.0 和单笔止损风险不高于配置中的 V35 上限继续使用共享 V35 经济计算。A3 固定为非生产失败突破研究规则。

A0 只用可由原始 V35 Prompt、模型输入、状态、版本和决策时间重新计算的精确缓存键。A1/A2 的缓存键还包含冻结行情、状态、约束和 Prompt 哈希。模型输入排除结果字段；review 输入只在决策记录生成后读取，并要求 experiment_id 与 decision_id 精确匹配。成交、完整平仓和模型成本分别统计，不从提案或委托接受推断成交。

## PLAN → IMPLEMENT → TEST → AUDIT → REVIEW

1. PLAN：只读审计生产与历史回放链路、压缩周期、V25 样本与缓存范围，记录边界于 docs/audits/V36-price-action-input-audit.md。
2. IMPLEMENT：新增隔离研究模块、Schema、运行与复盘 CLI、配置、输入格式说明和测试。CLI 输出采用独占新建，不覆盖原始输入。
3. TEST：V36 定向 pytest、Python compileall、Ruff 0.16.4、git diff --check；旧失败测试采用隔离副本在同一解释器和环境下做完整 node ID 对照，不删除或重标历史失败。
4. AUDIT：核对 V25 样本 SHA256、生产代码差异、V35 风险约束、未来数据隔离、实验组缓存键、A0/A1/A2/A3 状态及报告中提案/接受/成交/完整平仓的区别。
5. REVIEW：只提交 V36 代码、配置、测试和审计文档；本地 ignored reports 不强制上传。凭据和敏感数据扫描后推送 codex/v36-pa-decision-quality 并创建未合并 PR，等待人工审核。

## 基线与样本

- 修改前工作区 JUnit 锚点 reports/v36-feasibility/pytest-baseline.xml：2,238 项，100 failed、2,137 passed、1 skipped、0 errors；SHA256 A9C5A944987AAFE0876E731A7C2FA190F3193685837CE68888101524F57D7085。
- 隔离副本运行旧测试集合另有一份可复现基线：修改前后均为 2,065 passed、172 failed、1 skipped、0 errors。修改后按全部 node ID、phase、环境、收集结果和退出码比较为 PASS；没有新增/消失失败、环境差异、缺失测试或丢失通过项。其环境与工作区 JUnit 不同，172 与 100 个失败不混为一组。逐项结果见 evidence/v36-pa-decision-quality/baseline/pytest-comparison.json。
- tracked V25 样本包含 100 次已完成扫描、13 个被接受的模拟开仓事件和 10 笔完整平仓。10 笔平仓不与前 100 次扫描简单一一对应。样本没有同期完整 K 线、原始模型输入、时点账户权益/保证金和完整 Gate 合约规则。
- V35 已有报告在代理成本下得出 10 笔中 9 笔净盈亏比低于 2.0。该结论属于 V35 经济代理审查，不是 V36 对 Gemini 价格行为判断能力的结论。

## 实验设计

| 组 | 内容 | 本阶段约束 |
|---|---|---|
| A0 | V35 原始 Gemini 决策 | 仅精确缓存；缺原始输入或状态则 NOT_RUN |
| A1 | 增加因果 Context / Location / Signal | 模型仍自主解释；不写生产 Prompt |
| A2 | A1 加客观目标空间和最强反证 | 目标缺乏结构证据时 WAIT，不为通过净 RR 拉远目标 |
| A3 | 固定失败突破规则 | 无 Gemini、无生产权限、不得强制下单 |

所有组只有在行情、费用、合约规则、状态和模拟执行假设一致时才能比较。缺少 V25 同期输入时，A0–A3 和两种仓位模式均报告未运行或无法计算候选，不推断决策质量或收益改善。模型费用没有已验证价格依据时记为 UNKNOWN。
