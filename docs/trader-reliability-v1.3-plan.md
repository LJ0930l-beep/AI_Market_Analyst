# Trader Reliability v1.3 — bounded local implementation plan

Date: 2026-09-08 (Asia/Shanghai)

Milestone: repair the three independently reproduced reliability blockers and deliver the smallest runnable trader-assistant/AI-quant loop on top of the existing v1.2 architecture.

Owner: Luna Max, one continuous implementation context. Separate review/acceptance is outside this development turn.

## Scope and non-negotiable boundaries

In scope: `core/trading`, `core/monitoring_runtime.py`, `apps/api/v2.py`, existing React workspace/components, additive SQLite migrations, focused tests, regression tests, and machine-readable evidence. Reuse the existing six strategies, evaluator, news/revision services, API client, Qwen service boundary, ledger, Gateway, Guardian, and manifest.

Out of scope: reset/clean/commit/push, unrelated refactors, new exchanges, copy trading, strategy marketplace, cloud multi-account, model competition, changing existing real accounts or authorization, private keys, real or TESTNET orders, LIVE unlock, downloading models, or changing system settings.

Safety invariants:

1. Only PAPER may locally simulate a price-crossing stop or fill. TESTNET/LIVE protection is a venue fact: use a scoped native-protection/Unified Gateway adapter and reconciliation; without an authorized client preserve the position and report `DEGRADED`/`PROTECTION_UNVERIFIED`, never locally close it.
2. A runtime may not rebind from account A to B while A has open/protected positions, in-flight/UNKNOWN orders, or protection duties. A rejected rebind must leave A protection alive and must not make B appear started.
3. Runtime lease ownership is a persistent fencing boundary. Every renew/acquire returns a monotonically increasing fencing token. New AI/strategy cycles, risk reservations, and Gateway opening submissions validate the current holder/token immediately before the side effect. A stale holder is cancelled/degraded and cannot revive by renewing an old lease. Protection handoff remains available to a safe current owner.
4. The ledger snapshot is the only source for equity, positions, orders, reservations, fees, and reconciliation time. Unknown, stale, missing, or mismatched data remains explicit UNKNOWN/DEGRADED; the UI never fabricates values.
5. AI proposes only a structured action. Program code owns environment, account, authorization, market facts, quantity, limits, execution state, protection state, and accounting.

## Implementation work packages

### W1 — three blocking defects

- W1-A: make `PositionGuardian` environment-aware. PAPER retains the local matching path. TESTNET/LIVE requires a scoped Gateway/adapter callback and a confirmed remote fill; no adapter or uncertain remote state leaves the original remainder/open status and marks protection degraded. Add native protection identity, requested/filled quantity, status, reconciliation timestamp, and evidence source to status/reporting.
- W1-B: make runtime account binding durable and safe. Detect all scoped protection duties and in-flight/UNKNOWN orders before `start(account_id=...)`; reject a second account bind while the first still owns any duty. Preserve A Guardian/feed/lease on failure. Add API error and frontend explanation. Test start(A) → stop → start(B), pause/resume, restart/recovery, and same-symbol cross-account isolation through the real runtime/API entry.
- W1-C: add persistent lease fencing. Extend the existing lease row/API with `fencing_token`, holder, expiry, and state. Lease loss immediately cancels/invalidates AI and strategy generations, blocks new risk/opens at Gateway, records a durable reason, and cannot be repaired by an old holder. Reconciliation/protection may continue or transfer through an explicit current owner; do not turn off the only protection feed.

### W2 — trusted account risk cockpit

Expose one account-scoped snapshot containing equity/cash/reserved/available margin, open positions, orders, UNKNOWN orders, protection identities and quantities, last market event, last reconciliation, model/session health, and interruption/emergency instructions. Every field has `as_of`, source, and status. Missing external data is UNKNOWN. API and workspace use the existing client and wait for real lifecycle responses.

### W3 — evidence-backed strategy evaluation

