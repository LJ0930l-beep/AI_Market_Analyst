# AI Market Analyst Decisions

## ADR-001 - Verified repository state supersedes stale phase status in source document

Date: 2026-08-15

The v2.0 source document says Phase 3 is not implemented. The repository now contains a completed Phase 3 implementation, tests and a 300-sample real-Qwen replay generated after that baseline. Phase 3 is therefore treated as PASS with recorded errors, and execution starts at Phase 4. Product intent and all Phase 3 integrity constraints remain authoritative.

## ADR-002 - Sol supervises; one persistent luna-max thread implements

Date: 2026-08-15

All code implementation and repair from Phase 4 onward is delegated serially to `luna-max`. Sol owns planning, bounded research, acceptance commands, review, decisions and final acceptance. No parallel writers are allowed in the shared working tree.

## ADR-003 - Preserve FastAPI backend and add React + TypeScript frontend

Date: 2026-08-15

Phase 4 will extend the existing optional FastAPI application and add a React + TypeScript frontend. Backend financial rules remain in Python. The UI does not duplicate validator, time, risk, performance or calibration logic.

## ADR-004 - No broad Phase 1-3 refactor

Date: 2026-08-15

New work should extend existing providers, storage and services. Broad rewrites for framework or directory preference are rejected unless verified evidence shows the existing architecture cannot satisfy a Gate.

## ADR-005 - Phase 3 model errors remain visible

Date: 2026-08-15

The 45 final `repair_failed` replay samples remain part of the audit trail and invalid-count reporting. They are excluded from valid performance calculations but are not deleted or relabeled. Future quality work may improve new runs without rewriting historical results.

## ADR-006 - Stable API contract before frontend implementation

Date: 2026-08-15

Phase 4 uses an injectable FastAPI app factory while preserving existing successful endpoint shapes. New failures use structured error codes. UI read paths are backed by explicit SQLite query helpers, and Follow remains paper-only, idempotent and limited to non-expired LONG/SHORT Predictions. This contract is accepted before creating the React frontend.

## ADR-007 - Research-ledger frontend identity

Date: 2026-08-15

The local UI uses a restrained research-ledger identity rather than a broker terminal or generic dashboard. Mineral/paper surfaces, ruled structure, local system fonts and a reusable time/provenance rail make data age and signal validity visible. The interface avoids fake data, gradients, trading language and decorative motion; responsive and reduced-motion behavior are part of the foundation.

## ADR-008 - Operational health and performance evidence remain separate

Date: 2026-08-15

Dashboard panels query backend, provider, model, database counts and performance independently so one failure does not erase unrelated evidence. Backend connectivity never implies model or provider availability. For performance, `metrics.resolved_actionable` is the only resolved-sample count; total Prediction or actionable counts cannot be presented as resolved outcomes. Missing or zero resolved samples remain visibly degraded and PRELIMINARY.

## ADR-009 - Asset reads cannot create Predictions

Date: 2026-08-15

`GET /instruments/{symbol}/snapshot` is a strictly read-only provider-plus-quant path. It may return actual OHLCV, quote, deterministic indicators and provider freshness, but it cannot invoke news, a model, Signal construction or persistence. Only the explicit `POST /analysis/{symbol}` workflow may create a Prediction, including a WAIT Prediction.

## ADR-010 - Asset analysis is explicit and Signal rendering is allowlisted

Date: 2026-08-15

Asset Detail loads snapshot and news evidence without analysis side effects. A Prediction is created only after the user explicitly runs analysis. The Signal Card renders an allowlist of typed product fields and preserves raw confidence exactly; raw model responses and serialized context never enter the DOM. WAIT remains a saved Prediction for coverage and auditability, but has no entry, stop, targets, Follow control, PaperTrade or broker implication.

## ADR-011 - Development API traffic is namespaced under `/api`

Date: 2026-08-19

Vite development traffic to the FastAPI backend uses the `/api` prefix and rewrites that prefix at the proxy boundary. Product routes such as `/predictions`, `/paper-trades`, `/performance` and `/replay` therefore remain direct-loadable SPA routes instead of colliding with backend collection endpoints. Explicit API-base configuration remains supported, and production keeps same-origin behavior.

## ADR-012 - Follow uses a synchronous client lock and server authority

Date: 2026-08-19

