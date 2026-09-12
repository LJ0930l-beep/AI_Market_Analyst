# AI Market Analyst V2.0 — AI 自拟策略 + 新闻 + 技术面

当前机器人主链路为：公开 K 线与新闻 → Qwen3.5:9b 自拟策略 → 严格 JSON → 固定风控 → 开仓或等待。现有六策略候选仅作参考；AI 模式下旧策略循环不单独调用模型做单。已有持仓仍由独立 PositionGuardian 保护。

在交易控制台选择账户，再点击「启动 AI 自动做单」。每 15 分钟 UTC 边界运行一轮；首次启动保留历史 K 线校准门槛。启动按钮允许向所选账户自动提交订单，请核对账户的 PAPER / TESTNET / LIVE 标识。LIVE 是否可执行仍取决于既有适配器能力门禁；本次开发没有启用真实账户交易。

新开仓要求有效新闻引用、15m/1h 已收盘 K 线、当前价格位于入场区间、有效止盈止损、扣费后盈亏比至少 2、AI 主观置信分数至少 70。单笔风险上限 0.25%，组合风险上限 1%，日亏损熔断 1.5%，杠杆上限 3 倍（未指定时 1 倍）。分数不等于胜率。

实现、运行、验证和兼容边界见 [AI 新闻技术面机器人说明](docs/ai-news-trader-2026-09-12.md)。真实公开数据与本机模型联调记录见 [smoke evidence](evidence/ai_news_strategy_smoke.json)。它验证了结构化 WAIT 流程，不代表收益或真实资金执行已验证。

以下是历史 V1.2.1 的安装和研究功能记录；涉及版本号、研究专用边界与当前 V2.0 不同时，以以上当前行为及 V2.0 文档为准。

## Quick start (Windows desktop)

For a packaged install, run the generated NSIS installer:

`src-tauri\target\x86_64-pc-windows-gnu\release\bundle\nsis\AI Market Analyst_1.2.1_x64-setup.exe`

The default program directory is `%LOCALAPPDATA%\Programs\AI Market Analyst`. Launch `ai-market-analyst.exe` from the Start menu or that directory. Runtime data is kept separately under `%LOCALAPPDATA%\AI Market Analyst` (`data`, `logs`, `backups`, `runtime`) so upgrades and uninstall preserve the research ledger. The app starts its owned FastAPI sidecar on the first free loopback port in `127.0.0.1:18765..18828`, verifies the instance/ownership contract, waits for `/health`, and opens the desktop window. It diagnoses but never takes over or kills an unrelated listener. Re-launching is single-instance and focuses the existing window. The installed production runtime contains the built React assets and packaged sidecar; it does not require Vite, npm or Node.js.

Before using Smart monitoring, install the exact local models once:

```powershell
ollama pull qwen3.5:4b
ollama pull qwen3.5:9b
```

Monitoring, auto-start and resume are all off by default. Enable an individual BTC/ETH/SOL policy in the Monitoring page, save it, and press Start monitoring when you explicitly want the sidecar worker to run. Run now remains a separate one-cycle action; there is no startup scan. The tray menu can show the terminal, pause/resume monitoring, restart the owned backend, open Settings or exit the application. Window close while monitoring is active always hides to the tray and explains that work continues; when inactive it follows the close-to-tray preference. Alert Center entries can request an optional local Windows native notification; the always-mounted desktop bridge also keeps a safe in-app route when native access is unavailable.

Signals remains useful before the first AI Prediction exists: it shows deterministic public market/freshness evidence, Python trigger state and any validated monitoring opportunity for BTC/ETH/SOL plus watchlist symbols. Every card links to the explicit asset analysis workflow. Merely opening Signals does not invoke Qwen or create a Prediction, and backend/provider failures remain labeled instead of becoming fabricated signals.

For repository development, the V1.1 command wrappers remain available, but V1.2 packaging and live evidence use `scripts\build-tauri.ps1`, `scripts\v12-sidecar-smoke.ps1` and `scripts\v12-live-smoke.py`. The reproducible Windows release command is run from `src-tauri` and builds the frontend, owned sidecar, Tauri shell and NSIS installer in one pass:

```powershell
cd D:\RJ\codex\ai-market-analyst\src-tauri
cargo +stable-x86_64-pc-windows-gnu tauri build --target x86_64-pc-windows-gnu
```

The configured wrapper resolves repository-root scripts from `$PSScriptRoot`, so the command is independent of Tauri's child-process working directory.

