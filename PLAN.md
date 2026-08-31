# AI Market Analyst V1.2 Execution Plan

## V1.2 phase handoff and acceptance

The single active milestone is the complete V1.2 Windows desktop crypto smart-monitoring delivery. The V1.2 DOCX specification supplied by the user is authoritative over older repository details. Scope includes the Tauri 2 desktop shell and packaged FastAPI sidecar; AppData/import/migration; tray, native notification, installer and owned-process lifecycle; public crypto REST/WS with cache/freshness/reconnect/backfill; 15m close exactly-once; explicit-off MonitoringPolicy with `trigger_policy_v2`, ledger/dedupe/cooldown; real Smart 9B OpportunityAnalysis plus Python validation and Prediction/Outcome/Calibration; Fast 4B Chinese news translation with evidence and `numeric_guard`; Lightweight Charts 15m/1h overlays; typed bilingual UI and all release/live-smoke evidence.

Required boundaries are unchanged: no account, secret, broker or real order; TradingView is chart-only; Python owns financial calculation and triggers; Smart 9B is never silently replaced by 4B; monitoring/auto-start/resume are off with no startup side effects; Exit stops only the exact owned sidecar tree. Required delivery gates are V1.1 regression, Python/frontend/Tauri/installer, real Windows install/launch/single-instance/tray/notification/exit/relaunch, migration/data retention, resource bounds, BTC/ETH/SOL live REST/WS, exactly-once, real Ollama 9B/4B behavior, chart and route accessibility/overflow checks.

## V1.2 execution status

1. PLAN — COMPLETE: specification, repository governance and acceptance gates read; V1.1 architecture retained.
2. BUILD — COMPLETE: backend, storage, realtime/monitoring/news services, typed UI, Tauri shell, sidecar packaging, installer, docs and evidence implemented.
3. VERIFY — COMPLETE: regression/unit/frontend/browser/package/live Windows and live provider/model gates executed.
4. REPAIR — COMPLETE: the Sol review repairs add a real sidecar-owned monitoring runtime, dynamic tray lifecycle, official Windows autostart registration, active-close-to-tray safety, owned-backend watchdog/restart/degraded state, fixed production-build path, restricted WebView capabilities and an always-mounted alert-event bridge.

## Current phase

V1.2 complete Windows desktop crypto smart-monitoring delivery — DEVELOPER_COMPLETE; the repaired final package, live evidence and lifecycle evidence are recorded in the V1.2 documents and are pending Sol's independent review/acceptance. V1.0/V1.1 history remains preserved. No V1.3 or real-trading scope was started.

## Repair Gate evidence

- Production command is reproducible from `src-tauri`: `cargo +stable-x86_64-pc-windows-gnu tauri build --target x86_64-pc-windows-gnu`; the before-build wrapper resolves from `$PSScriptRoot`, invokes frontend and PyInstaller builds from the repository root, and fails fast on child errors.
- Runtime acceptance covers startup no-scan, explicit start, pause/no-work, resume, stop, durable resume authorization, bounded 50-symbol selection, public stream degradation/backoff and alert event publication. Desktop source/security tests cover fixed Rust sidecar arguments, no WebView spawn permission, port-conflict safety, tray routing, backend watchdog/restart and exact uninstall autostart cleanup.
- The live smoke uses the packaged sidecar with fixture fallback disabled and records the resident runtime lifecycle before the closed-bar exactly-once one-shot check. Installed lifecycle evidence separately records real install/relaunch/single-instance/data retention and the honest Windows notification click limitation.

## V1.1 task graph

1. Premium terminal design system, eleven-route information architecture and responsive accessibility — IMPLEMENTED.
2. Saved-evidence Dashboard, Markets, Calendar, News, Heatmap, Watchlist monitoring and Signal research handoff — IMPLEMENTED.
3. `qwen_consult_v2` Assistant plus deterministic Fast/Smart/Auto `qwen_route_v1` and explicit Daily Brief — IMPLEMENTED.
4. Complete typed English/Chinese presentation catalog, Intl formatting and durable language/model preferences — IMPLEMENTED.
5. Windows double-click model preparation/start/status/stop, real-model smoke, screenshots, reports and consolidated V1.1 Gate — COMPLETE (developer-verified).

V1.1 remains an Unreleased developer milestone pending supervisor review. Its consolidated developer Gate passed, and it does not modify the accepted V1.0 evidence or begin a Phase 8/real-trading scope.

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
