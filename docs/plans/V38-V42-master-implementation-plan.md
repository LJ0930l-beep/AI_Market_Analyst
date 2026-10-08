# V38–V42 Master Implementation Plan — Execution Record

Source: `C:\Users\baicha\Downloads\AI_Market_Analyst_V38_V42_Unattended_Development_MasterPlan.md`.

This file records execution against the plan without replacing the user's source document. The repository baseline was refreshed from the attachment's stale reference to the verified V37.1 head `64a5c4206a5073e0c44e9d5cc4178705ffa24664`; work is isolated on `codex/v38-research-readiness`, based on `codex/v37.1-research-fixes`.

## Guardrails

- V35 risk calculations, Gate production paths, saved production settings, and the historical V25–V37.1 reports remain unchanged.
- No LIVE or TestNet session, private exchange request, order, Gemini request, or large archive download was made.
- `reports/` is ignored. Research datasets and test-census evidence remain local and are not part of the PR.
- V25 A0 remains unrecovered: `0/100` exact decisions. New archive contexts are not backfilled into V25.
- No test was deleted or weakened. The frozen baseline had 100 inherited failures; the latest V41 comparison resolves 73 nodes and still records 27 inherited failures in the current node register. The earlier 53-failure addendum remains a historical checkpoint.
- The product-contract supplement is recorded separately in `AI-Market-Analyst-Product-Contract-V1.md` and `Multi-Asset-Trader-Terminal-Roadmap.md`. These are future architecture/design deliverables; they do not alter the current V38–V42 order, scope, or production behavior.
- Multi-asset metadata, broader trader adapters, memory governance, and terminal consolidation remain sequenced after the active research and verification work. No early UI or execution expansion was made.

## Gate status

| Gate | Status | Evidence / boundary |
| --- | --- | --- |
| Gate 0 — repository, PR-chain, artifact, and test-baseline readiness | `DONE` | PRs #1–#5 are open with auto-merge disabled; V37.1 artifacts and the exact 64a5c42 baseline are preserved. See `docs/audits/V38-gate0-readiness.md`. |
| V38.1 — account-free market-only schema and offline runner | `DONE` | Separate V38 schemas; only `OBSERVE`/`WAIT`; recursive rejection of account, risk, position, order, fill, and close fields; local fixture runner has zero model calls and zero orders. |
| V38.2 — frozen sample plan and archive reconstruction | `DONE` | Data-preparation scope only: 180 contexts selected; 120 optimization/validation payloads and 60 hash-only untouched-test rows. No outcomes or labels. Availability remains an assumed proxy. |
| V39 — transport reliability | `PARTIAL` | 91 focused tests pass. Loopback and deterministic fixtures cover parsing, bounded streams, deadlines, and timeout provenance. No real provider call or remote exactly-once guarantee. |
| V40 — simulation economics | `PARTIAL` | 95 focused tests pass; one known inherited template-runner assertion fails identically to the frozen baseline. No production economics code changed. |
| V41 — legacy full-suite census | `PARTIAL` | Latest exact baseline 64a5c42 comparison: 73 baseline failures now pass, 27 remain, with no new failure nodes or phase changes; all 25 new test nodes pass. V13 lease-loss cancellation, V2 simulation permission/revocation, and current response-identity behavior have focused offline coverage. The current node register is `docs/audits/V41-node-remediation-register.json`; the 53-failure addendum remains a historical checkpoint and `V41-provider-identity-followup.md` records the latest run. |
| V42 — unattended model/research operation | `NOT_STARTED` | Gated on V38–V41 review, a separate explicit model/budget authorization, provider retry/idempotency policy, and approved operational stop conditions. No unattended call path is enabled. |

## V38 data capacity

- Frozen plan: `V38_BINANCE_BTC_ETH_MARKET_CONTEXTS_20261009_V1`, SHA-256 `e08254d847ccfe6280d3cd209bbc53478efcb8eb60a93e54316b48e70dd4e02a`.
- Dataset manifest: SHA-256 `ca0786a4acdec7a7410b7df89e947bc2b698377fed69a13fd865fe4d1187bd99`.
- 30 contexts per symbol and partition across BTCUSDT/ETHUSDT and optimization/validation/untouched_test: 180 total.
- 120 complete inputs are staged in optimization and validation. The 60 untouched-test records contain only hashes and coverage metadata.
- Archive price evidence is `VERIFIED_ARCHIVE_RECONSTRUCTION`; availability is `ASSUMED_PROXY` (`bar_end + 60s`). Twenty-three overlapping input-window clusters mean rows are not 180 independent trades.
- Prepared research input count: 120. Gemini-scored observations: 0. Gate-executable trades: 0. Complete closes: 0.

## V41 acceptance result

The exact full-suite baseline was rerun at `64a5c4206a5073e0c44e9d5cc4178705ffa24664` in the same environment as the latest V41 run. The comparator reports `PASS_NO_NEW_FAILURES`: no missing baseline tests, no new failure nodes, no phase changes, and all 25 new test nodes passed. The current run is 2,321 passed, 27 failed, and 1 skipped across 2,349 collected tests; 73 of the 100 baseline failures no longer fail. Focused offline tests cover lease-loss cancellation, simulation authorization revocation, and response-identity verification. Gate 5 remains partial because 27 inherited failures remain and remote provider behavior is not verified. See `docs/audits/V41-provider-identity-followup.md`, the prior checkpoint in `docs/audits/V41-gate5-node-audit-addendum.md`, and the current 100-node register.

## V42 entry conditions

V42 may be planned only after the post-change full-suite comparison is complete, the remaining legacy failures have an explicit disposition, V38 data and schema boundaries pass review, and a separate user authorization defines the Gemini model and budget. Provider retry behavior must not be described as exactly-once until an idempotency mechanism or an explicit duplicate-cost policy is verified. No condition here authorizes exchange access or orders.

## Product-contract supplement

The long-term destination is the **Multi-Asset Autonomous AI Trading Terminal** defined by the product contract and roadmap documents. The product contract preserves model autonomy for market research and trade judgment while reserving product eligibility, account permission, execution, and risk limits to independently validated services. The roadmap records reuse and gaps across the instrument registry, Gate/economics/gateway/settlement modules, model provider boundary, market intelligence, decision memory, replay, APIs, and React workbench. This supplement is documentation only; V38 P0 was completed first, V38–V42 gate status is unchanged, and no model/provider/exchange call, production setting change, or order is authorized.
