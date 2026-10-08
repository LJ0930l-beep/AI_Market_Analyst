"""Audited optimization trade cases; no causal/profit claims or live writes."""
import argparse
from collections import Counter
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest
from scripts.audit_gemini_phase_outcomes import audit_snapshot
from scripts.verify_ai_template_replay import number, audit_effective_request

OPEN = {'OPEN_LONG', 'OPEN_SHORT'}


def visible_market_facts(inputs, instrument_id):
    """Project only this instrument from the audited final request, not history.

    The action may target a different instrument than a visible managed position.
    Never substitute the action's market or the execution-time snapshot.
    """
    if not isinstance(instrument_id, str) or not instrument_id:
        raise ValueError('CASE_VISIBLE_MARKET_INSTRUMENT_REQUIRED')
    return deepcopy({
        'instrument_id': instrument_id,
        'market_snapshot': (inputs.get('market_snapshots') or {}).get(instrument_id),
        'technical_context': (inputs.get('technical_context') or {}).get(instrument_id),
        'candle_columns': (inputs.get('technical_context') or {}).get('candle_columns'),
        'price_action_encoding': (inputs.get('technical_context') or {}).get('price_action_encoding'),
        'news_coverage_status': inputs.get('news_coverage_status'),
        'news_revisions': inputs.get('news_revisions'),
        'visible_evidence_refs': inputs.get('evidence_refs'),
    })


def position_visible_scans(trade, rows):
    """Link by position actually visible in the bound request, not time overlap."""
    observations = []
    seen = set()
    for row in rows:
        if row['status'] != 'COMPLETED':
            continue
        positions = row.get('effective_managed_positions') or []
        matches = [p for p in positions if p.get('position_id') == trade['position_id']
                   and p.get('ownership') == 'VERIFIED_SYSTEM']
        if not matches:
            continue
        if (len(matches) != 1 or row['scan_key'] in seen
                or matches[0].get('instrument_id') != trade['instrument_id']
                or matches[0].get('side') != trade['side']):
            raise ValueError('CASE_VISIBLE_POSITION_BINDING_INVALID')
        seen.add(row['scan_key'])
        position = matches[0]
        facts = (row.get('effective_market_facts_by_instrument') or {}).get(trade['instrument_id'])
        if facts is not None and facts.get('instrument_id') != trade['instrument_id']:
            raise ValueError('CASE_VISIBLE_POSITION_MARKET_IDENTITY_INVALID')
        observations.append({
            'scan_key': row['scan_key'], 'as_of': row['as_of'],
            'effective_model_request_hash': row['effective_request_hash'],
            'source_evidence_bundle_id': row['context']['evidence_bundle_id'],
            'response_sha256': row['response_sha256'],
            'decision': row['decision'],
            'decision_explicitly_targets_this_position': row['decision'].get('position_id') == trade['position_id'],
            'effective_model_visible_position_facts': deepcopy(facts),
            'visible_position': {k: position.get(k) for k in ('position_id', 'instrument_id', 'side',
                'ownership', 'quantity', 'entry_price', 'mark_price', 'stop_price', 'take_profit', 'unrealized_pnl')},
            'position_execution_events': [e for e in row['result'].get('events', [])
                                          if e.get('position_id') == trade['position_id']],
            'scope': 'POSITION_VISIBLE_SCAN_NOT_AUTOMATICALLY_A_TARGETED_MANAGEMENT_ACTION',
        })
    return observations


