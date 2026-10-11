# V36 决策质量研究验收报告

## 验收结论

**V36 隔离研究基础：通过。历史质量对照：未运行。** 新增研究路径能建立严格的点时因果输入、分离模型判断与 Python 事实、为开仓提案调用 V35 共用经济诊断、按实验臂隔离复盘，并在默认模式下保持零模型调用。未修改生产交易路径、账户配置、风险默认值或历史样本。

**受控历史回测前置条件尚未满足。** V25 缺同期 OHLCV、原始 V35 提示/响应缓存和完整时点经济约束，因此 A0/A1/A2/A3 均 NOT_RUN；两种仓位模式的可行候选数均未知。

## 本次修改文件及原因

- core/replay/pa_decision_quality_v36/context.py：验证四周期时间、来源、质量、OHLCV 几何与连续性；只保留决策时点前已收盘且可用的 bar；为摆动、区间边界、突破和近期 candles 提供可追踪证据；提示输入有固定大小上限。
- core/replay/pa_decision_quality_v36/experiments.py：定义 A0–A3、独立 A1/A2 结构化提示、状态/约束白名单、精确缓存身份和 32 KB 上限。
- core/replay/pa_decision_quality_v36/schema.py：校验市场环境、高周期偏向、位置、信号质量、理由、证据、未来假设、OPEN 提案字段及绑定当前状态的管理提案；拒绝放宽止损、错误价位几何和事后结果字段。
- core/replay/pa_decision_quality_v36/runner.py：默认离线运行、显式总调用预算、缓存与模型响应记录、统一 V35 风险/费用/精度/保证金预检、只读且不缩仓的 A3 失败突破候选，以及不触发执行的持仓管理提案记录。
- core/replay/pa_decision_quality_v36/review.py：只读错误归因、WAIT 复盘、时延/Token/经济统计；结果必须按 experiment_id 和 decision_id 精确连接，提案、网关接受、成交、完整平仓分别计数。
- scripts/run_pa_decision_quality_v36.py、scripts/review_pa_decision_quality_v36.py：冻结 JSON 的离线执行与只读复盘 CLI；输出独占新建，不覆盖输入或已有结果。新模型调用需显式 --run、--max-decisions、--caller 与 --model-id。
- scripts/v36_test_evidence.py、evidence/v36-pa-decision-quality/baseline/run_offline.py：记录脱敏测试 node ID/phase 并在临时副本里跑旧测试；不导出异常文本、locals 或原始 stdout/stderr。
- configs/research/pa_decision_quality_v36.json、docs/research/v36-price-action-input-format.md、tests/v36/：提供研究配置、输入契约及自动化覆盖。
- docs/audits/V36-price-action-input-audit.md、docs/plans/V36-development-plan.md、docs/audits/V36-decision-quality-pilot.md：保存输入链路审计、设计/执行计划和历史覆盖边界。
- reports/v36-feasibility/v25-pilot.json：本地 ignored 覆盖报告，不纳入提交。

未修改 core/trading/、生产模型 Prompt、AI_ACTION_SCHEMA、真实账户参数、Gate 订单逻辑或 V25/V33/V34/V35 历史文件。

## V25 样本审计

输入 docs/research/v25-price-action-sample-20261008.json 的 SHA256 在开发前后均为 4193956555d7576a3a754fbeb80198ad5b4828079b574dedf71eb347a13353cb。样本包含 100 次扫描（WAIT 63、HOLD 24、OPEN_LONG 5、OPEN_SHORT 8）、13 个模拟 ACCEPTED 开仓事件，以及独立的 10 笔完整平仓（3 胜、7 负、净 PnL -4.77884509450 USDT）。模拟接受不是 Gate 委托或真实成交证明；平仓样本与 100 次扫描不能直接一一对应。

既有 V35 报告显示，在其代理费用和合约假设下，10 笔中 9 笔未达到 2.0 净盈亏比。由于 V25 没有精确原始 Prompt/模型响应、决策时 OHLCV、当时账户权益/保证金及完整 Gate 合约约束，该结果不判定 Gemini 的价格行为识别错误，也不表示风控拒绝消除了历史亏损。

## 验证结果

- V36 定向 pytest：41 passed。
- Ruff 0.16.4：新增 Python 包、CLI、证据工具和测试均通过。
- Python compileall：通过。
- git diff --check：通过。
- 隔离副本中的 pytest 证据插件自测：9 passed。
- 修改前本地 JUnit 锚点：2,238 项，100 failed、2,137 passed、1 skipped、0 errors；SHA256 A9C5A944987AAFE0876E731A7C2FA190F3193685837CE68888101524F57D7085。
- 本地 pre-commit 全量 pytest：100 failed、2,178 passed、1 skipped。与原有 100 个失败数相同；增加的 41 项通过来自 V36 定向测试。该硬钩子因此阻止了普通提交。
- 修改前与修改后隔离旧套件均为 2,065 passed、172 failed、1 skipped、0 errors。evidence/v36-pa-decision-quality/baseline/pytest-comparison.json 为 PASS：精确 node ID、phase、环境、收集结果和退出码均一致，没有新增/消失失败、丢失通过项或缺失测试。该隔离环境与本地 JUnit 不同，两个失败计数不能直接相减。
- 提交按该 pre-commit 钩子自身注明的继承失败例外路径使用 --no-verify；原因、全量测试结果和隔离对照均记录于提交说明与本报告。没有修改、删除或屏蔽旧测试。
- 未执行 Gemini 调用、Live、真实订单或大规模新回测。研究 fixture 仅是合成自动化测试数据，不是市场样本。

## 风险与遗留问题

- A0/A1/A2/A3 的历史绩效、市场环境正确率、入场位置、形态触发、止损和目标质量尚未评估。
- 当前 Gate 完整合约规则和当前账户可执行性未在本阶段读取；V25 代理值不能替代真实规则。
- 历史样本缺少同步账户状态及订单事件关联，因而固定模式和风险预算模式候选可行数未知。
- 开始受控回放前须恢复逐点因果 OHLCV、精确 A0 缓存、时点权益/保证金、完整合约/费用/报价输入，并冻结时间外验证集。

## V36 判定与下一阶段

研究基础达到验收目标：风险计算复用 V35 并 fail closed，两个实验仓位模式可独立诊断但不修改生产默认，历史结果可按实验精确隔离，原始样本未变，测试可复现。

历史对照和质量提升结论未达到验收目标，因为输入资产缺失。满足上列数据与身份前置条件后，可以启动小样本、非生产、显式限额的受控历史回放；在此之前不应扩大 Gemini 调用或把新研究提示写入生产。

本变更推送至独立 V36 分支并创建未合并 PR，等待人工审核；不自动合并或启用交易。
