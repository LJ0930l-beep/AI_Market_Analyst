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

Phase 5 implementation evidence is developer-complete and ready for Sol review; this ADR does not authorize Phase 6 implementation.
