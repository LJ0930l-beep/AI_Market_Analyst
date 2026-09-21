# AI Market Analyst V2 — PrismML Ternary Bonsai 2 27B 架构

> **中央推理引擎**：PrismML Ternary Bonsai 2 27B PTQ1_0 (~5.95GB)
> **运行设备**：Windows 11 / NVIDIA GeForce RTX 4060 8GB (CUDA 13.4, 驱动 616.64)
> **API 协议**：本地 OpenAI-Compatible API (`http://127.0.0.1:8080/v1`)
> **设计哲学**：单一中央大模型驱动全市场；数据与推理严格解耦；严禁大模型心算指标与编造数据。

---

## 运维与操作指南 (Operations Manual)

### 1. 启动系统 (One-Click Start)
```powershell
.\start_ai_market_analyst.ps1
```
启动顺序：
1. 自动检测 RTX 4060 GPU 显存与状态。
2. 校验 `D:\RJ\models\bonsai2\Ternary-Bonsai-2-27B-PTQ1_0.gguf` 权重。
3. 后台启动 PrismML 官方 `llama-server.exe`（监听 `127.0.0.1:8080`，offload `-ngl 99`，安全 Context `8192`）。
4. 探测等待 `/health` 就绪。
5. 启动系统后端服务（端口 8000），打印完整运行状态。

### 2. 安全停止系统 (Clean Shutdown)
```powershell
.\stop_ai_market_analyst.ps1
```
安全关闭后端与 `llama-server` 进程，彻底消除僵尸进程。

### 3. 健康自检 (Health Check)
```powershell
python health.py
# 或通过专用脚本
powershell -File infra\bonsai\health.ps1
```
输出包括：模型状态、GPU 显存占用、当前 Context、注册 Tools、RAG 向量库及数据库连接状态。

### 4. 显存与上下文基准评测 (VRAM Benchmark)
```powershell
python benchmark_vram.py
```
阶梯测试 4K、8K、16K、32K 上下文的显存占用、TTFT 与生成速度，结果自动输出至 `benchmark_results.json` 和 `benchmark_report.md`。

### 5. 50 项金融与全场景评测 (Evaluation Suite)
```powershell
python evaluation/benchmark.py
```
自动运行 `evaluation/cases.json` 中的 50 项客观用例（涵盖加密推演、Pine Script、Python Quant、数学推演、Tool Calling、长文本研报、严格 JSON 格式校验）。

### 6. 修改 Context 与运行参数
打开配置文件 `infra/bonsai/config.env`：
```env
BONSAI_CTX=8192      # 推荐 8192；若显存充裕可根据 benchmark_report.md 调整
BONSAI_NGL=99        # GPU 层全量 Offload
BONSAI_KV4=1        # 启用 4-bit KV Cache 降低显存占用
```
修改后执行 `.\stop_ai_market_analyst.ps1` 然后重新 `.\start_ai_market_analyst.ps1`。

### 7. 查看日志与审计流水
- **模型服务端日志**：`logs\bonsai_server.log`
- **Tool 工具调用审计**：`logs\tools\tools_YYYY-MM-DD.jsonl`
- **后端服务日志**：`logs\backend.log`
- **下载进度与硬件指标**：`logs\model_download.json` 与 `logs\hardware.json`

### 8. 故障恢复与常见排查
- **问题：提示端口 8080 被占用**
  - 执行 `powershell -File infra\bonsai\stop_model.ps1` 彻底清理残留进程。
- **问题：CUDA Out of Memory (OOM)**
  - 确认显卡仅运行 PTQ1_0 权重（5.95GB），严禁加载 7.21GB 的 PQ2_0。
  - 在 `infra/bonsai/config.env` 中确保 `BONSAI_CTX=8192` 且 `BONSAI_KV4=1`。
- **问题：Tool 返回 `DATA_UNAVAILABLE`**
  - 系统设计原则是“不编造数据”，交易所接口网络抖动或未连接时会明确返回 `DATA_UNAVAILABLE`，稍后自动重试即可。

### 9. 模型路由与身份验证
- 全部实时 AI 推理统一经 `ModelClient` 访问 `http://127.0.0.1:8080/v1`，请求模型固定为 `Bonsai-2-27B-PTQ1_0`。
- `python scripts\prepare-ai-models.ps1` 只检查 `/health` 和 `/v1/models`，不会下载、启动服务或回退到 Ollama。Bonsai 服务由 `infra\bonsai\start_model.ps1` / `start_ai_market_analyst.ps1` 管理。
- 就绪状态必须有唯一、精确匹配的 Bonsai manifest 项；推理回执优先记录服务返回的 model id。服务未返回 model id 时，回执明确标注为绑定到本次已验证 manifest。

---

## 历史架构与业务说明


## Quick start (Windows desktop)

For a packaged install, run the generated NSIS installer:

