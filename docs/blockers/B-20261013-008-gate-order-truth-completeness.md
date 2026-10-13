# B-20261013-008 Gate order truth must prove snapshot completeness

```yaml
id: B-20261013-008
severity: S1
status: FIX_IN_REVIEW
component: gate_account_truth
mode: RESEARCH_OFFLINE
branch_sha: f856859cb83517729b28a56e3ed310f920ee643f
trigger_signature: GATE_FULL_ORDER_PAGE_MISREPORTED_AVAILABLE
first_seen_at_utc: 2026-10-13T05:01:06Z
user_authorization: OFFLINE_REVIEW_AND_TESTS_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: fixture-only; no exchange order IDs
immediate_stop_or_isolation: Do not make Gate/TestNet requests; account truth with a full unpaginated order page must remain degraded.
evidence_files: docs/audits/V2-G0-G1-pr27-independent-review-followup-20261013T050106Z.md; tests/test_gate_account_chain.py; tests/test_gate_testnet_ai_chain.py
reproduction: pytest -q tests/test_gate_account_chain.py -k 'full_page'; pytest -q tests/test_gate_testnet_ai_chain.py -k 'noncanonical_native_boolean'
root_cause_status: CONFIRMED
safe_fallback: Reject account truth for cleanup/entry gates unless ordinary and trigger-order snapshots are complete; reject protection verification when native flags are not canonical.
patch_status: Local fail-closed repair and regression tests; exact-head full suite and GitHub CI pending.
negative_tests: Full 100/101 order pages; numeric native booleans 2, -1, 1.5, NaN and infinity; unproven account truth rejected by the E2E completeness validator.
unblock_criteria: Targeted and full offline suites plus Web checks pass on exact pushed SHA; independent GitHub review is recorded; G0 human acceptance remains separate.
rollback: Do not restore truthy-number parsing or mark a full unpaginated order page complete. Keep the E2E gate degraded until equivalent completeness proof exists.
needs_human_decision: true
```