The browser sets an immediate in-flight lock before starting a Follow request so same-tick double actions cannot emit duplicate requests. Selection changes and unmounts abort stale work. The FastAPI Follow contract remains the authoritative paper-only and idempotent boundary; the client guard improves interaction safety but is not treated as the source of truth.

## ADR-013 - Phase 4 Watchlist and Replay Lab are capability-honest read surfaces

Date: 2026-08-19

Before Phase 5, Watchlist is a searchable view of the backend instrument roster and does not persist membership or claim ranking, scans or alerts. Replay Lab uses only replay GET endpoints and does not expose the API request-record creation endpoint because that endpoint stores PENDING CLI metadata without starting a worker. PENDING and RUNNING are displayed as returned state, while replay execution remains an explicit CLI workflow.

## ADR-014 - Phase 4 Gate uses disposable real-browser infrastructure

Date: 2026-08-20

Phase 4 user-flow acceptance runs the production React build against real local FastAPI routes and a disposable SQLite database seeded through existing domain/storage APIs. Browser responses are not mocked. The harness is serial, local and paper-only; cleanup is restricted to a recognized direct child of the operating-system temporary directory, and the formal Phase 3 database is never opened or mutated.

## ADR-015 - Watchlist persistence is canonical first and settings remain inert

Date: 2026-08-20

Phase 5 begins with an idempotent v5 migration for Watchlist membership and typed local scheduler-resource settings. P5-T01a accepts only symbols resolved by the existing canonical registry; public-provider-compatible expansion is isolated in P5-T01b so network validation cannot weaken migration or CRUD safety. Persisted settings do not activate a scheduler, scan, ranking, alert or execution behavior by themselves.

## ADR-016 - Public instrument registration is strict and provider-validated

Date: 2026-08-20

P5-T01b accepts only strictly parsed public equity candidates or unambiguous Binance-compatible USDT spot candidates. Equity validation uses Yahoo public market data and crypto validation uses Binance public spot data, with bounded timeout/retry behavior; fixture or stale provider data is never compatibility proof. Expansion candidates are persisted in the existing `instruments` table only after a successful unambiguous probe, while failed or ambiguous probes do not persist. Registered expansion metadata remains explicitly labeled `inferred` or `unknown` rather than inventing exchange, sector or other descriptive facts.

## ADR-017 - Opportunity Radar is deterministic, auditable and read-only

Date: 2026-08-20

P5-T02 uses the versioned `opportunity_v1` deterministic Python scorer. Explicit weights, normalized component scores and per-component contributions are returned for audit; no LLM calculates or orders the score. Actionable ranking requires an ACTIVE Phase 3 calibration artifact with an effective sample count of at least 100 and compatible scope. WAIT is fixed `WAIT` and never ranked; incomplete evidence is `NOT_RANKED`; invalid or expired signals are `AVOID`.

Radar reads only durable Watchlist membership, each symbol's latest existing Prediction/Outcome, current calibration and saved context. GET `/radar` has no analysis, model, Prediction, PaperTrade, scheduler, scan, alert or broker side effect. Stored `time_policy.event_risk=true` and high-importance `risk_events` take precedence over news evidence; unavailable event/news evidence remains explicit and conservative.

## ADR-018 - P5-T03a uses a safe, explicit local scheduler lifecycle

Date: 2026-08-20

The P5-T03a scheduler is disabled by default and runs only through an explicit lifecycle. Model analysis has a hard effective concurrency limit of one. Equity session policy is deterministic weekday regular-hours in the instrument timezone; Crypto is 24/7, and the missing exchange holiday calendar is exposed as a limitation.

Scheduler context caching uses version `context_cache_v1` with a key containing symbol, timeframe, as-of, provider and context capability. The resource guard is read-only and bounded, using `nvidia-smi` when available; missing or failed probes are unavailable rather than assumed healthy. Resource failures use bounded persisted backoff. A process-wide lease keyed by canonical SQLite path prevents duplicate runtimes, and cooperative stop finishes at most the current analysis before marking remaining items interrupted.

This foundation does not introduce Redis/Celery, process-killing behavior, alerts, outcome settlement, broker connectivity or real orders.

## ADR-019 - P5-T03b uses point-in-time, idempotent background settlement

Date: 2026-08-20

