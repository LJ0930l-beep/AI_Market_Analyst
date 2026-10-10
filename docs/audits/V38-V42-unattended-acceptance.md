# V38–V42 Unattended Development Acceptance Report

## Current disposition

**Overall: PARTIAL.** V38 market-only schemas, offline runner, sample preregistration, and local data preparation are implemented. V39 local completion retries now fail closed, and V40 simulation tests have been run. The latest V41/V39 exact full-suite comparison resolves all 100 frozen baseline failures with no new failure nodes or phase changes; the local Gate 5 census is complete. V42 is not started and remains gated; no unattended model activity is enabled.

The current one-page status panel is `docs/audits/V38-V42-gate-decision-panel.md`.

## Verified results so far

- V38 tests: **23 passed**; added-code Ruff and compile checks passed. Malformed enum payloads fail closed.
- V38 integrity follow-up: all **42 tests under `tests/v38/` passed**, including 19 focused overlap/planner/auditor checks. Ruff and compile checks pass. The companion audit returned `PASS_WITH_CAVEATS`, recomputed 60 market-input hashes and 11,460 causal bars, verified the source archive set and deterministic rebuild, and made 0 network/model calls and 0 orders. Machine evidence is local, Git-ignored.
- Latest post-audit full suite: **2,373 passed, 1 skipped, 0 failed** across 2,374 collected nodes. The exact same-environment comparison to baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664` is `PASS_NO_NEW_FAILURES`: 100/100 baseline failure-phase nodes resolved, all 50 added nodes passed, 0 missing baseline nodes, 0 new failures, and 0 phase changes. Pytest reported one Starlette/httpx deprecation warning.
- V38 fixture CLI: 9/9 valid fixture records; zero model calls, orders, eligible proposals, fills, and completed closes.
- The original V38 dataset remains immutable at 180 nominal contexts (120 payloads and 60 sealed hashes), but its 8-day windows overlap heavily and its row count is not an independent sample count. Its audit found 20 global components and 10 cross-partition overlap pairs. The companion dataset contains 80 contexts across 40 paired UTC anchors (20/10/10 by partition): 60 optimization/validation payloads and 20 untouched-test hash-only rows. It has zero same-symbol window overlap; deterministic rebuild matches manifest `1d76f1cd0e2eaa658eb8d32decc434a10e151cb7119a6b2a4f85801639537785`; companion audit `report_sha256` is `e755f12b8de361ace1cee5a3f058ad5743fad9101749ca320620b083498bc64e`. Price grade is `VERIFIED_ARCHIVE_RECONSTRUCTION`; availability remains `ASSUMED_PROXY`. Neither dataset supplies trade labels or outcomes.
- V39 original transport checkpoint: **91 passed** on its exact focused command. Follow-up transport/consultation coverage: **128 passed**. An ambiguous completion POST now gets one client attempt; nonzero client or consultation retries fail before inference network access. No real Gemini call was made. Remote idempotency and duplicate-cost behavior after an ambiguous disconnect remain `BLOCKED_WITH_EVIDENCE`; see `V39-single-attempt-retry-followup.md`.
- Historical V40 economics/simulation checkpoint: **95 passed, 1 inherited failure**. Current exact V40 command: **96 passed, 0 failed**. The former `test_production_prompt_manages_positions_across_scans` node now verifies fail-closed behavior when a completion receipt is unverified. V40 is accepted for the offline simulation/economics scope; real Gate fills, historical order-book data, and account-specific margin remain unverified. No production economics code changed.
- Historical V38 full-suite comparison: baseline at 64a5c42 was **2,223 passed, 100 failed, 1 skipped**; the V38 run was **2,246 passed, 100 failed, 1 skipped**. This historical evidence remains unchanged.
- Historical V41 checkpoint: **2,293 passed, 53 failed, 1 skipped** across 2,347 tests; its report remains unchanged in `V41-gate5-node-audit-addendum.md`.
- Historical V41 identity follow-up: **2,321 passed, 27 failed, 1 skipped** across 2,349 tests. Its evidence remains in `V41-provider-identity-followup.md`; the earlier 53-failure checkpoint remains in `V41-gate5-node-audit-addendum.md`.
- Latest V41/V39 follow-up: **2,354 passed, 0 failed, 1 skipped** across 2,355 collected test nodes. Against the same-environment 64a5c42 baseline, **100 baseline failure nodes are resolved**, with **0 new failed node/phase pairs**, **0 missing baseline nodes**, **0 phase changes**, and all **31 new test nodes passing**. The current 100-node register and local comparison evidence are preserved; this completes the local Gate 5 full-suite census but does not verify remote provider, exchange, or production behavior.
- Model calls: **0**. Private exchange calls: **0**. Orders: **0**. Production risk parameters changed: **no**.

## Changes

V38 additions are isolated under `core/replay/pa_decision_quality_v38/`, frozen dataset plans, local-only dataset/audit/fixture CLIs, and tests under `tests/v38/`. The nonoverlap companion leaves the V1 dataset and all historical reports unchanged. Existing V35 risk and Gate production execution were not edited. New code paths reject execution and account-state fields and cannot produce order proposals.

## Open limits

1. The 60-second availability timestamp is an assumed proxy, not historical Gate receive-time proof.
2. The dataset has no account state, order book, contract snapshot, provider response, fill, or complete close. Gate-executable samples remain 0.
3. The original V1 sample has 20 global window components and 10 cross-partition overlaps; those counts do not establish independent samples. The companion dataset removes same-symbol window overlap, but its 30 visible temporal anchors remain serially dependent pilot observations, not IID trades.
4. V25 A0 exact recovery remains 0/100.
5. The latest full-suite run has 0 failures. The 100 baseline failure nodes and their resolution status are individually listed with risk priority, contract relevance, historical failure evidence, bounded root-cause assessment, and remediation disposition in `V41-node-remediation-register.json`.
6. Automatic completion retries are disabled locally. Provider idempotency, remote duplicate billing, and real Gemini transport behavior remain unverified; the remote V39 boundary is `BLOCKED_WITH_EVIDENCE` pending authoritative idempotency/cost policy and separate provider-call authorization.
7. No user-authorized Gemini model/budget is available for this phase; zero scored model decisions are therefore expected.

## Acceptance boundary

The offline V38 data-preparation stage is **accepted for research-input construction only**. V38 model-scored decision quality, Gate feasibility, profitability, V42 unattended operation, and any exchange execution are **not accepted**. V41's exact no-regression comparison and local full-suite census pass. Remote provider identity, transport retry semantics, exchange capability, and production execution remain unverified.

## Product-contract supplement

The requested long-term product contract is documented in `docs/plans/AI-Market-Analyst-Product-Contract-V1.md`; the dependency-ordered delivery plan, reuse inventory, gaps, proposed interfaces, and acceptance gates are in `docs/plans/Multi-Asset-Trader-Terminal-Roadmap.md`. These documents do not change the current V38–V42 execution order and do not implement a production multi-asset path. V38 P0 research-readiness work and its no-regression verification were completed first. Production risk settings, fixed 2,000 USDT behavior, historical research artifacts, and account permissions remain unchanged.

## Current Gate 2 source archive and regression snapshot — 2026-10-09

**Disposition: PASS for frozen local source-byte integrity and no-regression verification; overall V38–V42 remains PARTIAL.** The new source mode in `scripts/independent_verify_v38_gate2.py` verified the frozen source manifest and SQLite byte hashes, all 50 monthly archives, all 50 checksum sidecars, and all 50 provenance sidecars. It rehashed 48,627,283 archive bytes without opening SQLite, network access, source writes, archive extraction, model calls, or order calls. The local machine report is Git-ignored at `reports/v38+/verification/v38-source-archive-byte-audit-20261009-v3.json`; canonical report SHA-256 is `5b1b86e5cb04284938aa67a233f618b8e16c97ae2c78386dbaac510d10958347`.

- Focused verifier tests: **19 passed**, comprising eleven new source-bundle tests and eight existing independent Gate 2 checks.
- Full local suite: **2,451 passed, 1 skipped, 0 failed**, with one existing Starlette/httpx deprecation warning. Git-ignored machine evidence: `reports/v38+/verification/v38-source-archive-full-20261009-v2.json` (SHA-256 `cd9881f5a9d0236d4d634a98bf7e071a76cbd6770b7c4c722b391a2e62130458`) and the comparison `reports/v38+/verification/v38-source-archive-full-20261009-v2-vs-baseline.json` (SHA-256 `279972a0c2c3a306dddf099d310d880d8669c806505e869ec45372435411d015`).
- Exact same-environment comparison to baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**. All 100 baseline failure-phase nodes are resolved; all 128 post-baseline test nodes pass; no baseline nodes are missing, no new failures, and no phase changes.
- Full-repository Ruff remains at its pre-existing **4,119** diagnostics. Exact comparison to the frozen Ruff JSON found **0 added and 0 removed** diagnostics; both touched Python files pass targeted Ruff with no findings. The ignored current diagnostic JSON is `reports/v38+/verification/v38-source-archive-ruff-current-20261009-v2.json` (SHA-256 `a4ed7e056067738703030ffd3685453ca1f9aab08286b7851984527d5a492932`). `py_compile` and `git diff --check` pass.
- Fresh primary-data standalone report remains `PASS_WITH_EVIDENCE_LIMITS`: `reports/v38+/verification/v38-gate2-standalone-crosscheck-20261009-v6.json`, report SHA-256 `c6b326634ef7f9aa89503b8df4784831e06d1393512bc857dd90dec007e2b21b`.

The local evidence JSON and source bytes are not included in Git. This verifies byte identity against the previously frozen local manifest and cached sidecars, not fresh source-host authenticity or actual historical receipt times. SQLite schema/table contents and V36 derived features were not independently rebuilt. The V38 input remains preparation-only: labels, Gemini-scored decisions, and executable samples remain zero; the 60-second availability timestamp remains a proxy.

### Product-contract supplement placement

`docs/plans/AI-Market-Analyst-Product-Contract-V1.md` and `docs/plans/Multi-Asset-Trader-Terminal-Roadmap.md` already record the requested terminal contract, reuse inventory, missing modules, interfaces, dependencies, and acceptance gates. They remain design-only. The active V38 Gate 2 integrity P0 is completed before product implementation; subsequent roadmap work is dependency-ordered from `InstrumentSpec`/capability metadata to account-level Gate fee settlement, trader/intelligence interfaces, governed memory, portfolio simulation, and terminal consolidation. The production 2,000 USDT fixed notional, Gate production paths, account settings, Live state, and historical artifacts are unchanged.

## Gate 2 derived-input audit and causality repair — 2026-10-09

The independent standard-library verifier rebuilt all **54** frozen visible V3 market inputs. Every market-input hash and evidence-reference count matched its frozen descriptor. It did not open the 18 untouched-test payloads, source database, or account state and made no network/model calls or orders. The redacted report is Git-ignored at `reports/v38+/verification/v38-market-input-independent-audit-20261009-v4.json`, canonical SHA-256 `c742c56522ab262873cb74e6e5227217b82a47996467dfdee4e3b7f6b27ee655`.

The audit also found and repaired a causal metadata edge case in `market_only._source_provenance`: late duplicate rows sharing the same bar end were included in provenance even when their availability time excluded them from the decision context. The provenance join now also requires the selected row's `available_at`. The full frozen V3 hashes and reference counts remain unchanged. See `V38-derived-market-input-independent-audit.md` for independent method, regression evidence, and limitations.

Verification after this repair: `tests/v38/` **100 passed**; full suite **2,462 passed, 1 skipped, 0 failed**; exact baseline comparison **`PASS_NO_NEW_FAILURES`** (100/100 inherited failure nodes pass, all 139 new nodes pass, 0 new failure or phase-change nodes). Gate 2 remains accepted for input preparation only: historical availability is still a proxy and blind labels, Gemini-scored decisions, and Gate-executable samples remain zero.

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
- This V38 nonoverlap correction is an addition-only update to PR #7. The 80-context dataset and audit JSON remain Git-ignored; the PR contains only plan/code/tests and the acceptance documentation. Auto-merge remains disabled.
- After commit `0baa0e2844d2cf96d41a5bdd0461fb793acfd381`, GitHub reports PR #7 `OPEN`, merge state `CLEAN`, and `autoMergeRequest: null`. No merge was performed.
- Earlier commit `e34e78c` used the disclosed pre-commit hook bypass while inherited failures remained. Current commit `0baa0e2` passed the normal full-suite pre-commit gate.

## Remaining follow-up

Human review of the V41 evidence remains open. V42 still requires independent data/schema review, a verified provider retry/idempotency policy, and separate explicit authorization for any model and budget. Nothing in this PR authorizes exchange access, Live mode, or orders.


## Latest additive verification snapshot — 2026-10-09

This section supersedes the earlier full-suite count above for the current V39/V40 research chain; the earlier counts and their machine reports are retained as historical checkpoints.

- V39 progressive consultation follow-up: focused Gate 3 suite **154 passed**. Incomplete SSE can stream partial deltas but must end in an error, never a completion event or receipt.
- V40 duplicate-bar replay follow-up: focused V35/V40 economics suite **97 passed**. A bar that already filled an order is idempotent when redelivered to the original simulator or restored checkpoint.
- Latest full suite with frozen evidence: **2,419 passed, 1 skipped, 0 failed** across 2,420 collected nodes. Exact same-environment comparison to V37.1 baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664` is `PASS_NO_NEW_FAILURES`: all 100 inherited failure nodes remain resolved, all 96 post-baseline nodes pass, and there are no missing baseline tests, new failures, or phase changes. Local machine evidence is Git-ignored under `reports/v38+/verification/`.
- PR #9 remains open, clean, and without auto-merge. This V40 replay test is an additive change on the V39 branch and does not modify the simulation engine or production execution path.
- Gate 4 evidence proves local OHLCV replay behavior only. It does not establish venue fill idempotency, Gate market data or margins, profitability, or live readiness. No model/provider call, exchange request, order, or production setting change occurred.

