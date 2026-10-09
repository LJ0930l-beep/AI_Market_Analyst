# AI Market Analyst — Product Contract V1.0

Status: **product contract and long-term design baseline**. This document supplements the V38–V42 research plan. It does not authorize production changes, additional paid model calls, exchange access, Live mode, or orders.

## 1. Product identity

AI Market Analyst is a **Multi-Asset Autonomous AI Trading Terminal**. It is a professional terminal where a model can independently research markets and news, form and explain a view, compare opportunities across markets, and propose opening, waiting, reducing, closing, and protection-management decisions. The product is not limited to a fixed-condition BUY/SELL signal generator or a backtest interface.

The AI owns market judgment and may propose novel ideas. It does not own account permissions, product eligibility, risk limits, execution authorization, or the ability to enable Live mode. A deterministic, independently versioned control plane validates every actionable proposal before any execution adapter can receive it. Invalid, stale, unsupported, unauthorized, or over-risk proposals fail closed.

## 2. Operating modes and state transitions

The product exposes distinct, auditable modes:

1. **Research** — point-in-time data analysis; no executable order intent.
2. **Replay** — causal historical analysis using only data available at the simulated decision time.
3. **Simulation** — simulated orders, fills, costs, margin, and settlement.
4. **Shadow** — current market observation and recorded proposals without exchange submission.
5. **Manually authorized Live** — available only after separate account-, venue-, region-, product-, and operator-level checks and explicit authorization.

Live is disabled by default. A model cannot change operating mode, permissions, credentials, risk limits, or emergency-stop state. The UI must show the effective account, venue, mode, permission state, capability status, and any lock reason. A global emergency stop and independent trading circuit breaker must be available and their state must be durable and visible.

## 3. Instrument identity and capability contract (P0)

`InstrumentSpec` is the canonical identity and rule snapshot for an executable product. Two products with the same underlying remain separate instruments: for example, spot BTC, a Gate BTC perpetual, a CFD on BTC, and a tokenized equity are not interchangeable.

The versioned specification must contain at least:

- Stable instrument ID; venue/exchange; product type; native symbol; underlying; base, quote, and settlement currencies.
- Trading session, timezone, holidays, scheduled halts, and relevant earnings/event calendar requirements.
- Contract multiplier/size; minimum and maximum order quantity/notional; quantity step; price tick; price precision; supported order and protection types.
- Margin mode and limits; leverage caps; maintenance margin/liquidation rules; funding, borrow, and overnight costs where applicable.
- Price-source semantics (last, bid/ask, mark, index, or official close) and any material divergence guard.
- Market-data, research, order, cancel, reduce-only, protection, and settlement API capabilities, each with source, status, observation time, and expiry.
- Jurisdiction eligibility and its authority/source, account scope, verification time, and expiry.
- Provenance, metadata version, effective interval, validation status, and the version/hash used for each decision.

Missing, stale, contradictory, unverified, or unsupported rules mean **not eligible for automatic execution**. Capability is certified independently per venue and product; a shared research schema does not imply shared execution behavior.

## 4. Fees, settlement, and small-account controls (P0)

Fee schedules are account-, venue-, instrument-, and maker/taker-specific, time-bounded, and provenance-backed. Gate is the first priority integration. The dealer's stated 60% discount is an entitlement to verify, not a default rate assumption: eligibility, product coverage, effective time, calculation base, and actual settlement must each be evidenced. It must not be presumed to be immediately deducted.

For each closed trade, preserve separate auditable amounts for:

- Gross realized PnL before fees and funding.
- Actual fees and funding/overnight costs, including currency and source evidence.
- **Actual net PnL before rebate**.
- Rebate/discount amount that was eligible and actually settled, with settlement evidence and time.
- **Final net PnL after confirmed rebate**.

An expected, pending, estimated, or unverified rebate is excluded from available risk capital and may never make an otherwise rejected order pass. Unknown fee treatment is a fail-closed state for automatic execution.

The existing **2,000 USDT fixed notional remains the production behavior** until a separate, explicit production-change approval. The independent research sizing design may calculate a risk-budgeted maximum notional, but must not silently shrink a model proposal to manufacture a pass. Small-account controls must additionally bound per-trade stop loss, total gross/net exposure, concentration, correlated exposure, margin use, liquidation distance, and fees/slippage. Dynamic leverage may support margin efficiency but can never reduce measured stop risk or bypass a risk limit.

Reject an unchanged proposal when verified equity or free margin cannot cover the required margin, existing and reserved exposure, conservative fees/slippage, and policy buffer, or when a portfolio/concentration/correlation limit would be exceeded. The rejection records explicit reason codes and the exact inputs used. Neither the risk service nor an execution adapter may automatically resize the notional, alter leverage, or rely on an expected rebate to turn that proposal into a pass; a revised size requires a new model decision and a fresh risk review.

## 5. Independent portfolio and execution risk

Before execution, the risk plane evaluates the exact product rules and the account's latest verified state. It must account for trading sessions and holidays; earnings and halts; bid/ask liquidity and conservative slippage; last/mark/index divergence; funding and overnight charges; contract restrictions and liquidation; existing positions and pending orders; concentration and cross-asset correlation; total account risk; and jurisdiction/product eligibility.