`src-tauri\target\x86_64-pc-windows-gnu\release\bundle\nsis\AI Market Analyst_1.2.1_x64-setup.exe`

The default program directory is `%LOCALAPPDATA%\Programs\AI Market Analyst`. Launch `ai-market-analyst.exe` from the Start menu or that directory. Runtime data is kept separately under `%LOCALAPPDATA%\AI Market Analyst` (`data`, `logs`, `backups`, `runtime`) so upgrades and uninstall preserve the research ledger. The app starts its owned FastAPI sidecar on the first free loopback port in `127.0.0.1:18765..18828`, verifies the instance/ownership contract, waits for `/health`, and opens the desktop window. It diagnoses but never takes over or kills an unrelated listener. Re-launching is single-instance and focuses the existing window. The installed production runtime contains the built React assets and packaged sidecar; it does not require Vite, npm or Node.js.

Before using AI monitoring, start the local Bonsai runtime and verify its model manifest:

```powershell
.\start_ai_market_analyst.ps1
python scripts\prepare-ai-models.ps1
```

Monitoring, auto-start and resume are all off by default. Enable an individual BTC/ETH/SOL policy in the Monitoring page, save it, and press Start monitoring when you explicitly want the sidecar worker to run. Run now remains a separate one-cycle action; there is no startup scan. The tray menu can show the terminal, pause/resume monitoring, restart the owned backend, open Settings or exit the application. Window close while monitoring is active always hides to the tray and explains that work continues; when inactive it follows the close-to-tray preference. Alert Center entries can request an optional local Windows native notification; the always-mounted desktop bridge also keeps a safe in-app route when native access is unavailable.

Signals remains useful before the first AI Prediction exists: it shows deterministic public market/freshness evidence, Python trigger state and any validated monitoring opportunity for BTC/ETH/SOL plus watchlist symbols. Every card links to the explicit asset analysis workflow. Merely opening Signals does not invoke Bonsai or create a Prediction, and backend/provider failures remain labeled instead of becoming fabricated signals.

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

The interface includes **Bonsai Consult / 本地模型咨询**. It streams responses from the server-configured Bonsai ModelClient and remains read-only. It does not analyze, scan, settle, Follow, create Predictions/Outcomes/PaperTrades/alerts, or place orders.

## V1.2.1 product surface

- Public Binance crypto REST and WebSocket data covers BTCUSDT, ETHUSDT and SOLUSDT. REST responses persist bounded 15m/1h bars and freshness; the WebSocket path reconnects and backfills through REST. Python owns 15-minute bar-close exactly-once ledgering.
- Monitoring is `MonitoringPolicy` explicit opt-in. `trigger_policy_v2`, persistent trigger fingerprints, cooldowns and bounded 20/50-symbol resource limits run in Python. OpportunityAnalysis calls only the manifest-verified Bonsai 2 27B model through `ModelClient`; unavailable or invalid output is visible as degraded/quarantined evidence and never relabeled as a different model.
- The validator owns direction, entry, stop, TP1 risk/reward and WAIT safety. Valid predictions feed the existing Prediction/Outcome/Calibration records and chart annotations; invalid model output is retained as quarantine evidence.
- Lightweight Charts is a frontend-only visualization library. `/chart/{symbol}/bars` and `/chart/{symbol}/annotations` are app APIs backed by public market data and Python annotations; TradingView is not a backend data source and no proprietary chart library is redistributed.
- English RSS/news evidence keeps the full original title/summary/source and offers explicit Bonsai Chinese translation caching. `numeric_guard` rejects translations that lose structured numbers, dates, percentages or prices.
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
| `LLM_MODE` | `bonsai` | Local Bonsai only; `disabled` preserves a saved `WAIT`. |
| `BONSAI_BASE_URL` | `http://127.0.0.1:8080/v1` | Exact local OpenAI-compatible endpoint; alternate ports, paths and remote hosts fail closed. |
| `BONSAI_MODEL_NAME` | `Bonsai-2-27B-PTQ1_0` | The only accepted inference model. |
| `BONSAI_API_KEY` / `BONSAI_TIMEOUT_SEC` / `BONSAI_CTX` | local placeholder / `180` / `8192` | Local server authentication value, finite request timeout and configured context. Runtime identity is verified independently from `/v1/models`. |
| `FAST_MODEL` / `SMART_MODEL` | `Bonsai-2-27B-PTQ1_0` / same | Workload tiers both resolve to Bonsai; other overrides are reported and ignored. |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | legacy | Never used for routing; stale values are reported as rejected overrides. |
| `ALLOW_FIXTURE_FALLBACK` | `0` | Must be explicitly enabled for tests; packaged/live smoke runs with fallback disabled. |
| `MAX_MONITOR_SYMBOLS` | `50` | Finite monitoring cache/policy bound; the normal UI universe is BTC/ETH/SOL. |
| `MONITORING_MAX_CONCURRENCY` | `1` | Serial Python-owned monitoring/model execution. |
| `OLLAMA_TIMEOUT_SEC` / `OLLAMA_RETRIES` | `45` / `1` | Finite model request timeout and at most two attempts. |
| `QWEN_CONSULT_CONNECT_TIMEOUT_SEC` | `3` | Consultation connection timeout; bounded to 0.5-10 seconds. |
| `QWEN_CONSULT_FIRST_TOKEN_TIMEOUT_SEC` / `QWEN_CONSULT_STREAM_IDLE_TIMEOUT_SEC` | `20` / `20` | First-token and between-token limits; each bounded to 1-60 seconds. |
| `QWEN_CONSULT_TOTAL_TIMEOUT_SEC` | `90` | Whole consultation generation limit; bounded to 5-180 seconds. |
| `QWEN_CONSULT_MAX_BODY_BYTES` / `QWEN_CONSULT_MAX_MESSAGES` | `32768` / `24` | Request body and message-count limits. |
| `QWEN_CONSULT_MAX_MESSAGE_CHARS` / `QWEN_CONSULT_MAX_TOTAL_CHARS` | `4000` / `16000` | Per-message and complete-history character limits. |
| `QWEN_CONSULT_MAX_OUTPUT_CHARS` / `QWEN_CONSULT_MAX_OUTPUT_TOKENS` | `12000` / `700` | Output budgets are bounded by the API and explicitly passed to the Bonsai stream request. |
| `QWEN_CONSULT_RETRIES` / `QWEN_CONSULT_CONCURRENCY` | `0` / `1` | Retry only before output (maximum one); consultation model work is always serial. |
| `QWEN_CONSULT_FRESHNESS_SEC` | `3600` | Threshold used to label saved symbol evidence fresh/stale. |
| `API_CORS_ORIGINS` | local dev origins | Comma-separated bounded allowlist; wildcard credentials are rejected. |
| `API_DEBUG_ERRORS` | `false` | Production-safe API errors omit unexpected exception text. |

