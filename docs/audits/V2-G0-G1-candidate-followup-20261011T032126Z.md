# V2 G0/G1 候选增量复核 — 2026-10-11 03:21Z

## 范围

此增量复核承接 [02:30Z 全量 PR 快照](PR-INTEGRATION-MATRIX-LIVE-20261011T023050Z.json)，只刷新 PR #26/#27 的 Gate 字段校验和候选同步状态，不改写旧快照、历史账本或其他 PR 的历史检查结果。

完整机器快照：[V2-G0-G1-candidate-followup-20261011T032126Z.json](V2-G0-G1-candidate-followup-20261011T032126Z.json)。

## 修复与验证

- PR #26 新 head：00d1c4405a513b9e5cdee9f6fa15808b4386d8b1。仅把 Gate order_type 的子串判断收紧为官方列出的六种精确枚举；is_close=false 与 is_reduce_only=true 的部分减仓语义仍分开处理。细节见 [Gate 字段与类型复核](V2-G0-order-type-enum-review-20261011.md)。
- 目标测试：33 passed，1 条既有 Starlette/httpx 弃用警告。
- 提交硬门禁：2,537 passed、1 skipped，1 条既有 Starlette/httpx 弃用警告，377.27 秒。
- PR #26 精确 head 的 GitHub run [38107795624](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38107795624)：快速门禁通过；全量门禁在本次 03:21Z 快照时仍运行中。
- PR #27 远端当时仍是旧 head 57b987a...，其旧 head run [38105897070](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38105897070) 通过。已在本地将新 PR #26 head 合入候选 af258eecf2cbb80bf764be67349cec3becb0f873；该同步候选当时尚未推送，也没有新精确 head CI 结果。此报告提交会推进 PR #27 head，随后必须看新的 GitHub 检查结果。

## 门禁决定

- **G0 仍为 FIX_IN_REVIEW。** PR #26 全量 CI 尚在运行；PR #27 同步后需新的精确 head 检查；两条 PR 均无人工审查且保持 Draft。不得合并。
- **G1 仍仅通过离线契约层。** 远端 response_format 行为、实际模型身份/权重证明与账单验证仍未知，G2 不解锁。
- 不调用 Gemini，不请求 Gate TestNet 或 Live，不提交订单，不修改账户/生产参数，不启用自动合并。

## 下一步

等 PR #26 全量检查结束；推送同步后的 PR #27 审计提交并确认它的新精确 head 全套 CI。只有检查通过后，才把两个 Draft PR 交给人工审查；G0 接受仍需人工审查和明确合并决定。
