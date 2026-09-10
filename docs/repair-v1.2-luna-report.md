# Repair plan v1.2 — Luna implementation report

Date: 2026-09-08 (Asia/Shanghai)

Milestone: complete the single repair package defined by `D:\RJ\codex\deliverables\ai-market-analyst-redesign\repair-plan-v1.2.md`.

Disposition: implementation complete and handed to Sol for independent review, verification, and acceptance. This report does not claim product acceptance, live trading, TESTNET execution, or real Qwen E2E verification.

## Scope and safety boundary

The changes are limited to the assigned execution, monitoring runtime, API v2, related frontend, tests, and evidence paths. Existing dirty and untracked user work was preserved; no reset, clean, push, real funds, private exchange credentials, or LIVE unlock was used.

`AGENTS.md` and the exact uppercase `SPEC.md` were not present in the checkout. The repository equivalents `MASTER_SPEC.md` and `D:\RJ\codex\deliverables\ai-market-analyst-redesign\spec.md` were read, together with the complete repair plan v1.2 and the relevant repair-spec v1.1 material.

## Finding-to-change-to-evidence map

| Finding | Implementation paths | Direct regression evidence | Verification tier | Residual limitation |
|---|---|---|---|---|
| P0 unified risk and budget bypass | `core/trading/execution_gateway.py`, `core/trading/risk_engine.py`, `core/trading/ledger.py`, `core/trading/authorization.py` | `tests/test_repair_v12_luna.py::test_gateway_requires_fresh_quote_and_unified_budget`; `::test_authorization_portfolio_cap_reaches_unified_risk_engine`; AT05/AT23 coverage in full JUnit | Local SQLite/PAPER with fresh market, fee/slippage/contract/step and atomic reservation | No external venue execution was authorized or run |
| P0 reduce-only direction and oversell | `core/trading/execution_gateway.py`, `core/trading/ledger.py`, `core/trading/position_guardian.py` | `::test_reduce_only_direction_scope_and_concurrent_exit_are_safe`; `::test_reduce_only_requires_position_identity_for_multiple_scoped_positions`; RT06 and RT01-RT03 | Local SQLite concurrent CAS and scoped ledger | Legacy rows without ownership stay unmanaged |
| P0 strategy termination must preserve protection | `core/monitoring_runtime.py`, `core/trading/position_guardian.py`, `core/trading/session_manager.py` | `::test_runtime_terminate_keeps_scoped_protection_stream`; AT08/AT10 and RT03 | Local runtime with an injected deterministic stream/Guardian harness | External reconnect/restart behavior still needs authorized deployment smoke |
| P1 production AI session path | `core/trading/ai_session_coordinator.py`, `core/trading/ai_led_engine.py`, `core/monitoring_runtime.py`, `apps/api/v2.py`, `web/src/components/AITraderPanel.tsx`, `web/src/pages/V2WorkspacePage.tsx` | `::test_production_ai_session_coordinator_persists_two_cycles`; frontend 85/85; full JUnit AI lifecycle cases | Mock Qwen-9B-shaped provider trace only; persistent local cycles and cancellation/timeout paths | Real local Qwen 9B was not available/authorized in this run; manifest records `NOT_RUN`, and RT17 is mock-only |
| P0/P1 account and venue isolation | `core/v2_store.py`, `core/trading/ledger.py`, `core/trading/position_guardian.py`, `core/monitoring_runtime.py`, `apps/api/v2.py`, frontend account controls | `::test_api_scope_and_runtime_unavailable_are_explicit`; `::test_scoped_emergency_stop_does_not_mutate_global_subscription_policy`; `::test_reduce_only_direction_scope_and_concurrent_exit_are_safe` | Local two-account SQLite/PAPER/API scope tests | Accountless legacy strategy-subscription storage remains a future migration boundary; trading/position/protection queries reject or skip unverified ownership |
| P1 TESTNET capability and evidence honesty | `core/trading/testnet_capabilities.py`, `core/manifest.py`, `core/diagnostics.py`, `apps/api/v2.py` | `::test_testnet_capability_is_not_run_without_adapter_and_verified_with_probe`; AT27 diagnostic bundle; full JUnit | Local capability state machine and redaction/manifest tests | No external adapter authorization, protective order, fill, or fee evidence; RT23/RT25 remain authorization-gated |

