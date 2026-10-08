"""Review sealed optimization WAIT evidence; not a formal strategy candidate."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.ai.ollama import OllamaProvider
from core.model_client import ModelClient
from core.replay.ai_history import digest
from core.replay.ai_template_runner import frozen_source_fingerprint
from core.replay.relay_policy import read_relay_policy
from core.trading.ai_session_coordinator import _estimate_tokens
from core.trading.model_schemas import validate_schema
from scripts.review_gemini_wait_structure import candidate_summary
from scripts.verify_ai_template_replay import audit_effective_request


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wire_digest(messages):
    # CompletionTrace hashes ASCII-escaped sorted JSON. Replay plan digests
    # use Unicode JSON and are intentionally a different representation.
    return hashlib.sha256(json.dumps({'messages': messages}, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def utc_point(value):
    point = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if point.tzinfo is None:
        raise ValueError('RETEST_EVENT_AWARE_TIME_REQUIRED')
    return point.astimezone(timezone.utc)


def retest_candle_fact(frame, as_of):
    event = frame['price_action'].get('breakout_retest')
    if not isinstance(event, dict) or event.get('state') != 'ACTIVE':
        return None
    point = utc_point(as_of)
    known = utc_point(event['confirmed_at'])
    event_at = utc_point(event['bar_at'])
    last_closed = utc_point(frame['last_closed_at'])
    if max(known, event_at, last_closed) > point:
        raise ValueError('RETEST_EVENT_NOT_YET_KNOWN')
    if event_at != last_closed:
        return {'current_candle_is_event_candle': False,
                'retest_event': event,
                'event_candle_geometry_not_inferred_from_latest_candle': True}
    candles = frame.get('candles')
    if not isinstance(candles, list) or not candles or len(candles[-1]) < 4:
        raise ValueError('RETEST_EVENT_CANDLE_REQUIRED')
    opening, high, low, close = candles[-1][:4]
    values = (opening, high, low, close, event['level'])
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError('RETEST_EVENT_NUMERIC_GEOMETRY_REQUIRED')
    if low > min(opening, close) or high < max(opening, close) or low > high:
        raise ValueError('RETEST_EVENT_OHLC_INVALID')
    level = event['level']
    side = event['side']
    if side not in ('LONG', 'SHORT'):
        raise ValueError('RETEST_EVENT_SIDE_REQUIRED')
    return {
        'current_candle_is_event_candle': True, 'retest_event': event,
        'ohlc': {'open': opening, 'high': high, 'low': low, 'close': close},
        'range_touched_level': low <= level <= high,
        'closed_on_breakout_side': close >= level if side == 'LONG' else close <= level,
        'body_direction': 'UP' if close > opening else 'DOWN' if close < opening else 'FLAT',
        'known_at_decision': True,
        'not_a_complete_setup_or_required_trade': True,
    }


def build_payload(directory):
    registration = read(directory / 'research-plan.json')
    proof = read(directory / 'checkpoint-0050-final-evidence.json')
    plan = registration['plan']
    if digest(plan) != registration['plan_sha256'] or proof['plan_sha256'] != registration['plan_sha256']:
        raise ValueError('SEALED_PLAN_BINDING_REQUIRED')
    if any(w['partition'] != 'optimization' for w in plan['pilot_windows']):
        raise ValueError('OPTIMIZATION_ONLY')
    if plan['source_sha256'] != frozen_source_fingerprint() or plan['relay_inference_policy'] != read_relay_policy():
        raise ValueError('ORIGINAL_SOURCE_AND_RELAY_REQUIRED')
    for name, expected in proof['preserved_artifact_sha256'].items():
        if sha(directory / name) != expected:
            raise ValueError('SEALED_ARTIFACT_CHANGED')
    wait_path = directory / 'checkpoint-0050-preserved-wait-structure-review.json'
    report = read(wait_path)
    summary = candidate_summary(report, registration['plan_sha256'])
    selected = {(row['window'], row['scan_key']) for row in report['records']
                if row['visible_structure_states'][1] == 'ACTIVE'}
    cases = []
    for name, expected in proof['consistent_sqlite_snapshots_sha256'].items():
        snapshot = directory / name
        if sha(snapshot) != expected:
            raise ValueError('SEALED_SQLITE_CHANGED')
        with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            def bundle(identity):
                row = db.execute('SELECT payload_json FROM evidence_bundles WHERE bundle_id=?', (identity,)).fetchone()
                if row is None:
                    raise ValueError('ACTUAL_REQUEST_BUNDLE_REQUIRED')
                return json.loads(row[0])
            for row in db.execute('SELECT * FROM ai_template_replay_decisions ORDER BY as_of,template_id'):
                if (snapshot.parent.name, row['scan_key']) not in selected:
                    continue
                if row['status'] != 'COMPLETED':
                    raise ValueError('COMPLETED_WAIT_REQUIRED')
                decision = json.loads(row['decision_json'])
                context = json.loads(row['context_json'])
                effective = audit_effective_request(context, bundle(context['evidence_bundle_id']), bundle)
                wrapper = json.loads(effective['messages'][1]['content'])
                visible = wrapper.get('inputs', wrapper)
                frame = visible['technical_context'][decision['instrument_id']]['timeframes']['15m']
                cases.append({
                    'case_id': row['scan_key'], 'as_of': row['as_of'], 'instrument': decision['instrument_id'],
                    'actual_request_hash': effective['request_hash'], 'price_action': frame['price_action'],
                    'actual_visible_recent_candles': frame['candles'],
                    'model_reason': decision['reason'], 'model_analysis': decision['strategy_analysis'],
                    'retest_candle_fact': retest_candle_fact(frame, row['as_of']),
                })
    if len(cases) != len(selected):
        raise ValueError('ALL_SELECTED_ACTIVE_RETEST_WAITS_REQUIRED')
    return {
        'purpose': 'IN_PROGRESS_OPTIMIZATION_HYPOTHESIS_NOT_FORMAL_CANDIDATE_OR_PROFIT_ACCEPTANCE',
        'current_terminal_economics': proof['economic_summary'],
        'current_complete_execution_audit': proof['full_native_protocol_status'],
        'all_wait_facts': summary, 'all_selected_active_retest_wait_cases': cases,
        'selection': 'ALL_COMPLETED_WAIT_ROWS_WITH_SELECTED_15M_RETEST_ACTIVE_NOT_OUTCOME_SELECTED',
        'frozen_instructions': plan['templates'][0]['sections'],
        'source_bindings': {'plan_sha256': registration['plan_sha256'], 'checkpoint_sha256': sha(directory / 'checkpoint-0050-final-evidence.json'),
                            'wait_sha256': sha(wait_path), 'sqlite_sha256': proof['consistent_sqlite_snapshots_sha256']},
        'boundaries': ['No heldout/final data', 'No private account inputs',
            'Error denominator retained', 'ACTIVE and touch-close are facts, not complete setups or mandatory entries',
            'No future profit guarantee or mechanical stop/RR gates', 'No live activation'],
    }


def prepare_request(payload):
    case_ids = [case['case_id'] for case in payload['all_selected_active_retest_wait_cases']]
    schema = {'type': 'object', 'additionalProperties': False,
              'required': ['status', 'hypotheses', 'draft_instruction', 'validation_checks'],
              'properties': {
                  'status': {'type': 'string', 'enum': ['HYPOTHESIS_ONLY_UNVALIDATED']},
                  'hypotheses': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {
                      'type': 'object', 'additionalProperties': False,
                      'required': ['case_ids', 'observation', 'alternative_explanation', 'test'],
                      'properties': {'case_ids': {'type': 'array', 'minItems': 1, 'maxItems': len(case_ids),
                                                 'items': {'type': 'string', 'enum': case_ids}},
                                     **{key: {'type': 'string', 'minLength': 1, 'maxLength': 260}
                                        for key in ('observation', 'alternative_explanation', 'test')}}}},
                  'draft_instruction': {'type': 'string', 'minLength': 1, 'maxLength': 500},
                  'validation_checks': {'type': 'array', 'minItems': 1, 'maxItems': 5,
                                        'items': {'type': 'string', 'minLength': 1, 'maxLength': 220}},
              }}
    system = (
        '你是Gemini价格行为策略研究员，只输出JSON。审查已封存的优化区等待输入，提出下一冻结研究种子的假设草案；'
        '这不是正式候选，不能进入生产或绕过全执行、独立验证和60%收益验收。'
        '49有效WAIT和1ERROR零成交，不能判盈利。6个案例是所有所选15m ACTIVE回测等待，不是6个盈利机会。'
        '先区分已知事实与未完成额外条件：回测event bar/confirmed_at及同根OHLC已经证明触价并收在破位方向时，'
        '不能说尚未回测；可以认为反转质量、失效位或扣费空间仍不足，并具体说明。'
        'BOS回测与扫荡收回是可分别评估的setup，不要叠成全部必需；1h用于背景和反证，不机械要求两周期同时BOS。'
        '每项假设绑定case_ids并给最强相反解释及可证伪测试，不把模型理由当因果证据。'
        '草案保留已收盘/可知时点、2000USDT固定目标、真实账户及Gate保证金/杠杆限制、现有净RR2.0研究要求；'
        '不触价盲入、不缩仓，不新增机械风控，不强制开仓或微利抢平刷胜率。'
        '明确已成立setup的条件，不在每轮满足后移动确认目标；失效及时退出，盈利结构有效时留出运行空间。')
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps(
        {'inputs': payload, 'required_json_schema': schema}, ensure_ascii=False, separators=(',', ':'))}]
    return messages, schema


def project(messages, schema, decoded):
    captured = {}
    client = ModelClient(retries=0)
    def capture(**kwargs):
        captured.update(kwargs)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(decoded, ensure_ascii=False)}}]}
    client.chat_completion = capture
    assert client.structured_analysis(messages, schema=schema, model_name='gemini-3.8-flash-high',
                                      allow_syntax_repair=False) == decoded
    return captured['messages']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    directory = args.directory.resolve()
    payload = build_payload(directory)
    messages, schema = prepare_request(payload)
    example = {'status': 'HYPOTHESIS_ONLY_UNVALIDATED', 'hypotheses': [{
        'case_ids': [payload['all_selected_active_retest_wait_cases'][0]['case_id']],
        'observation': 'offline projection', 'alternative_explanation': 'offline projection', 'test': 'offline projection'}],
        'draft_instruction': 'offline projection', 'validation_checks': ['offline projection']}
    wire = project(messages, schema, example)
    budget = sum(_estimate_tokens(m['content']) for m in wire) + 1536 + 256
    if budget > 32768:
        raise ValueError('DIAGNOSTIC_INPUT_BUDGET_EXCEEDED')
    artifact = {'status': 'PREPARED_NO_MODEL_CALL', 'scope': payload['purpose'],
                'source_bindings': payload['source_bindings'], 'request_messages': messages,
                'actual_projected_wire_messages': wire, 'caller_input_hash': digest({'messages': messages}),
                'actual_wire_messages_sha256': wire_digest(wire), 'estimated_budget_with_reserves': budget,
                'native_tokenizer_capacity_verified': False, 'formal_candidate_generated': False,
                'production_strategy_writes': 0, 'private_exchange_calls': 0, 'profit_acceptance_passed': False}
    if not args.run:
        (directory / 'confirmation-semantics-input-preview.json').write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': artifact['status'], 'all_active_retest_cases': len(payload['all_selected_active_retest_wait_cases']), 'budget': budget}))
        return
    processes = subprocess.run(['powershell', '-NoProfile', '-Command',
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\\.exe$' -and $_.CommandLine -match 'run_gemini.*research' } | Select-Object ProcessId | ConvertTo-Json -Compress"],
        capture_output=True, text=True, check=True)
    if processes.stdout.strip():
        raise ValueError('NO_ACTIVE_RESEARCH_RUNNER_REQUIRED')
    with urlopen('http://127.0.0.1:18765/v2/ai-session/status', timeout=35) as response:
        session = json.load(response)['ai_session']
    if session['state'] != 'STOPPED' or session['enabled'] is not False:
        raise ValueError('LIVE_SCAN_PRIORITY_REQUIRED')
    path = directory / 'confirmation-semantics-model-review.json'
    artifact['status'] = 'MODEL_STARTED'
    with path.open('x', encoding='utf-8') as output:
        json.dump(artifact, output, ensure_ascii=False, indent=2)
    started = time.monotonic()
    try:
        decoded, raw, receipt = OllamaProvider(context_length=32768, max_tokens=1536, retries=0, timeout=170).generate_json(
            messages, model_name='gemini-3.8-flash-high', prompt_version='pa_confirmation_semantics_diagnostic_v1',
            input_hash=artifact['caller_input_hash'], temperature=0, schema=schema, max_tokens=1536,
            deadline_monotonic=started + 170, allow_syntax_repair=False)
        artifact.update(decoded=decoded, raw_response=raw, receipt=receipt)
        validate_schema(decoded, schema)
        trace = receipt['transport_trace']
        if (trace.get('phase') != 'COMPLETED' or trace.get('http_status') != 200
            or trace.get('stream_done') is not True or trace.get('stream_finish_reason') != 'stop'
            or trace.get('wire_messages_sha256') != artifact['actual_wire_messages_sha256']
            or receipt['actual_model_id'] != 'gemini-3.8-flash-high'
            or receipt['input_hash'] != artifact['caller_input_hash']
            or json.loads(receipt['raw_response']) != decoded or json.loads(raw) != decoded):
            raise ValueError('COMPLETE_BOUND_HIGH_RESPONSE_REQUIRED')
        artifact['status'] = 'REAL_HIGH_HYPOTHESIS_BOUND_REQUIRES_INDEPENDENT_REVIEW'
    except Exception as error:
        artifact.update(status='DIAGNOSTIC_FAILED', error_type=type(error).__name__,
                        transport_trace=getattr(error, 'transport_trace', None))
    finally:
        artifact.update(observed_at=datetime.now(timezone.utc).isoformat(), wall_seconds=time.monotonic() - started)
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: artifact[key] for key in ('status', 'observed_at', 'wall_seconds', 'estimated_budget_with_reserves')}))
    if artifact['status'] == 'DIAGNOSTIC_FAILED':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
