# V38 Gate 2 Standalone Data Cross-Check

Date: 2026-10-09  
Disposition: **`PASS_WITH_EVIDENCE_LIMITS` for frozen sample schedule, partition membership, visible raw-bar integrity, causality, pairing, and overlap controls**

## Purpose

The earlier Gate 2 refresh reran the repository's committed auditor and explicitly disclosed that limitation. This follow-up adds a separate verifier implemented with the Python standard library and no imports from `core/` or other `scripts/` auditors. It independently checks the frozen primary V3 sample's calendar/seed selection, partition assignment, visible OHLCV records, causal availability, eight-day input windows, and sealed-test boundary.

The verifier reads only the frozen plan and the three existing dataset artifacts under Git-ignored `reports/v38+/dataset-stratified-purged-20261009-v1/`. It does not open the source research database, re-read any untouched-test payload, access the network, or modify source data. It writes a new report with exclusive-create semantics; the original V1/V2/V3 datasets and all prior reports remain unchanged. The sealed-test index is additionally bound row-for-row to the frozen manifest descriptors.

## Independent result

- Frozen plan SHA-256: `094ee37ff932b3f237c4eef77a42dc94cf496928a46afd5a5c30efa35cfa873f`.
- Dataset manifest SHA-256: `1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a`.
- Standalone verifier source SHA-256: `55503fa1ec24847866d3098b736c2687a67c8fde33ac125d5d49eb337425b07c`.
- Local machine report: `reports/v38+/verification/v38-gate2-standalone-crosscheck-20261009-v4.json` (Git-ignored); canonical report SHA-256: `f8507e81f781235642ae3fde928b42b98eb4e663b224bb252e5e59ce7dd54524`. The earlier V3 report is retained unchanged.
- The independent selection calculation reproduced 36 UTC anchors (18 optimization, 9 validation, 9 untouched test), with paired BTCUSDT/ETHUSDT rows at each anchor.
- It checked all 54 visible contexts and 10,314 raw OHLCV records: 2,592 each for 15m, 5m, and 1h; 2,538 for 4h. Bar timestamps, closed status, OHLCV geometry, contiguous intervals, source metadata bindings, and the 60-second availability proxy were checked independently.
- The 18 untouched-test entries remain hashes and coverage metadata only. All visible and sealed rows matched the frozen half-open partition boundaries and eight-day input windows; same-symbol overlap count was 0.
- The data contain 0 annotations, 0 model outputs, and 0 executable samples. No decision-quality or profitability conclusion follows from this audit.

## Verification

- `python scripts/independent_verify_v38_gate2.py --output reports/v38+/verification/v38-gate2-standalone-crosscheck-20261009-v3.json`: **`PASS_WITH_EVIDENCE_LIMITS`**.
- `python -m pytest -q tests/v38`: **89 passed**, including eight standalone-verifier tests.
- Ruff, `py_compile`, and `git diff --check`: passed.
- The verifier tests cover half-open partition boundaries, deterministic selection, malformed and future-available bars, OHLCV geometry, source binding, exact sealed-row descriptor binding, hash-only test rows, and timezone-aware timestamps.

### Full-suite regression check

- `python -m pytest -q -p scripts.v36_test_evidence --v36-report=reports/v38+/verification/v38-gate2-standalone-crosscheck-full-20261009-v2.json`: **2,440 passed, 1 skipped, 0 failed**, with one existing Starlette/httpx deprecation warning.
- Same-environment comparison against frozen baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**. All 100 inherited failure-phase nodes are resolved; all 117 post-baseline test nodes pass; there are no missing baseline nodes, new failure nodes, or phase changes.
- The full-suite evidence and comparison JSON are Git-ignored local artifacts under `reports/v38+/verification/`; they are not added to this PR.

## Limits

- The source Binance archive files and source database bytes were not available to this standalone verifier, so it could not rehash those bytes. It checks their claimed hashes and provenance fields as present in the frozen plan/input documents only.
- It does not independently rebuild the V36-derived feature frames, `market_input_sha256`, or `evidence_ref_count`. Those remain covered by the original auditor, whose implementation is shared with the builder; they are not independently confirmed here.
- Price data remain Binance UM archive reconstruction, not Gate market data. `available_at = bar_end + 60 seconds` remains an assumed proxy, not observed Gate receive-time evidence. There is no point-in-time bid/ask, account state, fee settlement, proposal, fill, or complete close evidence.
- The paired contexts and separated windows are not IID trades. The human blind-label and controlled Gemini evaluation gates remain unfulfilled; untouched-test data remain sealed.

No Gemini/provider call, exchange request, TestNet/Live session, production risk change, or order occurred.