Live settlement is evaluated in Python at a supplied `as_of`, using only bars after `generated_at` and no later than that point. WAIT Predictions settle to `NOT_ACTIONABLE`; they never count as wins or losses, create R/R evidence or create PaperTrades. Final Outcomes are immutable and saved idempotently, so retries and concurrent/repeated runs cannot overwrite an existing result.

Public-provider bars are batched and reused by symbol/timeframe within a bounded settlement round. Provider, `as_of`, capability and error evidence are persisted. Provider failures, malformed SignalProposal payloads and evaluation failures are isolated with bounded retry/quarantine evidence rather than producing fabricated Outcomes. Settlement runs before model scanning and is independent of the model GPU/resource guard; provider availability still controls whether a settlement item can complete.

Live Performance snapshots use only real live-record dimensions for filtering, reuse equivalent snapshots when metrics have not changed, and apply bounded retention. Settlement does not retrain or activate calibration, alter raw confidence, or mutate PaperTrade records.

## ADR-020 - P5-T04 is a local, durable and deterministic Alert Center

Date: 2026-08-20

P5-T04 adds SQLite schema migration v8 with an `alerts` ledger. `alert_policy_v1` makes event identity, fingerprint, source, severity, evidence and UTC first/last-seen timestamps auditable and immutable after creation; acknowledgement status, actor and timestamp are the only mutable event fields. Prediction IDs, final Outcome identity/status, Radar category transitions and normalized stored news/event evidence distinguish materially new events. Repeated provider/resource/model operational failures coalesce by stage, symbol, failure and 15-minute UTC cooldown window with occurrence counts.

The scheduler reconciles alerts only after its settlement and Watchlist scan stages, with per-record/stage isolation. Supported sources are actionable live Predictions, newly visible final actionable Outcomes, meaningful read-only Radar transitions, provider/resource failures and stored Prediction context `news`/`risk_events`. Event-risk evidence gives `time_policy.event_risk=true` and `risk_events.importance >= 70` explicit priority; no external news fetch is performed by the alert layer, so absent stored context remains a capability limitation rather than fabricated evidence.

Retention is capped at 500 rows. Open/unacknowledged rows are retained ahead of acknowledged history; oldest acknowledged rows are pruned first, and old open rows are pruned only when necessary to respect the hard cap. API reads are bounded/filterable and side-effect free; single-alert acknowledgement is strict, idempotent and local. The UI exposes severity/source/status/evidence and keyboard-accessible acknowledgement without any outbound notifier, cloud service, Redis/Celery worker, broker/order path or PaperTrade mutation. Reconciliation never changes raw or calibrated confidence, calibration artifacts, Outcomes or Prediction records.

Phase 5 implementation evidence is supervisor-accepted. Phase 6 is the active scope; this ADR does not authorize Phase 7 implementation.

## ADR-021 - Phase 5 acceptance is the Phase 6 compatibility baseline

Date: 2026-08-20

Phase 5 is accepted on independent evidence of 99 Python tests plus 10 subtests, 55 frontend tests, frontend lint/typecheck/build, Python compileall and `pip check`, zero vulnerabilities in full and production npm audits, standalone E2E 10/10 with 18 desktop/mobile axe and overflow checks, and clean `git diff --check`. No repair defects were found.

Phase 6 must preserve the accepted default-off scheduler lifecycle, serial model/resource behavior, point-in-time settlement, read-only Radar and local Alert Center, as well as the no-cloud/no-queue/no-broker/no-real-order/no-private-key/no-automatic-calibration boundaries. Phase 7 remains out of scope until Phase 6 is complete.

## ADR-022 - Phase 6 context intelligence is typed, point-in-time and deterministic

Date: 2026-08-20

Phase 6 adds additive SQLite migration v10 and API/package contract `phase=6` / `0.6.0`. Benchmark metadata uses explicit versioned public mappings (`SPY`, `QQQ`, `SOXX`, and a BTCUSDT crypto baseline); Python computes relative performance/strength and returns provider, timeframe, independent target/benchmark freshness, `as_of` and capability provenance. Crypto total-market and dominance context remain explicitly unavailable rather than fabricated.

