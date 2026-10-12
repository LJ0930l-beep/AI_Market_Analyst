# B-20261012-005 — V38 多响应 choice 静默择一

```yaml
id: B-20261012-005
severity: S1
status: FIX_IN_REVIEW
component: model
mode: RESEARCH_OFFLINE
base_sha: 5f70d186d169261f0f337232c70e246a2e88b4e6
trigger_signature: V38_RESPONSE_CHOICES_MULTIPLE_SILENTLY_FIRST
first_seen_at_utc: 2026-10-12T19:15:10Z
user_authorization: DOCUMENTED_OFFLINE_REPAIR_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: NONE; immutable V38 ledger and samples unchanged
immediate_stop_or_isolation: Keep remote provider dispatch disabled; do not retry old intents or accept multi-choice envelopes as valid.
evidence_files_and_hashes: docs/audits/V2-G0-G1-live-state-20261012T191510Z.json SHA-256 756a4b09584a0d168490464ba863521db6f52f91e8ff93fd9e86f7ccc071ebcc; core/replay/pa_decision_quality_v38/response_contract_v2.py SHA-256 872413fa6420a5a3093f329e809004ac0c5c755db7ad66cf87d8b2032c169cd6; tests/v38/test_response_contract_v2.py SHA-256 a14f5c83c02119b10f7f18e5ec26a02b61445383e9075565bd133c663c7bdedb
reproduction: On base code 5f70d186, add the failure-first regression test and run python -m pytest -q tests/v38/test_response_contract_v2.py -k multiple_provider_choices; the test fails because two choices are accepted using only the first result.
root_cause_status: CONFIRMED
safe_fallback: Reject any response whose choices field is not a one-element list containing an object; keep remote dispatch disabled.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27 (stacked G1 offline branch; Draft; no auto-merge)
negative_tests: Multiple choices with conflicting/truncated second content; invalid choice count; non-object choice; all existing V2 schema, evidence_refs, finish_reason, identity, empty/truncated-response and offline-runner tests.
unblock_criteria: Push the repair to PR #27; require quick, full, and Web/lint/type-check/build hosted checks to pass on the exact updated head; confirm immutable V38 ledger/sample hashes remain unchanged. This does not establish remote response_format, model identity, billing, quota, or G0 integration approval.
needs_human_decision: true
next_owner_action: "Record independent review and G0 integration decision; keep provider dispatch disabled and G2 unauthorized."
rollback: Revert only the multiple-choice evaluator and regression-test change if exact-head review or CI rejects it; retain this blocker history, prior audit snapshots, V35 risk controls, and immutable research records.
```

## Finding and repair

The evaluator selected `choices[0]` without validating the count. A response with a valid first choice and conflicting or truncated later choice could therefore be classified as `VALID_LOCAL_CONTRACT`. The regression test first reproduced this on the unmodified base evaluator.

The local repair accepts exactly one object in `choices`; missing, empty, multiple, or malformed choices fail closed as `INVALID_PROVIDER_RESPONSE`. It adds no provider call, retry, trading behavior, or strategy change.

## Local verification and remaining checks

Latest local verification: the complete V38 suite passed 212 tests in 28.94s, the repository quick gate passed 289 tests in 31.11s, and the commit hook passed 2,602 tests / 1 skip. PR #27 exact head `f4461a72486b8aba132eb23045e03c9b1ffe0ba3` passed hosted run [38298606199](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38298606199): quick 289; full 2,603 / 25 subtests / 1 warning; Web 29 files / 161 tests; lint, type-check, and build succeeded. This does not replace independent human review or establish remote model/provider capability.

The frozen V38 ledger, historical samples, V35 risk controls, and production configuration were not modified. No Gemini, Gate, TestNet, or Live API was called; no order was submitted. The provider's structured-output capability, authenticated model/weight identity, billing, and quota remain unknown.

## 2026-10-12T19:54:50Z Hosted verification

The repair passed all automated checks on PR #27 exact head `f4461a72486b8aba132eb23045e03c9b1ffe0ba3` in run [38298606199](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38298606199). B005 remains `FIX_IN_REVIEW` because PR #27 is still Draft and independent human review/G0 integration approval have not occurred. No model or exchange call, order, production setting change, or historical evidence rewrite occurred.
