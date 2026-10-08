"""Chronological ten-close diagnostics, not future win guarantees."""
from collections import defaultdict
from decimal import Decimal
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest


def ten_trade_quality(casebook):
    cases = casebook['cases']
    if casebook['case_count'] != len(cases) or len({c['case_id'] for c in cases}) != len(cases):
        raise ValueError('TEN_TRADE_CASE_SET_INVALID')
    groups = defaultdict(list)
    unavailable = 0
    for case in cases:
        net = Decimal(str(case['net_pnl_usdt']))
        if not net.is_finite():
            raise ValueError('TEN_TRADE_NONFINITE_NET')
        # Older casebooks without window identity cannot be pooled into a fake
        # continuous account sequence. Keep their denominator explicit.
        if not case.get('window'):
            unavailable += 1
            continue
        point = datetime.fromisoformat(case['closed_at'].replace('Z', '+00:00'))
        if point.utcoffset() is None:
            raise ValueError('TEN_TRADE_TIMEZONE_REQUIRED')
        groups[(case['template_id'], case['window'])].append((point, case['case_id'], net))
    rows = []
    for (template, window), values in sorted(groups.items()):
        values.sort()
        # Every overlapping sequence is diagnostic, never independent samples.
        sequences = [values[i:i+10] for i in range(max(0,len(values)-9))]
        block_values = [values[i:i+10] for i in range(0,len(values)-9,10)]
        def passed(sequence):
            return sum(net > 0 for _,_,net in sequence) >= 6
        wins = [net for _,_,net in values if net>0]
        losses = [net for _,_,net in values if net<0]
        rows.append({'template_id':template,'window':window,'closed_positions':len(values),
            'consecutive_ten_sequences':len(sequences),
            'sequences_with_at_least_six_net_wins':sum(passed(s) for s in sequences),
            'nonoverlapping_ten_blocks':len(block_values),
            'blocks_with_at_least_six_net_wins':sum(passed(s) for s in block_values),
            'remaining_closes_outside_complete_blocks':len(values)%10,
            'minimum_net_wins_in_ten':min((sum(n>0 for _,_,n in s) for s in sequences),default=None),
            'minimum_net_pnl_in_ten_usdt':str(min(sum(n for _,_,n in s) for s in sequences)) if sequences else None,
            'average_net_win_usdt':str(sum(wins)/len(wins)) if wins else None,
            'average_absolute_net_loss_usdt':str(-sum(losses)/len(losses)) if losses else None,
            'aggregate_net_pnl_usdt':str(sum(n for _,_,n in values)),
            'all_observed_ten_sequences_meet_six_wins':all(passed(s) for s in sequences) if sequences else None})
    return {'scope':'WITHIN_WINDOW_CLOSED_NET_ONLY_DESCRIPTIVE_NOT_PROFIT_ACCEPTANCE',
        'casebook_sha256':digest(casebook),'case_count':len(cases),
        'window_identity_unavailable_cases':unavailable,'rows':rows,
        'overlapping_sequences_are_not_independent':True,'floating_profit_excluded':True,
        'future_six_wins_in_every_ten_guaranteed':False}


def main():
    import argparse
    import json
    from pathlib import Path
    from scripts.build_gemini_trade_casebook import build_directory
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    args=parser.parse_args()
    casebook=build_directory(args.directory)
    review=ten_trade_quality(casebook)
    (args.directory/'ten-trade-quality-casebook.json').write_text(json.dumps(casebook,indent=2),encoding='utf-8')
    (args.directory/'ten-trade-quality-review.json').write_text(json.dumps(review,indent=2),encoding='utf-8')
    print(json.dumps(review))


if __name__=='__main__':
    main()