Developer setup remains available from PowerShell:

From the repository root:

```powershell
python -m pip install -e ".[api,market,dev]"
cd web
npm ci
npm run build
cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action start
```

The launcher binds the API and built UI to loopback (`127.0.0.1`), checks Python/uvicorn/Node/npm, checks the database path and ports, and writes only owned-child state under the OS temporary directory. Each child record includes the executable, exact UTC start-time ticks, command-line hash, role and port markers. Status reports `ownership_mismatch`, and stop retains the state without acting, if any fingerprint does not match. It never searches by process name or stops ComfyUI, Ollama, or another unrelated process. If the build is missing, `start` builds it; use `-Build` to force a rebuild.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action stop
```

The legacy browser launcher is available at `http://127.0.0.1:4173`; the desktop discovers its packaged API from the selected loopback port rather than assuming a foreign listener on `18765` is safe. The sidecar uses AppData by default and does not run the trigger/model monitoring lifecycle at startup. Public hydration is a bounded read/cache worker with zero monitoring, LLM or financial-ledger writes. A user-started `monitoring_runtime_v1` worker continues while the window is hidden and can be paused or stopped from the tray or Monitoring page.

The frontend language selector is available in the workspace header and supports `中文` and `English`. The selection is stored locally under `ai-market-analyst.language`, survives route changes and refreshes, and uses the browser language (`zh-*` or English fallback) when no choice is stored. Translation is presentation-only: API paths, symbols, URLs, numeric values, timestamps, database values and standard backend contracts are unchanged; provider/model free text remains evidence returned by the backend.

The interface includes **Qwen Consult / Qwen 咨询**. It streams responses from the server-configured local Ollama/Qwen model and remains read-only. It does not analyze, scan, settle, Follow, create Predictions/Outcomes/PaperTrades/alerts, or place orders.

## V1.2.1 product surface

- Public Binance crypto REST and WebSocket data covers BTCUSDT, ETHUSDT and SOLUSDT. REST responses persist bounded 15m/1h bars and freshness; the WebSocket path reconnects and backfills through REST. Python owns 15-minute bar-close exactly-once ledgering.
- Monitoring is `MonitoringPolicy` explicit opt-in. `trigger_policy_v2`, persistent trigger fingerprints, cooldowns and bounded 20/50-symbol resource limits run in Python. Smart OpportunityAnalysis calls only `qwen3.5:9b`; unavailable or invalid output is visible as degraded/quarantined evidence, never silently relabeled as Fast 4B.
- The validator owns direction, entry, stop, TP1 risk/reward and WAIT safety. Valid predictions feed the existing Prediction/Outcome/Calibration records and chart annotations; invalid model output is retained as quarantine evidence.
- Lightweight Charts is a frontend-only visualization library. `/chart/{symbol}/bars` and `/chart/{symbol}/annotations` are app APIs backed by public market data and Python annotations; TradingView is not a backend data source and no proprietary chart library is redistributed.
- English RSS/news evidence keeps the full original title/summary/source and offers explicit qwen3.5:4b Chinese translation caching. `numeric_guard` rejects translations that lose structured numbers, dates, percentages or prices.
- Native Windows notification routing and alert deep links are wired through Tauri. The app has no remote notification service.

## V1.2.1 API and data contracts

The packaged sidecar exposes `/health`, `/market/realtime/{symbol}`, `/market/realtime/{symbol}/stream`, `/monitoring`, `/monitoring/status`, `/monitoring/start`, `/monitoring/resume`, `/monitoring/pause`, `/monitoring/stop`, `/monitoring/events`, `/monitoring/opportunities`, `/triggers`, `/monitoring/run`, `/chart/{symbol}/bars`, `/chart/{symbol}/annotations`, `/news/{symbol}` and `/news/translate/{news_id}` in addition to the additive V1.1 routes. Contract identifiers are `crypto_realtime_v1`, `monitoring_policy_v1`, `monitoring_runtime_v1`, `trigger_policy_v2`, `opportunity_analysis_v1`, `chart_data_v1`, `chart_annotations_v1` and `news_evidence_v1`.

Schema 13 includes the additive V1.2 monitoring tables and the V1.2.1 `public_hydration_runs` audit table. Migrations are additive and idempotent; an old database is not silently copied or destroyed. Use the explicit importer when bringing a V1.1 database into AppData:

```powershell
python -B scripts\import_legacy_database.py --source data\market_analyst.sqlite3
```

The importer reports source/destination, schema and row evidence and refuses ambiguous or unsafe paths.

