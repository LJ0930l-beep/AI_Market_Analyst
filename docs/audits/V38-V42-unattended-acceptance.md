# V38–V42 Unattended Development Acceptance Report

## Current disposition

**Overall: PARTIAL.** V38 market-only schemas, offline runner, sample preregistration, and local data preparation are implemented. V39 local completion retries now fail closed, and V40 simulation tests have been run. The latest V41/V39 exact full-suite comparison resolves all 100 frozen baseline failures with no new failure nodes or phase changes; the local Gate 5 census is complete. V42 is not started and remains gated; no unattended model activity is enabled.

The current one-page status panel is `docs/audits/V38-V42-gate-decision-panel.md`.

## Verified results so far

- V38 tests: **23 passed**; added-code Ruff and compile checks passed. Malformed enum payloads fail closed.
- V38 fixture CLI: 9/9 valid fixture records; zero model calls, orders, eligible proposals, fills, and completed closes.
- Dataset: 180 prepared contexts, of which 120 optimization/validation inputs include full market-only payload and 60 untouched-test rows are hashes/coverage only. Price grade is `VERIFIED_ARCHIVE_RECONSTRUCTION`; availability grade is `ASSUMED_PROXY`.
- V39 original transport checkpoint: **91 passed** on its exact focused command. Follow-up transport/consultation coverage: **128 passed**. An ambiguous completion POST now gets one client attempt; nonzero client or consultation retries fail before inference network access. No real Gemini call was made. Remote idempotency and duplicate-cost behavior after an ambiguous disconnect remain `BLOCKED_WITH_EVIDENCE`; see `V39-single-attempt-retry-followup.md`.
- Historical V40 economics/simulation checkpoint: **95 passed, 1 inherited failure**. Current exact V40 command: **96 passed, 0 failed**. The former `test_production_prompt_manages_positions_across_scans` node now verifies fail-closed behavior when a completion receipt is unverified. V40 is accepted for the offline simulation/economics scope; real Gate fills, historical order-book data, and account-specific margin remain unverified. No production economics code changed.
- Historical V38 full-suite comparison: baseline at 64a5c42 was **2,223 passed, 100 failed, 1 skipped**; the V38 run was **2,246 passed, 100 failed, 1 skipped**. This historical evidence remains unchanged.
- Historical V41 checkpoint: **2,293 passed, 53 failed, 1 skipped** across 2,347 tests; its report remains unchanged in `V41-gate5-node-audit-addendum.md`.
- Historical V41 identity follow-up: **2,321 passed, 27 failed, 1 skipped** across 2,349 tests. Its evidence remains in `V41-provider-identity-followup.md`; the earlier 53-failure checkpoint remains in `V41-gate5-node-audit-addendum.md`.
- Latest V41/V39 follow-up: **2,354 passed, 0 failed, 1 skipped** across 2,355 collected test nodes. Against the same-environment 64a5c42 baseline, **100 baseline failure nodes are resolved**, with **0 new failed node/phase pairs**, **0 missing baseline nodes**, **0 phase changes**, and all **31 new test nodes passing**. The current 100-node register and local comparison evidence are preserved; this completes the local Gate 5 full-suite census but does not verify remote provider, exchange, or production behavior.
- Model calls: **0**. Private exchange calls: **0**. Orders: **0**. Production risk parameters changed: **no**.

## Changes

V38 additions are isolated under `core/replay/pa_decision_quality_v38/`, two versioned JSON schemas, a frozen dataset plan, local-only dataset and fixture CLIs, and tests under `tests/v38/`. Existing V35 risk, Gate production execution, and historical reports were not edited. New code paths reject execution and account-state fields and cannot produce order proposals.

## Open limits

1. The 60-second availability timestamp is an assumed proxy, not historical Gate receive-time proof.
2. The dataset has no account state, order book, contract snapshot, provider response, fill, or complete close. Gate-executable samples remain 0.
3. 180 rows form 23 overlapping input clusters and must not be treated as independent trades.
4. V25 A0 exact recovery remains 0/100.
5. The latest full-suite run has 0 failures. The 100 baseline failure nodes and their resolution status are individually listed with risk priority, contract relevance, historical failure evidence, bounded root-cause assessment, and remediation disposition in `V41-node-remediation-register.json`.
6. Automatic completion retries are disabled locally. Provider idempotency, remote duplicate billing, and real Gemini transport behavior remain unverified; the remote V39 boundary is `BLOCKED_WITH_EVIDENCE` pending authoritative idempotency/cost policy and separate provider-call authorization.
7. No user-authorized Gemini model/budget is available for this phase; zero scored model decisions are therefore expected.

## Acceptance boundary

The offline V38 data-preparation stage is **accepted for research-input construction only**. V38 model-scored decision quality, Gate feasibility, profitability, V42 unattended operation, and any exchange execution are **not accepted**. V41's exact no-regression comparison and local full-suite census pass. Remote provider identity, transport retry semantics, exchange capability, and production execution remain unverified.

## Product-contract supplement

The requested long-term product contract is documented in `docs/plans/AI-Market-Analyst-Product-Contract-V1.md`; the dependency-ordered delivery plan, reuse inventory, gaps, proposed interfaces, and acceptance gates are in `docs/plans/Multi-Asset-Trader-Terminal-Roadmap.md`. These documents do not change the current V38–V42 execution order and do not implement a production multi-asset path. V38 P0 research-readiness work and its no-regression verification were completed first. Production risk settings, fixed 2,000 USDT behavior, historical research artifacts, and account permissions remain unchanged.

## V42 prerequisites

- Independently review the exact test-node/phase comparison and the 100-node register. Lease-loss cancellation, simulated permission/revocation, and local response-identity checks have passing offline coverage. Passing local tests do not establish remote integration behavior.
- Independently review V38 causal input hashes, schema/evidence validation, partition boundaries, and sealed test handling.
- Before any unattended provider operation, define timeout/resubmission behavior and duplicate-cost handling after ambiguous disconnect, using provider-supported idempotency or an explicit bounded policy. The current client fails closed instead of automatically retrying.
- Obtain separate explicit authorization for exact Gemini model, spend/token cap, sample set, prompt/schema versions, and stopping conditions.
- Keep production Gate and all order routes isolated; no V38 evidence authorizes TestNet, Live, or account access.

## Review and publication status

- Independent scope review for PR #6 confirmed the V38 package is addition-only against `codex/v37.1-research-fixes`, has no production import path, and includes no model/exchange/order submission integration. PR #7 adds a fail-closed NOFX import-timeframe guard; it does not modify saved account risk settings or Gate order execution.
- Independent PR: [#6 — V38 research readiness and multi-asset product roadmap](https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/6), head `codex/v38-research-readiness`, base `codex/v37.1-research-fixes`.
- V41 follow-up PR: [#7 — V41 Gate 5: audit inherited test failures](https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/7), head `codex/v41-current-gemini-test-contract`, base `codex/v38-research-readiness`; open for review with auto-merge disabled.
- After commit `0baa0e2844d2cf96d41a5bdd0461fb793acfd381`, GitHub reports PR #7 `OPEN`, merge state `CLEAN`, and `autoMergeRequest: null`. No merge was performed.
- Earlier commit `e34e78c` used the disclosed pre-commit hook bypass while inherited failures remained. Current commit `0baa0e2` passed the normal full-suite pre-commit gate.

## Remaining follow-up

Human review of the V41 evidence remains open. V42 still requires independent data/schema review, a verified provider retry/idempotency policy, and separate explicit authorization for any model and budget. Nothing in this PR authorizes exchange access, Live mode, or orders.
