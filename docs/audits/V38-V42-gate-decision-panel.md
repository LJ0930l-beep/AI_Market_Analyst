# V38–V42 Project Gate Decision Panel

> **This iteration moves the project closer to research and engineering readiness. It does not prove trading profitability or authorize Live trading.**

Evidence snapshot: latest local full-suite and exact baseline evidence is recorded for the V38 data-integrity and V39 single-attempt retry follow-ups in `reports/v38+/verification/` (Git-ignored). The PR chain was checked with PRs #1–#7 open, no auto-merge; GitHub reports no checks for PR #7, so the results below are local evidence, not CI evidence.

| Gate | Current evidence | Decision | Next action |
| --- | --- | --- | --- |
| Baseline and PR dependencies | Verified V37.1 baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`; PRs #1–#7 are OPEN, chained, none merged; auto-merge is disabled. | **READY FOR HUMAN REVIEW** | Review the stacked PR chain in order. |
| Data availability | Original V1 remains preserved but is superseded for independent-row claims: 120 visible rows had 20 global window components and 10 cross-partition overlaps. New companion set: 80 contexts across 40 paired anchors (20/10/10 by partition); 60 visible optimization/validation payloads, 20 sealed hash-only test rows. Audit recomputed 60 input hashes and 11,460 causal bars, verified 50 source archives and a deterministic rebuild; 80 same-symbol components, zero overlap. Price evidence is Binance archive reconstruction; availability remains an assumed `bar_end + 60s` proxy. | **PASS WITH CAVEATS / PROXY** | Keep BTC/ETH paired and temporal anchors out of IID claims; preserve the sealed test; obtain actual Gate availability and execution evidence before any Gate point-in-time or trade claim. |
| Gemini qualitative study | Actual Gemini calls: **0**. Verified scored decisions: **0**. No model-call budget was authorized. | **NOT RUN** | Obtain separate model, spend, sample, prompt/schema, and stop-condition authorization before a controlled run. |
| A0 / B0 comparison | V25 exact A0 recovery remains **0/100** and unrecoverable from available evidence. New B0 baseline: **NOT RUN**. | **NOT COMPARABLE** | If authorized later, preregister B0 as a new baseline; never relabel it as V25 A0. |
| Price-action decision quality | No Gemini-scored or independently blind-labeled sample in this phase. V38 inputs support market-only research but contain no trade outcome labels. | **UNKNOWN / INSUFFICIENT** | Define blind labels and evaluation denominator before model research; keep WAIT and errors visible. |
| Costs and risk | Production fixed notional remains **2,000 USDT**; no saved risk settings changed. V40 focused offline economics/simulation tests: **96 passed**. Gate-executable samples: **0**. Dealer rebate eligibility/settlement is not verified. | **OFFLINE PASS / EXECUTION BLOCKED** | Obtain product/account-specific fee, contract, quote, and settlement evidence before any execution claim. |
| Transport and execution reliability / test governance | V39 follow-up: **128 focused tests passed**. Latest full suite: **2,373 passed, 0 failed, 1 skipped**. Exact baseline comparison: **100/100** baseline failure-phase nodes resolved; all 50 added nodes pass; no missing nodes, new failures, or phase changes. Completion retries fail closed locally. Remote idempotency and duplicate-cost behavior remain unverified. | **LOCAL PASS / REMOTE BLOCKED_WITH_EVIDENCE** | Define a provider-supported idempotency or bounded duplicate-cost policy; do not claim exactly-once behavior. |
| Profitability | V25 has 10 historical close summaries but lacks exact decision/input/identity provenance. New V38 complete closes: **0**. | **INSUFFICIENT SAMPLE** | Do not infer strategy edge. Meet preregistered complete-close and uncertainty requirements in a future authorized study. |
| Shadow / deployment | This phase ran no shadow observation window, TestNet session, Live session, or order. No 30-day/1,000-scan shadow evidence exists. | **NOT AUTHORIZED** | Keep Live disabled; require independent product/account/region review, stop/rollback drills, and separate human authorization. |

## Evidence references

- Master plan and current gate states: `docs/plans/V38-V42-master-implementation-plan.md`.
- Product contract and dependency roadmap: `docs/plans/AI-Market-Analyst-Product-Contract-V1.md` and `docs/plans/Multi-Asset-Trader-Terminal-Roadmap.md`.
- Full-suite and exact comparison: `docs/audits/V41-gate5-legacy-repair-followup.md`; machine evidence remains Git-ignored under `reports/v38+/verification/`.
- V39 retry boundary: `docs/audits/V39-single-attempt-retry-followup.md`.
- V38 data limits and overall acceptance: `docs/audits/V38-V42-unattended-acceptance.md`.
- V38 overlap correction and companion dataset: `docs/audits/V38-nonoverlap-dataset-followup.md`.

Overall V38–V42 status remains **PARTIAL**. V40 is complete for offline simulation/economics evidence and V41 is complete for the local full-suite census. V39 remote retry semantics and V42 controlled research remain gated. No entry in this panel authorizes paid model calls, exchange access, account changes, Live mode, or orders.