Overall status remains **PARTIAL**. Gate 2 has zero blind labels and zero scored Gemini decisions; V42 remains gated on independent review, provider semantics and a separate model/budget authorization. Research success and deployment success are not established.

### Distance to the final success criteria

| Criterion | Current evidence | Status | Remaining gap |
| --- | --- | --- | --- |
| Engineering (S1) | Local deterministic replay, causal-input, transport, identity, and risk tests; 2,419 passed / 1 skipped; exact baseline comparison has zero new failures. | **PARTIAL** | Stacked PRs remain open; no CI result or live provider/venue integration evidence. Venue event idempotency, protective-order failure recovery, and a sustained operational run are not proven. |
| Research (S2) | V38 V3 has 72 paired market contexts; historical availability is an assumed proxy. V25 A0 remains 0/100; current blind labels, scored Gemini decisions, verified executable samples, and complete closes are 0. | **NOT ESTABLISHED** | Requires independent labels, separately authorized model/budget, point-in-time execution evidence, and at least 30 independent complete closes per evaluated strategy plus preregistered economics/uncertainty criteria. |
| Deployment (S3) | No shadow window, TestNet/Live session, or order was run. | **NOT AUTHORIZED** | Requires a separately approved real-time shadow protocol and observation window, operational/error/stop drills, verified venue/product/account rules, then explicit human deployment approval. |

