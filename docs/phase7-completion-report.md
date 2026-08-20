# Phase 7 completion report — developer-ready V1.0 release hardening

Status: `DEVELOPER_READY_PENDING_SUPERVISOR_FINAL_GATE`

This report covers the bounded Phase 7 release work on the accepted Phase 0-6 baseline. No Phase 8 work is included.

## Scope and architecture boundary

Phase 7 adds:

- package/API version `1.0.0` / API Phase `7`, with truthful local capability health at `GET /health/release`;
- bounded local configuration defaults, loopback CORS/host policy, redacted unexpected/model errors and explicit local-only capability flags;
- `core/backup.py` and `scripts/phase7_backup.py`, using SQLite’s online backup API, manifest/checksum/schema/count validation, atomic staged restore and retained sibling safety artifacts;
- Windows `scripts/phase7-local.ps1` plus `web` `start:local`/`stop:local`/`status:local`/audit scripts for owned API/UI child lifecycle, with v2 start-time/command-line/role/port ownership fingerprints;
- reproducible security/license summaries (`scripts/phase7_audit.py`) and a bounded backup/resource release smoke (`scripts/phase7-release-smoke.py`);
- final README, operations runbook, this report and machine-readable acceptance inventory.

The architecture remains local-first: public/fixture data → deterministic Python context and rules → optional local Ollama → saved Prediction → user-only PaperTrade → deterministic point-in-time Outcome. No LLM computes numeric/risk/time rules. GET reads remain side-effect safe. Scheduler startup is default-off and explicit; it does not analyze, settle, alert, Follow or materialize memory on process startup. No cloud queue, Redis/Celery, broker, real order, private key, external notification or telemetry path was added.

## Backup/restore evidence

`tests/test_phase7_hardening.py` covers consistent backup/reopen/count preservation, tampered artifact rejection before target mutation, target-replace failure with retained safety artifact, target sidecar and persisted-WAL refusal before safety-backup creation, existing-target recovery after post-replacement validation failure, new-target failure quarantine, self-target rejection, empty/broad destination rejection and symlink rejection logic. The symlink case is safely skipped on this Windows account because symlink creation is unavailable; the runtime path guard remains covered by the implementation and non-symlink path checks.

Artifacts contain only `database.sqlite3` and `manifest.json`. The manifest records `phase7_backup_v1`, app version, current schema `10`, UTC timestamp, source basename, SHA-256 and table counts. Restore is offline-only and rejects target `-wal`/`-shm` sidecars or persisted WAL mode before creating a safety backup or replacing the target. Existing targets recover from their retained safety artifact after a post-replacement validation failure; a previously absent failed target is quarantined as `*.restore-failed-*` or removed only as that exact file. No recursive delete is used.

## Startup/resource evidence

