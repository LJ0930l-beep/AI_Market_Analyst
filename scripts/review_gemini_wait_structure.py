"""Actual visible WAIT structure; descriptive, never an entry signal."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest
from core.replay.ai_template_runner import frozen_source_fingerprint
from scripts.verify_ai_template_replay import audit_effective_request

PATTERNS = {'confirmation':r'二次|确认|企稳|confirmation|retest',
    'trend_conflict':r'冲突|背离|conflict', 'cost_or_reward':r'手续费|盈亏比|fee|reward',
    'funding':r'资金费|funding','oi':r'未平仓|(?<![a-z0-9])oi(?![a-z0-9])','news':r'新闻|news'}
GROUP_COLUMNS=['window','template_id','account_scope','15m_bos','15m_retest','1h_bos','1h_retest','count']
MENTION_COLUMNS=['window','template_id','label','count']
MARKET_COLUMNS=['window','template_id','instrument_id','selected','15m_status','15m_bos',
                '15m_retest','1h_status','1h_bos','1h_retest','count']

def visible_markets(visible):
    """All allowed instruments from the actual request, never inferred availability."""
    allowed=visible['allowed_instruments']
    if (not isinstance(allowed,list) or not allowed or any(not isinstance(x,str) or not x for x in allowed)
        or len(set(allowed))!=len(allowed)):
        raise ValueError('WAIT_STRUCTURE_ALLOWED_MARKETS_INVALID')
    technical=visible.get('technical_context',{})
    result=[]
    for symbol in sorted(allowed):
        data=technical.get(symbol)
        if data is not None and not isinstance(data,dict):
            raise ValueError('WAIT_STRUCTURE_MARKET_CONTEXT_INVALID')
        frames=(data or {}).get('timeframes',{})
        if not isinstance(frames,dict):raise ValueError('WAIT_STRUCTURE_MARKET_FRAMES_INVALID')
        row=[symbol]
        for timeframe in ('15m','1h'):
            frame=frames.get(timeframe,{})
            if not isinstance(frame,dict):raise ValueError('WAIT_STRUCTURE_MARKET_FRAME_INVALID')
            summary=frame.get('price_action')
            if summary is not None and not isinstance(summary,dict):
                raise ValueError('WAIT_STRUCTURE_MARKET_SUMMARY_INVALID')
            row.extend([str(summary.get('status') or 'UNKNOWN') if summary is not None else 'ABSENT',
                        structure_state((summary or {}).get('bos')),
                        structure_state((summary or {}).get('breakout_retest'))])
        result.append(row)
    return result

def structure_state(value):
    if value is None:return 'ABSENT'
    if not isinstance(value,dict):raise ValueError('WAIT_STRUCTURE_EVENT_TYPE_INVALID')
    return str(value.get('state') or 'PRESENT_WITHOUT_STATE')

def describe_wait(decision,visible):
    symbol=decision.get('instrument_id')
    if symbol not in visible['allowed_instruments']:
        raise ValueError('WAIT_STRUCTURE_VISIBLE_INSTRUMENT_REQUIRED')
    truth=visible['account_truth']
    owned=[p for p in truth.get('managed_positions') or [] if p.get('ownership')=='VERIFIED_SYSTEM']
    owned += [o for o in truth.get('owned_entry_orders') or []
        if o.get('ownership')=='SYSTEM_ORDER_ID_MATCH' and o.get('order_id')]
    account_scope=('UNKNOWN_ACCOUNT' if truth.get('status')!='AVAILABLE' else
        'SYSTEM_MANAGED' if owned else 'SYSTEM_FLAT')
    frames=visible.get('technical_context',{}).get(symbol,{}).get('timeframes',{})
    states=[]
    for frame in ('15m','1h'):
        summary=frames.get(frame,{}).get('price_action',{})
        states.extend(structure_state(summary.get(event)) for event in ('bos','breakout_retest'))
    gaps=decision.get('strategy_analysis',{}).get('missing_conditions') or []
    if not isinstance(gaps,list) or any(not isinstance(x,str) for x in gaps):
        raise ValueError('WAIT_STRUCTURE_GAP_LIST_INVALID')
    text=' '.join(gaps)
    mentions=[label for label,pattern in PATTERNS.items() if re.search(pattern,text,re.I)]
    return account_scope,states,mentions

def review_directory(directory):
    directory=Path(directory).resolve()
    registration=json.loads((directory/'research-plan.json').read_text(encoding='utf-8'))
    plan=registration['plan']
    if digest(plan)!=registration['plan_sha256']:raise ValueError('WAIT_STRUCTURE_PLAN_HASH_INVALID')
    if any(w['partition']!='optimization' for w in plan['pilot_windows']):
        raise ValueError('WAIT_STRUCTURE_OPTIMIZATION_ONLY')
    if plan['source_sha256']!=frozen_source_fingerprint():
        raise ValueError('WAIT_STRUCTURE_ORIGINAL_SOURCE_REQUIRED')
    records=[];groups=Counter();mentions=Counter();market_groups=Counter();windows=[]
    for window in plan['pilot_windows']:
        path=(directory/window['id']).resolve()
        if path.parent!=directory:raise ValueError('WAIT_STRUCTURE_WINDOW_PATH_INVALID')
        database=path/'results.sqlite3'
        if not database.exists():
            windows.append({'window':window['id'],'row_statuses':{},'run_state':'NOT_STARTED'})
            continue
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            rows=list(db.execute('SELECT * FROM ai_template_replay_decisions ORDER BY as_of,template_id'))
            statuses=Counter(r['status'] for r in rows)
            windows.append({'window':window['id'],'row_statuses':dict(statuses),
                'run_state':db.execute('SELECT status FROM ai_template_replay_runs').fetchone()[0]})
            def bundle(identity):
                item=db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?',(identity,)).fetchone()
                if item is None:raise ValueError('WAIT_STRUCTURE_REQUEST_BUNDLE_MISSING')
                return json.loads(item[0])
            for row in rows:
                if row['status']!='COMPLETED':continue
                decision=json.loads(row['decision_json'])
                if decision['action']!='WAIT':continue
                ctx=json.loads(row['context_json']);original=bundle(ctx['evidence_bundle_id'])
                effective=audit_effective_request(ctx,original,bundle)
                wrapper=json.loads(effective['messages'][1]['content'])
                visible=wrapper['inputs'] if 'inputs' in wrapper else wrapper
                scope,states,labels=describe_wait(decision,visible)
                markets=visible_markets(visible)
                market_groups.update((window['id'],row['template_id'],market[0],
                    market[0]==decision['instrument_id'],*market[1:]) for market in markets)
                groups[(window['id'],row['template_id'],scope,*states)]+=1
                mentions.update((window['id'],row['template_id'],label) for label in labels)
                records.append({'window':window['id'],'template_id':row['template_id'],'scan_key':row['scan_key'],
                    'as_of':row['as_of'],'instrument_id':decision['instrument_id'],
                    'actual_effective_request_hash':effective['request_hash'],'account_scope':scope,
                    'visible_structure_states':states,'visible_market_states':markets,
                    'allowed_instruments':list(visible['allowed_instruments']),
                    'model_declared_missing_conditions':decision['strategy_analysis']['missing_conditions']})
    return {'schema_version':'actual_wait_structure_review_v1','plan_sha256':registration['plan_sha256'],
        'scope':'ACTUAL_FINAL_VISIBLE_WAIT_INPUTS_NOT_CAUSAL_SETUP_OR_PROFIT_ACCEPTANCE',
        'windows':windows,'wait_records_checked':len(records),
        'group_columns':GROUP_COLUMNS,
        'group_rows':[[*key,count] for key,count in sorted(groups.items())],
        'mention_columns':MENTION_COLUMNS,
        'mention_rows':[[*key,count] for key,count in sorted(mentions.items())],
        'market_group_columns':MARKET_COLUMNS,
        'market_group_rows':[[*key,count] for key,count in sorted(market_groups.items())],
        'market_coverage_does_not_prove_model_comparison_or_trade_opportunity':True,
        'mentions_overlap_not_additive':True,'records':records,'profit_acceptance_passed':False,
        'account_scope_definition':'SYSTEM_OWNED_POSITION_OR_ENTRY_ORDER_NOT_ALL_ACCOUNT_EXPOSURE',
        'active_retest_does_not_prove_full_setup_or_required_entry':True}

def candidate_summary(report,plan_sha256):
    if report.get('plan_sha256')!=plan_sha256:
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_PLAN_MISMATCH')
    if report.get('schema_version')!='actual_wait_structure_review_v1':
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_VERSION_INVALID')
    if report['group_columns']!=GROUP_COLUMNS or report['mention_columns']!=MENTION_COLUMNS:
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_COLUMNS_CHANGED')
    if report.get('mentions_overlap_not_additive') is not True:
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_OVERLAP_CAVEAT_REQUIRED')
    if report.get('account_scope_definition')!='SYSTEM_OWNED_POSITION_OR_ENTRY_ORDER_NOT_ALL_ACCOUNT_EXPOSURE':
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_ACCOUNT_SCOPE_REQUIRED')
    groups=report['group_rows'];records=report['records']
    if (report['wait_records_checked']!=len(records)
        or any(len(row)!=8 or type(row[-1]) is not int or row[-1]<=0 for row in groups)
        or sum(row[-1] for row in groups)!=len(records)):
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_DENOMINATOR_INVALID')
    expected_groups=Counter();expected_mentions=Counter();identities=set()
    for row in records:
        identity=(row['window'],row['template_id'],row['scan_key'])
        if identity in identities:raise ValueError('WAIT_STRUCTURE_CANDIDATE_DUPLICATE_SCAN')
        identities.add(identity)
        if len(row['visible_structure_states'])!=4:
            raise ValueError('WAIT_STRUCTURE_CANDIDATE_STATE_DIMENSIONS_INVALID')
        expected_groups[(row['window'],row['template_id'],row['account_scope'],
            *row['visible_structure_states'])]+=1
        text=' '.join(row['model_declared_missing_conditions'])
        expected_mentions.update((row['window'],row['template_id'],label)
            for label,pattern in PATTERNS.items() if re.search(pattern,text,re.I))
    if groups!=[[*key,count] for key,count in sorted(expected_groups.items())]:
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_GROUPS_CHANGED')
    if report['mention_rows']!=[[*key,count] for key,count in sorted(expected_mentions.items())]:
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_MENTIONS_CHANGED')
    if report.get('active_retest_does_not_prove_full_setup_or_required_entry') is not True:
        raise ValueError('WAIT_STRUCTURE_CANDIDATE_CAUSAL_CAVEAT_REQUIRED')
    summary={k:report[k] for k in ('windows','wait_records_checked','group_columns','group_rows',
        'mention_columns','mention_rows','mentions_overlap_not_additive',
        'account_scope_definition',
        'active_retest_does_not_prove_full_setup_or_required_entry')}
    if 'market_group_rows' in report or any('visible_market_states' in r for r in records):
        if report.get('market_group_columns')!=MARKET_COLUMNS:
            raise ValueError('WAIT_STRUCTURE_MARKET_COLUMNS_CHANGED')
        if report.get('market_coverage_does_not_prove_model_comparison_or_trade_opportunity') is not True:
            raise ValueError('WAIT_STRUCTURE_MARKET_CAVEAT_REQUIRED')
        rebuilt=Counter()
        for row in records:
            allowed=row.get('allowed_instruments')
            markets=row.get('visible_market_states')
            if (not isinstance(allowed,list) or not allowed or any(not isinstance(x,str) or not x for x in allowed)
                or len(set(allowed))!=len(allowed)
                or row['instrument_id'] not in allowed or not isinstance(markets,list)
                or any(not isinstance(m,list) or len(m)!=7 or any(not isinstance(x,str) for x in m) for m in markets)
                or [m[0] for m in markets]!=sorted(allowed)):
                raise ValueError('WAIT_STRUCTURE_MARKET_COVERAGE_CHANGED')
            rebuilt.update((row['window'],row['template_id'],market[0],
                            market[0]==row['instrument_id'],*market[1:]) for market in markets)
        actual=report['market_group_rows']
        if (any(len(r)!=11 or type(r[3]) is not bool or type(r[-1]) is not int or r[-1]<=0 for r in actual)
            or actual!=[[*key,count] for key,count in sorted(rebuilt.items())]):
            raise ValueError('WAIT_STRUCTURE_MARKET_GROUPS_CHANGED')
        summary.update({k:report[k] for k in ('market_group_columns','market_group_rows',
            'market_coverage_does_not_prove_model_comparison_or_trade_opportunity')})
        # Preserve every distinct model-declared condition, with observed
        # structure states. Frequencies are per scan, not causal trade labels.
        conditions=Counter()
        for row in records:
            conditions.update((row['window'],row['template_id'],row['account_scope'],row['instrument_id'],
                *row['visible_structure_states'],gap) for gap in set(row['model_declared_missing_conditions']))
        summary['missing_condition_fact_columns']=['window','template_id','account_scope','instrument_id',
            '15m_bos','15m_retest','1h_bos','1h_retest','model_declared_condition','scan_count']
        summary['missing_condition_fact_rows']=[[*key,count] for key,count in sorted(conditions.items())]
        summary['missing_condition_frequencies_overlap_not_additive']=True
    return summary

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',required=True,type=Path)
    args=parser.parse_args();report=review_directory(args.directory)
    (args.directory/'wait-structure-review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('records','group_rows','mention_rows','market_group_rows')}))
if __name__=='__main__':main()
