# Multi-Asset Trader Terminal — Architecture and Delivery Roadmap

Status: **long-term roadmap** supplementary to V38–V42. Current V38–V42 research-quality, risk, model-reliability, sample-out-of-sample, and test-governance gates retain priority. This roadmap is design only; it does not authorize production execution or paid provider calls.

Historical placement snapshot (2026-10-09, after the V38.4 label-agreement tooling; superseded by the current placement addendum at the end): Product Contract V1.0 and this dependency/reuse/interface plan are design-only supplements; R0 remains first. The V38 V3 data/schema audit was freshly reproduced byte-for-byte: 54 causal inputs, 18 sealed hashes, 50 archive files, and zero overlaps verified. This is data/evaluation preparation only: historical availability is still an `ASSUMED_PROXY`, independent annotations and Gemini-scored observations are zero, and there are no executable samples. V38.3 blind-label handoff and V38.4 agreement metrics are tooling-ready, but no human label batch or real agreement metric exists. `tests/v38/` passes 81 tests. V39 has 154 focused offline transport tests; response completion requires both `finish_reason="stop"` and `[DONE]`, while provider compatibility, remote idempotency, replay semantics, and duplicate-cost handling remain unverified. V40 has 97 focused offline simulation/economics tests, including duplicate finalized-bar delivery after checkpoint restore. At the current V38.4 code tip, the local full suite reports 2,432 passed, 1 skipped, and 0 failed; the exact frozen-baseline comparison reports `PASS_NO_NEW_FAILURES`. GitHub Actions run 37879468194 for PR #14 also passed. These local and CI results do not verify remote provider, exchange, or production behavior. Product implementation R1 has not started: there is no new instrument registry, account-fee migration, multi-asset order route, or terminal UI. PRs #13 and #14 remain open and unmerged. No provider or exchange calls, Live/TestNet actions, production setting changes, or orders are authorized. See [`V38-gate2-independent-review-followup.md`](../audits/V38-gate2-independent-review-followup.md), [`V38-blind-label-handoff-tooling.md`](../audits/V38-blind-label-handoff-tooling.md), [`V38-blind-label-agreement-metrics.md`](../audits/V38-blind-label-agreement-metrics.md), [`V39-progressive-consult-stream-followup.md`](../audits/V39-progressive-consult-stream-followup.md), and [`V39-offline-fault-injection-followup.md`](../audits/V39-offline-fault-injection-followup.md) for evidence and limits.

## 1. Delivery order and dependencies

| Stage | Scope | Exit evidence / dependency |
| --- | --- | --- |
| R0 — V38–V42 research readiness | Complete the current frozen-data, causal-input, reliability, simulation-economics, legacy-test, and controlled-research gates. | Exact baseline/no-regression evidence; data and schema review; provider spend/stop policy separately authorized before any model use. This stage is active and must not be displaced by terminal work. |
| R1 — Product and instrument contract | Versioned `InstrumentSpec`, product capability matrix, metadata provenance/effective intervals, jurisdiction gate, and adapter/API contracts. Begin with read-only metadata and fixtures. | Contract/schema review and fail-closed tests for unknown/stale rules. No automatic execution capability inferred from the schema. |
| R2 — Gate fee and account economics | Account/instrument fee schedules, maker/taker evidence, rebate eligibility and settlement reconciliation, account equity/exposure/margin view. Preserve current production notional. | Reconcile actual Gate settlement evidence in a separately authorized environment; verify pre-/post-rebate accounting; uncertainty blocks eligibility. Insufficient verified equity/free margin, reserved exposure, or correlated-risk capacity rejects the unchanged proposal; no automatic resizing or expected rebate credit. No credentials or private calls in current phase. |
| R3 — TraderAdapter and MarketIntelligence | Provider-neutral trader profiles and typed decisions; response identity evidence; source and point-in-time intelligence contracts, revision and as-of queries. | Offline contract tests; provider failures and invalid identity/schema abstain; no production model path switched by this roadmap. |
| R4 — Memory and validation governance | Append-only fact/decision/fill/close/review/hypothesis/validated-experience/approved-version lineage and preregistered OOS promotion gates. | Rebuildable lineage and partition-leakage tests; no automatic promotion; sealed test remains unused until final evaluation. Depends on R3 evidence and R0 partition acceptance. |
| R5 — Portfolio risk and multi-asset simulation | Separate per-product execution policies; total and correlated exposure, liquidity, sessions, events, funding/overnight, mark/index divergence, margin/liquidation, and fee stresses. Paper/replay before shadow. | Instrument-by-instrument capability certification and stable reconciled simulation; zero unknowns allowed for an executable route. Depends on R1 and R2. |
| R6 — Terminal consolidation | Incrementally compose existing React/API surfaces into the professional operator terminal; expose modes, trader/evidence, risk, positions/orders/protection, reviews, performance, logs, versions, and emergency controls. | Operator usability and fault-state review; UI reflects backend authority; emergency stop/circuit breaker integration tested in non-live environments. Depends on stable R1–R5 contracts. |
| R7 — Shadow and separately authorized release | Start only after R0–R6 project acceptance. Observe live market data and record proposals without orders, then separately consider manual Live authorization for certified products/accounts. | Shadow reliability, account/region/product/legal review, security and operational sign-off, explicit per-scope human authorization and tested stop/rollback. Current task grants none of these permissions. |

