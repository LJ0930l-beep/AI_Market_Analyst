# AI Market Analyst Status

Updated: 2026-09-10

## Current milestone — institutional repair F01–F15 / chapters 12–16

The full first implementation package for the audit contract is `DEVELOPER_COMPLETE_PENDING_SOL_ACCEPTANCE`. It covers packages A–D: strategy/data correctness, evidence/research/API, portfolio/execution risk, the AI trader workspace, and the account-scoped Gate addition. Sol alone performs final independent review and acceptance.

Independent audit and repair follow-up on 2026-09-09: the two runtime findings were reproduced in disposable SQLite databases, repaired, and re-run through dedicated regression cases. `AUDIT-001` (research cancellation `scope` reference) and `AUDIT-002` (existing-position fill `ProtectionStatus` reference) are `PASS_AFTER_REPAIR`; the initial pre-repair failures remain recorded as historical evidence rather than being overwritten.

Runtime/UI audit follow-up on 2026-09-10: the currently installed 2.0.0 binary was checked read-only and is healthy/ready/local-only, but it is not currently trading. The first snapshot showed `backoff` with `run_count=18`, `consecutive_failures=18`, `last_error=monitoring cycle degraded: ValueError,ValueError,ValueError`; a later read-only snapshot showed `degraded`, `worker_alive=false`, `run_count=32`, `consecutive_failures=32`, `last_error=Runtime lease renewal failed; new risk is fenced and in-flight discovery was cancelled while protection remains active`, `execution_blocked=true`, and a connected market stream. The AI session is `STOPPED` with underlying session `IDLE`; field-level account-scoped reads confirm zero trade plans, orders, positions, and trades. The earlier active database check found a SQLite journal and a committed-view/process-view discrepancy; a later file check found no journal. No repair, migration, journal deletion, or write was attempted against that active database.

The failure was reproduced in an isolated temporary database against the public Gate market path: `StrategyMonitoringService` omitted the canonical `instrument_key` while the provider returned native `BTC_USDT` for a `BTCUSDT` request, and storage then rejected the legitimate separator difference. The caller now persists the exact identity `gate:perpetual:BTC_USDT:USDT:last`; storage validates request/native equivalence after compacting provider separators while retaining the full native identity. The isolated public three-symbol cycle now completes (`BTCUSDT=UNSUPPORTED` by the explicit crypto-ORB policy, `ETHUSDT=NO_TRIGGER`, `SOLUSDT=NO_TRIGGER`) without degraded errors. The macro calendar is now a bounded, framed, keyboard-focusable internal-scroll region with responsive height caps. The running installed binary was not restarted or replaced in this follow-up, so source repair activation remains pending the next user-approved package update.

Version check: current checkout metadata is `2.0.0` (`pyproject.toml`, `web/package.json`, Tauri package) at `32596a3f1431a6646e3c5cb853a213fcc69a067c` / `v2.0.0`. The user's V1.7 reference was checked only; no version number or release metadata was changed.

Safety boundary: the pre-existing dirty and untracked worktree was preserved. No reset, checkout, clean, commit, push, or `scripts/direct_install.ps1` was used. After the user explicitly confirmed that the app had been exited, the current 2.0.0 installer updated the verified user-level install target and desktop shortcut; no business database, private account, Testnet action, LIVE unlock, or real order was used. Database migration checks used temporary/copy databases only.

### Implementation summary

- F01–F04: `StrategySpec`/parameter contracts, six strategy schemas, 5m signal versus 15m structure, session VWAP/ORB gates, Decimal tick/step quantization, EMA 1h/RSI environment, and score versus calibrated probability separation.
- F05–F07: canonical venue/market/native/settlement/price identity, additive `market_bar_versions`/catalog/migration records, event/available/fetched timestamps, latest/range/cursor queries, PIT replay and explicit signal-only/missing-context statuses.
- F08–F11: independent counterfactual cost recomputation, lifecycle/partial-exit/SHORT MAE-MFE/MTM risk metrics, authoritative fill-ledger research, exact SourceRegistry/redirect Origin checks, news revision/fact-impact/negation handling.
- F12–F15 and chapters 12–16: frozen EvidenceBundle, real-manifest-only model digest, conservative risk clusters/TCA/capacity status, transactional outbox, v3 data/research/risk/evidence APIs, unified account-scoped projection, Chinese UI evidence panel, and runtime identity diagnostics.
- Gate account chain: additive encrypted `secure_account_credentials`, explicit/idempotent `gate_paper` (legacy ID backed by official Gate TestNet) and `gate_live` registration, scoped Gate config/account/trade reads and writes, read-only credential verification before persistence, account-specific TestNet adapter/receipt projection, gateway-level adapter scope resolution, trade-plan account binding, and the unchanged server-side LIVE release lock. Local ledger projection remains limited to explicitly non-Gate simulated/PAPER accounts.
- Runtime/UI follow-up changed paths: `core/strategy_monitoring.py`, `core/storage/sqlite.py`, `tests/test_institutional_repair_regressions.py`, `web/src/pages/V2WorkspacePage.tsx`, `web/src/pages/V2WorkspacePage.test.tsx`, `web/src/v2.css`, `docs/institutional-repair-implementation.md`, `docs/institutional-repair-acceptance.md`, `evidence/institutional-repair-result.json`, and this `STATUS.md`.
- LIVE remains default `LOCKED`; `scripts/institutional_acceptance.py` is an offline isolated acceptance script and reports external dependencies honestly.

