# V34 offline frozen verification

## Status

`OFFLINE_CONTRACT_VERIFICATION_COMPLETE_NOT_PROFIT_ACCEPTANCE`

Base commit: `c63bf94f6f1db8db96f18e3b8af260a25a82e93b`

Freeze: [`v34-source-freeze-c63bf94-20261008.json`](v34-source-freeze-c63bf94-20261008.json)

Companion audit: [`audit-c63bf94-risk-rr-repair-20261008.md`](audit-c63bf94-risk-rr-repair-20261008.md)

This freeze validates execution contracts with offline tests. It is not a new Gemini replay or a profitability test. The model-call count, private exchange-call count, and real-order count are all zero. No Live session was enabled. The V25 sample is bound as a historical reference only; no V33 records are included.

## Policy frozen in this verification

- Active strategy: `price_action_structure` only.
- Notional target: 2,000 USDT; fixed mode. Amount precision may floor quantity only. No risk- or margin-driven silent downsizing.
- Stop-loss risk: no more than `fresh equity × 0.15 / 100`, after the modeled fees and adverse slippage.
- PA net reward/risk: hard floor 2.0 after modeled fees and adverse slippage. A stronger saved value remains stronger; a saved value below 2.0 cannot weaken PA.
- `MARGIN_ONLY` remains a margin-reservation mode. It does not waive stop-risk validation.
- Missing equity, fee, slippage, stop/target, or Gate precision data fails closed.

## Verification

Targeted Gate policy tests: 87 passed across `test_gate_entry_economics.py`, `test_nofx_gate_route.py`, `test_price_action_specialization.py`, and `test_ai_strategy_book.py`. This includes both TESTNET and LIVE route contracts, a persisted lower-RR attempt, fresh-equity ledger rejection, and fixed-notional preservation.

Targeted historical replay test: 2 passed. Replay rejects low net RR and stop risk before applying a simulated fill, including an attempted PA policy value of 1.5.

New economics/helper tests pass Ruff; changed sources and tests compile. Full suite: **100 failed, 2,116 passed, 1 skipped, 1 warning** in 377.75 seconds, versus baseline **103 failed, 2,090 passed, 1 skipped**. Exact node-ID comparison found **zero new failures**, three baseline failures cleared, and 100 inherited failures remaining (90 retired model/contract expectations and 10 environment dependencies). No failure was removed or ignored. The compact node-ID comparison is in [`pytest-postrepair-comparison-c63bf94-20261008.json`](../../docs/audits/pytest-postrepair-comparison-c63bf94-20261008.json); the 382 KB JUnit XML stays local as diagnostic evidence.

## Outcome boundary

Passing these tests verifies deterministic policy enforcement, not realized Gate fills, not Gemini strategy quality, and not 60% win-rate acceptance. The separately bound V25 sample has 3 wins / 7 losses across 10 closes and -4.77884509450 USDT net replay PnL. The 0.15% risk cap plus fixed 2,000-USDT notional will reject orders when the account equity and stop geometry cannot support the modeled loss budget. That is expected; the sizing target is not silently reduced.

V33 transport errors remain in its 50-scan checkpoint denominator (47 COMPLETED, 3 ERROR); their cause is still unknown. V33 logs, SQLite, and report files were not changed. See the redacted [transport diagnostic](gemini-v33-transport-timeout-c63bf94-20261008.json).
