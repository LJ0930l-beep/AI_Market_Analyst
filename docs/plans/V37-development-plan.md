# V37 Historical Evidence Reconstruction 开发计划

## PLAN

- 基线：`codex/v36.1-research-integrity`，提交 `23ca359082193033d9c40ee282c41a074eee53a2`；本分支 `codex/v37-historical-evidence` 从该精确提交创建。
- 目标：建立历史事实与重建代理严格分离、可审计、可重复构建的研究输入；不优化 Prompt/策略、不训练或调用模型。
- 安全边界：历史 SQLite 只读；不私连 Gate、不改账户/订单/生产风控；不覆盖任何旧样本/报告；公共提交不含本地数据库、未脱敏 Prompt 或研究运行结果。
- 先验待证假设：本地 `data/market_analyst.sqlite3`、V25/V31/V32/V33 报告和 V33 脱敏响应归档真实存在，但其表结构、覆盖范围及与 V25 `decision_id` 的关联尚未证明。未完成盘点前不把它们称为可恢复证据。

## INVENTORY

1. 只检查任务书点名的源码、跟踪的 V25/V33 资产及本机相关研究目录。
2. 对每个候选资产记录路径、SHA256、来源说明、表/字段结构、可公开的覆盖区间/计数、可用性依据、敏感性和能支持的实验；SQLite 通过 `mode=ro` 与 `query_only` 读取，不查询或输出账户/密钥/订单明细。
3. 按 `EXACT_OBSERVED`、`VERIFIED_ARCHIVE_RECONSTRUCTION`、`ASSUMED_PROXY`、`UNAVAILABLE` 分级。先盘点，再选择最小的恢复实现；没有关联证据的数据明确判为不可匹配。

## RECONSTRUCT

1. 从 V35 提交 `2a5d7b9992a7909a6a597917e409ecb0737f9c89` 的 `core/trading/model_schemas.py` 提取 `AI_ACTION_SCHEMA`，校验字节规范化 SHA256，冻结到 `configs/research/schemas/a0-v35-action-schema.json`。A0 不再从运行时生产模块加载 Schema；旧记录缺少原始版本证据时只报未验证，不重标。
2. 在 V37 专用研究包中提供因果行情证据构建器，复用 V36 输入校验；输出逐 bar 来源、文件哈希、收盘与可用时刻、可用时刻依据及可信度。只允许完整、严格升序、无重复、无缺口且在决策时已经可用的记录进入观测集；延迟假设只能进入独立代理集。
3. A0 恢复只接受完整 V36.1 精确身份；V25 与不同运行/标的/时间的响应缓存不做近似拼接。
4. 历史权益、保证金、费用和时点合约规则缺失时将经济可行性记为 `BLOCKED`/`UNKNOWN`；情景假设单独标记代理，不用假设值填充历史事实。
5. 研究输出写到 ignored `reports/v37/` 新路径，独占创建；报告和 manifest 仅提交脱敏摘要。

## VERIFY

- 测试固定 Schema 哈希与生产 Schema 解耦。
- 测试 SQLite 只读、因果边界、未收盘高周期、未来 Swing 确认、重复/乱序/缺口和哈希稳定性。
- 测试缓存身份、Schema/模型身份/证据引用失败关闭，账户/合约信息缺失时经济状态不通过。
- 测试脱敏、样本分区隔离、重复构建确定性、V25 源哈希不变、零模型调用及零订单。
- 运行 V37、V35/V36/V36.1 定向回归；全仓 pytest 记录失败 node ID 和阶段，与同环境基线逐项比较，不以失败总数替代对照。

## AUDIT → ACCEPT

- 逐项核对本计划与六份 V37 交付文档、忽略文件下的本地报告、测试对照及 Git 差异。
- 只有在证据资产可验证时才认定已恢复决策点；如果 A0 不可恢复，明确列出阻断字段及不可恢复原因，仍可验收审计和重建 benchmark，但不宣称完成 V25 A0 对照。
- PR base 固定为当前 V36.1 分支，保持未合并；V38 只能在准备度报告列出的数据和身份条件满足后开始受控模型实验。

## 修改范围

计划修改 V37 专用 `core/replay/pa_decision_quality_v37/`、`configs/research/schemas/`、最小离线脚本、V37 测试和任务书列出的文档。不会修改 `core/trading/`、Gate 执行路径、生产资金/风险配置、V25/V33/V34/V35/V36/V36.1 既有源码与报告、历史 SQLite、V25 原样本或根工作区未跟踪文件。
