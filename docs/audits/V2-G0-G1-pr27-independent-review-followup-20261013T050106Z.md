# V2 G0/G1 PR #27 independent review follow-up

## Reviewed report and result

Reviewed the user-provided `AI_Market_Analyst_PR27_G0_独立复审_20261013.md`. Its reviewed HEAD is `f856859cb83517729b28a56e3ed310f920ee643f`, base `main`. Its `REQUEST_CHANGES / G0 NOT_ACCEPTED / FIX_IN_REVIEW` conclusion is supported by both reported findings.

The GitHub PR was independently checked at that HEAD: open, Draft, base `main`, zero submitted reviews, and no review requests. The attached report is not itself a GitHub review. At 2026-10-13 05:01:06Z, Actions run `38348337484` for that exact SHA remained in progress: Web tests/lint/typecheck/build had succeeded; the offline Python quick/full job was still running.

## P1-01: native protection flags accepted non-boolean numbers

**Finding confirmed.** The pre-repair `_optional_bool()` used `bool(value)` for every finite numeric input, so `2`, `-1`, and `1.5` became `True` in `fetch_protection_order()` and the trigger-order snapshot. This was not an inference from the review prose: fake Gate payloads reproduced it. NaN, infinity, and text `"2"` already failed closed before this repair.

**Repair.** Added a dedicated parser for Gate native protection flags. It accepts actual booleans, numeric 0/1, and canonical text `true`/`false`/`0`/`1`; other values are unverified. The generic `_optional_bool()` semantics and unrelated callers remain unchanged. Both protection readback and trigger-order classification use the strict parser; contradictory flags continue to fail closed.

**Evidence.** Before repair, 12 of the new invalid-number cases failed: the 2/-1/1.5 values across native flag aliases and trigger classification were incorrectly accepted. After repair, the selected protection compatibility and malformed-value regressions passed; partial reduce-only behavior remains covered.

## P1-02: full order pages reported as complete

**Finding confirmed.** Both ordinary and native trigger-order reads request at most 100 items and expose no verified pagination-completion cursor. Before repair, fake 100- and 101-item responses returned `AVAILABLE`, allowing `get_account_truth()` to represent an unproven snapshot as complete.

**Repair.** A response whose length reaches the requested page limit now has `DEGRADED` status and a specific truncation error code. This deliberately treats an exactly full page as incomplete because the adapter cannot distinguish exactly 100 open orders from a longer list. One- or two-order snapshots remain `AVAILABLE`. The account truth completeness validator rejects the resulting `DEGRADED` snapshot, so Gate E2E cannot report cleanup complete from it. No pagination behavior was invented.

**Evidence.** Before repair, all four 100/101 full-page cases (ordinary and trigger orders) incorrectly returned `AVAILABLE`. Added offline tests for both page types and counts, normal 1/2-item pages, and account-truth/E2E completeness rejection.

## Local verification after repair

- `pytest -q tests/test_gate_account_chain.py tests/test_gate_testnet_ai_chain.py`: **100 passed**, one existing Starlette/httpx deprecation warning.
- `ruff check --select F821` on the three changed Python files: passed.
- `python -m compileall -q` on the changed Python files: passed.
- `git diff --check`: passed.
- Full repository pytest and GitHub CI for the repaired SHA remain required; Actions run `38348337484` only covers the prior `f856859c...` SHA and had not completed at the report timestamp.

## Scope and safety

- Only `core/trading/gate_live_client.py` and focused test files change runtime code behavior; the behavior change is fail-closed when the evidence is malformed or incomplete.
- No credentials, account settings, trading parameters, historical research ledgers, or prior reports were modified.
- No Gate/TestNet/Live API request, model call, order, merge, or Live authorization occurred.
- The fix is local and uncommitted at this report timestamp. The exact final SHA must receive full offline and Web CI before it can be treated as verified.

## Remaining acceptance conditions

1. Run the relevant Gate account and TestNet AI-chain suites and the repository offline suite against the repaired tree.
2. Commit/push the fix to PR #27 and obtain all required CI checks on that exact new SHA.
3. A reviewer with GitHub permission must publish an independent formal review; the attached document and this follow-up do not satisfy that GitHub state.
4. G0 remains `NOT_ACCEPTED`; G2 remains `NOT_AUTHORIZED`. Do not merge or dispatch model/exchange requests before the required independent review and human decision.
