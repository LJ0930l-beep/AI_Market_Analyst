"""Prepare a reviewable next-freeze patch without editing active source."""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_template_runner import frozen_source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError('SETTLED_FUNDING_PATCH_PREIMAGE_CHANGED')
    return source.replace(before, after, 1)


def proposed_sources(root=ROOT):
    names = ('core/replay/ai_template_runner.py', 'core/replay/gemini_research.py',
             'core/trading/ai_session_coordinator.py')
    original = {name:(Path(root)/name).read_text(encoding='utf-8') for name in names}
    runner = replace_once(original[names[0]], '    "core/replay/relay_policy.py",\n',
        '    "core/replay/relay_policy.py", "core/replay/settled_funding.py",\n')
    runner = replace_once(runner,
        '    snapshot = deepcopy(history.market_snapshot(symbol, point))\n',
        '    from .settled_funding import settled_funding_as_of\n'
        '    snapshot = deepcopy(history.market_snapshot(symbol, point))\n'
        '    settled = settled_funding_as_of(history.payload.get("funding", []), [symbol], point,\n'
        '        venue=history.payload.get("assumptions", {}).get("data_venue", "gate"))\n'
        '    snapshot["historical_settled_funding"] = settled["rates"][symbol]\n')
    research = replace_once(original[names[1]],
        '            "news": "NO_HISTORICAL_ARCHIVE_UNKNOWN_NOT_INVENTED",\n',
        '            "news": "NO_HISTORICAL_ARCHIVE_UNKNOWN_NOT_INVENTED",\n'
        '            "model_funding": "LAST_SETTLED_BINANCE_RATE_PAYMENT_PLUS_60S_RESEARCH_PUBLICATION_LAG_NOT_GATE_CURRENT_RATE",\n')
    research = replace_once(research,
        '(symbol, int(start.timestamp() * 1000), int(end.timestamp() * 1000))):\n',
        '(symbol, int((start - timedelta(days=2)).timestamp() * 1000), int(end.timestamp() * 1000))):\n')
    coordinator = replace_once(original[names[2]],
        '            "contract_rules",\n', '            "contract_rules", "historical_settled_funding",\n')
    coordinator = replace_once(coordinator,
        '        "environment", "market_data_environment", "slippage", "fee_rate", "taker_fee",\n',
        '        "environment", "market_data_environment", "slippage", "fee_rate", "taker_fee",\n'
        '        "historical_settled_funding",\n')
    revised = {names[0]:runner,names[1]:research,names[2]:coordinator}
    return original, revised


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    plan = json.loads((directory/'research-plan.json').read_text(encoding='utf-8'))['plan']
    if plan['source_sha256'] != frozen_source_fingerprint():
        raise ValueError('SETTLED_FUNDING_PREPARATION_SOURCE_DRIFT')
    original, revised = proposed_sources()
    patch = ''.join(''.join(difflib.unified_diff(original[name].splitlines(keepends=True),
        revised[name].splitlines(keepends=True),fromfile='a/'+name,tofile='b/'+name)) for name in original)
    (directory/'next-freeze-settled-funding.patch').write_text(patch,encoding='utf-8')
    metadata = {'status':'PREPARED_NOT_APPLIED', 'production_source_writes':0,'private_exchange_calls':0,
        'patch_sha256':hashlib.sha256(patch.encode()).hexdigest(),
        'files':{name:{'before_sha256':hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),
                      'after_sha256':hashlib.sha256(revised[name].encode()).hexdigest()} for name in original},
        'proposed_output_encoding':'UTF8_LF',
        'new_dependency':'core/replay/settled_funding.py',
        'new_dependency_sha256':hashlib.sha256((ROOT/'core/replay/settled_funding.py').read_bytes()).hexdigest(),
        'activation_requires':'OWNED_JOB_TERMINAL_AND_PRECHANGE_LEDGER_AUDIT_THEN_NEW_FROZEN_EXPERIMENT',
        'future_gate_rate_or_oi_fabricated':False}
    (directory/'next-freeze-settled-funding.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    print(json.dumps({'status':metadata['status'],'patch_sha256':metadata['patch_sha256'],'production_source_writes':0}))


if __name__ == '__main__':
    main()
