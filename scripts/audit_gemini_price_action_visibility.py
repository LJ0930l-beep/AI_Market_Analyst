"""Inventory PA evidence in actual final requests, never infer trade causes."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest
from scripts.verify_ai_template_replay import audit_effective_request


def inspect_inputs(inputs):
    technical = inputs.get('technical_context') or {}
    encoding = technical.get('price_action_encoding') or {}
    keys = encoding.get('keys') or {}
    defaults = encoding.get('defaults') or {}
    records = []
    for symbol in inputs.get('allowed_instruments') or []:
        instrument = technical.get(symbol) or {}
        for timeframe in ('15m', '1h'):
            frame = (instrument.get('timeframes') or {}).get(timeframe) or {}
            pa = frame.get('price_action')
            pa = pa if isinstance(pa, dict) else None
            swings = (pa or {}).get('confirmed_swings')
            counts = Counter()
            for swing in swings if isinstance(swings, list) else []:
                if not isinstance(swing, dict):
                    continue
                decoded = {keys.get(k, k): v for k, v in swing.items()}
                if decoded.get('side') in ('HIGH', 'LOW') and decoded.get('price') is not None:
                    counts[decoded['side']] += 1
            candles = frame.get('candles', frame.get('bars'))
            bar_count = len(candles) if isinstance(candles, list) else None
            states = {}
            for field in ('bos', 'sweep_reclaim', 'breakout_retest'):
                event = (pa or {}).get(field)
                decoded = {keys.get(k, k): v for k, v in event.items()} if isinstance(event, dict) else {}
                states[field] = decoded.get('state')  # No state does not mean ACTIVE.
            records.append({'instrument_id': symbol, 'timeframe': timeframe,
                'price_action_present': pa is not None,
                'price_action_status': pa.get('status', defaults.get('status')) if pa else None,
                'explicit_confirmed_highs': counts['HIGH'], 'explicit_confirmed_lows': counts['LOW'],
                'two_highs_and_two_lows_visible': counts['HIGH'] >= 2 and counts['LOW'] >= 2,
                'visible_candle_rows': bar_count, 'event_states': states,
                'only_one_high_low_and_at_most_one_candle': bool(pa) and counts['HIGH'] == counts['LOW'] == 1
                    and bar_count is not None and bar_count <= 1})
    return records


def audit_directory(directory):
    directory = Path(directory).resolve()
    registration = json.loads((directory/'research-plan.json').read_text(encoding='utf-8'))
    plan = registration['plan']
    if digest(plan) != registration['plan_sha256']:
        raise ValueError('PA_VISIBILITY_PLAN_HASH_INVALID')
    if any(w['partition'] != 'optimization' for w in plan['pilot_windows']):
        raise ValueError('PA_VISIBILITY_OPTIMIZATION_ONLY')
    counts, statuses, inventory, examples = Counter(), Counter(), [], []
    frame_counts = {'15m': Counter(), '1h': Counter()}
    for window in plan['pilot_windows']:
        target = (directory/window['id']).resolve()
        if target.parent != directory:
            raise ValueError('PA_VISIBILITY_PATH_OUTSIDE_EXPERIMENT')
        database = target/'results.sqlite3'
        if not database.exists():
            continue
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            def load(bundle_id):
                row = db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?', (bundle_id,)).fetchone()
                if row is None:
                    raise ValueError('PA_VISIBILITY_REQUEST_EVIDENCE_MISSING')
                return json.loads(row[0])
            for row in db.execute('SELECT * FROM ai_template_replay_decisions ORDER BY rowid').fetchall():
                statuses[row['status']] += 1
                if row['status'] != 'COMPLETED':
                    continue
                context, decision = json.loads(row['context_json']), json.loads(row['decision_json'])
                request = audit_effective_request(context, load(context['evidence_bundle_id']), load)
                wrapper = json.loads(request['messages'][1]['content'])
                inputs = wrapper['inputs'] if 'previous_decision' in wrapper else wrapper
                records = inspect_inputs(inputs)
                counts['completed_final_requests_checked'] += 1
                counts['effective_requests_without_allowed_instruments'] += not records
                for record in records:
                    own = frame_counts[record['timeframe']]
                    own['selected_symbol_frame_observations'] += 1
                    own['price_action_present'] += record['price_action_present']
                    own['two_highs_and_two_lows_visible'] += record['two_highs_and_two_lows_visible']
                    own['one_high_low_and_at_most_one_candle'] += record['only_one_high_low_and_at_most_one_candle']
                    if record['instrument_id'] == decision.get('instrument_id') and decision['action'] in ('OPEN_LONG','OPEN_SHORT'):
                        own['open_selected_symbol_frame_observations'] += 1
                        own['open_with_one_high_low_and_at_most_one_candle'] += record['only_one_high_low_and_at_most_one_candle']
                        if record['only_one_high_low_and_at_most_one_candle'] and not any(e['timeframe'] == record['timeframe'] for e in examples):
                            examples.append({**record, 'window': window['id'], 'scan_key': row['scan_key'],
                                'as_of': row['as_of'], 'request_hash': request['request_hash']})
                inventory.append({'window': window['id'], 'scan_key': row['scan_key'],
                    'request_hash': request['request_hash'], 'visibility': records})
    return {'scope': 'ACTUAL_FINAL_INPUT_VISIBILITY_NOT_CAUSAL_OR_PROFIT_ACCEPTANCE',
        'observed_at': datetime.now(timezone.utc).isoformat(), 'plan_sha256': registration['plan_sha256'],
        'recorded_row_statuses': dict(statuses), 'counts': dict(counts),
        'frames': {k: dict(v) for k, v in frame_counts.items()}, 'examples': examples,
        'request_inventory_sha256': digest(inventory),
        'limited_explicit_swing_history_does_not_prove_wrong_entry_or_require_wait': True,
        'not_a_new_trading_guard': True, 'frozen_source_modified': False,
        'model_calls': 0, 'private_exchange_calls': 0, 'profit_acceptance_passed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    report = audit_directory(args.directory)
    (args.directory/'price-action-visibility-audit.json').write_text(
        json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('scope','recorded_row_statuses','counts','frames','model_calls')}))


if __name__ == '__main__':
    main()
