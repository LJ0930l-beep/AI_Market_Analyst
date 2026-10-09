# V38 Blind Label Agreement Metrics Audit — 2026-10-09

## Result

**Implementation status: READY FOR VALIDATED LABEL BATCHES. Real research metrics: NOT AVAILABLE.** The V3 dataset currently has zero human annotations. The implementation was validated with synthetic test fixtures only; it emits no real-dataset metric artifact and supports no claim about Gemini decision quality, strategy quality, or returns.

## Frozen scope

- Dataset: `V38_BINANCE_BTC_ETH_STRATIFIED_PURGED_CONTEXTS_20261009_V1`.
- Dataset manifest SHA-256: `1f9e157bad2df4c32f863c2ae7a779b7d9ed573ad09d37f49ca44590eca4352a`.
- Visible input SHA-256: `4fd074eafb73ecd113c8734c5417c05878b4ee6ad6afbcb75f736b74c173399d`.
- Label protocol: `V38_PA_REFERENCE_BLIND_LABELS_20261009_V2`, SHA-256 `14827250890d37e33fc0b25121ca0414065e9e835c6beef830c95ed2a3fda64c`.
- Metric implementation policy: `V38_BLIND_LABEL_AGREEMENT_METRICS_V1`.
- Eligible sample scope remains 54 visible contexts (36 optimization, 18 validation). The 18 untouched-test rows remain hash-only and are never passed to the summarizer.

## Implementation

- Added `core/replay/pa_decision_quality_v38/blind_label_metrics.py` and `scripts/summarize_v38_blind_annotation_metrics.py`.
- The summarizer accepts only the exact label-batch schema emitted by the V38 importer. It rechecks batch metadata, exact dataset manifest, full per-rater coverage of all visible decision IDs, at least the protocol minimum number of raters, and every label's schema, protocol binding, decision/hash/partition binding, timestamp, and evidence membership.
- For exactly two raters, each of the eight categorical fields reports Cohen's kappa, observed and chance agreement, raw agreement, class prevalence per rater, UNKNOWN count/fraction, and unanimous-context count.
- For more than two raters, the calculator reports nominal Krippendorff alpha when the pooled expected disagreement is nonzero, plus all-rater-pair raw agreement, pair-level values, per-rater class prevalence, UNKNOWN coverage, and unanimous-context rate.
- Coefficients with insufficient observations or single-class marginals are `NOT_ESTIMABLE` with a reason and `value: null`. Raw agreement remains independently reported; a perfect raw rate is never substituted for an undefined coefficient.
- Evidence-reference integrity includes occurrence counts by role and reports zero invalid references only after strict batch validation. Invalid batches are rejected before producing a metric file.
- The output includes a hash of the source label batch, creates no consensus labels, and explicitly marks model decision quality as `NOT_ESTIMATED_NO_MODEL_DECISIONS` and trading performance as `NOT_ESTIMABLE_FROM_MARKET_LABELS`.
- The CLI accepts label batches and writes summaries only under Git-ignored `reports/v38+/`; outputs are exclusive-create and cannot silently overwrite earlier research artifacts.

## Test coverage

- Two-rater Cohen's kappa with known expected agreement, raw agreement, UNKNOWN fraction, and per-rater class prevalence.
- Single-class case returns `NOT_ESTIMABLE` instead of claiming perfect kappa.
- Three-rater nominal Krippendorff alpha, pairwise raw agreement, and unanimity.
- Full 54-context fixture path from packet generation through label import and metric summary, using synthetic market contexts and fixture annotations only.
- Stale manifest, incomplete rater coverage, and invalid label batch rejection.
- No sealed-test file is present in the full import/summary fixture; the summary does not create consensus fields.

## Verification

- `python -m pytest -q tests/v38`: **81 passed**.
- Full suite: **2,432 passed, 1 skipped, 1 existing Starlette/httpx deprecation warning** in 320.58 seconds.
- Exact comparison to frozen V41 baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**, 0 new failing node/phase pairs, all 100 inherited baseline failure-phase pairs resolved, and 0 phase changes. The branch has 109 test nodes added since that baseline.
- Isolated comparison to V38.3 handoff commit `42c425fddfdb220cbb7d407083ad65b1fa28d369`: **`PASS_NO_NEW_FAILURES`**, 6 new test nodes, 0 new failing nodes, and 0 phase changes.
- Ruff, `py_compile`, CLI `--help`, and `git diff --check`: **passed**.
- At initial audit authoring, this change's pre-commit rerun and GitHub CI had not yet run; the post-commit results are recorded below.
- No genuine reviewer annotations were created, transmitted, or summarized. No provider/model call, exchange request, order, Live/TestNet session, or production setting change occurred.

## Post-commit verification addendum — 2026-10-09

The initial metrics commit was `d6ac527edf872d6c24b7bd2b2c1fcf885adaf13d`. The repository pre-commit hook then passed the full suite: **2,432 passed, 1 skipped, 1 existing Starlette/httpx deprecation warning** in 319.04 seconds.

GitHub Actions run [37877417766](https://github.com/LJ0930l-beep/AI_Market_Analyst/actions/runs/37877417766) passed: quick research/risk/replay/transport gates **158 passed**; full Windows suite **2,433 passed, 25 subtests passed, 1 existing deprecation warning** in 1,049.88 seconds. PR #14 is OPEN and CLEAN with auto-merge unset; its base PR #13 is also OPEN/CLEAN with auto-merge unset. The run verifies commit `d6ac527`; no code changes followed it. This post-commit report update is documentation-only and triggers a fresh PR workflow.

## Limits and remaining gates

- Two pseudonymous IDs do not prove two independent humans. An independent coordinator must assign raters and protect that separation. No reviewer was contacted for this work.
- The 54 contexts share the existing `ASSUMED_PROXY` availability grade (`bar_end + 60s`) and omit historical news, funding, account state, and point-in-time order-book data. Metrics can only characterize inter-rater consistency on this supplied evidence.
- A coefficient does not establish correctness, market predictability, executable trades, profitability, or production fitness. Semantic support of model claims requires future model outputs and separate blinded review.
- V38 Gate 2 remains accepted only for data/evaluation preparation until genuine independent annotations, preregistered metrics, and subsequent decision-quality validation exist. No call, replay, or execution is authorized by this tool.
