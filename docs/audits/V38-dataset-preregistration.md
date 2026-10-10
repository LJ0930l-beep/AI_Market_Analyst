# V38 Dataset Preregistration and Freeze

## Frozen selection

Plan `V38_BINANCE_BTC_ETH_MARKET_CONTEXTS_20261009_V1` was frozen before the first retained dataset build. The first local build (`dataset-20261009-v1`) was created while the declared UTC freeze timestamp still pointed into the future. That artifact is preserved, marked `SUPERSEDED`, and excluded from analysis. The freeze-date metadata was corrected before the retained v2 build; all sample-selection parameters stayed unchanged. This timing wrinkle is disclosed rather than treating the superseded output as valid preregistration evidence.

The corrected plan is outcome-blind and selects deterministic UTC calendar-day anchors using SHA-256 ranking, a 48-hour minimum gap per symbol/partition, and monthly quotas. It does not use outcomes, A3 triggers, direction, volatility, or model output for selection.

Plan SHA-256: `e08254d847ccfe6280d3cd209bbc53478efcb8eb60a93e54316b48e70dd4e02a`.

| Partition | UTC interval | BTCUSDT | ETHUSDT | Total |
| --- | --- | ---: | ---: | ---: |
| optimization | `[2025-10-01, 2026-04-01)` | 30 | 30 | 60 |
| validation | `[2026-04-01, 2026-07-01)` | 30 | 30 | 60 |
| untouched_test | `[2026-07-01, 2026-10-01)` | 30 | 30 | 60 |

Dataset manifest SHA-256: `ca0786a4acdec7a7410b7df89e947bc2b698377fed69a13fd865fe4d1187bd99`.

## Data and evidence grade

- Source: locally verified Binance UM official monthly archives; 50 source archive files were checksum-verified. No new download was made.
- Source database SHA-256: `c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2`.
- Price evidence: `VERIFIED_ARCHIVE_RECONSTRUCTION`.
- Availability evidence: `ASSUMED_PROXY`, using `bar_end + 60 seconds`. This is not historical Gate receive-time evidence.
- 120 optimization/validation points contain full causal market-only input. The 60 untouched-test points persist only decision hashes and coverage; no market input or labels are retained for them.
- The 8-day context windows form 23 overlap clusters. Therefore 180 rows are not 180 independent trade samples.
- No account state, contemporaneous order book, contract snapshot, model output, fill, or completed close is present. Gate-executable sample count is 0.

## Reproducibility and limits

The current dataset is local under ignored `reports/v38+/dataset-20261009-v2/`; the earlier UTC-freeze mistake remains separately marked superseded under `dataset-20261009-v1/` and was not overwritten. The current manifest records file hashes and partition counts. All 120 retained inputs were rebuilt through the V38 causal-input validator and matched the manifest hashes. The untouched-test artifact was checked to contain no market payload.

V25 A0 exact recovery remains `0/100`; these new archive contexts do not alter or repair that historical finding. Model calls, outcome labels, earnings evaluation, and orders are all zero.

**Capacity statement:** 120 inputs are staged for future controlled analysis on optimization/validation only. Gemini authorization and scored observations are both zero. The held-out 60 are sealed and must remain untouched until a separately approved one-time evaluation.
