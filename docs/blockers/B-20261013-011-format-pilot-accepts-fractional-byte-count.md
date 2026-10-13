# B-20261013-011 — V38 format pilot accepts fractional transport byte counts

```yaml
id: B-20261013-011
severity: S1
status: FIX_IN_REVIEW
component: model
mode: RESEARCH_OFFLINE
branch_sha: 13cc31e2cb7713391fd93f779e906dea807b0324
trigger_signature: V38_FORMAT_PILOT_ACCEPTS_FRACTIONAL_TRANSPORT_BYTES
playbook_codes: B03
first_seen_at_utc: 2026-10-13T11:35:31Z
user_authorization: DOCUMENTED_SCOPE_ONLY
authorization_scope: OFFLINE_TEST_FIXTURES_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: NONE; fixed local format-pilot fixtures only
immediate_stop_or_isolation: Keep all model dispatch disabled; do not use a format-pilot result as transport/capability evidence unless request_bytes_written is a verified positive integer.
evidence_files_and_hashes: "Pre-fix source candidate: PR #27 SHA 13cc31e2cb7713391fd93f779e906dea807b0324; failure-first regression file tests/v38/test_format_pilot.py SHA-256 C49D8C39812CE7690A4E7B97FD2A7591037DE796F05EE451964F118B1CF76AC7; repaired source core/replay/pa_decision_quality_v38/format_pilot.py SHA-256 6C0216D8619549A3058D1F3AB5A92FB3AE74D14F4B0E726BD8231EC93AE7E9DD. The frozen V38 intent ledger remains SHA-256 820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7."
reproduction: "On pre-fix candidate 13cc31e2cb7713391fd93f779e906dea807b0324, run python -m pytest tests/v38/test_format_pilot.py -k 'noninteger_transport_byte_counts or rehashed_noninteger_transport_byte_count' -q. Result: 3 failed, 1 passed, 11 deselected; 1.0, 1.5, and a rehashed historical pilot result with 1.5 were accepted. The True case was already rejected."
root_cause_status: CONFIRMED
original_evidence: "Failure-first tests showed both format-pilot result creation and replay review accepted positive float request_bytes_written values. The same candidates already reject bool and nonpositive values; the defect was float acceptance."
root_cause: "format_pilot._result_event and _validate_result_event used type(value) in (int, float) plus a positive check instead of the shared exact-positive-integer transport predicate used by gemini_runner."
temporary_mitigation: "Do not dispatch or reuse old pilot intents. Treat any format-pilot result with non-integer request_bytes_written as unverified; keep provider capability unknown."
safe_fallback: "Reject the result as FORMAT_PILOT_INVALID_TRANSPORT and reject an existing valid-labeled ledger event as FORMAT_PILOT_VALID_RESULT_PROVENANCE_INVALID."
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_method: "Reuse gemini_runner._valid_request_byte_count in both format-pilot result creation and result-ledger review; this requires an exact positive integer with the shared upper bound."
negative_tests: "Failure-first cases cover 1.0 and 1.5 in result creation and a rehashed valid-labeled result with 1.5 during ledger review; True is confirmed rejected. Post-fix full tests/v38/test_format_pilot.py: 15 passed."
unblock_criteria: "Commit/push this fix on PR #27; require current exact-head hosted quick/full and Web tests/lint/type-check/build to succeed; preserve the immutable V38 ledger hash and confirm this change adds no provider call, retry, quality-sample eligibility, or order. Keep FIX_IN_REVIEW pending formal current-head review/G0 disposition."
needs_human_decision: true
next_owner_action: "Review the local shared-predicate repair, wait for exact-head hosted CI after push, and include this finding in the current PR #27 independent review. Keep G2 unauthorized."
rollback: "Never restore float acceptance. If reuse of the shared helper causes regressions, replace it with an equivalent exact positive-integer predicate and keep malformed transport evidence rejected; preserve prior pilot records and the frozen V38 ledger."
```

## Post-fix evidence

- `python -m pytest tests/v38/test_format_pilot.py -q`: **15 passed**.
- `ruff check core/replay/pa_decision_quality_v38/format_pilot.py tests/v38/test_format_pilot.py`: passed.
- `git diff --check`: passed.
- The V38 ledger was read-only rechecked at the SHA recorded in the YAML above; no provider, Gate, TestNet, Live, or order request was made.

## Exact-head hosted verification — 2026-10-13T13:38:28Z

- PR #27 exact head `132ad6b8a11b66fa17ee86f21fa4a3923c07b945` passed run [38403593249](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38403593249): quick gate 306 passed; full Windows/Python 3.12 suite 2,720 passed, 0 failed, 25 subtests passed; Web tests, lint, type-check, and build passed.
- The exact-head suite includes the format-pilot repair. This does not promote the consumed pilot to provider-capability evidence or authorize G2. Keep `FIX_IN_REVIEW` pending independent review/G0.