Additional related repairs include PAPER limit-price crossing and local reconciliation, UNKNOWN fill reconciliation with reservation hold, idempotent empty replay receipts/conflict detection, cumulative-fill fee deltas, foreign-fee valuation blocking, Guardian/AI CAS exit competition, and request-scoped SQLite connection cleanup on Windows.

## Exact verification commands and results

| Command | Result |
|---|---|
| `python -m pytest -q tests/test_spec_at01_at07.py --junitxml=artifacts/junit-at01-at07-current.xml` | PASS — 7 passed |
| `python -m pytest -q tests/test_repair_v12_luna.py tests/test_spec_at24_at30.py::test_at27_diagnostic_bundle_zero_secret_leakage --junitxml=artifacts/junit-repair-v12-focused-current.xml` | PASS — 16 passed, 1 known FastAPI/Starlette `httpx` deprecation warning |
| `python -m pytest -q --junitxml=artifacts/junit-full-current.xml` | PASS — 268 passed, 1 skipped, 1 known FastAPI/Starlette `httpx` deprecation warning; 269 collected |
| `python -m compileall -q core apps tests/test_repair_v12_luna.py tests/test_spec_at01_at07.py` | PASS |
| `cd web; npm test -- --run` | PASS — 19 files, 85 tests |
| `cd web; npm run typecheck` | PASS |
| `cd web; npm run build` | PASS — Vite production build, 82 modules transformed |
| `git diff --check` | No code error; reports existing/new EOF whitespace warnings in `core/providers/gateio_provider.py:144` and `web/src/v2.css:1344`, plus normal Windows LF/CRLF warnings |

JUnit SHA-256 evidence:

- `artifacts/junit-at01-at07-current.xml`: `174BFB730992ED0E1B5C6CB7B2B94D4C0F5DFA8DD0D1C4F37F6A1E8851CCEF8F`
- `artifacts/junit-repair-v12-focused-current.xml`: `D89EC75C23B641926582FBAFCD56AA42B033604D221A2751DA08C758D5D53C0E`
- `artifacts/junit-full-current.xml`: `810E8C2B7018CCF69184E72A6181F7498F25D32BE964A31B4DFA6CEE589E4C4F`

## Verification tiers not performed

- Real local Qwen `qwen3.5:9b` provider/session E2E: `NOT_RUN`; no model availability was assumed and no silent fallback was introduced.
- External Gate TESTNET adapter capability probe, native protection order, real fill, and fee/funding evidence: `NOT_RUN` because independent external authorization was not provided.
- Clean Windows installation/migration acceptance (RT25): `NOT_RUN`; the local dirty checkout and local tests cannot represent a clean installation.
- LIVE: `LOCKED` by release policy; no unlock or credentials were used.

## Residual risks returned to Sol

1. Cross-process authorization revocation and an external adapter callback can still have a provider-side timing window; local authorization rereads and the reservation/position CAS fence reduce, but do not prove, a distributed exchange race.
2. Migrated simulated positions without explicit account/venue/mode/position identity are marked `legacy_unverified` and excluded from management; ownership must be explicitly reconciled before any future migration tool manages them.
3. The production strategy-subscription table predates account ownership. Runtime execution and position/protection paths are account-scoped, but multi-account strategy subscription migration is intentionally not expanded in this milestone.
4. The local PAPER engine is a deterministic simulator and is not evidence of external exchange matching or protection semantics.

## Handoff

Source head at the start/end of this repair was `32596a3f1431a6646e3c5cb853a213fcc69a067c`; the worktree remains dirty by design. The machine-readable companion is `artifacts/repair-v1.2-luna-evidence.json`; the generated source/test manifest is `evidence/manifest.json`. Sol alone performs final review, verification, and acceptance.