## Configuration

All runtime configuration is local and bounded. Defaults are safe for a single-user development machine.

| Variable | Default | Boundary |
| --- | --- | --- |
| `DATABASE_PATH` | `data/market_analyst.sqlite3` | Explicit `.sqlite3`, `.sqlite`, or `.db` path; existing symlink/reparse paths are rejected. |
| `AIMA_DATA_ROOT` | `%LOCALAPPDATA%\AI Market Analyst` | Packaged AppData root; creates only `data`, `logs`, `backups` and `runtime` children. |
| `AIMA_SIDECAR_PORT` | unset | Optional exact loopback port for isolated smoke/test runs; normal desktop startup safely selects the first free port in `18765..18828`. |
| `MARKET_DATA_MODE` | `real` | Public-provider routing; use `fixture` only for deterministic local tests. |
| `BINANCE_WS_URL` | `wss://data-stream.binance.vision` | Public Binance stream endpoint; no authenticated stream or account data. |
| `NEWS_MODE` | `real` | RSS/public evidence or the explicit fixture test adapter. |
| `LLM_MODE` | `ollama` | Local Ollama only; `disabled` preserves a saved `WAIT`. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint; no cloud fallback. |
| `OLLAMA_MODEL` | `qwen3.5:4b` | Financial-analysis/scheduler model; retained as the Fast default. |
| `FAST_MODEL` / `SMART_MODEL` | `qwen3.5:4b` / `qwen3.5:9b` | V1.2 translation/Fast and Smart tiers; Smart monitoring requires 9B and never falls back to 4B. |
| `ALLOW_FIXTURE_FALLBACK` | `0` | Must be explicitly enabled for tests; packaged/live smoke runs with fallback disabled. |
| `MAX_MONITOR_SYMBOLS` | `50` | Finite monitoring cache/policy bound; the normal UI universe is BTC/ETH/SOL. |
| `MONITORING_MAX_CONCURRENCY` | `1` | Serial Python-owned monitoring/model execution. |
| `OLLAMA_TIMEOUT_SEC` / `OLLAMA_RETRIES` | `45` / `1` | Finite model request timeout and at most two attempts. |
| `QWEN_CONSULT_CONNECT_TIMEOUT_SEC` | `3` | Consultation connection timeout; bounded to 0.5-10 seconds. |
| `QWEN_CONSULT_FIRST_TOKEN_TIMEOUT_SEC` / `QWEN_CONSULT_STREAM_IDLE_TIMEOUT_SEC` | `20` / `20` | First-token and between-token limits; each bounded to 1-60 seconds. |
| `QWEN_CONSULT_TOTAL_TIMEOUT_SEC` | `90` | Whole consultation generation limit; bounded to 5-180 seconds. |
| `QWEN_CONSULT_MAX_BODY_BYTES` / `QWEN_CONSULT_MAX_MESSAGES` | `32768` / `24` | Request body and message-count limits. |
| `QWEN_CONSULT_MAX_MESSAGE_CHARS` / `QWEN_CONSULT_MAX_TOTAL_CHARS` | `4000` / `16000` | Per-message and complete-history character limits. |
| `QWEN_CONSULT_MAX_OUTPUT_CHARS` / `QWEN_CONSULT_MAX_OUTPUT_TOKENS` | `12000` / inherited bounded Ollama value | Output budgets enforced by server and model request. |
| `QWEN_CONSULT_RETRIES` / `QWEN_CONSULT_CONCURRENCY` | `0` / `1` | Retry only before output (maximum one); consultation model work is always serial. |
| `QWEN_CONSULT_FRESHNESS_SEC` | `3600` | Threshold used to label saved symbol evidence fresh/stale. |
| `API_CORS_ORIGINS` | local dev origins | Comma-separated bounded allowlist; wildcard credentials are rejected. |
| `API_DEBUG_ERRORS` | `false` | Production-safe API errors omit unexpected exception text. |

`GET /health/release` reports the API/package contract, schema, backup format and local-only capabilities without exposing the absolute database path. `/health/providers` and `/health/model` distinguish routing/model degradation from backend health. Model health includes the redacted `qwen_consult_v2` capability, Fast/Smart model IDs, installed availability and deterministic route policy; it never returns the configured base URL. Startup does not invoke Qwen.

