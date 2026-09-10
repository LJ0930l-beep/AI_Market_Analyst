# Trader reliability v1.4 implementation plan

Date: 2026-09-08  
Scope: `docs/v1.4-audit-repair-contract.md`, carried out locally in the existing workspace.

This plan is the execution checklist for the contract. It preserves the v1.2/v1.3 gateway, ledger, runtime, Guardian, authorization, and AI-session work. It does not add an exchange, a strategy, a cloud account, a paid dependency, a real order, a TESTNET order, or LIVE permission.

## Delivery boundaries

* `PAPER` may use the existing deterministic local matching engine. `TESTNET` and `LIVE` may only produce adapter/reconciliation evidence; this run sends no external order.
* Every new opening action must cross the registered account scope, fresh market, authorization, runtime fencing, RiskEngine, atomic reservation, gateway, and protection checks.
* A missing fact remains `UNKNOWN`, `BLOCKED_DATA`, `NOT_RUN`, or `EVIDENCE_INSUFFICIENT` according to the layer. No caller-supplied confidence, fee, event gap, or model status is promoted to permission.
* Existing dirty and untracked files are user-owned. Changes are additive and rollbackable; no reset, clean, commit, push, credential read, or system setting change is part of this plan.

## D01-D04 work package

| Contract | Implementation focus | Production entry | Durable effect | Local evidence |
|---|---|---|---|---|
| D01 | Versioned plan normalizer; explicit `reduce_fraction`/`reduce_quantity`; leverage; amount step/min; scoped position id; idempotent gateway/ledger | `POST /v2/trade-plans`, runtime plan consumer, AI/manual `ExecutionGateway.submit_intent` | Plan payload and execution receipt retain behavior fields; REDUCE never defaults to full close | V14-01..03, old RT/AT regression, mutation check |
| D02 | Typed versioned conditions; runtime trigger scheduling; separate entry expiry/position time exit; partial/trailing/event protection contract | `MonitoringRuntime.process_market_event`/stream callback, `TraderCapabilityService.execute_trade_plan`, `PositionGuardian` | ARMED/WAITING/EXPIRED/INVALIDATED/BLOCKED_DATA state; post-entry exits survive strategy pause/termination | V14-04..08 and runtime/API/UI E2E |
| D03 | Bounded replay of stored closed bars through existing strategy; frozen train/OOS, rolling windows, perturbations, costs, conservative same-bar execution | `POST /v2/research/evaluations` and persisted research task | Replay input/parameter/version/cost/window hashes and fills; historical prediction split remains explicitly retrospective | V14-09..11, deterministic rerun, future-data mutation |
| D04 | Immutable news revision provenance and inference evidence; no risk permission; plan revision gate and Guardian correction handling | `/v2/news/{news_id}/impacts`, plan creation/execution, `PositionGuardian` | Missing inference stays UNKNOWN; correction/retraction invalidates new entry and drives existing protection process | V14-07, news revision regression |

## Behaviour-field flow matrix

| Field | API input | Storage/read-back | Consumer | Observable effect | UI/test |
|---|---|---|---|---|---|
| `reduce_fraction` / `reduce_quantity` | `TradePlanBody` | canonical `trader_trade_plans.payload_json`; plan list | plan executor → scoped gateway/ledger | step-rounded partial fill; missing legacy REDUCE is `NEEDS_RECONFIRMATION` | plan card; V14-01/02 |
| `leverage` | `TradePlanBody` | canonical plan and execution metadata | RiskEngine sizing / ledger position | preserved integer; cannot widen hard risk limit | plan card/receipt; V14-01/03 |
| `entry_trigger` + `condition_spec` | API text plus typed spec | `conditions` and schema/version in payload | runtime plan scheduler | no order until typed condition is true; unsupported prose blocks | status/reason card; V14-04 |
| `abandon_chase_condition` | API text plus max-chase spec | canonical `conditions.abandon_chase` | condition evaluator before gateway | adverse quote beyond fixed bound stays waiting | condition evidence; V14-04 |
| `entry_expires_at` | API timestamp | canonical plan/conditions | condition evaluator/runtime | `EXPIRED`, no reservation/order | status badge; V14-04 |
| `time_exit_at` | API timestamp | protection contract on position | Guardian | time-based reduce-only exit independent of AI/session | position plan; V14-05 |
| `event_invalidation` + `news_revision_ids` | API structured keys | plan and position protection contract | revision gate + Guardian | missing evidence blocks; correction invalidates/executes protective route | evidence level/status; V14-07 |
| `partial_take_profits` | scalar prices or typed price/fraction | protection contract on position | Guardian, CAS/idempotent exit fill | exact fraction of original/remaining position, no oversell | exit history; V14-06 |
| `trailing_protection` | typed percent/distance | protection contract on position | Guardian one-way stop tightening | stop only moves in favorable direction, CAS protected | current stop; V14-06 |
| `evidence` | API list | plan payload and news/research provenance | plan/news gate and scorecard | traceable source/as-of; does not grant permission | evidence display; V14-03/07 |
| market bar/quote | existing public/local provider | `market_bars`/realtime state | runtime, RiskEngine, gateway, replay | fresh executable quote; closed-bar replay only | freshness banner; V14-04/09 |

## Acceptance matrix

| ID | Required proof | Evidence target |
|---|---|---|
| V14-01 | API/plan read-back/25% fill/0.75 remainder, long and short | production API/service path + isolated SQLite |
| V14-02 | invalid/missing fractions, legacy plan, explicit close | negative and state-transition tests |
| V14-03 | all behavior fields, unknown field, multi-position id | round-trip and scope tests |
| V14-04 | trigger, chase, expiry | runtime scheduling and no-order assertions |
| V14-05 | time exit while paused/stopped | Guardian + durable contract |
| V14-06 | 25/25/remainder, trailing, manual reduction, duplicate/乱序 | CAS/idempotency tests |
| V14-07 | correction and missing news inference | revision registry + plan/impact tests |
| V14-08 | restart and duplicate economic intent | durable plan/protection/gateway tests |
| V14-09 | existing strategy closed-bar replay | production service + stored bars |
| V14-10 | real perturbation, purge/embargo, cost pressure | replay result hashes and variants |
| V14-11 | deterministic rerun and insufficient-data boundary | same-input comparison + NOT_RUN result |
| V14-12 | UI/API → runtime → gateway → ledger → attribution | API/UI tests and local E2E evidence |
| V14-13 | v1.2/v1.3 safety regressions | focused and full backend/frontend suites |

## Verification / stop rules

The first D01 failing test is retained as `artifacts/junit-v14-d01-failing.xml`; the same test must pass in the final focused JUnit. New tests must fail under small temporary mutations that drop the reduction fraction or make an entry condition unconditional. Existing assertions are not loosened. External Qwen, TESTNET, LIVE, and clean-Windows-install checks are recorded `NOT_RUN`, not `PASS`. A local implementation gap is recorded `NOT_IMPLEMENTED`, not hidden behind an external limitation.

