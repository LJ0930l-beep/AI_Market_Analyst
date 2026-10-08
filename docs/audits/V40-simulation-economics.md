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
