# AI Market Analyst V1.1

AI Market Analyst is a local-first bilingual financial research terminal for market context, deterministic signals, paper tracking and auditable operations. The local V1.1 package/API contract is `1.1.0` / Phase 7 and is pending supervisor Gate; the V1.0 Final Acceptance remains historical and accepted. It does not place orders, connect to a broker, hold private keys, send external notifications, require a cloud service, or start background work implicitly.

## Quick start (Windows)

Daily use is one double-click from the repository root:

- First time only: double-click `Prepare_AI_Models.cmd` to download the exact official `qwen3.5:4b` and `qwen3.5:9b` tags.
- Start: double-click `Start_AI_Market_Analyst.cmd`. Repeating it is idempotent and only opens the application.
- Stop or inspect: double-click `Stop_AI_Market_Analyst.cmd` or `Status_AI_Market_Analyst.cmd`.

The Start command delegates to the accepted fingerprinted launcher, waits for API/UI health and opens `http://127.0.0.1:4173`. It owns only its recorded children and never stops Ollama, ComfyUI or unrelated processes.

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

The browser is available at `http://127.0.0.1:4173`; the API health document is `http://127.0.0.1:8000/health`. Scheduler settings remain disabled by default and startup does not analyze, scan, settle, alert, materialize memory, Follow, or create PaperTrades.

The frontend language selector is available in the workspace header and supports `中文` and `English`. The selection is stored locally under `ai-market-analyst.language`, survives route changes and refreshes, and uses the browser language (`zh-*` or English fallback) when no choice is stored. Translation is presentation-only: API paths, symbols, URLs, numeric values, timestamps, database values and standard backend contracts are unchanged; provider/model free text remains evidence returned by the backend.

The Unreleased Post-V1.0 interface also includes **Qwen Consult / Qwen 咨询**. It streams responses from the server-configured local Ollama/Qwen model, supports an optional registered symbol, and stores conversation history only in the current browser tab's `sessionStorage`. Consultation is a separate read-only research surface: it does not analyze, scan, settle, Follow, create Predictions/Outcomes/PaperTrades/alerts, or place orders. Select a symbol from Asset Detail with **Consult Qwen**, or open `http://127.0.0.1:4173/consult`.

## Configuration

All runtime configuration is local and bounded. Defaults are safe for a single-user development machine.

| Variable | Default | Boundary |
| --- | --- | --- |
| `DATABASE_PATH` | `data/market_analyst.sqlite3` | Explicit `.sqlite3`, `.sqlite`, or `.db` path; existing symlink/reparse paths are rejected. |
| `MARKET_DATA_MODE` | `real` | Public-provider routing; use `fixture` only for deterministic local tests. |
| `NEWS_MODE` | `real` | RSS/public evidence or the explicit fixture test adapter. |
| `LLM_MODE` | `ollama` | Local Ollama only; `disabled` preserves a saved `WAIT`. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint; no cloud fallback. |
| `OLLAMA_MODEL` | `qwen3.5:4b` | Financial-analysis/scheduler model; retained as the Fast default. |
| `FAST_MODEL` / `SMART_MODEL` | `qwen3.5:4b` / `qwen3.5:9b` | Central V1.1 model tiers; the browser cannot provide a model name or base URL. |
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
npm run lint
npm run typecheck
npm test -- --run
npm run build
npm run audit
npm run audit:production
npm run e2e:preflight
npm run e2e
npm run screenshots:v11

cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-launcher-ownership-smoke.ps1
```

The frontend unit suite includes catalog key-parity, browser-language detection, manual switching and local persistence checks. The browser suite also verifies Chinese titles across all current routes and language persistence after navigation and refresh.

The E2E suite builds and runs the real React app and FastAPI routes against a disposable SQLite database. It uses deterministic injected providers only for browser reproducibility; it does not claim live Yahoo/Binance/Ollama/ComfyUI behavior. `scripts\phase7_audit.py --output-dir docs` records the local npm/pip checks, optional-tool availability, tracked secret scan and dependency license inventory. An unavailable `pip-audit` or an unknown license remains explicitly marked in the report.

## Architecture and safety boundary

The core flow is:

`public/fixture provider → QuantSnapshot → typed Benchmark/Event/Memory context → local Ollama (optional) → SignalProposal → durable Prediction → user-only PaperTrade → point-in-time Outcome`

Python owns instrument validation, numeric scoring, time/session/risk rules, point-in-time settlement, event clustering, memory ranking, alert identity and persistence. React is a read/interaction surface and never recalculates financial rules. GET reads are side-effect safe; explicit analysis always saves a Prediction, including `WAIT`; Follow is the only PaperTrade entry; no automatic calibration, raw-confidence mutation, final-Outcome overwrite or real order path exists.

The local scheduler is explicit and default-off, serial for model work, bounded by session/resource/backoff/cache policies, and settles outcomes before model scanning. Alerts are durable local observability with deterministic dedupe and acknowledgement only. No Redis/Celery, telemetry, cloud notifier, broker, private key, or external account is required.

## Release artifacts and historical records

- [Phase 7 completion report](docs/phase7-completion-report.md)
- [V1.0 Final Acceptance evidence inventory](docs/final-acceptance-evidence.json) — supervisor-accepted historical evidence
- [Phase 7 operations runbook](docs/phase7-operations-runbook.md)
- [Post-V1.0 Qwen Consult report](docs/post-v1-qwen-consult.md)
- [V1.1 completion report](docs/v1.1-completion-report.md)
- [V1.1 user guide](docs/v1.1-user-guide.md)
- [V1.1 screenshot inventory](docs/v1.1-screenshot-inventory.md)
- [Phase 7 security audit artifact](docs/phase7-security-audit.json)
- [Phase 7 license inventory artifact](docs/phase7-license-audit.json)
- [Phase 6 historical completion report](docs/phase6-completion-report.md)
- [Phase 4 historical completion report](docs/phase4-completion-report.md)

Migration history is additive and idempotent through schema 11; Phase 0-7 data is preserved. The formal Phase 3 record remains `COMPLETED_WITH_ERRORS` where documented, and no zero-error or production-provider claim is inferred from fixture E2E evidence.

## Known limitations

- Public-provider availability, rate limits, network freshness, Ollama installation and GPU contention are runtime capabilities, not release guarantees.
- Regular equity session checks do not contain a complete exchange holiday calendar; crypto is treated as 24/7 under the accepted policy.
- Benchmark and event evidence can be unavailable or degraded when public data lacks a point-in-time capability. Memory remains local SQLite and refuses unsupported/future samples rather than generating a score.
- The browser harness uses deterministic injected data and does not measure production concurrency, live provider freshness, Qwen quality, ComfyUI contention or distributed deployment.
- License artifacts are an inventory/review input, not legal advice. The current local environment records optional `pip-audit` as unavailable and reports unknown package license metadata instead of claiming a clean legal review.
