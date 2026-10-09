# V38–V42 Master Implementation Plan — Execution Record

Source: `C:\Users\baicha\Downloads\AI_Market_Analyst_V38_V42_Unattended_Development_MasterPlan.md`.

This file records execution against the plan without replacing the user's source document. The repository baseline was refreshed from the attachment's stale reference to the verified V37.1 head `64a5c4206a5073e0c44e9d5cc4178705ffa24664`; work is isolated on `codex/v38-research-readiness`, based on `codex/v37.1-research-fixes`.

## Guardrails

- V35 risk calculations, Gate production paths, saved production settings, and the historical V25–V37.1 reports remain unchanged.
- No LIVE or TestNet session, private exchange request, order, Gemini request, or large archive download was made.
- `reports/` is ignored. Research datasets and test-census evidence remain local and are not part of the PR.
- V25 A0 remains unrecovered: `0/100` exact decisions. New archive contexts are not backfilled into V25.
- No test was deleted or weakened. The frozen baseline had 100 inherited failures; the latest V41 comparison resolves all 100 nodes. The earlier 53- and 27-failure reports remain immutable historical checkpoints.
- The product-contract supplement is recorded separately in `AI-Market-Analyst-Product-Contract-V1.md` and `Multi-Asset-Trader-Terminal-Roadmap.md`. These are future architecture/design deliverables; they do not alter the current V38–V42 order, scope, or production behavior.
- Multi-asset metadata, broader trader adapters, memory governance, and terminal consolidation remain sequenced after the active research and verification work. No early UI or execution expansion was made.

## Gate status

| Gate | Status | Evidence / boundary |
| --- | --- | --- |
| Gate 0 — repository, PR-chain, artifact, and test-baseline readiness | `DONE` | PRs #1–#5 are open with auto-merge disabled; V37.1 artifacts and the exact 64a5c42 baseline are preserved. See `docs/audits/V38-gate0-readiness.md`. |
| V38.1 — account-free market-only schema and offline runner | `DONE` | Separate V38 schemas; only `OBSERVE`/`WAIT`; recursive rejection of account, risk, position, order, fill, and close fields; local fixture runner has zero model calls and zero orders. |
| V38.2 — frozen sample plan and archive reconstruction | `DONE; V1 capacity superseded for independent-row claims` | Original V1 artifact remains immutable. The nonoverlap companion dataset has 80 contexts across 40 paired anchors, with zero same-symbol window overlap; only 60 optimization/validation payloads are visible and 20 untouched-test rows remain hash-only. |
| V39 — transport reliability | `PARTIAL; remote idempotency BLOCKED_WITH_EVIDENCE` | Latest structured and progressive stream Gate 3 suite: 154 focused offline tests pass. Progressive consultations now require a valid stop finish and `[DONE]` before emitting completion. Client completion POSTs default to one attempt and reject nonzero retries before network access. No real provider call was authorized; provider acceptance, deduplication, and duplicate-cost handling remain unverified. See `V39-progressive-consult-stream-followup.md` and `V39-offline-fault-injection-followup.md`. |
| V40 — simulation economics | `DONE (offline simulation scope)` | The V35/V40 focused command passes 97 tests, including duplicate finalized-bar replay after checkpoint restoration: no second fill or fee is recorded. Existing fail-closed model-receipt, risk, margin, and cost coverage remains. This is local simulator evidence; historical Gate order-book, account margin, real venue fills, and profitability remain unverified. No production economics code changed. |
| V41 — legacy full-suite census | `DONE (local full-suite gate)` | Latest post-audit comparison to exact baseline 64a5c42: 2,419 passed, 1 skipped, 0 failed; all 100 baseline failure-phase nodes pass, all 96 post-baseline nodes pass, with no missing nodes, new failures, or phase changes. The current node register is `docs/audits/V41-node-remediation-register.json`; earlier checkpoints remain preserved in their reports. Local results do not verify remote provider, exchange, or production behavior. |
| V42 — unattended model/research operation | `NOT_STARTED` | Gated on V38–V41 review, a separate explicit model/budget authorization, provider retry/idempotency policy, and approved operational stop conditions. No unattended call path is enabled. |