Stages may have parallel design work, but implementation gates above remain ordered: research integrity first; metadata before economics; economics and capabilities before executable multi-asset risk; validated evidence before memory promotion; stable backend contracts before UI consolidation; shadow before any Live release.

Before the project is accepted, every automated test and exchange-integration check must use offline simulation or Gate TestNet. Live credentials, endpoints, and orders are excluded from testing. Live connectivity begins only after R0–R6 acceptance and a separate explicit authorization; it is not part of TestNet acceptance.

## 2. Existing module reuse and boundary

| Existing code | Reuse | Remaining boundary |
| --- | --- | --- |
| `core/instruments.py` — `Instrument`, canonical IDs, trading hours, contract size/tick/step, contract type, price type | Extend its identity concepts and use canonical IDs. | Current `Instrument` is a compact registry model; it is not yet a complete versioned `InstrumentSpec` containing sessions/holidays, margin/funding/liquidation, provenance/effective dates, jurisdiction, and individually verified execution capabilities. |
| `core/trading/entry_economics.py`, `trade_feasibility.py`, `risk_engine.py` | Reuse the V34/V35 net economics, Decimal sizing, stop-risk, fee, slippage, and feasibility foundations. | Keep current fixed 2,000 USDT production behavior unchanged. Any research-only risk-budgeted sizing stays isolated. Extend only through reviewed common economics contracts and parity tests. |
| `core/trading/execution_gateway.py`, `trader_capabilities.py`, `gate_live_client.py`, `gate_accounts.py` | Reuse order intent, independent gateway, capability/permission state, account scopes, protection plans, Gate interfaces, and e-stop route. | Add per-instrument capability certification, explicit jurisdiction checks, portfolio-wide cross-asset risk, and clear permission-state surfaces only after separate acceptance. Existing Gate/crypto support does not certify other products. |
| `core/trading/gate_trade_settlement.py`, `gate_account_truth.py`, `ledger.py` | Reuse remote trade evidence, settlement episodes, fee currency/source, account ledger, and reconciliation concepts. | No verified dealer-discount/60% rebate contract is established by these fields alone. Add account/instrument entitlement and actual rebate settlement evidence; preserve pre-rebate net and final post-confirmed-rebate net. |
| `core/ai/contracts.py`, `core/ai/validator.py` (`ModelProvider`), `core/ai/ollama.py`, `core/model_client.py`, `core/model_routing.py` | Reuse typed signal contract, provider boundary, validation, transport, and current model routing. | `ModelProvider` is not a full `TraderAdapter`: stable trader profiles, product scope, philosophy/version, broad action schema, verified response identity, memory lineage, and risk-authorization class remain to design. |
| `core/market_intelligence.py`, `core/macro_calendar.py`, `core/news_engine.py`, `core/news_revision.py`, `core/providers/news.py` | Reuse stored evidence, revisions, news metadata, calendar, freshness and as-of read patterns. | Normalize all intelligence sources to a single published/available/received-time and asset-link contract. Some timestamps are absent or proxy-only; historical news gaps must remain unknown. |
| `core/trading/decision_memory.py`, `ai_decision_memory` schema, `core/memory.py` | Reuse durable model decision rows, outcome reconciliation and prompt retrieval safeguards. | Expand into explicit immutable event classes, hypothesis/OOS/approved-version states, partition lineage and governance. Current lesson/outcome columns do not prove a validated experience or authorize promotion. |
| `core/replay/ai_simulation.py`, `ai_template_runner.py`, `gemini_research.py`, V35–V38 replay modules | Reuse replay/simulation and frozen research patterns where their evidence contracts fit. | Preserve source lineage and partition boundaries; implement multi-product fills, margin, sessions and portfolio risk before cross-asset performance claims. |
| `apps/api/main.py`, `apps/api/v2.py`, `apps/api/v3.py` | Extend existing account-scoped API, risk summary, decision, research-run, timeline and emergency-stop surfaces. | Avoid duplicating API frameworks. Define stable contracts, lifecycle events and operator authorization before adding terminal controls. |
| `web/src/App.tsx`, `V2WorkspacePage`, `AITraderPanel`, dashboard, market/news, watchlist, replay, paper-trades, performance, monitoring and settings pages | Compose and incrementally redesign existing React pages/components. | Current UI is not yet a unified multi-asset terminal; it needs consistent product identity, trader/version/evidence views, portfolio risk, permissions, lifecycle, circuit-breaker and emergency-stop visibility. |

