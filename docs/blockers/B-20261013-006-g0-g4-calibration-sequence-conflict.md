# B-20261013-006 — G0 与 G4 校准远程模式顺序冲突

```yaml
id: B-20261013-006
severity: S1
status: RESOLVED
component: integration
mode: RESEARCH_OFFLINE
branch_sha: 952d38a8e4d6aa800c507f6c54ee9477d744181b
trigger_signature: G0_G4_CALIBRATION_REMOTE_MODE_SEQUENCE_CONFLICT
first_seen_at_utc: 2026-10-12T21:46:04Z
manual_playbook: G0 integration; mapped calibration item B05 / G4
user_authorization: DOCUMENTED_OFFLINE_AUDIT_ONLY
external_cost_or_order_attempted: false
remote_effect_known: false
risk_to_funds: NONE
affected_sample_or_order_ids: PR-26-PR-27 integration audit only; no samples or orders
immediate_stop_or_isolation: Keep AICalibrationService fail-closed when the weight digest is unavailable; keep remote dispatch disabled; do not call model/exchange APIs, submit orders, merge PRs, or implement G4 early.
evidence_files_and_hashes: "Handbook SHA-256 A31A70795C02B9F5BE3ED505361AA3133ADFE82DA09DDD18DE8FE5D482841B4D; docs/blockers/B-20261010-003-calibration-identity-evidence.md; core/trading/ai_calibration.py requires model_weight_digest at lines 205-207; tests/test_ai_calibration_identity_gate.py SHA-256 1B1A89F1F641A2137758E07D25F790BC8CAD2957FEEAC9ED80947726F700AB02; decision audit docs/audits/V2-G0-G1-gate-adjudication-20261012T223231Z.md; exact-head PR #27 CI run 38314459575"
reproduction: "Handbook §5.1 item 5 requires calibration remote mode in the G0 combination test; handbook G4 defines that mode; B-20261010-003 says implementation is deferred behind G0/G1/G2/G3. Static inspection finds no REMOTE_WEIGHTS_NOT_EXPOSED handling in ai_calibration.py. python -m pytest -q tests/test_ai_calibration_identity_gate.py tests/test_ai_calibration.py passes and proves missing digest returns NOT_READY with zero generate_json calls and no active profile."
root_cause_status: CONFIRMED
safe_fallback: Preserve fail-closed NOT_READY and zero remote calls whenever verified identity/weights evidence is unavailable; keep G0 unaccepted until all remaining acceptance criteria and independent review are complete.
repair_plan: "Adjudicated per the user's instruction to decide directly: select option (2) and amend the G0 matrix so only fail-closed calibration behavior is required at G0; the positive remote calibration identity mode and its combination assertion belong to G4 after G0-G3. Do not implement G4 early or claim positive-mode coverage at G0."
negative_tests: "Missing digest stays NOT_READY; wrong model receipt, forged digest, model-name hash, and local artifact SHA cannot satisfy remote identity; research/paper/testnet identity mode cannot grant LIVE; zero provider calls when prerequisites are absent."
unblock_criteria: "The sequencing conflict is resolved by the recorded gate adjudication. This does not accept G0: keep G0 pending independent PR review and the remaining gate criteria; keep G2 NOT_AUTHORIZED. G4 remote identity mode remains gated on upstream G0-G3 acceptance and its own authorization."
needs_human_decision: false
next_owner_action: "Treat B-006 as resolved. Maintain G0 unaccepted pending independent review of PR #26/#27 and all other criteria; keep G2 unauthorized and B-003 open for the later G4 calibration contract."
rollback: "No product-code patch was made for this blocker. If independent review later changes this adjudication, revert only the additive decision notes and status in B-006, the blocker index, and the plan; preserve fail-closed behavior, V38 ledgers, prior audit snapshots, V35 controls, and main."
```

## Evidence and disposition

The conflict was in the written gate dependency, not a test failure. The offline regression verifies only safe rejection and must not be reported as remote-mode coverage. At discovery, the dependency required an explicit gate interpretation; this report records the delegated decision and does not authorize safety relaxation.

## Decision and disposition — 2026-10-12T22:32:31Z

The user delegated the sequencing choice with “你直接决定”. The chosen interpretation is option (2): the G0 combination matrix requires the calibration fail-closed guard (missing verified identity or weight digest yields NOT_READY, makes zero provider calls, and creates no active profile). It does not require the positive REMOTE_WEIGHTS_NOT_EXPOSED calibration mode. That positive mode and its combination test belong to G4, after G0-G3.

This resolves only the circular dependency. It does not relax risk controls, implement G4 early, accept G0, authorize G2, or permit provider/exchange calls. The exact PR #27 head 34f09e717b6303019c873ef274781fcadec079c9 passed hosted run 38314459575 (quick 289 passed; full 2,605 passed, 25 subtests; Web tests, lint, type-check, and build passed). PR #26/#27 remain Draft and unreviewed. B-20261010-003 remains OPEN for the unimplemented G4 contract.

PR #27 remains Draft and unreviewed. No Gemini/Antigravity or exchange request was made, no order was submitted, and no production setting changed.
