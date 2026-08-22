# Phase 7 local operations runbook

This runbook covers the supported Windows local lifecycle through V1.1. It owns only the API and static UI child processes started by `scripts\phase7-local.ps1`.

## Start, status and stop

For normal use, double-click `Start_AI_Market_Analyst.cmd`. First-time model preparation is `Prepare_AI_Models.cmd`; status and stop wrappers are also provided. These wrappers only delegate to the safe launcher and exact Ollama model pulls.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action start
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action status
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\phase7-local.ps1 -Action stop
```

Use `-DatabasePath`, `-ApiPort` and `-WebPort` for an explicit local instance. `start` validates Python/uvicorn/Node/npm, creates only the selected database parent, refuses occupied ports, builds a missing UI, waits for `/health`, and then starts the static proxy. It never enables the scheduler or invokes analysis. The v2 state records each child’s executable, exact UTC start-time ticks, command-line hash, role and port markers. `status` reports `ownership_mismatch`; `stop` refuses to act and retains the state if any fingerprint differs. Otherwise it checks the recorded PID and fingerprint before a graceful close and bounded owned-process fallback; it does not use process names, `taskkill`, recursive deletion, or a broad port kill. Child logs and the state file are under `%TEMP%\ai-market-analyst-phase7-launcher`.

If a port is occupied, choose another port or stop the process through its owner. Do not use this launcher to stop an unrelated service. If the state file is malformed, inspect it and remove only that one state file after confirming the recorded children are gone.

## Database backup and restore

Use the artifact CLI, never a raw copy of a live database:

```powershell
python -B scripts\phase7_backup.py backup --database data\market_analyst.sqlite3 --output data\backups\before-maintenance
python -B scripts\phase7_backup.py restore --input data\backups\before-maintenance --database data\market_analyst.sqlite3
```

The backup directory must be new/empty and the target parent must already exist. The manifest records format `phase7_backup_v1`, app version, schema, UTC time, checksum and domain counts. Restore is offline-only: stop the owned launcher, close every database connection, and confirm there is no target `-wal` or `-shm` sidecar before invoking it. A sidecar or persisted WAL journal mode is rejected before safety-backup creation or target mutation; no live checkpoint is attempted. Restore performs manifest/checksum/integrity/schema validation before making a sibling safety artifact for an existing target. It stages and atomically replaces the target, retaining the safety artifact. A failure before replacement leaves the target untouched; a post-replacement validation failure recovers an existing target from its safety artifact, while a previously absent target is moved to an exact `*.restore-failed-*` quarantine (or removed) so it does not remain partially accepted. Do not manually delete safety or failure artifacts until the restored database has been reopened and verified.

## Health and degraded operation

Check:

- `GET /health` for API contract and explicit no-real-order/private-key flags.
- `GET /health/release` for schema, backup format, local-only capabilities and redacted database identity.
- `GET /health/providers` for public routing and deferred news probe capability.
- `GET /health/model` for local Ollama availability; error details are redacted.
- `GET /scheduler/status` for default-off lifecycle, session, resource, cache and bounded backoff state.

Provider, model, GPU and database failures are capability/degraded evidence. A provider outage does not fabricate bars, context, prediction confidence or outcomes. A resource guard blocks model scanning with bounded backoff but does not stop ComfyUI/Ollama. Pure settlement has its own provider evidence and remains independent of the model GPU guard.

### Post-V1.0 Qwen consultation

`GET /health/model` reports the redacted `qwen_consult_v2` capability, deterministic `qwen_route_v1` policy and configured Fast/Smart model IDs. It does not expose `OLLAMA_BASE_URL`; startup and `GET /health/release` do not invoke Qwen. The browser's **AI Assistant / Qwen 咨询** page streams through `POST /consult/stream`. Availability depends on loopback Ollama and the selected routed model. A missing model is an honest unavailable state; Auto routing never invents a response or silently changes a user-selected tier.

Consult requests are serial and bounded by the `QWEN_CONSULT_*` limits documented in the README. Stop cancels only the current HTTP/model request; it does not stop Ollama or any process. Clear removes only this tab's browser session conversation. Chat text is not a database/backup artifact and should not appear in project logs. Optional symbol context reads existing saved evidence only and does not trigger a provider fetch, analysis, scheduler, settlement, Follow or financial write.

## Recovery checklist

1. Stop only the owned local instance and preserve its logs; do not restore while the launcher or any database connection is active.
2. Confirm the target has no `-wal` or `-shm` sidecar and is not in persisted WAL mode; the restore command fails closed otherwise.
3. Make a fresh SQLite backup artifact if the source opens and passes health.
4. If corruption is suspected, restore the last validated artifact to a new filename first, reopen it with `SQLiteStore.initialize()`, and compare `/stats` plus the manifest counts.
5. Restore over the inactive target only after the validation step; keep the generated `*.pre-restore-*` or `*.restore-failed-*` artifact.
6. Restart the launcher and verify `/health/release`, scheduler disabled state, Watchlist, Predictions, Outcomes, PaperTrades and alerts.
7. Do not infer live-provider/model availability from a successful database restore.

## Privacy and ownership

Database paths are redacted in health responses. Logs do not intentionally include secrets or raw model context. The product is local single-user software; no telemetry, cloud queue, external notifier, broker, private-key or real-money permission exists. External news/model text is untrusted evidence and is not executed as configuration.