The real Windows smoke used:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action start -DatabasePath <temp>\ai-market-analyst-phase7-launcher-smoke.sqlite3 -ApiPort 18000 -WebPort 14173
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action status -ApiPort 18000 -WebPort 14173
Invoke-RestMethod http://127.0.0.1:18000/health/release
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:14173/
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action stop -ApiPort 18000 -WebPort 14173
```

Observed: API/UI reached ready state, `/health/release` returned Phase 7 / `1.0.0` / schema 10, the UI returned HTTP 200, status showed both fingerprinted owned PIDs, and stop released both ports. `scripts\phase7-launcher-ownership-smoke.ps1` additionally passed stale same-executable PID refusal, state retention and normal lifecycle cleanup. The launcher does not kill unrelated processes. The bounded release smoke ran on Windows 11, Python 3.12.10, 12 logical CPUs, temporary fixture SQLite, no network/model/orders; the recorded backup/restore timings and read-only `nvidia-smi` capability result are in [phase7-release-smoke.json](phase7-release-smoke.json). The current desktop GPU guard reported `gpu_competition` with bounded retry evidence; this is capability evidence, not a performance threshold or a request to stop any process.

## Exact automated evidence

The final local Gate commands and observed results are:

| Area | Command | Result |
| --- | --- | --- |
| Python regression | `python -m pytest -q` | PASS — 117 passed, 1 skipped, 10 subtests, one known Starlette/httpx deprecation warning; the symlink-specific hardening test is a safe environment skip. |
| Python unittest | `python -B -m unittest discover -s tests -v` | PASS — 118 tests, 1 skipped. |
| Python syntax | `python -B -m compileall -q apps core tests scripts` | PASS. |
| Python dependencies | `python -m pip check` | PASS — no broken requirements. |
| Frontend lint | `npm run lint` | PASS. |
| Frontend types | `npm run typecheck` | PASS. |
| Frontend unit | `npm test -- --run` | PASS — 12 files / 55 tests. |
| Frontend build | `npm run build` | PASS — Vite 6.4.3. |
| npm full audit | `npm audit --audit-level=high` | PASS — 0 vulnerabilities. |
| npm production audit | `npm audit --omit=dev --audit-level=high` | PASS — 0 vulnerabilities. |
| Browser preflight | `npm run e2e:preflight` | PASS — pinned Playwright 1.62.1, installed Chrome/Edge candidates. |
| Standalone browser Gate | `npm run e2e` | PASS — 10/10, one worker, 39.0s, Chrome `151.0.7922.140`, real built React + FastAPI/SQLite; 18 desktop/mobile axe scans and 18 overflow checks. |
| Launcher ownership smoke | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/phase7-launcher-ownership-smoke.ps1` | PASS — stale same-executable record refused and retained, helper stayed alive, normal start/stop lifecycle passed. |
| Release smoke | `python -B scripts/phase7-release-smoke.py --output docs/phase7-release-smoke.json` | PASS — temporary SQLite backup `14.55ms`, restore `22.349ms`, read-only resource probe `96.93ms`; no threshold claim; GPU reported bounded `gpu_competition`. |
| Security/license artifacts | `python -B scripts/phase7_audit.py --output-dir docs` | PASS for pip check/npm audits/secret scan; `pip-audit` unavailable and license inventory `review_required` are preserved honestly. |
| Diff hygiene | `git diff --check` | PASS; only expected repository LF/CRLF conversion warnings may be emitted by Git on Windows. |

The browser suite retains the accepted Phase 4-6 side-effect assertions: explicit analysis saves WAIT, same-tick Follow is exactly-once PaperTrade, settlement/Radar/Performance/Alert reads do not mutate financial records, deep links serve HTML while `/api/health` remains JSON, and all checked routes pass accessibility/390px overflow checks. Existing Phase 3 replay/calibration and Phase 5 scheduler/settlement/Radar/Alert tests remain in the full counts.

## Security, dependency and privacy evidence

`docs/phase7-security-audit.json` records npm full/production vulnerability totals of zero, `pip check` success, optional `pip-audit` unavailable, and a scoped-source private-key/literal-secret scan with zero findings for the stated patterns. `docs/phase7-license-audit.json` inventories 338 installed npm lock packages and the declared Python optional dependencies. It records 51 unknown npm license metadata entries, uninstalled `yfinance`, and Python distributions with missing license metadata as `review_required`; it makes no legal compatibility or clean-license claim.

## Honest limitations and residual risks

- This local Gate does not prove live Yahoo/Binance freshness, Ollama/Qwen quality, ComfyUI production contention, exchange holiday completeness or distributed deployment.
- The GPU smoke observed other desktop compute processes and therefore reported conservative competition; no process was killed or altered. Injected scheduler tests cover parser/failure/backoff behavior without using real GPU state.
- Public-provider outages, missing optional packages and model unavailability remain explicit degraded capabilities. Fixture browser data is not provider compatibility proof.
- License review requires human/legal follow-up for unknown metadata; `pip-audit` should be installed and rerun in the release environment when available.
- The final acceptance inventory is developer-ready only. Sol/supervisor must perform the final Gate acceptance.
