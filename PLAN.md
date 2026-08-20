# AI Market Analyst V1.0 Execution Plan

## Current phase

Phase 7 - Security/release hardening, backup/restore, one-command startup and V1.0 Final Acceptance — SUPERVISOR_ACCEPTED.

Current task: Post-V1.0 `Qwen Consult` streaming consultation milestone — DEVELOPER_COMPLETE / supervisor Gate pending; V1.0 Final Acceptance remains SUPERVISOR_ACCEPTED / PASS and historical.

P4-T04 execution split:

- `P4-T04a` read-only market snapshot API - ACCEPTED: real OHLCV/quote/quant/provider provenance with zero Prediction side effects.
- `P4-T04b` Asset Detail frontend - ACCEPTED: instrument/timeframe controls, truthful charts/news/quant, explicit Analyze and Signal Card workflow.

## Phase 4 task graph

1. `P4-T01` Backend product API contract - ACCEPTED
   - Structured response envelopes/errors, CORS for local UI, missing read/detail/filter endpoints and API contract tests.
   - No frontend work and no core financial-rule changes.
2. `P4-T02` React + TypeScript application foundation - ACCEPTED
   - Frontend workspace, routing, design tokens, typed API client, local dev proxy, build/lint/typecheck/test commands.
3. `P4-T03` Dashboard, Settings/Health and shared status components - ACCEPTED
   - Provider/model/database health, market summary shell, loading/error/stale/degraded states.
4. `P4-T04` Asset Detail and analysis workflow - ACCEPTED
   - Instrument search, quote/quant/news/event views, charts, Signal Card and validity/horizon/re-evaluation/invalidation display.
5. `P4-T05` Predictions, Follow/Paper Trades and Performance - ACCEPTED
   - Filters/details, PaperTrade-only Follow semantics, outcome metrics, calibration reliability/buckets and PRELIMINARY state.
6. `P4-T06` Watchlist and Replay Lab UI - ACCEPTED
   - Phase 4 UI contract/shell for Watchlist and advanced replay visibility; Phase 5 owns live scanning/ranking behavior.
7. `P4-T07` Phase 4 E2E, accessibility/responsive pass and completion report - ACCEPTED
   - Main workflow E2E, regression, build artifacts and Phase 4 Gate.

## Phase 5 task graph

1. `P5-T01` Watchlist/AppSetting foundation and public symbol registration - ACCEPTED.
   - `P5-T01a` durable canonical membership, safe settings, migration and CRUD - ACCEPTED.
   - `P5-T01b` public-provider-compatible symbol expansion - ACCEPTED.
2. Versioned auditable Opportunity Score and Radar API - ACCEPTED.
3. Low-concurrency scheduler, market-session policy, caching, resource backoff, background settlement and live refresh - ACCEPTED.
   - `P5-T03a` safe local Watchlist scheduler and scan execution foundation - ACCEPTED.
   - `P5-T03b` background Outcome settlement + performance/Radar refresh - ACCEPTED.
4. `P5-T04` local alert model, deduplication, acknowledge flow and UI integration - ACCEPTED.
5. Restart/recovery, performance/resource tests, E2E and Phase 5 Gate evidence - ACCEPTED.

## Phase 6 task graph

1. Benchmark metadata/providers and deterministic Benchmark Context - COMPLETE (developer-verified).
2. Event schema/provider interfaces and major-event TimePolicy effects - COMPLETE (developer-verified).
3. Multi-source event clustering and credibility/primary-source model - COMPLETE (developer-verified).
4. Leakage-safe deterministic Market Memory and API/UI integration - COMPLETE (developer-verified).
5. Capability fallback, leakage tests, E2E and Phase 6 report - ACCEPTED by supervisor.

Phase 6 Gate: ACCEPTED by supervisor on independent final evidence: 106 pytest tests plus 10 subtests, 55 frontend tests, lint/typecheck/build/compileall/pip check PASS, both npm audits 0, standalone E2E 10/10 in 52.6s with 18 desktop/mobile axe+overflow checks, and clean diff check.

## Phase 7 task graph

1. Dependency/license/security/privacy audit and configuration hardening - ACCEPTED.
2. SQLite backup/restore and restart-persistence tooling/tests - ACCEPTED.
3. One-command startup, shutdown and resource-health behavior - ACCEPTED.
4. Full unit/integration/E2E/replay/resilience/performance/release smoke - ACCEPTED.
5. User/developer README, reports, artifact inventory and final acceptance - ACCEPTED.

Phase 7 / V1.0 Final Acceptance: SUPERVISOR_ACCEPTED / PASS. Security repair commit `bd32acc`; English/中文 interface commit `55af154`. Independent evidence: pytest 117 passed, 1 skipped, 1 warning, 10 subtests; frontend 13 files / 59 tests; lint, typecheck, production build, compileall and pip check PASS; full and production npm audits 0 vulnerabilities; standalone E2E 11/11 in 43.9s covering Chinese switching, all routes, refresh persistence, accepted workflows, axe and overflow; `git diff --check` and `git status` clean; launcher restored running with `ownership_errors=[]` and API/UI HTTP 200.

## Task-pack policy

- Only one `luna-max` task is active at a time.
- Allowed paths are explicit and narrow.
- Existing user changes and accepted Phase 0-3 data are preserved.
- Local verification is run before supervisor review.
- The same Luna thread handles one evidence-backed repair attempt.
- Accepted tasks receive a checkpoint and update `STATUS.md` / `CHANGELOG.md`.
