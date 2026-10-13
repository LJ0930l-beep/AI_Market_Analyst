# V40 Simulation Economics Audit

## Scope

V40 reviewed the existing V35 Decimal-based feasibility and risk-budget logic plus offline AI simulation and resumable-run behavior. No production risk formula, Gate adapter, or strategy configuration was changed in this V38 branch.

## Verification

Command:

```text
python -m pytest tests/test_v35_trade_feasibility.py tests/test_ai_simulation.py tests/test_ai_template_runner.py -q
```

Historical result: **95 passed, 1 failed**. The failed node was `tests/test_ai_template_runner.py::test_production_prompt_manages_positions_across_scans`. Its frozen-baseline assertion treated an exit as a close even when the fixture lacked a verified model completion receipt.

Current result from the same command: **96 passed, 0 failed**. The node now asserts fail-closed behavior (`MODEL_EXIT_RECEIPT_UNVERIFIED`) when the fixture cannot prove the required response receipt. The exact full-suite comparison also confirms all 100 frozen baseline failure nodes pass. The node-level disposition and evidence are recorded in `V41-node-remediation-register.json`.

Existing coverage includes fee-adjusted stop risk and net reward/risk, fixed-notional and risk-budgeted sizing, leverage not bypassing stop-risk limits, margin/contract checks, same-bar stop/target ordering, partial fills, expiry/cancellation, funding idempotency, recovery after interrupted model-result persistence, and duplicate-call prevention in fixture replay.

## Limits

These are deterministic replay and fixture tests. They do not prove historical Gate order-book availability, real venue fills, or account-specific margin. **V40 status: DONE for the offline simulation/economics scope.** This status covers the implemented deterministic simulation and regression evidence; it does not certify real venue execution, complete historical economics, or profitability.

## Duplicate-bar and checkpoint replay follow-up — 2026-10-09

The earlier `advance()` idempotency test used an empty account and did not prove that replaying a bar that had already filled an order would avoid a duplicate fill after state recovery. Added `test_filled_bar_replay_is_idempotent_after_checkpoint_restore`: submit a resting limit, fill it on a later closed bar, restore a checkpoint, then redeliver that exact bar to both the original and restored accounts. Both replays produce no events; serialized state matches, the position still has one lot, and the filled-order count remains one. The existing `processed_bars` checkpoint state already implements this behavior; no simulation or production execution code changed.

Verification:

- V35/V40 focused economics suite: **97 passed**.
- Ruff differential for `tests/test_ai_simulation.py`: **0 added diagnostics**; its 2 pre-existing diagnostics remain unchanged. The undefined-name check passed. `compileall` and `git diff --check` pass.
- Full suite with frozen evidence: **2,419 passed, 1 skipped, 0 failed** (2,420 collected).
- Same-environment comparison to frozen V37.1 baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**. All 100 inherited failure nodes remain resolved; all 96 post-baseline tests pass; no missing baseline nodes, new failures, or phase changes.
- Git-ignored local evidence:
  - `reports/v38+/verification/v40-duplicate-fill-idempotency-20261009.json` — SHA-256 `932eeb318a243aecff3e3848b95812369181b25686c8b7c50032427f8f6f487c`
  - `reports/v38+/verification/v40-duplicate-fill-idempotency-20261009-vs-baseline.json` — SHA-256 `4ec5cd7e4a96d8c12bc9c44d23d44e23d749e2b4a5ff51fced926dda50190472`

This proves idempotent handling of repeated finalized OHLCV bars in the local simulator, including checkpoint recovery. It does not exercise duplicate Gate websocket/order-update payloads or establish live venue fill idempotency. No exchange access or order was made; the observed fill is a local simulation event, not a Gate fill.
