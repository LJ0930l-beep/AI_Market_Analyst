from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from threading import Event, RLock
import sqlite3
import pytest
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import ACTIVE_TEMPLATES, AIStrategyBook, TEMPLATES
from core.trading.strategy_execution import normalize_execution
from core.trading.strategy_schedule import StrategySchedule, aligned_at
from core.trading.market_universe import MarketUniverse
from core.trading.autonomous_strategy import strategy_frames
from core.trading.ai_session_coordinator import (
    AISessionCoordinator,
    MAX_CANDIDATE_REFILL_ATTEMPTS,
    _candidate_has_recent_closed_volume,
    _candidate_has_required_technical_history,
    _candidate_refresh_plan,
    _candidate_snapshot_is_executable,
    _compact_decision_experience,
    _deep_scan_symbol_limit,
    _select_bounded_scan_symbols,
)
from core.trading.candidate_scanner import CandidateScanner
from core.trading.institutional_schema import ensure_institutional_trader_schema
from core.trading.execution_gateway import TradingMode


def test_deep_scan_breadth_stays_bounded_after_repeated_waits():
    assert [_deep_scan_symbol_limit(count) for count in (0, 5, 6, 11, 12, 50)] == [2, 2, 3, 3, 3, 3]


def test_candidate_refresh_plan_keeps_normal_scan_small_and_refills_with_a_budget():
    managed, initial, refill, target = _candidate_refresh_plan(
        ["OWNEDUSDT", "BADUSDT", "SECONDUSDT"],
        ["OWNEDUSDT", "BADUSDT", "SECONDUSDT", *[f"ALT{i}USDT" for i in range(10)]],
        ["OWNEDUSDT"], limit=3,
    )

    assert managed == ["OWNEDUSDT"]
    assert initial == ["BADUSDT", "SECONDUSDT"]
    assert len(initial) == target - len(managed) == 2
    assert len(refill) == MAX_CANDIDATE_REFILL_ATTEMPTS == 4
    assert refill == ["ALT0USDT", "ALT1USDT", "ALT2USDT", "ALT3USDT"]


def test_original_four_templates_keep_cadence_and_price_action_is_an_additive_profile():
    original_ids = {'aggressive_impulse', 'aggressive_breakout', 'conservative_pullback', 'conservative_defense'}
    assert original_ids <= {item['id'] for item in TEMPLATES}
    assert {item['id'] for item in ACTIVE_TEMPLATES} == {'price_action_structure'}
    assert not original_ids.intersection(item['id'] for item in ACTIVE_TEMPLATES)
    price_action = ACTIVE_TEMPLATES[0]
    assert price_action['style'] == 'PRICE_ACTION'
    assert price_action['scan_interval_minutes'] == 15
    assert price_action['profile']['signal_timeframe'] == '15m'
    assert price_action['profile']['context_timeframes'] == ['1h', '4h']
    assert strategy_frames(5) == (('5m',5),('15m',15),('1h',60))


def test_schedule_boundaries_and_restart_dedup_are_account_scoped(tmp_path):
    store = SQLiteStore(tmp_path/'schedule.db'); store.initialize()
    point = datetime(2026,9,13,8,4,30,tzinfo=timezone.utc)
    assert aligned_at(point,5,next_slot=True).minute == 5
    assert aligned_at(point,15,next_slot=True).minute == 15
    boundary = aligned_at(point,5,next_slot=True)
    assert StrategySchedule(store).claim('a',boundary,1)
    assert not StrategySchedule(store).claim('a',boundary,2)
    assert StrategySchedule(store).claim('b',boundary,1)
    assert StrategySchedule(store).claim('a',boundary+timedelta(minutes=5),2)


def test_worker_waits_for_strategy_boundary_before_calling_model(tmp_path):
    store = SQLiteStore(tmp_path/'worker.db'); store.initialize()
    book = AIStrategyBook(store); active=book.active('a')
    book.save('a', name=active['name'], sections=active['sections'], expected_revision=0,
              execution={**active['execution'],'scan_interval_minutes':5})
    worker=object.__new__(AISessionCoordinator)
    worker.store=store; worker._account_id='a'; worker._schedule=StrategySchedule(store)
    worker._stop_event=Event(); worker._lock=__import__('threading').RLock()
    point=[datetime(2026,9,13,8,4,30,tzinfo=timezone.utc)]; worker.clock=lambda:point[0]
    waits=[]; calls=[]
    def wait(seconds):
        waits.append(seconds); point[0]+=timedelta(seconds=seconds); return False
    worker._wake_event=SimpleNamespace(wait=wait,clear=lambda:None)
    def run(**kwargs):
        calls.append(kwargs['scheduled_at']); worker._stop_event.set()
    worker.run_cycle_once=run
    worker._loop()
    # The default template is a 15m profile; a stale 5m execution value must
    # not make the worker scan or build 5m evidence for that strategy.
    assert waits == [630]
    assert calls == [datetime(2026,9,13,8,15,tzinfo=timezone.utc)]


def test_aggressive_five_minute_template_drives_coordinator_cadence(tmp_path):
    store = SQLiteStore(tmp_path/'five-minute-worker.db'); store.initialize()
    book = AIStrategyBook(store)
    active = book.active('a')
    impulse = next(item for item in TEMPLATES if item['id'] == 'aggressive_impulse')
    with pytest.raises(ValueError, match='STRATEGY_TEMPLATE_RETIRED'):
        book.save(
            'a', name=impulse['name'], sections=impulse['sections'],
            expected_revision=active['revision'], template_id='aggressive_impulse',
            execution=impulse['execution_defaults'],
        )
    worker = object.__new__(AISessionCoordinator)
    worker.store = store
    assert book.active('a')['template_id'] == 'price_action_structure'
    assert worker._strategy_scan_minutes('a') == 15
    point = datetime(2026,9,13,8,4,30,tzinfo=timezone.utc)
    assert worker._next_aligned_scan(point, worker._strategy_scan_minutes('a')) == datetime(2026,9,13,8,15,tzinfo=timezone.utc)


def test_active_strategy_profile_is_the_prompt_and_scanner_contract(tmp_path):
    from core.trading.autonomous_strategy import build_strategy_system_prompt

    store = SQLiteStore(tmp_path / 'active-strategy-contract.db'); store.initialize()
    book = AIStrategyBook(store)
    active = book.active('a')
    coordinator = _coordinator_for_symbols(store)
    coordinator._strategy_book = book

    assert coordinator._strategy_scan_minutes('a') == 15
    signal, context, strategies = coordinator._strategy_scan_contract(active)
    assert signal == active['profile']['signal_timeframe'] == '15m'
    assert context == ('5m', '1h', '4h')
    assert strategies == tuple(active['profile']['candidate_strategy_ids'])
    prompt = build_strategy_system_prompt(active)
    assert active['name'] in prompt
    assert 'signal_timeframe":"15m"' in prompt
    compact_prompt = build_strategy_system_prompt(active, context_length=8192)
    assert active['sections']['entry_standards'] in compact_prompt
    assert active['sections']['decision_process'] in compact_prompt
    assert active['sections']['custom_prompt'] in compact_prompt
    assert 'LIMIT' in compact_prompt
    assert active['profile']['strategy_id'] == 'price_action_structure'


