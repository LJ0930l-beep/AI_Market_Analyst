"""One real Gemini repair of an archived proposal; no order submission."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.ai.ollama import OllamaProvider
from core.model_routing import DEFAULT_MODEL, is_verified_model_receipt
from core.trading.model_schemas import (frozen_decision_schema, validate_schema,
    require_nofx_gate_open_contract, gate_open_schema_repair_fields, pin_gate_repair_trade_fields)
from core.trading.ai_session_coordinator import _estimate_tokens, _fit_prompt_payload
from core.model_client import model_client, ModelClientError

VERSION = 'archived_gate_open_contract_probe_v2_preserve_selected_symbol'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro', uri=True) as db:
        for raw_context, in db.execute("SELECT context_json FROM ai_template_replay_decisions WHERE status='ERROR' ORDER BY as_of,template_id"):
            context = json.loads(raw_context)
            attempts = context.get('model_inference_settings', {}).get('model_response_audit', {}).get('attempts', [])
            if not attempts:
                continue
            first = attempts[0]
            try:
                proposal = json.loads(first.get('raw_response') or '{}')
            except json.JSONDecodeError:
                continue
            fields = gate_open_schema_repair_fields(proposal, first.get('validation_error'), context['allowed_instruments'])
            if fields:
                bundle = json.loads(db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?', (context['evidence_bundle_id'],)).fetchone()[0])
                break
        else:
            raise ValueError('NO_RECOGNIZABLE_ARCHIVED_OPEN')
    inputs = json.loads(bundle['prompt_request']['messages'][1]['content'])
    schema = deepcopy(bundle['prompt_request']['local_validation_schema'])
    schema.pop('anyOf', None)
    schema['properties']['action']['enum'] = [proposal['action']]
    schema['properties']['instrument_id']['enum'] = [proposal['instrument_id']]
    concrete = {'confidence':'number', 'entry_price':'number', 'stop_price':'number', 'take_profit':'number',
        'position_size_usdt':'number', 'requested_leverage':'integer', 'order_preference':'string', 'evidence_refs':'array'}
    for field, kind in concrete.items():
        schema['properties'][field]['type'] = kind
        if field not in schema['required']:
            schema['required'].append(field)
    schema['properties']['order_preference']['enum'] = ['LIMIT', 'MARKET']
    schema = pin_gate_repair_trade_fields(schema, proposal, fields)
    system = bundle['prompt_request']['messages'][0]['content'] + '\n\n' + (
        '你只修复历史开仓JSON，不提交订单。保持previous_decision已有动作、标的、入场、止损、止盈及已有交易值。'
        '仅补齐mutable_fields及缺失的可见引用；依据inputs的历史账户与行情自主决定名义金额和杠杆。'
        'position_size_usdt是名义金额、requested_leverage是整数、order_preference只选LIMIT或MARKET。'
        'evidence_refs必须来自inputs；ttl_seconds若填写只能是60到1800的整数。只输出符合schema的JSON。')
    wrapper = {'validation_error':str(first.get('validation_error') or '')[:240],
               'previous_decision':proposal, 'mutable_fields':sorted(fields), 'inputs':inputs}
    tokenizer_available = {'value':True}
    def count(content):
        try:
            return model_client.count_tokens(content, timeout_sec=5.0)
        except ModelClientError:
            tokenizer_available['value'] = False
            return _estimate_tokens(content)
    overhead = count(json.dumps({**wrapper, 'inputs':{}}, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    required = {proposal['instrument_id']}
    required.update(ref.split(':',2)[1] for ref in proposal.get('evidence_refs') or []
        if isinstance(ref,str) and ref.startswith(('market_snapshot:', 'technical_snapshot:')))
    fitted, compaction = _fit_prompt_payload(inputs, system, 8192-overhead-32, reserve=1024,
        token_counter=count, required_symbols=tuple(sorted(required)))
    if not tokenizer_available['value']:
        compaction['tokenizer'] = 'CONSERVATIVE_ESTIMATE'
    wrapper['inputs'] = fitted
    schema = frozen_decision_schema(fitted, schema, gate_wait=True)
    messages = [{'role':'system', 'content':system},
                {'role':'user', 'content':json.dumps(wrapper,ensure_ascii=False,sort_keys=True,separators=(',', ':'))}]
    tokens = sum(_estimate_tokens(m['content']) for m in messages)
    if tokens+1024+256 > 8192:
        raise ValueError('PROBE_INPUT_BUDGET_EXCEEDED')
    request_hash = hashlib.sha256(json.dumps(messages,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    started = time.monotonic()
    decision, raw, receipt = OllamaProvider(max_tokens=1024,retries=0).generate_json(messages,
        model_name=DEFAULT_MODEL,prompt_version=VERSION,input_hash=request_hash,schema=schema,
        max_tokens=1024,reasoning_effort='high',allow_syntax_repair=False)
    validate_schema(decision, schema)
    require_nofx_gate_open_contract(decision)
    if any(decision.get(k) != v for k,v in proposal.items() if k in (
        'action','instrument_id','entry_price','stop_price','take_profit','position_size_usdt',
        'requested_leverage','order_preference','limit_price','ttl_seconds','requested_risk_fraction',
        'candidate_id','strategy_candidate_id') and k not in fields):
        raise ValueError('PROBE_REPAIR_CHANGED_ORIGINAL_TRADE')
    if not is_verified_model_receipt(receipt,expected_prompt_version=VERSION,expected_input_hash=request_hash):
        raise ValueError('PROBE_RECEIPT_UNVERIFIED')
    result = {'status':'REAL_COMPLETION_CONTRACT_PASS_NOT_ORDER_ACCEPTANCE','source_cycle_id':context['cycle_id'],
              'source_database':str(args.database.resolve()),'previous_decision':proposal,
              'decision':decision,'receipt':receipt,'raw_response':raw,
              'elapsed_seconds':time.monotonic()-started,'estimated_input_tokens':tokens,
              'repair_compaction':compaction,
              'private_exchange_calls':0,'orders_submitted':0,'profit_evidence':False}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('status','elapsed_seconds','private_exchange_calls','orders_submitted')},ensure_ascii=True))


if __name__ == '__main__':
    main()
