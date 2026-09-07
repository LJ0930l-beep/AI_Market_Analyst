# V2 execution record — in progress, not acceptance

Baseline: b28cde3. Supervisor's untracked `v2-audit-and-execution-plan.md` is preserved and part of this delivery. Sole developer; no delegated developer/reviewer, no push. V2 DOCX read in full on 2026-09-07. Its illustrative order code and chain-of-thought requirements are superseded by the user's safety/evidence requirements.

## Dependency order / working checkpoint

1. Trust: read-time TTL/future rejection, unknown RSS dates, strict 48h newest news, separate macro releases, explicit change period, remove decorative pseudo-chart. Initial targeted tests: 11 passed (one existing Starlette deprecation warning).
2. Public Gate metadata/CCXT adapter; unified subscriptions and bounded lifecycle; recoverable schema 14.
3. Four versioned strategies; facts/structured local model verdict; hard risk; durable simulation with failure/reconciliation tests.
4. 5+1 task navigation, dark terminal design, real chart, settings/ledger integration.
5. Full gates, production package, installed live evidence, retain old data, safe shutdown, commit.

Items 2–5 are NOT complete. No new installed UI evidence or installer has been produced yet.

## Source verification

- [Gate official futures contract schema](https://www.gate.com/docs/developers/apiv4/en/futures/): use contract multiplier and min/max size/price increment metadata, not symbol string substitution or assumptions about underlying-unit quantity.
- [CCXT Gate reference](https://docs.ccxt.com/docs/exchanges/gate) and [CCXT contract sizing FAQ](https://docs.ccxt.com/docs/faq): resolve loaded markets, use contractSize and precision. Actual locally loaded metadata still to be checked.
- [Gate official API guide](https://www.gate.com/docs/developers/apiv4/en/): authenticated futures TestNet requires separately generated credentials. No account/key is used for this milestone. Authenticated execution and external messaging remain locked/pending configuration, not PASS.

## UI design contract

Use the supplied design system: canvas #0B0D13, surface #12151E, positive #00C087, negative #FF3B69, warning #F59E0B; neutral text, tabular monospace prices/time. Compact ticker and 60/40 chart/intelligence split; no hero slogan or fabricated sparkline. Five task entries plus settings; preserve meaningful legacy data/calculations even when removing navigation.
