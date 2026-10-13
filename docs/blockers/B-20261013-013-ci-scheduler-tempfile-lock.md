# B-20261013-013 — Hosted Windows suite intermittently locks scheduler SQLite tempfile

```yaml
id: B-20261013-013
severity: S2
status: FIX_IN_REVIEW
component: ci
mode: RESEARCH_OFFLINE
branch_sha: c1ee66568f66923ae27022554399db96379f47b5
trigger_signature: WINDOWS_SCHEDULER_TEST_TEMPFILE_PERMISSIONERROR
playbook_codes: B21
first_seen_at_utc: 2026-10-13T12:05:30Z
user_authorization: DOCUMENTED_SCOPE_ONLY
authorization_scope: OFFLINE_CI_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: NONE
immediate_stop_or_isolation: Keep PR #27 unmerged until a fresh exact-head full CI succeeds; do not skip, xfail, or weaken the scheduler lifecycle test.
evidence_files_and_hashes: "GitHub full-suite run https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38392197155 failed only at tests/test_phase5_scheduler.py::SchedulerCoreTests::test_explicit_start_is_singleton_and_stop_leaves_no_thread while TemporaryDirectory removed lifecycle.sqlite3 (WinError 32); 2707 passed, 1 failed, 25 subtests passed. The test and core/scheduler.py have no diff from origin/main at the captured candidate. Current local full suite: 2718 passed, 1 skipped, 1 existing warning; the isolated failing test passed 10/10 repetitions."
reproduction: "Hosted: run 38392197155, full suite step, test body assertions completed before temporary-directory cleanup raised PermissionError for lifecycle.sqlite3. Local: python -m pytest tests/test_phase5_scheduler.py::SchedulerCoreTests::test_explicit_start_is_singleton_and_stop_leaves_no_thread -q passed 10 consecutive subprocess runs."
root_cause_status: HYPOTHESIS
original_evidence: "The hosted traceback points to Windows tempfile cleanup unlinking lifecycle.sqlite3 after the scheduler test body, with WinError 32. Current candidate changes neither that test nor core/scheduler.py; the same test passes repeatedly in the local Windows environment."
root_cause: "Likely an intermittent Windows file-handle lock at TemporaryDirectory cleanup; the available traceback does not identify which process/thread held the file, so the precise handle owner is unverified."
temporary_mitigation: "Preserve the failure evidence and rerun the complete suite on the next exact PR #27 head. Treat the current hosted check as failed until a new exact-head run is green."
safe_fallback: "Keep the PR unmerged and preserve the existing scheduler and test behavior; do not classify this hosted run as passed based on local retries."
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_method: "No scheduler or test patch is justified yet. If the cleanup failure repeats, capture the exact process/handle lifecycle and repair deterministic resource teardown without weakening assertions or changing trading behavior."
negative_tests: "Current local full suite passes; targeted scheduler lifecycle test passes 10/10; exact hosted run failed once as recorded. Rerun exact-head hosted quick/full suites after the current repair commit."
unblock_criteria: "A fresh exact-head hosted workflow completes successfully, including the full offline suite; preserve this run as a historical failure and do not rewrite its result. If the same tempfile lock recurs, confirm and repair its owner before acceptance."
needs_human_decision: false
next_owner_action: "Push the current bounded transport-receipt repair and blocker records; wait for its exact-head hosted CI. Keep G0 unaccepted until CI and independent review criteria pass."
rollback: "There is no source behavior change for this observation. If a later test-only cleanup patch is attempted, revert it if it hides the live-thread/file-lock condition or changes scheduler semantics; never skip the test."
```

## Disposition

- The source and test node are unchanged relative to `origin/main`; this run failure is not evidence that the current transport fix caused it.
- Ten isolated local repetitions and the local full suite passed, but they do not erase the hosted failure.
- No provider, Gate, TestNet, Live, or order request occurred. G0 remains gated on a fresh exact-head CI result and independent review.
