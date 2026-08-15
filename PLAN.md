# AI Market Analyst V1.0 Execution Plan

## Current phase

Phase 4 - Product UI and interaction.

Current task: `P4-T01` - establish the tested backend product API contract needed by the UI without changing Phase 1-3 business logic.

## Phase 4 task graph

1. `P4-T01` Backend product API contract
   - Structured response envelopes/errors, CORS for local UI, missing read/detail/filter endpoints and API contract tests.
   - No frontend work and no core financial-rule changes.
2. `P4-T02` React + TypeScript application foundation
   - Frontend workspace, routing, design tokens, typed API client, local dev proxy, build/lint/typecheck/test commands.
3. `P4-T03` Dashboard, Settings/Health and shared status components
   - Provider/model/database health, market summary shell, loading/error/stale/degraded states.
4. `P4-T04` Asset Detail and analysis workflow
   - Instrument search, quote/quant/news/event views, charts, Signal Card and validity/horizon/re-evaluation/invalidation display.
5. `P4-T05` Predictions, Follow/Paper Trades and Performance
   - Filters/details, PaperTrade-only Follow semantics, outcome metrics, calibration reliability/buckets and PRELIMINARY state.
6. `P4-T06` Watchlist and Replay Lab UI
   - Phase 4 UI contract/shell for Watchlist and advanced replay visibility; Phase 5 owns live scanning/ranking behavior.
7. `P4-T07` Phase 4 E2E, accessibility/responsive pass and completion report
   - Main workflow E2E, regression, build artifacts and Phase 4 Gate.

## Phase 5 task graph

1. Watchlist/AppSetting schema, migration and CRUD.
2. Versioned auditable Opportunity Score and Radar API.
3. Low-concurrency scheduler, market-session policy, caching and resource backoff.
4. Alert model, deduplication, acknowledge flow and UI integration.
5. Restart/recovery, performance/resource tests, E2E and Phase 5 report.

## Phase 6 task graph

1. Benchmark metadata/providers and deterministic Benchmark Context.
2. Event schema/provider interfaces and major-event TimePolicy effects.
3. Multi-source event clustering and credibility/primary-source model.
4. Leakage-safe deterministic Market Memory and API/UI integration.
5. Capability fallback, leakage tests, E2E and Phase 6 report.

## Phase 7 task graph

1. Dependency/license/security/privacy audit and configuration hardening.
2. SQLite backup/restore and restart-persistence tooling/tests.
3. One-command startup, shutdown and resource-health behavior.
4. Full unit/integration/E2E/replay/resilience/performance/release smoke.
5. User/developer README, reports, artifact inventory and final acceptance.

## Task-pack policy

- Only one `luna-max` task is active at a time.
- Allowed paths are explicit and narrow.
- Existing user changes and accepted Phase 0-3 data are preserved.
- Local verification is run before supervisor review.
- The same Luna thread handles one evidence-backed repair attempt.
- Accepted tasks receive a checkpoint and update `STATUS.md` / `CHANGELOG.md`.
