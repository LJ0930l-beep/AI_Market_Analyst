# V2 G0 — PR #23 全量 CI 失败的根因与候选闭合证据

## 事件与状态

- Blocker：`B-20261010-001`，严重度 `S1`（集成 / CI）。
- 原始事件：PR #23 head `5831f9785e5f8b09d6a819c562949beceb6db4b0`，GitHub run [37963206552](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/37963206552) 失败。quick gate 为 177 passed；full suite 为 **3 failed、2,462 passed、25 subtests passed**。
- 处置状态：三个失败在 PR #24 的时钟确定性修复 `5e9ef3781a06cdab1f70353242fdd8124bf2ad11` 中复现并修复；PR #24 CI [37966405672](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/37966405672) 成功。修复已包含在 PR #26/#27 整合候选中。原始 PR #23 失败记录保持不变。
- G0 状态仍为 `FIX_IN_REVIEW`：需要对 PR #26/#27 进行人工审阅与明确的合并决定；本报告不构成自审或合并授权。

## 根因

PR #23 的宏观日历测试使用固定的 2026-10-02 日程样本。运行时已到 2026-10-09，样本超出七天回看范围。测试向 `refresh_calendar()` 传了历史 `now`，但在注入实际值 provider 时没有同时固定 scheduler `clock`；后续日程筛选与 `calendar_status()` 仍读取宿主机当前 UTC 时间，因而测试样本被排除或错误判为过期。这是测试时钟未统一造成的确定性缺陷，不是交易下单或 Gate 行为故障。

原始失败节点：

1. `tests/test_macro_actuals.py::test_bls_writer_availability_is_completion_time_and_reader_has_asof_cutoff`：断言 4 条事件，实际 0 条。
2. `tests/test_macro_actuals.py::test_bls_period_mismatch_retries_after_15m_and_then_uses_one_batch`：读取空事件列表触发 `IndexError`。
3. `tests/test_macro_actuals.py::test_stale_actual_is_retained_with_error_after_later_fetch_failure`：读取空事件列表触发 `IndexError`。

## 修复与验收

PR #24 的提交 `5e9ef37` 为 `calendar_status()` 增加显式 `now` 参数，并由 `refresh_calendar()` 将有效时钟传递给状态读取；三个测试都注入一致的 scheduler clock，保留原业务断言。普通生产调用仍默认使用真实 UTC 时钟。

当前候选的复核证据：

- 本地 `python -m pytest -q tests/test_macro_actuals.py tests/test_final_calendar.py`：**16 passed**。
- 本地三条原失败节点重跑：**3 passed**。
- PR #24 的精确 head CI run `37966405672`：成功，full suite 2,465 passed、25 subtests。
- PR #26 整合 head `00d1c4405a513b9e5cdee9f6fa15808b4386d8b1` 的 exact-head full CI run `38107795624`：成功。
- PR #27 最新 head `5346f0375a649e78123221ab57d1e55b759bb1ab` 的 exact-head full CI run `38111025018`：成功，quick 284 passed；full 2,591 passed、25 subtests。
- 当前 PR #26 候选包含 PR #1–25 全部 head（逐个 `git merge-base --is-ancestor` 检查，遗漏 0）。PR #1–11 的独立 checks 仍记录为 `NO_CHECKS`，但其代码已在通过 full CI 的整合候选中一同执行。

## 临时措施、回滚与边界

- 临时措施：始终保留 PR #23 的失败结论和 run 链接；在整合候选 full CI 与人工审阅前，不合并任何 PR。
- 回滚：若 PR #24 时钟参数传递引入新的回归，只回退提交 `5e9ef37` 的日历时钟透传与测试注入，并把 blocker 重新标为未解决；不删除或改写 PR #23 的失败节点与 CI 结果，保留最后一个已验证的隔离候选。
- 本次调查和修复验证未调用 Gemini 或 Gate API，未使用交易凭证、未产生 TestNet/Live 订单，也未改动风险参数、生产交易路径、历史账本或模型原始回包。
- B-002 的 provider `response_format` 原生能力、远端模型权重/身份与账单仍未知；此处 CI 结果不解锁 G1 以外阶段，也不授权 G2。

## 下一关

由人工审阅 PR #26/#27 的完整 diff、依赖与回滚点，作出明确合并决定。若审阅发现问题，按 S1 隔离并在独立候选修复；否则保持现有 Draft 状态直到审阅授权完成。G2 仍需新的明确、限额且有停止规则的授权。
