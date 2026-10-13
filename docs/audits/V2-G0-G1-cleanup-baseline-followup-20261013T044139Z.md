# V2 G0/G1 Gate cleanup baseline follow-up (2026-10-13T04:41:39Z)

## Finding and severity

New S1 finding B-20261013-007: the prior Gate TestNet E2E cleanup check rejected added exposure and added open orders, but accepted a final account snapshot that had lost part of a pre-existing position or a pre-existing pending order. That could falsely mark cleanup complete after changing baseline account state.

The issue was reproduced entirely with local fixtures. No Gate API, model API, order, or account setting was touched. Impacted exchange order IDs: none. Funds risk from this audit: none.

## Failure-first evidence

Before the implementation change, these new fixture cases failed as expected:

- Baseline BTCUSDT LONG exposure 0.5, final exposure 0.4: result incorrectly reported COMPLETED.
- Baseline pending order baseline-order-1, final pending-order list empty: result incorrectly reported COMPLETED.

Command: python -m pytest tests/test_gate_testnet_ai_chain.py::test_gate_testnet_e2e_requires_final_account_truth_after_protection_cleanup -q
Before patch: 5 passed, 2 failed. The two failures prove the false-completion path.

## Repair and fallback

The cleanup finalization now requires exact equality between pre-entry and final position exposure maps, and between pre-entry and final pending-order ID sets. Added exposure remains classified as CLEANUP_NOT_RECONCILED; a missing or otherwise changed baseline is CLEANUP_BASELINE_CHANGED; newly added pending orders retain CLEANUP_OPEN_ORDERS_REMAIN. The E2E status remains RECONCILIATION_REQUIRED on any mismatch.

Safe fallback while this repair is under review: keep the Gate E2E cleanup gate fail-closed and require manual reconciliation for any baseline mismatch. Do not submit Gate/TestNet requests to exercise this patch.

## Verification

- Failure-first reproduction: 5 passed, 2 failed before patch.
- Final-state parameterized regressions: 7 passed after patch.
- Full Gate TestNet AI-chain file: 45 passed.
- Ruff F821 check on changed Python files: passed.
- The complete offline pytest hook passed on prior documentation-only SHA 166b5841: 2,615 passed, 1 skipped, one existing Starlette/httpx warning. GitHub run 38346167714 is also for SHA 166b5841 and was still in progress at this report timestamp; it does not verify this new code patch.
- The new code is currently uncommitted on branch codex/v2-g1-offline-20261010. Exact-head full CI and independent review remain required.

## Acceptance and rollback

Acceptance requires the targeted regressions and full offline suites to pass on the exact new commit SHA, the GitHub Web checks to pass, and independent review of the baseline-preservation behavior. G0 remains NOT_ACCEPTED until an independent reviewer and human decision are recorded.

Rollback must not restore the earlier false-completion behavior. If the patch needs to be reverted, keep E2E completion blocked on any baseline mismatch until an equivalent fail-closed check is available. Preserve the failure-first tests and this record as audit evidence.

No historical V38 ledger or sample was modified. No Gemini/Antigravity/Gate API call, order, Live action, production setting change, or PR merge occurred.