def test_five_minute_profile_drives_ai_technical_context(monkeypatch):
    import core.trading.ai_session_coordinator as coordinator_module

    worker = object.__new__(AISessionCoordinator)
    worker.store = object()
    worker.ledger = SimpleNamespace(get_open_positions=lambda *_args, **_kwargs: [])
    worker.gateway = SimpleNamespace(runtime_lease_holder_id=None, runtime_fencing_token=None)
    seen = []
    monkeypatch.setattr(coordinator_module, "resolve_account_scope", lambda *_args: {"environment": "testnet"})
    monkeypatch.setattr(coordinator_module, "memory_for_prompt", lambda *_args: [])
    monkeypatch.setattr(
        coordinator_module,
        "technical_context",
        lambda _store, _symbols, _now, **kwargs: seen.append(kwargs.get("interval", 15)) or {},
    )

    worker._build_context(
        cycle_id="five-minute-context",
        now=datetime(2026, 9, 20, 2, 15, tzinfo=timezone.utc),
        session_id="session",
        generation=1,
        account_id="gate_testnet",
        mode=TradingMode.TESTNET,
        venue="gate",
        authorization=SimpleNamespace(limits={}),
        snapshots={"BTCUSDT": {"price": 100}},
        strategy_instructions={"profile": {"signal_timeframe": "5m"}},
    )

    assert seen == [5]


def test_nofx_verified_gate_derivatives_reach_technical_context(tmp_path, monkeypatch):
    import core.trading.ai_session_coordinator as coordinator_module

    store = SQLiteStore(tmp_path / 'nofx-verified-derivatives.db')
    store.initialize()
    now = datetime(2026, 9, 21, 8, 15, tzinfo=timezone.utc)
    store.save_gate_open_interest(
        'BTCUSDT', 'BTC_USDT',
        [{'timestamp': int(now.timestamp() * 1000), 'openInterestAmount': 1250}],
        provider='gate', environment='TESTNET', now=now,
    )
    store.save_gate_funding(
        'BTCUSDT', 'BTC_USDT',
        {'history': [{'timestamp': int(now.timestamp() * 1000), 'fundingRate': 0.0003}]},
        provider='gate', environment='TESTNET', now=now,
    )
    worker = object.__new__(AISessionCoordinator)
    worker.store = store
    worker.ledger = SimpleNamespace(get_open_positions=lambda *_args, **_kwargs: [])
    worker.gateway = SimpleNamespace(runtime_lease_holder_id=None, runtime_fencing_token=None)
    worker._evaluate_cycle_dynamic_risk = lambda *_args: {}
    seen = {}
    monkeypatch.setattr(coordinator_module, 'resolve_account_scope', lambda *_args: {'environment': 'testnet'})
    monkeypatch.setattr(coordinator_module, 'memory_for_prompt', lambda *_args: [])
    monkeypatch.setattr(
        coordinator_module,
        'technical_context',
        lambda _store, _symbols, _now, **kwargs: seen.update(kwargs) or {},
    )
    runtime = {
        'version': 1,
        'source': 'NOFX_IMPORT',
        'signal_timeframe': '5m',
        'context_timeframes': ['15m'],
        'candidate_sources': ['gate_active_usdt_perpetuals'],
        'excluded_symbols': [],
        'unsupported_sources': [],
        'indicators': {
            'raw_klines': {'enabled': True}, 'ema': {'enabled': False, 'periods': [20, 50]},
            'macd': {'enabled': False}, 'rsi': {'enabled': False, 'periods': [7, 14]},
            'atr': {'enabled': False, 'periods': [14]}, 'bollinger': {'enabled': False, 'periods': [20]},
            'volume': {'enabled': False},
            'open_interest': {'enabled': True, 'status': 'CONFIGURED'},
            'funding_rate': {'enabled': True, 'status': 'CONFIGURED'},
        },
    }

    worker._build_context(
        cycle_id='nofx-derivatives', now=now, session_id='session', generation=1,
        account_id='gate_testnet', mode=TradingMode.TESTNET, venue='gate',
        authorization=SimpleNamespace(limits={}), snapshots={'BTCUSDT': {'price': 100}},
        candidates=[], strategy_instructions={'profile': {'signal_timeframe': '5m'}, 'nofx_runtime': runtime},
    )

    derivatives = seen['verified_derivatives']['BTCUSDT']
    assert seen['timeframes'] == ['5m', '15m']
    assert seen['nofx_indicators']['enable_oi'] is True
    assert derivatives['open_interest']['verified'] is True
    assert derivatives['open_interest']['value'] == 1250
    assert derivatives['funding_rate']['verified'] is True
    assert derivatives['funding_rate']['value'] == pytest.approx(0.0003)


def test_verified_gate_derivatives_reject_stale_or_other_provider_rows(tmp_path):
    from core.trading.ai_session_coordinator import _load_verified_gate_derivatives

    store = SQLiteStore(tmp_path / 'stale-derivatives.db')
    store.initialize()
    now = datetime(2026, 9, 21, 8, 15, tzinfo=timezone.utc)
    old = now - timedelta(hours=1)
    store.save_gate_open_interest(
        'BTCUSDT', 'BTC_USDT',
        [{'timestamp': int(old.timestamp() * 1000), 'openInterestAmount': 1250}],
        provider='gate', now=old,
    )
    store.save_gate_funding(
        'BTCUSDT', 'BTC_USDT',
        {'history': [{'timestamp': int(now.timestamp() * 1000), 'fundingRate': 0.0003}]},
        provider='untrusted', now=now,
    )

    assert _load_verified_gate_derivatives(store, ('BTCUSDT',), now) == {}


