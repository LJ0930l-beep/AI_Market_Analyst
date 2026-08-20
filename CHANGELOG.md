# Changelog

## Unreleased

### V1.0 Final Acceptance — SUPERVISOR_ACCEPTED / PASS

- Recorded the independent Phase 7/V1.0 Final Acceptance evidence: pytest 117 passed, 1 skipped, 1 warning and 10 subtests; frontend 13 files / 59 tests; lint, typecheck, production build, compileall and pip check PASS; full and production npm audits at 0 vulnerabilities; standalone E2E 11/11 in 43.9s with Chinese switching, all routes, refresh persistence, accepted workflows, axe and overflow checks; clean diff/status and restored running launcher with `ownership_errors=[]` and API/UI HTTP 200.
- Recorded security/recovery repair commit `bd32acc` and English/中文 interface commit `55af154`. V1.0 remains local-only and makes no live-provider, Qwen/ComfyUI, broker or real-trading claim.

### V1.0 final-acceptance enhancement — English/中文 interface switching

- Added a typed, dependency-free React language catalog with complete `en`/`zh-CN` key parity, browser-language detection, local persistence and an accessible global language selector.
- Added presentation-only Chinese coverage for the existing shell, routes, controls, states, labels, statuses and accessibility text without changing API/database contracts, symbols, URLs, numeric values or timestamps.
- Added unit and real-browser coverage for first-choice detection, manual switching, persistence, route-wide Chinese titles, refresh persistence, existing workflows, axe and responsive overflow checks.

### Phase 7 supervisor repair — offline restore and process ownership hardening

- Added `phase7_launcher_v2` ownership fingerprints: exact UTC start-time ticks, command-line hash, child role and port markers; mismatches report and retain state without stopping a potentially reused or tampered PID. Added isolated same-executable stale-record refusal smoke coverage.
- Made restore explicitly offline and fail closed on target `-wal`/`-shm` sidecars or persisted WAL mode before safety-backup creation; existing-target failures recover from the safety artifact and previously absent failed targets are quarantined or removed only as the exact target file.
- Updated the V1.0 runbook, completion report, ADR-026 and final acceptance evidence to preserve these recovery semantics and the existing `pip-audit`/license review limitations. Consolidated repair Gate: 117 pytest tests plus 10 subtests, 118 unittest tests, 55 frontend tests, standalone E2E 10/10 in 39.0s, launcher/recovery/release smoke PASS, and both npm audits at 0 vulnerabilities.

### Phase 7 V1.0 release-hardening — developer-ready

- Set the package/API release contract to `1.0.0` / Phase 7; added bounded local configuration, redacted health errors, `/health/release`, explicit capability evidence and loopback-only startup defaults.
- Added SQLite-consistent `phase7_backup_v1` manifest/checksum/schema/count backup and atomic restore with retained safety artifacts, plus the Windows owned-child API/UI launcher and reproducible audit/release-smoke artifacts.
- Added V1.0 README, operations runbook, completion report and machine-readable final acceptance inventory. The initial developer Gate recorded 113 pytest tests plus 10 subtests, 114 unittest tests, 55 frontend tests, lint/typecheck/build/compileall/pip check PASS, npm audits 0, standalone E2E 10/10 in 40.9s with 18 axe/overflow desktop-mobile checks; `pip-audit` unavailable and license metadata review remains explicit. Final supervisor acceptance is recorded in the V1.0 Final Acceptance entry above.

### Phase 6 supervisor acceptance handoff

- Recorded supervisor acceptance of Phase 6 after the consolidated repair: 106 pytest tests plus 10 subtests, 55 frontend tests, lint/typecheck/build/compileall/pip check PASS, both npm audits 0 vulnerabilities, standalone E2E 10/10 in 52.6s with 18 desktop/mobile axe and overflow checks, and clean `git diff --check`.
- Phase 7 is now the active V1.0 release-hardening scope; no post-V1.0/Phase 8 work is included.

### Phase 6 supervisor repair — benchmark/event/memory evidence

