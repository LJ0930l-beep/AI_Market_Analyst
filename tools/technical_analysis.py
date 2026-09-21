"""Algorithmic technical analysis module for AI Market Analyst V2.

Computes mathematical indicators deterministically in Python:
- EMA (20, 50, 200)
- ATR (14)
- RSI (14)
- VWAP
- Swing Highs / Swing Lows
- Dynamic Support & Resistance levels
- Market Structure (HH_HL, LH_LL, Range_Bound, Trend_Shift)
- Volume regime

LLMs are strictly forbidden from calculating indicators by mental arithmetic.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple


def calculate_ema(prices: List[float], period: int) -> List[float]:
    if len(prices) < period or period <= 0:
        return []
    multiplier = 2.0 / (period + 1)
    # Seed with SMA
    sma = sum(prices[:period]) / period
    ema_values = [sma]
    for price in prices[period:]:
        new_ema = (price - ema_values[-1]) * multiplier + ema_values[-1]
        ema_values.append(new_ema)
    return ema_values


def calculate_rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(highs) < period + 1:
        return round((highs[-1] - lows[-1]), 4) if highs else 0.0
    tr_list = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i - 1])
        lc = abs(lows[i] - closes[i - 1])
        tr_list.append(max(hl, hc, lc))
    
    if len(tr_list) < period:
        return round(sum(tr_list) / len(tr_list), 4)
    atr = sum(tr_list[:period]) / period
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)


def find_swing_points(highs: List[float], lows: List[float], window: int = 3) -> Tuple[List[float], List[float]]:
    swing_highs = []
    swing_lows = []
    n = len(highs)
    if n < window * 2 + 1:
        return [], []
        
    for i in range(window, n - window):
        is_high = all(highs[i] >= highs[i - j] for j in range(1, window + 1)) and \
                  all(highs[i] >= highs[i + j] for j in range(1, window + 1))
        if is_high:
            swing_highs.append(round(highs[i], 2))
            
        is_low = all(lows[i] <= lows[i - j] for j in range(1, window + 1)) and \
                 all(lows[i] <= lows[i + j] for j in range(1, window + 1))
        if is_low:
            swing_lows.append(round(lows[i], 2))
            
    return swing_highs, swing_lows


def determine_market_structure(highs: List[float], lows: List[float], closes: List[float]) -> Dict[str, Any]:
    sh, sl = find_swing_points(highs, lows, window=3)
    if len(sh) < 2 or len(sl) < 2:
        return {
            "structure": "Range_Bound",
            "trend": "Neutral",
            "swing_highs": sh,
            "swing_lows": sl,
            "description": "Insufficient structural swings to establish directional trend",
        }
        
    higher_highs = sh[-1] > sh[-2]
    higher_lows = sl[-1] > sl[-2]
    lower_highs = sh[-1] < sh[-2]
    lower_lows = sl[-1] < sl[-2]
    
    if higher_highs and higher_lows:
        structure = "HH_HL"
        trend = "Bullish"
    elif lower_highs and lower_lows:
        structure = "LH_LL"
        trend = "Bearish"
    elif higher_highs and lower_lows:
        structure = "Broadening_Expanding"
        trend = "Volatile_Neutral"
    else:
        structure = "Trading_Range"
        trend = "Neutral"
        
    return {
        "structure": structure,
        "trend": trend,
        "swing_highs": sh[-3:],
        "swing_lows": sl[-3:],
    }


def analyze_ohlcv(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Take standard OHLCV bars and return complete mathematical technical dossier."""
    if not bars:
        return {"error": "NO_DATA_AVAILABLE"}
        
    closes = [float(b.get("close", 0.0)) for b in bars]
    highs = [float(b.get("high", 0.0)) for b in bars]
    lows = [float(b.get("low", 0.0)) for b in bars]
    volumes = [float(b.get("volume", 0.0)) for b in bars]
    
    current_price = closes[-1]
    
    # EMAs
    ema20_vals = calculate_ema(closes, 20)
    ema50_vals = calculate_ema(closes, 50)
    ema200_vals = calculate_ema(closes, 200)
    
    ema20 = round(ema20_vals[-1], 2) if ema20_vals else None
    ema50 = round(ema50_vals[-1], 2) if ema50_vals else None
    ema200 = round(ema200_vals[-1], 2) if ema200_vals else None
    
    rsi14 = calculate_rsi(closes, 14)
    atr14 = calculate_atr(highs, lows, closes, 14)
    
    # Structure & Swings
    struct = determine_market_structure(highs, lows, closes)
    sh = struct["swing_highs"]
    sl = struct["swing_lows"]
    
    # Support & Resistance levels
    supports = sorted(list(set([round(l, 2) for l in sl if l < current_price])), reverse=True)[:3]
    resistances = sorted(list(set([round(h, 2) for h in sh if h > current_price])))[:3]
    
    if not supports and lows:
        supports = [round(min(lows[-20:]), 2)]
    if not resistances and highs:
        resistances = [round(max(highs[-20:]), 2)]
        
    # Volume analysis
    avg_vol = sum(volumes[-20:]) / max(1, len(volumes[-20:]))
    curr_vol = volumes[-1]
    vol_ratio = curr_vol / (avg_vol + 1e-6)
    if vol_ratio > 1.8:
        vol_state = "high_expansion"
    elif vol_ratio < 0.6:
        vol_state = "low_compression"
    else:
        vol_state = "normal"
        
    # Location
    if supports and abs(current_price - supports[0]) / current_price < 0.005:
        location = f"Testing Support Zone ({supports[0]})"
    elif resistances and abs(resistances[0] - current_price) / current_price < 0.005:
        location = f"Testing Resistance Zone ({resistances[0]})"
    elif ema20 and abs(current_price - ema20) / current_price < 0.003:
        location = f"Mean Reversion Equilibrium at EMA20 ({ema20})"
    else:
        location = "Mid-Range Fluctuation"
        
    return {
        "current_price": round(current_price, 2),
        "trend": struct["trend"].lower(),
        "market_structure": struct["structure"],
        "location": location,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "rsi14": rsi14,
        "atr14": atr14,
        "support": supports,
        "resistance": resistances,
        "volume_state": vol_state,
        "volume_ratio": round(vol_ratio, 2),
    }


# Aliases for compatibility across agent and test callers
compute_technical_indicators = analyze_ohlcv
calculate_indicators = analyze_ohlcv
