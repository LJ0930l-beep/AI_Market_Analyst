# AI Market Analyst V1.0 Master Spec

Source of product intent: `C:\Users\baicha\Downloads\AI_Market_Analyst_总开发与Sol-Luna自主执行规格书_v2.0.docx`.

This file is the concise execution contract for the repository. If it conflicts with verified repository evidence, record the conflict in `DECISIONS.md`; never silently rewrite history or discard user data.

## Product objective

Deliver a local-first US-stock and crypto market research application that:

- obtains public market/news/event data with explicit freshness and capability flags;
- computes deterministic quant, price, time and risk rules in Python;
- uses local Ollama `qwen3.5:4b` for interpretation and synthesis only;
- emits auditable LONG / SHORT / WAIT proposals with Entry, Stop, TP, validity, holding horizon, re-evaluation, invalidation and confidence;
- persists every Prediction independently of whether the user follows it;
- creates PaperTrade only when the user follows;
- settles Outcomes from later real market data and reports honest performance and calibration;
- exposes the workflow through a local FastAPI backend and React + TypeScript UI.

## Non-negotiable boundaries

- No real order execution, Broker private APIs, exchange secrets, account or fund management.
- WAIT is a normal outcome. It is included in coverage and excluded from wins/losses.
- Raw confidence is immutable; calibrated confidence is stored separately.
- No future bars/news or post-hoc revisions in model input for replay.
- No deletion of losses, timeouts or ignored Predictions to improve reported performance.
- No LLM-computed indicators, R:R, price rules or time rules.
- No LLM self-modification, prompt/code/threshold editing, fine-tuning, LoRA, RLHF or online training.
- Local Qwen is the only required AI dependency; cloud models must not be required at runtime.
- No model arena, multi-agent product runtime, production ML calibration model, Forex/ETF/Index expansion or external notification channels in V1.0.
- Preserve Phase 0-3 behavior and data; avoid broad framework-driven refactors.

## Development operating model

- Project supervisor: Sol. Owns planning, bounded research, task packs, verification, review, Gate decisions and final acceptance.
- Sole developer/fixer: `luna-max`. All code implementation and repair is delegated to one persistent Luna thread.
- Loop: `PLAN -> BUILD -> VERIFY -> REVIEW -> ACCEPT/REPAIR -> NEXT`.
- One Luna task at a time in the shared working tree.
- Each task pack must define objective, dependency, read-first files, allowed paths, forbidden paths, implementation constraints, acceptance commands and Definition of Done.
- A failed task is repaired in the same Luna thread with concrete evidence. Two failures with the same root cause trigger replanning.
- Each accepted task produces a small identifiable commit or checkpoint.

## Verified baseline

- Phase 0: PASS; external `stock_signal_analyzer` was architecture reference only, with no unlicensed source copied.
- Phase 1: PASS; instrument/provider/quant/signal/prediction/paper-trade/outcome/SQLite boundaries exist.
- Phase 2: PASS; public providers, news, structured context, local Qwen, validator and paper/outcome flow exist.
- Phase 3: PASS with recorded model-output errors; 300 replay samples and 300 Predictions are present, 191 actionable Predictions are resolved, calibration is ACTIVE, and final sample errors are retained as 45 `repair_failed` records.
- Current regression baseline: 41 tests PASS and `compileall` PASS on 2026-08-15.

## Phase gates

### Phase 4 - Product UI and interaction

Deliver a React + TypeScript local web UI over the existing FastAPI backend:

- Dashboard / Market Radar shell, Watchlist, Asset Detail, Predictions, Paper Trades, Performance, Replay Lab (advanced), Settings / Health.
- Explicit stale/model-unavailable/error states.
- Signal Card separates raw and calibrated confidence and displays PRELIMINARY when applicable.
- Signal validity, holding horizon, re-evaluation and invalidation are visible.
- Follow creates only PaperTrade; no order language.
- Backend remains the owner of business rules.
- Gate: frontend build/tests, API contract tests and user-flow E2E PASS.

### Phase 5 - Radar, watchlist, scanning and alerts

- Local Watchlist persistence and public-provider-compatible symbol expansion.
- Auditable, versioned Opportunity Score using available quality inputs.
- Configurable, session-aware, low-concurrency scheduler; no Redis/Celery dependency.
- Background outcome settlement, performance refresh and watchlist scanning.
- Local Alert Center with deduplication, acknowledge state and no execution authority.
- Long tasks expose progress, cancel/resume and errors.
- Gate: stable scans, auditable ranking, deduplicated alerts, restart recovery and resource-limit tests PASS.

### Phase 6 - Context and intelligence quality

- Programmatic equity/crypto Benchmark Context with capability fallback.
- Earnings/known-company and macro-event interfaces; point-in-time limits remain explicit.
- Multi-source event clustering, source credibility and primary-source distinction.
- TimePolicy interaction for major events.
- Deterministic, leakage-safe Market Memory over saved Prediction/Outcome/Quant/Event features.
- Gate: context quality, event reliability, capability fallback and no-leakage similarity tests PASS.

### Phase 7 - Hardening and release

- Full build, lint, typecheck, unit, integration, replay leakage, E2E, resilience and release smoke suites.
- SQLite backup/restore and restart persistence verification.
- One-command local backend + frontend startup and clean shutdown.
- User and developer documentation, dependency/license inventory and Phase 3-7 completion reports.
- Gate: fresh-install smoke, backup/restore, full regression and release audit PASS.

## Final acceptance

`FINAL ACCEPTED` is allowed only when:

- Phase 0-7 Gates are PASS;
- the six current stock/crypto symbols run stably and Watchlist can expand;
- LONG/SHORT/WAIT, Prediction/PaperTrade/Outcome and time/risk fields work end-to-end;
- replay/performance/calibration report sample limits honestly;
- Radar, Watchlist, Alert Center, benchmark/event/news quality and Market Memory are usable with capability fallback;
- local Qwen is sufficient and no cloud token is required;
- no Broker, real trading, secret or fund-management capability exists;
- restart persistence, SQLite backup/restore and one-command startup are verified;
- full tests, E2E and release smoke PASS;
- final report lists limitations and makes no profit promise.

## Stop conditions requiring user direction

Stop only for a required violation of the no-trading/no-secret boundary, irreversible destructive migration or broad rewrite, unresolved legal/license/data-source issue, unrecoverable workspace permission failure, irreconcilable test/spec conflict, or evidence that a prior accepted core phase did not actually exist.