The risk decision is an immutable, versioned record containing the proposal hash, account/instrument snapshots and their ages, applicable fee and cost assumptions, quantized order parameters, calculated loss/margin/exposure, policy version, decision time, accept/reject result, and explicit reason codes. A quote, proposal, accepted intent, venue acknowledgement, partial/full fill, protection state, and complete close are distinct lifecycle facts; none may be inferred from another.

## 6. Model-neutral trader interface (P1)

Define a versioned `TraderAdapter` boundary independent of any model vendor. Every configured trader has an immutable `trader_id`, provider/model and verified response identity, model version, philosophy and strategy version, asset/product scope, decision schema version, research records, memory scope, performance lineage, and risk-authorization class.

The adapter returns typed research/decision content plus request metadata and response-identity evidence. Requested model ID is not proof of the responding model's identity. Identity without provider evidence is recorded as `UNVERIFIED`. All providers pass through the same schema validation, causal-evidence checks, product-capability checks, and independent risk review. Timeout, provider error, malformed output, identity mismatch where identity is required, or schema/evidence rejection produces an abstention and no order intent.

Decision vocabulary must express `OPEN`, `WAIT`, `REDUCE`, `CLOSE`, and protection/position-management actions, with thesis, evidence references, invalidation, horizon, and uncertainty. Execution and protection parameters are still validated and authorized outside the model.

## 7. Market intelligence and point-in-time evidence (P1)

`MarketIntelligence` is a versioned evidence contract for market data, macro releases, news, earnings, economic calendars, funding, and other relevant data. Every item records source/publisher, source ID, publication time, first observed/available time, ingestion/received time when known, revision history, affected instrument IDs/assets, confidence/verification status, and content hash.

Historical decisions may use only evidence whose actual available time is no later than the decision time. Backtests must use as-of joins and retain the exact evidence IDs/versions. If actual availability cannot be verified, label it as a proxy and exclude it from claims requiring precise point-in-time validity. Missing historical news remains `UNKNOWN`; no synthetic or backdated news may be inserted to fill gaps. Corrections create revisions and never rewrite the evidence a prior decision saw.

## 8. Trader memory and governed learning (P1)

Durable memory separates: (1) observed facts, (2) trader decisions, (3) accepted intents and actual fills, (4) settlement/close outcomes, (5) post-trade reviews, (6) hypotheses awaiting validation, (7) experience that passed preregistered out-of-sample validation, and (8) strategy versions explicitly approved for use.

Every item links to its source evidence, trader/model identity, instrument, decision and outcome timestamps, data partition, policy/schema/prompt version, and parent hypothesis or strategy version. Reviews and model-generated lessons remain proposals. Promotion requires a preregistered validation protocol, untouched-test isolation, minimum evidence and uncertainty reporting, independent review, and an explicit approval record. Once a final test set has influenced a hypothesis or decision, it cannot continue to be represented as untouched.

## 9. Lifecycle ledger and traceability

For each candidate, persist the sequence and status of: research observation, model decision/proposal, schema acceptance/rejection, risk acceptance/rejection, order intent, gateway acceptance/rejection, venue acknowledgement, each fill, protection changes, cancel/replace, and complete close/settlement. Keep provenance and hashes so a reviewer can reconstruct what the model knew, proposed, what the independent risk plane permitted, what the venue did, and how PnL was reconciled.

Performance reports distinguish proposal, risk-accepted candidate, submitted order, fill, and complete closed trade. Counterfactual feasibility and simulation results never count as real fills or realized production performance.

## 10. Professional terminal contract (P2)

The terminal progressively unifies market charts, asset/product filtering, trader management, news/intelligence, current AI thesis and plan, independent risk/capability state, positions/orders/protection, trade review, returns/drawdown, operational logs, and memory/strategy version history. It supports all modes in Section 2 and makes their boundaries clear.

Critical controls and status—Live default-off, account permissions, product eligibility, risk lock, circuit breaker, emergency stop, stale data, and venue connectivity—must be explicit, readable, and usable without interpreting model prose. UI actions cannot bypass backend authorization. The UI is an operator surface over authoritative backend state, not the risk authority itself.

## 11. Product acceptance

Acceptance requires a combined evidence package, not only profitable replay results or green tests:

- Causal, partitioned, reproducible research with immutable source/input/prompt/schema hashes and disclosed uncertainty.
- Independent risk and execution-capability checks that fail closed under missing/stale/contradictory data, malformed proposals, fee/rebate uncertainty, and portfolio stress.
- Stable simulation and shadow operation across failure/restart/reconciliation scenarios; simulated and real venue facts are never conflated.
- Operator usability review for the terminal, permissions, circuit breaker, emergency stop, and lifecycle visibility.
- End-to-end lineage from trader identity and evidence to strategy version, risk decision, order/fill, settlement, review, and any approved promotion.
- Separate venue/product/jurisdiction certification and explicit human authorization before any Live release.

No item in this product contract constitutes authorization to contact an exchange, submit an order, enable Live, call a paid model, or merge a PR.
