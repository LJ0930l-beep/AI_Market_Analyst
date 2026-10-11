# V38 Data Integrity Follow-up — 2026-10-09

## Disposition

**`PASS_WITH_CAVEATS` for local market-input construction and integrity only.** This follow-up does not accept Gemini decision quality, Gate execution feasibility, trading profitability, or deployment readiness. It makes no provider or exchange request, submits no order, and changes no production risk setting.

## Scope and changes

- Added `scripts/audit_v38_dataset_integrity.py`, a read-only auditor for the frozen V38 dataset, source database, local public-archive files, point-in-time bar constraints, partition coverage, sealed-test contents, and global cross-partition input-window overlap.
- Fixed direct-script repository imports for `scripts/build_v38_market_dataset.py` and `scripts/run_market_only_v38.py`; module execution already worked, while documented `python scripts/...` invocation failed to import `core`.
- Added `tests/v38/test_dataset_integrity_audit.py` for overlap accounting, malformed interval rejection, frozen-source binding, and direct CLI help.
- Added this acceptance record and refreshed only the current master status and gate summaries. The prior V38 checkpoints, source archive, dataset v2, plan, and historic V25–V37 reports were not overwritten.
- The long-term product contract and multi-asset terminal roadmap are already present in `docs/plans/AI-Market-Analyst-Product-Contract-V1.md` and `docs/plans/Multi-Asset-Trader-Terminal-Roadmap.md`. They remain design-only and do not reprioritize active V38–V42 work.

## Data evidence

- Frozen sample plan SHA-256: `e08254d847ccfe6280d3cd209bbc53478efcb8eb60a93e54316b48e70dd4e02a`.
- Retained V38 dataset manifest SHA-256: `ca0786a4acdec7a7410b7df89e947bc2b698377fed69a13fd865fe4d1187bd99`.
- Independent final audit report SHA-256: `6a1e8cf684e87568fafeeb4069eea28c83d5bae3977ba79be74eb8a54ebe7173`.
- The report verified all dataset output-file hashes, the plan binding, **120** reconstructed market-input hashes, **22,920** OHLCV bars across `15m/5m/1h/4h`, 60 hash-and-coverage-only untouched-test rows, the local database and source-manifest hashes, and **50** raw archive file hashes/sizes. All **20 distinct raw archive hashes referenced by selected inputs** are members of that verified source manifest.
- A fresh local rebuild reproduced the retained dataset manifest hash exactly. It wrote to a separate ignored directory; the retained V2 artifact was not modified.
- Source evidence grade is `VERIFIED_ARCHIVE_RECONSTRUCTION` for Binance UM public archive prices. Availability grade is `ASSUMED_PROXY`: each bar uses `bar_end + 60s`, not an observed historical Gate receive timestamp.

## Overlap accounting correction

The stored `overlap_cluster_count=23` is computed per symbol and partition. Rechecking all half-open 8-day input windows per symbol, across all partitions, yields **20 global connected components** and **10 direct cross-partition overlapping pairs** contained in **2 components that span partition boundaries**. The 23 and 20 values use different scopes and are not contradictory. A component is not an independent sample, and these counts do not imply 20 independent trades. The audit enumerates direct pairs to prevent a partition-local cluster count from hiding links across boundaries.

No outcomes, labels, or model results were read or included in this calculation. The untouched-test partition remains hash/coverage-only and contains no market payload or labels.

## Verification

- `python -m pytest -q tests/v38/test_dataset_integrity_audit.py`: **8 passed**.
- `python -m pytest -q tests/v38`: **31 passed**.
- Full suite with the immutable evidence recorder: **2,362 passed, 1 skipped, 0 failed** (2,363 collected); one Starlette/httpx deprecation warning was reported.
- Exact comparison to frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**; all 100 baseline failure-phase nodes resolve, all 39 added test nodes pass, with 0 missing baseline nodes, 0 new failures, and 0 phase changes.
- `ruff check scripts/audit_v38_dataset_integrity.py scripts/build_v38_market_dataset.py scripts/run_market_only_v38.py tests/v38/test_dataset_integrity_audit.py`: **passed**.
- `python -m py_compile` on the same four files: **passed**.
- Direct `--help` invocation for the builder, offline runner, and auditor: **passed**.
- A reproducible local notebook verifies the report hash and key invariants: `reports/v38+/verification/v38-data-integrity-followup-20261009-v2.ipynb` (local, Git-ignored).
- Machine-readable report: `reports/v38+/verification/v38-data-integrity-followup-20261009-v2.json` (local, Git-ignored).
- Machine-readable full-suite comparison: `reports/v38+/verification/v38-data-integrity-full-suite-20261009-vs-baseline.json` (local, Git-ignored).

Reproduction commands (run from the repository root; each output path is write-once):

```powershell
python scripts/audit_v38_dataset_integrity.py --dataset "reports/v38+/dataset-20261009-v2" --archive "D:\RJ\codex\ai-market-analyst\reports\btc-eth-year-proxy-20261004" --rebuild-dataset "reports/v38+/dataset-rebuild-20261009-v1" --output "reports/v38+/verification/v38-data-integrity-followup-20261009-v2.json"
python -m pytest -q tests/v38
python -m pytest -q -p scripts.v36_test_evidence "--v36-report=reports/v38+/verification/v38-data-integrity-full-suite-20261009.json"
python scripts/compare_v37_test_evidence.py "reports/v38+/verification/full-suite-test-evidence-baseline-64a5.json" "reports/v38+/verification/v38-data-integrity-full-suite-20261009.json" --output "reports/v38+/verification/v38-data-integrity-full-suite-20261009-vs-baseline.json" --baseline-commit 64a5c4206a5073e0c44e9d5cc4178705ffa24664
```

The notebook JSON and assertions are checked by executing the code cell with the system Python interpreter. This environment has no Jupyter kernel, so there is no saved notebook execution output bundle.

## First controlled Gemini research capacity

At most **120** prepared observations are available for an initially authorized optimization/validation study. The **60** untouched-test rows must remain sealed until a preregistered final evaluation. Gemini-scored observations remain **0**; this work does not authorize any paid model call. The 120 observations have reconstructed Binance price evidence and only proxy availability evidence. They do not prove Gate prices, account state, order-book liquidity, executable size, fills, labels, or complete closes.

## Remaining limits and V38 acceptance boundary

1. Actual Gate receive/availability timestamps and historical Gate prices are not in this dataset.
2. No account snapshot, contract-rule snapshot, quote/order book, provider response, fill, outcome label, or complete close exists in the V38 prepared contexts.
3. No Gemini quality or profitability inference is supported. V25 A0 remains unrecoverable as previously recorded.
4. The dataset is accepted for local, causal market-input construction and provenance checking with the stated timestamp proxy. It is **not** an executable trade sample set or an accepted trading study.
5. Existing V35 risk enforcement, the production fixed 2,000 USDT behavior, saved account settings, and all historical reports remain unchanged.