Event evidence is typed with source/category, affected symbols, event/published/known/retrieved/revision timestamps, importance, credibility, primary-source flag, normalized identity and capability. Point-in-time selection excludes evidence not known/published/revised by the requested `as_of`. Deterministic clustering preserves source disagreement and provenance; versioned credibility prefers official/primary evidence without turning a weak single source into consensus. Major-event TimePolicy effects remain Python-owned and constrain validity/holding/re-evaluation; the LLM only receives supplied structured context and cannot calculate or override these rules.

Market Memory uses `market_memory_v1` / `feature_representation_v1`, fixed Python feature distance, deterministic tie-breaking, bounded SQLite retention and an explicit minimum resolved-sample gate. Query and materialization exclude future generated/data/context boundaries, outcomes after `as_of`, self-matches and incomplete feature records; insufficient evidence is preliminary/unavailable. GET context routes are read-only; only the documented explicit `/memory/materialize` command persists feature evidence. Memory cannot mutate raw or calibrated confidence, calibration artifacts, Outcomes, PaperTrades or deterministic risk/time rules.

The Phase 6 browser harness remains local and disposable: real built React plus FastAPI/SQLite routes, injected deterministic providers for repeatability, no browser network mocks, no cloud/Redis/Celery/notifier/broker/order/private-key integration. Historical event-calendar/revision coverage, live provider freshness, crypto total/dominance data, production Qwen/GPU contention and exchange holiday calendars remain capability limitations. This ADR records developer Gate completion for supervisor review and does not authorize Phase 7 implementation.

## ADR-023 - Phase 6 repair: conservative freshness, source identity and auditable Memory cohorts

Date: 2026-08-20

The Phase 6 repair keeps API/package contract `phase=6` / `0.6.0` and adds additive SQLite migration v10. Benchmark freshness is calculated independently for target and benchmark bars after future-bar exclusion; a required stale side makes aggregate context `stale`, while an unusable side remains `unavailable`. Both timestamps, ages and side statuses are retained in the response.

Event clustering uses a deterministic normalized publisher identity derived from the source field, never the article URL. Same-publisher reports with different URLs remain one source and cannot create confirmed consensus alone. Independent source identities can confirm; primary/official evidence may confirm under the existing explicit rule. Every URL and source record remains in cluster provenance.

Market Memory defines one cohort as the deterministic eligible ranking truncated to `top_k`; resolved count, win rate, average R and typical outcomes are computed only from that displayed cohort. Broader eligible count remains diagnostic and cannot make a distant analogue produce READY statistics. Explicit materialization stores generated time, symbol, timeframe, source type, feature snapshot `as_of` and outcome-known boundary. Reads prefer valid materialized rows at or before the query boundary, deduplicate repeated snapshots by prediction, and report the exact raw prediction-ledger fallback when no valid materialized cohort exists; the fallback is not claimed to obey the 1,000-row materialized retention bound. GETs remain read-only and no context feature changes confidence, calibration, Outcomes or PaperTrades.

## ADR-024 - Phase 6 supervisor acceptance is the Phase 7 release-hardening baseline

Date: 2026-08-20

Phase 6 is supervisor-accepted on independent final evidence: 106 pytest tests plus 10 subtests, 55 frontend tests, lint/typecheck/build/compileall/`pip check` PASS, full and production npm audits with zero vulnerabilities, standalone E2E 10/10 in 52.6 seconds with 18 desktop/mobile axe and overflow checks, and clean `git diff --check`. Phase 7 must preserve the Phase 6 API/package contract `phase=6` / `0.6.0` until the final release-version change is verified.

Phase 7 is limited to local security/dependency/license/privacy/configuration hardening, SQLite-consistent backup/restore, safe one-command lifecycle/resource health, resilience/release evidence and V1.0 documentation. It does not authorize cloud runtime, telemetry, external notification, Broker/order/private-key paths, automatic analysis/materialization/Follow/alerts at startup, scheduler default-on behavior, or Phase 8/post-V1.0 features. Final acceptance remains developer-ready until Sol/supervisor reviews the recorded inventory.

## ADR-025 - Phase 7 V1.0 release boundary and recovery contract

Date: 2026-08-20

Phase 7 is developer-complete and ready for supervisor final acceptance. The package/API contract is `1.0.0` / Phase 7 while Phase 0-6 historical reports retain their original version evidence. Runtime defaults remain loopback/local-only, scheduler disabled, explicit lifecycle, bounded timeouts/retries/paths and redacted unexpected/model errors. No cloud runtime, telemetry, external notifier, Redis/Celery, broker/order client, private key or real-money path is present.