def test_market_universe_adapter_receives_active_limits_positions_and_environment(tmp_path):
    store = SQLiteStore(tmp_path / 'universe-adapter.db'); store.initialize()
    coordinator = _coordinator_for_symbols(store)
    calls = []

    class Universe:
        def select(self, config, **kwargs):
            calls.append((config, kwargs))
            return {'status': 'READY', 'selected_symbols': ['BTCUSDT', 'LINKUSDT']}

    coordinator._market_universe = Universe()
    strategy = AIStrategyBook(store).active('gate_testnet')
    positions = [{'symbol': 'LINKUSDT', 'side': 'long'}]
    scheduled = datetime(2026, 9, 20, 2, 15, tzinfo=timezone.utc)
    selected = coordinator._select_market_universe(
        strategy, mode=TradingMode.TESTNET, scheduled_at=scheduled, positions=positions,
    )

    assert selected['selected_symbols'] == ['BTCUSDT', 'LINKUSDT']
    assert calls[0][0]['universe_mode'] == 'ALL'
    assert calls[0][1] == {
        'testnet': True, 'scheduled_at': scheduled, 'positions': positions, 'limit': 3, 'nofx_runtime': None,
    }


def test_local_paper_universe_does_not_call_remote_exchange(tmp_path):
    store = SQLiteStore(tmp_path / 'local-paper-universe.db'); store.initialize()
    coordinator = _coordinator_for_symbols(store)
    coordinator._mode = TradingMode.PAPER

    class RemoteUniverse:
        def select(self, *_args, **_kwargs):
            raise AssertionError('local PAPER must not query an exchange universe')

    coordinator._market_universe = RemoteUniverse()
    selected = coordinator._select_market_universe(
        AIStrategyBook(store).active('paper'),
        mode=TradingMode.PAPER,
        scheduled_at=datetime(2026, 9, 20, 2, 15, tzinfo=timezone.utc),
        account_id='paper',
    )

    assert selected['environment'] == 'PAPER'
    assert selected['selected_symbols'] == ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']


def test_candidate_schema_migration_allows_same_bar_on_different_timeframes(tmp_path):
    db = sqlite3.connect(tmp_path / 'legacy-candidates.db')
    db.execute('''CREATE TABLE ai_strategy_candidates (
        candidate_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, provider TEXT NOT NULL,
        environment TEXT NOT NULL, symbol TEXT NOT NULL, strategy_id TEXT NOT NULL,
        strategy_version TEXT NOT NULL, signal_timeframe TEXT NOT NULL DEFAULT '15m',
        closed_15m_bar TEXT NOT NULL, status TEXT NOT NULL, side TEXT,
        entry_price REAL, stop_price REAL, take_profit REAL, rule_score REAL,
        calibrated_probability REAL, calibration_sample_size INTEGER NOT NULL DEFAULT 0,
        rationale TEXT NOT NULL DEFAULT '', source_hash TEXT NOT NULL,
        conditions_json TEXT NOT NULL DEFAULT '[]', trigger_completion_pct REAL,
        entry_zone_json TEXT, invalidation TEXT, targets_json TEXT NOT NULL DEFAULT '[]',
        rr REAL, evidence_json TEXT NOT NULL DEFAULT '[]', signal_time TEXT,
        expires_at TEXT, context_timeframe TEXT, market_regime TEXT, direction_bias TEXT,
        trigger_status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(account_id, symbol, strategy_id, closed_15m_bar)
    )''')
    db.execute('''INSERT INTO ai_strategy_candidates(
        candidate_id,account_id,provider,environment,symbol,strategy_id,strategy_version,
        signal_timeframe,closed_15m_bar,status,source_hash,created_at,updated_at
    ) VALUES('legacy15','a','gate','testnet','BTCUSDT','ema_trend','v1','15m',
        '2026-09-20T02:15:00+00:00','NO_TRIGGER','hash1','now','now')''')

    ensure_institutional_trader_schema(db)
    db.execute('''INSERT INTO ai_strategy_candidates(
        candidate_id,account_id,provider,environment,symbol,strategy_id,strategy_version,
        signal_timeframe,closed_15m_bar,status,source_hash,created_at,updated_at
    ) VALUES('fresh5','a','gate','testnet','BTCUSDT','ema_trend','v1','5m',
        '2026-09-20T02:15:00+00:00','NO_TRIGGER','hash2','now','now')''')

    rows = db.execute('''SELECT candidate_id,signal_timeframe FROM ai_strategy_candidates
        ORDER BY CASE signal_timeframe WHEN '5m' THEN 0 ELSE 1 END''').fetchall()
    assert rows == [('fresh5', '5m'), ('legacy15', '15m')]
    assert db.execute(
        "SELECT context_json FROM ai_strategy_candidates WHERE candidate_id='legacy15'"
    ).fetchone()[0] == '{}'
    unique_columns = []
    for index in db.execute('PRAGMA index_list(ai_strategy_candidates)').fetchall():
        if index[2]:
            unique_columns.append(tuple(row[2] for row in db.execute(f'PRAGMA index_info("{index[1]}")')))
    assert ('account_id', 'symbol', 'strategy_id', 'signal_timeframe', 'closed_15m_bar') in unique_columns
    db.close()


def test_unverified_model_health_is_not_reported_ready():
    coordinator = object.__new__(AISessionCoordinator)
    coordinator._lock = RLock()
    coordinator._health_cache = None
    coordinator._health_checked_at = None
    coordinator.clock = lambda: datetime(2026, 9, 20, 2, 15, tzinfo=timezone.utc)

    class Provider:
        provider_name = 'offline-test-provider'
        def health(self, **_kwargs):
            return {'available': False, 'model_available': False, 'model_id': 'wrong-model'}

    coordinator.model_provider = Provider()
    health = coordinator._health()
    assert health['status'] == 'UNAVAILABLE'
    assert health['model_available'] is False


