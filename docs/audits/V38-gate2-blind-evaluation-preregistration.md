# V38 Gate 2 Blind Evaluation Preregistration — 2026-10-09

## Decision

**`PASS_FOR_DATA_AND_EVALUATION_PREPARATION_WITH_CAVEATS`.** The primary Gate 2 set now combines seeded monthly/temporal-stratum sampling with purged, nonoverlapping 8-day input windows. The earlier V38 V1 and V2 artifacts are preserved as diagnostic-only predecessors. This report does not claim that reference labels, Gemini analyses, executable proposals, or profitable trades exist.

No V25/V37 artifact or prior V38 dataset/report was overwritten. No Binance archive was downloaded. No Gemini call, private exchange request, order, or production risk change was performed for the dataset build and audit.

## Primary sample and preserved predecessors

| Dataset | Sampling | Total | Visible optimization + validation | Untouched test | Disposition |
| --- | --- | ---: | ---: | ---: | --- |
| `V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1` | Frozen SHA-256 seed selects one day from each predeclared month × 3 temporal-slot stratum | 72 | 54 (36 optimization, 18 validation) | 18 hashes/coverage only | **Primary Gate 2 sample.** 36 paired temporal anchors, 0 same-symbol window overlaps, 0 cross-partition overlaps. No IID trade claim. |
| `V38_BINANCE_BTC_ETH_MARKET_CONTEXTS_20261009_V1` | Earlier seeded UTC-day rank, monthly quotas, 48-hour minimum gap | 180 | 120 | 60 hashes/coverage only | Preserved, diagnostic-only. Its audit records 20 global overlap components and 10 cross-partition overlap pairs; not eligible for blind optimization/validation comparison. |
| `V38_BINANCE_BTC_ETH_NONOVERLAP_CONTEXTS_20261009_V1` | Earlier fixed 9-day UTC grid; 8-day windows | 80 | 60 | 20 hashes/coverage only | Preserved, fixed-grid sensitivity-only. Its zero overlaps do not make it a random sample. Do not pool it with the primary set. |

No row counts are pooled across these three datasets. The V3 primary contains 18 optimization anchors, 9 validation anchors, and 9 sealed test anchors; BTC/ETH rows share timestamps and are paired. Month × slot strata use deterministic SHA-256 selection among two predeclared UTC dates per slot, not market outcomes, A3 triggers, direction, volatility, or model output. Each 8-day half-open window stays within its frozen partition; adjacent windows touch at most at the exclusive boundary and do not share bars.

The frozen partition policy remains optimization `[2025-10-01, 2026-04-01)`, validation `[2026-04-01, 2026-07-01)`, and untouched test `[2026-07-01, 2026-10-01)` UTC. V3 sample plan SHA-256 is `094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f`; its generated dataset manifest SHA-256 is `1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a`. The V3 integrity audit report SHA-256 is `c9a71e8fb1f6a062058ec0ae7a23e1f5298b36ee60c671f6414818657fc7fa15` and is local under ignored `reports/v38+/verification/`.

All 50 existing Binance UM archive files were checksum-verified; all 54 visible context inputs were rebuilt through the V38 causal market-input validator. The builder emitted 10,314 OHLCV bars across 15m (2,592), 5m (2,592), 1h (2,592), and 4h (2,538); the independent auditor checked their strict cutoff, geometry, provenance, and partition membership. Price evidence is `VERIFIED_ARCHIVE_RECONSTRUCTION`; availability evidence remains `ASSUMED_PROXY` (`bar_end + 60 seconds`). No Gate receive-time, account state, contract snapshot, news history, or point-in-time bid/ask is available. Missing news stays `UNKNOWN`. V25 A0 remains unrecoverable at `0/100`.

The independent auditor now enforces exact field sets for the manifest, visible input document/rows, and untouched-test hash/coverage document/rows. Unregistered payload fields—including label, model output, or outcome fields—are rejected even if a caller recomputes the outer manifest hash. The frozen V3 artifacts pass this audit; the audit implementation has explicit extra-field rejection coverage.

## Blind reference-label protocol and evaluation arms

Protocol `V38_PA_REFERENCE_BLIND_LABELS_20261009_V2` is hash-frozen with canonical SHA-256 `14827250890d37e33fc0b25121ca0414065e9e835c6beef830c95ed2a3fda64c`. Its embedded registry binds the 54 visible V3 decision IDs to exact causal-input hashes; a label that only claims the V3 manifest without matching a registered ID/hash/partition is rejected. Only the V3 primary manifest is label-eligible. V1 and V2 datasets are explicitly diagnostic-only. Two independent pseudonymous raters annotate each visible context, with model output, arm identity, future prices/outcomes, and untouched-test payload hidden. `UNKNOWN` and per-rater disagreements are retained; adjudication adds a separate record and cannot overwrite earlier ratings. The validator binds each annotation to the exact V3 manifest, decision ID, input hash, and partition, and rejects test labels, malformed or unavailable evidence, and model/outcome fields. Protocol V1 was superseded before any annotation was created.