SQLite release backups are explicit `phase7_backup_v1` artifacts produced through SQLite’s online backup API. A manifest records app/schema/version/time/checksum/count evidence. Restore validates the artifact and target safety before creating a sibling safety backup, stages the replacement atomically, retains recoverable evidence and never recursively deletes a broad path. Restart and migration behavior remains additive through schema 10.

The Windows launcher owns only its recorded API/UI child PIDs after executable-path verification, refuses non-loopback hosts and occupied ports, waits for health, and stops gracefully with a bounded fallback only for those verified children. It never kills ComfyUI, Ollama or unrelated processes. Provider/model/GPU/database/backup states are exposed as capability/degraded evidence; the release smoke records local timings without an arbitrary performance claim.

Security and license artifacts are reproducible summaries. npm full/production audits and `pip check` passed with zero npm vulnerabilities; `pip-audit` is unavailable in the current environment and license metadata has explicit review-required entries. The final acceptance inventory is developer-ready only; Sol/supervisor performs the final Gate.

## ADR-026 - Phase 7 repair: offline restore and durable launcher ownership

Date: 2026-08-21

The Windows launcher state is `phase7_launcher_v2`. Every API/UI child record captures the executable path, exact UTC process start-time ticks, command-line SHA-256, role and configured host/port markers after `Start-Process`. Status reports an ownership mismatch and stop refuses to act while retaining the state if PID reuse, stale/tampered state, executable, start time or command line verification fails. Only the verified recorded child process objects may receive graceful close or the bounded fallback; the launcher never searches or kills by process name, port, ComfyUI, Ollama or unrelated process. The isolated ownership smoke proves a stale record pointing at a same-executable helper cannot stop that helper, while normal start/stop still passes.

Restore is an explicit offline operation. Before any safety backup or target mutation, `core.backup` rejects target `-wal`/`-shm` sidecars and persisted WAL journal mode with an actionable stop/close-connections message; it does not attempt a live checkpoint. Existing targets retain a SQLite-consistent `*.pre-restore-*` safety artifact and recover from it after post-replacement validation failure. A previously absent target that fails after replacement is moved to an exact `*.restore-failed-*` quarantine (or removed only as that validated file), preserving the original nonexistent state and failure evidence. Corruption, tampering and path safety remain fail-closed; no broad deletion is used.

## ADR-027 - V1.1 saved-evidence terminal and deterministic dual-model routing

Date: 2026-08-21

V1.1 keeps API Phase 7 and advances the package/API version to `1.1.0` with additive SQLite migration 11. `market_intelligence_v1` is a read-only aggregate over durable Watchlist, latest saved Predictions and point-in-time stored events/news. Pulse, Calendar, monitoring and Heatmap expose as-of, source, freshness, capability and missing evidence; they never invent a live quote, macro value, sector return or AI explanation. `daily_brief_v1` is an explicit POST-only saved artifact with source hash, missing evidence and route audit. GET does not generate it.

`qwen_route_v1` owns Fast=`qwen3.5:4b` and Smart=`qwen3.5:9b`. The server deterministically maps a bounded task plus Fast/Smart/Auto preference to model/tier/reason and returns that audit in `qwen_consult_v2`; clients cannot submit model IDs or base URLs. There is no silent model fallback. Model work remains serial, local Ollama-only and bounded. AI may interpret supplied evidence but cannot calculate or override Python financial, scoring, risk or time rules.

The typed English/Chinese display catalog and Intl helpers localize fixed copy and standard enums without changing symbols, API/SQLite values, model IDs, URLs, numeric/time semantics or free evidence text. UI, AI response, local notification and model preferences are allowlisted settings; none activates background work. The premium navy terminal tokens and dense grid adapt the supplied visual reference's hierarchy and semantic color language without copying brand or fictitious data.

The Windows V1.1 wrappers delegate process ownership to `phase7_launcher_v2` and exact official Ollama pulls. Repeated Start is idempotent; only fingerprinted API/UI children are stopped. Ollama, ComfyUI and unrelated processes are never managed. V1.0 Final Acceptance remains historical; V1.1 is pending its own supervisor Gate.

