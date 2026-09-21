from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.providers.gateio_provider import GatePublicProvider
from core.storage import SQLiteStore
from core.trading.candidate_scanner import CandidateScanner


def test_gate_derivatives_history_is_environment_tagged_and_timestamped():
    provider = GatePublicProvider(testnet=True)
    calls: list[tuple[str, dict[str, object]]] = []

    def request(path: str, params: dict[str, object] | None = None):
        calls.append((path, params or {}))
        if path.endswith("/funding_rate"):
            return [{"t": 1_789_854_000, "r": 0.0007}]
        if path.endswith("/contract_stats"):
            return [{"time": 1_789_854_000, "open_interest": "1234.5"}]
        raise AssertionError(f"unexpected Gate endpoint: {path}")

    provider._request = request  # type: ignore[method-assign]
    facts = provider.derivatives_history("BTCUSDT")

    assert facts["provider"] == "gate"
    assert facts["environment"] == "TESTNET_PUBLIC"
    assert facts["data_status"] == "AVAILABLE"
    assert facts["source"] == "gate_native_rest_derivatives_history"
    assert facts["funding_history"][0]["timestamp"] == 1_789_854_000_000
    assert facts["oi_history"][0]["openInterestAmount"] == 1234.5
    assert [path.rsplit("/", 1)[-1] for path, _ in calls] == ["funding_rate", "contract_stats"]


def test_funding_strategy_accepts_gate_epoch_millisecond_series(tmp_path):
    now = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "derivatives.db")
    store.initialize()

    class TestnetDerivatives:
        environment = "TESTNET_PUBLIC"

        def __init__(self):
            self.calls: list[str] = []

        def derivatives_history(self, symbol: str):
            self.calls.append(symbol)
            funding = [
                {
                    "timestamp": int((now - timedelta(hours=30 - index)).timestamp() * 1000),
                    "fundingRate": 0.0001 if index < 24 else 0.001,
                    "unit": "decimal_fraction",
                }
                for index in range(25)
            ]
            oi = [
                {"timestamp": int((now - timedelta(hours=2)).timestamp() * 1000), "openInterestAmount": 1000, "unit": "contracts"},
                {"timestamp": int((now - timedelta(hours=1)).timestamp() * 1000), "openInterestAmount": 1060, "unit": "contracts"},
            ]
            return {
                "provider": "gate",
                "environment": "TESTNET_PUBLIC",
                "data_status": "AVAILABLE",
                "source": "gate_native_rest_derivatives_history",
                "data_as_of": now.isoformat(),
                "funding_history": funding,
                "oi_history": oi,
            }

    test_provider = TestnetDerivatives()
    scanner = CandidateScanner(store, derivatives_provider_factory=lambda testnet: test_provider)

    def bars(_symbol: str, timeframe: str, *, now: datetime, limit: int):
        minutes = {"5m": 5, "15m": 15, "1h": 60, "8h": 480}[timeframe]
        boundary = now.replace(second=0, microsecond=0)
        boundary -= timedelta(minutes=boundary.minute % minutes)
        rows = []
        for index in range(80):
            bar_end = boundary - timedelta(minutes=minutes * (79 - index))
            price = 100 + index * 0.1
            rows.append({
                "bar_start": (bar_end - timedelta(minutes=minutes)).isoformat(),
                "bar_end": bar_end.isoformat(),
                "open": price - 0.05,
                "high": price + 0.1,
                "low": price - 0.1,
                "close": price,
                "volume": 1000 + index,
                "is_closed": True,
                "available_at": bar_end.isoformat(),
                "data_as_of": bar_end.isoformat(),
                "provider": "gate_native_rest",
            })
        return rows[-limit:]

    scanner._bars = bars  # type: ignore[method-assign]
    scanner.has_pending_candidate = lambda _account, _candidate: False  # type: ignore[method-assign]
    candidate = scanner.scan(
        account_id="gate_testnet",
        provider="gate",
        environment="testnet",
        symbols=("BTCUSDT",),
        now=now,
        strategy_ids=("funding_extreme",),
    )[0]

    assert test_provider.calls == ["BTCUSDT"]
    assert candidate["status"] != "UNSUPPORTED"
    assert "funding rate history missing" not in candidate["reason"].lower()
    assert candidate["context_timeframe"]["signal"] == "15m"
    assert candidate["closed_signal_bar"].endswith("03:00:00+00:00")
    with store._connect() as db:
        persisted = db.execute(
            "SELECT context_json FROM ai_strategy_candidates WHERE candidate_id=?",
            (candidate["candidate_id"],),
        ).fetchone()[0]
    assert '"derivatives_environment": "TESTNET_PUBLIC"' in persisted
    assert '"derivatives_source": "gate_native_rest_derivatives_history"' in persisted


