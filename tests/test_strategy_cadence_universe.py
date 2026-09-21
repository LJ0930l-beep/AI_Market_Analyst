from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from threading import Event, RLock
import sqlite3
import pytest
from core.storage import SQLiteStore
from core.trading.ai_strategy_book import AIStrategyBook, TEMPLATES
from core.trading.strategy_execution import normalize_execution
from core.trading.strategy_schedule import StrategySchedule, aligned_at
from core.trading.market_universe import MarketUniverse
from core.trading.autonomous_strategy import strategy_frames
from core.trading.ai_session_coordinator import AISessionCoordinator
from core.trading.candidate_scanner import CandidateScanner
from core.trading.institutional_schema import ensure_institutional_trader_schema
from core.trading.execution_gateway import TradingMode


def test_four_templates_have_two_aggressive_two_defensive_and_one_five_minute():
    assert len(TEMPLATES) == 4
    assert [t['style'] for t in TEMPLATES].count('AGGRESSIVE') == 2
    assert [t['style'] for t in TEMPLATES].count('CONSERVATIVE') == 2
    assert sorted(t['scan_interval_minutes'] for t in TEMPLATES) == [5, 15, 15, 15]
    decision_text = " ".join(t['sections']['decision_process'] for t in TEMPLATES)
    assert '75~85' not in decision_text
    assert '75~82' not in decision_text
    assert all('不得' in t['sections']['decision_process'] for t in TEMPLATES)
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
    saved = book.save(
        'a', name=impulse['name'], sections=impulse['sections'],
        expected_revision=active['revision'], template_id='aggressive_impulse',
        execution=impulse['execution_defaults'],
    )
    worker = object.__new__(AISessionCoordinator)
    worker.store = store
    assert saved['execution']['scan_interval_minutes'] == 5
    assert worker._strategy_scan_minutes('a') == 5
    point = datetime(2026,9,13,8,4,30,tzinfo=timezone.utc)
    assert worker._next_aligned_scan(point, worker._strategy_scan_minutes('a')) == datetime(2026,9,13,8,5,tzinfo=timezone.utc)


def test_active_strategy_profile_is_the_prompt_and_scanner_contract(tmp_path):
    from core.trading.autonomous_strategy import build_strategy_system_prompt

    store = SQLiteStore(tmp_path / 'active-strategy-contract.db'); store.initialize()
    book = AIStrategyBook(store)
    active = book.active('a')
    impulse = next(item for item in TEMPLATES if item['id'] == 'aggressive_impulse')
    active = book.save(
        'a', name=impulse['name'], sections=impulse['sections'],
        expected_revision=active['revision'], template_id=impulse['id'],
        execution=impulse['execution_defaults'],
    )
    coordinator = _coordinator_for_symbols(store)
    coordinator._strategy_book = book

    assert coordinator._strategy_scan_minutes('a') == 5
    signal, context, strategies = coordinator._strategy_scan_contract(active)
    assert signal == '5m'
    assert context == ('15m', '1h')
    assert strategies == ('liquidity_sweep', 'ema_trend', 'bollinger_squeeze')
    prompt = build_strategy_system_prompt(active)
    assert '当前策略：闪电动量 · 5m 激进' in prompt
    assert 'signal_timeframe":"5m"' in prompt


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
        assert snapshot['selection'] == 'positions_then_liquidity_up_momentum_down_momentum_range_and_rotation'
    assert seen == set(symbols)
    custom={**config,'universe_mode':'CUSTOM','symbols':['NEW12USDT']}
    selected=universe.select(custom,testnet=True,scheduled_at=point,positions=[{'symbol':'NEW8USDT'}])
    assert selected['selected_symbols'] == ['NEW8USDT','NEW12USDT']


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
    wide = SimpleNamespace(allowed_instruments=['AUSDT', 'BUSDT', 'CUSDT', 'DUSDT', 'EUSDT', 'FUSDT'])
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
