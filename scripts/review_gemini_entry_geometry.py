"""Describe all audited optimization closes; never infer causes or tune heldout."""
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest
from scripts.review_gemini_closed_costs import amount


def summary(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {'count': 0, 'min': None, 'median': None, 'max': None}
    n = len(values)
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    return {'count': n, 'min': str(values[0]), 'median': str(median), 'max': str(values[-1])}


def geometry_review(casebook, signal_timeframes):
    """A scenario is valid only for one unnetted fill; keep all other cases."""
    cases = casebook['cases']
    if (casebook['case_count'] != len(cases)
            or len({c['case_id'] for c in cases}) != len(cases)
            or not signal_timeframes or any(c['template_id'] not in signal_timeframes for c in cases)):
        raise ValueError('GEOMETRY_CASE_SET_INVALID')
    rows = []
    for case in cases:
        net = amount(case['net_pnl_usdt'])
        expected = 'WIN' if net > 0 else 'LOSS' if net < 0 else 'BREAKEVEN'
        if case['net_outcome'] != expected or case['side'] not in {'LONG', 'SHORT'}:
            raise ValueError('GEOMETRY_CASE_IDENTITY_INVALID')
        opened, closed = (datetime.fromisoformat(case[k].replace('Z', '+00:00')) for k in ('opened_at', 'closed_at'))
        if opened.tzinfo is None or closed.tzinfo is None or closed < opened:
            raise ValueError('GEOMETRY_TIME_INVALID')
        elapsed = closed - opened
        seconds = Decimal(elapsed.days * 86400 + elapsed.seconds) + Decimal(elapsed.microseconds) / 1000000
        risk_atr = net_rr = drift = width_pct = None
        reason = 'MULTIPLE_OR_NETTED_FILLS_WITHOUT_SINGLE_INITIAL_SCENARIO'
        scenario = case.get('initial_plan_scenario')
        if scenario is not None:
            fills, proposals = case['entry_fills'], case['entry_model_proposals']
            if len(fills) != 1 or len(proposals) != 1 or amount(fills[0].get('netted_quantity', 0)) != 0:
                raise ValueError('GEOMETRY_SINGLE_SCENARIO_BINDING_INVALID')
            fill = fills[0]
            if fill['order_id'] not in proposals:
                raise ValueError('GEOMETRY_ORDER_BINDING_INVALID')
            proposal = proposals[fill['order_id']]
            entry, stop, target, reference = (amount(scenario[k]) for k in
                ('entry_fill_price', 'initial_stop', 'initial_target', 'model_reference_entry_price'))
            if entry != amount(fill['fill_price']) or reference != amount(proposal['decision']['entry_price']):
                raise ValueError('GEOMETRY_PRICE_BINDING_INVALID')
            direction = Decimal(1) if case['side'] == 'LONG' else Decimal(-1)
            risk, reward = direction * (entry - stop), direction * (target - entry)
            if entry <= 0 or risk <= 0 or reward <= 0:
                raise ValueError('GEOMETRY_PRICE_DIRECTION_INVALID')
            stop_cost = amount(scenario['stop_loss_including_fees_usdt'])
            target_net = amount(scenario['target_net_usdt'])
            net_rr = amount(scenario['net_reward_to_loss'])
            if stop_cost <= 0 or net_rr != target_net / stop_cost:
                raise ValueError('GEOMETRY_SCENARIO_ARITHMETIC_INVALID')
            drift = amount(scenario['entry_price_change_cost_including_latency_usdt'])
            width_pct = risk / entry * 100
            facts = proposal.get('effective_model_visible_entry_facts') or {}
            context = facts.get('technical_context') or {}
            frame = context.get('timeframes', {}).get(signal_timeframes[case['template_id']], {})
            atr = frame.get('indicators', {}).get('atr14_simple')
            if atr is None:
                reason = 'MODEL_VISIBLE_SIGNAL_ATR_UNAVAILABLE'
            else:
                atr = amount(atr)
                if atr <= 0:
                    raise ValueError('GEOMETRY_ATR_INVALID')
                risk_atr = risk / atr
                reason = None
        rows.append({'case_id': case['case_id'], 'window': case['window'], 'template_id': case['template_id'],
            'net_outcome': expected, 'net_pnl_usdt': str(net), 'hold_seconds': str(seconds),
            'initial_stop_width_pct': str(width_pct) if width_pct is not None else None,
            'initial_stop_distance_over_visible_signal_atr': str(risk_atr) if risk_atr is not None else None,
            'initial_fee_only_reward_to_loss': str(net_rr) if net_rr is not None else None,
            'entry_change_cost_including_latency_usdt': str(drift) if drift is not None else None,
            'unavailable_reason': reason})
    columns = ['template_id', 'net_outcome', 'closed_count', 'net_pnl_usdt', 'initial_scenario_count',
               'hold_seconds', 'stop_width_pct', 'stop_distance_over_visible_signal_atr',
               'initial_fee_only_reward_to_loss', 'adverse_entry_change_count', 'unavailable_counts']
    aggregate_rows = []
    for identity in signal_timeframes:
        for outcome in ('WIN', 'LOSS', 'BREAKEVEN'):
            selected = [r for r in rows if r['template_id'] == identity and r['net_outcome'] == outcome]
            def numbers(key):
                return [amount(r[key]) if r[key] is not None else None for r in selected]
            aggregate_rows.append([identity, outcome, len(selected), str(sum(numbers('net_pnl_usdt'), Decimal(0))),
                sum(r['initial_fee_only_reward_to_loss'] is not None for r in selected),
                summary(numbers('hold_seconds')), summary(numbers('initial_stop_width_pct')),
                summary(numbers('initial_stop_distance_over_visible_signal_atr')),
                summary(numbers('initial_fee_only_reward_to_loss')),
                sum(v is not None and v > 0 for v in numbers('entry_change_cost_including_latency_usdt')),
                dict(Counter(r['unavailable_reason'] for r in selected if r['unavailable_reason']))])
    return {'scope': 'ALL_AUDITED_OPTIMIZATION_CLOSES_DESCRIPTIVE_NOT_CAUSAL_OR_PROFIT_ACCEPTANCE',
        'plan_sha256': casebook['plan_sha256'], 'casebook_sha256': digest(casebook),
        'case_count': len(cases), 'case_rows': rows, 'aggregate_columns': columns, 'aggregate_rows': aggregate_rows,
        'signal_timeframes': signal_timeframes, 'scenario_excludes_exit_slippage_funding_and_later_updates': True,
        'atr_is_only_model_visible_at_entry_decision_not_future_fill_time_atr': True,
        'entry_change_includes_latency_not_pure_exchange_slippage': True,
        'thresholds_are_not_live_rules': True, 'production_strategy_writes': 0, 'private_exchange_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    from scripts.build_gemini_trade_casebook import build_directory
    directory = args.directory.resolve()
    plan = json.loads((directory / 'research-plan.json').read_text(encoding='utf-8'))['plan']
    casebook = build_directory(directory)  # Independently audited, refuses heldout inputs.
    timeframes = {t['template_id']: t['profile']['signal_timeframe'] for t in plan['templates']}
    result = geometry_review(casebook, timeframes)
    result['casebook_artifact'] = 'entry-geometry-casebook.json'
    (directory / 'entry-geometry-casebook.json').write_text(
        json.dumps(casebook, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'entry-geometry-review.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('scope', 'case_count', 'aggregate_columns', 'aggregate_rows')}))


if __name__ == '__main__':
    main()