def test_funding_scanner_fails_closed_on_cross_environment_derivatives(tmp_path):
    now = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "derivatives-mismatch.db")
    store.initialize()

    class MainnetDerivatives:
        def derivatives_history(self, _symbol: str):
            return {
                "provider": "gate",
                "environment": "LIVE_PUBLIC",
                "data_status": "AVAILABLE",
                "source": "gate_native_rest_derivatives_history",
                "funding_history": [{"timestamp": 1, "fundingRate": 1}],
                "oi_history": [{"timestamp": 1, "openInterestAmount": 2}],
            }

    scanner = CandidateScanner(store, derivatives_provider_factory=lambda _testnet: MainnetDerivatives())

    def bars(_symbol: str, timeframe: str, *, now: datetime, limit: int):
        minutes = {"5m": 5, "15m": 15, "1h": 60, "8h": 480}[timeframe]
        boundary = now.replace(second=0, microsecond=0)
        boundary -= timedelta(minutes=boundary.minute % minutes)
        rows = []
        for index in range(80):
            bar_end = boundary - timedelta(minutes=minutes * (79 - index))
            price = 100 + index * 0.1
            rows.append({
                "bar_start": (bar_end - timedelta(minutes=minutes)).isoformat(),
                "bar_end": bar_end.isoformat(),
                "open": price - 0.05,
                "high": price + 0.1,
                "low": price - 0.1,
                "close": price,
                "volume": 1000 + index,
                "is_closed": True,
                "available_at": bar_end.isoformat(),
                "data_as_of": bar_end.isoformat(),
                "provider": "gate_native_rest",
            })
        return rows[-limit:]

    scanner._bars = bars  # type: ignore[method-assign]
    scanner.has_pending_candidate = lambda _account, _candidate: False  # type: ignore[method-assign]
    candidate = scanner.scan(
        account_id="gate_testnet",
        provider="gate",
        environment="testnet",
        symbols=("BTCUSDT",),
        now=now,
        strategy_ids=("funding_extreme",),
    )[0]

    assert candidate["status"] == "UNSUPPORTED"
    assert "environment is not verified" in candidate["reason"]
    assert candidate["context_timeframe"]["signal"] == "15m"
    with store._connect() as db:
        persisted = db.execute(
            "SELECT context_json FROM ai_strategy_candidates WHERE candidate_id=?",
            (candidate["candidate_id"],),
        ).fetchone()[0]
    assert '"derivatives_source": "UNAVAILABLE"' in persisted
    assert '"derivatives_environment": "UNKNOWN"' in persisted


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("source", "proxy_guess", "source is not supported"),
        ("native_symbol", "ETH_USDT", "symbol does not match candidate"),
        ("data_as_of", "2026-09-20T02:54:00+00:00", "stale or future-dated"),
    ],
)
def test_funding_scanner_fails_closed_on_unverified_derivative_envelope(
    tmp_path, field, value, expected_reason
):
    now = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / f"unverified-{field}.db")
    store.initialize()

    class UnverifiedDerivatives:
        def derivatives_history(self, _symbol: str):
            facts = {
                "provider": "gate",
                "environment": "TESTNET_PUBLIC",
                "data_status": "AVAILABLE",
                "source": "gate_native_rest_derivatives_history",
                "data_as_of": now.isoformat(),
                "funding_history": [],
                "oi_history": [],
            }
            facts[field] = value
            return facts

    scanner = CandidateScanner(
        store,
        derivatives_provider_factory=lambda _testnet: UnverifiedDerivatives(),
    )
    scanner._bars = lambda _symbol, _timeframe, *, now, limit: [{
        "bar_start": (now - timedelta(minutes=15)).isoformat(),
        "bar_end": now.isoformat(),
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100,
        "volume": 100,
        "is_closed": True,
        "available_at": now.isoformat(),
        "provider": "gate_native_rest",
    }][-limit:]  # type: ignore[method-assign]
    scanner.has_pending_candidate = lambda _account, _candidate: False  # type: ignore[method-assign]

    candidate = scanner.scan(
        account_id="gate_testnet",
        provider="gate",
        environment="testnet",
        symbols=("BTCUSDT",),
        now=now,
        strategy_ids=("funding_extreme",),
    )[0]

    assert candidate["status"] == "UNSUPPORTED"
    assert expected_reason in candidate["reason"]
    with store._connect() as db:
        persisted = db.execute(
            "SELECT context_json FROM ai_strategy_candidates WHERE candidate_id=?",
            (candidate["candidate_id"],),
        ).fetchone()[0]
    assert '"derivatives_source": "UNAVAILABLE"' in persisted
