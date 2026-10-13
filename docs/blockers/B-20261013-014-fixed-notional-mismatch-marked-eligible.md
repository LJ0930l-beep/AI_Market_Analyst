# B-20261013-014 — Fixed-notional proposal mismatch marked eligible

```yaml
id: B-20261013-014
severity: S1
status: FIX_IN_REVIEW
component: risk
mode: RESEARCH_OFFLINE
branch_sha: e792c2d0a084224f777b65d1f051af3fc612359e
trigger_signature: FIXED_NOTIONAL_PROPOSAL_MISMATCH_MARKED_ELIGIBLE
playbook_codes: B14
first_seen_at_utc: 2026-10-13T12:25:25Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: NONE; offline feasibility API call only
immediate_stop_or_isolation: Do not rely on a FIXED_NOTIONAL diagnostic when fixed_target_differs_from_model_proposal is true; keep all production settings and execution paths unchanged.
evidence_files_and_hashes: "Pre-fix source blob SHA-256 57695B9876CE4356B315E6FFBAE1048D69F50D6EAECE8C481C033FC3634CBB01 at PR #27 SHA e792c2d0a084224f777b65d1f051af3fc612359e; pre-fix regression test is present at the same commit. Repaired source SHA-256 6376C74416CB4483D9D1EFE529CCD6BCA1A67156A5CA5BFCB75C8168B0F478EA; regression test SHA-256 FCFD64B27AFC7AB52EC7A733127339490A80870C5D2F95C912DD2D80A2FD6F05. The V25 sample has only 2000.0 proposals; core/replay/pa_decision_quality_v36/runner.py already rejects mismatches before invoking this shared diagnostic."
reproduction: "Before repair, run python -c \"import runpy; p=runpy.run_path('tests/test_v35_trade_feasibility.py')['proposal']; r=p(proposed_notional_usdt='1000'); print(r['status'], r['feasible'], r['reason_codes'], r['fixed_target_differs_from_model_proposal'], r['evaluated_notional_target_usdt'])\"; output: ELIGIBLE_PROPOSAL True [] True 2000. A failure-first pytest assertion expected REJECTED but observed ELIGIBLE_PROPOSAL."
root_cause_status: CONFIRMED
original_evidence: "diagnose_trade_proposal selected fixed_notional_usdt for the calculation but did not convert fixed_target_differs_from_model_proposal into a rejection. The V36 wrapper independently rejects this mismatch, so the defect was isolated to direct/shared diagnostic callers; no production order path was reached."
root_cause: "The fixed-size mismatch guard existed in the V36 wrapper but was absent from the reusable trade feasibility function, allowing an unchanged model proposal to be labeled eligible against a substituted 2000 USDT target."
temporary_mitigation: "V36 callers already fail closed on this mismatch, and every proposal in the immutable V25 ten-trade sample is exactly 2000.0 USDT. Keep production fixed-notional parameters unchanged and do not use mismatched direct diagnostics as acceptance evidence."
safe_fallback: "Return REJECTED with FIXED_NOTIONAL_PROPOSAL_MISMATCH before computing economics whenever the model-proposed amount differs from the fixed target."
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_method: "Add the fixed-target equality guard directly to diagnose_trade_proposal and a failure-first regression asserting REJECTED, preserved proposed/fixed amounts, no evaluated substitute target, and no proposal resizing."
negative_tests: "Failure-first reproduction failed before repair. After repair, tests/test_v35_trade_feasibility.py plus tests/v36/test_runner.py pass (62 tests); the V36 mismatch wrapper test still passes."
unblock_criteria: "Run the related full risk/replay regression and the complete offline suite on the final PR #27 SHA; require exact-head hosted Python and Web CI success, blocker-register validation, immutable V25/report checks, and current-candidate review/G0 disposition."
needs_human_decision: true
next_owner_action: "Finish the exact-head offline and hosted checks for the current combined patch; include B-014 in the PR #27 independent review. Do not use this repair to change production sizing or to authorize an order."
rollback: "If the guard conflicts with a documented fixed-notional contract, revert only this helper guard after updating the contract and adding an equivalent fail-closed caller check; never report a proposal against a different size as eligible, and preserve V25 samples, production parameters, and risk thresholds."
```

## Repair verification

- `python -m pytest tests/test_v35_trade_feasibility.py tests/v36/test_runner.py -q`: **62 passed**.
- The repository full-suite hosted check on the earlier SHA remains separate and does not include this repair.
- No model, exchange, TestNet, Live, order, account-configuration, or historical-sample access occurred.