The V1.2.1 packaged defaults are additionally visible from `/monitoring`: all policies are disabled, `primary_timeframe=15m`, `context_timeframe=1h`, `auto_start=false` and `resume=false`. A policy must be explicitly saved and the sidecar runtime must be explicitly started before background monitoring does work. `/monitoring/run` remains a separate explicit one-cycle action. The realtime REST path is read/cache write only; trigger ledger writes occur only in the explicit monitoring lifecycle and are idempotent by bar-close fingerprint.

V1.1 adds Market Pulse, stored point-in-time Calendar/News, deterministic Watchlist monitoring, evidence-only Heatmap, Signal research handoff and an explicitly generated Daily Brief. These surfaces reuse saved evidence. `unavailable` and `stale` are deliberate evidence states, not placeholder live data. Daily Brief generation is a POST-only user action and records its as-of, sources, missing evidence, model tier and route reason.

## Backup, restore and recovery

Do not copy a live SQLite file directly. Use the explicit artifact command, which calls the SQLite backup API and writes a checksum- and schema-validated directory:

```powershell
python -B scripts\phase7_backup.py backup `
  --database data\market_analyst.sqlite3 `
  --output data\backups\market-20260820T120000Z

python -B scripts\phase7_backup.py restore `
  --input data\backups\market-20260820T120000Z `
  --database data\market_analyst.sqlite3
```

Each artifact contains `database.sqlite3` and `manifest.json` with `phase7_backup_v1`, app version, schema version, UTC creation time, source basename, SHA-256 and table-count evidence. Restore is explicitly offline: stop the owned launcher and close all SQLite connections first. A target `-wal`/`-shm` sidecar or persisted WAL journal mode is rejected before a safety artifact is created or the target is touched; the tool does not checkpoint or merge live WAL state. Restore validates the manifest, checksum, SQLite integrity and supported schema before touching the target. An existing target first receives a sibling `*.pre-restore-<UTC>-<nonce>` safety artifact; replacement is staged and atomic, with recovery from that artifact if post-replacement validation fails. If a previously absent target fails post-replacement validation, the exact failed file is quarantined as `*.restore-failed-<UTC>-<nonce>` (or removed if quarantine is unavailable), so the original absent state is preserved. Invalid, tampered, self-referential, symlinked, broad or path-confused targets are rejected. Safety and failure artifacts are retained for operator recovery and no recursive delete is used.

## Developer setup and verification

```powershell
python -m pytest -q
python -B -m unittest discover -s tests -v
python -B -m compileall -q apps core tests scripts
python -m pip check

cd web
npm run build
npm run lint
npm run typecheck
npm test -- --run
npm run audit
npm run audit:production
npm run e2e:preflight
npm run e2e
npm run screenshots:v11

cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-launcher-ownership-smoke.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-sidecar.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\v12-sidecar-smoke.ps1 18768
python scripts\v12-live-smoke.py

cd src-tauri
cargo +stable-x86_64-pc-windows-gnu tauri build --target x86_64-pc-windows-gnu
```

The production command above is the reproducible release gate. Tauri runs its
`beforeBuildCommand` from the repository root, where
`scripts/build-tauri.ps1` resolves both frontend and PyInstaller inputs from
`$PSScriptRoot`; the wrapper also fails fast when either native build returns a
non-zero exit code. It produces the frontend, both target-tagged sidecars, the
Tauri executable and the NSIS installer without a Vite/npm/Node dependency in
the installed app.

The frontend unit suite includes catalog key-parity, browser-language detection, manual switching and local persistence checks. The browser suite also verifies Chinese titles across all current routes and language persistence after navigation and refresh.

The E2E suite builds and runs the real React app and FastAPI routes against a disposable SQLite database. It uses deterministic injected providers only for browser reproducibility; it does not claim live Binance/Ollama behavior. `scripts\v12-live-smoke.py` is the separate disposable live REST/WS/RSS/Ollama/monitoring check and records `docs/v1.2.1-live-smoke.json`; fixture fallback is disabled. `scripts\phase7_audit.py --output-dir docs` records the local npm/pip checks, optional-tool availability, tracked secret scan and dependency license inventory. An unavailable `pip-audit` or an unknown license remains explicitly marked in the report.

## Architecture and safety boundary

The V1.2.1 core flow is:

`Binance public REST/WS → bounded SQLite bar/realtime cache → Python indicators + trigger_policy_v2 → qwen3.5:9b OpportunityAnalysis → Python validator → Prediction/Outcome/Calibration + chart/alert evidence`

