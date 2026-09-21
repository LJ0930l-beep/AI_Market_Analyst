"""Application-owned sizing and execution policy for AI strategy instructions."""
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN
import math
import re

DEFAULT_EXECUTION = {
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "direction": "BOTH", "sizing_mode": "RISK_BASED",
    "fixed_notional_usdt": 1000.0, "equity_notional_pct": 5.0,
    "max_notional_usdt": 5000.0, "risk_per_trade_pct": 0.25,
    "leverage": 3, "max_positions": 3, "max_margin_pct": 20.0,
    "min_confidence": 70, "min_net_rr": 2.0, "cooldown_minutes": 30,
    "order_preference": "AUTO",
    "universe_mode": "ALL", "scan_interval_minutes": 15,
    "atr_adaptive_sizing": True,
    "consecutive_loss_lock_enabled": True,
    "us_open_defense_enabled": True,
}
LIMITS = {
    "fixed_notional_usdt": (10, 1000000), "equity_notional_pct": (0.1, 100),
    "max_notional_usdt": (10, 1000000), "risk_per_trade_pct": (0.01, 0.25),
    "leverage": (1, 100), "max_positions": (1, 5), "max_margin_pct": (1, 80),
    "min_confidence": (60, 100), "min_net_rr": (1.5, 10), "cooldown_minutes": (0, 1440),
    "scan_interval_minutes": (5, 15),
}


def venue_leverage_limit(market):
    """Return the exchange-advertised contract leverage ceiling.

    Gate native REST exposes ``leverage_max`` while CCXT normally maps the
    same value into either ``limits.leverage.max`` or ``info.leverage_max``.
    Treat this as venue metadata, not a strategy default.  Missing or invalid
    metadata stays unknown so live/testnet execution can fail closed instead
    of silently assuming a platform-wide number.
    """

    if not isinstance(market, dict):
        return None
    limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
    leverage_limits = limits.get("leverage") if isinstance(limits.get("leverage"), dict) else {}
    raw = market.get("raw") if isinstance(market.get("raw"), dict) else {}
    info = market.get("info") if isinstance(market.get("info"), dict) else {}
    candidates = (
        market.get("leverage_max"),
        leverage_limits.get("max"),
        raw.get("leverage_max"),
        info.get("leverage_max"),
    )
    for candidate in candidates:
        if candidate is None or isinstance(candidate, bool):
            continue
        try:
            number = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 1:
            # Gate accepts an integer leverage.  Rounding down can never
            # exceed the exchange contract ceiling.
            return max(1, int(math.floor(number)))
    return None


def resolve_execution_leverage(
    config,
    *,
    market,
    rule_leverage,
    requested_leverage=None,
    require_venue_limit=True,
):
    """Resolve actual leverage from AI preference, policy and venue facts.

    ``execution.leverage`` is the user's ceiling.  The AI value is also a
    ceiling/preference, while ``rule_leverage`` is computed from the observed
    stop distance by the risk engine.  None of those may exceed the current
    Gate contract's advertised maximum.
    """

    normalized = normalize_execution(config)
    venue_limit = venue_leverage_limit(market)
    if require_venue_limit and venue_limit is None:
        raise ValueError("VENUE_LEVERAGE_LIMIT_UNAVAILABLE")
    try:
        rule_limit = int(rule_leverage)
    except (TypeError, ValueError) as exc:
        raise ValueError("RULE_LEVERAGE_INVALID") from exc
    if rule_limit < 1:
        raise ValueError("RULE_LEVERAGE_INVALID")
    ceilings = {
        "strategy_user_cap": int(normalized["leverage"]),
        "risk_rule_cap": rule_limit,
    }
    if venue_limit is not None:
        ceilings["venue_contract_cap"] = venue_limit
    if requested_leverage is not None:
        if isinstance(requested_leverage, bool):
            raise ValueError("AI_LEVERAGE_INVALID")
        try:
            requested = int(requested_leverage)
        except (TypeError, ValueError) as exc:
            raise ValueError("AI_LEVERAGE_INVALID") from exc
        if requested < 1:
            raise ValueError("AI_LEVERAGE_INVALID")
        ceilings["ai_requested_cap"] = requested
    actual = min(ceilings.values())
    return {
        "leverage": actual,
        "source": "MIN_OF_AI_STRATEGY_VENUE_AND_STOP_RISK",
        "ceilings": ceilings,
        "binding_limit": min(ceilings, key=ceilings.get),
    }


