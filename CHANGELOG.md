# Changelog

## Unreleased

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