Build a research-task input and evaluator pipeline over stored market bars, predictions, paper fills, and outcomes rather than accepting arbitrary trade arrays as a validation claim. Persist input snapshot hash, interval, in-sample/out-of-sample or rolling split, strategy/version, symbol, environment, regime, data-quality status, fee/slippage stress, sample threshold, and evaluator version. Report cost-after performance, drawdown, consecutive losses, tail/recovery measures, parameter perturbation, and `INSUFFICIENT_EVIDENCE` where the history is inadequate. AI_LED receives a separate decision path and cannot inherit fixed-strategy performance.

### W4 — portfolio risk and budget explanation

Aggregate open, pending, and UNKNOWN risk; show single-order capacity versus portfolio capacity. Apply conservative explicit limits for missing correlation/event data, and display concentration by direction, asset/group, event, and strategy. Keep atomic single/portfolio/cluster/daily reservations and explicit reduce-risk/pause/recovery transitions. AI cannot raise limits; a recovery requires an explicit user action and current lease/authorization.

### W5 — complete trade-plan contract

Persist for every action: evidence references, entry trigger, chase-abandon condition, stop, time exit, event invalidation, partial TP/reduce, trailing-protection rule, worst-loss budget, and why WAIT was not selected (or why WAIT/HOLD is the formal result). Re-read and monitor the original assumptions for existing positions. No model field can alter environment, permission, or price facts. Rules execute through the existing engine/Gateway, not only in text.

### W6 — news/event and AI scorecard

Use existing collection/revision records. Preserve original source, published/first-seen/revised times, facts, AI inference, conflicts, verification state, direction, horizon, expected-vs-realized gap, absorbed/unknown state, and invalidation. Unverified news cannot independently trigger high risk. Link input snapshot, model/prompt/strategy digest, authorization scope, execution result, and attribution. Compare fixed strategy, AI filter, and AI_LED over the same data interval/cost assumptions, separating decision, data, and execution errors; model/prompt changes create a new version requiring new evidence.

### W7 — trader workspace

Keep the existing UI architecture and prioritize risk exceptions → existing position plans → new opportunities → daily attribution. Add real asynchronous states for account/environment, authorization, lease/fencing, model, market freshness, protection, orders, plan, and evidence sufficiency. Keep Chinese/English translations and no hardcoded sample performance.

## Acceptance matrix

