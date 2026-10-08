"""Read-only, consistent snapshots and pooled closed-trade outcome diagnostics.

Windows reset their accounts. Pooled figures are not one continuous annual
portfolio; floating profit cannot satisfy the closed-profit criterion.
Numeric screening is not private Gate acceptance or proof of independence.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest, utc
from core.replay.research_observation import model_actions, wilson_interval
from scripts.audit_fixed_entry_budget import audit_entry
from scripts.verify_ai_template_replay import number


def audit_snapshot(snapshot, manifest, *, complete):
    # The frozen auditor uses SQLite context managers that retain handles
    # until collection. Isolate it so Windows releases every handle on exit.
    output = snapshot.parent / 'snapshot-audit.json'
    command = [sys.executable, str(Path(__file__).with_name('verify_ai_template_replay.py')),
               '--database', str(snapshot), '--manifest', str(manifest), '--output', str(output)]
    if complete:
        command.append('--require-complete')
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode or not output.exists():
        raise ValueError('OUTCOME_INDEPENDENT_LEDGER_AUDIT_FAILED')
    return json.loads(output.read_text(encoding='utf-8'))


def summarize_strategy(trades, rows, accounts, *, phase, expected_windows,
                       complete_audited, scan_interval_minutes, maximum_drawdown=None,
                       win_rate_target=.5):
    """Metrics consume individually audited windows, never average win rates."""
    if maximum_drawdown is not None and (type(maximum_drawdown) not in (int, float) or
            not math.isfinite(maximum_drawdown) or not 0 < maximum_drawdown < 1):
        raise ValueError('OUTCOME_INVALID_DRAWDOWN_LIMIT')
    identities, nets = set(), []
    clusters = defaultdict(list)
    for trade in trades:
        identity = (trade['window'], trade['position_id'])
        if identity in identities:
            raise ValueError('OUTCOME_DUPLICATE_CLOSED_POSITION')
        identities.add(identity)
        opened, closed = utc(trade['opened_at']), utc(trade['closed_at'])
        if closed < opened:
            raise ValueError('OUTCOME_INVALID_TRADE_LIFETIME')
        net = number(trade['net_pnl'])
        if net != number(trade['realized_gross_pnl']) - number(trade['fees']) + number(trade['funding_pnl']):
            raise ValueError('OUTCOME_NET_PNL_FORMULA')
        nets.append(net)
        clusters[trade['window']].append((opened, closed))
    # Count overlapping BTC/ETH lifetimes together. Nearby re-entries can be
    # the same market opportunity; this count does not prove independence.
    cluster_count = 0
    for intervals in clusters.values():
        end = None
        for opened, closed in sorted(intervals):
            if end is None or opened > end + timedelta(minutes=scan_interval_minutes):
                cluster_count += 1
                end = closed
            else:
                end = max(end, closed)
    wins, total = sum(n > 0 for n in nets), len(nets)
    interval = wilson_interval(wins, total)
    statuses = Counter(row['status'] for row in rows)
    actions = Counter(json.loads(row.get('decision_json') or '{}').get('action', 'UNKNOWN')
                      for row in rows if row['status'] == 'COMPLETED')
    proposals = sum(bool(model_actions(row) & {'OPEN_LONG', 'OPEN_SHORT'}) or
                    json.loads(row.get('decision_json') or '{}').get('action') in {'OPEN_LONG', 'OPEN_SHORT'}
                    for row in rows)
    max_wait = 0
    for window in expected_windows:
        streak = 0
        for row in sorted((r for r in rows if r['window'] == window), key=lambda r: utc(r['as_of'])):
            waiting = row['status'] == 'COMPLETED' and json.loads(row.get('decision_json') or '{}').get('action') == 'WAIT'
            streak = streak + 1 if waiting else 0
            max_wait = max(max_wait, streak)
    accepted, filled = set(), set()
    for account in accounts:
        for event in account['events']:
            if event.get('action') in {'OPEN_LONG', 'OPEN_SHORT'}:
                key = (account['window'], event.get('order_id'))
                if event.get('status') == 'ACCEPTED':
                    accepted.add(key)
                if event.get('status') in {'FILLED', 'PARTIAL_FILL'}:
                    filled.add(key)
    floating = sum(number(a['metrics']['unrealized_pnl']) for a in accounts)
    drawdown = max((a['metrics']['max_drawdown'] for a in accounts), default=None)
    net_closed = sum(nets, Decimal(0))
    issues = []
    if not complete_audited:
        issues.append('ALL_PREREGISTERED_WINDOWS_NOT_COMPLETE_AND_AUDITED')
    if statuses['ERROR'] or statuses['ACCOUNT_HALTED']:
        issues.append('FAILED_OR_HALTED_SCANS')
    if total < 30:
        issues.append('FEWER_THAN_30_COMPLETE_CLOSED_POSITIONS')
    if cluster_count < 30:
        issues.append('FEWER_THAN_30_NONOVERLAPPING_OPPORTUNITY_CLUSTERS')
    if win_rate_target not in (.5, .6):
        raise ValueError('OUTCOME_UNREGISTERED_WIN_RATE_TARGET')
    if not total or wins / total < win_rate_target:
        issues.append('OBSERVED_NET_WIN_RATE_BELOW_TARGET_OR_UNDEFINED')
    if interval is None or interval[0] < win_rate_target:
        issues.append(f'WILSON_LOWER_BOUND_BELOW_{int(win_rate_target * 100)}_PERCENT_OR_UNDEFINED')
    if net_closed <= 0:
        issues.append('NO_POSITIVE_COMPLETE_CLOSED_NET_PNL')
    if not filled:
        issues.append('NO_FILLED_ENTRY_EVIDENCE')
    if set(clusters) != set(expected_windows):
        issues.append('NO_CLOSED_TRADE_COVERAGE_IN_EACH_CALENDAR_WINDOW')
    if maximum_drawdown is None:
        issues.append('DRAWDOWN_ACCEPTANCE_LIMIT_NOT_SPECIFIED')
    elif drawdown is None or drawdown > maximum_drawdown:
        issues.append('WINDOW_EQUITY_DRAWDOWN_ABOVE_LIMIT_OR_UNDEFINED')
    return {'closed_positions': total, 'wins_after_fees_and_funding': wins,
            'losses': sum(n < 0 for n in nets), 'breakeven': sum(n == 0 for n in nets),
            'pooled_win_rate': wins / total if total else None, 'wilson_95': interval,
            'registered_win_rate_target': win_rate_target,
            'closed_net_pnl_usdt': str(net_closed), 'unrealized_pnl_usdt_excluded': str(floating),
            'max_window_equity_drawdown': drawdown, 'drawdown_limit': maximum_drawdown,
            'opportunity_clusters': cluster_count, 'closed_trade_windows': sorted(clusters),
            'observed_row_statuses': dict(statuses), 'valid_actions': dict(actions),
            'raw_open_proposals_including_errors': proposals, 'accepted_entry_orders': len(accepted),
            'filled_entry_orders': len(filled), 'max_wait_streak_within_one_window': max_wait,
            'numeric_screen_met': phase != 'optimization' and not issues,
            'issues': issues, 'independence_and_regime_review_still_required': True,
            'scope': 'POOLED_RESET_WINDOW_ACCOUNTS_NOT_CONTINUOUS_ANNUAL_OR_GATE_RETURN'}


def audit_directory(directory, *, maximum_drawdown=None, acceptance_policy=None):
    directory = Path(directory).resolve()
    registration = json.loads((directory / 'research-plan.json').read_text(encoding='utf-8'))
    plan = registration['plan']
    if registration['plan_sha256'] != digest(plan):
        raise ValueError('OUTCOME_PLAN_HASH_INVALID')
    policy_hash = None
    win_rate_target = plan.get('acceptance_win_rate_target', .5)
    if acceptance_policy is not None:
        policy_artifact = json.loads(Path(acceptance_policy).read_text(encoding='utf-8'))
        policy = policy_artifact['policy']
        policy_hash = policy_artifact['policy_sha256']
        expected_origin = plan.get('source_optimization_plan_sha256', registration['plan_sha256'])
        if (policy_hash != digest(policy) or policy.get('source_optimization_plan_sha256') != expected_origin
                or sorted(policy.get('template_ids', [])) != sorted(t['template_id'] for t in plan['templates'])
                or policy.get('minimum_observed_closed_net_win_rate') != win_rate_target
                or policy.get('wilson_95_lower_bound_minimum') != win_rate_target
                or policy.get('minimum_complete_closed_positions_per_strategy') != 30
                or policy.get('minimum_nonoverlapping_opportunity_clusters_per_strategy') != 30
                or policy.get('entry_notional_usdt') != 2000):
            raise ValueError('OUTCOME_ACCEPTANCE_POLICY_BINDING_INVALID')
        policy_dd = policy['maximum_window_equity_drawdown']
        if maximum_drawdown is not None and maximum_drawdown != policy_dd:
            raise ValueError('OUTCOME_ACCEPTANCE_POLICY_DRAWDOWN_CONFLICT')
        maximum_drawdown = policy_dd
    phases = {w['partition'] for w in plan['pilot_windows']}
    if len(phases) != 1 or not phases <= {'optimization', 'validation', 'untouched_test'}:
        raise ValueError('OUTCOME_MIXED_OR_UNKNOWN_PARTITIONS')
    phase = next(iter(phases))
    expected = [w['id'] for w in plan['pilot_windows']]
    if len(expected) != 3 or len(set(expected)) != 3:
        raise ValueError('OUTCOME_THREE_UNIQUE_CALENDAR_WINDOWS_REQUIRED')
    records = {t['template_id']: {'trades': [], 'rows': [], 'accounts': []} for t in plan['templates']}
    windows, all_complete = [], True
    for window in plan['pilot_windows']:
        target = (directory / window['id']).resolve()
        if target.parent != directory:
            raise ValueError('OUTCOME_WINDOW_PATH_OUTSIDE_EXPERIMENT')
        source = target / 'results.sqlite3'
        if not source.exists():
            windows.append({'id': window['id'], 'status': 'NOT_STARTED', 'complete_audited': False})
            all_complete = False
            continue
        # Audit and summarize the same SQLite backup, even while research
        # advances. The source connection is read-only; no frozen file changes.
        with tempfile.TemporaryDirectory(prefix='gemini-outcome-audit-') as scratch:
            snapshot = Path(scratch) / 'snapshot.sqlite3'
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
            with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                run = dict(db.execute('SELECT * FROM ai_template_replay_runs').fetchone())
                config, checkpoint = json.loads(run['config_json']), json.loads(run['checkpoint_json'])
                rows = [dict(row) for row in db.execute('SELECT * FROM ai_template_replay_decisions')]
            if config['templates'] != plan['templates']:
                raise ValueError('OUTCOME_FROZEN_TEMPLATE_MISMATCH')
            for template in config['templates']:
                execution = template['execution']
                if (execution.get('sizing_mode') != 'FIXED_NOTIONAL' or
                        number(execution['fixed_notional_usdt']) != 2000 or execution.get('leverage_mode') != 'VENUE_LIMIT'):
                    raise ValueError('OUTCOME_FIXED_2000_VENUE_LEVERAGE_REQUIRED')
            row_statuses = dict(Counter(row['status'] for row in rows))
            complete = run['status'] == 'COMPLETED' and all(row['status'] == 'COMPLETED' for row in rows)
            try:
                audit = audit_snapshot(snapshot, target / 'history.json', complete=complete)
                for row in rows:
                    if row['status'] != 'COMPLETED':
                        continue
                    decision, result = json.loads(row['decision_json']), json.loads(row['result_json'])
                    execution = next(t['execution'] for t in config['templates'] if t['template_id'] == row['template_id'])
                    for event in result.get('events', []):
                        if event.get('status') == 'ACCEPTED' and event.get('action') in {'OPEN_LONG', 'OPEN_SHORT'}:
                            audit_entry(decision, event, result['execution_market_snapshots'][event['instrument_id']], execution)
            except (ValueError, KeyError) as exc:
                windows.append({'id': window['id'], 'status': 'AUDIT_FAILED', 'complete_audited': False,
                                'recorded_row_statuses': row_statuses, 'error': str(exc)[:200]})
                all_complete = False
                continue
            windows.append({'id': window['id'], 'status': run['status'], 'complete_audited': complete,
                            'recorded_row_statuses': row_statuses,
                            'snapshot_completed_rows': audit['completed_decisions'], 'audit_scope': audit['scope']})
            all_complete &= complete
            for template, state in checkpoint['accounts'].items():
                data = state['state']
                records[template]['accounts'].append({'window': window['id'], 'metrics': audit['accounts'][template],
                                                     'events': data['events']})
                records[template]['trades'].extend({**t, 'window': window['id']} for t in data['completed_trades'])
                records[template]['rows'].extend({**r, 'window': window['id']} for r in rows if r['template_id'] == template)
    strategies = {}
    for template in plan['templates']:
        strategies[template['template_id']] = summarize_strategy(**records[template['template_id']], phase=phase,
            expected_windows=expected, complete_audited=all_complete,
            scan_interval_minutes=int(template['execution']['scan_interval_minutes']), maximum_drawdown=maximum_drawdown,
            win_rate_target=win_rate_target)
    return {'scope': 'READ_ONLY_PHASE_NUMERIC_SCREEN_NOT_FINAL_PROFIT_OR_PRIVATE_GATE_ACCEPTANCE',
            'phase': phase, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'plan_sha256': registration['plan_sha256'],
            'acceptance_policy_sha256': policy_hash,
            'audit_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'all_windows_complete_audited': all_complete, 'windows': windows, 'strategies': strategies,
            'status': 'OPTIMIZATION_DIAGNOSTIC_ONLY' if phase == 'optimization' else
                      'NUMERIC_SCREEN_MET_REQUIRES_REVIEW' if all(s['numeric_screen_met'] for s in strategies.values()) else 'NOT_READY',
            'profit_acceptance_passed': False, 'future_win_rate_guaranteed': False, 'production_strategy_writes': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--maximum-drawdown', type=float)
    parser.add_argument('--acceptance-policy', type=Path)
    args = parser.parse_args()
    if args.maximum_drawdown is not None and not 0 < args.maximum_drawdown < 1:
        parser.error('maximum-drawdown must be a fraction strictly between zero and one')
    result = audit_directory(args.directory, maximum_drawdown=args.maximum_drawdown,
                             acceptance_policy=args.acceptance_policy)
    (args.directory / 'phase-outcomes.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'phase': result['phase'], 'strategies': result['strategies']}))


if __name__ == '__main__':
    main()
