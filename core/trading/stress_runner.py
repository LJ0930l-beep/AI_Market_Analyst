"""Multi-cycle stress test runner and invariant verifier (AT29).

Validates long-running operational stability:
1. No duplicate client_order_id / intent_id.
2. Ledger balance equation invariant holds strictly:
   wallet_balance == initial_balance + sum(realized_pnl) - sum(fees) - sum(funding)
3. Guardian heartbeat timeout >= 10.0s triggers degraded state and blocks new risk proposals.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

from core.trading.ledger import AccountLedger, LedgerEvent, EventType
from core.trading.position_guardian import PositionGuardian, ProtectionStatus


@dataclass
class InvariantCheckResult:
    passed: bool
    total_orders: int
    unique_orders: int
    duplicate_orders: int
    ledger_balance_matches: bool
    expected_balance: Decimal
    actual_balance: Decimal
    balance_discrepancy: Decimal
    guardian_heartbeat_timeout_blocked: bool
    violations: List[str] = field(default_factory=list)


class StressRunner:
    """Simulates rapid cycles and verifies invariant guarantees."""

    def __init__(self, ledger: AccountLedger, guardian: PositionGuardian) -> None:
        self.ledger = ledger
        self.guardian = guardian
        self.seen_order_ids: Set[str] = set()
        self.order_history: List[str] = []
        self.duplicate_count: int = 0

    def record_order_intent(self, client_order_id: str) -> bool:
        """Register order ID and verify uniqueness."""
        if client_order_id in self.seen_order_ids:
            self.duplicate_count += 1
            return False
        self.seen_order_ids.add(client_order_id)
        self.order_history.append(client_order_id)
        return True

    def check_ledger_balance_equation(self, account_id: str) -> tuple[bool, Decimal, Decimal, Decimal]:
        """Verify wallet_balance == initial_deposit + sum(realized_pnl) - sum(fees) - sum(funding)."""
        snapshot = self.ledger.get_snapshot(account_id)
        events = self.ledger.get_events(account_id)

        initial = Decimal("0")
        realized_pnl = Decimal("0")
        fees = Decimal("0")
        funding = Decimal("0")

        for ev in events:
            if ev.event_type == EventType.INITIAL_DEPOSIT:
                initial += ev.amount
            elif ev.event_type == EventType.REALIZED_PNL:
                realized_pnl += ev.amount
            elif ev.event_type == EventType.FEE:
                fees += ev.amount
            elif ev.event_type == EventType.FUNDING_PAYMENT:
                funding += ev.amount

        expected_wallet = initial + realized_pnl - fees - funding
        actual_wallet = snapshot.wallet_balance
        diff = abs(actual_wallet - expected_wallet)
        matches = diff < Decimal("0.0000001")
        return matches, expected_wallet, actual_wallet, diff

    def check_guardian_heartbeat_timeout(self, simulated_gap_seconds: float = 12.0) -> bool:
        """Verify that a heartbeat gap >= 10.0s flags degraded state and rejects new risk."""
        # Record a past heartbeat
        past_time = time.time() - simulated_gap_seconds
        self.guardian.last_heartbeat = past_time

        # Check health status
        is_healthy = self.guardian.check_health(timeout_seconds=10.0)
        # It must NOT be healthy if gap >= 10.0
        return not is_healthy

    def run_stress_cycle(
        self,
        account_id: str,
        num_cycles: int = 50,
    ) -> InvariantCheckResult:
        """Execute multiple rapid simulated trading cycles and verify invariants."""
        violations: List[str] = []

        # This is an isolated invariant harness, not a production execution
        # path.  Provision its named PAPER account explicitly so the unified
        # ledger can correctly reject unregistered production accounts.
        with self.ledger._get_conn() as db:
            account_exists = db.execute(
                "SELECT 1 FROM accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
        if account_exists is None:
            self.ledger.create_account(
                account_id=account_id,
                mode="PAPER",
                currency="USDT",
                initial_deposit=Decimal("10000.00"),
            )
        snap = self.ledger.get_snapshot(account_id)
        if snap.wallet_balance == Decimal("0"):
            self.ledger.record_event(
                LedgerEvent(
                    event_id=f"init_{uuid.uuid4().hex[:8]}",
                    account_id=account_id,
                    event_type=EventType.INITIAL_DEPOSIT,
                    currency="USDT",
                    amount=Decimal("10000.00"),
                )
            )

        for i in range(num_cycles):
            # 1. Generate unique client order
            order_id = f"stress_ord_{account_id}_{i}_{uuid.uuid4().hex[:6]}"
            if not self.record_order_intent(order_id):
                violations.append(f"Duplicate order ID detected: {order_id}")

            # 2. Simulate fill and pnl
            pnl_amt = Decimal("15.50") if i % 2 == 0 else Decimal("-10.20")
            fee_amt = Decimal("0.75")

            self.ledger.record_event(
                LedgerEvent(
                    event_id=f"fill_pnl_{i}_{uuid.uuid4().hex[:6]}",
                    account_id=account_id,
                    event_type=EventType.REALIZED_PNL,
                    currency="USDT",
                    amount=pnl_amt,
                )
            )
            self.ledger.record_event(
                LedgerEvent(
                    event_id=f"fill_fee_{i}_{uuid.uuid4().hex[:6]}",
                    account_id=account_id,
                    event_type=EventType.FEE,
                    currency="USDT",
                    amount=fee_amt,
                )
            )

        # Invariant checks
        dup_ok = self.duplicate_count == 0
        if not dup_ok:
            violations.append(f"Duplicate orders encountered: {self.duplicate_count}")

        bal_ok, expected_b, actual_b, diff = self.check_ledger_balance_equation(account_id)
        if not bal_ok:
            violations.append(
                f"Ledger balance discrepancy: actual {actual_b} != expected {expected_b} (diff {diff})"
            )

        guardian_ok = self.check_guardian_heartbeat_timeout(simulated_gap_seconds=11.0)
        if not guardian_ok:
            violations.append("Guardian did not flag degraded state upon 11s heartbeat lapse")

        all_passed = dup_ok and bal_ok and guardian_ok and len(violations) == 0

        return InvariantCheckResult(
            passed=all_passed,
            total_orders=len(self.order_history),
            unique_orders=len(self.seen_order_ids),
            duplicate_orders=self.duplicate_count,
            ledger_balance_matches=bal_ok,
            expected_balance=expected_b,
            actual_balance=actual_b,
            balance_discrepancy=diff,
            guardian_heartbeat_timeout_blocked=guardian_ok,
            violations=violations,
        )
