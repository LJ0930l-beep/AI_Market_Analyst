# V38 Gate 2 Data and Schema Recheck

Date: 2026-10-09
Disposition: **REPRODUCIBLE DATA PASS WITH EVIDENCE CAVEATS / LABELS NOT RUN**

## Scope

This is a fresh read-only recheck of the current primary V38 V3 dataset and its market-only and blind-label boundaries. It verifies that the local frozen artifacts still reproduce from the locally held public Binance UM archive. It does not replace the previously recorded V38 audits or turn the archive proxy into historical Gate evidence.

The repository's V38 auditor was rerun to a new ignored output file. The source archive loader was inspected and uses SQLite URI `mode=ro` with `PRAGMA query_only=ON`. The dataset, source archive, historical reports, and production settings were not changed. This recheck reuses the committed auditor implementation; it is a fresh reproducibility check, not a second independently implemented auditor.

## Reproduced evidence

- Source database SHA-256: `c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2`.
- Source manifest SHA-256: `2f348e6a190690e173f0237b5f5614102bebeb8e0573a1266014c04f5b25012a`.
- V3 sample plan SHA-256: `094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f`.
- V3 dataset manifest SHA-256: `1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a`.
- Blind-label protocol SHA-256: `14827250890d37e33fc0b25121ca0414065e9e835c6beef830c95ed2a3fda64c`.
- Recheck status: **`PASS_WITH_EVIDENCE_CAVEATS`**; all 50 archive files verify, all 54 visible inputs recompute causally, the 18 untouched-test rows remain hash/coverage only, and same-symbol/cross-partition overlap counts remain zero. The blind-label registry still binds all 54 visible decision IDs and their exact input hashes.
- Observed counts remain 72 contexts (36 optimization, 18 validation, 18 untouched-test), 36 paired BTC/ETH anchors, 0 annotations, 0 scored Gemini decisions, 0 Gate-executable proposals, 0 fills, 0 complete closes, 0 orders, and 0 network calls.
- Price grade remains `VERIFIED_ARCHIVE_RECONSTRUCTION`; availability remains `ASSUMED_PROXY` using `bar_end + 60 seconds`. Historical Gate receive times, account state, product rules, news, and point-in-time bid/ask remain unavailable.

The new ignored audit JSON is byte-for-byte identical to the existing V3 integrity audit output: both have file SHA-256 `beb2a5682091671463fa767db9d88407b233d7f8fa1f7cf580c67df17e6be8db` and embedded canonical `report_sha256` `c9a71e8fb1f6a062058ec0ae7a23e1f5298b36ee60c671f6414818657fc7fa15`. The new file is `reports/v38+/verification/v38-gate2-independent-review-20261009.json`; reports remain Git-ignored.

## Schema and causality review

- `build_market_only_input` rejects unregistered top-level fields, requires the frozen partition label and the four required timeframes, checks each `available_at` is not before confirmation, and requires the latest availability for every timeframe to be strictly earlier than the decision time.
- The V3 selector is deterministic under the frozen seed and month/slot strata. Validation requires exact registered anchors, half-open partition membership, 8-day windows contained inside their partition, paired symbols, and zero overlap metadata; the fresh dataset audit recomputed these constraints from the source archive.
- The blind-label validator binds each annotation to the frozen manifest, registered decision ID, exact market-input hash, partition, and allowed evidence references. It refuses untouched-test annotations and model/outcome fields. The frozen protocol preserves `UNKNOWN` and rater disagreement and requires two independent raters.
- Fresh `tests/v38/` run: **68 passed**. Coverage includes future-available inputs, modified partition/anchor rejection, exact input binding, unknown/disagreement preservation, evidence-reference validation, and sealed-test refusal.

## Remaining blockers and limits

- Gate 2 is accepted for **data construction and evaluation preparation only**. It is not evidence of market-decision quality or strategy profitability.
- No independent human annotations exist. The minimum 30 verified complete closes per strategy remains unmet; no performance metric is inferable.
- A1/A2 model identity and paid-call budget remain unauthorized; no Gemini call was made. V39 provider idempotency and duplicate-cost semantics remain blocked.
- The 60-second availability assumption is not Gate receive-time evidence. No historical Gate quote, account, settlement, fee/rebate, or real execution record is available.
- The 36 paired temporal anchors are a small pilot set, not IID trades. Their paired rows must not be counted as 72 independent market episodes.

## Acceptance

This recheck confirms the frozen V3 data and schema preparation are reproducible in the current environment, with the evidence caveats above. Gate 2 decision-quality research remains **NOT RUN**. No model/provider call, exchange request, order, TestNet/Live session, or production risk change occurred.