These gaps are requirements, not predicted outcomes. None of the current tests or reconstructed Binance samples proves positive expected value or authorizes trading.

### Gate 2 fresh reproducibility recheck — 2026-10-09

The read-only audit rerun reproduced the V3 manifest, 54 visible inputs, 18 sealed test hashes, archive hashes, zero overlaps, and exact blind-label registry binding. Its output was byte-identical to the earlier final V3 audit. Fresh `tests/v38/` run: **68 passed**. The recheck reused the existing committed auditor implementation; it is reproducibility evidence, not a second independent implementation. It does not change Gate 2's `PASS_FOR_DATA_AND_EVALUATION_PREPARATION_WITH_CAVEATS` boundary: annotations, scored Gemini decisions, executable samples, and complete closes remain 0, and data availability remains a proxy.


## Gate 2 blind-evaluation follow-up — 2026-10-09

The addition-only V3 primary sample uses a hash-frozen seeded monthly/temporal-stratum selector and partition-purged 8-day windows. It contains 72 paired BTC/ETH contexts: 36 optimization, 18 validation, and 18 untouched-test hash-only; the 54 visible inputs were independently rebuilt and the audit found zero same-symbol or cross-partition overlaps. The V3 manifest SHA-256 is `1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a`. Original V1 (180 contexts, 10 cross-partition overlaps) and V2 fixed-grid (80 contexts, no overlap) remain intact and are separately reported; they are not pooled with V3.