def build_case(trade, events, rows):
    """Caller must independently audit this immutable SQLite snapshot first."""
    position = trade['position_id']
    matching = [e for e in events if e.get('position_id') == position]
    accepted = [e for e in matching if e.get('action') in OPEN and e.get('status') == 'ACCEPTED']
    entries = [e for e in matching if e.get('action') in OPEN
               and e.get('status') in {'FILLED', 'PARTIAL_FILL'} and number(e.get('opened_quantity', 0)) > 0]
    exits = [e for e in matching if e.get('gross_pnl') is not None
             and e.get('status') in {'FILLED', 'PARTIAL_FILL'}]
    if not accepted or not entries or not exits:
        raise ValueError('CASE_ENTRY_OR_EXIT_LEDGER_BINDING_MISSING')
    net, gross, fees, funding = (number(trade[key]) for key in ('net_pnl', 'realized_gross_pnl', 'fees', 'funding_pnl'))
    if net != gross - fees + funding:
        raise ValueError('CASE_NET_PNL_FORMULA')
    proposals = {}
    for entry in accepted:
        candidates = [r for r in rows if r['status'] == 'COMPLETED' and any(
            event.get('order_id') == entry['order_id'] and event.get('status') == 'ACCEPTED'
            for event in r['result'].get('events', []))]
        if len(candidates) != 1:
            raise ValueError('CASE_ENTRY_MODEL_PROPOSAL_BINDING_MISSING_OR_AMBIGUOUS')
        row = candidates[0]
        if (row['decision']['action'] != entry['action']
                or row['decision']['instrument_id'] != trade['instrument_id']
                or entry['side'] != trade['side']):
            raise ValueError('CASE_ENTRY_MODEL_IDENTITY_MISMATCH')
        proposals[entry['order_id']] = {
            'scan_key': row['scan_key'], 'as_of': row['as_of'],
            'decision': row['decision'], 'source_evidence_bundle_id': row['context']['evidence_bundle_id'],
            'input_hash': row['context']['input_hash'],
            'response_sha256': row['response_sha256'],
            'effective_model_request_hash': row.get('effective_request_hash'),
            'effective_model_visible_entry_facts': row.get('effective_entry_facts'),
        }

    # This scenario is only defined for one unnetted fill. Scaling/FIFO cases
    # remain complete cases but do not get a misleading average-plan RR.
    scenario = None
    if len(accepted) == len(entries) == 1 and not number(entries[0].get('netted_quantity', 0)):
        entry, fill = accepted[0], entries[0]
        source = next(r for r in rows if r['scan_key'] == proposals[entry['order_id']]['scan_key'])
        market = source['result']['execution_market_snapshots'][trade['instrument_id']]['market']
        units = number(fill['opened_quantity']) * number(market['contractSize'])
        entry_price, stop, target = (number(item) for item in (fill['fill_price'], entry['stop_price'], entry['take_profit']))
        direction = Decimal(1) if trade['side'] == 'LONG' else Decimal(-1)
        risk, reward = direction * (entry_price - stop) * units, direction * (target - entry_price) * units
        if risk > 0 and reward > 0:
            stop_cost = risk + number(fill['fee']) + units * stop * number(market['taker'])
            target_net = reward - number(fill['fee']) - units * target * number(market['taker'])
            scenario = {
                'scope': 'INITIAL_PROTECTION_FEES_ONLY_EXCLUDES_EXIT_SLIPPAGE_FUNDING_AND_LATER_UPDATES',
                'entry_fill_price': str(entry_price), 'initial_stop': str(stop), 'initial_target': str(target),
                'target_net_usdt': str(target_net), 'stop_loss_including_fees_usdt': str(stop_cost),
                'net_reward_to_loss': str(target_net / stop_cost),
                'model_reference_entry_price': str(number(source['decision']['entry_price'])),
                'entry_price_change_cost_including_latency_usdt': str(
                    direction * (entry_price - number(source['decision']['entry_price'])) * units),
                'entry_price_change_is_not_pure_exchange_slippage': True,
            }
    return {'position_id': position, 'instrument_id': trade['instrument_id'], 'side': trade['side'],
        'opened_at': trade['opened_at'], 'closed_at': trade['closed_at'],
        'gross_pnl_usdt': str(gross), 'fees_usdt': str(fees), 'funding_pnl_usdt': str(funding),
        'net_pnl_usdt': str(net), 'net_outcome': 'WIN' if net > 0 else 'LOSS' if net < 0 else 'BREAKEVEN',
        'fees_to_absolute_gross_pnl': str(fees / abs(gross)) if gross else None,
        'entry_model_proposals': proposals,
        'position_visible_model_scans': position_visible_scans(trade, rows),
        'entry_fills': [{k:e.get(k) for k in ('order_id','event_id','fill_time','fill_price','quantity',
            'opened_quantity','netted_quantity','fee','fee_type','marketability_assumption')} for e in entries],
        'exit_ledger_triggers': [{k:e.get(k) for k in ('action','order_id','event_id','fill_time','fill_price',
            'quantity','gross_pnl','fee','fee_type','gap_stop')} for e in exits],
        'initial_plan_scenario': scenario,
        'model_reasons_are_claims_not_verified_profit_causes': True,
        'execution_scope': 'AUDITED_HISTORICAL_OHLCV_SIMULATION_NOT_GATE_PRIVATE_FILL', 'gate_verified': False}


