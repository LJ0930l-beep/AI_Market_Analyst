# V41 Gate 5 Node-Level Audit Addendum

## Disposition

**Gate 5: PARTIAL.** The exact full-suite comparison has no new failed node/phase pair, but the repository is not fully green and 53 inherited nodes still fail. The earlier `DONE` statement in the immutable V38-era census described the no-regression comparison only; this addendum supersedes it for Gate 5 completion without rewriting that historical report.

Every one of the 100 baseline failure nodes is listed in `V41-node-remediation-register.json`, including current status, contract relevance, priority, bounded root-cause assessment, confidence, remediation, and owner state.

## Reproducible evidence

- Baseline: commit `64a5c4206a5073e0c44e9d5cc4178705ffa24664`, 2,324 collected, 2,223 passed, 100 failed, 1 skipped.
- Current V41 tree: 2,347 collected, 2,293 passed, 53 failed, 1 skipped.
- Exact comparison: `PASS_NO_NEW_FAILURES`; same environment; 0 missing baseline nodes, 0 new failure nodes, 0 phase changes, 47 resolved baseline failure nodes, and all 23 new V38 tests passed.
- The focused V13 lease-loss and V2 authorization/revocation nodes now pass. Current failure trace rerun is local under ignored `reports/v38+/verification/v41-final-product-contract-followup-failure-traces.txt`; it is not published.
- Local evidence: `reports/v38+/verification/v41-final-product-contract-followup.json` and `reports/v38+/verification/v41-final-product-contract-followup-vs-baseline.json`. These reports are Git-ignored.
- The repository pre-commit hook requires the full suite to be green and therefore rejects commits while these 53 inherited failures remain. The documented `--no-verify` exception may be used only with the fresh exact comparison and explicit disclosure; it does not convert the comparison into a green-suite claim.

## Changes in this V41 repair

- `core/trading/ai_session_coordinator.py`: prompt compaction now retains the `symbols` list on news rows that lack a singular `symbol`, preserving the news-to-instrument mapping used by the focused entry-repair path. This prevents the repair request from losing its only visible asset association and then failing evidence re-citation.
- `tests/test_ai_strategy_validation.py` and `tests/test_autonomous_news_strategy.py`: local fake-provider artifacts and strategy-version fixtures were aligned to the configured Gemini and active PA contracts. Assertions for unavailable or invalid receipts remain fail-closed.
- `tests/test_ai_template_runner.py`: the PA model-exit scenario now asserts that an incomplete local fixture receipt is blocked with `MODEL_EXIT_RECEIPT_UNVERIFIED`; it does not fabricate transport proof to obtain a simulated close. Other template lifecycle assertions still require simulated closes.
- `tests/test_trader_reliability_v13.py`: the lease-loss fixture now supplies the active 5m/15m/1h/4h closed-bar frames and current Gemini identity metadata; it reaches an in-flight provider before asserting cancellation and pause.
- `tests/test_v2_execution.py`: the local provider stub now follows the pinned Gemini receipt contract and tests both simulated approval and authorization revocation before execution. The production adapter and endpoint allowlist are unchanged.
- Focused affected suite: **51 passed** across V13, V2 execution, and V38 tests. The fresh full-suite run is 2,293 passed, 53 failed, and 1 skipped; the exact baseline comparison is reported above.
- Static checks: `py_compile` passed for all changed Python files. Ruff diagnostic code/message counts are unchanged from the branch base for those files. The absolute Ruff check reports 114 findings and `ruff format --check` reports four files needing formatting; this change does not claim a clean lint/format run.

## Remaining high-priority gaps

- The V13 lease-loss and V2 simulation permission/revocation nodes now pass with deterministic local fixtures; this verifies those test scenarios without model or exchange calls.
- Model identity, manifest receipt, monitoring, and local provider endpoint tests include inherited failures. The current allowlist and identity checks fail closed, but a blocked legacy test does not prove the intended active Gemini path works.
- Price-action prompt/context tests, strategy API/profile tests, and news/monitoring lifecycle tests still need a node-specific decision: migrate to the current contract, repair an actual regression, or formally retire the test.

The register uses current short traceback evidence and the frozen baseline's first-failure summaries. Low-confidence entries explicitly do not claim a confirmed causal root cause; inherited status alone is not treated as harmless.

## Acceptance boundary and next gate

Gate 5 is not accepted as complete while 53 failure nodes remain. `PASS_NO_NEW_FAILURES` demonstrates that this V41 change adds no failures relative to the frozen anchor; it does not validate the failed behaviors. V42 remains gated until the remaining safety-relevant nodes are repaired or formally dispositioned with focused evidence, the V38 causal/data boundaries receive independent review, and a separate model/budget authorization is provided. No paid model call, private exchange request, TestNet/Live session, or order was initiated for this audit.
