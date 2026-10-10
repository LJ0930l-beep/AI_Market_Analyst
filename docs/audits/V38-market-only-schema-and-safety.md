# V38 Market-Only Schema and Safety Audit

## Contract

V38 lives in `core/replay/pa_decision_quality_v38/`; it does not route into the production trading engine. Input is a point-in-time projection with decision ID/time, symbol, partition, and causal OHLCV bars for 15m main, 5m entry confirmation, and 1h/4h context. The existing V36 context builder enforces causal ordering. V38 requires timezone-aware timestamps, rejects data available before its bar is confirmed, and validates the frozen half-open partition label.

The input and analysis schemas are versioned separately:

- `pa-market-only-v38/input-1`
- `pa-market-only-v38/analysis-1`
- record and review envelopes are versioned separately in code.

Analysis is limited to `OBSERVE` or `WAIT`. The schema requires evidence references for claims. Recursive validation rejects account/equity, margin, risk, leverage, entry/stop/target prices, positions, orders, proposals, fills, trade IDs, gateway acceptance, and complete-close fields. The offline CLI requires a labeled fixture mode, reads local input, refuses output overwrite, and writes below ignored `reports/v38+`.

## Lifecycle distinction

Fixture/market analysis, an eligible trade proposal, gateway acceptance, venue fill, and complete close are separate states. The market-only lane never creates an eligible proposal; gateway acceptance, venue fill, and complete close remain `NOT_OBSERVED`. Its review explicitly disallows trade-profit and market-quality claims for fixture records.

## Verification

- `python -m pytest tests/v38 -q`: **23 passed**, including malformed array/object enum values and non-string partition labels failing closed.
- The offline stub was run against 9 existing V37.1 market points. It produced 9 labeled fixture records, 0 eligible proposals, 0 model calls, 0 orders, 0 fills, and 0 complete closes. This is schema/runner verification, not Gemini decision evidence.
- No provider client is called by the offline stub. The V38 test suite patches network hooks to fail if touched and asserts zero model calls/orders.
- `python -m ruff check core/replay/pa_decision_quality_v38 scripts/run_market_only_v38.py scripts/build_v38_market_dataset.py tests/v38`: **passed** after lint fixes.

## Limits

The market-only output says nothing about account suitability, net risk/reward, margin, actual order feasibility, execution, or profitability. It is not a substitute for V35 risk enforcement and cannot be connected to Gate without a separately reviewed proposal/risk/gateway contract. Fixture output must not be counted as a model decision.
