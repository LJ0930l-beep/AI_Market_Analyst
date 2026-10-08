# c63bf94 NoFx Gate risk and net-RR audit

Date: 2026-10-08

Base: `main` at `c63bf94f6f1db8db96f18e3b8af260a25a82e93b`

Scope: read-only baseline audit, then isolated source/test repairs and a new offline frozen verification. No Gate order, private exchange request, Live enablement, or Gemini call was made.

## Findings

The price-action profile registered `min_net_rr = 2.0` and `risk_per_trade_pct = 0.15`. The NoFx Gate opening branch built the order itself and did not run the shared entry validator. At the gateway, `MARGIN_ONLY` reserved margin but did not independently verify fee-adjusted net RR or loss at the stop; the ledger recorded `amount_risk = 0`. Historical replay did not repeat the Gate stop-risk/net-RR check. These were production-path gaps, not merely stale pytest expectations.

The active PA strategy already fixes the order target at 2,000 USDT and limits it to that amount. The 1,500-USDT figure belongs to a retained legacy template; the active-strategy projection and save path enforce the 2,000-USDT PA default. This audit did not alter other retired template records.

## Repairs

`core/trading/entry_economics.py` now provides a single Decimal cost and sizing calculation. It validates the model-authored stop, target, limit entry, Gate price tick, contract size, amount step, taker fee, and adverse slippage. It computes the stop-loss risk including entry/stop exit costs, target net reward including both fees and adverse exit slippage, and net reward/risk. LIMIT entry is price-capped but still charged taker fees; MARKET uses an adverse rounded quote plus slippage. Missing cost, price, precision, equity, or policy data fails closed. The only sizing adjustment is flooring quantity to the contract step; the routine never shrinks a position to force it through a risk or margin cap.

PA has a hard net-RR floor of 2.0 across strategy save/read projection, AI preflight, Gate gateway, atomic ledger reservation, and historical replay. A stronger configured threshold is preserved; a saved or directly edited lower value is raised for the active PA contract. Low net-RR proposals are rejected before simulated or exchange submission.

`risk_per_trade_pct = 0.15` is interpreted in percentage points: the maximum loss at the modeled stop is `fresh_account_equity × 0.15 / 100`. For example, 1,000 USDT equity allows at most 1.50 USDT modeled loss. A 2,000-USDT position whose fees alone exceed that budget is rejected. It is not silently cut to fit. This stop-risk limit is independent of `MARGIN_ONLY`, which remains the reservation/routing mode. Gate TestNet and Live use the same route and atomic fresh-equity check; their only difference is the remote account snapshot and execution environment. Replay invokes the same calculator with explicit common assumptions of 0.075% taker fee per leg and 2 bps adverse slippage; these are research assumptions, not private Gate tier or historical order-book evidence.

The replay gateway now receives the execution-price snapshot needed for equity truth and independently rejects a low-RR/over-risk entry before applying a fill. This also fixes a replay-only defect where the sink accessed a nonexistent `prices` attribute.

## Pytest classification and results

The original 103-failure run is preserved locally. Its first-pass raw triage and the initial baseline audit remain local diagnostics and are not published; the corrected classification below is authoritative. Review of the one initially labeled `REAL_REGRESSION` found that the test compared an unexpanded compact prompt projection; the long decimal text remained lossless. That failure is a test expectation/helper defect, not a product regression. The corrected baseline classification is 92 retired model/contract expectations, 10 environment-dependent failures, 1 test helper defect, and 0 confirmed product regressions. The independent NoFx path audit separately found the real Gate economics gap described above, which is now covered by new regressions. No test was deleted or weakened.

The final full suite reported **100 failed, 2,116 passed, 1 skipped, 1 warning** in 377.75 seconds; the baseline was **103 failed, 2,090 passed, 1 skipped**. Node-ID comparison found zero new failures, three baseline failures cleared, and 100 inherited failures remaining: 90 retired model/contract expectations and 10 environment-dependent failures. The post-repair comparison is bound in [`pytest-postrepair-comparison-c63bf94-20261008.json`](pytest-postrepair-comparison-c63bf94-20261008.json); the JUnit XML remains local diagnostic evidence. The exact targeted commands and V34 freeze hashes are recorded in `reports/gemini-year-research-v34-risk-rr-2000-20261008/verification-report.md`.

## Gemini transport timeouts

The V33 frozen invocation was not running when checked: the monitor-state file still named PID 27780/session 97478, but the Win32 process query found no corresponding research process. V33's preserved execution audit reports 47 COMPLETED and 3 ERROR out of 50, with the errors retained in the denominator. The failed scans (indices 16, 22, 37) reached loopback relay port 8045 over HTTP 200/SSE, received headers in 4.125–4.531 seconds, saw 688–722 stream events, then timed out while reading the response body at 161.907–161.922 seconds. The first content arrived in 4.469–9.703 seconds. The evidence establishes a body-read stall after streaming began; it does not identify whether the provider stopped producing, the relay stalled/buffered, or client consumption failed. Root cause remains unknown. The calls were not retried or replayed, and V33's reports were not edited.

See [`gemini-v33-transport-timeout-c63bf94-20261008.json`](gemini-v33-transport-timeout-c63bf94-20261008.json) and the V33 original artifacts for the redacted evidence.

## Historical sample export

[`v25-price-action-sample-20261008.json`](../research/v25-price-action-sample-20261008.json) contains the first 100 COMPLETED scans and the first 10 chronological fully closed positions from **V25, pilot-1 only**. It does not include V33. The export links closes to opening scans when available and includes decision/entry/exit fill facts, fees, funding, net PnL, estimated initial stop risk, and net R. It excludes account/order/position IDs, raw prompts, model text, credentials, and private exchange data. It is an OHLCV replay with public contract proxies, 0.075% per-leg fee and 2 bps slippage assumptions; fills are simulated, not Gate order receipts.

For those 10 closed trades, 3 won and 7 lost; cumulative net PnL was -4.77884509450 USDT. This is a prior V25 sample, not a V34 result and not a profitability acceptance. Its history hash is `c712c0486244627584c4a85ba6773cbf179da22e47176e6bd9950a7284848d56` and its registered plan hash is `99d1c32bc07a5ce3afe58b00e8d6ccbc3c2d67850f7a571fa211c30663ef3739`.

## Frozen verification and limits

V34 freezes the repaired source and test hashes in a new directory. It is an **offline contract/regression verification**, not a new 100-scan Gemini experiment and not a profit test. No Gemini call or exchange request was issued. The existing V25/V33 data and reports remain immutable; V33 errors remain in its denominator.

The 60% independently closed-trade win-rate goal, positive net return, reasonable drawdown, activity, and independent opportunity sample are not established by these changes. The reported V25 sample is below 60% and negative. A 0.15% equity risk cap combined with a fixed 2,000-USDT notional may block many trades at small account equity; that is intentional fail-closed behavior, not a reason to shrink the target. Stop gaps, actual fill slippage, fee-tier variation, partial fills, and protection execution can still exceed replay estimates. TestNet/Live execution has only been covered by offline mocked contracts here; no live behavior has been exercised.

The [V34 offline verification report](v34-offline-contract-verification-20261008.md) and [source-freeze manifest](v34-source-freeze-c63bf94-20261008.json) are included with this audit. The raw pytest/JUnit captures and local SQLite data are not published.
