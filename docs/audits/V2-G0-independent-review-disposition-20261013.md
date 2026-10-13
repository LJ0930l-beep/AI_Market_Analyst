# V2 G0 independent-review finding disposition — 2026-10-13

## Scope and current review boundary

This additive disposition compares two user-provided, fixed-head review reports with the current PR #27 candidate. It records source and regression-test evidence; it is **not** a new independent GitHub review and does not accept G0.

- Review report `AI_Market_Analyst_PR27_G0_独立复审_20261013.md`: reviewed head `f856859cb83517729b28a56e3ed310f920ee643f`, SHA-256 `A895236DA9E721DAB141E2B723DB61D3F11AF758B08F4C59DAF788249A6F1802`.
- Review report `AI_Market_Analyst_PR26_PR27_G0_独立审阅_返修意见_20261013.md`: reviewed head `9492ec6858a62c072a96de56c32c727b77cddfeb`, SHA-256 `66B0E05CB9B0D8B994B6269BE3D15F2CDA5F2C20F9E23A68C78F2E298DEAD148`.
- Current PR #27 head at evidence capture: `13cc31e2cb7713391fd93f779e906dea807b0324`, based on `main@8642639a78ca9aa56fbad1e88aea4b82169ea433`. Both reviewed heads and the main baseline are ancestors of this candidate.
- Candidate size at capture: 307 files, +107,133 / −560. The reviewer expressly limited both reports to focused high-risk static review, not line-by-line review of the full integration diff.
- Exact-head hosted run [38390357606](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38390357606) was in progress at `2026-10-13T11:30:18Z`: quick gate passed, Web tests/lint/type-check/build passed, full Python suite still running. The local commit hook on this head reported 2,703 passed, 1 skipped, and one existing Starlette/httpx deprecation warning.

## Finding disposition

| Review finding | Current candidate evidence | Disposition |
|---|---|---|
| PR #26 is not a standalone G0/G1 release candidate (F-01) | PR #27 directly targets `main`; it contains `web/src/pages/V2WorkspacePage.tsx`, the account-selection guard in both Gate refresh paths, and a Web CI job. `web/src/pages/V2WorkspacePage.test.tsx::keeps an incomplete account cockpit read-only and leaves missing facts unverified` supplies an empty `/accounts` response and asserts the Gate TestNet workspace request remains scoped and singular. PR #26 remains Draft. | Candidate structure and focused regression evidence address the report. Do not merge PR #26 independently. Full semantic review is still outstanding. |
| Cleanup completion lacked a final account-wide reconciliation (F-02) | `core/trading/gate_testnet_e2e.py` fetches `final_truth` after owned protection cleanup, requires a complete TESTNET snapshot, compares final position exposure and pending-order IDs against the exact pre-entry baseline, then marks CLEANUP completed. Tests include `test_gate_testnet_e2e_requires_final_account_truth_after_protection_cleanup` and `test_gate_testnet_e2e_does_not_classify_trigger_success_or_unknown_as_cancelled`. | Code and fixture evidence address the reported false-completion path. No Gate/TestNet request was made; real venue behavior remains unverified. |
| Malformed native `is_close` could be hidden by a recognized close order type (F-03) | `GateLiveTrader.fetch_protection_order()` rejects an unparseable present `is_close` with `GATE_PROTECTION_IS_CLOSE_UNVERIFIED` before order-type inference. `tests/test_gate_testnet_ai_chain.py::test_gate_protection_readback_rejects_malformed_is_close_even_for_known_close_type` reproduces the exact reported shape. Noncanonical numeric aliases and valid partial reduce-only behavior also have regression cases. | Code and focused regression evidence address the reported trust-boundary mismatch. Exact-current-head independent review is still outstanding. |
| Noncanonical native protection booleans and full 100/101-order snapshots (P1-01/P1-02) | B-20261013-008 records the strict native parser, full-page degradation, failure-first evidence, and tests for normal and full pages. B-20261013-009 records the separate ordinary open-order alias checks. | Repairs remain fail-closed and `FIX_IN_REVIEW`; hosted CI and an independent review are acceptance evidence, not permission for TestNet requests. |

## G0/G1 disposition

- The source-code and named-fixture checks support that the listed review findings are repaired in the current candidate. This is a focused disposition only; it does not establish complete semantic review of the 307-file diff or remote Gate behavior.
- PR #27 remains Draft with no submitted review or reviewer request. Its current exact-head Python full suite was still running at evidence capture. Therefore **G0 remains `NOT_ACCEPTED / FIX_IN_REVIEW`**.
- G1 offline response-contract work remains partially verified. The exact-one-choice guard is shared by the V2 contract evaluator, authorized-runner result handler, and format-pilot result handler. The immutable V38 ledger remains SHA-256 `820212A5699B718C922232C584FCFDCB4F79B675EA8653B1898A0D4025B837A7`; historical invalid-response classifications remain unchanged.
- Provider `response_format`, authenticated model/weight identity, billing, quota, and idempotency remain `UNKNOWN_UNVERIFIED`. Both V2 configs keep remote dispatch disabled and call authorization at zero. **G2 remains unauthorized.**
- No Gemini/Antigravity call, Gate/TestNet request, order, Live action, production setting change, or merge occurred during this disposition.

## Next acceptance steps

1. Complete exact-head hosted CI for `13cc31e2cb7713391fd93f779e906dea807b0324` and refresh the all-PR check snapshot.
2. Obtain an authorized reviewer’s formal review of the current candidate and record a human G0 accept/repair decision. The two supplied reports are historical, focused reviews and are not current GitHub review state.
3. Keep PR #26/#27 Draft and unmerged until the full G0 checklist passes. Keep G2 and Gate/TestNet requests closed absent their own prerequisites and fresh explicit authorization.