## V38 data capacity and overlap correction

The original V1 artifacts remain preserved: plan `V38_BINANCE_BTC_ETH_MARKET_CONTEXTS_20261009_V1` (`e08254d847ccfe6280d3cd209bbc53478efcb8eb60a93e54316b48e70dd4e02a`), dataset manifest `ca0786a4acdec7a7410b7df89e947bc2b698377fed69a13fd865fe4d1187bd99`, and V1 audit report `a3135e64e8432a6223998386a8f690ca292b0f3a1fb9058c6a58138216836134`. V1 has 180 nominal rows and heavy 8-day overlap: 20 global components, including 10 cross-partition overlapping pairs in two spanning components. It is no longer acceptable to describe its 120 visible rows as 120 independent episodes.

The addition-only companion plan is `V38_BINANCE_BTC_ETH_NONOVERLAP_CONTEXTS_20261009_V1` (plan SHA-256 `561e2b78bb046468c2d07d6378f913b35aee0f551ab50c8568c8021ca336bf9a`); its frozen dataset manifest is `1d76f1cd0e2eaa658eb8d32decc434a10e151cb7119a6b2a4f85801639537785`. It selects 20/10/10 shared UTC anchors for optimization/validation/untouched_test, with BTC and ETH paired at each anchor: 40/20/20 contexts, 80 total. The 60 optimization/validation payloads are visible; the 20 untouched-test rows remain hash/coverage-only. The independent audit recomputed all 60 visible hashes and 11,460 causal OHLCV bars, verified all 50 archive files and 24 referenced archive hashes, found 80 singleton same-symbol window components and zero cross-partition overlap. Deterministic rebuild matched the retained manifest. Audit report SHA-256: `e755f12b8de361ace1cee5a3f058ad5743fad9101749ca320620b083498bc64e`.

Price evidence remains `VERIFIED_ARCHIVE_RECONSTRUCTION`; availability remains `ASSUMED_PROXY` (`bar_end + 60s`), not observed Gate availability. The 20 validation and 20 sealed-test rows represent 10 paired temporal anchors per partition, not 20 independent trades. Neither dataset contains model outputs, labels, fills, or complete closes. Gemini-scored observations, executable trades, and completed closes remain 0. V25 A0 remains unrecoverable at 0/100.

### V38 data-integrity follow-up

- V1 integrity remains documented in `docs/audits/V38-data-integrity-followup.md`; its V3 audit report and all previous hashes are retained locally.
- The companion dataset's independent audit and limitations are in `docs/audits/V38-nonoverlap-dataset-followup.md`. Machine evidence and both datasets remain under Git-ignored `reports/v38+/`.
- The companion builder/auditor and tests are offline-only. Rebuild and audit used 0 network calls, 0 model calls, and 0 orders.
- The product contract and terminal roadmap are already present as design-only supplements. They do not displace R0/V38–V42 work or change the fixed 2,000 USDT production behavior.

## V41 acceptance result

The exact full-suite baseline at `64a5c4206a5073e0c44e9d5cc4178705ffa24664` was compared in the same environment. The latest post-audit run reports `PASS_NO_NEW_FAILURES`: no missing baseline tests, no new failure nodes, no phase changes, and all 50 added test nodes passed. The current run is 2,373 passed, 0 failed, and 1 skipped across 2,374 collected tests; all 100 baseline failure-phase nodes pass. Gate 5's local full-suite census is complete. The earlier 2,354/31-node V41/V39 checkpoint and historical failure checkpoints remain unchanged in their reports. Remote provider behavior, exchange capability, and production execution remain unverified. See `docs/audits/V39-single-attempt-retry-followup.md`, `docs/audits/V41-gate5-legacy-repair-followup.md`, the prior identity checkpoint in `V41-provider-identity-followup.md`, the historical 53-failure addendum, and the refreshed 100-node register.

