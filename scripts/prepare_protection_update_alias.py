"""Prepare a lossless UPDATE_PROTECTION field bridge; never deploy to live/frozen code."""
import ast
import difflib
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FUNCTION = '''def normalize_protection_update_aliases(decoded: object) -> dict[str, object] | None:
    """Copy exact model-authored update prices; conflicting or invalid values fail closed."""
    if not isinstance(decoded, dict) or decoded.get("action") != "UPDATE_PROTECTION":
        return None
    changes = {}
    # Validate all fields first: an invalid second leg cannot leave the first mutated.
    for old, new in (("stop_price", "new_stop_price"), ("take_profit", "new_take_profit")):
        source, canonical = decoded.get(old), decoded.get(new)
        for value in (source, canonical):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError("MODEL_PROTECTION_PRICE_INVALID")
        if source is not None and canonical is not None and source != canonical:
            raise ValueError("MODEL_PROTECTION_FIELDS_CONFLICT")
        if source is not None and canonical is None:
            changes[new] = {"source_field": old, "source_value": source,
                            "canonical_original_value": canonical}
    if not changes:
        return None
    for field, detail in changes.items():
        decoded[field] = detail["source_value"]
    return {"normalization": "EXACT_MODEL_AUTHORED_UPDATE_PROTECTION_ALIAS",
            "normalized_fields": list(changes), "field_sources": changes,
            "original_price_fields_preserved": True, "execution_prices_generated": False}


'''


def prepared_function():
    namespace = {'math': math}
    exec(compile(ast.parse(FUNCTION), '<unapplied-protection-alias>', 'exec'), namespace)
    return namespace['normalize_protection_update_aliases']


def proposed_sources():
    schema = 'core/trading/model_schemas.py'
    coordinator = 'core/trading/ai_session_coordinator.py'
    old = {name:(ROOT/name).read_text(encoding='utf-8') for name in (schema,coordinator)}
    marker = 'def normalize_wait_unused_protection('
    assert old[schema].count(marker) == 1
    present = old[schema].count('def normalize_protection_update_aliases(')
    if present not in (0, 1):
        raise ValueError('PROTECTION_ALIAS_SOURCE_CHANGED')
    new = {schema:old[schema] if present else old[schema].replace(marker, FUNCTION+marker)}
    if present and FUNCTION not in old[schema]:
        raise ValueError('PROTECTION_ALIAS_SOURCE_CHANGED')
    source = old[coordinator]
    import_marker = 'normalize_wait_unused_protection, normalize_wait_symbol_alias'
    import_new = 'normalize_wait_unused_protection, normalize_protection_update_aliases, normalize_wait_symbol_alias'
    if import_new not in source:
        assert source.count(import_marker) == 1
        source = source.replace(import_marker, import_new)
    for variable, indent in [('candidate_decoded','            '),('decoded','        ')]:
        anchor = indent + f'projection = normalize_wait_unused_protection({variable})'
        assert source.count(anchor) == 1
        hook = (indent + f'projection = normalize_protection_update_aliases({variable})\n'
                + indent + 'if projection is not None:\n' + indent + '    '
                + 'context.model_inference_settings.setdefault("model_output_normalizations", []).append(projection)\n')
        if hook not in source:
            source = source.replace(anchor, hook+anchor)
    new[coordinator] = source
    for name,text in new.items(): compile(text,name,'exec')
    return old,new


def prepare(directory):
    directory = Path(directory)
    old,new=proposed_sources()
    patch=''.join(''.join(difflib.unified_diff(old[name].splitlines(True),new[name].splitlines(True),
        fromfile=name,tofile=name)) for name in old)
    patchfile=directory/'next-freeze-protection-update-alias.patch'
    patchfile.write_text(patch,encoding='utf-8')
    result={'status':'PREPARED_NOT_APPLIED',
        'source_before_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in old},
        'proposed_source_sha256':{name:hashlib.sha256(text.encode()).hexdigest() for name,text in new.items()},
        'patch_sha256':hashlib.sha256(patchfile.read_bytes()).hexdigest(),
        'scope':'UPDATE_PROTECTION_ONLY_EXACT_MODEL_NUMERIC_PRICES',
        'values_action_instrument_position_and_original_alias_preserved':True,
        'conflicting_aliases_rejected':True,'invalid_price_rejected_before_mutation':True,
        'raw_response_and_normalization_provenance_required':True,
        'ownership_and_gateway_checks_unchanged':True,
        'source_applied':False,'model_calls':0,'private_exchange_calls':0,
        'profit_acceptance_passed':False}
    (directory/'next-freeze-protection-update-alias.json').write_text(
        json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    assert all((ROOT/name).read_text(encoding='utf-8') == text for name,text in old.items())
    return result


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.directory)))