Python owns instrument validation, OHLCV calculations, bar-close exactly-once, trigger scoring, risk levels, model-output validation, dedupe/cooldown, persistence and outcome/calibration integration. React is a read/interaction surface and never recalculates financial rules. GET reads are side-effect safe; monitoring is explicit; invalid Smart output is quarantined, not rewritten as a valid signal.

The owned FastAPI sidecar is packaged by PyInstaller and launched by Tauri 2 with exact child ownership. Its `monitoring_runtime_v1` worker is sidecar-resident and remains active while the window is hidden; it owns public REST/WS ingestion, bounded backoff, closed-bar cycles and alert events. The tray and native notification bridge are local desktop features. TradingView Lightweight Charts receives app-supplied bars only; it is not a backend provider. No Redis/Celery, telemetry, cloud notifier, broker, private key, account or real order is required.

## Release artifacts and historical records

- [Phase 7 completion report](docs/phase7-completion-report.md)
- [V1.0 Final Acceptance evidence inventory](docs/final-acceptance-evidence.json) — supervisor-accepted historical evidence
- [Phase 7 operations runbook](docs/phase7-operations-runbook.md)
- [Post-V1.0 Qwen Consult report](docs/post-v1-qwen-consult.md)
- [V1.1 completion report](docs/v1.1-completion-report.md)
- [V1.1 user guide](docs/v1.1-user-guide.md)
- [V1.1 screenshot inventory](docs/v1.1-screenshot-inventory.md)
- [V1.2.1 completion report](docs/v1.2.1-completion-report.md)
- [V1.2.1 user guide](docs/v1.2.1-user-guide.md)
- [V1.2.1 installer smoke](docs/v1.2.1-installer-smoke.md)
- [V1.2.1 live smoke evidence](docs/v1.2.1-live-smoke.json)
- [V1.2.1 installed live UI evidence](docs/v1.2.1-installed-live-smoke.json)
- [V1.2.1 test evidence](docs/v1.2.1-test-evidence.json)
- [V1.2.1 screenshot inventory](docs/v1.2.1-screenshot-inventory.md)
- [V1.2.1 third-party notices](docs/THIRD-PARTY-NOTICES.md)
- [Phase 7 security audit artifact](docs/phase7-security-audit.json)
- [Phase 7 license inventory artifact](docs/phase7-license-audit.json)
- [Phase 6 historical completion report](docs/phase6-completion-report.md)
- [Phase 4 historical completion report](docs/phase4-completion-report.md)

Migration history is additive and idempotent through schema 13; Phase 0-7, V1.1 and V1.2 data is preserved. The formal Phase 3 record remains `COMPLETED_WITH_ERRORS` where documented, and no zero-error or production-provider claim is inferred from fixture E2E evidence.

## Known limitations

- Public-provider availability, rate limits, network freshness, Ollama installation and GPU contention are runtime capabilities, not release guarantees.
- Binance public REST/WS is unauthenticated market data; endpoint throttling, disconnects and RSS publisher availability can make a result stale or unavailable. Reconnect/backfill and cache evidence remain visible.
- Native Windows toast visibility depends on OS notification settings. The installed application successfully requested a native notification, but this automation surface could not directly observe a durable ordinary-toast click callback. Every background alert also reaches the always-mounted in-app banner/Alert Center route, and a safe Tauri action listener remains enabled where the platform supplies an action event. Direct Windows toast-click observation is therefore an explicit acceptance limitation, not a claimed PASS.
- The live monitoring smoke uses the installed local Ollama models and records real 9B analyses, but it is not a model-quality or trading-performance guarantee. Invalid model outputs are intentionally quarantined.
- Regular equity session checks do not contain a complete exchange holiday calendar; crypto is treated as 24/7 under the accepted policy.
- Benchmark and event evidence can be unavailable or degraded when public data lacks a point-in-time capability. Memory remains local SQLite and refuses unsupported/future samples rather than generating a score.
- The browser harness uses deterministic injected data and does not measure production concurrency, live provider freshness, Qwen quality, ComfyUI contention or distributed deployment.
- The packaged build is Windows x64 and normally selects a free loopback port in `18765..18828`; `AIMA_SIDECAR_PORT` supports an exact alternate loopback port for isolated runs. An occupied port is diagnosed and skipped without port stealing. Official Tauri autostart registration and resume authorization are implemented, but both remain off by default; autostart only launches the app and never enables monitoring by itself.
- License artifacts are an inventory/review input, not legal advice. The current local environment records optional `pip-audit` as unavailable and reports unknown package license metadata instead of claiming a clean legal review.
