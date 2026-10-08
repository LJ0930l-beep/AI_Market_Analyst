"""Isolated research fixtures; no model, exchange or production store writes."""
from copy import deepcopy
import math

import numpy as np
import pandas as pd
import pytest

from core.replay.technical_proxy import MINUTE, ProxyAccount, aggregate_minutes, features, frozen_proxy_config, signal


def test_aggregate_rejects_duplicate_minute_and_missing_interior():
    rows = pd.DataFrame({"open_time_ms": [0, MINUTE, MINUTE, 3*MINUTE, 4*MINUTE],
                         "open": [100]*5, "high": [101]*5, "low": [99]*5,
                         "close": [100]*5, "volume": [1]*5})
    assert aggregate_minutes(rows, 5).empty


def test_features_prior_range_and_pivot_do_not_read_future():
    index = np.arange(1, 71) * 5 * MINUTE
    values = np.arange(100., 170.)
    bars = pd.DataFrame({"open": values-.1, "high": values+.2, "low": values-.2,
                         "close": values, "volume": np.ones(70)}, index=index)
    original = features(bars)
    changed = bars.copy()
    changed.loc[index[40]:, "high"] += 1000
    future = features(changed)
    pd.testing.assert_frame_equal(original.loc[:index[39]], future.loc[:index[39]])
    assert original.loc[index[39], "prev_high"] == pytest.approx(values[38]+.2)
    assert original.loc[index[39], "close"] > original.loc[index[39], "prev_high"]


def account():
    config = frozen_proxy_config()
    return ProxyAccount(config, config["templates"][0])


def proposal():
    return {"side": "LONG", "direction": 1, "limit": 100., "stop": 98., "target": 104., "ttl_ms": 480_000, "score": 1}


def test_delay_limit_strict_crossing_and_stop_first_costs():
    a = account()
    a.last_close["BTCUSDT"] = 101
    a.decide(0, {"BTCUSDT": proposal()}, {"BTCUSDT": 101})
    a.minute(0, {"BTCUSDT": [101, 102, 99, 101, 1000]})
    assert not a.positions and len(a.orders) == 1  # 60s submission delay
    a.minute(MINUTE, {"BTCUSDT": [101, 102, 100, 101, 1000]})
    assert not a.positions  # exact touch is not a queue-proven fill
    a.minute(2*MINUTE, {"BTCUSDT": [101, 105, 97, 100, 1000]})
    t = a.trades[0]
    assert t["exit_reason"] == "STOP_LOSS"
    assert t["entry_fee_type"] == "MAKER"
    expected_exit = 98*(1-.0002)
    expected_fee = 250*.0002 + expected_exit*2.5*.00075
    expected_net = (expected_exit-100)*2.5-expected_fee
    assert t["fees"] == pytest.approx(expected_fee)
    assert t["net_pnl"] == pytest.approx(expected_net)
    assert a.result()["roi"] == pytest.approx(expected_net/1000)


def test_pending_margin_is_counted_and_zero_trade_win_rate_undefined():
    a = account()
    a.template["margin_cap_pct"] = 3
    a.decide(0, {"BTCUSDT": proposal()}, {"BTCUSDT": 100, "ETHUSDT": 100})
    a.decide(MINUTE, {"ETHUSDT": proposal()}, {"BTCUSDT": 100, "ETHUSDT": 100})
    assert list(a.orders) == ["BTCUSDT"]
    assert a.wait_reasons["MARGIN_CAP"] == 1
    assert a.result()["win_rate"] is None


def test_funding_changes_equity_and_closed_net_pnl():
    a = account()
    a.last_close["BTCUSDT"] = 101
    a.decide(0, {"BTCUSDT": proposal()}, {"BTCUSDT": 101})
    a.minute(MINUTE, {"BTCUSDT": [101, 102, 99, 100, 1000]})
    a.pay_funding("BTCUSDT", .0001, 100, MINUTE+5)
    a.minute(2*MINUTE, {"BTCUSDT": [100, 105, 99, 104, 1000]})
    assert a.trades[0]["funding_pnl"] == pytest.approx(-.025)
    assert a.trades[0]["net_pnl"] == pytest.approx(a.trades[0]["gross_pnl"]-a.trades[0]["fees"]-.025)
    assert a.result()["ending_equity"] == pytest.approx(1000+a.trades[0]["net_pnl"])


def test_reversal_does_not_invent_missing_funding_evidence():
    cfg = frozen_proxy_config()
    defense = next(t for t in cfg["templates"] if t["template_id"] == "conservative_defense")
    row = {"open": 100., "close": 101., "high": 102., "low": 95., "ema": 100., "atr": 2.,
           "prev_high": 110., "prev_low": 94., "volume_ratio": 1., "vwap": 105., "rsi": 30.}
    assert signal(defense, row, None, None) is None
    assert signal(defense, row, None, -.0002) is not None
