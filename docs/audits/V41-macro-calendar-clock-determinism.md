# V41 Macro Calendar Clock Determinism Follow-up — 2026-10-10

## Scope

This follow-up is based on Gate TestNet repair commit `5831f9785e5f8b09d6a819c562949beceb6db4b0`. It fixes three existing macro-actual tests that became dependent on the host wall clock after their fixed October 2, 2026 schedule fixtures aged past the calendar's seven-day lookback window. It does not change market data, trading, Gate credentials, or production risk settings.

## Finding and change

The tests supplied a deterministic `now` value to `refresh_calendar`, but omitted the scheduler `clock` dependency. When an actual provider was injected, the calendar fetch completion time still came from the real clock. The fixed schedule rows were then filtered as out of window. After freezing the scheduler clock, a second wall-clock read in `calendar_status` still marked a simulated historical status stale.

`calendar_status` now accepts an optional aware `now` value and uses it consistently for stale and retry calculations. `refresh_calendar` passes its effective time through status reads. Normal production calls still default to the real UTC clock. The three tests now inject the same deterministic scheduler clock and retain all business assertions.

## Verification

- Before the fix, the three affected node IDs reproduced as failures on the exact parent commit; after the fix, all three pass.
- `python -m pytest -q tests/test_macro_actuals.py tests/test_final_calendar.py`: **16 passed**.
- Full local suite: **2,464 passed, 1 skipped, 1 existing Starlette/httpx deprecation warning** in 325.92 seconds.
- `compileall` and `git diff --check`: passed.
- Ruff on the two touched files still reports existing diagnostics. Baseline-to-candidate counts changed from 19 to 18 in `core/macro_calendar.py`, and stayed 16 in `tests/test_macro_actuals.py`; no diagnostic code count increased. Repository Ruff is therefore not claimed fully clean.
- This patch made no network, model, or exchange calls and created no orders.

## Boundary

This is a deterministic clock/status fix and test-fixture correction, not new macro-source evidence. It does not establish point-in-time availability of historical news, validate Gate execution, or change the status of the V38 blind-label/Gemini evaluation gate. PR #23's GitHub full-suite job was still running when this follow-up was prepared; its result remains separate evidence.
