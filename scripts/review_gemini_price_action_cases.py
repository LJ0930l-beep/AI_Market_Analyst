"""Full entry claims and parameters for every audited PA close, no model calls."""
from copy import deepcopy
from collections import Counter
import json


def compact_price_action_objects(details):
    """Losslessly share object key layouts in the full entry/holding tables.

    Every dictionary cell and nested dictionary is encoded, including a source
    dictionary that happens to contain our marker key. Arrays remain arrays;
    nulls, exact price strings, numbers and complete text remain unchanged.
    """
    result = deepcopy(details)
    shapes = []
    lookup = {}
    frequencies = Counter()
    def identity(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    def count(value):
        if isinstance(value, dict):
            key = identity(value)
            if len(key) >= 64:
                frequencies[key] += 1
            for item in value.values():
                count(item)
        elif isinstance(value, list):
            for item in value:
                count(item)
    for field in ('proposal_rows', 'position_scan_rows'):
        count(result[field])
    catalog, catalog_lookup = [], {}
    latest_shape, catalog_values = {}, {}
    def encode(value):
        if isinstance(value, dict):
            key = identity(value)
            if key in catalog_lookup:
                return '$' + str(catalog_lookup[key])
            keys = tuple(sorted(value))
            if keys not in lookup:
                lookup[keys] = len(shapes)
                shapes.append(list(keys))
            shape = lookup[keys]
            values = [encode(value[k]) for k in keys]
            encoded = {'o': [shape, *values]}
            if len(identity(encoded)) >= 32 or frequencies[key] > 1:
                previous = latest_shape.get(shape)
                if previous is not None:
                    indices = [i for i, item in enumerate(values) if item != catalog_values[previous][i]]
                    delta = {'d': [previous, indices, [values[i] for i in indices]]}
                    if len(identity(delta)) < len(identity(encoded)):
                        encoded = delta
                index = len(catalog)
                catalog_lookup[key] = index
                catalog_values[index] = values
                latest_shape[shape] = index
                catalog.append(encoded)
                return '$' + str(catalog_lookup[key])
            return encoded
        if isinstance(value, list):
            return [encode(v) for v in value]
        if isinstance(value, str) and value.startswith('$'):
            return '$' + value
        return value
    for field in ('proposal_rows', 'position_scan_rows'):
        result[field] = encode(result[field])
    delta_widths = {}
    for field, columns in (('proposal_rows', 'proposal_columns'), ('position_scan_rows', 'position_scan_columns')):
        width = len(result[columns])
        previous, patches = None, []
        for row in result[field]:
            if not isinstance(row, list) or len(row) != width:
                raise ValueError('PA_CASE_DETAILS_ROW_WIDTH_INVALID')
            indices = list(range(width)) if previous is None else [i for i in range(width) if row[i] != previous[i]]
            patches.append([indices, [row[i] for i in indices]])
            previous = row
        result[field] = patches
        delta_widths[field] = width
    result['object_shapes'] = shapes
    result['object_catalog'] = catalog
    result['object_encoding_scope'] = ['proposal_rows', 'position_scan_rows']
    result['object_reference_encoding'] = '$i=object_catalog[i]; $$...=original literal string starting $'
    result['row_delta_widths'] = delta_widths
    result['row_delta_rule'] = '[changed_indices,values] relative to previous full row; first row specifies all indices; explicit null replaces prior value'
    result['object_encoding_rule'] = '{r:i}=object_catalog[i]; {o:[shape_index,values...]} uses object_shapes keys; {d:[base_index,changed_indices,values]} patches sorted keys of decoded earlier catalog object; recursive; arrays/scalars unchanged'
    return result


def expand_price_action_objects(details):
    """Independent lossless round-trip check, after resolving shared text."""
    result = deepcopy(details)
    shapes = result.pop('object_shapes')
    catalog = result.pop('object_catalog')
    if (result.pop('object_encoding_scope') != ['proposal_rows', 'position_scan_rows']
            or not isinstance(catalog, list) or not isinstance(shapes, list) or any(not isinstance(s, list)
                or any(not isinstance(k, str) for k in s) or len(set(s)) != len(s) for s in shapes)):
        raise ValueError('PA_CASE_DETAILS_OBJECT_TABLE_INVALID')
    result.pop('object_encoding_rule')
    short_refs = result.pop('object_reference_encoding', None)
    if short_refs not in (None, '$i=object_catalog[i]; $$...=original literal string starting $'):
        raise ValueError('PA_CASE_DETAILS_OBJECT_REFERENCE_ENCODING_INVALID')
    widths = result.pop('row_delta_widths', None)
    result.pop('row_delta_rule', None)
    if widths is not None and (not isinstance(widths, dict)
            or set(widths) != {'proposal_rows', 'position_scan_rows'}):
        raise ValueError('PA_CASE_DETAILS_DELTA_WIDTH_INVALID')
    for field in ('proposal_rows', 'position_scan_rows') if widths is not None else ():
        width = widths.get(field)
        if type(width) is not int or width != len(result[field.replace('_rows', '_columns')]):
            raise ValueError('PA_CASE_DETAILS_DELTA_WIDTH_INVALID')
        rows, previous = [], None
        for patch in result[field]:
            if (not isinstance(patch, list) or len(patch) != 2 or not isinstance(patch[0], list)
                    or not isinstance(patch[1], list) or len(patch[0]) != len(patch[1])):
                raise ValueError('PA_CASE_DETAILS_DELTA_ROW_INVALID')
            indices, values = patch
            if (any(type(i) is not int or not 0 <= i < width for i in indices)
                    or indices != sorted(set(indices))
                    or previous is None and indices != list(range(width))):
                raise ValueError('PA_CASE_DETAILS_DELTA_INDICES_INVALID')
            row = [None] * width if previous is None else deepcopy(previous)
            for i, value in zip(indices, values):
                row[i] = value
            rows.append(row)
            previous = row
        result[field] = rows
    decoded_catalog = {}
    def decode(value, limit=None):
        if limit is None:
            limit = len(catalog)
        if short_refs is not None and isinstance(value, str) and value.startswith('$'):
            if value.startswith('$$'):
                return value[1:]
            digits = value[1:]
            if not digits or any(c not in '0123456789' for c in digits) or (len(digits) > 1 and digits[0] == '0'):
                raise ValueError('PA_CASE_DETAILS_OBJECT_CATALOG_REFERENCE_INVALID')
            return decode({'r': int(digits)}, limit)
        if isinstance(value, dict):
            if set(value) == {'r'}:
                index = value['r']
                if type(index) is not int or not 0 <= index < limit:
                    raise ValueError('PA_CASE_DETAILS_OBJECT_CATALOG_REFERENCE_INVALID')
                if index not in decoded_catalog:
                    decoded_catalog[index] = decode(catalog[index], index)
                return deepcopy(decoded_catalog[index])
            if set(value) == {'d'}:
                delta = value['d']
                if (not isinstance(delta, list) or len(delta) != 3
                        or type(delta[0]) is not int or not 0 <= delta[0] < limit
                        or not isinstance(delta[1], list) or not isinstance(delta[2], list)
                        or len(delta[1]) != len(delta[2])):
                    raise ValueError('PA_CASE_DETAILS_OBJECT_DELTA_INVALID')
                base = decode({'r': delta[0]}, limit)
                if not isinstance(base, dict):
                    raise ValueError('PA_CASE_DETAILS_OBJECT_DELTA_BASE_INVALID')
                keys, indices = sorted(base), delta[1]
                if (any(type(i) is not int or not 0 <= i < len(keys) for i in indices)
                        or indices != sorted(set(indices))):
                    raise ValueError('PA_CASE_DETAILS_OBJECT_DELTA_INDICES_INVALID')
                for i, item in zip(indices, delta[2]):
                    base[keys[i]] = decode(item, limit)
                return base
            values = value.get('o')
            if (set(value) != {'o'} or not isinstance(values, list) or not values
                    or type(values[0]) is not int or not 0 <= values[0] < len(shapes)
                    or len(values) != len(shapes[values[0]]) + 1):
                raise ValueError('PA_CASE_DETAILS_OBJECT_REFERENCE_INVALID')
            return {k: decode(v, limit) for k, v in zip(shapes[values[0]], values[1:])}
        if isinstance(value, list):
            return [decode(v, limit) for v in value]
        return value
    for field in ('proposal_rows', 'position_scan_rows'):
        result[field] = decode(result[field])
    return result


def price_action_case_details(casebook):
    cases = casebook['cases']
    if (casebook['case_count'] != len(cases)
            or len({c['case_id'] for c in cases}) != len(cases)
            or any(c['template_id'] != 'price_action_structure' for c in cases)):
        raise ValueError('PA_CASE_DETAILS_CASE_SET_INVALID')
    case_columns = ['casebook_index', 'window', 'instrument_id', 'side', 'net_outcome',
                    'net_pnl_usdt', 'opened_at', 'closed_at']
    fields = ['reason', 'entry_condition', 'action', 'confidence', 'order_preference',
              'entry_price', 'limit_price', 'stop_price', 'take_profit', 'take_profit_1',
              'take_profit_2', 'position_size_usdt', 'requested_leverage', 'timeframe_analysis']
    proposal_columns = ['casebook_index', 'proposal_index', *fields,
                        'visible_15m_ema_side', 'visible_1h_ema_side', 'visible_15m_atr',
                        'visible_price_action_encoding', 'visible_15m_price_action', 'visible_1h_price_action']
    management_fields = ['action', 'instrument_id', 'position_id', 'reason', 'entry_condition',
                         'timeframe_analysis', 'strategy_analysis', 'new_stop_price', 'new_take_profit', 'reduce_fraction']
    management_columns = ['casebook_index', 'scan_index', 'as_of', *management_fields,
                          'visible_15m_ema_side', 'visible_1h_ema_side', 'visible_15m_atr',
                          'visible_market_snapshot', 'visible_price_action_encoding',
                          'visible_15m_price_action', 'visible_1h_price_action',
                          'visible_position', 'decision_explicitly_targets_this_position', 'position_execution_events']
    case_rows, proposal_rows, management_rows = [], [], []
    for index, case in enumerate(cases):
        case_rows.append([index, *(case.get(k) for k in case_columns[1:])])
        proposals = case['entry_model_proposals']
        if not isinstance(proposals, dict) or not proposals:
            raise ValueError('PA_CASE_DETAILS_ENTRY_PROPOSALS_REQUIRED')
        for ordinal, proposal in enumerate(proposals.values()):
            decision = proposal['decision']
            if decision.get('reason') is not None and not isinstance(decision['reason'], str):
                raise ValueError('PA_CASE_DETAILS_REASON_INVALID')
            facts = proposal.get('effective_model_visible_entry_facts') or {}
            frames = (facts.get('technical_context') or {}).get('timeframes') or {}
            signal, context = frames.get('15m') or {}, frames.get('1h') or {}
            proposal_rows.append([index, ordinal, *(decision.get(k) for k in fields),
                signal.get('price_vs_ema20'), context.get('price_vs_ema20'),
                (signal.get('indicators') or {}).get('atr14_simple'),
                facts.get('price_action_encoding'), signal.get('price_action'), context.get('price_action')])
        for ordinal, scan in enumerate(case.get('position_visible_model_scans') or []):
            facts = scan.get('effective_model_visible_position_facts') or {}
            if facts and facts.get('instrument_id') != case['instrument_id']:
                raise ValueError('PA_CASE_DETAILS_POSITION_MARKET_IDENTITY_INVALID')
            frames = (facts.get('technical_context') or {}).get('timeframes') or {}
            signal, context = frames.get('15m') or {}, frames.get('1h') or {}
            management_rows.append([index, ordinal, scan['as_of'],
                *(scan['decision'].get(k) for k in management_fields),
                signal.get('price_vs_ema20'), context.get('price_vs_ema20'),
                (signal.get('indicators') or {}).get('atr14_simple'), facts.get('market_snapshot'),
                facts.get('price_action_encoding'), signal.get('price_action'), context.get('price_action'),
                scan['visible_position'],
                scan['decision_explicitly_targets_this_position'], scan['position_execution_events']])
    return deepcopy({'scope': 'ALL_PA_CLOSES_COMPLETE_ENTRY_CLAIMS_NOT_CAUSAL_PROOF',
        'case_columns': case_columns, 'case_rows': case_rows,
        'proposal_columns': proposal_columns, 'proposal_rows': proposal_rows,
        'position_scan_columns': management_columns, 'position_scan_rows': management_rows,
        'position_scan_count': len(management_rows),
        'position_scan_scope': 'ACTUALLY_VISIBLE_POSITION_NOT_TIME_INFERRED_OR_CAUSAL_PROOF',
        'case_count': len(cases), 'proposal_count': len(proposal_rows),
        'case_indices_reference_digest_bound_casebook': True,
        'text_truncated': False, 'unknown_projected_facts_are_null': True,
        'full_model_input_not_reproduced_in_this_projection': True})
