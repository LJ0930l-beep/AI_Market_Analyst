# B-20261013-007 Gate cleanup must preserve pre-entry baseline

```yaml
id: B-20261013-007
severity: S1
status: FIX_IN_REVIEW
component: gate
mode: RESEARCH_OFFLINE
branch_sha: 166b5841e0e29d9593b74e295fc551711e068f8e
trigger_signature: GATE_E2E_CLEANUP_BASELINE_MISMATCH_FALSE_COMPLETION
playbook_codes: B18
first_seen_at_utc: 2026-10-13T04:41:39Z
user_authorization: DOCUMENTED_SCOPE_ONLY
authorization_scope: OFFLINE_TESTS_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: fixture-only; no exchange order IDs
immediate_stop_or_isolation: Do not run Gate/TestNet requests; keep cleanup mismatches at RECONCILIATION_REQUIRED.
evidence_files_and_hashes: "Original finding and failure-first evidence: docs/audits/V2-G0-G1-cleanup-baseline-followup-20261013T044139Z.md SHA-256 735F7BA7F2E5D669CFF71D9726FB18FADF82831700169A11F1CFDC7148897B70; regression tests: tests/test_gate_testnet_ai_chain.py SHA-256 9602C772CD00032B249B94198BE6D01C7BB3CF70B53E9D5DBB7FB9C4580E80EE; current candidate implementation: core/trading/gate_testnet_e2e.py SHA-256 66EEAD448AC8583D92DC8CC440757A3FD9174B3FFF0FE7D66EC370CA1FA4D916"
reproduction: python -m pytest tests/test_gate_testnet_ai_chain.py::test_gate_testnet_e2e_requires_final_account_truth_after_protection_cleanup -q; before patch 5 passed, 2 failed due false COMPLETED.
root_cause_status: CONFIRMED
original_evidence: "Failure-first test test_gate_testnet_e2e_requires_final_account_truth_after_protection_cleanup produced false COMPLETED when final account truth differed from the pre-entry position/order baseline; the original failure report and fixture tests are hash-recorded above."
root_cause: "The E2E cleanup path could accept an end state without proving that all pre-existing positions and pending orders were preserved and the new exposure was fully removed."
temporary_mitigation: "Do not run Gate/TestNet requests; keep any cleanup mismatch or incomplete final account truth at RECONCILIATION_REQUIRED."
safe_fallback: Manually reconcile any changed baseline; never mark cleanup complete when final truth differs from pre-entry positions or pending orders.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_plan: After native protection cleanup, fetch a fresh complete TestNet account snapshot and require positions and pending orders to match the pre-entry baseline; any missing, stale, or changed truth remains RECONCILIATION_REQUIRED. Implemented in repair commit f856859cb83517729b28a56e3ed310f920ee643f.
repair_method: "After cleanup, fetch a fresh complete account snapshot and require the original positions and pending orders to match; reject missing, stale, changed, or wrong-environment truth."
negative_tests: Baseline position reduction; disappearance of a pre-existing pending order; added exposure/order and incomplete/wrong-environment snapshots.
unblock_criteria: Targeted and full offline CI pass on exact new SHA; Web checks pass; independent review approves baseline-preservation logic.
rollback: Do not restore prior false-completion behavior; keep the E2E gate blocked on mismatch until equivalent fail-closed validation is present.
needs_human_decision: true
next_owner_action: Obtain independent review of the baseline-preservation path on PR #27 and record the human G0 decision; keep TestNet requests disabled meanwhile.
```

## Latest verification — 2026-10-13T05:38:56Z

Repair commit `dfcdd5ff5b7a05c6a6305c3566753884bc282f35` is current PR #27 HEAD; exact-head GitHub run `38350073282` passed Python and Web checks. Independent formal review/G0 decision is still pending. No TestNet request was made.

## Attribution correction and current candidate evidence — 2026-10-13T09:24:06Z

The baseline-preservation code change is commit `f856859cb83517729b28a56e3ed310f920ee643f`. Commit `dfcdd5ff5b7a05c6a6305c3566753884bc282f35` was the later candidate containing that repair; the preceding sentence incorrectly called the candidate commit the repair commit. The later exact candidate run [38367957079](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38367957079) passed on source head `40f65d7b05675ef2f7df69450af7d20c4418eb6e`. The current PR head at this snapshot is documentation-only `90344019d20a7db1464ddd7e82f3378bd3e791de`; run [38374291874](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38374291874) has Web checks passed and Python full suite still in progress. G0 remains unaccepted pending independent review and the human decision.

## Exact-head CI completion — 2026-10-13T09:47:51Z

Run `38374291874` completed SUCCESS on PR #27 head `90344019d20a7db1464ddd7e82f3378bd3e791de`: quick 289 passed; full suite 2,683 passed, 25 subtests passed, one existing deprecation warning; Web tests, lint, type-check, and build passed. PR #27 remains Draft with no formal review. No TestNet request was made.