def build_directory(directory):
    directory = Path(directory).resolve()
    registration = json.loads((directory / 'research-plan.json').read_text(encoding='utf-8'))
    plan = registration['plan']
    if registration['plan_sha256'] != digest(plan):
        raise ValueError('CASE_PLAN_HASH_INVALID')
    if any(w['partition'] != 'optimization' for w in plan['pilot_windows']):
        raise ValueError('CASE_OPTIMIZATION_ONLY_NO_HELDOUT_TUNING')
    cases, audits = [], []
    for window in plan['pilot_windows']:
        target = (directory / window['id']).resolve()
        if target.parent != directory:
            raise ValueError('CASE_PATH_OUTSIDE_EXPERIMENT')
        database = target / 'results.sqlite3'
        if not database.exists():
            continue
        with tempfile.TemporaryDirectory(prefix='gemini-casebook-') as scratch:
            snapshot = Path(scratch) / 'snapshot.sqlite3'
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
            with closing(sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory = sqlite3.Row
                run = dict(db.execute('SELECT * FROM ai_template_replay_runs').fetchone())
                raw_rows = [dict(r) for r in db.execute('SELECT * FROM ai_template_replay_decisions ORDER BY rowid')]
            config, checkpoint = json.loads(run['config_json']), json.loads(run['checkpoint_json'])
            if config['templates'] != plan['templates']:
                raise ValueError('CASE_FROZEN_TEMPLATE_MISMATCH')
            row_statuses = dict(Counter(r['status'] for r in raw_rows))
            fully_completed = run['status']=='COMPLETED' and all(r['status']=='COMPLETED' for r in raw_rows)
            # A finished schedule containing transport failures is still
            # diagnostic. Independently audit valid rows without erasing the
            # failed denominator or claiming a complete execution pass.
            audit = audit_snapshot(snapshot, target/'history.json', complete=fully_completed)
            audits.append({'window':window['id'], 'run_id':run['run_id'], 'scope':audit['scope'],
                           'status':audit['status'], 'completed_decisions':audit['completed_decisions'],
                           'run_status':run['status'],'recorded_row_statuses':row_statuses,
                           'complete_execution_audit':fully_completed})
            rows = [{**r, 'decision':json.loads(r['decision_json'] or '{}'),
                'context':json.loads(r['context_json'] or '{}'), 'result':json.loads(r['result_json'] or '{}')}
                for r in raw_rows]
            # The effective repair may have a narrower view than the initial
            # prompt. Use the independently bound final request, not a broader
            # evidence bundle the model never saw or a later market quote.
            with closing(sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True)) as db:
                def load_bundle(bundle_id):
                    item = db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?', (bundle_id,)).fetchone()
                    if item is None:
                        raise ValueError('CASE_MODEL_EVIDENCE_BUNDLE_MISSING')
                    return json.loads(item[0])
                for row in rows:
                    if row['status'] != 'COMPLETED' or not row['decision'].get('action'):
                        continue
                    ctx = row['context']
                    effective = audit_effective_request(ctx, load_bundle(ctx['evidence_bundle_id']), load_bundle)
                    wrapper = json.loads(effective['messages'][1]['content'])
                    inputs = wrapper['inputs'] if 'previous_decision' in wrapper else wrapper
                    symbol = row['decision']['instrument_id']
                    row['effective_request_hash'] = effective['request_hash']
                    truth = inputs.get('account_truth') or {}
                    positions = truth.get('managed_positions')
                    if positions is None:
                        positions = truth.get('positions') or []
                    if not isinstance(positions, list) or any(not isinstance(p, dict) for p in positions):
                        raise ValueError('CASE_VISIBLE_POSITION_LIST_INVALID')
                    row['effective_managed_positions'] = positions
                    instruments = {symbol, *(p.get('instrument_id') for p in positions
                                             if p.get('ownership') == 'VERIFIED_SYSTEM')}
                    row['effective_market_facts_by_instrument'] = {
                        instrument: visible_market_facts(inputs, instrument) for instrument in instruments}
                    if row['decision'].get('action') not in OPEN:
                        continue
                    row['effective_entry_facts'] = row['effective_market_facts_by_instrument'][symbol]
            for template, account in checkpoint['accounts'].items():
                state = account['state']
                for trade in state['completed_trades']:
                    case = build_case(trade, state['events'], [r for r in rows if r['template_id']==template])
                    case.update(template_id=template, window=window['id'], run_id=run['run_id'])
                    case['case_id'] = digest(case)
                    cases.append(case)
    return {'scope':'AUDITED_OPTIMIZATION_CASES_NOT_FINAL_PROFIT_OR_GATE_ACCEPTANCE',
        'casebook_schema_version': 3,
        'position_observation_scope': 'FINAL_REQUEST_VISIBLE_VERIFIED_POSITION_ID_NOT_INFERRED_FROM_HOLD_TEXT',
        'observed_at':datetime.now(timezone.utc).isoformat(), 'plan_sha256':registration['plan_sha256'],
        'audits':audits, 'cases':cases, 'case_count':len(cases), 'production_strategy_writes':0,
        'private_exchange_calls':0, 'profit_acceptance_passed':False, 'future_win_rate_guaranteed':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    result = build_directory(args.directory)
    (args.directory/'trade-casebook.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'case_count':result['case_count'], 'scope':result['scope'],
                      'cases':[{'template_id':c['template_id'],'net_outcome':c['net_outcome'],
                                'net_pnl_usdt':c['net_pnl_usdt']} for c in result['cases']]}))


if __name__ == '__main__':
    main()
