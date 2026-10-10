# V41 Legacy Test-Failure Census

## Baseline and classification

The exact pre-V38 anchor was rerun from V37.1 commit `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: 2,324 collected, 2,223 passed, 100 failed, 1 skipped. All 100 failures occur in the `call` phase. An older saved V37.1 comparison report omitted one A3 test that exists at the commit; it was not used as the exact final anchor.

The exact 100 node IDs were matched against the previously reviewed postrepair triage by node ID. Classification:

| Classification | Count | Meaning |
| --- | ---: | --- |
| `RETIRED_OLD_MODEL_OR_CONTRACT` | 90 | Existing assertions expect a retired model/strategy/API contract and need migration or explicit disposition. |
| `ENVIRONMENT_DEPENDENCY` | 10 | Existing failures depend on blocked or unavailable runtime/provider/environment conditions. |
| Unclassified | 0 | Every node has a prior reviewed category. |

The source review also reported zero confirmed product regressions after the V34 postrepair changes. That historical review is not a substitute for checking any new failure or re-reviewing a changed production path. Risk, gateway, and execution tests remain in the suite and were not removed or weakened.

## Local evidence

The full node-by-node census, including test file, phase, category, and sanitized first-failure summary, is in ignored local `reports/v38+/gate0/failure-census.json`. Its source hashes and category totals are in `reports/v38+/gate0/pytest-baseline.json`. Raw traceback output is not copied into Git.

## Required post-change check

The final post-V38 full suite collected 2,347 tests: 2,246 passed, 100 failed, and 1 skipped. `scripts/compare_v37_test_evidence.py` compared the exact 64a5c42 baseline against the final V38 evidence and returned `PASS_NO_NEW_FAILURES`: 0 missing baseline tests, 0 new failed node/phase pairs, 0 phase changes, and all 23 new V38 tests passed. The local comparison is `reports/v38+/verification/full-suite-failure-comparison-final-v38.json`; baseline and current test evidence are in the same ignored verification directory.

**V41 status: DONE.** The 100 inherited failures remain a known test/release limitation; no test was removed or weakened, and the current V38 work introduces no new full-suite failure.
