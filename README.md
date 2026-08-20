# AI Market Analyst V1.0

AI Market Analyst is a local-first research ledger for market context, deterministic signals, paper tracking and auditable operations. The released package/API contract is `1.0.0` / Phase 7. It does not place orders, connect to a broker, hold private keys, send external notifications, require a cloud service, or start background work implicitly.

## Quick start (Windows)

From the repository root:

```powershell
python -m pip install -e ".[api,market,dev]"
cd web
npm ci
npm run build
cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action start
```

The launcher binds the API and built UI to loopback (`127.0.0.1`), checks Python/uvicorn/Node/npm, checks the database path and ports, and writes only owned-child state under the OS temporary directory. It never searches by process name or stops ComfyUI, Ollama, or another unrelated process. If the build is missing, `start` builds it; use `-Build` to force a rebuild.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action stop
```

The browser is available at `http://127.0.0.1:4173`; the API health document is `http://127.0.0.1:8000/health`. Scheduler settings remain disabled by default and startup does not analyze, scan, settle, alert, materialize memory, Follow, or create PaperTrades.

## Configuration

All runtime configuration is local and bounded. Defaults are safe for a single-user development machine.

| Variable | Default | Boundary |
| --- | --- | --- |
| `DATABASE_PATH` | `data/market_analyst.sqlite3` | Explicit `.sqlite3`, `.sqlite`, or `.db` path; existing symlink/reparse paths are rejected. |
| `MARKET_DATA_MODE` | `real` | Public-provider routing; use `fixture` only for deterministic local tests. |
| `NEWS_MODE` | `real` | RSS/public evidence or the explicit fixture test adapter. |
| `LLM_MODE` | `ollama` | Local Ollama only; `disabled` preserves a saved `WAIT`. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint; no cloud fallback. |
| `OLLAMA_MODEL` | `qwen3.5:4b` | Local model identifier; Phase 2-6 bounded timeout/output/retry policy remains active. |
| `OLLAMA_TIMEOUT_SEC` / `OLLAMA_RETRIES` | `45` / `1` | Finite model request timeout and at most two attempts. |
| `API_CORS_ORIGINS` | local dev origins | Comma-separated bounded allowlist; wildcard credentials are rejected. |
| `API_DEBUG_ERRORS` | `false` | Production-safe API errors omit unexpected exception text. |

`GET /health/release` reports the API/package contract, schema, backup format and local-only capabilities without exposing the absolute database path. `/health/providers` and `/health/model` distinguish routing/model degradation from backend health.

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

Each artifact contains `database.sqlite3` and `manifest.json` with `phase7_backup_v1`, app version, schema version, UTC creation time, source basename, SHA-256 and table-count evidence. Restore validates the manifest, checksum, SQLite integrity and supported schema before touching the target. An existing target first receives a sibling `*.pre-restore-<UTC>-<nonce>` safety artifact; replacement is staged and atomic. Invalid, tampered, self-referential, symlinked, broad or path-confused targets are rejected. Safety artifacts are retained for operator recovery and no recursive delete is used.

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
```

The E2E suite builds and runs the real React app and FastAPI routes against a disposable SQLite database. It uses deterministic injected providers only for browser reproducibility; it does not claim live Yahoo/Binance/Ollama/ComfyUI behavior. `scripts\phase7_audit.py --output-dir docs` records the local npm/pip checks, optional-tool availability, tracked secret scan and dependency license inventory. An unavailable `pip-audit` or an unknown license remains explicitly marked in the report.

## Architecture and safety boundary

The core flow is:

`public/fixture provider → QuantSnapshot → typed Benchmark/Event/Memory context → local Ollama (optional) → SignalProposal → durable Prediction → user-only PaperTrade → point-in-time Outcome`

Python owns instrument validation, numeric scoring, time/session/risk rules, point-in-time settlement, event clustering, memory ranking, alert identity and persistence. React is a read/interaction surface and never recalculates financial rules. GET reads are side-effect safe; explicit analysis always saves a Prediction, including `WAIT`; Follow is the only PaperTrade entry; no automatic calibration, raw-confidence mutation, final-Outcome overwrite or real order path exists.

The local scheduler is explicit and default-off, serial for model work, bounded by session/resource/backoff/cache policies, and settles outcomes before model scanning. Alerts are durable local observability with deterministic dedupe and acknowledgement only. No Redis/Celery, telemetry, cloud notifier, broker, private key, or external account is required.

## Release artifacts and historical records

- [Phase 7 completion report](docs/phase7-completion-report.md)
- [V1.0 Final Acceptance evidence inventory](docs/final-acceptance-evidence.json) — developer-ready and pending supervisor final Gate
- [Phase 7 operations runbook](docs/phase7-operations-runbook.md)
- [Phase 7 security audit artifact](docs/phase7-security-audit.json)
- [Phase 7 license inventory artifact](docs/phase7-license-audit.json)
- [Phase 6 historical completion report](docs/phase6-completion-report.md)
- [Phase 4 historical completion report](docs/phase4-completion-report.md)

Migration history is additive and idempotent through schema 10; Phase 0-6 data is preserved. The formal Phase 3 record remains `COMPLETED_WITH_ERRORS` where documented, and no zero-error or production-provider claim is inferred from fixture E2E evidence.

## Known limitations

- Public-provider availability, rate limits, network freshness, Ollama installation and GPU contention are runtime capabilities, not release guarantees.
- Regular equity session checks do not contain a complete exchange holiday calendar; crypto is treated as 24/7 under the accepted policy.
- Benchmark and event evidence can be unavailable or degraded when public data lacks a point-in-time capability. Memory remains local SQLite and refuses unsupported/future samples rather than generating a score.
- The browser harness uses deterministic injected data and does not measure production concurrency, live provider freshness, Qwen quality, ComfyUI contention or distributed deployment.
- License artifacts are an inventory/review input, not legal advice. The current local environment records optional `pip-audit` as unavailable and reports unknown package license metadata instead of claiming a clean legal review.
