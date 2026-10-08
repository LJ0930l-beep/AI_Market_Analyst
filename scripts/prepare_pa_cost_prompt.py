"""Preview a cost clarification against sealed optimization inputs, no calls/writes to strategy."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.replay.ai_history import digest
from core.replay.ai_template_runner import frozen_source_fingerprint
from core.replay.relay_policy import read_relay_policy
from core.trading.ai_session_coordinator import _estimate_tokens
from scripts.verify_ai_template_replay import audit_effective_request


def proposed_seed(preparation):
    original = preparation['current_frozen_seed']
    old_heading = '价格行为 / Gemini High · 15m 价格结构：'
    if not original.startswith(old_heading):
        raise ValueError('EXPECTED_SEED_HEADING_REQUIRED')
    new = '15m价格行为：' + original[len(old_heading):] + preparation['proposed_additional_research_cost_explanation']
    if not 1 <= len(new) <= 500:
        raise ValueError('PREPARED_SEED_LENGTH_EXCEEDED')
    return new


def replaced_messages(messages, original, new):
    changed = deepcopy(messages)
    occurrences = changed[0]['content'].count(original)
    if occurrences != 1 or changed[0]['role'] != 'system':
        raise ValueError('ONE_SYSTEM_SEED_REQUIRED')
    changed[0]['content'] = changed[0]['content'].replace(original, new)
    assert changed[1:] == messages[1:]
    return changed


def prepare(directory):
    directory = Path(directory).resolve()
    def read(name): return json.loads((directory/name).read_text(encoding='utf-8'))
    registration = read('research-plan.json')
    plan = registration['plan']
    assert digest(plan) == registration['plan_sha256']
    assert plan['source_sha256'] == frozen_source_fingerprint()
    assert plan['relay_inference_policy'] == read_relay_policy()
    if any(w.get('partition') != 'optimization' for w in plan['pilot_windows']):
        raise ValueError('OPTIMIZATION_ONLY')
    preparation = read('next-freeze-cost-math-preparation.json')
    seal_path = directory/'checkpoint-0050-final-evidence.json'
    assert hashlib.sha256(seal_path.read_bytes()).hexdigest() == preparation['sealed_checkpoint_sha256']
    seal = read(seal_path.name)
    new = proposed_seed(preparation)
    checks = []
    for relative, expected in seal['consistent_sqlite_snapshots_sha256'].items():
        snapshot = (directory/relative).resolve()
        assert snapshot.is_relative_to(directory)
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == expected
        with sqlite3.connect(snapshot.as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            def load(bundle_id):
                record = db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?', (bundle_id,)).fetchone()
                if record is None: raise ValueError('EVIDENCE_MISSING')
                return json.loads(record[0])
            for row in db.execute("SELECT * FROM ai_template_replay_decisions WHERE status='COMPLETED' ORDER BY as_of"):
                context = json.loads(row['context_json'])
                actual = audit_effective_request(context, load(context['evidence_bundle_id']), load)
                projected = replaced_messages(actual['messages'], preparation['current_frozen_seed'], new)
                reserve = context['model_inference_settings']['output_token_reserve']
                estimate = sum(_estimate_tokens(m['content']) for m in projected) + reserve + 256
                if estimate > plan['application_input_budget']:
                    raise ValueError('PREPARED_COST_PROMPT_BUDGET_EXCEEDED')
                checks.append({'scan_key':row['scan_key'], 'as_of':row['as_of'],
                    'original_effective_request_hash':actual['request_hash'],
                    'projected_messages_sha256':digest(projected),
                    'estimated_total_with_output_and_safety':estimate,
                    'response_reserve':reserve,'complete_user_content_preserved':True})
    assert len(checks) == seal['checkpoint'] == 50
    assert plan['source_sha256'] == frozen_source_fingerprint()
    assert plan['relay_inference_policy'] == read_relay_policy()
    return {'status':'PREPARED_NOT_APPLIED_ARCHIVED_INPUT_BUDGET_PASS',
        'observed_at':datetime.now(timezone.utc).isoformat(),
        'plan_sha256':registration['plan_sha256'],'sealed_checkpoint_sha256':preparation['sealed_checkpoint_sha256'],
        'seed':new,'seed_characters':len(new),'checks':checks,
        'maximum_estimated_total':max(x['estimated_total_with_output_and_safety'] for x in checks),
        'native_tokenizer_attestation':False,
        'scope':'ALL_FIRST_50_SEALED_EFFECTIVE_MESSAGES_NOT_FUTURE_INPUT_OR_REAL_INFERENCE_PROOF',
        'frozen_source_modified':False,'model_calls':0,'private_exchange_calls':0,
        'production_strategy_changed':False,'formal_candidate_generated':False,
        'profit_acceptance_passed':False}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    result = prepare(args.directory)
    (Path(args.directory)/'next-freeze-cost-prompt-budget.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key:result[key] for key in ('status','seed_characters','maximum_estimated_total','model_calls')}))
