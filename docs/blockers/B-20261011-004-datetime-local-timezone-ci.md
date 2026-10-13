# B-20261011-004 — datetime-local 日期过滤断言依赖 UTC+8

```yaml
id: B-20261011-004
severity: S1
status: VERIFIED
component: ci
mode: RESEARCH_OFFLINE
branch_sha: 230550b87abccb1530bc34e9997a8afee46732c4
trigger_signature: DATETIME_LOCAL_TEST_EXPECTATION_ASSUMES_UTC_PLUS_8
playbook_codes: B21
first_seen_at_utc: 2026-10-11T11:10:30Z
user_authorization: DOCUMENTED_SCOPE_ONLY
external_cost_or_order_attempted: false
remote_effect_known: true
risk_to_funds: NONE
affected_sample_or_order_ids: PR-27 web CI only; no samples or orders
immediate_stop_or_isolation: Keep the failure classified as a frontend test-contract issue; no runtime or trading action is involved.
evidence_files_and_hashes: docs/audits/V2-G0-G1-web-test-loop-fix-20261011T110319Z.md; exact failing run 38134595746 on 090df0facb75f2b6ea1b3ddabeb70b189505ae9a; passing exact-head run 38136662102 on 230550b87abccb1530bc34e9997a8afee46732c4.
reproduction: Run the date-filter test with TZ=UTC; before the repair the request correctly contained 2030-01-02T11:00:00.000Z while the assertion expected 03:00Z.
root_cause_status: CONFIRMED
original_evidence: "Hosted run 38134595746 failed the date-filter assertion on head 090df0facb75f2b6ea1b3ddabeb70b189505ae9a; the TZ=UTC reproduction showed the application emitted the correct 2030-01-02T11:00:00.000Z value."
root_cause: "The test asserted one fixed UTC conversion for a datetime-local input whose value is defined in the browser's local timezone."
temporary_mitigation: "Keep the corrected timezone-neutral assertion and preserve the original failing run as historical evidence; do not change runtime date conversion to satisfy the old expectation."
safe_fallback: Derive the expected query value from the browser-local input using the same JavaScript Date-to-ISO conversion contract.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_method: "Derive the expected request timestamp from the same local datetime value and Date-to-ISO behavior as the application, then run the UTC-targeted case and full Web checks."
negative_tests: Failure reproduced under TZ=UTC before the patch; targeted UTC test, complete 161-test web suite, lint, and build pass after the patch.
unblock_criteria: Exact PR #27 head checks pass for web tests, lint, build, and Python; preserve the original failure record and avoid changing product behavior to satisfy a timezone-specific expectation.
needs_human_decision: false
next_owner_action: "Keep the timezone-neutral assertion and preserve the original failing run plus exact-head repair evidence; no further runtime change is required unless datetime semantics change."
rollback: Revert only the timezone-dependent test expectation if a future product contract intentionally changes; leave runtime date handling and all trading code untouched.
```

## Cause and evidence

The web test asserted a fixed UTC timestamp for an HTML datetime-local input. Its value represents local wall time, so expected UTC depends on the runtime timezone. The initial hosted run 38134595746 on head 090df0facb75f2b6ea1b3ddabeb70b189505ae9a failed 1 of 161 web tests, while its Python job passed. A local TZ=UTC reproduction showed the application request value was correct (2030-01-02T11:00:00.000Z); only the test's hard-coded 03:00Z expectation was wrong.

## Repair and verification

Commit 230550b87abccb1530bc34e9997a8afee46732c4 changes the assertion to derive its expected encoded timestamp from new Date(input).toISOString(). This is a test-only correction. The targeted UTC test passes; the full Web suite passed 29 files / 161 tests; lint and TypeScript/Vite build passed. Exact-head hosted run 38136662102 passed the web job and Python jobs. The existing Vite large-chunk advisory and one Starlette/httpx deprecation warning remain unrelated.

## Containment, rollback, and external effects

No product runtime, production trading path, credentials, account settings, risk limits, sample, ledger, provider, exchange API, or order was changed or used. The original failure stays recorded in the prior audit. If the product intentionally changes datetime semantics later, review the contract and revert/replace only the assertion and its focused test; do not change trading behavior to satisfy a host-specific timezone assumption.
