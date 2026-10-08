from copy import deepcopy

import pytest

from scripts.audit_fixed_entry_budget import audit_entry


def data():
    return ({"action": "OPEN_SHORT", "instrument_id": "BTCUSDT", "order_preference": "MARKET", "requested_leverage": 20},
            {"action": "OPEN_SHORT", "instrument_id": "BTCUSDT", "order_type": "MARKET", "order_id": "simulation-only",
             "quantity": 177, "requested_quantity": 177, "quantity_capped": False, "leverage": 20},
            {"price": 112787.6, "market": {"contractSize": .0001, "precision": {"amount": 1, "price": .1},
                                         "limits": {"leverage": {"max": 200}}}},
            {"sizing_mode": "FIXED_NOTIONAL", "fixed_notional_usdt": 2000, "leverage_mode": "VENUE_LIMIT"})


def test_independent_quantization_and_venue_leverage_formula():
    args = data()
    original = deepcopy(args)
    result = audit_entry(*args)
    assert result["accepted_notional_usdt"] == "1996.34052"
    assert args == original
    args[0]["requested_leverage"], args[1]["leverage"] = 500, 200
    assert audit_entry(*args)["accepted_leverage"] == 200


@pytest.mark.parametrize("field,changed", [("quantity", 150), ("quantity", 178), ("quantity_capped", True),
                                           ("requested_quantity", 150), ("leverage", 10)])
def test_silent_resize_or_nonvenue_leverage_change_is_rejected(field, changed):
    args = data()
    args[1][field] = changed
    with pytest.raises(ValueError, match="AUDIT_ENTRY_SILENT_RESIZING|AUDIT_LEVERAGE_CHANGED"):
        audit_entry(*args)


def test_limit_price_rounding_is_checked_independently():
    args = data()
    args[0].update(order_preference="LIMIT", entry_price=112740.04)
    args[1].update(order_type="LIMIT", limit_price=112740.1)
    assert audit_entry(*args)["submission_reference_price"] == "112740.1"
    args[1]["limit_price"] = 112740.0
    with pytest.raises(ValueError, match="LIMIT_PRICE_ROUNDING"):
        audit_entry(*args)
