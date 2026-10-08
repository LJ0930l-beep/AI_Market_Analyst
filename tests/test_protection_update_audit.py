from copy import deepcopy
import pytest
from core.trading.model_schemas import normalize_protection_update_aliases
from scripts.verify_ai_template_replay import audit_protection_update_values


def fixture():
    return {'action':'UPDATE_PROTECTION','instrument_id':'ETHUSDT','position_id':'fixture',
            'stop_price':4005,'take_profit':3900,'new_stop_price':None,'new_take_profit':None}


def test_old_recorded_field_loss_is_rejected_even_with_valid_json_and_model_receipt():
    raw=fixture()
    with pytest.raises(ValueError,match='PRICE_LOST_OR_CHANGED'):
        audit_protection_update_values(raw,deepcopy(raw),[])


def test_production_normalizer_preserves_exact_prices_and_independent_audit_accepts():
    raw=fixture();decision=deepcopy(raw);proof=normalize_protection_update_aliases(decision)
    audit_protection_update_values(raw,decision,[proof])


@pytest.mark.parametrize('key,value',[('new_stop_price',4005.000000001),('position_id','other'),('stop_price',4006)])
def test_even_small_price_or_identity_tampering_is_rejected(key,value):
    raw=fixture();decision=deepcopy(raw);proof=normalize_protection_update_aliases(decision)
    decision[key]=value
    with pytest.raises(ValueError): audit_protection_update_values(raw,decision,[proof])


def test_metadata_cannot_replace_raw_price_evidence():
    raw=fixture();decision=deepcopy(raw);proof=normalize_protection_update_aliases(decision)
    proof['field_sources']['new_stop_price']['source_value']=4006
    with pytest.raises(ValueError,match='PROVENANCE_INVALID'):
        audit_protection_update_values(raw,decision,[proof])
