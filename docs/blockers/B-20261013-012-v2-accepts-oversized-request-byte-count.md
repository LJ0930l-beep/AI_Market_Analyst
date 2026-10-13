# B-20261013-012 — V2 response contract accepts oversized transport byte counts

```yaml
id: B-20261013-012
severity: S1
status: FIX_IN_REVIEW
component: model
mode: RESEARCH_OFFLINE
branch_sha: c1ee66568f66923ae27022554399db96379f47b5
trigger_signature: V2_RESPONSE_CONTRACT_ACCEPTS_OVERSIZED_TRANSPORT_BYTES
playbook_codes: B03
first_seen_at_utc: 2026-10-13T11:51:00Z
user_authorization: DOCUMENTED_SCOPE_ONLY
authorization_scope: OFFLINE_CONTRACT_FIXTURES_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: NONE; fixed offline response-contract fixtures only
immediate_stop_or_isolation: Keep remote dispatch disabled; do not treat any response receipt above the shared 1,000,000,000,000-byte bound as verified transport evidence.
evidence_files_and_hashes: "Repaired core/ai/transport_diagnostics.py SHA-256 D6C20C61F1C8123AC6209E2CD088871CC5A90030BA1D2C917E1C905BF35CAFC8; core/replay/pa_decision_quality_v38/gemini_runner.py SHA-256 F6A7FF2A35D055D8908E4204C01C1F1FC7E76E07F323765A443C33DBF163EB8F; core/replay/pa_decision_quality_v38/response_contract_v2.py SHA-256 EEAE0ADE03EC041B166A69F5CF6696040509A7AB184A95AB40BB6C8D9BF99D51; tests/test_gemini_transport_diagnostics.py SHA-256 E2935ACCE9C6EE61A367FABF6EA4E8F268C076A44D680692D9103754E7108EEA; tests/v38/test_gemini_runner.py SHA-256 464423A4CC26BD18CAB9EA69111FBE1209DF643204BDB4AD8DF3935EBE7ABF99; tests/v38/test_format_pilot.py SHA-256 FC31C21939B5AEB32D139C9A5637894F32ACA5BA87940052470CC2EFDD867A9D; tests/v38/test_response_contract_v2.py SHA-256 21C19100E86125A89FDBBC8764445FE3197970B5C17F4C3F42B13F6A3865C61E. Baseline candidate c1ee66568f66923ae27022554399db96379f47b5."
reproduction: "On baseline c1ee665, evaluate_response_v2 accepted request_bytes_written=1000000000001 as VALID_LOCAL_CONTRACT / MATCHED_COMPLETED_JSON_HTTP_200 and emitted normalized_analysis. Failure-first test tests/v38/test_response_contract_v2.py::test_v2_rejects_invalid_transport_byte_counts[1000000000001] failed while 9 sibling cases passed (42 deselected)."
root_cause_status: CONFIRMED
original_evidence: "The V2 evaluator checked only exact-int and >0, while the V38 runner enforced the shared upper bound of 1,000,000,000,000. safe_transport_trace also sanitized request_bytes_written through the general numeric-metadata path, which could preserve fractional values."
root_cause: "Transport byte-count acceptance rules were duplicated: the V2 contract evaluator omitted the V38 upper bound and the shared sanitizer treated byte counts like floating-point timing fields."
temporary_mitigation: "Both V2 prompt configs remain remote-disabled, unauthorized, and capped at zero calls; no V2 response is eligible for model research or execution."
safe_fallback: "Return INVALID_TRANSPORT, leave transport receipt unverified, and emit no normalized analysis whenever the byte count is not an exact positive integer within the shared bound."
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_method: "Add one bounded exact-positive-integer predicate in core.ai.transport_diagnostics; use it in the V38 runner and V2 contract evaluator, and make safe_transport_trace retain request byte metadata only for exact bounded integers (including zero for failure diagnostics)."
negative_tests: "Failure-first oversized-int case; below/at/above-bound and malformed bool/float/negative cases; sanitizer drops bool, float, negative, and oversized byte counts; exact max bound remains valid; runner and format-pilot execution plus rehashed-ledger review reject oversized receipts. Focused response-contract, transport-diagnostics, Gemini-runner, and format-pilot suites: 128 passed."
unblock_criteria: "Commit the repair on PR #27; require exact-head hosted quick/full Python and Web CI success; rerun blocker-register validation and preserve the V38 frozen ledger hash. Keep FIX_IN_REVIEW until a current-candidate independent review and G0 decision."
needs_human_decision: true
next_owner_action: "Finish exact-head CI after publishing this bounded shared validator; refresh the G0/G1 evidence and include B-012 in current-candidate review. Keep G2 unauthorized and do not call Gemini/Gate/TestNet or place orders."
rollback: "If shared validation causes regressions, replace it with one equivalent bounded exact-integer validator used by all consumers; never restore unbounded or fractional acceptance. Preserve historical ledgers, risk controls, and prior reports."
```

## Local verification

- The failure-first fixture reproduces the oversized-byte acceptance on the pre-fix PR #27 candidate.
- `python -m pytest tests/v38/test_response_contract_v2.py tests/test_gemini_transport_diagnostics.py tests/v38/test_gemini_runner.py tests/v38/test_format_pilot.py -q`: **128 passed**.
- `python -m pytest -q`: **2,718 passed, 1 skipped, 1 existing Starlette/httpx deprecation warning** in 367 seconds.
- `git diff --check`: passed. Baseline lint findings in `tests/test_gemini_transport_diagnostics.py` and import ordering in `core/ai/transport_diagnostics.py` predate this change; no network, model, exchange, or order path was used.

## Exact-head hosted verification — 2026-10-13T13:38:28Z

- PR #27 exact head `132ad6b8a11b66fa17ee86f21fa4a3923c07b945` passed run [38403593249](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38403593249): quick gate 306 passed; full Windows/Python 3.12 suite 2,720 passed, 0 failed, 25 subtests passed; Web tests, lint, type-check, and build passed.
- The fix is verified offline only. Provider calls remain disabled; preserve the frozen V38 ledger and keep `FIX_IN_REVIEW` pending independent review/G0.
