# B-20261013-006 — G0 与 G4 校准远程模式顺序冲突

```yaml
id: B-20261013-006
severity: S1
status: OPEN
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
evidence_files_and_hashes: "Handbook SHA-256 A31A70795C02B9F5BE3ED505361AA3133ADFE82DA09DDD18DE8FE5D482841B4D; docs/blockers/B-20261010-003-calibration-identity-evidence.md; core/trading/ai_calibration.py requires model_weight_digest at lines 205-207; tests/test_ai_calibration_identity_gate.py SHA-256 1B1A89F1F641A2137758E07D25F790BC8CAD2957FEEAC9ED80947726F700AB02; exact-head PR #27 CI run 38309564947"
reproduction: "Handbook §5.1 item 5 requires calibration remote mode in the G0 combination test; handbook G4 defines that mode; B-20261010-003 says implementation is deferred behind G0/G1/G2/G3. Static inspection finds no REMOTE_WEIGHTS_NOT_EXPOSED handling in ai_calibration.py. python -m pytest -q tests/test_ai_calibration_identity_gate.py tests/test_ai_calibration.py passes and proves missing digest returns NOT_READY with zero generate_json calls and no active profile."
root_cause_status: CONFIRMED
safe_fallback: Do not claim G0 accepted. Preserve NOT_READY and zero remote calls until the dependency conflict is formally resolved.
repair_plan: "A human G0 reviewer must record one interpretation without weakening safety: (1) authorize a strictly offline/fake-provider identity-contract slice before G0, with no provider call and no execution authority; or (2) formally amend the G0 combination matrix so the remote calibration-mode test belongs to G4. Until then, do not implement or claim coverage."
negative_tests: "Missing digest stays NOT_READY; wrong model receipt, forged digest, model-name hash, and local artifact SHA cannot satisfy remote identity; research/paper/testnet identity mode cannot grant LIVE; zero provider calls when prerequisites are absent."
unblock_criteria: "Record the approved gate dependency in the audit/plan; keep all fake-provider negative tests passing; demonstrate the approved same-SHA combination coverage; independent review and exact-head CI pass; G0 decision recorded. Any later remote G2 work requires new explicit bounded authorization."
needs_human_decision: true
next_owner_action: "Obtain independent review of PR #26/#27 and a recorded decision on whether the offline-only identity contract may be pulled into G0 or the combination assertion moves to G4. Keep G0 unaccepted and G2 NOT_AUTHORIZED meanwhile."
rollback: "No product-code patch was made for this blocker. If a future offline-only contract slice is rejected, revert only that slice; preserve current fail-closed behavior, V38 ledgers, prior audit snapshots, V35 controls, and main."
```

## Evidence and disposition

The conflict is in the written gate dependency, not a test failure. The G0 combination requirement cannot pass with current code, while implementing its missing positive mode early would cross the G4 boundary. The latest offline regression verifies only safe rejection and must not be reported as remote-mode coverage.

PR #27 remains Draft and unreviewed. No Gemini/Antigravity or exchange request was made, no order was submitted, and no production setting changed. This blocker requires a human gate interpretation; it is not permission to relax a gate.