| ID | Acceptance criterion | Required production entry | Local test/evidence | Tier / release rule |
|---|---|---|---|---|
| V13-A | TESTNET/LIVE stop cannot locally fill/close; no client preserves remainder and reports degraded; PAPER still matches locally | Runtime → Guardian → environment adapter/Gateway | `test_v13_a_guardian_only_paper_can_locally_close`; `test_v13_a_remote_protection_does_not_duplicate_unresolved_exit` | Local SQLite; real venue `NOT_RUN` |
| V13-B | A protection duty blocks rebind to B; A feed/Guardian remains alive; API/UI show explicit rejection | API start → `MonitoringRuntime.start` | `test_v13_b_stop_then_rebind_is_blocked_until_account_a_recovers`; `test_v13_b_reduce_only_buy_cannot_open_or_reverse` | Local real runtime entry |
| V13-C | Lost lease fencing blocks new AI/strategy risk and Gateway open; stale holder cannot renew/revive; protection not dropped | Runtime lease → coordinator/Gateway | `test_v13_c_stale_fencing_token_cannot_reach_gateway`; `test_v13_c_runtime_lease_loss_cancels_ai_and_pauses_session` | Local multi-owner/fault injection |
| V13-D | Two-account risk cockpit has no position/order/equity leakage and marks missing facts UNKNOWN | API → scoped snapshot → workspace | `test_v13_d_cockpit_and_api_queries_are_account_scoped`; `test_v13_d_legacy_position_is_unknown_and_blocks_runtime_recovery` | Local API/E2E |
| V13-E | Research evaluation only accepts stored evidence/task inputs; splits, costs, versions, quality, sample threshold and limitations persist | API/task → evaluator → stored report | `test_v13_e_research_is_stored_costed_and_no_lookahead` | Local stored-data evidence |
| V13-F | Open+pending+UNKNOWN and concentration limits are one atomic budget; single vs portfolio capacity differs honestly | API/AI/strategy → Gateway/RiskEngine | `test_v13_f_portfolio_budget_includes_unknown_orders`; `test_v13_f_wait_news_and_unknown_facts_remain_explicit` | Local SQLite concurrency |
| V13-G | Full trade plan persists and executes trigger/abandon/stop/time/event/partial-protection/WAIT semantics | Coordinator → engine → Gateway → ledger/Guardian | `test_v13_g_trade_plan_runs_through_api_runtime_gateway_and_guardian` | Local PAPER E2E |
| V13-H | News provenance/revision/conflict/UNKNOWN and unverified-high-risk block are visible | News ingestion → analysis/risk | `test_v13_f_wait_news_and_unknown_facts_remain_explicit` | Local stored source fixtures |
| V13-I | AI scorecard joins input/model/prompt/strategy/auth/execution and separates error classes; new digest is not accepted as old validation | AI cycle → attribution/evaluator | `test_v13_g_ai_scorecard_links_receipts_but_not_profit_claims`; `test_v13_h_api_runtime_to_gateway_fill_guardian_ledger_attribution` | Local mock-provider trace only |
| V13-J | Workspace lifecycle and failure states are real API responses, bilingual, and free of fabricated cycles/metrics | Frontend → API → runtime/coordinator | `test_v13_i_api_never_fakes_start_or_opening_without_runtime`; Vitest workspace suite | Local frontend |
| V13-K | Prior v1.2 AT/RT/original regression remains green | Existing commands | Full Python + frontend suite | Required before handoff |
| V13-L | Evidence binds exact JUnit hashes, dirty source tree, tier, and unexecuted external gates | Test runner → manifest/report | Manifest integrity check | `RT23`, `RT25`, real Qwen remain `NOT_RUN` without authorization |

## Required verification commands

1. Focused v1.3 tests with JUnit, including the three blocker regressions and the real API/runtime → coordinator → intent → Gateway → fill/unknown → protection → ledger → attribution path.
2. Existing v1.2 focused tests and all AT/RT/original Python tests with a final JUnit.
3. `python -m compileall -q core apps tests`.
4. `cd web; npm test -- --run`, `npm run typecheck`, and `npm run build`.
5. JSON/manifest validation and exact artifact SHA-256 verification.

Real Qwen inference may be tested only if an already available local service is used for non-trading, non-secret reasoning; no model download or system change. External TESTNET protection/fill/fee, real Qwen acceptance, and clean Windows installation are `NOT_RUN` unless separately authorized and actually executed. Mock evidence never promotes those rows.

## Migration and rollback

- Use additive/idempotent schema migrations only. Backup is created by the existing store migration path before schema changes. New lease fencing, plan, research-task, provenance, and attribution columns/tables have safe defaults and preserve old rows.
- Existing positions/orders without provable account/venue/mode/position identity remain `legacy_unverified` and unmanaged; migration never guesses ownership.
- Rollback means stop the local runtime, retain the pre-migration backup, revert only the v1.3 code/evidence change set through normal version control review, and reopen the backup with integrity/schema checks. Do not reset or delete user dirty work as part of this milestone.
- New v1.3 records are append-only/auditable; old v1.2 reports remain historical and are not rewritten as v1.3 evidence.

## Definition of done

The local PAPER product can be driven through the real API/runtime/coordinator/Gateway path and can show a truthful account risk snapshot, persistent trade plan, protection lifecycle, ledger reconciliation, strategy/AI attribution, and UNKNOWN/degraded conditions. The three reproduced blockers have failure regressions. All old local regressions and frontend checks pass. Anything requiring Qwen service availability, external TESTNET access, or a clean installation is explicitly recorded as `NOT_RUN`, not treated as accepted.