def test_aggressive_profile_drives_five_minute_candidate_evidence_and_identity(tmp_path):
    store = SQLiteStore(tmp_path / 'five-minute-candidates.db'); store.initialize()
    scanner = CandidateScanner(store)

    def bars(_symbol, timeframe, *, now, limit):
        minutes = {'5m': 5, '15m': 15, '1h': 60}[timeframe]
        end = now.replace(second=0, microsecond=0)
        end -= timedelta(minutes=end.minute % minutes) if timeframe != '1h' else timedelta(minutes=end.minute)
        rows = []
        for index in range(80):
            bar_end = end - timedelta(minutes=minutes * (79 - index))
            bar_start = bar_end - timedelta(minutes=minutes)
            price = 100 + index * 0.1
            rows.append({
                'bar_start': bar_start.isoformat(), 'bar_end': bar_end.isoformat(),
                'open': price - 0.05, 'high': price + 0.1, 'low': price - 0.1,
                'close': price, 'volume': 1000 + index, 'is_closed': True,
                'available_at': bar_end.isoformat(), 'data_as_of': bar_end.isoformat(),
                'provider': 'gate_native_rest',
            })
        return rows[-limit:]

    scanner._bars = bars  # type: ignore[method-assign]
    scanner.has_pending_candidate = lambda _account_id, _candidate_id: False  # type: ignore[method-assign]
    first = scanner.scan(
        account_id='gate_testnet', provider='gate', environment='testnet',
        symbols=('BTCUSDT',), now=datetime(2026, 9, 20, 2, 10, tzinfo=timezone.utc),
        strategy_ids=('ema_trend',), signal_timeframe_override='5m',
        context_timeframes_override=('15m', '1h'),
    )[0]
    with store._connect() as db:
        db.execute(
            "UPDATE ai_strategy_candidates SET candidate_id=? WHERE candidate_id=?",
            ('candidate_legacy_15m_identity', first['candidate_id']),
        )
    migrated = scanner.scan(
        account_id='gate_testnet', provider='gate', environment='testnet',
        symbols=('BTCUSDT',), now=datetime(2026, 9, 20, 2, 10, tzinfo=timezone.utc),
        strategy_ids=('ema_trend',), signal_timeframe_override='5m',
        context_timeframes_override=('15m', '1h'),
    )[0]
    second = scanner.scan(
        account_id='gate_testnet', provider='gate', environment='testnet',
        symbols=('BTCUSDT',), now=datetime(2026, 9, 20, 2, 15, tzinfo=timezone.utc),
        strategy_ids=('ema_trend',), signal_timeframe_override='5m',
        context_timeframes_override=('15m', '1h'),
    )[0]

    assert first['signal_timeframe'] == '5m'
    assert first['status'] != 'UNSUPPORTED'
    assert 'Signal timeframe mismatch' not in first['reason']
    if first.get('proposal'):
        assert first['proposal']['signal_timeframe'] == '5m'
    assert first['closed_signal_bar'].endswith('02:10:00+00:00')
    assert first['context_timeframe'] == {'signal': '5m', 'context': ['15m', '1h']}
    assert any(':5m:' in item for item in first['evidence_refs'])
    assert migrated['candidate_id'] == 'candidate_legacy_15m_identity'
    assert migrated['candidate_id'] != second['candidate_id']


