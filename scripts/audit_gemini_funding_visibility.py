"""Read-only optimization archive-vs-effective-model-input funding diagnostic."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest, manifest_hash
from core.replay.settled_funding import settled_funding_as_of
from scripts.verify_ai_template_replay import audit_effective_request

RATE_FIELDS = {'fundingRate', 'funding_rate', 'funding_rate_pct', 'rate_fraction', 'rate_percent'}


def visible_rate_fields(inputs):
    """Inventory numeric rate fields, not PnL/fees or unknown/zero placeholders.

    Finding a numeric field is not proof of its venue or data provenance.
    """
    found = []
    def numeric(value):
        try:
            return value is not None and not isinstance(value, bool) and Decimal(str(value)).is_finite()
        except (InvalidOperation, ValueError):
            return False
    def walk(value, path):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in RATE_FIELDS and numeric(child):
                    found.append({'path': path+[key], 'value': str(child)})
                walk(child, path+[key])
            columns = value.get('derivatives_matrix_columns')
            rows = value.get('derivatives_matrix')
            if isinstance(columns, str) and isinstance(rows, list):
                for index, column in enumerate(columns.split(',')):
                    if 'funding' not in column.lower():
                        continue
                    for ri, row in enumerate(rows):
                        if isinstance(row, list) and len(row) > index and numeric(row[index]):
                            found.append({'path':path+['derivatives_matrix',ri,column], 'value':str(row[index])})
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path+[index])
    for root in ('market_snapshots','market_radar','technical_context'):
        walk(inputs.get(root), [root])
    return found


def inspect_scan(inputs, history, as_of, decision):
    symbols = inputs.get('allowed_instruments') or []
    venue = history.get('assumptions',{}).get('data_venue')
    archived = settled_funding_as_of(history.get('funding',[]), symbols, as_of, venue=venue)
    known = {key: row for key,row in archived['rates'].items() if row['rate_fraction'] is not None}
    fields = visible_rate_fields(inputs)
    return {'known_archive_rates':known, 'numeric_visible_rate_fields':fields,
        'archive_rate_present_but_no_numeric_rate_field':bool(known) and not fields,
        'wait_with_archive_rate_but_no_visible_rate_field':decision.get('action')=='WAIT' and bool(known) and not fields,
        'archive_scope':archived['scope'], 'publication_lag_assumption_seconds':60}


def audit_directory(directory):
    directory = Path(directory).resolve()
    registration = json.loads((directory/'research-plan.json').read_text(encoding='utf-8'))
    plan = registration['plan']
    if digest(plan) != registration['plan_sha256']:
        raise ValueError('FUNDING_VISIBILITY_PLAN_HASH_INVALID')
    if any(w['partition'] != 'optimization' for w in plan['pilot_windows']):
        raise ValueError('FUNDING_VISIBILITY_OPTIMIZATION_ONLY')
    counters, examples, requests = defaultdict(Counter), [], []
    for window in plan['pilot_windows']:
        target = (directory/window['id']).resolve()
        if target.parent != directory:
            raise ValueError('FUNDING_VISIBILITY_PATH_OUTSIDE_EXPERIMENT')
        database = target/'results.sqlite3'
        if not database.exists():
            continue
        history = json.loads((target/'history.json').read_text(encoding='utf-8'))
        if history['manifest_sha256'] != manifest_hash(history) or history['research_plan_sha256'] != registration['plan_sha256']:
            raise ValueError('FUNDING_VISIBILITY_HISTORY_BINDING_INVALID')
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            rows = db.execute('SELECT scan_key,template_id,as_of,status,context_json,decision_json FROM ai_template_replay_decisions ORDER BY rowid').fetchall()
            def load(bundle_id):
                row = db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?', (bundle_id,)).fetchone()
                if row is None:
                    raise ValueError('FUNDING_VISIBILITY_EVIDENCE_MISSING')
                return json.loads(row[0])
            for row in rows:
                counts = counters[row['template_id']]
                counts['recorded_rows'] += 1
                if row['status'] != 'COMPLETED':
                    counts['rows_not_completed_excluded_from_input_comparison'] += 1
                    continue
                context, decision = json.loads(row['context_json']), json.loads(row['decision_json'])
                effective = audit_effective_request(context, load(context['evidence_bundle_id']), load)
                wrapper = json.loads(effective['messages'][1]['content'])
                inputs = wrapper['inputs'] if 'previous_decision' in wrapper else wrapper
                result = inspect_scan(inputs, history, row['as_of'], decision)
                counts['completed_model_requests_checked'] += 1
                counts['requests_with_known_archive_rate'] += bool(result['known_archive_rates'])
                counts['requests_with_numeric_visible_rate_field'] += bool(result['numeric_visible_rate_fields'])
                counts['archive_rate_present_but_no_numeric_rate_field'] += result['archive_rate_present_but_no_numeric_rate_field']
                counts['wait_with_archive_rate_but_no_visible_rate_field'] += result['wait_with_archive_rate_but_no_visible_rate_field']
                requests.append({'window':window['id'],'scan_key':row['scan_key'],'request_hash':effective['request_hash']})
                if result['wait_with_archive_rate_but_no_visible_rate_field'] and not any(
                        item['template_id'] == row['template_id'] for item in examples):
                    examples.append({**result, 'window':window['id'],'template_id':row['template_id'],
                                     'scan_key':row['scan_key'],'as_of':row['as_of'],'effective_request_hash':effective['request_hash']})
    return {'scope':'READ_ONLY_ARCHIVE_INPUT_GAP_NOT_PROOF_OF_WRONG_WAIT_OR_PROFIT',
        'observed_at':datetime.now(timezone.utc).isoformat(), 'plan_sha256':registration['plan_sha256'],
        'request_inventory_sha256':digest(requests), 'strategies':dict(counters), 'examples':examples,
        'source_venue':'BINANCE_ARCHIVE_NOT_GATE_CURRENT_FUNDING',
        'publication_time_not_attested':True,'research_publication_lag_seconds':60,
        'running_frozen_source_modified':False,'model_calls':0,'private_exchange_calls':0,
        'profit_acceptance_passed':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    args = parser.parse_args()
    result = audit_directory(args.directory)
    (args.directory/'funding-visibility-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('scope','strategies','source_venue','model_calls')}))


if __name__ == '__main__':
    main()
