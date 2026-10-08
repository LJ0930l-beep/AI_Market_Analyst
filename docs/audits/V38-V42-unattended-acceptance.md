# V38–V42 Unattended Development Acceptance Report

## Current disposition

**Overall: PARTIAL.** V38 market-only schemas, offline runner, sample preregistration, and local data preparation are implemented. V39 transport and V40 simulation tests have been run. V41 exact full-suite comparison reports no new failure nodes or phase changes, but Gate 5 remains partial because 55 inherited failures remain. V42 is not started and remains gated; no unattended model activity is enabled.

## Verified results so far

- V38 tests: **23 passed**; added-code Ruff and compile checks passed. Malformed enum payloads fail closed.
- V38 fixture CLI: 9/9 valid fixture records; zero model calls, orders, eligible proposals, fills, and completed closes.
- Dataset: 180 prepared contexts, of which 120 optimization/validation inputs include full market-only payload and 60 untouched-test rows are hashes/coverage only. Price grade is `VERIFIED_ARCHIVE_RECONSTRUCTION`; availability grade is `ASSUMED_PROXY`.
- V39 transport tests: **91 passed**. No real Gemini call was made; exactly-once retry semantics remain unproven.
- V40 economics/simulation tests: **95 passed, 1 inherited failure** (`test_production_prompt_manages_positions_across_scans`), matched to the pre-V38 baseline.
- Historical V38 full-suite comparison: baseline at 64a5c42 was **2,223 passed, 100 failed, 1 skipped**; the V38 run was **2,246 passed, 100 failed, 1 skipped**. This historical evidence remains unchanged.
- Current V41 Gate 5 comparison: **2,291 passed, 55 failed, 1 skipped** across 2,347 collected tests. Against the exact same-environment 64a5c42 baseline, **45 baseline failure nodes are resolved**, **55 remain**, with **0 new failed node/phase pairs**, **0 missing baseline nodes**, and **0 phase changes**. All 23 new V38 tests still pass. The current status is `PARTIAL`, not full-suite acceptance. See `V41-gate5-node-audit-addendum.md` and `V41-node-remediation-register.json`.
- Model calls: **0**. Private exchange calls: **0**. Orders: **0**. Production risk parameters changed: **no**.

## Changes

V38 additions are isolated under `core/replay/pa_decision_quality_v38/`, two versioned JSON schemas, a frozen dataset plan, local-only dataset and fixture CLIs, and tests under `tests/v38/`. Existing V35 risk, Gate production execution, and historical reports were not edited. New code paths reject execution and account-state fields and cannot produce order proposals.

## Open limits

1. The 60-second availability timestamp is an assumed proxy, not historical Gate receive-time proof.
2. The dataset has no account state, order book, contract snapshot, provider response, fill, or complete close. Gate-executable samples remain 0.
3. 180 rows form 23 overlapping input clusters and must not be treated as independent trades.
4. V25 A0 exact recovery remains 0/100.
5. There are 55 inherited full-suite failures. The 100 baseline nodes are individually listed with current status, risk priority, contract relevance, failure evidence, bounded root-cause assessment, and owner/remediation state in `V41-node-remediation-register.json`.
6. Provider retry/idempotency and real Gemini transport behavior are not verified.
7. No user-authorized Gemini model/budget is available for this phase; zero scored model decisions are therefore expected.

## Acceptance boundary

The offline V38 data-preparation stage is **accepted for research-input construction only**. V38 model-scored decision quality, Gate feasibility, profitability, V42 unattended operation, and any exchange execution are **not accepted**. V41's exact no-regression comparison passes, but Gate 5's full-suite audit/remediation is **partial**: 55 baseline failures remain, including unresolved provider, authorization, and runtime-lifecycle coverage.

## Product-contract supplement

The requested long-term product contract is documented in `docs/plans/AI-Market-Analyst-Product-Contract-V1.md`; the dependency-ordered delivery plan, reuse inventory, gaps, proposed interfaces, and acceptance gates are in `docs/plans/Multi-Asset-Trader-Terminal-Roadmap.md`. These documents do not change the current V38–V42 execution order and do not implement a production multi-asset path. V38 P0 research-readiness work and its no-regression verification were completed first. Production risk settings, fixed 2,000 USDT behavior, historical research artifacts, and account permissions remain unchanged.

## V42 prerequisites

- Independently review the exact test-node/phase comparison and explicitly disposition the 55 remaining failure nodes, especially model identity, lease-loss cancellation, and simulated permission/revocation coverage. A no-new-failures result does not establish those behaviors.
- Independently review V38 causal input hashes, schema/evidence validation, partition boundaries, and sealed test handling.
- Define provider timeout/retry behavior including duplicate inference/cost after ambiguous disconnect, using a provider-supported idempotency mechanism or an explicit bounded policy.
- Obtain separate explicit authorization for exact Gemini model, spend/token cap, sample set, prompt/schema versions, and stopping conditions.
- Keep production Gate and all order routes isolated; no V38 evidence authorizes TestNet, Live, or account access.

## Review and publication status

- Independent scope review confirmed the change is addition-only against `codex/v37.1-research-fixes`; no existing production, risk, Gate, account, or UI file is changed. The V38 package has no production import path and no model/exchange/order submission integration.
- Independent PR: [#6 — V38 research readiness and multi-asset product roadmap](https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/6), head `codex/v38-research-readiness`, base `codex/v37.1-research-fixes`.
- GitHub reports the PR `OPEN`, merge state `CLEAN`, and `autoMergeRequest: null`. No GitHub checks were reported at review time; this is not represented as CI success. The PR has not been merged.
- Commit: `f2e73180d325d6aa52da3f3f46a2f0d8b391bd09`. The documented pre-commit bypass is disclosed in its subject and PR description because the frozen baseline and historical V38 full-suite runs retained the same 100 inherited failures.

## Remaining follow-up

Human review and disposition of the inherited test failures remain open. V42 still requires independent data/schema review, a verified provider retry/idempotency policy, and separate explicit authorization for any model and budget. Nothing in this PR authorizes exchange access, Live mode, or orders.