- Corrected benchmark aggregate freshness to require independently fresh target and benchmark data, normalized event publisher identity so same-source URLs cannot create consensus, and aligned Memory sample statistics with the selected `top_k` analogue cohort.
- Added additive SQLite migration v10 with immutable materialized Memory provenance; reads consume point-in-time materialized features when valid and explicitly report unbounded raw-ledger fallback when they are unavailable. GET routes remain non-mutating.
- Verification: 106 Python tests plus 10 subtests, 55 frontend tests, lint/typecheck/build/compileall/pip check PASS, full and production npm audits 0 vulnerabilities, standalone E2E 10/10 in 58.7s with 18 desktop/mobile axe scans and 18 overflow checks on Chrome 151.0.7922.140.
- Phase 6 repair Gate: developer PASS / ready for supervisor acceptance. Phase 7 not started.

### Phase 6 developer Gate — Benchmark Context, Events and Market Memory

- Added API/package contract `phase=6` / `0.6.0`, additive SQLite v9 typed benchmark/event/memory evidence, deterministic public-provider-routed benchmark context, point-in-time event selection, versioned source credibility/clustering and Python-owned major-event TimePolicy effects.
- Added leakage-safe fixed-feature Market Memory with explicit local materialization, as-of/self/future/incomplete evidence boundaries, read-only context APIs/UI, capability/degraded states and no model/confidence/calibration/Outcome/PaperTrade mutation.
- Verification: 105 Python tests plus 10 subtests, 55 frontend tests, lint/typecheck/build/compileall/pip check PASS, full and production npm audits 0 vulnerabilities, standalone E2E 10/10 with 18 desktop/mobile axe scans and 18 overflow checks, Chrome 151.0.7922.140, and clean diff check apart from expected LF/CRLF warnings.
- Phase 6 Gate: developer PASS / ready for supervisor acceptance. Phase 7 not started.

### Phase 5 acceptance handoff

- Recorded supervisor acceptance of the complete Phase 5 baseline: 99 Python tests plus 10 subtests, 55 frontend tests, full lint/typecheck/build/compile/pip checks, 0 npm audit vulnerabilities, standalone E2E 10/10 with 18 desktop/mobile axe and overflow checks, and clean diff check.
- Phase 6 is now the active implementation scope; no Phase 7 work is included.

### Phase 5 - P5-T04 local Alert Center

- Added SQLite v8 durable local alerts with `alert_policy_v1`, immutable event identity/evidence, deterministic prediction/outcome/Radar/news-event dedupe, bounded operational cooldown/coalescing, acknowledgement and retention-priority pruning.
- Integrated isolated post-settlement/post-scan reconciliation, read-only bounded/filterable alert APIs, Settings/Health capability evidence and accessible responsive Alert Center UI; no outbound notifier, broker, order, PaperTrade, confidence or calibration mutation.
- Verification: 99 Python tests plus 10 subtests, 55 frontend tests with lint/typecheck/build, npm audits 0 vulnerabilities, standalone E2E 10/10 PASS with 1 worker on Chrome 151.0.7922.140, axe/overflow coverage for 9 routes at desktop and 390x844, compileall/pip check/diff check PASS.
- Phase 5 Gate: ACCEPTED by supervisor; Phase 6 is the active scope.

### Phase 5 - P5-T03b background Outcome settlement and live refresh

- Added bounded point-in-time settlement for live Predictions and deduplicated live Performance/Radar refresh evidence while preserving WAIT, paper-only and no-calibration boundaries.
- Verification: 93 Python tests plus 10 subtests, 53 frontend tests with lint/typecheck/build, 0 npm audit vulnerabilities, standalone E2E 9/9 PASS and clean diff check (LF/CRLF warnings only).
- P5-T03b Gate: ACCEPTED.

### Phase 5 - P5-T03a safe local Watchlist scheduler

- Added the default-disabled, explicit-lifecycle local Watchlist scan runtime with serial model execution, deterministic market-session policy, versioned context caching, bounded resource backoff, restart recovery, process-scoped SQLite-path leasing and cooperative stop.
- Added read-only scheduler status/history and explicit start/stop/run-once controls without alerts, outcome settlement, broker access or real orders.
- Verification: 82 Python tests, 53 frontend tests and 9 browser E2E tests PASS; compile/pip checks, lint/typecheck/build, dependency audits, axe and responsive overflow checks PASS.
- P5-T03a Gate: ACCEPTED.

### Phase 5 - P5-T02 auditable Opportunity Score and read-only Market Radar