def normalize_execution(value=None):
    if value is None:
        return deepcopy(DEFAULT_EXECUTION)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_EXECUTION):
        raise ValueError("STRATEGY_EXECUTION_FIELDS_INVALID")
    config = {**deepcopy(DEFAULT_EXECUTION), **value}
    for name in ("atr_adaptive_sizing", "consecutive_loss_lock_enabled", "us_open_defense_enabled"):
        if type(config[name]) is not bool:
            raise ValueError(f"STRATEGY_EXECUTION_BOOLEAN_REQUIRED_{name.upper()}")
    for name, (low, high) in LIMITS.items():
        number = config[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"STRATEGY_EXECUTION_INVALID_{name.upper()}")
        if name in {"leverage", "max_positions", "min_confidence", "cooldown_minutes"} and int(number) != number:
            raise ValueError(f"STRATEGY_EXECUTION_INTEGER_REQUIRED_{name.upper()}")
    if config['direction'] not in {'BOTH', 'LONG_ONLY', 'SHORT_ONLY'} or config['sizing_mode'] not in {'RISK_BASED', 'FIXED_NOTIONAL', 'EQUITY_PERCENT'} or config['order_preference'] not in {'AUTO', 'MARKET', 'LIMIT'}:
        raise ValueError('STRATEGY_EXECUTION_MODE_INVALID')
    symbols = config['symbols']
    if not isinstance(symbols, list) or len(symbols) > 5000 or any(not isinstance(s, str) or not re.fullmatch(r'[A-Z0-9]{1,30}USDT', s) for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError('STRATEGY_SYMBOLS_INVALID')
    if config['universe_mode'] not in {'ALL', 'CUSTOM'} or (config['universe_mode'] == 'CUSTOM' and not symbols):
        raise ValueError('STRATEGY_UNIVERSE_INVALID')
    if config['scan_interval_minutes'] not in {5, 15}:
        raise ValueError('STRATEGY_SCAN_INTERVAL_INVALID')
    if config['fixed_notional_usdt'] > config['max_notional_usdt']:
        raise ValueError('STRATEGY_FIXED_NOTIONAL_EXCEEDS_CAP')
    return config


def size_position(config, *, equity, available, used_margin, entry, unit_risk, contract_size, step, risk_fraction, leverage, fee_rate, requested_notional=None):
    """Round down the smallest budget; leverage never multiplies the loss budget."""
    values = (equity, available, used_margin, entry, unit_risk, contract_size, step, risk_fraction, leverage, fee_rate)
    if any(not math.isfinite(float(x)) for x in values) or min(equity, entry, unit_risk, contract_size, step, risk_fraction, leverage) <= 0 or min(available, used_margin, fee_rate) < 0:
        raise ValueError('STRATEGY_SIZING_INPUT_INVALID')
    loss_budget = equity * min(risk_fraction, config['risk_per_trade_pct'] / 100)
    risk_notional = loss_budget / unit_risk * entry * contract_size
    target = risk_notional
    if config['sizing_mode'] == 'FIXED_NOTIONAL':
        target = config['fixed_notional_usdt']
    elif config['sizing_mode'] == 'EQUITY_PERCENT':
        target = equity * config['equity_notional_pct'] / 100
    if requested_notional is not None:
        if isinstance(requested_notional, bool) or not math.isfinite(float(requested_notional)) or float(requested_notional) <= 0:
            raise ValueError('STRATEGY_REQUESTED_NOTIONAL_INVALID')
        target = min(target, float(requested_notional))
    margin_budget = max(0.0, min(available, equity * config['max_margin_pct'] / 100 - used_margin))
    caps = {'requested_or_configured': target, 'stop_loss_risk': risk_notional,
            'single_position_cap': config['max_notional_usdt'],
            'available_margin': margin_budget / (1 / leverage + fee_rate * 2)}
    notional = min(caps.values())
    quantity = float((Decimal(str(notional / (entry * contract_size))) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))
    actual = quantity * entry * contract_size
    res = {'quantity': quantity, 'notional_usdt': actual, 'estimated_margin_usdt': actual / leverage,
            'leverage': leverage, 'estimated_loss_usdt': quantity * unit_risk, 'risk_budget_usdt': loss_budget,
            'sizing_mode': config['sizing_mode'], 'binding_limit': min(caps, key=caps.get), 'caps_usdt': caps}
    if requested_notional is not None:
        res['ai_requested_notional_usdt'] = float(requested_notional)
    return res
