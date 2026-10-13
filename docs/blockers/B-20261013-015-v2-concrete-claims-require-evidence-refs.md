# B-20261013-015 — V2 具体子结论缺少引用仍被判有效

```yaml
id: B-20261013-015
severity: S1
status: FIX_IN_REVIEW
component: model
mode: RESEARCH_OFFLINE
branch_sha: ff17115c844c52f0480cbb831ed9608e68b7e646
trigger_signature: V2_CONCRETE_NESTED_CLAIM_WITHOUT_EVIDENCE_REFS
playbook_codes: B01,B10
first_seen_at_utc: 2026-10-13T14:01:45Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: Offline V2 contract fixtures only; no historical records
immediate_stop_or_isolation: Keep V2 remote dispatch disabled and maximum_calls at zero; do not count an unsupported concrete subclaim as valid analysis.
evidence_files_and_hashes: "Failure-first reproduction: tests/v38/test_response_contract_v2.py parameterized cases for CONDITIONAL, PRIOR_SWING, RANGE_EXTREME, and TREND_MEASURED_MOVE with empty nested refs; pre-fix evaluator returned VALID_LOCAL_CONTRACT for all four. V38 frozen call-ledger SHA-256 remains 820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7."
reproduction: python -m pytest -q tests/v38/test_response_contract_v2.py -k concrete_nested
root_cause_status: CONFIRMED
original_evidence: "The V2 FULL semantic validator allowed empty candidate_setup.evidence_refs and target_structure.evidence_refs for all enum kinds. This allowed CONDITIONAL and concrete target claims to pass with no claim-specific supporting reference, although unknown/unsupported cases also need to retain empty arrays."
root_cause: "Nested evidence validation checked reference membership and duplicates but applied allow_empty=True without conditioning on the semantic claim kind."
temporary_mitigation: "Do not use V2 results to update historical classifications or remote research; configs remain remote-disabled, unauthorized, and capped at zero calls."
safe_fallback: "Reject a CONDITIONAL candidate or concrete target kind without its own nonempty evidence_refs; continue allowing empty refs for NONE, UNASSESSED, and UNKNOWN."
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_method: "Add local kind-aware evidence requirements in evaluate_response_v2 and failure-first tests for all concrete candidate/target kinds; preserve empty refs for unsupported/unknown kinds."
negative_tests: "Empty refs rejected for CONDITIONAL, PRIOR_SWING, RANGE_EXTREME, and TREND_MEASURED_MOVE; known refs accepted for concrete claims; existing NONE/UNASSESSED/UNKNOWN empty-ref fixture remains valid."
unblock_criteria: "Focused V2 and V38 offline tests pass; exact-head hosted quick/full Python and Web CI pass; V38 frozen ledger hash is unchanged; independent current-candidate review and G0 disposition remain required."
needs_human_decision: true
next_owner_action: "Complete exact-head CI for the repaired candidate, include this finding in the current PR #27 independent review, and keep G2 unauthorized."
rollback: "If a legitimate evidence-free concrete claim is part of the intended contract, revise the versioned schema/semantics with an explicit rule and matching tests; do not silently restore unconditional empty-ref acceptance. Preserve historical ledgers and all production risk controls."
```

## Failure-first and repair evidence

- Before the code change, all four concrete-kind cases returned `VALID_LOCAL_CONTRACT`; the positive unknown/unsupported fixture remained valid.
- After the repair, `python -m pytest -q tests/v38/test_response_contract_v2.py` passed **58 tests**.
- The patch is offline-only. No model, exchange, TestNet, Live, or order request occurred; no V25/V38 historical result or ledger changed.
- Hosted CI on the previous exact head remains a separate result and does not verify this repair. The new pushed head must pass its own complete CI.
