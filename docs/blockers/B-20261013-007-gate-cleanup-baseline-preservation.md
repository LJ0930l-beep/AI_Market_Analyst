# B-20261013-007 Gate cleanup must preserve pre-entry baseline

```yaml
id: B-20261013-007
severity: S1
status: FIX_IN_REVIEW
component: gate_testnet_e2e
mode: RESEARCH_OFFLINE
branch_sha: 166b5841e0e29d9593b74e295fc551711e068f8e
trigger_signature: GATE_E2E_CLEANUP_BASELINE_MISMATCH_FALSE_COMPLETION
first_seen_at_utc: 2026-10-13T04:41:39Z
user_authorization: OFFLINE_TESTS_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: fixture-only; no exchange order IDs
immediate_stop_or_isolation: Do not run Gate/TestNet requests; keep cleanup mismatches at RECONCILIATION_REQUIRED.
evidence_files: docs/audits/V2-G0-G1-cleanup-baseline-followup-20261013T044139Z.md; tests/test_gate_testnet_ai_chain.py
reproduction: python -m pytest tests/test_gate_testnet_ai_chain.py::test_gate_testnet_e2e_requires_final_account_truth_after_protection_cleanup -q; before patch 5 passed, 2 failed due false COMPLETED.
root_cause_status: CONFIRMED
safe_fallback: Manually reconcile any changed baseline; never mark cleanup complete when final truth differs from pre-entry positions or pending orders.
patch_status: Local isolated repair on codex/v2-g1-offline-20261010; not yet committed at report time.
negative_tests: Baseline position reduction; disappearance of a pre-existing pending order; added exposure/order and incomplete/wrong-environment snapshots.
unblock_criteria: Targeted and full offline CI pass on exact new SHA; Web checks pass; independent review approves baseline-preservation logic.
rollback: Do not restore prior false-completion behavior; keep the E2E gate blocked on mismatch until equivalent fail-closed validation is present.
needs_human_decision: true
