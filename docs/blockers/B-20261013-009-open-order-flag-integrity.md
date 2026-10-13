# B-20261013-009: Gate ordinary-order reduce-only evidence must fail closed

```yaml
id: B-20261013-009
severity: S1
status: FIX_IN_REVIEW
component: gate
handbook_mapping: B16
mode: RESEARCH_OFFLINE
branch_sha: 5bce67239ccf8e608993a901dab020cfa0a8e8f8
trigger_signature: GATE_OPEN_ORDER_REDUCE_ONLY_FLAG_UNVERIFIED
first_seen_at_utc: 2026-10-13T06:46:05Z
user_authorization: DOCUMENTED_SCOPE_ONLY
authorization_scope: OFFLINE_REVIEW_AND_TESTS_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: POTENTIAL
risk_detail: ACCOUNT_TRUTH_MISCLASSIFICATION_NOT_OBSERVED
affected_sample_or_order_ids: fixture-only; no exchange order IDs
immediate_stop_or_isolation: Do not make Gate/TestNet requests. Invalid or conflicting reduce-only fields must degrade the account snapshot and block consumers that require complete pending-order truth.
evidence_files_and_hashes: "Historical review attachment C:/Users/baicha/Downloads/AI_Market_Analyst_PR27_G0_%E7%8B%AC%E7%AB%8B%E5%A4%8D%E5%AE%A1_20261013.md, target HEAD f856859cb83517729b28a56e3ed310f920ee643f, SHA-256 A895236DA9E721DAB141E2B723DB61D3F11AF758B08F4C59DAF788249A6F1802; follow-up: docs/audits/V2-G0-G1-pr27-independent-review-followup-20261013T050106Z.md SHA-256 23893BB32896A3B04CE9E4C099B933EE4BF946849A3A1D0B9BC2B7CBC8DDC349; current candidate adapter: core/trading/gate_live_client.py SHA-256 AF3A5899427763CF41E1E08176F73E0C4076CDCFB5A9D17234332105B0F0F509; regression tests: tests/test_gate_account_chain.py SHA-256 39EBB5292F949C90FEFECD06E37AC2A24427BDA37A31F7C94960843E09F74A4D"
reproduction: py -3 -m pytest -q tests/test_gate_account_chain.py tests/test_gate_testnet_ai_chain.py -k open_order_reduce_only
root_cause_status: CONFIRMED
safe_fallback: Parse CCXT reduceOnly and Gate info.reduce_only/is_reduce_only only as canonical booleans; invalid or conflicting evidence normalizes conservatively and marks the account truth DEGRADED.
patch_pr: https://github.com/LJ0930l-beep/AI_Market_Analyst/pull/27
repair_plan: Parse every present CCXT/Gate reduce-only alias strictly; invalid or conflicting values degrade account truth and normalize conservatively. Implemented in commit 14e732546991c55d5a84c96631126dbadfc501bc; the legacy generic boolean parser remains unchanged.
negative_tests: Values 2, -1, 1.5, NaN, infinity, and "2" for each supported field; conflicting normalized/native aliases; canonical true/false/0/1 forms remain accepted.
unblock_criteria: Same-environment full-suite comparison has no missing baseline nodes, lost passes, new failure phases, unreported nodes, or added nonpassing tests; exact pushed SHA Python/Web CI succeeds; independent review and G0 decision are recorded.
rollback: Never restore truthy-number coercion. If this patch regresses, keep Gate account truth degraded until an equivalent strict parser is available; do not use an incomplete snapshot for new-risk or cleanup decisions.
needs_human_decision: true
next_owner_action: Obtain independent review of ordinary-order flag normalization on PR #27 and record the human G0 decision; do not use incomplete account truth for execution meanwhile.
```

## Root cause and impact

