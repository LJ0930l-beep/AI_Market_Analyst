# V41 Provider Identity and Full-Suite Follow-up

## Disposition

**Gate 5 remains PARTIAL.** This follow-up strengthens local response-identity verification and records a fresh exact comparison. The repository is not fully green, remote Gemini behavior has not been exercised, and 27 inherited full-suite failures remain. The earlier 53-failure addendum is preserved as a historical checkpoint; the current 100-node register records the latest run.

## Changes

- `core/consult.py`: do not substitute a requested or manifest model ID for a missing completion identity. Missing, disallowed, or changing response identity fails closed before content is forwarded; verified receipts use only the actual completion response identity.
- `core/analysis/institutional_dashboard.py`: use the shared receipt verifier so request-bound identity metadata cannot be presented as a verified model receipt.
- `scripts/v12-live-smoke.py` and `core/news_translation.py`: align helper naming with the active Gemini contract without broadening the trusted provider path.
- Provider, monitoring, consult, dashboard, news, segment, and provenance fixtures now assert the active Gemini receipt/health contract. Frozen historical pytest node names were retained for exact baseline matching. A new consult test covers an absent completion identity.

No production Gate execution, account configuration, fixed notional, risk threshold, or historical research sample was changed. No provider or exchange call was made.

## Verification

- Focused affected suites: **150 passed** (one Starlette/httpx deprecation warning).
- Full suite: **2,321 passed, 27 failed, 1 skipped** across **2,349** collected tests.
- Exact comparison to baseline `64a5c4206a5073e0c44e9d5cc4178705ffa24664`: **`PASS_NO_NEW_FAILURES`**, same environment; **73** of 100 baseline failure nodes resolved, **27** remain, **0** new failed node/phase pairs, **0** missing baseline nodes, **0** phase changes, and all **25** new test nodes passed.
- Local evidence: `reports/v38+/verification/v41-provider-identity-repair-final.json` and `reports/v38+/verification/v41-provider-identity-repair-final-vs-baseline.json` (Git-ignored and not published).
- The current register is `V41-node-remediation-register.json`. It stores outcome/exception/digest for the fresh run; current short traceback reruns were not performed for this follow-up.
- `py_compile`, JSON parsing, and `git diff --check` passed. Ruff reports **66 existing diagnostics** across the changed Python files; per-file diagnostic code/message counts match merge-base `b2bb544daf258461ce653db904de95e185f9449e` with **0 additions**. Ruff is not reported as clean.
- The repository pre-commit hook requires a fully green suite. Because the fresh exact comparison leaves 27 inherited failures, the follow-up commit uses the documented `--no-verify` exception after the comparison; this does not convert the suite into a green result.

## Remaining risks and acceptance boundary

- The 27 remaining failures are inherited relative to the frozen baseline, but remain unresolved and are not assumed harmless. They include strategy/API, price-action context, provider/monitoring, and lifecycle expectations. See the individual entries in the node register.
- Offline fixtures prove local contract behavior only. They do not prove remote provider identity, response consistency, transport retries, exchange capability, or execution behavior.
- The exact baseline comparison is a no-regression result, not a fully green-suite claim. Gate 5 remains `PARTIAL`; V42 remains gated on the remaining failure dispositions, independent V38 evidence review, retry/idempotency policy, and separate explicit model/budget authorization.
- Product Contract V1 and the Multi-Asset Trader Terminal roadmap remain sequenced after active V38–V42 research gates. No UI, multi-asset execution, Live enablement, paid model call, TestNet session, or order is authorized by this follow-up.
