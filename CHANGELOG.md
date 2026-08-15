# Changelog

## Unreleased

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
