# B-20261013-010 — 测试基线比较器接受不完整新增测试

```yaml
id: B-20261013-010
severity: S1
status: FIX_IN_REVIEW
component: test_evidence_comparison
mode: RESEARCH_OFFLINE
source_head_sha: 14e732546991c55d5a84c96631126dbadfc501bc
trigger_signature: V36_TEST_EVIDENCE_ADDED_NODE_PHASES_INCOMPLETE
first_seen_at_utc: 2026-10-13T08:01:05Z
user_authorization: DOCUMENTED_OFFLINE_AUDIT_SCOPE
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: V36 baseline comparison evidence only; no market sample or order
immediate_stop_or_isolation: Do not rely on PASS from added-node comparisons until every added test has complete passing phase evidence.
evidence_files_and_hashes: scripts/v36_test_evidence.py at source HEAD 14e732546991c55d5a84c96631126dbadfc501bc; failure-first test tests/v36/test_test_evidence.py::test_compare_rejects_added_node_without_complete_phase_record
reproduction: python -m pytest -q tests/v36/test_test_evidence.py -k complete_phase_record
root_cause_status: CONFIRMED
safe_fallback: Treat comparison as BLOCKED/FAIL if a new collected node lacks setup, call, or teardown evidence; do not accept or omit the node.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
negative_tests: New nodes with setup only or setup+call must fail; a complete passing setup/call/teardown node may pass; teardown failures must fail.
unblock_criteria: Focused regression passes; the frozen same-environment comparison remains PASS with all added nodes having setup/call/teardown and passing outcomes; exact new PR #27 head passes hosted quick/full Python and Web checks; independent review remains required for G0.
needs_human_decision: true
candidate_branch: codex/v2-g1-offline-20261010
candidate_local_verification: Pre-fix reproduction returned PASS for a new node with setup only. Failure-first pytest produced 2 failed cases. After the local fix, tests/v36/test_test_evidence.py reports 16 passed; the frozen baseline/candidate comparison remains PASS with same_environment=true, complete_runs=true, 356 extra nodes, 0 incomplete/nonpassing added nodes, 0 missing/lost/new failure phases, and exit codes [1,1].
prior_run_artifacts: Existing immutable same-environment reports remain in the local Temp directory; source ledger and prior comparison artifacts were not changed. No remote effect occurred.
remote_ci_status: Exact source head 14e732546991c55d5a84c96631126dbadfc501bc passed run 38363964249, but does not include this local patch. A new exact-head run is required after push.
next_owner_action: Push only the reviewed comparator guard, its failure-first tests, and this blocker record to existing PR #27; require exact-head CI. Keep G0 unaccepted until a reviewer with repository permission publishes a review and a human records the G0 decision. No merge or external dispatch.
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
