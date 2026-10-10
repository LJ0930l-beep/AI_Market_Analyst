# V2 G0 组合候选验证记录 — 2026-10-10

## 候选范围

- 基线：`origin/main` `8642639a78ca9aa56fbad1e88aea4b82169ea433`
- 合并顺序：PR #20 `43eb1835ef76b8c39f9b560ceaa3cac1b436d1bf` → PR #21 `941e38372a0c0f96d1aaecd4a4ca2faf62dd3070` → PR #22 `cb03f8e34cedca18d735187fdab41cda388eaa99` → PR #25 `97d138fc3f8072668af1bbeada3475d612f55a7d`。
- PR #25 的 ancestry 携带 PR #23/#24 修复；PR #23 红色旧 head 仍保持原状态。
- 候选分支：`codex/v2-g0-integration-20261010`，仅在本地隔离工作树验证，不改 `main` 或源 PR 分支。

## 冲突处理

合并时有 5 个内容冲突：

1. `core/macro_calendar.py`：采用调用方固定的 `now` 计算状态，保留 `utc_datetime(fetched)` 以比较 aware/naive 时间戳时统一到 UTC。
2. `core/trading/gate_testnet_e2e.py`：保留 exact order ID、symbol、contract、终态和顶层 `reduce_only` 核验。Gate adapter 先校验原生 `reduce_only`/`is_reduce_only`/`is_close`，冲突或 false 会拒绝，并只在验证后返回顶层 `reduce_only=True`；E2E 不重复要求原始 payload 必须使用某一个字段名。
3. `tests/test_gate_testnet_ai_chain.py`：保留 fixture 对原生字段两种表示的覆盖。
4. `tests/test_macro_actuals.py`：复用 provider 内注入的 clock，保留确定性时间 fixture。
5. `docs/plans/V38-V42-master-implementation-plan.md`：保留 PR #20 与 PR #25 两侧的独立追加记录，不删除旧快照或把历史状态改写成当前状态。

所有冲突标记已清除。原始历史账本和 Git 忽略的市场/交易报告没有纳入合并或重写。

## 验证结果

环境为 Windows、Python 3.12。测试命令：

```powershell
python -m pytest -q tests/test_macro_actuals.py tests/test_gate_testnet_ai_chain.py tests/test_gate_provider_timeframes.py
python -m pytest -q tests/v37_1 tests/v38 tests/test_v35_trade_feasibility.py tests/test_gate_entry_economics.py tests/test_ai_simulation.py tests/test_v39_sse_fault_injection.py
python -m pytest -q --junitxml=<worktree>\pytest-candidate.xml
```

结果：

| 集合 | 通过 | 失败 | 跳过 | 备注 |
|---|---:|---:|---:|---|
| 冲突关联回归 | 38 | 0 | 0 | 1 条既有 Starlette/httpx 弃用警告 |
| 仓库 CI 快速门禁 | 222 | 0 | 0 | 与 `.github/workflows/offline-tests.yml` 命令一致 |
| 组合候选完整套件 | 2,517 | 0 | 1 | 1 条既有弃用警告 |
| `origin/main` 同环境完整基线 | 2,113 | 103 | 1 | 103 个原有失败节点作为基线登记 |

JUnit node-id 差集：**103 个基线失败节点已消失；0 个新增失败节点；0 个基线节点缺失；候选新增 301 个测试节点**。完整测试集合包含所有快速门禁测试。

完整 JUnit 产物留在本机 worktree，未提交庞大原始 XML：

- 基线：`C:\Users\baicha\.codex\worktrees\v2-g0-baseline-20261010\pytest-baseline.xml`，SHA-256 `ec187fb87f395a5bb3949d358b13614b73dacfbfa873dc7d8a451ef54374ca9b`。
- 候选：`C:\Users\baicha\.codex\worktrees\v2-g0-integration-20261010\pytest-candidate.xml`，SHA-256 `dad101a071100b00d9071936e13233f1efcfa8813e82a1acd3d29fbbe9c0a733`。

## 远端状态与验收边界

PR #25 文档提交 `97d138fc3f8072668af1bbeada3475d612f55a7d` 的 GitHub Actions run `38042151994` 快速门禁已通过 222 项，完整套件仍在运行。它不是本地组合候选的远端结果。组合候选尚无 hosted exact-head workflow run。

**状态：本地组合验证通过，G0 仍待远端验收。** 后续将把候选作为 draft integration PR 提供审查，并等待相同候选 SHA 的 GitHub 快速/完整门禁；没有人工审查前不合并。若 CI 或审查发现问题，只修复隔离候选并重新验证；源 PR、`main`、历史报告和交易权限保持不变。

本轮只运行离线测试；未调用 Gemini、Gate TestNet 或 Live，未提交订单，也未修改生产账户配置。