Blind reference-label protocol V2 is frozen at canonical SHA-256 `14827250890d37e33fc0b25121ca0414065e9e835c6beef830c95ed2a3fda64c`. It accepts only the V3 primary dataset and binds its 54 visible decision IDs to exact causal-input hashes; two independent blinded raters, UNKNOWN, disagreement preservation, exact manifest/input binding, and metric denominators are specified. Labels remain 0. Protocol V1 was superseded before any annotation. A1/A2 provider/model identities and spend are unselected/unauthorized; A3 has no historical point-in-time bid/ask; B0 is omitted; V25 A0 remains 0/100.

V3 focused selection tests and independent source/data audit are reported in `V38-gate2-blind-evaluation-preregistration.md`. The final local full-suite evidence-recorded run completed with **2,399 passed, 1 skipped, 0 failed**; the exact baseline comparison resolved all 100 baseline failure nodes/phases, passed all 76 added nodes, and found no new failures, missing baseline nodes, or phase changes. All research outcomes remain null or `NOT_RUN`; zero close samples cannot support a strategy-quality or profitability claim.

The previously noted Codex `gpt-5.6-sol` capability probe may have initiated an inference request before interruption. It returned neither a response nor usage receipt, so service/quota status is unverified. Gemini research calls for V38 remain zero; the incident is kept distinct.

