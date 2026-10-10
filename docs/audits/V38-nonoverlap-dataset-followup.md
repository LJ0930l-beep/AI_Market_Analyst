# V38 Nonoverlap Dataset Follow-up — 2026-10-09

## Disposition

**`PASS_WITH_CAVEATS` for frozen, causal market-input construction and overlap control.** This is an addition-only companion dataset. It does not overwrite or relabel the original V38 V1 plan, dataset, reports, V25 samples, or V25–V37 evidence. It made no Gemini/provider call, network request, private exchange request, or order, and changed no production risk setting.

## Why a companion dataset was needed

The original V38 V1 plan staged 180 contexts (120 optimization/validation payloads and 60 hash-only untouched-test rows). Its 8-day input windows overlapped heavily. The preserved V1 global audit found 20 connected components and 10 direct cross-partition overlapping pairs across two spanning components. Several partition/symbol groups were dominated by a single component, so nominal row counts materially overstated the number of distinct market episodes. The original V1 audit, data, and hashes remain intact; V1 is not a valid basis for independent-row claims.

The companion plan uses fixed UTC calendar anchors, the already frozen research/optimization, validation, and untouched-test boundaries, and eight-day half-open input windows. Anchors are spaced nine days apart; the first anchor is eight days after each partition start. Every window stays inside its partition. BTC and ETH share each anchor and are explicitly paired, not counted as independent observations. Selection is deterministic and does not inspect outcomes, direction, volatility, model output, or A3 triggers.

## Frozen artifacts and data evidence

- Companion plan: `V38_BINANCE_BTC_ETH_NONOVERLAP_CONTEXTS_20261009_V1`; SHA-256 `561e2b78bb046468c2d07d6378f913b35aee0f551ab50c8568c8021ca336bf9a`.
- Parent V1 dataset manifest SHA-256: `ca0786a4acdec7a7410b7df89e947bc2b698377fed69a13fd865fe4d1187bd99`.
- Companion dataset manifest SHA-256: `1d76f1cd0e2eaa658eb8d32decc434a10e151cb7119a6b2a4f85801639537785`.
- Local audit `report_sha256`: `e755f12b8de361ace1cee5a3f058ad5743fad9101749ca320620b083498bc64e`; final report file `reports/v38+/verification/v38-nonoverlap-integrity-20261009-v4.json` is Git-ignored.
- Deterministic rebuild manifest hash matched the retained companion dataset exactly. Rebuild output is also local and ignored.
- Source database SHA-256 `c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2`; source manifest SHA-256 `2f348e6a190690e173f0237b5f5614102bebeb8e0573a1266014c04f5b25012a`.
- All 50 source archives passed size and SHA-256 verification. All 24 archive hashes referenced by the companion descriptors were present in the verified source manifest.
- Price evidence is `VERIFIED_ARCHIVE_RECONSTRUCTION` from Binance UM public archives. Availability evidence remains `ASSUMED_PROXY` (`bar_end + 60 seconds`), not a measured historical Gate receive time.

## Sample capacity and interpretation

| Partition | Paired UTC anchors | BTC/ETH contexts | Stored content |
| --- | ---: | ---: | --- |
| Optimization | 20 | 40 | Market-only payloads |
| Validation | 10 | 20 | Market-only payloads |
| Untouched test | 10 | 20 | Hashes and coverage only; payload remains sealed |
| **Total** | **40** | **80** | **60 visible contexts; 20 sealed hash-only rows** |

The independent auditor recomputed 60 visible input hashes and checked 11,460 included OHLCV bars for exact bar intervals, source provenance, geometry, and availability strictly before decision time. All 80 declared windows form singleton same-symbol overlap components; no same-symbol windows overlap within or across partitions. BTC and ETH contexts at a shared timestamp are paired. The 20 optimization anchors, 10 validation anchors, and 10 sealed-test anchors are temporal pilot points, not IID trades, powered evidence, actual model decisions, executable orders, fills, or completed closes.

The currently materialized maximum for a future separately authorized model study is 60 visible optimization/validation contexts. Access to the sealed test remains unavailable for model input until a preregistered final evaluation. This document does not authorize paid model calls; scored Gemini observations remain zero.

## Verification

- `python -m pytest -q tests/v38/test_nonoverlap_dataset.py tests/v38/test_dataset_integrity_audit.py`: **19 passed**.
- `python -m pytest -q tests/v38`: **42 passed**.
- Ruff checks for the new planner, builder, auditor, and affected tests: **passed**.
- Python compile check for the new planner, builder, auditor, and tests: **passed**.
- Full suite with the immutable evidence recorder: **2,373 passed, 1 skipped, 0 failed** (2,374 collected; one Starlette/httpx deprecation warning).
- Exact comparison to frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**; all 100 baseline failure-phase nodes resolved, all 50 new test nodes passed, 0 missing baseline nodes, 0 new failures, and 0 phase changes. Machine evidence remains local under ignored `reports/v38+/verification/`.
- Builder and auditor CLIs are local-only. Dataset and report paths are under ignored `reports/v38+/`; no research payload or raw archive is added to Git.

## Limits and acceptance boundary

1. Binance UM archives are not historical Gate prices, Gate account state, contemporaneous contract rules, order-book quotes, or Gate receive-time evidence.
2. The 60-second availability assumption is a proxy; it cannot support claims requiring verified point-in-time Gate availability.
3. The dataset contains no outcomes, labels, Gemini decisions, account state, orders, fills, or closes. It supports data/schema and overlap review only, not profitability or trade-quality conclusions.
4. Ten validation and ten untouched-test temporal anchors are small pilots. Paired assets and serial market regimes remain dependent even when bar windows do not overlap.
5. V25 A0 remains unrecoverable (`0/100` exact original decisions); no reconstructed context changes that result.
6. The sealed test remains hash-only and must not inform prompt, policy, strategy, or hypothesis changes before its predeclared final evaluation.

V38 market-input construction and overlap control are accepted with the stated evidence limits. A controlled Gemini study still requires a separate explicit model/budget authorization, reviewed prompt/schema and stopping rules, and the V39 provider retry/idempotency boundary. No evidence here authorizes exchange access, Live mode, account changes, or orders.