## ADR-028 - V1.2 public crypto monitoring desktop boundary

Date: 2026-08-31

V1.2 is delivered as a Tauri 2 Windows x64 application with one packaged PyInstaller FastAPI sidecar. The program is installed by current-user NSIS under `%LOCALAPPDATA%\Programs\AI Market Analyst`; the sidecar binds to a fixed loopback port (default `18765`, with an explicit test-only environment override), uses `%LOCALAPPDATA%\AI Market Analyst` for data/logs/backups/runtime, and is launched and stopped through the exact owned child handle/PID tree. Single-instance activation, tray show/exit and native notification routing are desktop concerns; no startup monitoring, auto-start or resume side effect is enabled. Ollama remains an external local dependency and is never stopped by the product.

Public Binance REST and combined WebSocket streams are the only V1.2 market source. The backend does not use TradingView as a data API. It persists bounded 15m/1h bars and freshness/reconnect evidence, and Python owns indicators, triggers, 15m bar-close exactly-once ledgering, dedupe/cooldown, risk validation and Prediction/Outcome/Calibration writes. The frontend uses the redistributable Lightweight Charts package for display only and sends no proprietary chart asset.

MonitoringPolicy is explicit opt-in and bounded to the supported public crypto universe/resource limits. `trigger_policy_v2` creates durable fingerprints and cooldown decisions. Smart OpportunityAnalysis must call `qwen3.5:9b`; unavailable, malformed or financially invalid output is surfaced as degraded/quarantined evidence and is never silently routed to `qwen3.5:4b`. The 4B model is reserved for explicit news translation caching, whose full original evidence and numeric_guard result remain durable.

Schema 12 is additive and idempotent. Legacy database movement into AppData is an explicit validated import, never an implicit copy or destructive migration. The release deliberately has no exchange account, API secret, broker, private-key, cloud notification or real-order path. Browser E2E fixtures prove UI contracts only; the separate live smoke records real public REST/WS/RSS and local Ollama evidence with fixture fallback disabled. Installed notification delivery was observed in the Windows platform event log; the automation surface did not provide a durable visual OS-toast/click assertion, so the safe Tauri action route remains documented as wired and test-covered rather than overclaimed as visually observed.

## ADR-029 - V1.2.1 production usability and desktop ownership repair

Date: 2026-09-02

This decision supersedes ADR-028 where the older record describes a fixed desktop port, schema 12 as current, static tray behavior, or observed Windows notification event-log delivery. Historical V1.2 evidence remains unchanged; the current package/API is `1.2.1` and additive schema 13.

The Tauri shell selects the first free loopback port in `18765..18828`, passes only Rust-constructed sidecar arguments, and validates a per-launch instance/ownership contract. A foreign listener is diagnosed and skipped; it is never adopted, restarted or killed. The WebView has no process-spawn capability. An owned-sidecar watchdog exposes degraded state and may restart only its own child. Explicit Exit and inactive X stop only that child tree; an active-monitoring X always hides to tray and emits a one-time notice.

`monitoring_runtime_v1` is sidecar-owned and remains alive independently of any mounted page. It starts only after an explicit user action, processes enabled policies only, uses public REST/WS with bounded retries/backfill/freshness, sends each closed 15m bar through Python's exactly-once trigger engine, and emits only deduped/cooldown-approved alerts. Pause and stop halt work. Resume after process startup requires both an earlier explicit authorization and the independent resume preference; defaults remain false. Public hydration is a bounded cache-read activity with no trigger, model or financial-domain write.

Windows login autostart uses the official Tauri plugin and OS state is authoritative in the UI. Autostart launches the app/tray only and is independent from monitoring resume. The installer removes stale Run entries on uninstall while AppData research data is retained. Native notification requests and safe action/deep-link handling are present, but ordinary Windows toast-click delivery was not directly observable through the available automation surface; Alert Center and the always-mounted in-app route are the reliable fallback. A direct native tray-popup click was likewise not observable, so neither is overclaimed.

The V1.2.1 repair retains the V1.2 safety boundary: no exchange account, secret, private key, broker, order, funds, cloud notifier or proprietary chart asset; Python owns calculations and validation; Smart monitoring never disguises 4B as 9B; the product never manages Ollama, ComfyUI or another unrelated process.
