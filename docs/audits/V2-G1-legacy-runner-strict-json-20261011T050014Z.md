# V2 G1 legacy runner strict JSON review — 2026-10-11 05:00Z

## Finding

An offline review found that the V38 research runner's initial response classifier and ledger reviewer both used Python's permissive `json.loads` directly. Python accepts duplicate object keys and silently keeps the final value. A completion such as `{"action": "OBSERVE", "action": "WAIT", ...}` could therefore be recorded as `VALID_ANALYSIS`, even though the raw completion is ambiguous JSON. The same raw text could pass ledger review after the event hashes were recomputed.

This finding concerns response classification and research evidence integrity. The runner has no account, risk, proposal, or order integration; this change does not modify those paths.

## Failure-first evidence

At PR #27 base candidate `92dec3992f0429f8883458ea21bf1fc7ddb6c994`, two new fake-provider tests failed as expected:

- Execution classified both duplicate-key A1/A2 completions as `VALID_ANALYSIS` instead of `INVALID_JSON`.
- Ledger review did not reject a rehashed `VALID_ANALYSIS` result whose raw response contained conflicting duplicate `action` keys.

The run reported `2 failed, 26 deselected`. It used only fixed local fixtures and a temporary ledger. No provider, exchange, or account API was called.

## Repair

Both initial response parsing and valid-result ledger review now use the versioned `RAW_JSON_ONLY_V1` parser from `json_response.py`. This applies the existing duplicate-key and non-JSON-constant rejection policy while retaining the legacy raw-only envelope behavior. The original completion text remains preserved; invalid completions are still counted as invalid rather than normalized into an accepted response.

The two regression tests cover execution classification and rehashed-ledger review. No frozen market input, historical call ledger, V35 risk code, trading route, account setting, or production order path was edited.

## Verification and gate state

- Failure-first regression run before repair: `2 failed, 26 deselected` (reproduced defect).
- After repair, the two new regressions: `2 passed, 26 deselected`.
- `tests/v38/test_gemini_runner.py tests/v38/test_response_contract_v2.py`: `76 passed`.
- Full repository `python -m pytest -q`: `2,592 passed, 1 skipped, 0 failed` in 353.48 seconds, with one existing Starlette/httpx deprecation warning.
- `ruff check` on the changed Python files: passed.
- `git diff --check`: passed.
- `ruff format --check` reports pre-existing whole-file formatting drift in both files; checking each file from the unchanged `92dec39` HEAD also returns the same nonzero result. The formatter was not run because it would reformat unrelated existing code.
- Exact-head hosted CI for the repair is required before this finding can be accepted.

The repair status is `PATCHED_LOCAL_SUITE_PASS_AWAITING_EXACT_HEAD_CI`. G0 remains `FIX_IN_REVIEW`; PR #26 and PR #27 remain Draft with no reviews. Remote response-format capability, remote model identity/weights, and billing remain unknown. This work does not authorize G2.

## Verification update — 2026-10-11 05:07Z

Full offline verification finished on the local candidate: `2,592 passed, 1 skipped, 0 failed`, with one existing Starlette/httpx deprecation warning. The run included both new strict-JSON regressions. The hosted run `38112389382` still describes the pre-repair PR #27 SHA above; it is not evidence for this patch. The new commit requires its own matching hosted quick/full result.

## Rollback

If a regression is found, revert only the two parser call-site changes and these two tests. Keep the strict V2 parser, immutable historical ledgers/reports, V35 risk controls, and the no-dispatch G2 boundary intact.