### Exact verification and results

- `python -m pytest -q` — PASS, `358 passed, 1 skipped, 1 warning in 150.69s (0:02:30)`.
- `python -m pytest -q tests/test_gate_account_chain.py tests/test_institutional_v3_api.py tests/test_institutional_repair_regressions.py` — PASS, `26 passed, 1 warning in 9.84s`.
- `python -m pytest -q tests/test_gate_account_chain.py` — PASS, `11 passed, 1 warning in 5.95s`; this includes the two repaired audit reproducers, read-only credential verification, and the Gate plan/adapter scope cases.
- `python -m pytest -q tests/test_institutional_repair_regressions.py tests/test_spec_at24_at30.py tests/test_trader_reliability_v14.py::test_v14_d03_closed_bar_replay_and_real_parameter_perturbation` — PASS, `18 passed, 1 warning`; the repair regression set had `10 failed` against the pre-repair baseline.
- `ruff check apps core scripts/institutional_acceptance.py tests/test_gate_account_chain.py --select F821` — PASS, all checks passed after the two runtime repairs and account routing addition.
- `python -m compileall -q core apps scripts/institutional_acceptance.py tests/test_gate_account_chain.py tests/test_institutional_repair_regressions.py` — PASS, exit 0.
- `python scripts/institutional_acceptance.py` — PASS, 4/4 isolated checks; the evidence wrapper was preserved and updated manually.
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-tauri.ps1` — PASS, current React production assets and PyInstaller sidecar rebuilt from source.
- `cargo +stable-x86_64-pc-windows-gnu tauri build --target x86_64-pc-windows-gnu --config '{"build":{"beforeBuildCommand":""}}'` — PASS, current NSIS installer generated; the inline override bypassed the existing relative `beforeBuildCommand` path issue without editing user-owned config.
- Current installer `/S` user-level install — PASS, exit code 0; installed `ai-market-analyst.exe` reports version `2.0.0`, the installed sidecar SHA-256 matches the freshly rebuilt source sidecar, and the desktop shortcut points to the verified install directory. No post-install process remained running.
- Current installer artifact: `src-tauri\target\x86_64-pc-windows-gnu\release\bundle\nsis\AI Market Analyst_2.0.0_x64-setup.exe`, SHA-256 `E26D30FBD39948391A0E40E77BF7248288075B4D7193DF5D7391E354BF3F7225`.
- `cd web; npm run build` — PASS, Vite production build, 85 modules transformed.
- `cd web; npm test -- --run` — PASS, 22 test files / 96 tests in 30.98s; includes the keyboard-focusable macro calendar regression.
- `cd web; npm run typecheck` — PASS.
- `cd web; npm run lint` — PASS with `--max-warnings=0`.
- The acceptance script was intentionally run without `--output` so the hand-curated delivery wrapper remained intact; its 4/4 result is recorded above and in `evidence/institutional-repair-result.json`.
- Read-only Gate public endpoint check — PASS: TestNet `GET /futures/usdt/contracts` returned HTTP 200 with 63 JSON contracts; Live futures `GET /futures/usdt/contracts` returned HTTP 200 with 973 JSON contracts. No private request or order was made.
- Isolated `GatePublicProvider` + `StrategyMonitoringService` cycle with a temporary SQLite database — PASS: 240 bars per symbol; BTC `UNSUPPORTED` by explicit crypto ORB policy, ETH/SOL `NO_TRIGGER`, overall `COMPLETED`, with no degraded error.
- `cd web; npx vitest run src/pages/V2WorkspacePage.test.tsx` — PASS, 1 file / 7 tests in 2.78s; includes macro-calendar scroll-region semantics.

### Deliverables

- [Implementation record](docs/institutional-repair-implementation.md)
- [Acceptance record](docs/institutional-repair-acceptance.md)
- [Machine-readable evidence](evidence/institutional-repair-result.json)
- [Isolated acceptance script](scripts/institutional_acceptance.py)

### Blockers and residual risks

- Gate public futures-contract GETs were network-verified for the configured TestNet and Live base URLs; other real public HTTP provider/backfill, real Ollama `qwen3.5:9b` weight digest/E2E, fixed model evaluation, forward shadow account, external Testnet and private fills were not run and remain `NOT_CONFIGURED`/`NOT_RUN`.
- Gate account writes, TestNet adapter/receipt fixtures, and non-Gate local PAPER execution are covered in disposable databases; no real Gate private endpoint, TestNet order/fill, or live account read was attempted. `gate_live` remains release-locked and its stored credentials, if later supplied, are never used to bypass that lock.
- DSR/PBO remain unavailable until a valid sufficiently sized experiment/OOS matrix and required distribution inputs exist; no demo statistic was inserted.
- Production activity-database migration and cross-process external reconciliation were intentionally not performed. The user-level package was updated only after explicit confirmation that the prior app had exited; this was not a production database migration or a live-trading activation.
- Real order-book depth, participation, venue margin, funding and external fee evidence are not configured; PAPER capacity remains explicit unknown/conservative.
- `git diff --check` exits 2 only for a pre-existing `core/providers/gateio_provider.py:144` blank line at EOF and Windows line-ending warnings; the current repair/UI paths pass individually; unrelated user whitespace was preserved.

Only after this section, the earlier v1.2 handoff is retained for history.

## Previous milestone — repair plan v1.2 complete package (preserved)

The assigned Luna Max implementation package is `DEVELOPER_COMPLETE_PENDING_SOL_ACCEPTANCE`. It covers the full repair plan v1.2 scope for unified execution/risk, reduce-only semantics, protection-preserving session termination, production AI session coordination, account/venue/mode isolation, dynamic TESTNET capability reporting, and evidence honesty. Sol alone performs final review, verification, and acceptance.

Safety boundary: work stayed in the existing dirty/untracked checkout; no reset, clean, push, real funds, private exchange credential, external TESTNET action, or LIVE unlock was used. `AGENTS.md` and exact uppercase `SPEC.md` were absent; `MASTER_SPEC.md`, the repository/spec equivalents, complete `repair-plan-v1.2.md`, and relevant `repair-spec-v1.1.md` material were read.

### Implementation summary

- `ExecutionGateway` is the single PAPER/TESTNET/LIVE submission boundary. Fresh market data, contract/step/fee/slippage, authorization ceilings, atomic single/portfolio/cluster/daily budget reservation, UNKNOWN hold, and terminal/idempotent reconciliation are enforced before or during execution.
- `AccountLedger` and `simulated_positions` now carry normalized account/venue/mode/position identity, protection/version state, fill audit metadata, foreign-fee valuation state, and `legacy_unverified` migration handling. Reduce-only direction, remaining quantity, partial fills, and concurrent CAS exits are scoped and idempotent.
- `MonitoringRuntime` owns the AI session and lease lifecycle while `PositionGuardian` and its protection stream survive strategy pause/termination for protected positions. `AISessionCoordinator` provides the production Qwen 9B path without silent fallback; model unavailability is explicit.
- V2 API and frontend control/query paths require account/runtime scope and use the existing API client. Model status and TESTNET capabilities are dynamic; diagnostics and the manifest bind actual source/JUnit evidence rather than fixed claims.

### Changed paths in this repair package

Core/API: `core/trading/execution_gateway.py`, `core/trading/risk_engine.py`, `core/trading/ledger.py`, `core/trading/authorization.py`, `core/trading/position_guardian.py`, `core/trading/ai_led_engine.py`, `core/trading/ai_session_coordinator.py`, `core/trading/session_manager.py`, `core/trading/simulated_execution.py`, `core/trading/testnet_capabilities.py`, `core/monitoring_runtime.py`, `core/v2_store.py`, `core/agent_execution.py`, `core/strategy_monitoring.py`, `apps/api/v2.py`, `core/diagnostics.py`, and `core/manifest.py`.

Frontend/tests/evidence: `web/src/components/AITraderPanel.tsx`, `web/src/components/AuthorizationWizardModal.tsx`, `web/src/pages/V2WorkspacePage.tsx`, `tests/test_repair_v12_luna.py`, `tests/test_spec_at01_at07.py`, `docs/repair-v1.2-luna-report.md`, `artifacts/repair-v1.2-luna-evidence.json`, and `evidence/manifest.json`. Other dirty/untracked paths listed by Git are preserved user work and were not reset or cleaned.

### Exact verification and results

- `python -m pytest -q tests/test_spec_at01_at07.py --junitxml=artifacts/junit-at01-at07-current.xml` — PASS, 7 passed.
- `python -m pytest -q tests/test_repair_v12_luna.py tests/test_spec_at24_at30.py::test_at27_diagnostic_bundle_zero_secret_leakage --junitxml=artifacts/junit-repair-v12-focused-current.xml` — PASS, 16 passed, 1 known FastAPI/Starlette `httpx` deprecation warning.
- `python -m pytest -q --junitxml=artifacts/junit-full-current.xml` — PASS, 268 passed, 1 skipped, 1 warning, 269 collected.
- `python -m compileall -q core apps tests/test_repair_v12_luna.py tests/test_spec_at01_at07.py` — PASS.
- `cd web; npm test -- --run` — PASS, 19 files/85 tests.
- `cd web; npm run typecheck` — PASS.
- `cd web; npm run build` — PASS, Vite production build with 82 modules transformed.

### Blockers and residual risks

- Real local Qwen `qwen3.5:9b` E2E is `NOT_RUN`; the coordinator’s fake-provider coverage is mock-only and RT17 must not be promoted to real model acceptance.
- External TESTNET adapter capability/protection/fill/fee evidence is `NOT_RUN` because independent authorization was not provided. RT23/RT25 and external acceptance rows remain authorization-gated; clean Windows installation is `NOT_RUN`.
- LIVE remains `LOCKED` by release policy.
- Cross-process provider-side revoke/cancel timing, ownership reconciliation for `legacy_unverified` positions, and account ownership migration for legacy strategy subscriptions remain residual risks. Local PAPER matching is not external venue evidence.
- `git diff --check` has no code error but reports EOF whitespace warnings at `core/providers/gateio_provider.py:144` and `web/src/v2.css:1344`, plus normal Windows line-ending warnings.

### Evidence paths

- Report: `docs/repair-v1.2-luna-report.md`
- Machine-readable results: `artifacts/repair-v1.2-luna-evidence.json`
- Source/JUnit manifest: `evidence/manifest.json`
- Current full JUnit: `artifacts/junit-full-current.xml`

## Historical prior milestone status (preserved)

AI Market Analyst V1.2.1 Production Usability Repair is `DEVELOPER_COMPLETE` and ready for Sol's independent acceptance. It remains one local-only Windows x64 product stage on top of the V1.2 history; no V1.3 or real-trading work was introduced.

The repaired contract is API/package `1.2.1`, desktop contract `desktop_backend_v1`, monitoring runtime `monitoring_runtime_v1`, trigger policy `trigger_policy_v2`, and additive/idempotent SQLite schema `13`. Monitoring, Windows auto-start, and resume-monitoring-on-startup are all off by default.

## Delivered repair

- Reproducible official build from `src-tauri`, including React production assets, packaged PyInstaller sidecar, Tauri executable and NSIS installer.
- Sidecar-owned background monitoring with explicit start/resume/pause/stop, enabled-policy-only public ingestion, WS reconnect/backfill/freshness, bounded 20/50-symbol resources, 15m close exactly-once, 1h context, Smart 9B-only analysis, alert persistence and background events.
- Dynamic tray status and actions, accurate Settings route, active-close-to-tray safety notice, explicit inactive exit, single instance, owned-sidecar watchdog/degraded/restart and safe automatic free-port selection across `18765..18828`.
- Official Tauri Windows autostart registration with OS-authoritative UI state; auto-start and independent resume authorization remain explicit and default off.
- Restricted WebView capability with no shell spawn permission; Rust owns fixed executable/host/port/model arguments. No account, exchange secret, private key, broker, real order, funds or proprietary chart asset exists.
- Always-mounted deduped alert bridge, Alert Center fallback, native-notification request path, bilingual typed routes, responsive/accessibility checks, schema-13 public hydration audit, 4B translation cache and strict numeric guard.
- Signals zero-Prediction state now presents deterministic BTC/ETH/SOL/watchlist market evidence, monitoring and `trigger_policy_v2` status, transparent unavailable/degraded reasons, and explicit analysis links without calling Qwen on page load.

## Final developer Gate summary

- Python: `158 passed, 1 skipped, 1 warning`; V1.2/V1.2.1 focus: `28 passed, 1 warning`; unittest: `Ran 132 tests`, `OK (skipped=1)`; compileall and `pip check` passed.
- Frontend required order: build passed (Vite 6.4.3, 75 modules), lint passed, typecheck passed, Vitest passed (`18 files, 82 tests`).
- Browser E2E: Playwright 1.62.1, `13 passed` in 1.1m, including 15 routes, 30 axe scans and 30 page-overflow checks. It uses deterministic fixtures and is not live proof.
- Security/dependencies: full and production npm audit both report zero vulnerabilities; tracked secret scan reports zero findings; `pip-audit` is unavailable and license inventory remains `review_required`.
- Cargo GNU check passed. The exact official Tauri production command passed and produced the V1.2.1 NSIS installer.
- Packaged live smoke passed with fixture fallback false: real Binance public BTC/ETH/SOL REST (240 bars each), WS, 15m/1h chart data (120 bars per symbol/timeframe), runtime lifecycle, bar-close exactly-once, qwen3.5:9b Smart analysis, 20 English RSS events, qwen3.5:4b Chinese translation and numeric guard.

## Release artifact

- Installer: `D:\RJ\codex\ai-market-analyst\src-tauri\target\x86_64-pc-windows-gnu\release\bundle\nsis\AI Market Analyst_1.2.1_x64-setup.exe`
- Size: `42,553,612` bytes
- SHA-256: `B94D80529BA4D49CE5FA3F2F2AA0FB9C3466915AE62D8257939D83216F2FD360`
- Packaged sidecar SHA-256: `DC8D0293991E134DD924E510B597662D1B1E4104BAAE519EA5F30F4671825A92`

## Installed Windows acceptance

The final installed package passed launch/backend-ready, single-instance, explicit monitoring start, hidden-window background worker, active-close-to-tray notice, stop, inactive X exit, relaunch, exact owned-sidecar exit, crash-to-degraded and owned-backend restart. A controlled foreign listener retained `127.0.0.1:18765` while the app safely selected `18766`. Enabling auto-start created the exact current-user Run entry; disabling removed it. Uninstall removed program files and autostart registry entries while preserving the AppData database byte-for-byte; reinstall/upgrade retained the database. Ollama remained alive throughout.

The final installed UI was captured directly from the Tauri window on the normal owned port `18765` with no test-port override or fixture/mock: Dashboard real providers, Settings ownership identity, Monitoring 240-bar chart/running worker, and Signals zero-Prediction deterministic evidence all passed. The final runtime is safely stopped; resume eligibility is cleared, auto-start and resume are false, the app is exited, all owned listeners are released, and AppData is retained.

## Honest limitations

- The installed app requested a native Windows notification, and action/deep-link plus in-app fallback code is test-covered, but this automation surface could not directly observe an ordinary Windows toast click. That direct-click Gate is not claimed as observed.
- The native taskbar tray popup is not exposed as a targetable automation window and Windows-key taskbar navigation is prohibited. Dynamic tray behavior is covered by source/tests plus installed active-close/background/reopen evidence, but a direct popup-menu click sequence is not claimed.
- `pip-audit` is not installed. PyInstaller reports the non-fatal hidden-import warning `tzdata not found`; packaged sidecar and live smokes pass. FastAPI TestClient emits one known Starlette/httpx deprecation warning.
- Public Binance/RSS availability, Windows notification policy and local Ollama latency/model availability remain external runtime dependencies.

## Evidence

- `docs/v1.2.1-completion-report.md`
- `docs/v1.2.1-installer-smoke.md`
- `docs/v1.2.1-live-smoke.json`
- `docs/v1.2.1-test-evidence.json`
- `docs/v1.2.1-user-guide.md`
- `docs/v1.2.1-screenshot-inventory.md`
- `docs/phase7-security-audit.json`, `docs/phase7-license-audit.json`, `docs/phase7-release-smoke.json`

Sol alone decides final acceptance. Historical V1.0/V1.1/V1.2 reports remain historical and are not rewritten as current evidence.