## Current V38/V39 status correction — 2026-10-10

This update supersedes the earlier “Gemini research calls for V38 remain zero” snapshot. The authorized V38 Gemini campaign completed its fixed optimization-only ceiling with **72 dispatch intents, 71 terminal results, 67 `INVALID_JSON`, 4 `CALL_ERROR_NO_RETRY`, 1 `AMBIGUOUS_NO_RESULT_NEVER_RETRY`, and 0 valid analyses**. Each of the 67 HTTP 200 results identified `gemini-3.8-flash-high`; strict content/schema validation rejected them as `COMPLETION_CONTENT_NOT_JSON`. The 67 usage receipts do not provide a verifiable cost total, and no 429 was observed; the relay's weekly quota reaching zero is not established. The operator later corrected the local route, but that does not replenish the exhausted study authorization. Do not alter or resend any prior result. A future run needs a new frozen prompt/schema and bounded authorization, beginning with a small fresh optimization-only parseability check. Full ledger evidence, report hashes, and execution boundaries are in `V38-gemini-smoke-followup.md`.

V39 also has a local timeout-boundary repair in the current working change. The new deterministic regression reproduced a `30.000000000000057` second request against a 30-second cap before the fix; after clamping the computed remainder, the focused affected command passes **35 tests**. The same two files retain their 7 pre-existing Ruff diagnostics, with no new diagnostics. Full local suite and remote CI for this patch remain pending. This does not establish upstream retry/idempotency behavior or change production trading behavior. No Gate requests, orders, account-setting changes, or Live activation occurred.
