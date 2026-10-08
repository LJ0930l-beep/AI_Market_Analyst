"""Exact closed-case cost decomposition; arithmetic is not causal attribution."""
from collections import Counter
from decimal import Decimal, InvalidOperation

from core.replay.ai_history import digest


def amount(raw):
    if isinstance(raw, bool):
        raise ValueError('CLOSED_COST_INVALID_AMOUNT')
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError('CLOSED_COST_INVALID_AMOUNT') from exc
    if not value.is_finite():
        raise ValueError('CLOSED_COST_INVALID_AMOUNT')
    return value


def summarize_closed_costs(casebook, template_ids):
    ids = tuple(template_ids)
    cases = casebook['cases']
    if (len(set(ids)) != len(ids) or casebook['case_count'] != len(cases)
            or len({c['case_id'] for c in cases}) != len(cases)
            or any(c['template_id'] not in ids for c in cases)):
        raise ValueError('CLOSED_COST_CASE_SET_INVALID')
    rows = []
    for identity in ids:
        selected = [c for c in cases if c['template_id'] == identity]
        gross = fees = funding = net = Decimal(0)
        fee_flips = gross_losses = wins = 0
        entry_types, exits = Counter(), Counter()
        for case in selected:
            g, f, u, n = (amount(case[k]) for k in
                ('gross_pnl_usdt','fees_usdt','funding_pnl_usdt','net_pnl_usdt'))
            outcome = 'WIN' if n > 0 else 'LOSS' if n < 0 else 'BREAKEVEN'
            if f < 0 or n != g-f+u or case['net_outcome'] != outcome:
                raise ValueError('CLOSED_COST_ECONOMIC_IDENTITY_INVALID')
            gross += g
            fees += f
            funding += u
            net += n
            wins += n > 0
            gross_losses += g < 0
            fee_flips += g+u > 0 and n <= 0
            for fill in case['entry_fills']:
                if fill['fee_type'] not in {'MAKER','TAKER'}:
                    raise ValueError('CLOSED_COST_UNKNOWN_FEE_TYPE')
                entry_types[fill['fee_type']] += 1
            exits.update(e['action'] for e in case['exit_ledger_triggers'])
        rows.append([identity,len(selected),wins,gross_losses,fee_flips,
                     str(gross),str(fees),str(funding),str(net),dict(entry_types),dict(exits)])
    return {'scope':'CLOSED_CASE_ACCOUNTING_NOT_CAUSAL_OR_ANNUAL_PROFIT_PROOF',
        'casebook_sha256':digest(casebook),'case_count':len(cases),
        'columns':['template_id','closed_count','net_wins','gross_loss_count',
                   'positive_before_fees_nonpositive_after_fees_count','gross_pnl_usdt',
                   'fees_usdt','funding_pnl_usdt','net_pnl_usdt',
                   'entry_fill_fee_type_counts','exit_fill_trigger_counts'],
        'rows':rows,'fill_counts_are_not_position_counts':True,
        'zero_sample_is_not_profit_evidence':True}
