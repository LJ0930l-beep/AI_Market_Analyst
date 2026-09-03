# AI Market Analyst Status

Updated: 2026-09-03

## Current milestone

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
