# V2 G0 — 已消耗研究授权重放审查

日期：2026-10-11
候选分支：`codex/v2-g0-integration-20261010`
审查基线：`fa5832f7f81c8cb5438aecc5b8bead9e02ce9cfc`（PR #26 修复前 head）
范围：只处理 V38 两个已耗尽研究授权在空账本克隆中可再次执行的问题。没有调用模型、交易所或其他外部交易服务。

## 结论

发现授权去重仅覆盖同一 Git 克隆内的共享账本。授权记录明确指出独立克隆未协调；在新克隆空账本上，两个执行入口仍可能建立模型调用意图并进入 provider 调用。已用 fake provider 和临时空账本复现，再增加版本化、默认 fail-closed 的撤销登记，并在执行库函数和 CLI 入口阻断两个已耗尽授权。

这项修复限制于已识别的两个旧授权。它不提供跨设备的在线原子授权服务；未来新授权耗尽后，必须先核对账本证据并将其撤销登记纳入受审代码版本，才能保证新克隆也拒绝该授权。PR 仍需同 head hosted CI 与人工审阅，本报告不代表可合并或 G0 全部通过。

## 原始证据及不可变性

| 项目 | 原始观察 | SHA-256 |
|---|---:|---|
| A1/A2 授权文件 | 上限 72 次调用意图 | `cbb67684cb36b20d5103ef84be083938faae43f99a7b6768832504d2a9d706c3` |
| A1/A2 原始账本 | 144 行：72 `DISPATCH_INTENT`、71 `RESULT`、1 `PROVIDER_STOP_REARM`；71 个结果中 67 `INVALID_JSON`、4 `CALL_ERROR_NO_RETRY` | `820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7` |
| JSON 格式 pilot 授权文件 | 上限 1 次调用意图 | `8d244a409d79a66d6c7766dba73ad0dc01a60d6cafb96bc8699c91a69b418354` |
| JSON 格式 pilot 原始账本 | 2 行：1 `DISPATCH_INTENT`、1 `RESULT` | `4852f2f893f3f68fca325d5ff07621958c42336861dc3ced2d59da423aebdf19` |

授权文件和原始账本均未修改。原始账本位于本机忽略的研究报告目录，不纳入 PR；登记文件只记录相对证据路径、摘要、授权上限及观察计数。PR 修改未触碰交易策略、Gate 风控、生产配置、V25–V38 历史结果或数据库。

## 根因与复现

1. V38 runner 将幂等账本定位于当前克隆的 Git common directory。该机制覆盖同一克隆的 linked worktree，不覆盖独立 clone。
2. 两个已耗尽授权都可从 JSON 文件重新加载。克隆账本为空时，旧执行校验无法获知别处已使用的调用意图。
3. 修复前的 failure-first 离线测试分别通过 campaign 与 format-pilot 执行库函数传入 fake provider 和临时空账本；预期的拒绝异常没有出现，证明代码可继续走向 fake provider。测试没有初始化真实模型客户端，也没有发出网络请求。

## 修复内容

- 新增 `configs/research/authorizations/revocations-v1.json`，精确登记已耗尽的 A1/A2 与 pilot 授权 ID。
- 新增 `authorization_guard.py`：校验撤销登记 schema、唯一 ID、调用上限与观察计数、安全相对路径及 SHA 格式；并在运行时核对授权文件的实际 SHA-256 和授权 ID。登记或绑定授权文件缺失/损坏时 fail closed。
- 在 campaign 和 pilot 的执行库函数加撤销检查；在两个 CLI 的模型客户端/路由探测初始化之前再检查一次，避免入口旁路。
- Dry-run summary 明示 `authorization_execution_status`，但 dry-run 不创建客户端、不生成调用意图。
- 为历史 runner 的 fake-provider 单元测试增加局部 guard 替身；新的专门回归测试保留真实 guard 行为。
- 不自动缩小调用数、重试请求、重写响应或更新旧研究账本。

## 验证

- `python -m pytest -q tests/v38`：154 passed，最终 guard 和授权哈希绑定代码版本。
- 提交钩子执行 `python -m pytest -q`：2528 passed、1 skipped、1 个 Starlette/httpx 弃用警告，6m07s；无测试失败。
- `ruff check` 覆盖新增 guard、修改的 runner/pilot/CLI 与相关测试：通过。
- `ruff format --check` 覆盖新增 guard 和新测试：通过；没有对原有大型文件进行整文件重排。
- `git diff --check`：通过。
- 回归覆盖：两个已耗尽 ID 被识别、未登记 ID 不与撤销 ID 混淆、登记缺失/损坏或授权哈希不符时 fail closed、两个执行库函数在空账本下不调用 fake provider/不创建账本，以及两个 CLI 在初始化模型客户端和 route GET 前拒绝。
- hosted CI 尚待修复后的 PR head；原 PR head 的既有绿灯不能替代修复后同 head 验证。

## 安全边界、回滚与后续条件

- 远端 registry 是受 Git 审阅的撤销快照，不是密码学签名或跨设备在线原子账本。拥有仓库写权限的人仍可更改/删除它；这属于剩余权限风险。
- 未被登记的授权仍由其现有授权校验和本地账本约束。新一轮研究授权必须使用新 ID；达到上限或发生部分/歧义 dispatch 后，应冻结账本哈希和意图计数，再追加撤销登记，禁止复用该授权。
- 如果撤销文件缺失或格式损坏，执行 fail closed；回滚代码时必须保留撤销数据，并且不得恢复这两个已耗尽授权的可执行状态。
- 合并门槛：修复后精确 PR head 的 quick/full CI 通过、G0 组合候选回归无新增失败、差异复核无越界改动。即便这些条件满足，本报告也不授权自动合并。
