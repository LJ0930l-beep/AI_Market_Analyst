# V38 Blind Label Handoff Tooling Audit — 2026-10-09

## Result

**Tooling status: READY FOR INDEPENDENT MANUAL REVIEW. Research status: NOT SCORED.** The packet builder and importer are offline-only. No human rater has received a packet, no labels have been generated from the real dataset, and no decision-quality or profitability conclusion follows from this work.

## Scope and immutable inputs

- Dataset: `V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1`.
- Dataset manifest SHA-256: `1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a`.
- Visible input file SHA-256: `4fd074eafb73ecd113c8734c5417c05878b4ee6ad6afbcb75f736b74c173399d`.
- Frozen annotation protocol: `V38_PA_REFERENCE_BLIND_LABELS_20261009_V2`, SHA-256 `14827250890d37e33fc0b25121ca0414065e9e835c6beef830c95ed2a3fda64c`.
- Visible coverage: 54 market contexts (36 optimization, 18 validation). Untouched-test coverage is 18 hashes only; no test payload or labels are in the rater packet.

The original V25–V37.1 evidence, dataset files, manifest, label protocol, and production trading/risk paths were not modified. Packet generation reads the dataset manifest and visible input file. The code does not open the sealed-test file. Tests also build a fixture with no sealed file present and successfully create/import packets. This is code-path and fixture evidence, not OS-level file-access telemetry.

## Changes

- Added `core/replay/pa_decision_quality_v38/blind_label_packets.py` with exact manifest/input/protocol hash binding, randomized per-packet order and item IDs, separate escrow, reviewer HTML rendering, strict importer validation, and exclusive output creation.
- Added `scripts/build_v38_blind_annotation_packet.py` and `scripts/import_v38_blind_annotation_submissions.py`. Both constrain outputs to Git-ignored `reports/v38+/`; the builder does not emit labels, and the importer requires all 54 contexts from each of at least two distinct pseudonymous IDs.
- Added `scripts/templates/v38-blind-label-packet.html`, a self-contained reviewer interface with a restrictive CSP (`connect-src 'none'`), no external asset links or network APIs, structured evidence references, local progress restore/export, and validation before collecting labels.
- Review of the first rendering found that exact calendar dates could reveal the chronological split even when the explicit partition field was omitted. Packet/escrow schema 2 now freezes `V38_RATER_RELATIVE_TIMESTAMP_SECONDS_V1`: `T0` is the decision point and bar timestamps are shown only as relative seconds. Original time is retained only in escrow and import rebinds it to the frozen input.
- Hardened `validate_blind_label_record` so an unhashable `target_structure` produces schema errors rather than raising a `TypeError`.
- Added focused tests for blinding, causal bars, disabled network, exact hash binding, sealed-file absence, full two-rater coverage, duplicate IDs, malformed labels, tampering, and exclusive writes.

## Generated local packets

The following latest packet versions are blank and remain under Git-ignored `reports/v38+/`. Neither packet nor its escrow has been sent to a reviewer. The opaque IDs below are audit references only.

| Pseudonymous packet label | Packet ID | Public HTML SHA-256 | Escrow SHA-256 | Items |
| --- | --- | --- | --- | ---: |
| `reviewer_c` | `4571a5412aad4da7bcf3d56921d5d227` | `8de1b9309e4621ee3f6e017ca61d5bf1388c21a527574bdf110e05637b6a1986` | `18595e6c3b6fafb77a499e3c0b19ece2ec978ac8b535e8c252636c539380729e` | 54 |
| `reviewer_d` | `3500532281474b42b8ec395d4df11971` | `62fe3cba1a7a95ffb6ca2b5487d90a71300b62b66d5569cb8d42424465291818` | `2d406f75b6a589d7c8bf32dc715c6ab25779daeb1e2823bc2f2bc66adeefbb2e` | 54 |

CLI summaries reported `labels_generated=0`, `model_calls_used=0`, `orders_created=0`, and `untouched_test_file_read=false`. The last field is emitted by the CLI and is not an independent runtime telemetry counter; the stronger evidence is the reviewed code path and the fixture test with no sealed file.

## Verification

- `python -m pytest -q tests/v38`: **75 passed**.
- Ruff on all changed Python source/test files: **passed**.
- `python -m py_compile` on the two modules and two CLIs: **passed**.
- `git diff --check`: **passed**.
- Decoded both generated HTML packet payloads and found 54 items in each, the expected frozen timestamp policy ID, and **zero absolute ISO timestamp values**.
- Full suite: **2,426 passed, 1 skipped, 1 existing Starlette/httpx deprecation warning** in 341.25 seconds.
- Exact comparison to frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**, 0 new failing node/phase pairs, all 100 inherited baseline failure-phase pairs resolved, and 0 phase changes. The branch contains 103 nodes added since that baseline; 96 were already on the stacked V38–V42 branch and 7 are new in this tooling change.
- The HTML is statically tested for disabled network access and validated through its packet encoder. It has not been independently browser-automated end-to-end.

No Gemini/provider call, exchange request, TestNet/Live session, order, or paid model use occurred. Generated artifacts and test-evidence reports under `reports/` are ignored by Git.

## Limits and next step

- No labels exist yet; the 54 visible contexts have not received human annotations. V38 Gate 2 remains accepted only for data/evaluation preparation.
- Distinct pseudonymous IDs are not proof of independent people. An independent coordinator must assign reviewers, send only each HTML file, keep escrow private, and confirm reviewer separation outside this tool.
- Relative time hides the obvious calendar boundary but cannot prevent a reviewer from recognizing a historical market sequence from its prices. Reviewers must use only the supplied bars; this residual identification risk must be considered when interpreting blinded labels.
- Historical news, funding, account state, order book, observed data-arrival timestamps, model decisions, executable proposals, and completed closes remain unavailable for this annotation set. `bar_end + 60s` remains an assumed availability proxy.
- Label validation preserves disagreements; it does not adjudicate them, infer consensus, or establish trading performance.
- A successful label import will still not authorize Gemini calls, economic replay, production setting changes, or Live execution.
