"""Acceptance tests for AT24 through AT30 (Milestone M3).

AT24: Partial TP & performance stats (aggregate into 1 trade, full cost deduction, EVIDENCE_INSUFFICIENT gate)
AT25: Identical history AI counterfactual experiment (3-branch evaluation, opportunity cost, 2x fee/slippage stress)
AT26: Data migration interrupted & re-run (idempotency, LEGACY_UNVERIFIED, RECONCILIATION_REQUIRED)
AT27: Backend crash and market stream disconnect (degradation tracking, zero secret leakage in diagnostic bundle)
AT28: Clean Windows install / sidecar lifecycle (strict PID isolation, never kill system python or unrelated processes)
AT29: 72h running stress invariants (no duplicate orders, exact ledger balance equation, Guardian 10s timeout)
AT30: Version & delivery evidence check (manifest.json, SHA256 hashes, full acceptance matrix)
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest

from core.analysis.strategy_evaluator import (
    evaluate_strategy_effectiveness,
    run_counterfactual_comparison,
)
from core.data_migration import (
    DataMigrator,
    LegacyAccountStatus,
    LegacyOrderStatus,
)
from core.diagnostics import (
    DegradationTracker,
    collect_system_diagnostics,
    export_diagnostic_bundle,
    redact_secrets,
    scan_for_leaks,
)
from core.manifest import (
    SPEC_RELEASE_CODE,
    SPEC_VERSION,
    compute_file_sha256,
    generate_evidence_manifest,
)
from core.sidecar_lifecycle import SidecarProcessManager, SidecarState
from core.trading.ledger import AccountLedger, EventType, LedgerEvent
from core.trading.position_guardian import PositionGuardian
from core.trading.stress_runner import StressRunner


# ============================================================================
# AT24: Partial TP & Performance Statistics
# ============================================================================
def test_at24_partial_tp_aggregated_into_single_closed_trade():
    """AT24: Multiple partial TP fills for 1 position must count as 1 closed trade,

    and deduct full round-trip fees before calculating win/loss and win rate.
    """
    # Create 3 positions:
    # Trade 1: entry at 100, exit 50% at 110 (TP1), 50% at 120 (TP2). Gross pnl = +15, fees = 2.0 -> Net = +13 (WIN)
    # Trade 2: entry at 100, exit 100% at 90 (SL). Gross pnl = -10, fees = 1.0 -> Net = -11 (LOSS)
    # Total closed trades = 2 (NOT 3, despite 2 exit fills on trade 1)
    trades = [
        # Trade 1: Entry
        {
            "order_id": "ord_entry_1",
            "position_id": "pos_001",
            "symbol": "BTC_USDT",
            "side": "BUY",
            "executed_price": 100.0,
            "filled_quantity": 1.0,
            "fee": 0.5,
            "is_exit": False,
            "status": "FILLED",
        },
        # Trade 1: TP 1 (partial exit)
        {
            "order_id": "ord_tp1_1",
            "position_id": "pos_001",
            "symbol": "BTC_USDT",
            "side": "SELL",
            "executed_price": 110.0,
            "filled_quantity": 0.5,
            "fee": 0.5,
            "is_exit": True,
            "status": "FILLED",
        },
        # Trade 1: TP 2 (final exit)
        {
            "order_id": "ord_tp2_1",
            "position_id": "pos_001",
            "symbol": "BTC_USDT",
            "side": "SELL",
            "executed_price": 120.0,
            "filled_quantity": 0.5,
            "fee": 0.5,
            "is_exit": True,
            "status": "FILLED",
        },
        # Trade 2: Entry
        {
            "order_id": "ord_entry_2",
            "position_id": "pos_002",
            "symbol": "BTC_USDT",
            "side": "BUY",
            "executed_price": 100.0,
            "filled_quantity": 1.0,
            "fee": 0.5,
            "is_exit": False,
            "status": "FILLED",
        },
        # Trade 2: SL exit
        {
            "order_id": "ord_sl_2",
            "position_id": "pos_002",
            "symbol": "BTC_USDT",
            "side": "SELL",
            "executed_price": 90.0,
            "filled_quantity": 1.0,
            "fee": 0.5,
            "is_exit": True,
            "status": "FILLED",
        },
    ]

    eval_result = evaluate_strategy_effectiveness(
        strategy_id="s1_trend_pullback",
        trades=trades,
    )

    assert eval_result["total_closed_trades"] == 2
    assert eval_result["winning_trades"] == 1
    assert eval_result["losing_trades"] == 1
    assert eval_result["win_rate"] == 0.5
    assert eval_result["total_fees"] == 2.5
    # Less than 100 samples must trigger EVIDENCE_INSUFFICIENT gate
    assert eval_result["evidence_status"] == "EVIDENCE_INSUFFICIENT"
    assert "cvar_95" in eval_result
    assert "mae_average" in eval_result
    assert "mfe_average" in eval_result


# ============================================================================
# AT25: Identical History AI Counterfactual Comparison
# ============================================================================
def test_at25_counterfactual_comparison_with_opportunity_cost():
    """AT25: 3-branch comparison (STRATEGY_BASELINE vs AI_FILTERED vs AI_LED),

    evaluating opportunity cost of rejected winners, tail loss reduction, and 2x stress.
    """
    # 3 baseline trades:
    # t1: pnl +50 (AI APPROVED)
    # t2: pnl +100 (AI REJECTED -> Opportunity cost = +100)
    # t3: pnl -200 (AI REJECTED -> Tail loss prevented = -200)
    baseline_trades = [
        {"trade_id": "t1", "pnl": 50.0, "mae": -5.0, "mfe": 60.0},
        {"trade_id": "t2", "pnl": 100.0, "mae": -10.0, "mfe": 110.0},
        {"trade_id": "t3", "pnl": -200.0, "mae": -200.0, "mfe": 10.0},
    ]
    ai_reviews = [
        {"trade_id": "t1", "verdict": "REVIEW_PASS"},
        {"trade_id": "t2", "verdict": "REJECT"},
        {"trade_id": "t3", "verdict": "REJECT"},
    ]
    ai_led_trades = [
        {"trade_id": "ai_led_1", "pnl": 75.0, "mae": -8.0, "mfe": 85.0},
    ]

    comp = run_counterfactual_comparison(
        baseline_trades=baseline_trades,
        ai_reviews=ai_reviews,
        ai_led_trades=ai_led_trades,
    )

    branches = comp["branches"]
    assert "STRATEGY_BASELINE" in branches
    assert "AI_FILTERED" in branches
    assert "AI_LED" in branches

    # Baseline PnL: 50 + 100 - 200 = -50
    assert branches["STRATEGY_BASELINE"]["net_pnl"] == -50.0
    # AI_FILTERED PnL: only t1 = +50
    assert branches["AI_FILTERED"]["net_pnl"] == 50.0
    # Opportunity cost of rejected winning signal (t2) = 100
    assert comp["opportunity_cost_rejected_winners"] == 100.0
    # Tail loss avoided by rejecting t3 = 200
    assert comp["tail_loss_prevented"] == 200.0

    # Stress testing outputs present
    stress = comp["stress_tests"]
    assert "baseline" in stress
    assert "double_fees" in stress
    assert "double_slippage" in stress


# ============================================================================
# AT26: Data Migration Interruption and Idempotency
# ============================================================================
def test_at26_data_migration_idempotency_and_unverified_flagging():
    """AT26: Migration flags ambiguous accounts as LEGACY_UNVERIFIED, unfilled

    orders as RECONCILIATION_REQUIRED, and is strictly idempotent on re-run.
    """
    tmp_db = Path(tempfile.gettempdir()) / f"test_at26_{time.time_ns()}.db"
    try:
        # 1. Create legacy schema and ambiguous state
        conn = sqlite3.connect(tmp_db)
        try:
            conn.execute(
                "CREATE TABLE accounts (account_id TEXT PRIMARY KEY, balance REAL, initial_deposit REAL)"
            )
            conn.execute(
                "CREATE TABLE orders (order_id TEXT PRIMARY KEY, account_id TEXT, symbol TEXT, filled_quantity REAL, status TEXT)"
            )
            # Ambiguous account: initial_deposit is 0 / null
            conn.execute(
                "INSERT INTO accounts VALUES ('acc_ambiguous', 5000.0, 0.0)"
            )
            # Verified account: initial_deposit is explicit 10000.0
            conn.execute(
                "INSERT INTO accounts VALUES ('acc_clean', 10000.0, 10000.0)"
            )
            # Order with missing fills
            conn.execute(
                "INSERT INTO orders VALUES ('ord_missing_fill', 'acc_clean', 'BTC_USDT', 0.0, 'OPEN')"
            )
            conn.commit()
        finally:
            conn.close()

        migrator = DataMigrator(tmp_db)

        # First migration run
        result_1 = migrator.migrate()
        assert result_1.success
        assert result_1.accounts_migrated == 2

        # Verify classifications in DB
        conn = sqlite3.connect(tmp_db)
        try:
            row_ambiguous = conn.execute(
                "SELECT status FROM accounts WHERE account_id='acc_ambiguous'"
            ).fetchone()[0]
            assert row_ambiguous == LegacyAccountStatus.LEGACY_UNVERIFIED.value

            row_clean = conn.execute(
                "SELECT status FROM accounts WHERE account_id='acc_clean'"
            ).fetchone()[0]
            assert row_clean == LegacyAccountStatus.VERIFIED.value

            row_order = conn.execute(
                "SELECT reconciliation_status FROM orders WHERE order_id='ord_missing_fill'"
            ).fetchone()[0]
            assert row_order == LegacyOrderStatus.RECONCILIATION_REQUIRED.value
        finally:
            conn.close()

        # Second migration run: MUST BE IDEMPOTENT (no duplicates or changes)
        result_2 = migrator.migrate()
        assert result_2.success

        conn = sqlite3.connect(tmp_db)
        try:
            total_accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            assert total_accounts == 2
        finally:
            conn.close()

    finally:
        import gc
        gc.collect()
        if tmp_db.exists():
            try:
                tmp_db.unlink()
            except PermissionError:
                pass


# ============================================================================
# AT27: Backend Crash, Disconnect & Redacted Diagnostic Bundle
# ============================================================================
def test_at27_diagnostic_bundle_zero_secret_leakage():
    """AT27: Status degradation tracking and redacted bundle export with ZERO secret leakage."""
    tracker = DegradationTracker()
    tracker.record_transition(
        component="market_feed",
        from_status="AVAILABLE",
        to_status="DEGRADED",
        reason_code="STREAM_DISCONNECTED",
        detail="Websocket heartbeats dropped for 15s",
    )
    events = tracker.get_recent_events()
    assert len(events) >= 1
    assert events[-1]["reason_code"] == "STREAM_DISCONNECTED"

    # Context containing sensitive mock credentials
    secret_key = "secret_mock_key_9876543210_never_leak"
    api_token = "bearer_super_secret_token_123456789"
    sensitive_context = {
        "api_key": secret_key,
        "api_secret": "my_super_secret_passphrase_000",
        "nested": {
            "token": api_token,
            "safe_metric": 42.5,
        },
        "log_message": f"Connecting with key={secret_key} and token={api_token}",
    }

    # Redact test
    redacted = redact_secrets(sensitive_context)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["api_secret"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["nested"]["safe_metric"] == 42.5

    # Export bundle and verify archive contents
    tmp_dir = tempfile.mkdtemp()
    try:
        archive_path = export_diagnostic_bundle(
            output_dir=tmp_dir,
            custom_context=sensitive_context,
        )
        assert archive_path.exists()
        assert zipfile.is_zipfile(archive_path)

        with zipfile.ZipFile(archive_path, "r") as zf:
            namelist = zf.namelist()
            assert "diagnostics.json" in namelist
            assert "manifest.json" in namelist
            diag_content = zf.read("diagnostics.json").decode("utf-8")
            bundle_manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            assert bundle_manifest["secret_scan_status"] == "COMPLETED_NO_MATCHES"
            assert bundle_manifest["secret_scan_scope"]

            # Must guarantee ZERO leak of the secret values
            leaks = scan_for_leaks(
                diag_content,
                [secret_key, api_token, "my_super_secret_passphrase_000"],
            )
            assert leaks == [], f"Secret leaks detected in bundle: {leaks}"

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================================
# AT28: Clean Windows Install & Process Isolation (Self-PID only)
# ============================================================================
def test_at28_sidecar_process_manager_strict_pid_isolation():
    """AT28: Sidecar manager tracks exact child PID and only terminates its own child."""
    manager = SidecarProcessManager(name="test_worker")
    assert manager.state == SidecarState.STOPPED

    # Spawn a non-destructive python sleep child process
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    child_pid = manager.spawn(cmd)
    assert child_pid > 0
    assert manager.child_pid == child_pid
    assert manager.is_running()

    # Terminate strictly own PID
    success = manager.terminate_own_pid_only(timeout_seconds=2.0)
    assert success
    assert not manager.is_running()
    assert manager.child_pid is None


# ============================================================================
# AT29: 72h Running Stress Invariants
# ============================================================================
def test_at29_stress_runner_invariants_hold():
    """AT29: Multi-cycle stress validates no duplicate orders, ledger balance equation,

    and Guardian 10s timeout fail-safe.
    """
    tmp_db = Path(tempfile.gettempdir()) / f"stress_{time.time_ns()}.db"
    ledger = None
    try:
        ledger = AccountLedger(db_path=tmp_db)
        guardian = PositionGuardian(ledger=ledger)
        runner = StressRunner(ledger=ledger, guardian=guardian)

        result = runner.run_stress_cycle(account_id="stress_acc_01", num_cycles=20)
        assert result.passed, f"Stress invariant checks failed: {result.violations}"
        assert result.unique_orders == result.total_orders
        assert result.duplicate_orders == 0
        assert result.ledger_balance_matches
        assert result.balance_discrepancy < Decimal("0.00001")
        assert result.guardian_heartbeat_timeout_blocked

    finally:
        if ledger is not None:
            ledger.close()
        import gc
        gc.collect()
        if tmp_db.exists():
            try:
                tmp_db.unlink()
            except PermissionError:
                pass


# ============================================================================
# AT30: Version and Delivery Evidence Check
# ============================================================================
def test_at30_manifest_evidence_completeness():
    """AT30: Manifest generator conforms to V2R1 specification with SHA-256 and AT01-AT42 matrix."""
    tmp_dir = tempfile.mkdtemp()
    try:
        manifest_rel = "evidence/manifest.json"
        manifest = generate_evidence_manifest(
            workspace_root=".",
            output_relative_path=manifest_rel,
        )

        assert manifest["release_code"] == SPEC_RELEASE_CODE
        assert manifest["spec_version"] == SPEC_VERSION
        assert "file_sha256" in manifest
        assert len(manifest["file_sha256"]) > 5

        # Check full AT01-AT42 matrix
        matrix = manifest["acceptance_matrix"]
        for i in range(1, 43):
            code = f"AT{i:02d}"
            assert code in matrix, f"Missing acceptance code {code} in manifest"
            assert matrix[code]["status"] in (
                "PASS",
                "FAIL",
                "NOT_RUN",
                "UNSUPPORTED",
                "WAITING_USER_AUTHORIZATION",
            )

        assert Path(manifest_rel).exists()

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
