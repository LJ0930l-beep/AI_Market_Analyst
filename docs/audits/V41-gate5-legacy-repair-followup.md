# V41 Gate 5 Legacy Failure Repair Follow-up

## Disposition

**The local V41 full-suite census is complete.** The current suite has no failed tests, and the exact comparison against frozen baseline commit `64a5c4206a5073e0c44e9d5cc4178705ffa24664` resolves all 100 baseline failure nodes. This is local offline evidence; it does not establish remote provider identity, exchange capability, or production execution behavior. The earlier 53- and 27-failure reports remain historical checkpoints.

## Changes

- Updated legacy fixtures and assertions to use the active Gemini response-receipt, price-action cadence, strategy-template, and API contracts.
- Added a fail-closed NOFX import guard for unsupported primary timeframes. The active contract is 15m decision cadence with 5m entry confirmation; a 5m imported primary timeframe is rejected without changing the active strategy.
- Preserved execution settings, production risk limits, fixed 2,000 USDT notional, account permissions, V35 risk logic, and historical research artifacts.
- No Gemini/model call, exchange request, TestNet/Live session, or order was made.

## Verification

- Full suite: **2,349 passed, 0 failed, 1 skipped** across **2,350 collected test nodes**; one Starlette/httpx deprecation warning.
- Exact comparison: **`PASS_NO_NEW_FAILURES`**, same environment as the frozen baseline; **100/100** baseline failed nodes now pass, **26** new test nodes pass, **0** failed nodes, **0** missing baseline nodes, and **0** phase changes.
- Local evidence (Git-ignored): `reports/v38+/verification/v41-gate5-after-legacy-repair.json` and `reports/v38+/verification/v41-gate5-after-legacy-repair-vs-baseline.json`.
- Evidence digests: current run `d859834cda51f37422f6b8bc419eaec0ccc5a70cf69906bb5a8ef5e58e9b9607`; comparison file SHA-256 `31b4c796f87f9c0c1e5325eb635220fc611d7448c24663221316508aa78a7080`.
- Ruff diff against the prior branch state across the 16 modified Python files: **124 baseline diagnostics, 120 current diagnostics, 0 added, 4 removed**. Focused checks for unused imports/locals and duplicate receipt keys passed. Existing lint diagnostics remain in those files.
- `git diff --check` passed.

## Remaining limits

- Offline fixtures prove local contract handling only. Remote Gemini model identity, response consistency, retry/idempotency, and market-data/provider availability were not exercised.
- Gate and account execution paths were not invoked. Product Contract V1 and the multi-asset roadmap remain design artifacts sequenced after V38–V42 research gates.
- V38 research data still has assumed availability timestamps and no account, order-book, or execution evidence. No profitability or trade-quality conclusion follows from this test run.
- V42 remains gated on independent data/schema review, an explicit model and spend authorization, retry/duplicate-cost policy, and operational stop conditions.

## Acceptance

V41's local full-suite census and exact baseline comparison meet the test-remediation target. Overall V38–V42 acceptance remains **PARTIAL**; this report does not authorize paid model calls, exchange access, Live mode, production setting changes, or orders.
