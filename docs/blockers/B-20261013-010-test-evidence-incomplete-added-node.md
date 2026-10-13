# B-20261013-010 — 测试基线比较器接受不完整新增测试

```yaml
id: B-20261013-010
severity: S1
status: FIX_IN_REVIEW
component: ci
mode: RESEARCH_OFFLINE
branch_sha: 14e732546991c55d5a84c96631126dbadfc501bc
trigger_signature: V36_TEST_EVIDENCE_ADDED_NODE_PHASES_INCOMPLETE
playbook_codes: B21
first_seen_at_utc: 2026-10-13T08:01:05Z
user_authorization: DOCUMENTED_SCOPE_ONLY
authorization_scope: OFFLINE_AUDIT_SCOPE
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: V36 baseline comparison evidence only; no market sample or order
immediate_stop_or_isolation: Do not rely on PASS from added-node comparisons until every added test has complete passing phase evidence.
evidence_files_and_hashes: "Failure-first comparator source branch SHA 14e732546991c55d5a84c96631126dbadfc501bc; current candidate scripts/v36_test_evidence.py SHA-256 53EF91E342E02B049FA1F756AAFDFE43B716F4857585A80B6C80E2DA10AA1728; failure-first and regression test tests/v36/test_test_evidence.py SHA-256 2F66AC290D16BC215EFCFF149F851A0DD56A6B8B609696B67F4F2D50CCD57335; exact repair commit 40f65d7b05675ef2f7df69450af7d20c4418eb6e"
reproduction: python -m pytest -q tests/v36/test_test_evidence.py -k complete_phase_record
root_cause_status: CONFIRMED
original_evidence: "The failure-first comparator accepted a newly collected test node with setup-only evidence as PASS; the recorded pre-fix reproduction and baseline comparison artifacts identify the affected comparator."
root_cause: "The comparator treated a new test node as passing without requiring successful setup, call, and teardown phase evidence."
temporary_mitigation: "Treat any added node with absent or nonpassing phase evidence as BLOCKED/FAIL; do not accept or omit that node."
safe_fallback: Treat comparison as BLOCKED/FAIL if a new collected node lacks setup, call, or teardown evidence; do not accept or omit the node.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_method: "Require each added test node to provide successful setup, call, and teardown evidence and fail closed on missing or unsuccessful phases."
negative_tests: New nodes with setup only or setup+call must fail; a complete passing setup/call/teardown node may pass; teardown failures must fail.
unblock_criteria: Focused regression passes; the frozen same-environment comparison remains PASS with all added nodes having setup/call/teardown and passing outcomes; exact new PR #27 head passes hosted quick/full Python and Web checks; independent review remains required for G0.
needs_human_decision: true
candidate_branch: codex/v2-g1-offline-20261010
candidate_local_verification: Pre-fix reproduction returned PASS for a new node with setup only. Failure-first pytest produced 2 failed cases. After the local fix, tests/v36/test_test_evidence.py reports 16 passed; the frozen baseline/candidate comparison remains PASS with same_environment=true, complete_runs=true, 356 extra nodes, 0 incomplete/nonpassing added nodes, 0 missing/lost/new failure phases, and exit codes [1,1].
prior_run_artifacts: Existing immutable same-environment reports remain in the local Temp directory; source ledger and prior comparison artifacts were not changed. No remote effect occurred.
remote_ci_status: "Historical at discovery: run 38363964249 passed on source head 14e732546991c55d5a84c96631126dbadfc501bc before this repair. Superseded by repair head 40f65d7b05675ef2f7df69450af7d20c4418eb6e and successful exact-head run 38367957079. At audit snapshot 2026-10-13T09:24:06Z, documentation-only PR head 90344019d20a7db1464ddd7e82f3378bd3e791de was running 38374291874; Web checks passed and Python full suite was in progress."
next_owner_action: Obtain independent review of the comparator guard on PR #27 and record the human G0 decision after exact-head CI completes; keep G0 unaccepted and do not merge or dispatch external requests.
rollback: If the phase guard causes a regression, do not restore permissive acceptance. Make comparisons fail closed for any added node until the evidence schema can prove a complete passing execution; preserve prior reports, V35 controls, frozen V38 ledger, and all source PR refs.
```

## Evidence and repair

The previous comparator considered an added test “observed” when any phase or collection record existed. A local, offline input with a complete baseline plus a newly collected test containing only a successful setup phase returned `PASS` with the added test listed and no unreported-node warning. The same happened with setup and call but no teardown.

The pre-fix failure-first command, `python -m pytest -q tests/v36/test_test_evidence.py -k complete_phase_record`, returned **2 failed, 13 deselected**. The repair now requires every added node to have setup, call, and teardown phases, and rejects any added node with a non-passing phase. It does not alter prior evidence files or test outcomes.

Post-fix local evidence:

- `python -m pytest -q tests/v36/test_test_evidence.py`: **16 passed**.
- The frozen same-environment baseline/candidate comparison remains **PASS** under the stricter comparator: 356 added node IDs, no incomplete/nonpassing additions, no missing nodes/phases, no lost passes, no new failure phases, both runs complete in the same environment, exit codes `[1, 1]`.
- Ruff on the two changed Python files and `git diff --check`: passed.
- Exact-head hosted verification of this patch is pending; the green run on SHA `14e7325` predates this repair.

No Gemini, Antigravity, Gate, TestNet, or Live request was made; no order, account setting, frozen ledger, or historical report was changed.

## Latest hosted verification — 2026-10-13

The repair is committed as 40f65d7b05675ef2f7df69450af7d20c4418eb6e on PR #27. Exact-head run [38367957079](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38367957079) completed successfully: quick gate 289 passed; full suite 2,683 passed, one warning, 25 subtests passed; Web tests, lint, type-check, and build passed. The local commit hook also passed 2,682 tests with one skip and one warning. B-010 remains FIX_IN_REVIEW pending independent formal review and the human G0 decision; these test results do not accept G0 or authorize G2.

## Exact-head CI completion — 2026-10-13T09:47:51Z

Run `38374291874` completed SUCCESS on PR #27 head `90344019d20a7db1464ddd7e82f3378bd3e791de`: quick 289 passed; full suite 2,683 passed, 25 subtests passed, one existing deprecation warning; Web tests, lint, type-check, and build passed. The run includes the test-evidence comparator repair. It does not include the currently local blocker-register validator/tests; those require exact-head CI after push. Independent review/G0 acceptance remain pending.