def test_five_minute_signal_reuses_filtered_closed_5m_and_preserves_15m_context(tmp_path, monkeypatch):
    import core.trading.candidate_scanner as scanner_module

    store = SQLiteStore(tmp_path / 'signal-context.db'); store.initialize()
    scanner = CandidateScanner(store)
    scanner.has_pending_candidate = lambda _account_id, _candidate_id: False  # type: ignore[method-assign]
    now = datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc)
    steps = {'5m': timedelta(minutes=5), '15m': timedelta(minutes=15), '1h': timedelta(hours=1)}
    raw_by_timeframe = {}
    for timeframe, step in steps.items():
        minutes = int(step.total_seconds() // 60)
        end = now.replace(minute=0) if timeframe == '1h' else now
        end -= timedelta(minutes=end.minute % minutes)
        rows = []
        for index in range(80):
            bar_end = end - step * (79 - index)
            rows.append({
                'bar_start': (bar_end - step).isoformat(), 'bar_end': bar_end.isoformat(),
                'open': 100 + index * 0.1, 'high': 100.2 + index * 0.1,
                'low': 99.8 + index * 0.1, 'close': 100 + index * 0.1,
                'volume': 1000, 'is_closed': True,
                'available_at': bar_end.isoformat(), 'data_as_of': bar_end.isoformat(),
                'quality_status': 'VALID', 'provider': 'gate_native_rest',
            })
        # These rows exercise the scanner's existing closed/availability/
        # quality filters. The delayed duplicate must not shadow the valid bar.
        rows.extend([
            {**rows[-1], 'available_at': (now + timedelta(seconds=1)).isoformat()},
            {**rows[-1], 'is_closed': False},
            {**rows[-1], 'bar_start': (end).isoformat(), 'bar_end': (end + step).isoformat(),
             'available_at': (end + step).isoformat()},
            {**rows[-2], 'bar_start': (end - step * 2).isoformat(),
             'bar_end': (end - step).isoformat(), 'quality_status': 'LEGACY_UNVERIFIED'},
        ])
        raw_by_timeframe[timeframe] = rows

    reads = []
    def read_rows(_store, _symbol, timeframe, *, limit):
        reads.append(timeframe)
        return raw_by_timeframe[timeframe][-limit:]

    monkeypatch.setattr(scanner_module, 'read_trading_bars', read_rows)

    observations = []

    class ContextProbe:
        strategy_id = 'liquidity_sweep'
        signal_timeframe = '15m'
        context_timeframes = ('5m',)
        version = 'context-probe'

        def __init__(self, _params):
            self.last_status = 'NO_TRIGGER'
            self.last_reason = ''

        def evaluate(self, _symbol, bars, *, now, context):
            observations.append((self.signal_timeframe, bars, context, now))
            self.last_status = 'NO_TRIGGER'
            self.last_reason = 'captured strategy context'

    monkeypatch.setitem(scanner_module.STRATEGIES, 'liquidity_sweep', ContextProbe)

    five_minute = scanner.scan(
        account_id='gate_testnet', provider='gate', environment='testnet',
        symbols=('BTCUSDT',), now=now, strategy_ids=('liquidity_sweep',),
        signal_timeframe_override='5m', context_timeframes_override=('15m', '1h'),
    )[0]
    signal_tf, signal_bars, signal_context, captured_now = observations[-1]
    closed_5m = signal_context['closed_5m']
    assert five_minute['status'] == 'NO_TRIGGER'
    assert signal_tf == '5m' and captured_now == now
    assert closed_5m is signal_bars
    assert signal_context['context_timeframes'] == ['15m', '1h']
    assert reads.count('5m') == 1  # no second read for the required capability
    assert len(closed_5m) == 80
    assert len({bar.timestamp for bar in closed_5m}) == len(closed_5m)
    assert all(bar.is_closed and bar.bar_end <= now and bar.available_at <= now for bar in closed_5m)

    reads.clear()
    fifteen_minute = scanner.scan(
        account_id='gate_testnet', provider='gate', environment='testnet',
        symbols=('BTCUSDT',), now=now, strategy_ids=('liquidity_sweep',),
        signal_timeframe_override='15m', context_timeframes_override=('5m', '1h'),
    )[0]
    signal_tf, signal_bars, signal_context, captured_now = observations[-1]
    closed_5m = signal_context['closed_5m']
    assert fifteen_minute['status'] == 'NO_TRIGGER'
    assert signal_tf == '15m' and captured_now == now
    assert closed_5m is not signal_bars
    assert signal_context['context_timeframes'] == ['5m', '1h']
    assert reads.count('5m') == 1
    assert len(closed_5m) == 80
    assert len({bar.timestamp for bar in closed_5m}) == len(closed_5m)
    assert all(bar.is_closed and bar.bar_end <= now and bar.available_at <= now for bar in closed_5m)


def test_dynamic_universe_rotates_without_fixed_symbol_allowlist():
    symbols=['BTCUSDT','ETHUSDT']+[f'NEW{i}USDT' for i in range(16)]
    class Provider:
        def list_active_usdt_contracts(self,limit):
            assert limit is None
            return [{'symbol':s} for s in symbols]
        def list_contract_tickers(self):
            return [{'contract':s[:-4]+'_USDT','last':'1','volume_24h_quote':str(1000-i),
                     'change_percentage': str((i % 7) - 3), 'high_24h': '1.2', 'low_24h': '0.8'}
                    for i,s in enumerate(symbols)]+[{'contract':'DELISTED_USDT','last':'2','volume_24h_quote':'9000'}]
    universe=MarketUniverse(lambda testnet:Provider())
    config=normalize_execution({'symbols':[],'universe_mode':'ALL','scan_interval_minutes':5})
    point=datetime(2026,9,13,8,tzinfo=timezone.utc)
    seen=set()
    # Three symbols per cycle still cover the whole eligible set through the
    # discovery slot; allow one complete deterministic rotation.
    for i in range(20):
        snapshot=universe.select(config,testnet=True,scheduled_at=point+timedelta(minutes=5*i))
        seen.update(snapshot['selected_symbols'])
        assert len(snapshot['selected_symbols']) == 3
        assert snapshot['contract_count'] == len(symbols)
        assert len(snapshot['candidate_metrics']) == 3
        assert len(snapshot['candidate_pool_symbols']) <= 12
        assert set(snapshot['selected_symbols']).issubset(snapshot['candidate_pool_symbols'])
        assert len(snapshot['candidate_pool_metrics']) >= len(snapshot['candidate_metrics'])
        assert snapshot['selection'] == 'positions_then_liquidity_up_momentum_down_momentum_range_and_rotation'
    assert seen == set(symbols)
    custom={**config,'universe_mode':'CUSTOM','symbols':['NEW12USDT']}
    selected=universe.select(custom,testnet=True,scheduled_at=point,positions=[{'symbol':'NEW8USDT'}])
    assert selected['selected_symbols'] == ['NEW8USDT','NEW12USDT']


def test_gate_contract_fee_reaches_testnet_ai_snapshot_without_a_default(tmp_path):
    store = SQLiteStore(tmp_path / 'fee-bridge.db'); store.initialize()
    now = datetime(2026, 9, 24, 15, 20, tzinfo=timezone.utc)
    store.save_realtime_state({
        'symbol': 'BTCUSDT', 'provider': 'gate_testnet', 'price': 84300,
        'data_as_of': now.isoformat(), 'freshness_status': 'fresh',
        'market_data_environment': 'TESTNET_PUBLIC', 'slippage': 0.0004,
    }, now=now)

    class Provider:
        def list_active_usdt_contracts(self, _limit):
            return [{'symbol': 'BTCUSDT', 'taker_fee_rate': '0.00075',
                     'source': 'gate_native_rest_contracts'}]

        def list_contract_tickers(self):
            return [{'contract': 'BTC_USDT', 'last': '84300',
                     'volume_24h_quote': '1000000'}]

    universe = MarketUniverse(lambda _testnet: Provider()).select(
        normalize_execution({'symbols': [], 'universe_mode': 'ALL'}),
        testnet=True, scheduled_at=now, limit=1,
    )
    coordinator = _coordinator_for_symbols(store)
    snapshots = coordinator._market_snapshots(
        ('BTCUSDT',), now=now, timeframe='5m', universe_snapshot=universe,
    )
    assert universe['candidate_metrics'][0]['taker_fee_rate'] == 0.00075
    assert snapshots['BTCUSDT']['fee_rate'] == 0.00075
    assert snapshots['BTCUSDT']['fee_rate_source'] == 'gate_native_rest_contracts'

    missing = {**universe, 'candidate_metrics': [{
        **universe['candidate_metrics'][0], 'taker_fee_rate': None,
    }]}
    snapshots_without_fee = coordinator._market_snapshots(
        ('BTCUSDT',), now=now, timeframe='5m', universe_snapshot=missing,
    )
    assert snapshots_without_fee['BTCUSDT'].get('fee_rate') is None


def test_ai_input_refresh_uses_one_account_environment_provider(monkeypatch):
    import core.strategy_monitoring as monitoring_module
    import core.news_refresh as news_module

    providers = []
    calls = []

    class Provider:
        def __init__(self, *, testnet):
            self.testnet = testnet
            self.environment = 'TESTNET_PUBLIC' if testnet else 'LIVE_PUBLIC'
            providers.append(self)

    service = object.__new__(monitoring_module.StrategyMonitoringService)
    service.store = object()
    service.gate = object()
    service.run = lambda **kwargs: calls.append(('run', kwargs['gate_provider']))
    service.refresh_derivatives = lambda _symbols, **kwargs: calls.append(('derivatives', kwargs['gate_provider']))
    service.refresh_execution_quotes = lambda _symbols, **kwargs: calls.append(('quotes', kwargs['gate_provider']))
    monkeypatch.setattr(monitoring_module, 'GatePublicProvider', Provider)
    monkeypatch.setattr(news_module, 'refresh_public_news', lambda *_args, **_kwargs: None)

    service.refresh_ai_inputs(('BTCUSDT',), scan_interval_minutes=5, testnet=True)
    assert len(providers) == 1
    assert providers[0].environment == 'TESTNET_PUBLIC'
    assert [name for name, _provider in calls] == ['run', 'derivatives', 'quotes']
    assert all(provider is providers[0] for _name, provider in calls)
    assert service.gate is not providers[0]


def test_testnet_ai_snapshot_reloads_if_background_live_quote_overwrites_it(tmp_path, monkeypatch):
    import core.providers.gateio_provider as gate_module

    store = SQLiteStore(tmp_path / 'cross-environment.db'); store.initialize()
    now = datetime(2026, 9, 24, 15, 45, tzinfo=timezone.utc)
    store.save_realtime_state({
        'symbol': 'BTCUSDT', 'provider': 'gate_public_swap', 'price': 90,
        'data_as_of': now.isoformat(), 'freshness_status': 'fresh',
        'market_data_environment': 'LIVE_PUBLIC', 'slippage': 0.001,
    }, now=now)

    class TestNetProvider:
        def __init__(self, *, testnet):
            assert testnet is True

        def market(self, _symbol):
            return {'id': 'BTC_USDT', 'contractSize': 0.0001,
                    'precision': {'amount': 1, 'price': 0.1},
                    'taker': 0.00075}

        def _native_ticker(self, _symbol):
            return {'last': '100'}

        def order_book(self, _symbol, *, limit):
            assert limit == 20
            return {'bids': [{'p': '99.9', 's': '100'}],
                    'asks': [{'p': '100.1', 's': '100'}], 'source': 'gate_testnet_book'}

    monkeypatch.setattr(gate_module, 'GatePublicProvider', TestNetProvider)
    coordinator = _coordinator_for_symbols(store)
    coordinator.clock = lambda: now + timedelta(seconds=30)
    snapshot = coordinator._market_snapshots(('BTCUSDT',), now=now)['BTCUSDT']
    assert snapshot['price'] == 100
    assert snapshot['market_data_environment'] == 'TESTNET_PUBLIC'
    assert snapshot['slippage'] == pytest.approx(0.001)
    assert snapshot['source'] == 'gate_testnet_native_rest_ticker'


def test_universe_includes_both_momentum_directions_before_rotation():
    symbols = ['BTCUSDT', 'UPUSDT', 'DOWNUSDT', 'WIDEUSDT', 'OTHERUSDT']
    class Provider:
        def list_active_usdt_contracts(self, limit): return [{'symbol': symbol} for symbol in symbols]
        def list_contract_tickers(self):
            return [
                {'contract':'BTC_USDT','last':'100','volume_24h_quote':'9999','change_percentage':'0','high_24h':'101','low_24h':'99'},
                {'contract':'UP_USDT','last':'100','volume_24h_quote':'1000','change_percentage':'18','high_24h':'119','low_24h':'98'},
                {'contract':'DOWN_USDT','last':'100','volume_24h_quote':'900','change_percentage':'-16','high_24h':'102','low_24h':'83'},
                {'contract':'WIDE_USDT','last':'100','volume_24h_quote':'800','change_percentage':'2','high_24h':'130','low_24h':'70'},
                {'contract':'OTHER_USDT','last':'100','volume_24h_quote':'700','change_percentage':'1','high_24h':'102','low_24h':'98'},
            ]
    result = MarketUniverse(lambda _testnet: Provider()).select(
        normalize_execution({'symbols': [], 'universe_mode': 'ALL'}),
        testnet=True, scheduled_at=datetime(2026, 9, 13, 8, tzinfo=timezone.utc), limit=5,
    )
    assert result['selected_symbols'][0] == 'BTCUSDT'
    assert {'UPUSDT', 'DOWNUSDT', 'WIDEUSDT'}.issubset(result['selected_symbols'])


def test_thin_extreme_mover_cannot_occupy_the_factor_slot_each_cycle():
    symbols = ['BTCUSDT'] + [f'LIQ{i}USDT' for i in range(25)] + ['THINUSDT']

    class Provider:
        def list_active_usdt_contracts(self, _limit):
            return [{'symbol': symbol} for symbol in symbols]

        def list_contract_tickers(self):
            return [
                {'contract': symbol[:-4] + '_USDT', 'last': '1',
                 'volume_24h_quote': str(100000 - i * 1000),
                 'change_percentage': '900' if symbol == 'THINUSDT' else str(i % 5),
                 'high_24h': '1.1', 'low_24h': '0.9'}
                for i, symbol in enumerate(symbols)
            ]

    universe = MarketUniverse(lambda _testnet: Provider())
    config = normalize_execution({'symbols': [], 'universe_mode': 'ALL', 'scan_interval_minutes': 5})
    point = datetime(2026, 9, 13, 8, tzinfo=timezone.utc)
    selections = [universe.select(config, testnet=True, scheduled_at=point + timedelta(minutes=5*i))
                  for i in range(30)]
    assert all(row['selected_symbols'][1] != 'THINUSDT' for row in selections)
    assert any('THINUSDT' in row['selected_symbols'] for row in selections)


def test_zero_volume_and_unexecutable_candidates_are_replaced_but_managed_symbol_stays_first():
    now = datetime(2026, 10, 1, 16, 5, tzinfo=timezone.utc)
    end = now.replace(minute=0)

    def bar(volume, minutes_ago=0):
        closed = end - timedelta(minutes=minutes_ago)
        return {
            "bar_start": (closed - timedelta(minutes=15)).isoformat(),
            "bar_end": closed.isoformat(), "available_at": closed.isoformat(),
            "open": 100, "high": 102, "low": 99, "close": 101,
            "volume": volume, "is_closed": True, "quality_status": "VALID",
        }

    rows = {
        "ZEROUSDT": [bar(10, 15), bar(0)],
        "NOQUOTEUSDT": [bar(10)],
        "GOODUSDT": [bar(10)],
        "NEXTUSDT": [bar(8)],
    }

    class Store:
        def latest_bars(self, symbol, timeframe, *, limit, **filters):
            assert timeframe == "15m" and limit == 64
            return rows.get(symbol, [])

    market = {
        "contractSize": 0.01,
        "precision": {"amount": 1, "price": 0.1},
        "limits": {"amount": {"min": 1, "max": 100000}},
        "leverage_max": 50,
        "taker": 0.0005,
    }

    def executable_snapshot(*, include_rules=True):
        return {
            "price": 100, "fresh": True, "market": market if include_rules else {},
            "fee_rate": 0.0005, "slippage": 0.001,
            "liquidity_ok": True, "cost_evidence_status": "OBSERVED_DEPTH_ENVELOPE",
        }

    store = Store()
    bar_ready = {
        symbol: _candidate_has_recent_closed_volume(store, symbol, "15m", now)
        for symbol in rows
    }
    snapshot_ready = {
        "ZEROUSDT": _candidate_snapshot_is_executable(executable_snapshot(), remote=True),
        "NOQUOTEUSDT": _candidate_snapshot_is_executable(executable_snapshot(include_rules=False), remote=True),
        "GOODUSDT": _candidate_snapshot_is_executable(executable_snapshot(), remote=True),
        "NEXTUSDT": _candidate_snapshot_is_executable(executable_snapshot(), remote=True),
    }
    selected = _select_bounded_scan_symbols(
        ["ZEROUSDT", "NOQUOTEUSDT"],
        ["ZEROUSDT", "NOQUOTEUSDT", "GOODUSDT", "NEXTUSDT"],
        ["OWNEDUSDT"], limit=3,
        is_candidate_ready=lambda symbol: bar_ready.get(symbol, False) and snapshot_ready.get(symbol, False),
    )

    assert bar_ready["ZEROUSDT"] is False
    assert snapshot_ready["NOQUOTEUSDT"] is False
    assert snapshot_ready["GOODUSDT"] is True
    assert selected == ["OWNEDUSDT", "GOODUSDT", "NEXTUSDT"]


def _closed_frame_bars(now, timeframe, count=32, *, gap_index=None, latest_offset_minutes=0):
    minutes = {"5m": 5, "15m": 15, "1h": 60}[timeframe]
    aligned = now.replace(second=0, microsecond=0)
    aligned -= timedelta(minutes=aligned.minute % minutes)
    latest_end = aligned + timedelta(minutes=latest_offset_minutes)
    total = count + (1 if gap_index is not None else 0)
    rows = []
    for index in range(total):
        if index == gap_index:
            continue
        end = latest_end - timedelta(minutes=minutes * (total - index - 1))
        rows.append({
            "bar_start": (end - timedelta(minutes=minutes)).isoformat(),
            "bar_end": end.isoformat(),
            "available_at": end.isoformat(),
            "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
            "volume": 10.0, "is_closed": True, "quality_status": "VALID",
            "venue": "gate", "market_type": "perpetual", "price_type": "last",
            "provider": "gate", "source": "gate_native_rest:last",
        })
    return rows


def test_recent_closed_volume_requires_exact_duration_and_explicit_real_bar_flags():
    now = datetime(2026, 10, 1, 16, 5, tzinfo=timezone.utc)
    rows = _closed_frame_bars(now, "15m", count=1)

    class Store:
        def latest_bars(self, _symbol, _timeframe, *, limit, **_filters):
            return rows[-limit:]

    store = Store()
    assert _candidate_has_recent_closed_volume(store, "PAIRUSDT", "15m", now)

    row = rows[0]
    row["bar_start"] = (datetime.fromisoformat(row["bar_end"]) - timedelta(minutes=1)).isoformat()
    assert not _candidate_has_recent_closed_volume(store, "PAIRUSDT", "15m", now)

    row["bar_start"] = (datetime.fromisoformat(row["bar_end"]) - timedelta(minutes=15)).isoformat()
    row["is_closed"] = "false"
    assert not _candidate_has_recent_closed_volume(store, "PAIRUSDT", "15m", now)

    row["is_closed"] = 1  # SQLite's integer representation of TRUE.
    row["synthetic"] = True
    assert not _candidate_has_recent_closed_volume(store, "PAIRUSDT", "15m", now)

    row["synthetic"] = "false"
    assert not _candidate_has_recent_closed_volume(store, "PAIRUSDT", "15m", now)

    row["synthetic"] = 0  # SQLite's integer representation of FALSE.
    assert _candidate_has_recent_closed_volume(store, "PAIRUSDT", "15m", now)


class _TechnicalBarsStore:
    def __init__(self, frames):
        self.frames = frames

    def latest_bars(self, symbol, timeframe, *, limit, **_filters):
        return list(self.frames.get((symbol, timeframe), []))[-limit:]


def test_unready_top_ranked_candidate_refills_from_next_usable_technical_symbol():
    now = datetime(2026, 10, 1, 16, 5, tzinfo=timezone.utc)
    store = _TechnicalBarsStore({
        ("TOPUSDT", "15m"): _closed_frame_bars(now, "15m"),
        # The latest signal bar is fresh, but the required 1h context is empty.
        ("ALTUSDT", "15m"): _closed_frame_bars(now, "15m"),
        ("ALTUSDT", "1h"): _closed_frame_bars(now, "1h"),
    })
    executable_market = {
        "contractSize": 0.01,
        "precision": {"amount": 1, "price": 0.1},
        "limits": {"amount": {"min": 1, "max": 100000}},
        "leverage_max": 50,
        "taker": 0.0005,
    }
    snapshots = {
        symbol: {
            "price": 100, "fresh": True, "market": executable_market,
            "fee_rate": 0.0005, "slippage": 0.001,
            "liquidity_ok": True, "cost_evidence_status": "OBSERVED_DEPTH_ENVELOPE",
        }
        for symbol in ("TOPUSDT", "ALTUSDT")
    }
    technical = {
        symbol: _candidate_has_required_technical_history(
            store, symbol, now, signal_timeframe="15m", context_timeframes=("1h",),
        )
        for symbol in ("TOPUSDT", "ALTUSDT")
    }

    assert _candidate_has_recent_closed_volume(store, "TOPUSDT", "15m", now)
    assert _candidate_snapshot_is_executable(snapshots["TOPUSDT"], remote=True)
    assert technical["TOPUSDT"] == (False, ("1h=INSUFFICIENT_OR_STALE(0)",))
    assert technical["ALTUSDT"] == (True, ())

    selected = _select_bounded_scan_symbols(
        ["TOPUSDT"], ["TOPUSDT", "ALTUSDT"], ["OWNEDUSDT"], limit=2,
        is_candidate_ready=lambda symbol: (
            _candidate_has_recent_closed_volume(store, symbol, "15m", now)
            and technical.get(symbol, (False, ()))[0]
            and _candidate_snapshot_is_executable(snapshots.get(symbol), remote=True)
        ),
    )
    assert selected == ["OWNEDUSDT", "ALTUSDT"]


def test_technical_readiness_uses_32_fresh_contiguous_closed_bars():
    now = datetime(2026, 10, 1, 16, 5, tzinfo=timezone.utc)
    ready = {
        ("PAIRUSDT", "15m"): _closed_frame_bars(now, "15m"),
        ("PAIRUSDT", "1h"): _closed_frame_bars(now, "1h"),
    }
    assert _candidate_has_required_technical_history(
        _TechnicalBarsStore(ready), "PAIRUSDT", now,
        signal_timeframe="15m", context_timeframes=("1h",),
    ) == (True, ())

    insufficient = dict(ready)
    insufficient[("PAIRUSDT", "1h")] = _closed_frame_bars(now, "1h", count=31)
    assert _candidate_has_required_technical_history(
        _TechnicalBarsStore(insufficient), "PAIRUSDT", now,
        signal_timeframe="15m", context_timeframes=("1h",),
    )[1] == ("1h=INSUFFICIENT_OR_STALE(31)",)

    discontinuous = dict(ready)
    discontinuous[("PAIRUSDT", "15m")] = _closed_frame_bars(now, "15m", gap_index=15)
    assert _candidate_has_required_technical_history(
        _TechnicalBarsStore(discontinuous), "PAIRUSDT", now,
        signal_timeframe="15m", context_timeframes=("1h",),
    )[1] == ("15m=INSUFFICIENT_OR_STALE(32)",)

    stale = dict(ready)
    stale[("PAIRUSDT", "1h")] = _closed_frame_bars(now, "1h", latest_offset_minutes=-120)
    assert _candidate_has_required_technical_history(
        _TechnicalBarsStore(stale), "PAIRUSDT", now,
        signal_timeframe="15m", context_timeframes=("1h",),
    )[1] == ("1h=INSUFFICIENT_OR_STALE(32)",)

    future_dated = dict(ready)
    future_dated[("PAIRUSDT", "15m")] = _closed_frame_bars(now, "15m", latest_offset_minutes=15)
    assert _candidate_has_required_technical_history(
        _TechnicalBarsStore(future_dated), "PAIRUSDT", now,
        signal_timeframe="15m", context_timeframes=("1h",),
    )[1] == ("15m=INSUFFICIENT_OR_STALE(31)",)


def test_technical_readiness_honors_five_minute_signal_and_configured_context_frames():
    now = datetime(2026, 10, 1, 16, 5, tzinfo=timezone.utc)
    store = _TechnicalBarsStore({
        ("FASTUSDT", "5m"): _closed_frame_bars(now, "5m"),
        ("FASTUSDT", "1h"): _closed_frame_bars(now, "1h"),
        # Explicit 1h context means the unrelated 15m frame is not required.
    })
    assert _candidate_has_required_technical_history(
        store, "FASTUSDT", now, signal_timeframe="5m", context_timeframes=("1h",),
    ) == (True, ())
    assert _candidate_has_required_technical_history(
        store, "FASTUSDT", now, signal_timeframe="5m", context_timeframes=None,
    )[1] == ("15m=INSUFFICIENT_OR_STALE(0)",)


def test_all_bad_refill_is_bounded_and_unready_managed_or_pending_symbols_survive():
    candidates = [f"BAD{index}USDT" for index in range(10)]
    managed, initial, refill, target = _candidate_refresh_plan(
        candidates[:2], candidates, ["OWNEDUSDT", "PENDINGUSDT"], limit=3,
    )
    assert managed == ["OWNEDUSDT", "PENDINGUSDT"]
    assert len(initial) == target - len(managed) == 1
    assert len(refill) == MAX_CANDIDATE_REFILL_ATTEMPTS

    attempted = [*initial, *refill]
    ready = {symbol: False for symbol in attempted}
    selected = _select_bounded_scan_symbols(
        initial, candidates, managed, limit=3,
        is_candidate_ready=lambda symbol: ready.get(symbol, False),
    )
    assert len(attempted) == 1 + MAX_CANDIDATE_REFILL_ATTEMPTS
    assert selected == managed
    assert _candidate_snapshot_is_executable(None, remote=True) is False


def test_wait_history_is_not_reintroduced_as_a_current_decision_anchor():
    recent, _experience = _compact_decision_experience([
        {"action": "WAIT", "symbol": "ETHUSDT", "reason": "上一轮 WAIT 仍有效", "cycle_status": "WAITING"},
        {"action": "OPEN_LONG", "symbol": "BTCUSDT", "reason": "prior actionable decision", "cycle_status": "REJECTED"},
    ])
    assert [row["action"] for row in recent] == ["OPEN_LONG"]
    assert all(row.get("action") not in {"WAIT", "HOLD"} for row in recent)


def _coordinator_for_symbols(store):
    coordinator = object.__new__(AISessionCoordinator)
    coordinator.store = store
    coordinator._account_id = 'gate_testnet'
    coordinator._mode = TradingMode.TESTNET
    coordinator._lock = RLock()
    coordinator.clock = lambda: datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
    return coordinator


def test_allowed_symbols_is_authorization_scoped_and_falls_back_to_majors(tmp_path):
    """`_allowed_symbols` 只认账户授权 + 启用的监控策略，从不擅自扩散到全交易所。

    这是有意的收敛：`universe_mode='ALL'` 的动态发现由 `MarketUniverse` 承担，
    但只有当调用方显式取用其 `selected_symbols` 时才生效；`_allowed_symbols`
    本身永远受 `authorization.allowed_instruments` 约束，且上限为 MAX_CYCLE_SYMBOLS。
    """
    store = SQLiteStore(tmp_path / 'coordinator-symbols.db'); store.initialize()
    coordinator = _coordinator_for_symbols(store)

    # 1) 无授权（未限制账户）且无监控策略 -> 回落到三大主流币，而不是全市场
    assert coordinator._allowed_symbols(None) == ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')

    # 2) 启用一条监控策略 -> 该标的进入扫描面
    from core.monitoring import MonitoringPolicy
    store.upsert_monitoring_policy({**MonitoringPolicy.defaults('LINKUSDT').to_dict(), 'enabled': True})
    assert 'LINKUSDT' in coordinator._allowed_symbols(None)

    # 3) 带授权的账户：授权之外的监控策略不得借策略扩张标的范围
    restricted = SimpleNamespace(allowed_instruments=['ETHUSDT'])
    symbols = coordinator._allowed_symbols(restricted)
    assert symbols == ('ETHUSDT',), 'monitoring policy must not expand a restricted authorization'
    assert 'LINKUSDT' not in symbols

    # 4) 上限始终是 MAX_CYCLE_SYMBOLS
    from core.trading.ai_session_coordinator import MAX_CYCLE_SYMBOLS
    wide = SimpleNamespace(allowed_instruments=['AUSDT', 'BUSDT', 'CUSDT', 'DUSDT', 'EUSDT', 'FUSDT', 'GUSDT', 'HUSDT', 'IUSDT'])
    assert len(coordinator._allowed_symbols(wide)) == MAX_CYCLE_SYMBOLS


def test_discovery_failure_does_not_fabricate_three_major_symbols():
    class Offline:
        def list_active_usdt_contracts(self,limit): raise OSError('offline')
    with pytest.raises(OSError):
        MarketUniverse(lambda testnet:Offline()).select(normalize_execution(),testnet=True,scheduled_at=datetime.now(timezone.utc))


def test_discovered_contract_is_also_supported_by_news_refresh(tmp_path):
    from core.instruments import Instrument, AssetType, TradingHours
    from core.news_refresh import refresh_public_news
    store=SQLiteStore(tmp_path/'news.db'); store.initialize()
    store.save_instrument(Instrument(symbol='NEWUSDT', asset_type=AssetType.CRYPTO, exchange='GATE',
        currency='USDT', timezone='UTC', trading_hours=TradingHours.AROUND_THE_CLOCK, contract_type='perp'))
    calls=[]
    class News:
        provider_name='test'
        def get_events(self,instrument,limit=12): calls.append(instrument.symbol); return []
    result=refresh_public_news(store,symbols=['NEWUSDT'],provider=News())
    assert calls == ['NEWUSDT']
    assert result['results'][0]['status'] != 'INVALID_SYMBOL'
