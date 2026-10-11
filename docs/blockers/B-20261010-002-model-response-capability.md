# B-20261010-002 — 响应 envelope 已定位，代理结构化能力仍未知

```yaml
id: B-20261010-002
severity: S1
status: OPEN
component: model
mode: RESEARCH_OFFLINE
branch_sha: fcfc9ec37102d0ec4a09fc777fec0e44d20291cc
trigger_signature: FENCED_JSON_RESPONSES_AND_RESPONSE_FORMAT_CAPABILITY_UNKNOWN
first_seen_at_utc: 2026-10-10T09:27:00Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: 67 immutable INVALID_JSON results and 1 separate consumed format pilot
immediate_stop_or_isolation: No new Gemini call, no retry, no rewriting/relabeling old result; validation and untouched-test remain sealed.
evidence_files_and_hashes: docs/audits/V38-invalid-json-classification-20261010.json SHA-256 4ba18a5bbb41ad8525e334cf44bcad98ab6d5d98ac76823b49f0f6c871785db7; source ledger SHA-256 820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7
reproduction: Read-only _read_ledger + review_gemini_ledger, then RAW_OR_SINGLE_JSON_FENCE_V1 parse and V38 schema/evidence validation; no network path.
root_cause_status: HYPOTHESIS
safe_fallback: Strict local parser/schema validation with capability PROMPT_ONLY_UNVERIFIED and offline fake-provider tests.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/25
negative_tests: Empty/partial/truncated body; finish_reason length/error; missing/mismatched model ID; malformed/multiple fence; invalid/unknown/future evidence refs; unsupported response_format; duplicate intent; usage mismatch.
unblock_criteria: New immutable Prompt/Schema V2 and capability labels; full offline negative matrix passes; raw replies/hash and cost-unknown state retained; G0 integration candidate passes before G1 acceptance; separate user authorization before any new paid request.
needs_human_decision: true
next_owner_action: Freeze V2 contract and implement/test offline capability and response failure matrix; do not call the model.
```

## 已证实与仍未知

旧解析器对完整 `message.content` 直接执行 `json.loads`。只读复算显示原始账本 67/67 个 `INVALID_JSON` 均是单层、完整 JSON fence；新的兼容解析和现有 V38 schema/evidence_refs 诊断 67/67 通过。源账本校验前后均为 SHA-256 `820212a5699b718c922232c584fcfdcb4f79b675ea8653b1898a0d4025b837a7`。

这证实本地 envelope 解析缺口，不证明 Antigravity/Gemini 是否支持、忽略或透传 `response_format`。账本未保存完整 HTTP response body，usage 67/67 算术不一致，费用未知。独立 one-intent pilot 的结果不计旧研究，也因复用样本而无 quality eligibility；它的授权已经消耗。

## 处置、验收与回滚

- 临时方案：旧响应及 72/71 分母保持原样；只用本地固定回包和当前版本化 parser 做诊断。所有未知保持 `UNKNOWN`，不得增发请求以“确认”。
- 修复：新建与 V1 并行的 Prompt/Schema V2 与最小 parseability 阶梯；明确 capability enum，当前路线默认 `prompt_only_unverified`。增加 empty、partial、`length`、错误 finish、身份缺失/不符、坏引用、代理包装和重复意图的 fake-provider 负例；确保原始文本哈希永久保留且旧 ledger 不可写回。
- 验收：G0 候选先通过；新 parser 与 schema 负例在本地和候选 CI 通过，unknown provider capability 明确拒绝提升；无新的 Gemini 调用。任何 G2 付费试验另需用户新授权和独立冻结范围。
- 回滚：V1 runner、prompt、历史 ledger 从不覆盖；如果 V2 解析或 capability 声明回归，仅撤回 V2 分支改动并保持 RESEARCH_OFFLINE，不改旧结果。

## 当前状态

历史 67 条失败分类已完成；24 项 parser/format 专项测试通过，当前 PR #25 full CI 通过。该 blocker 仍 `OPEN`，因为完整 capability 合同和负例矩阵未完成，G1 尚未整体验收，G2 未授权。
