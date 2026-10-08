"""Read-only optimization diagnostics; model-declared gaps are not proven facts.

Separates flat-account waiting from managing positions/pending entries. Does
not grade strategy profitability, label a price touch as a complete setup,
alter frozen prompts, or produce/activate candidate instructions.
"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest


def summarize(rows):
    statuses, actions, gaps, data_mentions = Counter(), Counter(), Counter(), Counter()
    flat_scans = flat_opens = flat_waits = managed_scans = unknown_accounts = 0
    examples = []
    for row in rows:
        statuses[row['status']] += 1
        if row['status'] != 'COMPLETED':
            continue
        decision, account = row['decision'], row['account']
        action = decision['action']
        actions[action] += 1
        # Ownership and complete authoritative account status are necessary to
        # call a scan flat. Missing evidence is not an empty account.
        if account.get('status') != 'AVAILABLE':
            unknown_accounts += 1
            continue
        owned = [item for item in account.get('managed_positions') or []
                 if isinstance(item, dict) and item.get('ownership') == 'VERIFIED_SYSTEM']
        owned.extend(item for item in account.get('owned_entry_orders') or []
                     if isinstance(item, dict) and item.get('ownership') == 'SYSTEM_ORDER_ID_MATCH'
                     and item.get('order_id'))
        if owned:
            managed_scans += 1
        else:
            flat_scans += 1
            flat_opens += action in {'OPEN_LONG', 'OPEN_SHORT'}
            flat_waits += action == 'WAIT'
        if action != 'WAIT':
            continue
        analysis = decision.get('strategy_analysis') or {}
        missing = analysis.get('missing_conditions') or []
        for condition in missing:
            if isinstance(condition, str):
                gaps[condition[:500]] += 1
        combined = ' '.join(str(item) for item in missing)
        for label, needles in {'OI': ('未平仓量', 'open_interest', 'open interest'), 'funding': ('资金费率', 'funding'),
                               'CVD': ('CVD',), 'news': ('新闻', 'news')}.items():
            if (any(needle.lower() in combined.lower() for needle in needles)
                    or label == 'OI' and re.search(r'(?<![a-z0-9])oi(?![a-z0-9])', combined.lower())):
                data_mentions[label] += 1
        if len(examples) < 6:
            examples.append({'window': row['window'], 'as_of': row['as_of'],
                'instrument_id': decision.get('instrument_id'), 'reason': decision.get('reason'),
                'missing_conditions': missing, 'next_trigger_price': decision.get('next_trigger_price'),
                'entry_condition': decision.get('entry_condition'), 'flat_account': not owned})
    return {'statuses': dict(statuses), 'valid_actions': dict(actions),
            'flat_account_scans': flat_scans, 'flat_account_open_proposals': flat_opens,
            'flat_account_waits': flat_waits,
            'flat_account_open_proposal_fraction': flat_opens / flat_scans if flat_scans else None,
            'scans_with_system_position_or_pending_entry': managed_scans,
            'unknown_account_scans_excluded_from_flat_denominator': unknown_accounts,
            'model_declared_missing_conditions': gaps.most_common(12),
            'missing_condition_data_mentions': dict(data_mentions), 'wait_examples': examples,
            'data_mentions_are_review_candidates_not_proof_wait_is_wrong': True,
            'open_proposals_are_not_order_acceptance_or_fill_proof': True}


def review_directory(directory):
    directory = Path(directory).resolve()
    registration = json.loads((directory / 'research-plan.json').read_text(encoding='utf-8'))
    plan = registration['plan']
    if registration['plan_sha256'] != digest(plan):
        raise ValueError('WAIT_CAUSE_PLAN_HASH_INVALID')
    if any(w['partition'] != 'optimization' for w in plan['pilot_windows']):
        raise ValueError('WAIT_CAUSE_REVIEW_OPTIMIZATION_ONLY')
    records = {t['template_id']: [] for t in plan['templates']}
    for window in plan['pilot_windows']:
        target = (directory / window['id']).resolve()
        if target.parent != directory:
            raise ValueError('WAIT_CAUSE_PATH_OUTSIDE_EXPERIMENT')
        database = target / 'results.sqlite3'
        if not database.exists():
            continue
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')  # decisions and evidence from the same read snapshot
            for source in db.execute('SELECT * FROM ai_template_replay_decisions ORDER BY as_of,template_id'):
                row = dict(source)
                ctx = json.loads(row['context_json'] or '{}')
                record = {'status': row['status'], 'as_of': row['as_of'], 'window': window['id']}
                if row['status'] == 'COMPLETED':
                    bundle = db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?',
                                        (ctx['evidence_bundle_id'],)).fetchone()
                    if bundle is None:
                        raise ValueError('WAIT_CAUSE_MODEL_EVIDENCE_MISSING')
                    evidence = json.loads(bundle[0])
                    record.update(decision=json.loads(row['decision_json']),
                                  account=evidence['prompt_inputs']['account_truth'])
                records[row['template_id']].append(record)
    return {'scope': 'READ_ONLY_OPTIMIZATION_MODEL_GAP_REVIEW_NOT_PROFIT_ACCEPTANCE',
            'observed_at': datetime.now(timezone.utc).isoformat(), 'plan_sha256': registration['plan_sha256'],
            'strategies': {key: summarize(rows) for key, rows in records.items()},
            'production_strategy_writes': 0, 'private_exchange_calls': 0,
            'profit_acceptance_passed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    result = review_directory(args.directory)
    (args.directory / 'wait-cause-review.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'scope': result['scope'], 'strategies': {key: {k:v for k,v in value.items()
        if k in ('flat_account_scans', 'flat_account_open_proposal_fraction', 'scans_with_system_position_or_pending_entry',
                 'missing_condition_data_mentions')} for key, value in result['strategies'].items()}}))


if __name__ == '__main__':
    main()