Actual annotations created: **0**. `A1_MARKET_ONLY` and `A2_MARKET_ONLY` have frozen prompt file hashes and the common market-only schema; provider/model IDs remain unselected and their status is `NOT_RUN_NO_MODEL_BUDGET_AUTHORIZATION`. Validation is reserved for one selection after the caller separately authorizes and freezes model identity, prompts, schema, metrics, and stopping rules. `A3_FROZEN_RULE` is hash-bound to `FAILED_BREAKOUT_20X15M_5M_REENTRY_FRESH_V2`; no point-in-time bid/ask exists, so executable A3 evaluation is blocked. `B0_NEW_BASELINE` is not included and cannot be relabeled as V25 A0.

Metric definitions preserve all 54 planned visible decisions in status/completion denominators, retain ERROR/TIMEOUT/invalid records, keep OBSERVE/WAIT and UNKNOWN concordance separate, score evidence and target support only against independent labels, and count proposals, gateway acceptance, fills, and complete closes separately. Provider latency/tokens/cost require verified telemetry. Every research metric result remains null; the offline stub is not a Gemini response. Zero complete closes means strategy PnL and drawdown remain `INSUFFICIENT_SAMPLE`; the minimum strategy-level threshold remains 30 verified complete closes.

## Safety and remaining blockers

- Gemini research calls: 0; exact model and budget authorization: absent.
- Blind annotations and scored provider decisions: 0.
- Proposals, gateway acceptances, fills, complete closes, and orders: 0.
- Production risk settings changed: no; Live/TestNet not used.
- V39 remote idempotency and duplicate-cost semantics remain `BLOCKED_WITH_EVIDENCE`.
- V40 remains offline simulation only; private Gate settlement, account-specific fee/rebate, real margin, and historical quotes remain unverified.
- V41 local test census is not GitHub CI or remote provider proof; PR review/CI remains separate.
- V42 controlled research and shadow operation are not started. The 30-close minimum and shadow observation window are unmet.
- The multi-asset terminal, instrument capability registry, fee/rebate settlement reconciliation, `TraderAdapter`, unified point-in-time intelligence store, governed memory promotion, and professional terminal UI remain roadmap/design work, not implemented production behavior.

A local orchestrator capability command was interrupted after inspection showed that `--check-models` launches real inference. The first configured probe was `gpt-5.6-sol`; no response or usage receipt was returned, so whether a request reached the service or consumed quota is **unverified**. No Gemini request was issued. This is recorded as an operational incident, separate from the V38 Gemini research count.

## Acceptance boundary

Gate 2 is accepted for **primary data construction, partition isolation, and evaluation design only**, with the evidence-grade caveats above. It is not accepted for market-decision quality, model reliability, executable trading, strategy efficacy, or PnL. A later controlled Gemini run requires separate exact-model, budget, sample, and stopping-rule authorization and must first clear the V39 provider idempotency/duplicate-cost boundary. Untouched-test reveal, Gate access, Live mode, and orders remain unauthorized.

## Final local verification — 2026-10-09

- `tests/v38/`: **68 passed**; Ruff and `compileall` passed for the touched Python files.
- Evidence-recorded full suite: **2,399 passed, 1 skipped, 0 failed** in 279.01 seconds; one Starlette/httpx deprecation warning remains.
- Exact comparison with frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: `PASS_NO_NEW_FAILURES`; all 100 baseline failure nodes/phases resolved in the current suite, all 76 added nodes passed, with 0 new failures, 0 missing baseline nodes, and 0 phase changes.
- Local ignored evidence files: `reports/v38+/verification/v38-gate2-v3-registry-audit-final-20261009.json`, `...-vs-baseline.json`, `v38-stratified-purged-integrity-20261009-v4.json`, and `v38-gate2-v3-final-acceptance-v4-20261009.json`. The independent data audit remains `PASS_WITH_EVIDENCE_CAVEATS` with 54 visible inputs and blind-label registry bindings recomputed against the manifest, 18 sealed contexts, and 0 overlap pairs.
- These are local tests, not remote CI, provider identity, market-performance, or execution evidence. No model research, exchange call, order, TestNet, Live, or production-risk change was made. The interrupted Codex capability probe's provider/billing status remains unverified as recorded above.