- Added deterministic, versioned `opportunity_v1` scoring over durable Watchlist/latest Prediction evidence with auditable weights, components, contributions, calibration gating and conservative WAIT/NOT_RANKED/AVOID boundaries.
- Added read-only `/radar` API and Dashboard integration with asset/category filters, registered-symbol compatibility, event-risk provenance and no-analysis/no-trade side effects.
- Verification: 68 Python tests, 52 frontend tests and 8 browser E2E tests PASS; 16 axe scans, 16 overflow checks and dependency audits PASS.
- P5-T02 Gate: ACCEPTED.

### Phase 5 - P5-T01b public-provider-compatible symbol expansion

- Added strict equity/crypto candidate parsing, bounded Yahoo/Binance public validation, durable registered Instrument metadata and Watchlist/API/client support with explicit inferred/unknown metadata.
- Verification: 57 Python tests, 50 frontend tests and 7 browser E2E tests PASS; failure-safe persistence and dependency audits PASS.

### Phase 5 - P5-T01a durable Watchlist and AppSetting foundation

- Added idempotent SQLite schema migration v5 with durable `watchlist_entries` and typed `app_settings`.
- Added strict canonical Watchlist CRUD and four validated scheduler-resource settings whose persistence does not activate background behavior.
- Upgraded Watchlist to separate durable Saved membership from Available canonical instruments with real API Add/Remove actions.
- Aligned API, Python and web package contracts to Phase 5 / version 0.5.0.
- Verification: 52 Python tests, 47 frontend tests and 7 browser E2E tests PASS; migration/restart persistence and dependency audits PASS.

### Phase 4 - P4-T07 browser Gate and completion

- Added a disposable real FastAPI/SQLite E2E harness, production-build preview/proxy server and serial Playwright browser suite.
- Covered shell health, Watchlist navigation, explicit WAIT analysis, exactly-once PaperTrade Follow, linked Outcome evidence, Performance/calibration, Replay capability/error evidence and SPA/API routing.
- Added axe and page-overflow checks for all eight Phase 4 routes at desktop and 390x844 mobile, plus strict temporary-directory and static-root safety checks.
- Fixed verified contrast, mobile grid and keyboard-focus issues in OHLCV and Performance scroll regions.
- Verification: 45 frontend unit tests, 49 Python tests and 7 browser E2E tests PASS; 16 axe scans and 16 overflow checks PASS; dependency audits report 0 vulnerabilities.
- Phase 4 Gate: PASS. See `docs/phase4-completion-report.md`.

### Phase 4 - P4-T06 Watchlist and Replay Lab

- Replaced the final Phase 4 placeholders with a searchable/filterable read-only instrument roster and an advanced read-only Replay Lab.
- Linked all six returned instruments to canonical Asset Detail routes while explicitly deferring saved membership, CRUD, ranking, scans, scheduling and alerts to Phase 5.
- Added replay status filters, pagination, selectable run detail, allowlisted run/configuration/count provenance and per-sample capability/status coverage.
- Kept replay mutation and execution out of the UI; PENDING/RUNNING are presented as passive stored metadata and actual execution remains an explicit CLI workflow.
- Verification: 49 Python tests and 45 frontend tests PASS; lint/typecheck/build/compile/dependency/diff checks PASS; dependency audits report 0 vulnerabilities.
- Live QA against the formal Phase 3 database showed all six instruments and the complete 300-row replay evidence, including 45 retained errors and technical-only historical-news limits. Desktop and 390x844 mobile had no page-level horizontal overflow.

### Phase 4 - P4-T05 Predictions, Paper Trades and Performance

- Added live filtered/paginated Prediction and PaperTrade ledgers with allowlisted details and linked Outcome evidence.
- Added paper-only Follow for active LONG/SHORT Predictions, including a synchronous client lock for same-tick duplicate actions while retaining server-side idempotency as the authority.
- Added PRELIMINARY-aware performance summary, confidence buckets and an independently scoped current calibration panel.
- Kept raw model/context payloads out of the DOM and made WAIT, expiry, empty evidence and zero-resolved states explicit.
- Moved the Vite development API proxy under `/api` so SPA routes no longer collide with backend collection endpoints.
- Verification: 49 Python tests and 37 frontend tests PASS; lint/typecheck/build/compile/dependency/diff checks PASS; dependency audits report 0 vulnerabilities.
- Live QA: browser double-click created exactly one PaperTrade; formal replay showed 255 total samples, 191 actionable/resolved and 45 invalid; ACTIVE calibration sample was 191; desktop and 390x844 mobile had no horizontal overflow.