## 3. Missing modules and proposed interfaces

Names below are proposed contracts, not claims that code already exists.

### 3.1 Product registry and execution certification

- `InstrumentSpec`: versioned immutable product identity and rules as defined in the Product Contract.
- `InstrumentRegistry.get(instrument_id, as_of) -> InstrumentSpec | Unknown`: retrieves exact effective metadata; never guesses product type from a ticker.
- `ExecutionCapabilityCertificate`: instrument, venue, account/jurisdiction scope, supported operations, verified constraints, evidence, observer, validity interval, and status.
- `CapabilityRegistry.evaluate(spec, account, operation, as_of) -> CapabilityDecision`: `ALLOW` only when every mandatory rule is known, current, and eligible; otherwise return explicit `DENY` codes.
- Metadata sources must be independently versioned and auditable. Public metadata and account entitlement are distinct evidence classes.

### 3.2 Fees and account-level risk

- `FeeSchedule` / `FeeScheduleProvider`: account + venue + product + instrument + maker/taker + effective time, with source and confidence/verification state.
- `FeeSettlementReconciler`: compares estimate, native fills, closing/settlement records, and rebate credit. It emits immutable `PRE_REBATE_NET`, `CONFIRMED_REBATE`, and `POST_REBATE_NET` records; pending rebate cannot affect eligible equity.
- `PortfolioRiskSnapshot`: equity, available margin, open and pending notional, stop-loss-at-risk, concentration, correlated groups, venue/product exposure, funding/overnight, and data age.
- `PortfolioRiskService.evaluate(proposal, instrument_spec, fee_snapshot, portfolio_snapshot, policy_version)`: returns accept/reject plus full calculation trace and explicit reason codes. Unknown or insufficient equity/free margin, reserved exposure, fees, instrument capability, or portfolio/correlation budget rejects the unchanged proposal. Size adjustment is not implicit; proposal changes require an explicit new decision.
- Keep existing 2,000 USDT target fixed in production until a distinct approved change. Risk-budgeted max-notional calculation remains research-only until then.

### 3.3 Trader and intelligence boundaries

- `TraderProfile`: trader ID; provider/model; verified identity state; model, philosophy, strategy, prompt and schema versions; product scope; memory scope; risk-authority class.
- `TraderAdapter.research(context) -> ResearchResult` and `TraderAdapter.decide(context, allowed_actions) -> TraderDecision`: typed content plus provider call metadata, returned-model identity evidence, timestamps, evidence refs and version hashes. Adapter cannot authorize or submit orders.
- `MarketIntelligenceItem`: source, published/available/received times, revision, affected instrument IDs, verification/availability status, payload hash and provenance.
- `MarketIntelligenceStore.query_as_of(instrument_ids, decision_time)`: returns only evidence known by the cutoff and explicit unknown/missing reasons. No fabricated historical coverage.

### 3.4 Memory, audit, and terminal APIs

- `TraderMemoryEvent`: typed append-only fact, decision, accepted intent, fill, close/settlement, review, hypothesis, validated experience, or approved strategy version, linked to evidence and data partitions.
- `HypothesisGate`: preregistration, allowed partitions, outcome definition, minimum sample/effect/uncertainty rules, reviewer/approval and immutable promotion record.
- `DecisionLifecycleEvent`: correlates candidate through proposal, validation, risk decision, order, exchange acknowledgement, fills, protection, close and settlement without collapsing their states.
- API responses should expose authoritative `mode`, permission state, account/venue, instrument capability, risk result/reason codes, data freshness, and event version. Commands revalidate these server-side at time of use.
- `OperatorControlService`: authenticated, audited circuit breaker/emergency stop and recovery protocol, separate from model adapters and normal strategy permissions.