## V42 entry conditions

V42 may be planned only after the post-change full-suite comparison and the independent V38 data/schema review are complete, and a separate user authorization defines the Gemini model and budget. Provider retry behavior must not be described as exactly-once until an idempotency mechanism or an explicit duplicate-cost policy is verified. No condition here authorizes exchange access or orders.

## Product-contract supplement

The long-term destination is the **Multi-Asset Autonomous AI Trading Terminal** defined by the product contract and roadmap documents. The product contract preserves model autonomy for market research and trade judgment while reserving product eligibility, account permission, execution, and risk limits to independently validated services. The roadmap records reuse and gaps across the instrument registry, Gate/economics/gateway/settlement modules, model provider boundary, market intelligence, decision memory, replay, APIs, and React workbench. This supplement is documentation only; V38 P0 was completed first, V38–V42 gate status is unchanged, and no model/provider/exchange call, production setting change, or order is authorized.

## V39 offline SSE fault-injection addendum — 2026-10-09

The V39 transport follow-up is recorded in [`V39-offline-fault-injection-followup.md`](../audits/V39-offline-fault-injection-followup.md). It adds separate request/attempt IDs, partial-byte and explicit incomplete-stream audit state, fail-closed handling for an exact explicit-ID SSE replay, and local loopback tests for TCP reset, truncated JSON, duplicate event, non-stop provider finish, body stall, and success. The final full suite is 2,410 passed, 1 skipped, 0 failed; comparison to frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664` is `PASS_NO_NEW_FAILURES` with 0 new failures and 0 phase changes. This remains local transport evidence: V39 is still partial while remote idempotency, provider replay, and duplicate-cost semantics are unknown.

The Product Contract V1 and Multi-Asset Trader Terminal Roadmap requested as this task's supplement already exist as design documents in the V38–V42 branch. R0 remains first. Product implementation starts only after the applicable research, reliability, and test-governance gates are reviewed; then use the roadmap dependency order: versioned instrument/capability metadata before account fee and settlement economics; those before executable portfolio risk; typed trader/intelligence and governed memory before terminal consolidation; shadow readiness before any separately authorized Live review. The added product-contract work does not change V35 production economics or grant model, exchange, account, Live, or merge authorization.

## V39 progressive consultation stream addendum — 2026-10-09

The separate progressive `/consult/stream` client now fails closed on missing `[DONE]`, missing or non-`stop` `finish_reason`, malformed events, provider errors, and data after finish. Intentional consumer cancellation remains cancellation, and partial deltas cannot produce a `done` event or model receipt after a transport failure. The new audit is `docs/audits/V39-progressive-consult-stream-followup.md`. Focused Gate 3 verification is 154 passed; the full suite is 2,418 passed and 1 skipped. Exact same-environment comparison against baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664` is `PASS_NO_NEW_FAILURES`: 100 inherited failures remain resolved, 95 post-baseline tests pass, with zero missing tests, new failure nodes, or phase changes. No model/provider or exchange request was made. V39 remains partial pending actual provider compatibility, idempotency, replay, and duplicate-cost evidence; V42 remains separately gated.

## V40 duplicate-bar replay addendum — 2026-10-09

`tests/test_ai_simulation.py::test_filled_bar_replay_is_idempotent_after_checkpoint_restore` now proves a filled limit order is not filled or charged twice when the same closed bar is redelivered to either the original simulator or its restored checkpoint. The full V35/V40 focused suite is 97 passed. Full local evidence is 2,419 passed and 1 skipped; exact frozen-baseline comparison is `PASS_NO_NEW_FAILURES` with 100 inherited failures resolved, all 96 post-baseline nodes passing, and no missing/new failed nodes or phase changes. See `docs/audits/V40-simulation-economics.md`. This verifies repeated OHLCV-bar handling in the simulation only; venue event delivery and Gate fill idempotency remain unverified.