At the observed candidate head, `get_open_orders()` used `_optional_bool()` for both CCXT `reduceOnly` and Gate `info.reduce_only`. That helper converts any finite nonzero number to `True`. The normal-order path did not check the Gate `info.is_reduce_only` alias or contradictory values. `get_account_truth()` uses the resulting flag when classifying unreserved pending orders; an invalid value could therefore be represented as verified reduce-only evidence while the endpoint status remained `AVAILABLE`.

The attached PR #27 review identified strict boolean handling for the protection-order path. Its repair covered native trigger/protection reads, but not the separate ordinary open-orders path. This follow-up maps to handbook B16. The finding is from source review and fake responses only; no real account snapshot was requested.

## Repair and verification

The local repair parses CCXT `reduceOnly`, Gate `info.reduce_only`, and Gate `info.is_reduce_only` using the strict Gate boolean parser. Any malformed value or disagreement sets a stable error code, reports `DEGRADED`, and normalizes `reduce_only` to `false` so an order is treated conservatively. The shared `_optional_bool()` implementation is unchanged.

Verification evidence:

- Focused Gate and comparator tests: **140 passed**, one existing Starlette/httpx deprecation warning.
- Baseline full suite at `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **2,220 passed, 103 failed, 1 skipped** (exit 1).
- Candidate full suite: **2,678 passed, 1 failed, 1 skipped** (exit 1). The sole remaining failure is the same baseline node: `tests/test_trader_reliability_v13.py::test_v13_c_runtime_lease_loss_cancels_ai_and_pauses_session`.
- Same-environment node comparison: **PASS**; both runs completed, zero missing nodes/phases, zero lost passes, zero new failure phases, 356 additional nodes all passed, zero unreported added nodes. It identified 102 baseline failures that now pass and the one unchanged baseline failure above.
- Raw local reports remain outside the repository. SHA-256: baseline `f45ee91b5093320991c1405bf513b54cbcb363b8ceccb90731933b6ca7289afb`; candidate `221f0bebe1a1096e0e8f1a161c9d9e78926a744d558e5a01d3dcd5b16b20c5f1`; comparison `097662605d786b599ff0677be18aba9753688a9ada4442b0a6b13a2254c04dd7`.
- `git diff --check` passed. Filtered Ruff inspection found no diagnostics on changed lines; two `F601` duplicate-key diagnostics remain on unchanged lines of `gate_live_client.py` and are not claimed as a clean Ruff run.

The comparator repair permits newly added tests only when each has a recorded outcome and every added outcome passes. It still rejects missing baseline nodes, lost passes, unreported nodes, new failure phases, and any newly added failing, skipped, xfail, xpass, error, or collection-error outcome. The old test ID accidentally changed during an earlier draft was restored before the final comparison; the final comparison reports zero missing nodes and phases.

No Gemini/Antigravity/Gate/TestNet request, order, Live action, production-setting change, ledger edit, or historical-sample edit occurred. Exact pushed-SHA CI and independent review remain pending.

## Hosted repair evidence — 2026-10-13T09:24:06Z

The ordinary-order strict-flag repair is commit `14e732546991c55d5a84c96631126dbadfc501bc`; its exact-head run [38363964249](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38363964249) completed successfully. The later source candidate `40f65d7b05675ef2f7df69450af7d20c4418eb6e` also passed exact-head run [38367957079](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38367957079). The current PR head is documentation-only `90344019d20a7db1464ddd7e82f3378bd3e791de`; run [38374291874](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/38374291874) has Web checks passed and Python full suite still in progress. This does not count as independent approval; keep G0 unaccepted and make no Gate/TestNet requests.

## Exact-head CI completion — 2026-10-13T09:47:51Z

Run `38374291874` completed SUCCESS on PR #27 head `90344019d20a7db1464ddd7e82f3378bd3e791de`: quick 289 passed; full suite 2,683 passed, 25 subtests passed, one existing deprecation warning; Web tests, lint, type-check, and build passed. The strict flag parser is included. Independent review/G0 acceptance remains pending; no Gate/TestNet request was made.
