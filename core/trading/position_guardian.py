"""Independent Background PositionGuardian (R05).

Decoupled from strategy discovery loop:
- Operates in its own dedicated background thread.
- Does NOT terminate when strategy monitoring is paused, subscriptions are removed, or LLM crashes.
- Evaluates ProtectionPlans for all active OPEN positions.
- Implements conservative stop-first execution and slippage modeling.
- Records realized PnL and fees directly to AccountLedger.
- Emits 2-second heartbeat; marks status DEGRADED if heartbeat exceeds 10s.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from contextlib import contextmanager
import json
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

from .ledger import AccountLedger, LedgerEventType
from .account_scope import resolve_account_scope
from core.news_revision import NewsRevisionRegistry
from .execution_gateway import (
    ExecutionGateway,
    GatewayError,
    OrderIntent,
    ProtectionPlan,
    ProtectionStatus,
    TradingMode,
)


def _number_or_none(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") and parsed > 0 else None


def _safe_json_dict(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if not isinstance(value, dict) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class PositionGuardian:
    """Independent background watchdog protecting open positions and executing exit plans."""

    def __init__(
        self,
        store: Any = None,
        ledger: Optional[AccountLedger] = None,
        *,
        heartbeat_interval: float = 2.0,
        degraded_timeout: float = 10.0,
        execution_gateway: Optional[ExecutionGateway] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        if ledger is None and isinstance(store, AccountLedger):
            ledger = store
            store = getattr(ledger, "_store", None)
        elif store is None and ledger is not None:
            store = getattr(ledger, "_store", None)

        self.store = store
        self.ledger = ledger
        self.heartbeat_interval = heartbeat_interval
        self.degraded_timeout = degraded_timeout
        self.execution_gateway = execution_gateway
        # The default is the real wall clock.  Replay callers may inject a
        # virtual clock explicitly; a bar's fixture timestamps never replace
        # this clock implicitly.
        self.clock = clock or (lambda: datetime.now(timezone.utc))

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._last_heartbeat: Optional[datetime] = None
        self._is_degraded: bool = False
        self._degraded_reason: Optional[str] = None
        self._price_feed: Dict[str, Dict[str, Any]] = {}
        self._active: bool = False
        # A production runtime binds one guardian to one registered account.
        # ``None`` is retained for the standalone compatibility harnesses;
        # those rows are still required to carry an explicit account_id.
        self._account_id: str | None = None

    def _now(self) -> datetime:
        """Return the Guardian clock as an aware UTC instant."""
        value = self.clock()
        if not isinstance(value, datetime):
            return datetime.now(timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def set_account_scope(self, account_id: str | None) -> None:
        """Bind this guardian to one account for runtime-owned protection."""
        with self._lock:
            self._account_id = str(account_id) if account_id else None

    def set_execution_gateway(self, gateway: Optional[ExecutionGateway]) -> None:
        """Bind the guardian to the runtime's unified remote exit gateway."""
        with self._lock:
            self.execution_gateway = gateway

    @property
    def account_id(self) -> str | None:
        with self._lock:
            return self._account_id

    def start(self) -> None:
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._last_heartbeat = datetime.now(timezone.utc)
            self._is_degraded = False
            self._degraded_reason = None
            self._active = True
            self._worker_thread = threading.Thread(
                target=self._run_guardian_loop,
                name="aima-position-guardian",
                daemon=True,
            )
            self._worker_thread.start()

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)

    def is_active(self) -> bool:
        with self._lock:
            return self._active and self._worker_thread is not None and self._worker_thread.is_alive()

    def is_running(self) -> bool:
        """Alias for is_active to check background guardian loop status."""
        return self.is_active()

    @property
    def last_heartbeat(self) -> Optional[float]:
        if self._last_heartbeat is None:
            return None
        return self._last_heartbeat.timestamp()

    @last_heartbeat.setter
    def last_heartbeat(self, val: Any) -> None:
        if isinstance(val, (int, float)):
            self._last_heartbeat = datetime.fromtimestamp(val, tz=timezone.utc)
        elif isinstance(val, datetime):
            self._last_heartbeat = val
        else:
            self._last_heartbeat = None

    def check_health(self, timeout_seconds: Optional[float] = None) -> bool:
        """Check whether Guardian is healthy based on last heartbeat within timeout."""
        timeout = timeout_seconds if timeout_seconds is not None else self.degraded_timeout
        now = datetime.now(timezone.utc)
        with self._lock:
            if self._is_degraded:
                return False
            if self._last_heartbeat is None:
                return False
            gap = (now - self._last_heartbeat).total_seconds()
            return gap < timeout

    def update_market_event(self, symbol: str, event_data: Dict[str, Any]) -> None:
        """Inject real-time bar or tick event for guardian processing."""
        with self._lock:
            self._price_feed[symbol] = {
                **event_data,
                "received_at": datetime.now(timezone.utc),
            }

    def process_bar(
        self,
        symbol: str,
        bar: Any,
        *,
        replay_now: datetime | str | None = None,
    ) -> List[Dict[str, Any]]:
        """Process one bar using wall time unless a replay clock is explicit.

        ``replay_now`` is deliberately opt-in.  A future timestamp on a live
        bar can never advance the default wall clock, while a bounded replay
        harness can advance its own virtual clock without changing live
        semantics.
        """
        with self._lock:
            return self._evaluate_positions_for_symbol(symbol, bar, replay_now=replay_now)

    def _run_guardian_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = self._now()
                with self._lock:
                    self._last_heartbeat = now
                    # A feed is an input, not the inventory of protection
                    # responsibility.  After restart or a stream outage the
                    # durable scoped positions still need deadline/news
                    # evaluation even when no quote has arrived.
                    feed_symbols = list(self._price_feed.keys())

                symbols_to_check = list(dict.fromkeys(
                    feed_symbols + self._durable_protected_symbols()
                ))

                for symbol in symbols_to_check:
                    feed_data = self._price_feed.get(symbol)
                    bar = feed_data.get("bar") if isinstance(feed_data, dict) else None
                    if isinstance(feed_data, dict) and bar is not None:
                        received_at = self._parse_plan_time(feed_data.get("received_at"))
                        if received_at is None or (now - received_at).total_seconds() > 120:
                            # A retained last bar is not a live quote after a
                            # stream outage.  Evaluate only independent
                            # deadline/news duties and persist the missing
                            # market-data boundary.
                            bar = None
                    self._evaluate_positions_for_symbol(symbol, bar)

            except Exception as exc:
                with self._lock:
                    self._is_degraded = True
                    self._degraded_reason = f"Guardian loop error: {str(exc)[:100]}"

            self._stop_event.wait(self.heartbeat_interval)

    def _durable_protected_symbols(self) -> List[str]:
        """Return symbols with live, explicitly scoped protection duties.

        This query deliberately does not require a market-feed row.  Unknown
        or legacy ownership is excluded rather than guessed, while an
        account/mode/venue mismatch cannot make another account's position
        eligible for this Guardian.
        """
        account_id = self.account_id
        if not account_id:
            return []
        try:
            with self._connection_scope() as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
                ).fetchone()
                if not table:
                    return []
                account_row = conn.execute(
                    "SELECT mode, config_json FROM accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if account_row is None:
                    return []
                scope = resolve_account_scope(self.store, account_id) or {}
                if str(scope.get("account_type") or "").upper() == "GATE_TESTNET":
                    # Gate TestNet protection is observed and submitted by the
                    # remote adapter.  This local Guardian only owns PAPER
                    # positions and must not act on historical Gate mirrors.
                    return []
                expected_mode = str(scope.get("mode") or account_row["mode"]).upper()
                expected_venue = str(
                    scope.get("venue")
                    or ("simulated" if expected_mode == "PAPER" else "gate")
                ).lower()
                rows = conn.execute(
                    """SELECT symbol, account_id, venue, mode, payload_json, legacy_unverified
                       FROM simulated_positions
                       WHERE status IN ('OPEN','PARTIALLY_CLOSED')
                         AND (account_id=? OR (account_id IS NULL AND json_extract(payload_json, '$.account_id')=?))""",
                    (account_id, account_id),
                ).fetchall()
                symbols: List[str] = []
                for row in rows:
                    if int(row["legacy_unverified"] or 0):
                        continue
                    payload = _safe_json_dict(row["payload_json"])
                    row_account = row["account_id"] or payload.get("account_id")
                    row_mode = str(row["mode"] or payload.get("mode") or "PAPER").upper()
                    row_venue = str(row["venue"] or payload.get("venue") or "simulated").lower()
                    symbol = str(row["symbol"] or payload.get("symbol") or "").strip().upper()
                    if (
                        row_account == account_id
                        and row_mode == expected_mode
                        and row_venue == expected_venue
                        and symbol
                    ):
                        symbols.append(symbol)
                return list(dict.fromkeys(symbols))
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            self._mark_degraded("Unable to enumerate durable protection responsibilities")
            return []

    @contextmanager
    def _connection_scope(self) -> Iterator[sqlite3.Connection]:
        """Use one connection for a complete Guardian operation.

        Request-scoped ``AccountLedger`` instances intentionally close their
        file connection after public methods.  Guardian needs a connection
        that remains valid while it performs the position CAS, audit events,
        fill record, and simulation event as one local transaction.  Using
        the store's operation-scoped connection also prevents a health/status
        read from leaving a Windows file handle behind.
        """
        if hasattr(self.store, "_connect"):
            with self.store._connect() as conn:
                yield conn
            return

        conn = self.ledger._get_conn()
        try:
            yield conn
        finally:
            if getattr(self.ledger, "_ephemeral_file_connection", False):
                self.ledger.close()

    def _evaluate_positions_for_symbol(
        self,
        symbol: str,
        bar: Any,
        *,
        replay_now: datetime | str | None = None,
    ) -> List[Dict[str, Any]]:
        with self._connection_scope() as conn:
            return self._evaluate_positions_for_symbol_with_conn(symbol, bar, conn, replay_now=replay_now)

    def _evaluate_non_market_protection_with_conn(
        self,
        symbol: str,
        conn: sqlite3.Connection,
    ) -> List[Dict[str, Any]]:
        """Evaluate durable clock/news protection when no quote is available.

        The loop must not disappear just because a websocket has not yet
        repopulated its in-memory feed after restart.  This path intentionally
        has no price and therefore cannot book a PAPER fill or submit a remote
        order; it persists an explicit degraded/pending boundary instead.
        """
        account_id = self.account_id
        if not account_id:
            return []
        account_row = conn.execute(
            "SELECT mode, config_json FROM accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if account_row is None:
            return []
        scope = resolve_account_scope(self.store, account_id) or {}
        if str(scope.get("account_type") or "").upper() == "GATE_TESTNET":
            return []
        expected_mode = str(scope.get("mode") or account_row["mode"]).upper()
        expected_venue = str(
            scope.get("venue")
            or ("simulated" if expected_mode == "PAPER" else "gate")
        ).lower()
        rows = conn.execute(
            """SELECT * FROM simulated_positions
               WHERE symbol=? AND status IN ('OPEN','PARTIALLY_CLOSED')
                 AND (account_id=? OR (account_id IS NULL AND json_extract(payload_json, '$.account_id')=?))""",
            (symbol, account_id, account_id),
        ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            if int(row["legacy_unverified"] or 0):
                continue
            pos = _safe_json_dict(row["payload_json"])
            identity = str(pos.get("position_id") or row["position_id"] or "")
            scoped_account = row["account_id"] or pos.get("account_id")
            row_mode = str(row["mode"] or pos.get("mode") or "PAPER").upper()
            row_venue = str(row["venue"] or pos.get("venue") or "simulated").lower()
            if (
                not identity
                or scoped_account != account_id
                or row_mode != expected_mode
                or row_venue != expected_venue
            ):
                continue
            pos["account_id"] = account_id
            pos.setdefault("venue", row_venue)
            pos.setdefault("mode", row_mode)
            contract = pos.get("protection_contract") if isinstance(pos.get("protection_contract"), dict) else {}
            event_state = self._event_invalidation_state(contract)
            event_unknown = bool(contract.get("event_invalidation")) and event_state is None
            deadline = self._parse_plan_time(contract.get("time_exit_at"))
            deadline_reached = self._deadline_reached(
                deadline,
                {"clock_now": self._now()},
            )
            trigger_reason = "EVENT_INVALIDATION" if event_state is True else "TIME_EXIT" if deadline_reached else None
            if event_unknown:
                reason = "EVENT_INVALIDATION_EVIDENCE_MISSING"
                evidence = {
                    "source": "news_revision_registry",
                    "status": "UNKNOWN",
                    "reconciliation_required": True,
                    "reason": reason,
                    "observed_at": self._now().isoformat(),
                }
                self._mark_degraded(f"Event invalidation evidence is unknown for {identity}")
            else:
                reason = trigger_reason or "MARKET_DATA_UNAVAILABLE"
                evidence = {
                    "source": "guardian_clock_or_news" if trigger_reason else "guardian_loop",
                    "status": "PENDING" if trigger_reason else "DEGRADED",
                    "reconciliation_required": True,
                    "reason": "MARKET_DATA_UNAVAILABLE",
                    "observed_at": self._now().isoformat(),
                }
                self._mark_degraded(
                    f"No fresh executable market data is available for protected position {identity}"
                )
            desired_status = "DEGRADED"
            desired_protected = False
            prior_evidence = pos.get("protection_evidence") if isinstance(pos.get("protection_evidence"), dict) else {}
            evidence_changed = {
                key: value for key, value in prior_evidence.items() if key != "observed_at"
            } != {
                key: value for key, value in evidence.items() if key != "observed_at"
            }
            changed = (
                str(pos.get("protection_status") or row["protection_status"] or "").upper() != desired_status
                or bool(pos.get("protected")) != desired_protected
                or pos.get("protection_last_reason") != reason
                or evidence_changed
            )
            if changed:
                old_version = int(row["position_version"] or pos.get("position_version", 0))
                pos["protection_status"] = desired_status
                pos["protected"] = desired_protected
                pos["protection_last_observed_at"] = self._now().isoformat()
                pos["protection_last_reason"] = reason
                pos["protection_evidence"] = evidence
                pos["position_version"] = old_version + 1
                updated = conn.execute(
                    """UPDATE simulated_positions
                       SET payload_json=?, updated_at=?, position_version=?, protection_status=?
                       WHERE position_id=? AND status IN ('OPEN','PARTIALLY_CLOSED')
                         AND position_version=? AND legacy_unverified=0""",
                    (
                        json.dumps(pos, allow_nan=False),
                        self._now().isoformat(),
                        old_version + 1,
                        desired_status,
                        identity,
                        old_version,
                    ),
                )
                if updated.rowcount != 1:
                    continue
            if trigger_reason:
                results.append({
                    "position_id": identity,
                    "symbol": symbol,
                    "reason": trigger_reason,
                    "quantity": float(pos.get("remaining_contracts", pos.get("contracts", 0.0))),
                    "price": None,
                    "status": "PENDING",
                    "remote": row_mode in {TradingMode.TESTNET.value, TradingMode.LIVE.value},
                    "economic_fill_recorded": False,
                    "error_code": "MARKET_DATA_UNAVAILABLE",
                    "message": "Protection trigger is known, but no fresh executable quote is available; position remains open.",
                    "protection_evidence": evidence,
                })
        conn.commit()
        return results

    @staticmethod
    def _event_status_matches(observed: str, invalid_if: Any) -> bool:
        """Match an explicit invalidation status without treating active news as bad."""
        observed_clean = str(observed or "").strip().upper()
        requested = str(invalid_if or "ACTIVE_OR_CORRECTED").strip().upper()
        # ``ACTIVE_OR_CORRECTED`` is the historical contract name for an
        # event that is valid now but becomes invalid when its revision is
        # changed/retracted.  It must not match ACTIVE at the moment the
        # protection contract is armed.
        if requested in {"ACTIVE_OR_CORRECTED", "ACTIVE_OR_CHANGED"}:
            return observed_clean in {"CORRECTED", "RETRACTED", "DISPUTED", "EXPIRED", "INVALIDATED"}
        requested_statuses = {
            part.strip().upper()
            for part in requested.replace("/", "|").replace(",", "|").split("|")
            if part.strip()
        }
        return observed_clean in requested_statuses

    def _event_invalidation_state(self, contract: Dict[str, Any]) -> Optional[bool]:
        """Return True for a stored correction/retraction, None for unknown."""
        conditions = contract.get("event_invalidation") if isinstance(contract, dict) else None
        if not conditions:
            return False
        registry = NewsRevisionRegistry(self.store)
        saw_unknown = False
        for condition in conditions:
            if not isinstance(condition, dict):
                saw_unknown = True
                continue
            key = str(condition.get("event_key") or "").strip()
            if not key:
                saw_unknown = True
                continue
            observed_status: str | None = None
            revision = registry.get_revision(key)
            if revision is not None:
                latest = registry.get_revisions_for_news(revision.news_id)
                if latest and latest[-1].revision_id != revision.revision_id:
                    latest_status = str(latest[-1].claim_status or "").upper()
                    observed_status = latest_status if latest_status in {"RETRACTED", "DISPUTED", "EXPIRED", "INVALIDATED"} else "CORRECTED"
                else:
                    observed_status = str(revision.claim_status or "ACTIVE").upper()
            else:
                # An event key may be a news_id rather than a revision_id.
                # Resolve it through the durable registry, never through the
                # caller's display text.
                revisions = registry.get_revisions_for_news(key)
                if revisions:
                    latest = revisions[-1]
                    observed_status = str(latest.claim_status or "ACTIVE").upper()
                else:
                    saw_unknown = True
                    continue
            if self._event_status_matches(observed_status, condition.get("invalid_if")):
                return True
        return None if saw_unknown else False

    @staticmethod
    def _parse_plan_time(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            point = value
        else:
            try:
                point = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        return point.replace(tzinfo=timezone.utc) if point.tzinfo is None else point.astimezone(timezone.utc)

    @classmethod
    def _position_activation_time(cls, position: Dict[str, Any]) -> Optional[datetime]:
        """Return the earliest time at which this protection may use OHLC.

        Entry fill time and protection confirmation time are deliberately
        separate facts.  The later one is the safe activation boundary: an
        aggregate candle that started before it cannot prove a post-activation
        stop hit.
        """
        points = [
            cls._parse_plan_time(position.get("entry_filled_at")),
            cls._parse_plan_time(position.get("protection_effective_at")),
        ]
        if not any(points):
            points.extend(
                [
                    cls._parse_plan_time(position.get("created_at")),
                    cls._parse_plan_time(position.get("opened_at")),
                ]
            )
        points = [point for point in points if point is not None]
        return max(points) if points else None

    def _market_observation_context(self, symbol: str, bar: Any) -> Dict[str, Any]:
        """Normalize bar start, event time, sequence, and executable point.

        Providers historically exposed only ``Bar.timestamp`` (the bar
        start).  Newer streams may also provide ``bar_end``/``event_at`` and a
        sequence.  The runtime feed's receipt time is used only when it is
        attached to this exact bar, which lets an unclosed current candle be
        observed without treating a historical candle as a live quote.
        """
        bar_start = self._parse_plan_time(
            getattr(bar, "bar_start", None) or getattr(bar, "timestamp", None)
        )
        bar_end = self._parse_plan_time(getattr(bar, "bar_end", None))
        is_closed = getattr(bar, "is_closed", None)
        event_at = self._parse_plan_time(
            getattr(bar, "event_at", None)
            or getattr(bar, "observed_at", None)
            or getattr(bar, "event_time", None)
        )
        sequence = getattr(bar, "sequence", None) or getattr(bar, "event_sequence", None)
        received_at = None

        feed = self._price_feed.get(str(symbol).upper())
        feed_matches = False
        if isinstance(feed, dict):
            feed_bar = feed.get("bar")
            if feed_bar is bar:
                feed_matches = True
            elif feed_bar is not None and bar_start is not None:
                feed_start = self._parse_plan_time(
                    getattr(feed_bar, "bar_start", None) or getattr(feed_bar, "timestamp", None)
                )
                feed_matches = feed_start == bar_start
                if feed_matches:
                    for field in ("open", "high", "low", "close"):
                        if getattr(feed_bar, field, None) != getattr(bar, field, None):
                            feed_matches = False
                            break
            if feed_matches:
                event_at = event_at or self._parse_plan_time(feed.get("event_at"))
                sequence = sequence or feed.get("sequence") or feed.get("event_sequence")
                received_at = self._parse_plan_time(feed.get("received_at"))
                if feed.get("is_closed") is not None:
                    is_closed = bool(feed.get("is_closed"))

        observation_at = event_at or bar_start
        # A transport receipt is a usable current point only for a bar that
        # still plausibly represents the current intrabar.  A delayed
        # historical candle must keep its logical bar-start time; otherwise a
        # late news/deadline callback could reuse that candle's close as if it
        # were a new quote.  Realtime providers attach ``event_at`` explicitly
        # and are not subject to this compatibility inference.
        point_at = event_at or bar_start
        if event_at is None and received_at is not None and bar_start is not None:
            transport_age = (received_at - bar_start).total_seconds()
            if transport_age <= 120:
                point_at = received_at
        return {
            "bar_start": bar_start,
            "bar_end": bar_end,
            "is_closed": is_closed,
            "event_at": event_at,
            "observation_at": observation_at,
            # Transport receipt time is useful for a current intrabar point,
            # but it is not the market event/deadline time.  Keeping both
            # prevents a locally delivered future-dated simulation bar from
            # losing its logical time semantics.
            "received_at": received_at,
            "point_at": point_at or received_at or bar_start,
            "sequence": sequence,
            "clock_now": self._now(),
            "feed": feed if feed_matches else None,
            "feed_matches": feed_matches,
        }

    def _fresh_executable_price(
        self,
        symbol: str,
        bar: Any,
        context: Dict[str, Any],
        activation: Optional[datetime],
    ) -> Optional[float]:
        """Return a current executable point, never a historical bar open.

        Event invalidation and deadline exits are not allowed to reuse the
        candle open that happened before the trigger.  A point is accepted
        only when its observation time is at/after protection activation and
        is not older than the local freshness bound.  An explicit bar end
        before activation is never revived by a late delivery timestamp.
        """
        observation_at = context.get("point_at") or context.get("observation_at")
        if observation_at is None:
            return None
        if activation is not None:
            if context.get("bar_end") is not None and context["bar_end"] <= activation:
                return None
            if observation_at < activation:
                return None
        now = context.get("clock_now") or self._now()
        if not isinstance(now, datetime):
            now = self._now()
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        if observation_at <= now and (now - observation_at).total_seconds() > 120:
            return None

        candidates: list[Any] = []
        feed = context.get("feed")
        if isinstance(feed, dict):
            candidates.extend(feed.get(name) for name in ("price", "last", "mark"))
            feed_bar = feed.get("bar")
            if feed_bar is not None:
                candidates.extend(getattr(feed_bar, name, None) for name in ("price", "last", "mark", "close"))
        candidates.extend(getattr(bar, name, None) for name in ("price", "last", "mark", "close"))
        for candidate in candidates:
            value = _number_or_none(candidate)
            if value is not None:
                return value
        return None

    @staticmethod
    def _sequence_is_older(current: Any, previous: Any) -> bool:
        if current is None or previous is None:
            return False
        try:
            return int(current) < int(previous)
        except (TypeError, ValueError):
            return str(current) < str(previous)

    @staticmethod
    def _deadline_reached(deadline: Optional[datetime], context: Dict[str, Any]) -> bool:
        if deadline is None:
            return False
        clock_now = context.get("clock_now")
        if not isinstance(clock_now, datetime):
            clock_now = datetime.now(timezone.utc)
        elif clock_now.tzinfo is None:
            clock_now = clock_now.replace(tzinfo=timezone.utc)
        else:
            clock_now = clock_now.astimezone(timezone.utc)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        else:
            deadline = deadline.astimezone(timezone.utc)

        # A live wall clock is the authority for a deadline.  A market event
        # may corroborate it only after that event has actually arrived; a
        # future-dated fixture/event cannot move real time forward.  The
        # interval end is considered only when its producer explicitly marks
        # the bar closed, and it still cannot be later than the clock.
        if clock_now >= deadline:
            return True
        for field in ("observation_at", "event_at"):
            point = context.get(field)
            if isinstance(point, datetime) and point <= clock_now and point >= deadline:
                return True
        bar_end = context.get("bar_end")
        if context.get("is_closed") is True and isinstance(bar_end, datetime):
            if bar_end <= clock_now and bar_end >= deadline:
                return True
        return False

    @staticmethod
    def _trailing_stop(pos: Dict[str, Any], contract: Dict[str, Any], bar: Any) -> Optional[float]:
        trailing = contract.get("trailing_protection") if isinstance(contract, dict) else None
        if not isinstance(trailing, dict):
            return None
        kind = str(trailing.get("type") or "").upper()
        try:
            value = float(trailing.get("value"))
            activation = trailing.get("activation_price")
            activation_value = float(activation) if activation is not None else None
            side = str(pos.get("side") or "").upper()
            current_stop = float(pos.get("stop", pos.get("stop_loss", 0.0)))
            if value <= 0 or current_stop <= 0:
                return None
            favorable_raw = getattr(bar, "high" if side == "LONG" else "low", None)
            if favorable_raw is None:
                favorable_raw = getattr(bar, "price", None) or getattr(bar, "close", None)
            favorable = float(favorable_raw)
            if activation_value is not None and ((side == "LONG" and favorable < activation_value) or (side == "SHORT" and favorable > activation_value)):
                return None
            candidate = favorable * (1.0 - value / 100.0) if kind == "PERCENT" and side == "LONG" else favorable * (1.0 + value / 100.0) if kind == "PERCENT" and side == "SHORT" else favorable - value if side == "LONG" else favorable + value
            if side == "LONG" and candidate > current_stop and candidate < favorable:
                return candidate
            if side == "SHORT" and candidate < current_stop and candidate > favorable:
                return candidate
        except (TypeError, ValueError, OverflowError):
            return None
        return None

    def _evaluate_positions_for_symbol_with_conn(
        self,
        symbol: str,
        bar: Any,
        conn: sqlite3.Connection,
        *,
        replay_now: datetime | str | None = None,
    ) -> List[Dict[str, Any]]:
        """Evaluate open positions for symbol against market bar (Stop-First & Conservative Exit)."""
        if bar is None:
            return self._evaluate_non_market_protection_with_conn(symbol, conn)
        exits_executed = []

        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
        ).fetchone()
        if not has_table:
            return exits_executed

        scoped_account_id = self.account_id
        expected_mode: str | None = None
        expected_venue: str | None = None
        if scoped_account_id:
            account_row = conn.execute(
                "SELECT mode, config_json FROM accounts WHERE account_id=?",
                (scoped_account_id,),
            ).fetchone()
            if account_row is None:
                return exits_executed
            scope = resolve_account_scope(self.store, scoped_account_id) or {}
            if str(scope.get("account_type") or "").upper() == "GATE_TESTNET":
                return exits_executed
            expected_mode = str(scope.get("mode") or account_row["mode"]).upper()
            expected_venue = str(
                scope.get("venue")
                or ("simulated" if expected_mode == "PAPER" else "gate")
            ).lower()

        account_id = self.account_id
        query = "SELECT * FROM simulated_positions WHERE symbol=? AND status IN ('OPEN', 'PARTIALLY_CLOSED')"
        params: list[Any] = [symbol]
        if account_id:
            query += " AND (account_id=? OR (account_id IS NULL AND json_extract(payload_json, '$.account_id')=?))"
            params.extend([account_id, account_id])
        rows = conn.execute(query, tuple(params)).fetchall()

        for row in rows:
            if "legacy_unverified" in row.keys() and int(row["legacy_unverified"] or 0):
                continue
            pos = json.loads(row["payload_json"])
            identity = pos.get("position_id")
            if not identity:
                continue
            scoped_account = row["account_id"] if "account_id" in row.keys() else None
            scoped_account = scoped_account or pos.get("account_id")
            if not scoped_account:
                # A position without an explicit account is not safe to
                # manage.  Migration never guesses ownership.
                continue
            row_venue = str(
                (row["venue"] if "venue" in row.keys() else None)
                or pos.get("venue")
                or "simulated"
            ).lower()
            row_mode = str(
                (row["mode"] if "mode" in row.keys() else None)
                or pos.get("mode")
                or "PAPER"
            ).upper()
            if scoped_account_id and (
                scoped_account != scoped_account_id
                or row_mode != expected_mode
                or row_venue != expected_venue
            ):
                continue
            pos["account_id"] = scoped_account
            pos.setdefault("venue", row_venue)
            pos.setdefault("mode", row_mode)
            base_version = int(row["position_version"] if "position_version" in row.keys() and row["position_version"] is not None else pos.get("position_version", 0))
            observation_context = self._market_observation_context(symbol, bar)
            if replay_now is not None:
                virtual_now = self._parse_plan_time(replay_now)
                if virtual_now is not None:
                    observation_context["clock_now"] = virtual_now
                    observation_context["is_replay"] = True
                    # A replay's logical point is its event/bar timestamp,
                    # not the wall-clock time at which the fixture happened
                    # to be delivered to this process.
                    observation_context["point_at"] = observation_context.get("event_at") or observation_context.get("observation_at")
            bar_time = observation_context["bar_start"]
            previous_bar_time = self._parse_plan_time(pos.get("protection_last_bar_at"))
            out_of_order = bool(
                bar_time is not None
                and previous_bar_time is not None
                and bar_time < previous_bar_time
            )
            same_bar = bar_time is not None and bar_time == previous_bar_time
            point_value = next(
                (
                    _number_or_none(getattr(bar, name, None))
                    for name in ("price", "last", "mark", "close")
                    if _number_or_none(getattr(bar, name, None)) is not None
                ),
                None,
            )
            if point_value is None:
                # An invalid market event cannot be turned into an exit
                # price.  Keep the durable event/deadline path alive below,
                # but do not manufacture OHLC values.
                return self._evaluate_non_market_protection_with_conn(symbol, conn)
            observation = {
                name: _number_or_none(getattr(bar, name, None)) or point_value
                for name in ("open", "high", "low", "close")
            }
            # Price-only events remain valid for risk protection. Unknown
            # volume is not zero and must not suppress a new adverse price.
            volume = getattr(bar, "volume", None)
            try:
                volume_value = float(volume) if volume is not None else None
            except (TypeError, ValueError, OverflowError):
                volume_value = None
            observation["volume"] = (
                volume_value
                if volume_value is not None
                and volume_value == volume_value
                and abs(volume_value) != float("inf")
                and volume_value >= 0
                else None
            )
            previous_observation = pos.get("protection_last_bar_observation") or {}
            previous_observation_at = self._parse_plan_time(pos.get("protection_last_observation_at"))
            observation_at = observation_context.get("observation_at")
            distinct_event = bool(
                observation_context.get("event_at") is not None
                and isinstance(observation_at, datetime)
                and (previous_observation_at is None or observation_at != previous_observation_at)
            )
            current_sequence = observation_context.get("sequence")
            previous_sequence = pos.get("protection_last_observation_sequence")
            if current_sequence is not None and current_sequence != previous_sequence:
                distinct_event = True
            market_revision_accepted = not out_of_order
            same_observation = False
            if same_bar and previous_observation:
                if observation == previous_observation and not distinct_event:
                    same_observation = True
                    market_revision_accepted = False
                # Cumulative OHLCV must not regress on a stale websocket
                # revision. Timestamp alone identifies a candle, not an event.
                if ((observation["volume"] is not None and previous_observation.get("volume") is not None
                        and observation["volume"] < previous_observation["volume"])
                        or observation["high"] < previous_observation["high"]
                        or observation["low"] > previous_observation["low"]):
                    market_revision_accepted = False
            sequence_out_of_order = self._sequence_is_older(
                observation_context.get("sequence"),
                pos.get("protection_last_observation_sequence"),
            )
            if sequence_out_of_order:
                market_revision_accepted = False
            marker_persisted = False
            if bar_time is not None and market_revision_accepted:
                pos["protection_last_bar_at"] = bar_time.isoformat()
                pos["protection_last_bar_observation"] = observation
                if isinstance(observation_at, datetime):
                    pos["protection_last_observation_at"] = observation_at.isoformat()
                if observation_context.get("sequence") is not None:
                    pos["protection_last_observation_sequence"] = observation_context["sequence"]
            side = str(pos.get("side", "LONG")).upper()
            sign = 1 if side == "LONG" else -1
            entry = float(pos.get("entry", 0.0))
            stop = float(pos.get("stop", 0.0))
            targets = [float(t) for t in pos.get("targets", [])]
            rem_contracts = float(pos.get("remaining_contracts", pos.get("contracts", 0.0)))
            protection_contract = pos.get("protection_contract") if isinstance(pos.get("protection_contract"), dict) else {}
            partial_targets = protection_contract.get("partial_take_profits") if isinstance(protection_contract.get("partial_take_profits"), list) else []
            contract_size = float(pos.get("contract_size", 1.0))
            fee_rate = float(pos.get("fee_rate", 0.00075))
            slippage = float(pos.get("slippage", 0.001))

            if rem_contracts <= 0:
                continue

            bar_high = observation["high"]
            bar_low = observation["low"]
            bar_open = observation["open"]

            activation_time = self._position_activation_time(pos)
            bar_start = observation_context.get("bar_start")
            bar_end = observation_context.get("bar_end")
            if activation_time is None or bar_start is None:
                ohlc_usable = True
            elif bar_end is not None and bar_end <= activation_time:
                ohlc_usable = False
            else:
                # A candle that began before protection was effective is a
                # cumulative interval.  Only a separately timestamped point
                # may be used for its current intrabar state.
                ohlc_usable = bar_start >= activation_time
            executable_price = None
            if (
                not out_of_order
                and not sequence_out_of_order
                # An exact duplicate may still carry an independently
                # verified news/deadline trigger and can reuse its current
                # quote.  A regressive OHLC/volume revision cannot be used
                # as either a stop point or an event-exit price.
                and (market_revision_accepted or same_observation)
            ):
                executable_price = self._fresh_executable_price(
                    symbol, bar, observation_context, activation_time
                )
            point_usable = executable_price is not None and not ohlc_usable
            market_usable = bool(market_revision_accepted and ohlc_usable)
            # The first observation of a candle that straddles activation is
            # only a baseline: its cumulative low/high may predate the fill.
            # A later, ordered revision that extends that same candle in the
            # adverse direction is new evidence after the baseline.  It may
            # trigger the stop, but only with the separately timestamped
            # current quote above; the aggregate extreme is never used as an
            # invented execution price.
            crossing_bar_new_extreme = bool(
                not ohlc_usable
                and same_bar
                and previous_observation
                and distinct_event
                and market_revision_accepted
                and (
                    observation["low"] < _number_or_none(previous_observation.get("low"))
                    if sign == 1 and _number_or_none(previous_observation.get("low")) is not None
                    else observation["high"] > _number_or_none(previous_observation.get("high"))
                    if sign == -1 and _number_or_none(previous_observation.get("high")) is not None
                    else False
                )
            )

            # Missing event evidence is an explicit degraded protection fact,
            # not a false event and not permission to invent a close.  Set it
            # before the trailing CAS so the status/evidence travels with any
            # same-bar tightening or watermark update.
            event_state = self._event_invalidation_state(protection_contract)
            event_unknown = bool(protection_contract.get("event_invalidation")) and event_state is None
            if event_unknown:
                pos["protection_status"] = "DEGRADED"
                pos["protected"] = False
                pos["protection_evidence"] = {
                    "source": "news_revision_registry",
                    "status": "UNKNOWN",
                    "reconciliation_required": True,
                    "reason": "EVENT_INVALIDATION_EVIDENCE_MISSING",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
                self._mark_degraded(f"Event invalidation evidence is unknown for {identity}; hard stop retained")
            elif (
                event_state is not True
                and not self._deadline_reached(
                    self._parse_plan_time(protection_contract.get("time_exit_at")),
                    observation_context,
                )
                and (market_usable or point_usable)
                and str(pos.get("protection_status") or "").upper() != "ACTIVE"
            ):
                # A fresh quote has restored the ordinary protection path
                # after a feed outage.  Triggered event/deadline states stay
                # degraded until their fresh execution/reconciliation path
                # succeeds; they are never cleared by a heartbeat alone.
                pos["protection_status"] = "ACTIVE"
                pos["protected"] = True

            # Stop-first is conservative for bars that both make a new high
            # (or low) and cross a newly computed trailing stop.  The stop
            # that existed at the beginning of the bar is the only stop that
            # can be credited with a same-bar fill; a trailing tightening is
            # persisted only after that check and is effective on the next
            # bar.  This avoids manufacturing a fill from future intrabar
            # ordering that OHLC bars do not reveal.
            stop_hit = False
            current_close = observation["close"]
            close_crossed = current_close <= stop if sign == 1 else current_close >= stop
            if market_usable:
                stop_hit = bar_low <= stop if sign == 1 else bar_high >= stop
            elif point_usable:
                # The bar began before activation.  A timestamped current
                # point can trigger, but its historical high/low cannot.
                current_close = float(executable_price)
                close_crossed = current_close <= stop if sign == 1 else current_close >= stop
                stop_hit = close_crossed or crossing_bar_new_extreme
            if same_bar and market_usable:
                # A stop may have tightened since the last revision. Old
                # intrabar extrema cannot establish a crossing of that new
                # stop; a new adverse extreme or the current price can.
                new_extreme = bool(previous_observation) and (
                    bar_low < previous_observation["low"] if sign == 1
                    else bar_high > previous_observation["high"]
                )
                stop_hit = close_crossed or (new_extreme and stop_hit)
            exits = []

            if stop_hit:
                # Conservative stop execution price
                exec_price = min(bar_open, stop) if sign == 1 else max(bar_open, stop)
                if point_usable:
                    exec_price = min(current_close, stop) if sign == 1 else max(current_close, stop)
                elif same_bar:
                    # The candle open predates this stop activation.
                    exec_price = (min(current_close, stop) if sign == 1 else max(current_close, stop)) if close_crossed else stop
                exits.append((rem_contracts, exec_price, "STOP"))
            else:
                # A plan's trailing rule is a one-way risk tightening
                # operation.  Persist it with the same position CAS used by
                # exits, so a concurrent manual/AI update cannot widen or
                # overwrite it.
                candidate_stop = self._trailing_stop(pos, protection_contract, bar) if market_usable else None
                if candidate_stop is not None:
                    old_version = base_version
                    pos["stop"] = candidate_stop
                    pos["stop_loss"] = candidate_stop
                    pos["trailing_last_stop"] = candidate_stop
                    pos["trailing_last_observed_at"] = self._bar_timestamp(bar)
                    pos["position_version"] = old_version + 1
                    tightened = conn.execute(
                        """UPDATE simulated_positions
                           SET payload_json=?, updated_at=?, position_version=?, protection_status=?
                           WHERE position_id=? AND status IN ('OPEN','PARTIALLY_CLOSED')
                             AND position_version=? AND legacy_unverified=0""",
                        (json.dumps(pos, allow_nan=False), datetime.now(timezone.utc).isoformat(), old_version + 1, pos.get("protection_status", "ACTIVE"), identity, old_version),
                    )
                    if tightened.rowcount == 1:
                        base_version = old_version + 1
                        stop = candidate_stop
                        marker_persisted = True
                    else:
                        # A concurrent owner changed the position.  Refrain
                        # from making an exit decision from stale state.
                        continue

                # A verified news correction/retraction is a deterministic
                # event invalidation.  Unknown event evidence never creates a
                # synthetic exit; the hard stop remains the protection fact.
                time_exit = self._parse_plan_time(protection_contract.get("time_exit_at"))
                deadline_reached = self._deadline_reached(time_exit, observation_context)
                if event_state is True or deadline_reached:
                    reason = "EVENT_INVALIDATION" if event_state is True else "TIME_EXIT"
                    # Event and clock triggers are independent of candle
                    # changes.  They require a fresh point and must never use
                    # the old bar open as a fabricated execution price.
                    if executable_price is not None:
                        exits.append((rem_contracts, executable_price, reason))
                    else:
                        # The trigger is a durable protection fact, but there
                        # is no timestamped executable quote with which to
                        # book an economic fill.  Persist that boundary so a
                        # PAPER caller cannot mistake the pending observation
                        # for a local close and an operator can see that the
                        # protection needs data/adapter recovery.
                        pending_at = datetime.now(timezone.utc).isoformat()
                        pos["protection_status"] = "DEGRADED"
                        pos["protected"] = False
                        pos["protection_last_observed_at"] = pending_at
                        pos["protection_last_reason"] = reason
                        pos["protection_evidence"] = {
                            "source": "guardian_clock_or_news",
                            "status": "PENDING",
                            "reconciliation_required": True,
                            "reason": "MARKET_DATA_UNAVAILABLE",
                            "observed_at": pending_at,
                        }
                        self._mark_degraded(
                            f"Protection {reason} is known, but no fresh executable quote is available for {identity}"
                        )
                        exits_executed.append({
                            "position_id": identity,
                            "symbol": symbol,
                            "reason": reason,
                            "quantity": rem_contracts,
                            "price": None,
                            "status": "PENDING",
                            "remote": row_mode in {TradingMode.TESTNET.value, TradingMode.LIVE.value},
                            "economic_fill_recorded": False,
                            "error_code": "MARKET_DATA_UNAVAILABLE",
                            "message": "Protection trigger is known, but no fresh executable quote is available; position remains open.",
                            "protection_evidence": {
                                "source": "guardian_clock_or_news",
                                "status": "PENDING",
                                "reconciliation_required": True,
                                "observed_at": pending_at,
                            },
                        })
                elif market_usable:
                    if partial_targets:
                        original_contracts = float(pos.get("contracts", pos.get("filled_contracts", rem_contracts)))
                        done = {int(item) for item in (pos.get("partial_take_profit_done") or []) if str(item).isdigit()}
                        for i, partial in enumerate(partial_targets):
                            if not isinstance(partial, dict) or i in done:
                                continue
                            try:
                                target = float(partial["price"])
                                fraction = float(partial["fraction"])
                            except (KeyError, TypeError, ValueError):
                                continue
                            target_hit = bar_high >= target if sign == 1 else bar_low <= target
                            if target_hit:
                                qty = min(original_contracts * fraction, rem_contracts - sum(e[0] for e in exits))
                                if qty > 0:
                                    exits.append((qty, target, f"TP{i+1}"))
                                    done.add(i)
                        pos["partial_take_profit_done"] = sorted(done)
                        final_target = _number_or_none(protection_contract.get("take_profit"))
                        if final_target is None and targets:
                            # A legacy target list remains valid when a plan
                            # only adds partials; it is the final target.
                            final_target = targets[-1]
                        final_hit = final_target is not None and (bar_high >= final_target if sign == 1 else bar_low <= final_target)
                        if final_hit and rem_contracts - sum(e[0] for e in exits) > 0:
                            exits.append((rem_contracts - sum(e[0] for e in exits), final_target, "TP_FINAL"))
                    else:
                        # Check legacy targets only when the whole OHLC
                        # interval is known to be post-activation.
                        tp_ratio = float(pos.get("tp_ratio", 0.5))
                        for i, target in enumerate(targets):
                            if i == 0 and pos.get("tp1_done"):
                                continue
                            target_hit = bar_high >= target if sign == 1 else bar_low <= target
                            if target_hit:
                                if i == 0 and (len(targets) > 1 or tp_ratio < 1.0):
                                    qty = float(pos.get("filled_contracts", rem_contracts)) * tp_ratio
                                    qty = min(qty, rem_contracts - sum(e[0] for e in exits))
                                else:
                                    qty = rem_contracts - sum(e[0] for e in exits)
                                if qty > 0:
                                    exits.append((qty, target, f"TP{i+1}"))
                                    if i == 0:
                                        pos["tp1_done"] = True
                                        pos["stop"] = entry

            for qty, price, reason in exits:
                if row_mode in {TradingMode.TESTNET.value, TradingMode.LIVE.value}:
                    remote_result = self._submit_remote_protection(
                        pos=pos,
                        symbol=symbol,
                        quantity=qty,
                        reason=reason,
                        bar=bar,
                        executable_price=executable_price,
                        executable_at=observation_context.get("point_at") or observation_context.get("observation_at"),
                        conn=conn,
                    )
                    self._record_remote_protection_observation(
                        conn,
                        row=row,
                        pos=pos,
                        symbol=symbol,
                        reason=reason,
                        bar=bar,
                        result=remote_result,
                    )
                    marker_persisted = True
                    exits_executed.append({
                        "position_id": identity,
                        "symbol": symbol,
                        "reason": reason,
                        "quantity": qty,
                        "price": None,
                        "status": remote_result.get("status", "DEGRADED"),
                        "remote": True,
                        "economic_fill_recorded": bool(remote_result.get("economic_fill_recorded")),
                        "error_code": remote_result.get("error_code"),
                        "message": remote_result.get("message"),
                        "receipt": remote_result.get("receipt"),
                        "protection_evidence": remote_result.get("protection_evidence"),
                    })
                    # TESTNET/LIVE fills are changed only by a concrete
                    # adapter report reconciled through ExecutionGateway.
                    # A bar crossing itself is never a local fill fact.
                    continue
                # Claim the exact position revision before computing economic
                # effects.  A second Guardian instance (or AI reduce) loses
                # this compare-and-set race instead of overselling.
                claimed = conn.execute(
                    """UPDATE simulated_positions
                       SET status='CLOSING', position_version=position_version+1
                       WHERE position_id=? AND status IN ('OPEN','PARTIALLY_CLOSED') AND position_version=? AND legacy_unverified=0""",
                    (identity, base_version),
                )
                if claimed.rowcount != 1:
                    break
                base_version += 1
                fill_price = price * (1 - sign * slippage)
                gross_realized = qty * contract_size * (fill_price - entry) * sign
                fee = qty * contract_size * fill_price * fee_rate
                net_realized = gross_realized - fee

                pos["realized_pnl"] = pos.get("realized_pnl", 0.0) + net_realized
                pos["remaining_contracts"] = max(0.0, rem_contracts - qty)
                rem_contracts = pos["remaining_contracts"]

                new_status = "CLOSED" if pos["remaining_contracts"] <= 1e-8 else "PARTIALLY_CLOSED"
                pos["status"] = new_status
                pos["protection_status"] = (
                    "CLOSED"
                    if new_status == "CLOSED"
                    else "DEGRADED" if event_unknown else "ACTIVE"
                )
                pos["protected"] = pos["protection_status"] == "ACTIVE"
                pos["updated_at"] = datetime.now(timezone.utc).isoformat()
                pos["position_version"] = base_version + 1

                # Update database
                conn.execute(
                    """UPDATE simulated_positions
                       SET status=?, payload_json=?, updated_at=?, account_id=?, venue=?, mode=?,
                           position_version=?, protection_status=?
                       WHERE position_id=? AND status='CLOSING' AND position_version=?""",
                    (new_status, json.dumps(pos, allow_nan=False), pos["updated_at"], scoped_account, pos.get("venue", "simulated"), pos.get("mode", "PAPER"), pos["position_version"], pos.get("protection_status", "ACTIVE"), identity, base_version),
                )
                base_version = int(pos["position_version"])

                # Record economic exit in ledger (gross PnL and fee separately, no double deduction)
                account_id = pos.get("account_id", "default")
                account_row = conn.execute(
                    "SELECT currency FROM accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                account_currency = str((account_row[0] if account_row else "USDT") or "USDT").upper()
                event_at = (
                    observation_context.get("point_at")
                    or observation_context.get("observation_at")
                    or getattr(bar, "timestamp", None)
                    or datetime.now(timezone.utc)
                )
                if not isinstance(event_at, datetime):
                    event_at = datetime.now(timezone.utc)
                if event_at.tzinfo is None:
                    event_at = event_at.replace(tzinfo=timezone.utc)
                recorded_at = datetime.now(timezone.utc)
                self.ledger._record_event_locked(
                    conn,
                    event_id=f"guardian_exit:{identity}:{getattr(bar, 'timestamp', 'unknown')}:{reason}",
                    account_id=account_id,
                    event_type=LedgerEventType.FILL_EXIT,
                    currency=account_currency,
                    amount=Decimal(str(gross_realized)),
                    payload={"position_id": identity, "reason": reason, "fill_price": fill_price, "qty": qty, "gross_realized": gross_realized, "fee": fee},
                    occurred_at=event_at,
                    recorded_at=recorded_at,
                    commit=False,
                )
                self.ledger._record_event_locked(
                    conn,
                    event_id=f"guardian_fee:{identity}:{getattr(bar, 'timestamp', 'unknown')}:{reason}",
                    account_id=account_id,
                    event_type=LedgerEventType.FEE,
                    currency=account_currency,
                    amount=Decimal(str(fee)),
                    payload={"position_id": identity, "type": "EXIT_FEE", "fee": fee},
                    occurred_at=event_at,
                    recorded_at=recorded_at,
                    commit=False,
                )

                # Guardian exits are real fills in the local PAPER execution
                # contract too.  The position CAS above already applied the
                # state transition, so this audit row intentionally records
                # the fill without calling record_trade_fill a second time.
                has_fill_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_fills'"
                ).fetchone()
                if has_fill_table:
                    guardian_trade_id = f"guardian:{identity}:{getattr(bar, 'timestamp', 'unknown')}:{reason}"
                    guardian_fill_id = f"{account_id}|{str(pos.get('venue', 'simulated')).lower()}|{str(pos.get('mode', 'PAPER')).upper()}|{guardian_trade_id}"
                    exit_side = "SELL" if side == "LONG" else "BUY"
                    event_at = (
                        observation_context.get("point_at")
                        or observation_context.get("observation_at")
                        or getattr(bar, "timestamp", None)
                        or datetime.now(timezone.utc)
                    )
                    if not isinstance(event_at, datetime):
                        event_at = datetime.now(timezone.utc)
                    conn.execute(
                        """INSERT OR IGNORE INTO trade_fills
                           (fill_id, account_id, venue, mode, order_id, trade_id,
                            position_id, symbol, side, quantity, price, fee,
                            fee_amount, fee_currency, fx_rate, contract_size,
                            status, payload_json, created_at, event_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   'RECORDED', ?, ?, ?)""",
                        (
                            guardian_fill_id,
                            account_id,
                            str(pos.get("venue", "simulated")).lower(),
                            str(pos.get("mode", "PAPER")).upper(),
                            guardian_trade_id,
                            guardian_trade_id,
                            identity,
                            symbol,
                            exit_side,
                            str(qty),
                            str(fill_price),
                            str(fee),
                            str(fee),
                            account_currency,
                            None,
                            str(contract_size),
                            json.dumps({
                                "source": "position_guardian",
                                "position_id": identity,
                                "account_id": account_id,
                                "venue": pos.get("venue", "simulated"),
                                "mode": pos.get("mode", "PAPER"),
                                "reason": reason,
                                "event_at": event_at.isoformat(),
                                "order_id": guardian_trade_id,
                                "trade_id": guardian_trade_id,
                                "reduce_only": True,
                            }, allow_nan=False),
                            pos["updated_at"],
                            event_at.isoformat(),
                        ),
                    )

                # Record simulation event if table exists
                has_sim_evt = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulation_events'"
                ).fetchone()
                if has_sim_evt:
                    conn.execute(
                        "INSERT OR IGNORE INTO simulation_events (position_id, payload_json, created_at, event_identity) VALUES (?, ?, ?, ?)",
                        (identity, json.dumps({
                            "type": "EXIT",
                            "reason": reason,
                            "fill_price": fill_price,
                            "qty": qty,
                            "gross_realized": gross_realized,
                            "fee": fee,
                            "account_id": account_id,
                            "venue": pos.get("venue", "simulated"),
                            "mode": pos.get("mode", "PAPER"),
                            "position_id": identity,
                        }), pos["updated_at"], f"guardian_exit:{identity}:{getattr(bar, 'timestamp', 'unknown')}:{reason}"),
                    )
                conn.commit()

                exits_executed.append({
                    "position_id": identity,
                    "symbol": symbol,
                    "reason": reason,
                    "quantity": qty,
                    "price": fill_price,
                    "realized_pnl": net_realized,
                    "gross_realized": gross_realized,
                    "status": new_status,
                })

            if bar_time is not None and not marker_persisted:
                # No trailing update or exit needed to persist the watermark;
                # use the same position CAS so a concurrent owner wins
                # cleanly instead of having its payload overwritten.
                marker_version = base_version
                pos["position_version"] = marker_version + 1
                marked = conn.execute(
                    """UPDATE simulated_positions
                       SET payload_json=?, updated_at=?, position_version=?, protection_status=?
                       WHERE position_id=? AND status IN ('OPEN','PARTIALLY_CLOSED')
                         AND position_version=? AND legacy_unverified=0""",
                    (json.dumps(pos, allow_nan=False), datetime.now(timezone.utc).isoformat(), marker_version + 1, pos.get("protection_status", "ACTIVE"), identity, marker_version),
                )
                if marked.rowcount == 1:
                    marker_persisted = True

        conn.commit()
        if any(
            not item.get("remote")
            and item.get("status") in {"CLOSED", "PARTIALLY_CLOSED"}
            for item in exits_executed
        ):
            self._mark_recovered()
        return exits_executed

    @staticmethod
    def _bar_timestamp(bar: Any) -> str:
        value = getattr(bar, "timestamp", None)
        if isinstance(value, datetime):
            point = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return point.astimezone(timezone.utc).isoformat()
        return str(value or "unknown")

    def _submit_remote_protection(
        self,
        *,
        pos: Dict[str, Any],
        symbol: str,
        quantity: float,
        reason: str,
        bar: Any,
        executable_price: Optional[float] = None,
        executable_at: Optional[datetime] = None,
        conn: sqlite3.Connection | None = None,
    ) -> Dict[str, Any]:
        """Submit a scoped reduce-only exit, or retain the fact as degraded.

        This method is intentionally the only non-PAPER path out of the
        guardian.  It never calculates a local fill price and never mutates
        the position to CLOSED.  A concrete remote fill is reconciled by the
        gateway/ledger; ACK/UNKNOWN/no-client states remain open.
        """
        gateway = self.execution_gateway
        client = None
        account_id = str(pos.get("account_id") or "")
        mode_text = str(pos.get("mode") or "TESTNET").upper()
        venue = str(pos.get("venue") or "gate").lower()
        position_id = str(pos.get("position_id") or "")
        side = str(pos.get("side") or "").upper()
        exit_side = "SELL" if side == "LONG" else "BUY" if side == "SHORT" else ""
        bar_ts = self._bar_timestamp(bar)
        base = {
            "status": "DEGRADED",
            "remote": True,
            "account_id": account_id,
            "venue": venue,
            "mode": mode_text,
            "position_id": position_id,
            "reason": reason,
            "triggered_at": bar_ts,
            "economic_fill_recorded": False,
            "protection_evidence": {
                "source": "guardian_bar_trigger",
                "status": "DEGRADED",
                "native_order_verified": False,
                "reconciliation_required": True,
            },
        }
        if conn is not None and account_id and position_id:
            try:
                pending = conn.execute(
                    """SELECT intent_id, status, execution_result_json
                       FROM order_intents
                      WHERE account_id=? AND venue=? AND mode=? AND position_id=?
                        AND reduce_only=1
                        AND status IN ('CREATED','RISK_APPROVED','ACKNOWLEDGED','SUBMITTED',
                                       'SUBMITTING','PARTIALLY_FILLED','UNKNOWN','CANCEL_PENDING')
                      ORDER BY created_at DESC LIMIT 1""",
                    (account_id, venue, mode_text, position_id),
                ).fetchone()
            except (sqlite3.OperationalError, TypeError):
                pending = None
            if pending is not None:
                try:
                    prior_receipt = json.loads(pending["execution_result_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    prior_receipt = {}
                prior_receipt = prior_receipt if isinstance(prior_receipt, dict) else {}
                prior_order_id = prior_receipt.get("order_id") or prior_receipt.get("id")
                base.update(
                    {
                        "status": "DEGRADED",
                        "error_code": "REMOTE_PROTECTION_IN_FLIGHT",
                        "message": "A scoped reduce-only protection order is still unresolved; no duplicate remote order was submitted.",
                        "receipt": {
                            "intent_id": pending["intent_id"],
                            "order_id": prior_order_id,
                            "status": pending["status"],
                            "reconciliation_required": True,
                        },
                        "protection_evidence": {
                            "source": "execution_gateway_in_flight_order",
                            "status": pending["status"],
                            "native_order_verified": False,
                            "remote_order_id": prior_order_id,
                            "reconciliation_required": True,
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    }
                )
                self._mark_degraded(base["message"])
                return base
        if gateway is None:
            base["error_code"] = "EXECUTION_ADAPTER_UNAVAILABLE"
            base["message"] = "Remote protection was triggered but no TESTNET/LIVE execution adapter is connected; position fact retained."
            self._mark_degraded(base["message"])
            return base
        if not account_id or not position_id or not exit_side or quantity <= 0:
            base["error_code"] = "REMOTE_PROTECTION_SCOPE_INVALID"
            base["message"] = "Remote protection scope is incomplete; position fact retained."
            self._mark_degraded(base["message"])
            return base

        if executable_price is None:
            base["error_code"] = "MARKET_DATA_UNAVAILABLE"
            base["message"] = "Remote protection trigger has no fresh executable quote; position fact retained."
            self._mark_degraded(base["message"])
            return base

        # This quote is only a freshness input for the gateway.  The adapter
        # owns the actual remote execution price, venue contract, fees, and
        # concrete fill evidence.  Do not inject PAPER/static amount rules
        # into TESTNET/LIVE protection; a connected adapter is the authority.
        bar_close = float(executable_price)
        now = datetime.now(timezone.utc)
        try:
            market_snapshot = {
                "symbol": symbol,
                "price": bar_close,
                "last": bar_close,
                "bid": bar_close,
                "ask": bar_close,
                "data_as_of": executable_at.isoformat() if isinstance(executable_at, datetime) else now.isoformat(),
                "received_at": now.isoformat(),
                "fresh": True,
                "freshness_status": "fresh",
            }
            intent = OrderIntent(
                intent_id=f"guardian_remote_{position_id}_{reason}_{bar_ts}",
                idempotency_key=f"guardian_remote:{account_id}:{venue}:{mode_text}:{position_id}:{reason}:{bar_ts}",
                account_id=account_id,
                mode=TradingMode(mode_text),
                instrument_id=symbol,
                side=exit_side,
                order_type="market",
                quantity=float(quantity),
                price=None,
                protection_plan=None,
                reduce_only=True,
                venue=venue,
                environment=mode_text,
                position_id=position_id,
                decision_path="STRATEGY_DRIVEN",
                candidate_id=pos.get("candidate_id"),
                cycle_id=pos.get("cycle_id"),
                strategy_id=pos.get("strategy_id"),
                strategy_version=pos.get("strategy_version"),
            )
            # Resolve the adapter from the account registry at the final
            # submission boundary.  A shared/global client may belong to a
            # different account or environment; managed Gate accounts must
            # use their own TestNet/Live credential slot instead.
            resolver = getattr(gateway, "_resolve_scoped_trader_client", None)
            if callable(resolver):
                client = resolver(intent, None)
            else:
                client = getattr(gateway, "trader_client", None)
                if client is None:
                    client = getattr(self.store, "_trader_client", None) or getattr(self.store, "trader_client", None)
            if client is None:
                base["error_code"] = "EXECUTION_ADAPTER_UNAVAILABLE"
                base["message"] = "Remote protection was triggered but no scoped TESTNET/LIVE execution adapter is connected; position fact retained."
                self._mark_degraded(base["message"])
                return base
            receipt = gateway.submit_intent(intent, trader_client=client, market_snapshot=market_snapshot)
            status = str(receipt.get("status") or "UNKNOWN").upper()
            record = dict(base)
            record.update({
                "status": status,
                "receipt": receipt,
                "economic_fill_recorded": bool(receipt.get("ledger_record")),
                "protection_evidence": {
                    "source": "execution_gateway_adapter",
                    "status": status,
                    "native_order_verified": False,
                    "remote_order_id": receipt.get("order_id") or receipt.get("id"),
                    "reconciliation_required": status not in {"FILLED", "PARTIALLY_FILLED"},
                    "observed_at": now.isoformat(),
                },
            })
            if status not in {"FILLED", "PARTIALLY_FILLED"}:
                self._mark_degraded(f"Remote protection {status} requires reconciliation for {position_id}")
            return record
        except (GatewayError, ValueError, TypeError, KeyError) as exc:
            base["error_code"] = getattr(exc, "code", type(exc).__name__)
            base["message"] = f"Remote protection submission did not reconcile: {exc}"
            self._mark_degraded(base["message"])
            return base

    def _mark_degraded(self, message: str) -> None:
        with self._lock:
            self._is_degraded = True
            self._degraded_reason = str(message)[:240]

    def _mark_recovered(self) -> None:
        """Clear a transient protection-data degradation after fresh evidence."""
        with self._lock:
            self._is_degraded = False
            self._degraded_reason = None

    def _record_remote_protection_observation(
        self,
        conn: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        pos: Dict[str, Any],
        symbol: str,
        reason: str,
        bar: Any,
        result: Dict[str, Any],
    ) -> None:
        """Persist remote protection evidence without inventing economics."""
        identity = str(pos.get("position_id") or row["position_id"])
        observed_at = datetime.now(timezone.utc).isoformat()
        updated = dict(pos)
        updated["account_id"] = str(pos.get("account_id") or row["account_id"] or "")
        updated["venue"] = str(pos.get("venue") or row["venue"] or "gate").lower()
        updated["mode"] = str(pos.get("mode") or row["mode"] or "TESTNET").upper()
        status = str(result.get("status") or "DEGRADED").upper()
        # A concrete gateway fill may already have closed the row.  Only an
        # still-open row can receive a non-economic observation update.
        current = conn.execute(
            "SELECT status FROM simulated_positions WHERE position_id=?",
            (identity,),
        ).fetchone()
        if current is not None and str(current[0]).upper() in {"OPEN", "PARTIALLY_CLOSED"}:
            updated["protection_status"] = "ACTIVE" if status == "ACTIVE" else "DEGRADED"
            updated["protected"] = updated["protection_status"] == "ACTIVE"
            updated["protection_last_observed_at"] = observed_at
            updated["protection_last_reason"] = reason
            updated["protection_evidence"] = result.get("protection_evidence") or {
                "source": "position_guardian",
                "status": "DEGRADED",
            }
            conn.execute(
                """UPDATE simulated_positions
                   SET payload_json=?, updated_at=?, protection_status=?
                   WHERE position_id=? AND status IN ('OPEN','PARTIALLY_CLOSED')""",
                (json.dumps(updated, allow_nan=False), observed_at, updated["protection_status"], identity),
            )
        has_events = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulation_events'"
        ).fetchone()
        if has_events:
            event_identity = f"guardian_remote:{identity}:{self._bar_timestamp(bar)}:{reason}"
            conn.execute(
                """INSERT OR IGNORE INTO simulation_events
                   (position_id, payload_json, created_at, event_identity)
                   VALUES (?, ?, ?, ?)""",
                (
                    identity,
                    json.dumps({
                        "type": "REMOTE_PROTECTION_OBSERVATION",
                        "account_id": updated["account_id"],
                        "venue": updated["venue"],
                        "mode": updated["mode"],
                        "position_id": identity,
                        "symbol": symbol,
                        "reason": reason,
                        "status": status,
                        "result": result,
                        "economic_fill_recorded": bool(result.get("economic_fill_recorded")),
                        "observed_at": observed_at,
                    }, allow_nan=False),
                    observed_at,
                    event_identity,
                ),
            )

    def get_health(self) -> Dict[str, Any]:
        with self._connection_scope() as conn:
            return self._get_health_with_conn(conn)

    def _get_health_with_conn(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """Query independent Guardian health status (R05, N03)."""
        now = datetime.now(timezone.utc)
        with self._lock:
            last_hb = self._last_heartbeat
            is_active = self._active and self._worker_thread is not None and self._worker_thread.is_alive()

        if not is_active:
            status = "STOPPED"
            degraded_reason = "Guardian background thread is not running"
        elif last_hb is None or (now - last_hb).total_seconds() > self.degraded_timeout:
            status = "DEGRADED"
            degraded_reason = f"Heartbeat expired (last: {last_hb.isoformat() if last_hb else 'never'})"
        elif self._is_degraded:
            status = "DEGRADED"
            degraded_reason = self._degraded_reason
        else:
            status = "HEALTHY"
            degraded_reason = None

        # Query open positions count
        has_pos_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulated_positions'"
        ).fetchone()
        open_count = 0
        unprotected_count = 0
        degraded_protection_count = 0
        if has_pos_table:
            account_id = self.account_id
            query = "SELECT payload_json, account_id, venue, mode, protection_status, legacy_unverified FROM simulated_positions WHERE status IN ('OPEN','PARTIALLY_CLOSED')"
            params: list[Any] = []
            if account_id:
                query += " AND (account_id=? OR (account_id IS NULL AND json_extract(payload_json, '$.account_id')=?))"
                params.extend([account_id, account_id])
            rows = conn.execute(query, tuple(params)).fetchall()
            expected_mode: str | None = None
            expected_venue: str | None = None
            if account_id:
                account_row = conn.execute(
                    "SELECT mode, config_json FROM accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                if account_row is not None:
                    scope = resolve_account_scope(self.store, account_id) or {}
                    if str(scope.get("account_type") or "").upper() == "GATE_TESTNET":
                        return {
                            "status": status,
                            "last_heartbeat": last_hb.isoformat() if last_hb else None,
                            "open_positions_count": 0,
                            "unprotected_count": 0,
                            "degraded_protection_count": 0,
                            "protection_scope": self.account_id,
                            "degraded_reason": degraded_reason,
                            "checked_at": now.isoformat(),
                        }
                    expected_mode = str(scope.get("mode") or account_row["mode"]).upper()
                    expected_venue = str(
                        scope.get("venue")
                        or ("simulated" if expected_mode == "PAPER" else "gate")
                    ).lower()
            for r in rows:
                if int(r["legacy_unverified"] or 0):
                    continue
                p = json.loads(r[0])
                scoped_account = r["account_id"] or p.get("account_id")
                if not scoped_account:
                    # Ownership cannot be inferred from a symbol.  Do not
                    # expose or count an unscoped legacy row as protected.
                    continue
                if account_id:
                    # The normalized columns are authoritative.  Payload
                    # values are only compatibility fallbacks for rows from
                    # before the additive schema migration.
                    row_venue = str(r["venue"] or p.get("venue") or "simulated").lower()
                    row_mode = str(r["mode"] or p.get("mode") or "PAPER").upper()
                    if scoped_account != account_id or row_mode != expected_mode or row_venue != expected_venue:
                        continue
                open_count += 1
                if not p.get("stop") or float(p.get("stop", 0.0)) <= 0:
                    unprotected_count += 1
                protection_status = str(r["protection_status"] or p.get("protection_status") or "UNKNOWN").upper()
                if protection_status not in {ProtectionStatus.ACTIVE.value, ProtectionStatus.CLOSED.value if hasattr(ProtectionStatus, "CLOSED") else "CLOSED"}:
                    degraded_protection_count += 1

        return {
            "status": status,
            "last_heartbeat": last_hb.isoformat() if last_hb else None,
            "open_positions_count": open_count,
            "unprotected_count": unprotected_count,
            "degraded_protection_count": degraded_protection_count,
            "protection_scope": self.account_id,
            "degraded_reason": degraded_reason,
            "checked_at": now.isoformat(),
        }
