# V38 Gate 0 Readiness Audit

## Scope and base

The V38 worktree is `codex/v38-research-readiness`, based on V37.1 commit `64a5c4206a5073e0c44e9d5cc4178705ffa24664`. Gate 0 was read-only: confirm dependency state, preserve prior evidence, and freeze a test baseline before adding V38 code.

## Dependency chain

At the readiness check, GitHub PRs #1 through #5 were all `OPEN`, and each had `autoMergeRequest = null`:

| PR | Head | Base | State |
| --- | --- | --- | --- |
| #1 | `codex/v35-feasibility` | `main` | OPEN |
| #2 | `codex/v36-pa-decision-quality` | `codex/v35-feasibility` | OPEN |
| #3 | `codex/v36.1-research-integrity` | `codex/v36-pa-decision-quality` | OPEN |
| #4 | `codex/v37-historical-evidence` | `codex/v36.1-research-integrity` | OPEN |
| #5 | `codex/v37.1-research-fixes` | `codex/v37-historical-evidence` | OPEN |

The new PR will target `codex/v37.1-research-fixes`, preserving the chain. No automatic merge is configured.

## Preserved artifacts and safety

- V25 A0 exact decision recovery remains `0/100`; no reconstructed rows were relabeled as recovered V25 decisions.
- Existing V37.1 reports were not rewritten. The prior A3 report remains an older-generation artifact; the corrected A3 policy is separately versioned by V37.1.
- The already verified public Binance archive contains 50 archive files. Its local database SHA-256 is `c04d69fa13b68efd5e9e64e02442524961589a2d805354017f773f46169ff4d2`. No new market-data download was performed.
- Model calls, private exchange calls, and orders: `0`. Production settings and the V35/Gate production path were not edited.
- V38+ dataset and audit JSON files are under ignored `reports/` and are excluded from the PR.

## Frozen test baseline

The old V37.1 saved report contained 2,323 collected tests and omitted one A3 test present at the V37.1 commit. To avoid treating that stale report as exact, the full suite was rerun on a detached checkout of `64a5c4206a5073e0c44e9d5cc4178705ffa24664` in the same environment used for V38. The exact baseline has 2,324 collected tests: 2,223 passed, 100 failed, and 1 skipped. All 100 failure nodes match the previously reviewed V34 postrepair census by exact node ID: 90 are classified as retired model/contract expectations and 10 as environment dependencies. No raw traceback or account data is included in the V38 local census.

Machine-readable evidence is in local ignored files:

- `reports/v38+/gate0/pytest-baseline.json`
- `reports/v38+/gate0/failure-census.json`
- `reports/v38+/gate0/readiness-manifest.json`

After V38 changes, the suite collected 2,347 tests: 2,246 passed, 100 failed, and 1 skipped. Exact node/phase comparison reports `PASS_NO_NEW_FAILURES`, 23 added V38 tests all passed, no baseline tests missing, no phase changes, and no new failures. The 100 inherited failures remain visible; the repository is not fully green.

## Result

**Gate 0: DONE, WITH REMAINDERS.** The exact baseline, dependency chain, and preserved-artifact boundary are known. The 100 inherited failures remain visible technical debt, and no live-provider or exchange-execution readiness is claimed.