`GET /health/release` reports the API/package contract, schema, backup format and local-only capabilities without exposing the absolute database path. `/health/providers` and `/health/model` distinguish routing/model degradation from backend health. Model health reports the Bonsai manifest identity, Fast/Smart workload labels and deterministic route policy; it never returns the configured base URL. Startup does not invoke inference.

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

The E2E suite builds and runs the real React app and FastAPI routes against a disposable SQLite database. It uses deterministic injected providers only for browser reproducibility; it does not claim live Binance or Bonsai behavior. `scripts\v12-live-smoke.py` is the separate public REST/WS/RSS/Bonsai/monitoring check; fixture fallback is disabled. `scripts\phase7_audit.py --output-dir docs` records the local npm/pip checks, optional-tool availability, tracked secret scan and dependency license inventory. An unavailable `pip-audit` or an unknown license remains explicitly marked in the report.

## Architecture and safety boundary

The V1.2.1 core flow is:

`Public market/news evidence → bounded local cache → Python indicators and risk checks → Bonsai ModelClient (8080/v1, manifest-verified) → Python schema/risk validator → prediction, calibration and chart/alert evidence`

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

- Public-provider availability, rate limits, network freshness, Bonsai runtime health and GPU contention are runtime capabilities, not release guarantees.
- Binance public REST/WS is unauthenticated market data; endpoint throttling, disconnects and RSS publisher availability can make a result stale or unavailable. Reconnect/backfill and cache evidence remain visible.
- Native Windows toast visibility depends on OS notification settings. The installed application successfully requested a native notification, but this automation surface could not directly observe a durable ordinary-toast click callback. Every background alert also reaches the always-mounted in-app banner/Alert Center route, and a safe Tauri action listener remains enabled where the platform supplies an action event. Direct Windows toast-click observation is therefore an explicit acceptance limitation, not a claimed PASS.
- The live monitoring smoke uses the verified local Bonsai model, but it is not a model-quality or trading-performance guarantee. Invalid model outputs are intentionally quarantined.
- Regular equity session checks do not contain a complete exchange holiday calendar; crypto is treated as 24/7 under the accepted policy.
- Benchmark and event evidence can be unavailable or degraded when public data lacks a point-in-time capability. Memory remains local SQLite and refuses unsupported/future samples rather than generating a score.
- The browser harness uses deterministic injected data and does not measure production concurrency, live provider freshness, Bonsai quality, ComfyUI contention or distributed deployment.
- The packaged build is Windows x64 and normally selects a free loopback port in `18765..18828`; `AIMA_SIDECAR_PORT` supports an exact alternate loopback port for isolated runs. An occupied port is diagnosed and skipped without port stealing. Official Tauri autostart registration and resume authorization are implemented, but both remain off by default; autostart only launches the app and never enables monitoring by itself.
- License artifacts are an inventory/review input, not legal advice. The current local environment records optional `pip-audit` as unavailable and reports unknown package license metadata instead of claiming a clean legal review.