### Phase 4 - P4-T04b Asset Detail and explicit analysis

- Added instrument and timeframe controls with independent snapshot/news request states.
- Added a local SVG candlestick and volume chart rendered only from returned OHLCV evidence.
- Added an explicit POST analysis workflow; page reads never create Predictions.
- Added a Signal Card with exact raw confidence, returned validity/horizon/re-evaluation fields, reason codes and model/provider provenance.
- Kept raw model/context payloads out of the DOM and removed all Follow/price-level affordances for WAIT.
- Verification: 49 Python tests and 24 frontend tests PASS; lint/typecheck/build/dependency/diff checks PASS; dependency audits report 0 vulnerabilities.
- Live desktop and 390x844 mobile QA PASS; initial load kept Predictions at 0, one explicit analysis saved one WAIT and zero PaperTrades, with no browser console warnings/errors.

### Phase 4 - P4-T04a read-only market snapshot

- Replaced the side-effecting GET snapshot path with provider fetch plus deterministic Python quant only.
- Added normalized timeframe and bounded history inputs with ascending real OHLCV bars.
- Preserved quote, quant and provider freshness provenance while keeping the legacy `time_policy` key explicitly null.
- Added structured snapshot provider/quant errors and injectable snapshot-service tests.
- Proved repeated GET snapshot calls do not change any SQLite table count; explicit POST analysis still creates one Prediction.
- Verification: 49 Python tests PASS, compile/dependency/diff checks PASS.

### Phase 4 - P4-T03 Dashboard and operational health

- Added real read-only Dashboard and Settings/Health surfaces backed by independent local API requests.
- Added reusable loading, empty, unavailable and degraded panel states with scoped retry controls.
- Added provider/news routing, local model, database-count, instrument-roster, Prediction-ledger and live-performance views.
- Connected the newest returned Prediction to the TimeProvenanceRail without inventing re-evaluation timestamps.
- Kept zero-resolved live performance explicitly degraded and made `metrics.resolved_actionable` the authoritative resolved count.
- Fixed native browser `fetch` invocation so the API client works in Chrome as well as test environments.
- Verification: 46 Python tests and 15 frontend tests PASS; lint/typecheck/build/compile/dependency checks PASS; dependency audits report 0 vulnerabilities.
- Live desktop and 390x844 mobile QA PASS with real local API responses, no horizontal overflow and no browser console warnings/errors.

### Phase 4 - P4-T02 frontend foundation

- Added the Vite React + TypeScript frontend workspace and reproducible lockfile.
- Added a typed API client with structured errors, filters and cancellation support.
- Added the responsive accessible research-ledger shell and all Phase 4 route placeholders.
- Added backend health loading/connected/unavailable states and TimeProvenanceRail.
- Added lint, typecheck, Vitest and production build commands.
- Upgraded vulnerable router/test dependencies; full and production audits report 0 vulnerabilities.
- Verification: lint/typecheck/build PASS, 7 frontend tests PASS, desktop/mobile visual QA PASS.

### Phase 4 - P4-T01 backend product API contract

- Added an injectable Phase 4 FastAPI app factory and local configurable CORS.
- Added structured API errors without exposing internal exception text by default.
- Added Prediction filters/details, linked PaperTrade/Outcome reads and Replay run listing.
- Made Follow idempotent and paper-only; WAIT and expired new Predictions are rejected.
- Aligned API/package version and replay prompt defaults with the verified baseline.
- Restored editable package installation and added isolated API contract tests.
- Verification: 46 tests PASS, compile check PASS and dependency check PASS.

### Project governance

- Adopted the V1.0 Master Spec for continuous Phase 4-7 execution.
- Established Sol supervisor / single `luna-max` developer workflow.
- Recorded the verified Phase 3 completion baseline and known model-output errors.
- Added the Phase 4-7 task graph, status and architecture decisions.

### Phase 3 accepted baseline

- Historical replay, performance metrics, confidence calibration, APIs and CLIs are present.
- Formal real-Qwen run completed 300 samples with all errors retained.
- Regression baseline is 41 passing tests.
