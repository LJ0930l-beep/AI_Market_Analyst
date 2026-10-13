# V38 Derived Market Input Independent Audit

Date: 2026-10-09
Disposition: **`PASS_WITH_EVIDENCE_LIMITS` for independent reconstruction of the frozen visible V3 market inputs**

## Scope and method

This follow-up closes the prior Gate 2 audit gap around the V36-derived frames, V38 `market_input_sha256`, and `evidence_ref_count`. `scripts/independent_verify_v38_market_inputs.py` is an independent Python-standard-library implementation. It imports no `core/` or project `scripts/` code and reconstructs the causal bar selection, evidence references, V36 objective facts, provenance, and canonical V38 input hash.

The audit read the frozen sample plan, dataset manifest, and visible `optimization-validation-inputs.json`. It did not open `untouched-test-sealed-manifest.json`, inspect the source database, access an account, call a model or network, or create an order. The report writer uses exclusive creation outside the dataset directory. The source archive byte audit is a separate check recorded in the preceding addendum of `V38-gate2-standalone-crosscheck.md`.

## Result

- Frozen plan canonical SHA-256: `094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f`.
- Dataset manifest canonical SHA-256: `1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a`.
- Visible input file SHA-256: `4fd074eafb73ecd113c8734c5417c05878b4ee6ad6afbcb75f736b74c173399d`.
- Independent auditor source SHA-256: `5445be9ead258b9a41bca8c3a67b75ac06375e50c518039cccd02e73aeab876f`.
- All **54/54** visible inputs were independently reconstructed; **54/54** market input hashes and **54/54** evidence-reference counts match the frozen descriptors (36 optimization, 18 validation).
- The untouched-test payload was not read. Its 18 hash-only descriptors remain sealed and unused.
- Local machine report: `reports/v38+/verification/v38-market-input-independent-audit-20261009-v4.json` (Git-ignored); canonical report SHA-256: `c742c56522ab262873cb74e6e5227217b82a47996467dfdee4e3b7f6b27ee655`.
- The report records 0 network calls, 0 model calls, 0 orders, no account/database opening, no untouched-test payload reads, and 0 source writes.

## Causality repair

A new regression test exposed that a late duplicate row with the same timeframe and bar end as a selected causal row could contribute its later `available_at` and archive hashes to V38 provenance, even though the V36 context correctly excluded that row. The resulting market input depended on metadata unavailable at decision time.

`core/replay/pa_decision_quality_v38/market_only.py` now joins provenance to the context-selected row by `(timeframe, bar_end, available_at)`. The independent implementation applies the same selection key without importing the application code. The regression confirms that adding a same-close-time record unavailable at the decision does not alter the input hash; all 54 frozen visible descriptors remain unchanged.

## Verification

- `tests/v38/test_independent_market_input_audit.py`: **11 passed**.
- `tests/v38/`: **100 passed**.
- Full suite with frozen evidence collection: **2,462 passed, 1 skipped, 0 failed**, with one existing Starlette/httpx deprecation warning.
- Exact same-environment comparison to frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**. All 100 inherited failure nodes pass; all 139 post-baseline test nodes pass; 0 baseline nodes are missing, 0 new failures, and 0 phase changes.
- The full-repository Ruff census remains at the frozen **4,119** diagnostics, with **0 added / 0 removed**. All changed Python files pass targeted Ruff. `py_compile` and `git diff --check` pass.
- Local ignored full-suite report canonical SHA-256: `dedbe40e9576b5eaa5797e10aec068b9dd9fc10c7702306325a0a60e4852d7df`; baseline-comparison report canonical SHA-256: `de12737567fecbefde1dbb640a7a73858f66375af0a46f5f0a3cb3ceb5e6485e`.

## Limits and acceptance boundary

This verifies reproducibility of frozen market-only research inputs. Price evidence is Binance UM archive reconstruction, not Gate quotes. Historical availability remains an assumed `bar_end + 60 seconds` proxy. There are still 0 independent human labels, 0 scored Gemini decisions, 0 Gate-executable proposals, 0 fills, and 0 complete closes. The audit does not establish decision quality, positive expectancy, actual execution feasibility, or deployment readiness. V25 A0 remains unrecoverable at 0/100.

No production order path, fixed notional, risk parameter, account setting, Live state, or prior research artifact changed. This audit does not authorize a model call, account access, TestNet, or Live activity.