## 4. Cross-asset policy matrix

Each instrument family gets a separate execution and simulation policy. A common AI research format does not imply shared order rules.

| Risk surface | Required product-specific evidence/control |
| --- | --- |
| Trading time | Session calendar, timezone, holidays, pre/post-market eligibility, halts, stale/closed-market detection. |
| Corporate events | Earnings/dividends/splits for shares and tokenized equities; no execution across a halt or unsupported corporate action. |
| Price basis | Last/bid/ask/mark/index/official-close semantics, divergence thresholds, fallback rejection policy. |
| Liquidity | Spread/depth/participation cap, size-aware slippage stress, and market impact. |
| Carry costs | Funding, borrow, overnight, roll and settlement currency; actual vs estimated evidence. |
| Contract/margin | Multiplier, min size, tick/step, leverage limits, initial/maintenance margin, liquidation mechanics and protection support. |
| Portfolio | Gross/net, per-asset and per-venue concentration, correlation stress, aggregated stop risk, pending-order reservation and available margin. |
| Eligibility | Product/venue/account and jurisdiction certification plus expiry/revocation check at execution time. |

## 5. Terminal information architecture

The unified workbench should progressively surface:

1. Market chart, timeframe/price basis, asset and product filter.
2. AI trader roster, model identity and strategy/schema versions.
3. News, macro/calendar, funding, and source/availability evidence.
4. Current thesis, alternatives, invalidation, horizon, and linked evidence.
5. Independent risk decision, account state, portfolio exposures, and capability/eligibility status.
6. Positions, orders, fills, protection, and exact lifecycle status.
7. Trade review, pre-/post-rebate performance, drawdown and benchmark context.
8. Operational/system logs, provider/venue health, circuit breaker and emergency stop.
9. Memory, open hypotheses, OOS results, and approved strategy versions.

Mode labels and permissions remain persistent and unmistakable. A disabled control explains the blocking reason. A proposal is never rendered as an order or fill. E-stop confirmation and recovery are distinct actions and are audited.

## 6. Definition of terminal readiness

Readiness requires independent evidence across five axes:

- **Research reliability:** causal inputs, sealed partitions, source hashes, correct identity/evidence validation, reproducible methods and explicit unknowns.
- **Risk control:** fail-closed sizing and capability gates, portfolio stress, cost/settlement reconciliation, stale-state rejection, independently tested circuit breaker and stop.
- **Simulation stability:** restart/reconciliation, partial fills, protection, venue/product costs, margin and failure-state tests; no synthetic fill presented as real.
- **Operator usability:** reviewed end-to-end flows for research, simulation, shadow, risk rejection, permissions, outage, stop and recovery.
- **Version traceability:** trader/model identity, prompt/schema, data/evidence, policy, risk result, order/fill/close, review and promotion all linked immutably.

Green tests or positive PnL alone do not satisfy readiness. Every venue/product/jurisdiction needs its own capability sign-off. Any Live release also requires separate explicit human authorization and a tested rollback/stop plan.

## Current V38–V42 placement — 2026-10-09

The product contract and this roadmap are the design response to the requested V38–V42 supplement. The active Gate 2 data-integrity P0 is complete locally before product implementation expands: frozen source archive bytes/sidecars were independently rehashed, and an independent standard-library auditor reconstructed all 54 visible V3 market inputs, matching every frozen hash and evidence-reference count. A late duplicate-row availability metadata leak was fixed without changing those descriptors. PR #18 carries this work; GitHub Actions run `37906365567` passed the hosted quick gates and complete offline suite. PR #18 remains open and unmerged, with auto-merge disabled. These checks do not enable model evaluation or execution; availability is still proxy-grade, labels and Gemini decisions are zero, and Gate-executable samples are zero. V38–V42 research-quality, risk, reliability, out-of-sample, and test-governance gates remain first. Product implementation begins only after those gates are reviewed and follows the dependency order above. Current production notional remains 2,000 USDT, Live remains disabled, and no model/provider or exchange authorization is implied.
