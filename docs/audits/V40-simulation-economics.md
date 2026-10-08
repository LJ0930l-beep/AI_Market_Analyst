# V40 Simulation Economics Audit

## Scope

V40 reviewed the existing V35 Decimal-based feasibility and risk-budget logic plus offline AI simulation and resumable-run behavior. No production risk formula, Gate adapter, or strategy configuration was changed in this V38 branch.

## Verification

Command:

```text
python -m pytest tests/test_v35_trade_feasibility.py tests/test_ai_simulation.py tests/test_ai_template_runner.py -q
```

Result: **95 passed, 1 failed**. The sole failure is `tests/test_ai_template_runner.py::test_production_prompt_manages_positions_across_scans`, asserting one closed trade after a fixture-driven multi-scan replay. This exact node and `call` phase are in the frozen V37.1 baseline and the earlier reviewed 100-failure census. It is an inherited failure, not a V38 regression. The combined V39/V40 run produced the same node as its only failure.

Existing coverage includes fee-adjusted stop risk and net reward/risk, fixed-notional and risk-budgeted sizing, leverage not bypassing stop-risk limits, margin/contract checks, same-bar stop/target ordering, partial fills, expiry/cancellation, funding idempotency, recovery after interrupted model-result persistence, and duplicate-call prevention in fixture replay.

## Limits

These are deterministic replay and fixture tests. They do not prove historical Gate order-book availability, real venue fills, or account-specific margin. The inherited failure remains visible; V40 is **PARTIAL**, and no blanket “all simulation states are correct” claim is made until that failure is diagnosed and the complete suite is compared after V38 changes.
