"""Frozen technical proxies for five AI styles, never claimed as AI decisions.

Uses Binance UM candles and settled funding, independent research accounts,
closed-bar signals and next-minute execution. No production account writes.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd


VERSION = "five_style_technical_proxy_v1"
MINUTE = 60_000
RULES = {
    "aggressive_impulse": "5m prior-20 close breakout or sweep/reclaim; volume ratio >= profile minimum; HTF is context, not veto",
    "aggressive_breakout": "15m prior-20 close breakout or first retest within four bars, volume threshold and aligned 1h EMA20 slope",
    "conservative_pullback": "15m EMA20/session-VWAP touch and directional close, aligned 1h EMA20 slope, entry within 0.45%",
    "conservative_defense": "15m session-VWAP displacement >= ATR, RSI extreme and rejection, previously settled funding magnitude >= 0.01%",
    "price_action_structure": "15m close BOS or sweep/reclaim of strict two-left/two-right confirmed pivots; no news prerequisite",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def frozen_proxy_config():
    from core.trading.ai_strategy_book import TEMPLATES
    templates = [{"template_id": t["id"], "name": t["name"], "profile": deepcopy(t["profile"]),
                  "scan_interval_minutes": t["scan_interval_minutes"],
                  "margin_cap_pct": float(t["execution_defaults"]["max_margin_pct"]),
                  "rules": RULES[t["id"]], "source_template_sha256": sha(t)} for t in TEMPLATES]
    return {"version": VERSION, "decision_source": "DETERMINISTIC_TECHNICAL_PROXY_NOT_AI",
            "initial_equity": 1000.0, "notional_usdt": 250.0, "leverage": 10.0,
            "maker_fee_rate": 0.0002, "taker_fee_rate": 0.00075, "slippage_bps": 2.0,
            "submission_delay_seconds": 60, "entry_participation_cap": 0.01,
            "max_positions": 2, "templates": templates,
            "news": "UNAVAILABLE_NOT_A_VETO", "open_interest": "UNAVAILABLE_NOT_A_CONFIRMATION",
            "funding_input": "PREVIOUSLY_SETTLED_RATE_PROXY_NOT_INTRADAY_FORECAST",
            "sizing": "COMMON_FIXED_NOTIONAL_AND_LEVERAGE_NOT_AI_CAPITAL_ALLOCATION",
            "exits": "STATIC_ATR_AND_STRUCTURE_STOP_SINGLE_PROFILE_R_TARGET_NOT_AI_DYNAMIC_MANAGEMENT"}


def aggregate_minutes(bars, minutes):
    """Discard incomplete buckets instead of filling missing exchange minutes."""
    frame = bars.copy()
    frame["bucket"] = frame["open_time_ms"] // (minutes * MINUTE) * (minutes * MINUTE)
    grouped = frame.groupby("bucket", sort=True)
    result = grouped.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                         close=("close", "last"), volume=("volume", "sum"),
                         count=("open_time_ms", "size"), unique=("open_time_ms", "nunique"), first=("open_time_ms", "min"),
                         last=("open_time_ms", "max"))
    complete = ((result["count"] == minutes) & (result["unique"] == minutes) & (result["first"] == result.index)
                & (result["last"] == result.index + (minutes - 1) * MINUTE))
    result = result.loc[complete, ["open", "high", "low", "close", "volume"]]
    result.index = result.index + minutes * MINUTE  # time of close, not open
    return result


def features(bars):
    out = bars.copy()
    close, high, low, volume = (out[k] for k in ("close", "high", "low", "volume"))
    out["ema"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
    out["slope"] = out["ema"] - out["ema"].shift(3)
    ranges = pd.concat((high - low, (high - close.shift()).abs(), (low - close.shift()).abs()), axis=1)
    out["atr"] = ranges.max(axis=1).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    out["rsi"] = (100 - 100 / (1 + gain / loss)).where(loss != 0, 100).where((gain + loss) != 0, 50)
    out["prev_high"] = high.shift().rolling(20).max()
    out["prev_low"] = low.shift().rolling(20).min()
    out["volume_ratio"] = volume / volume.shift().rolling(20).mean().replace(0, np.nan)
    # Timestamp at midnight belongs to the session in which the bar opened.
    session = (out.index - 1) // (1440 * MINUTE)
    out["vwap"] = (((high + low + close) / 3 * volume).groupby(session).cumsum()
                   / volume.groupby(session).cumsum().replace(0, np.nan))
    pivot_high = high.shift(2)
    pivot_low = low.shift(2)
    high_confirmed = low_confirmed = pd.Series(True, index=out.index)
    for offset in (0, 1, 3, 4):
        high_confirmed &= pivot_high > high.shift(offset)
        low_confirmed &= pivot_low < low.shift(offset)
    out["swing_high"] = pivot_high.where(high_confirmed).ffill().shift()
    out["swing_low"] = pivot_low.where(low_confirmed).ffill().shift()
    out["previous_close"] = close.shift()
    up = close > out["prev_high"]
    down = close < out["prev_low"]
    out["up_level"] = out["prev_high"].where(up).ffill().shift()
    out["down_level"] = out["prev_low"].where(down).ffill().shift()
    offsets = pd.Series(np.arange(len(out)), index=out.index)
    out["up_age"] = offsets - offsets.where(up).ffill().shift()
    out["down_age"] = offsets - offsets.where(down).ffill().shift()
    return out


def signal(template, row, hour, funding):
    """A declared proxy translation, not an inference of what Bonsai would do."""
    required = ("close", "open", "high", "low", "ema", "atr", "prev_high", "prev_low", "volume_ratio", "vwap", "rsi")
    if any(not math.isfinite(float(row[k])) for k in required) or row["atr"] <= 0:
        return None
    profile, kind = template["profile"], template["template_id"]
    close, opening, high, low, atr = (float(row[k]) for k in ("close", "open", "high", "low", "atr"))
    up, down = close > row["prev_high"], close < row["prev_low"]
    sweep_up = low < row["prev_low"] < close and close > opening
    sweep_down = high > row["prev_high"] > close and close < opening
    long = short = False
    long_level, short_level = float(row["prev_high"]), float(row["prev_low"])
    volume_ok = row["volume_ratio"] >= profile.get("volume_ratio_min", 0)
    hour_long = hour is not None and hour["close"] > hour["ema"] and hour["slope"] > 0
    hour_short = hour is not None and hour["close"] < hour["ema"] and hour["slope"] < 0
    if kind == "aggressive_impulse":
        long, short = volume_ok and (up or sweep_up), volume_ok and (down or sweep_down)
        if sweep_up:
            long_level = float(row["prev_low"])
        if sweep_down:
            short_level = float(row["prev_high"])
    elif kind == "aggressive_breakout":
        retest_up = 1 <= row["up_age"] <= 4 and low <= row["up_level"] <= close and close > opening
        retest_down = 1 <= row["down_age"] <= 4 and high >= row["down_level"] >= close and close < opening
        long, short = volume_ok and hour_long and (up or retest_up), volume_ok and hour_short and (down or retest_down)
        if retest_up:
            long_level = float(row["up_level"])
        if retest_down:
            short_level = float(row["down_level"])
    elif kind == "conservative_pullback":
        long_keys = [float(row[k]) for k in ("ema", "vwap") if low <= row[k] <= close]
        short_keys = [float(row[k]) for k in ("ema", "vwap") if high >= row[k] >= close]
        long = hour_long and close > opening and bool(long_keys)
        short = hour_short and close < opening and bool(short_keys)
        long_level = max(long_keys) if long_keys else close
        short_level = min(short_keys) if short_keys else close
    elif kind == "conservative_defense":
        long = funding is not None and funding <= -0.0001 and low < row["vwap"] - atr and row["rsi"] < 35 and close > opening
        short = funding is not None and funding >= 0.0001 and high > row["vwap"] + atr and row["rsi"] > 65 and close < opening
        long_level = (opening + close) / 2
        short_level = long_level
    elif kind == "price_action_structure":
        long = ((math.isfinite(row["swing_high"]) and row["previous_close"] <= row["swing_high"] < close)
                or (math.isfinite(row["swing_low"]) and low < row["swing_low"] < close and close > opening))
        short = ((math.isfinite(row["swing_low"]) and row["previous_close"] >= row["swing_low"] > close)
                 or (math.isfinite(row["swing_high"]) and high > row["swing_high"] > close and close < opening))
        long_level = float(row["swing_high"] if close > row["swing_high"] else row["swing_low"])
        short_level = float(row["swing_low"] if close < row["swing_low"] else row["swing_high"])
    if long == short:
        return None
    direction = 1 if long else -1
    level = long_level if long else short_level
    limit = min(level, close) if long else max(level, close)
    maximum_distance = 0.45 if kind == "conservative_pullback" else profile["max_limit_distance_pct"]
    if not math.isfinite(limit) or abs(limit / close - 1) * 100 > maximum_distance:
        return None
    # Common rule translation: structural wick and profile ATR outside entry.
    distance = max(float(profile["atr_stop_multiple"]) * atr, limit - low if long else high - limit)
    reward_r = float(profile["target_r_multiples"][0])
    stop, target = limit - direction * distance, limit + direction * distance * reward_r
    if stop <= 0 or target <= 0:
        return None
    return {"side": "LONG" if long else "SHORT", "direction": direction, "limit": limit,
            "stop": stop, "target": target, "ttl_ms": int(profile["limit_ttl_seconds"]) * 1000,
            "score": float(row["volume_ratio"]) + abs(close - opening) / atr,
            "rule": RULES[kind]}


@dataclass
class ProxyAccount:
    config: dict
    template: dict

    def __post_init__(self):
        self.cash = float(self.config["initial_equity"])
        self.orders, self.positions = {}, {}
        self.trades, self.equity_daily = [], []
        self.counts, self.wait_reasons = Counter(), Counter()
        self.gross = self.fees = self.funding = self.max_drawdown = 0.0
        self.peak = self.cash
        self.last_close, self.last_entry = {}, {}
        self.halted = None

    def equity(self, prices):
        return self.cash + sum(p["direction"] * (prices[symbol] - p["entry"]) * p["quantity"]
                               for symbol, p in self.positions.items())

    def decide(self, now, candidates, prices):
        self.counts["scans"] += 1
        if self.halted:
            self.wait_reasons["ACCOUNT_HALTED"] += 1
            return
        options = [(symbol, proposal) for symbol, proposal in candidates.items()
                   if proposal is not None and symbol not in self.positions and symbol not in self.orders]
        cooldown = self.template["profile"]["cooldown_minutes"] * MINUTE
        options = [(s, p) for s, p in options if now - self.last_entry.get(s, -cooldown) >= cooldown]
        if not options or len(self.positions) + len(self.orders) >= self.config["max_positions"]:
            self.wait_reasons["NO_ELIGIBLE_TECHNICAL_SETUP_OR_OCCUPIED"] += 1
            return
        symbol, proposal = max(options, key=lambda item: (item[1]["score"], item[0]))
        self.counts["proposals"] += 1
        equity = self.equity(prices)
        used = sum(p["quantity"] * prices[s] / self.config["leverage"] for s, p in self.positions.items())
        reserved = sum(o["quantity"] * o["limit"] / self.config["leverage"] for o in self.orders.values())
        required = self.config["notional_usdt"] / self.config["leverage"] + self.config["notional_usdt"] * self.config["taker_fee_rate"]
        if used + reserved + required > max(0, equity) * self.template["margin_cap_pct"] / 100:
            self.wait_reasons["MARGIN_CAP"] += 1
            return
        self.orders[symbol] = {**proposal, "quantity": self.config["notional_usdt"] / proposal["limit"],
                               "submitted": now + self.config["submission_delay_seconds"] * 1000,
                               "expires": now + self.config["submission_delay_seconds"] * 1000 + proposal["ttl_ms"],
                               "fee_type": None}
        self.last_entry[symbol] = now
        self.counts["accepted"] += 1

    def pay_funding(self, symbol, rate, mark, paid_at):
        p = self.positions.get(symbol)
        if p is None:
            return
        payment = -p["direction"] * p["quantity"] * mark * rate
        self.cash += payment
        self.funding += payment
        p["funding"] += payment
        p["funding_entries"].append({"paid_at_ms": paid_at, "rate": rate, "mark_price": mark, "pnl": payment})

    def minute(self, now, bars):
        if self.halted:
            return
        for symbol, bar in bars.items():
            opening, high, low, close, volume = map(float, bar)
            order = self.orders.get(symbol)
            if order is not None and now >= order["expires"]:
                self.orders.pop(symbol)
                self.counts["expired"] += 1
                order = None
            if order is not None and now >= order["submitted"]:
                if order["fee_type"] is None:
                    quote = self.last_close.get(symbol, opening)
                    crosses = order["limit"] >= quote if order["direction"] > 0 else order["limit"] <= quote
                    order["fee_type"] = "TAKER" if crosses else "MAKER"
                crossed = low < order["limit"] if order["direction"] > 0 else high > order["limit"]
                if crossed and volume * self.config["entry_participation_cap"] >= order["quantity"]:
                    rate = self.config["taker_fee_rate"] if order["fee_type"] == "TAKER" else self.config["maker_fee_rate"]
                    fee = order["quantity"] * order["limit"] * rate
                    self.cash -= fee
                    self.fees += fee
                    self.positions[symbol] = {**order, "entry": order["limit"], "opened_at_ms": now,
                                              "entry_fee": fee, "funding": 0.0, "funding_entries": []}
                    self.orders.pop(symbol)
                    self.counts["fills"] += 1
            p = self.positions.get(symbol)
            if p is not None:
                stop_hit = low <= p["stop"] if p["direction"] > 0 else high >= p["stop"]
                target_hit = high >= p["target"] if p["direction"] > 0 else low <= p["target"]
                if stop_hit or target_hit:
                    reason = "STOP_LOSS" if stop_hit else "TAKE_PROFIT"
                    if stop_hit:
                        trigger = min(opening, p["stop"]) if p["direction"] > 0 else max(opening, p["stop"])
                    else:
                        trigger = max(opening, p["target"]) if p["direction"] > 0 else min(opening, p["target"])
                    exit_price = trigger * (1 - p["direction"] * self.config["slippage_bps"] / 10_000)
                    gross = p["direction"] * (exit_price - p["entry"]) * p["quantity"]
                    fee = exit_price * p["quantity"] * self.config["taker_fee_rate"]
                    self.cash += gross - fee
                    self.gross += gross
                    self.fees += fee
                    self.trades.append({"symbol": symbol, "side": p["side"], "opened_at_ms": p["opened_at_ms"],
                                        "closed_at_ms": now + MINUTE, "entry": p["entry"], "exit": exit_price,
                                        "quantity": p["quantity"], "gross_pnl": gross, "fees": p["entry_fee"] + fee,
                                        "funding_pnl": p["funding"], "net_pnl": gross - p["entry_fee"] - fee + p["funding"],
                                        "funding_entries": p["funding_entries"], "exit_reason": reason,
                                        "entry_fee_type": p["fee_type"]})
                    self.positions.pop(symbol)
            self.last_close[symbol] = close
        equity = self.equity(self.last_close)
        self.peak = max(self.peak, equity)
        self.max_drawdown = max(self.max_drawdown, (self.peak - equity) / self.peak)
        if equity <= 0:
            self.halted = "NONPOSITIVE_EQUITY_UNSUPPORTED_LIQUIDATION"
        if (now + MINUTE) % (1440 * MINUTE) == 0:
            self.equity_daily.append({"at_ms": now + MINUTE, "equity": equity})

    def result(self):
        equity = self.equity(self.last_close)
        wins = sum(t["net_pnl"] > 0 for t in self.trades)
        gain = sum(t["net_pnl"] for t in self.trades if t["net_pnl"] > 0)
        loss = -sum(t["net_pnl"] for t in self.trades if t["net_pnl"] < 0)
        return {"template_id": self.template["template_id"], "name": self.template["name"],
                "ending_equity": None if self.halted else equity,
                "roi": None if self.halted else equity / self.config["initial_equity"] - 1,
                "win_rate": wins / len(self.trades) if self.trades else None, "wins": wins,
                "closed_trade_count": len(self.trades), "fees": self.fees, "funding_pnl": self.funding,
                "realized_gross_pnl": self.gross, "unrealized_pnl": equity - self.cash,
                "max_drawdown": self.max_drawdown, "profit_factor": gain / loss if loss > 0 else None,
                "pending_orders": len(self.orders), "open_positions": len(self.positions),
                "counts": dict(self.counts), "wait_reasons": dict(self.wait_reasons),
                "halted_reason": self.halted, "trades": self.trades, "daily_equity": self.equity_daily,
                "open_position_ledger": deepcopy(self.positions), "pending_order_ledger": deepcopy(self.orders)}


def run_proxy(database, manifest, config, *, progress=None):
    database = Path(database).resolve()
    start = int(pd.Timestamp(manifest["window_start"]).timestamp() * 1000)
    end = int(pd.Timestamp(manifest["window_end"]).timestamp() * 1000)
    if not manifest["complete_data"]:
        raise ValueError("PROXY_COMPLETE_HISTORY_REQUIRED")
    prices, frame_data, hourly, funding_rows = {}, {}, {}, {}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        for symbol in manifest["symbols"]:
            raw = pd.read_sql_query("SELECT * FROM bars WHERE symbol=? ORDER BY open_time_ms", db, params=(symbol,))
            expected = np.arange(int(raw["open_time_ms"].iloc[0]), end, MINUTE)
            if not np.array_equal(raw["open_time_ms"].to_numpy(), expected):
                raise ValueError("PROXY_MINUTE_GAP_OR_DUPLICATE")
            for frame in (5, 15):
                frame_data[(symbol, frame)] = features(aggregate_minutes(raw, frame))
            hourly[symbol] = features(aggregate_minutes(raw, 60))
            active = raw[(raw["open_time_ms"] >= start) & (raw["open_time_ms"] < end)]
            if len(active) != (end - start) // MINUTE:
                raise ValueError("PROXY_WINDOW_NOT_COVERED")
            prices[symbol] = active[["open", "high", "low", "close", "volume"]].to_numpy()
            funding_rows[symbol] = [tuple(r) for r in db.execute("SELECT payment_time_ms,rate FROM funding WHERE symbol=? ORDER BY payment_time_ms", (symbol,))]
    accounts = [ProxyAccount(config, template) for template in config["templates"]]
    feature_rows = {key: {int(t): dict(zip(df.columns, row)) for t, row in zip(df.index, df.to_numpy())}
                    for key, df in frame_data.items()}
    hour_rows = {symbol: {int(t): dict(zip(df.columns, row)) for t, row in zip(df.index, df.to_numpy())}
                 for symbol, df in hourly.items()}
    last_hours, last_funding = {}, dict.fromkeys(manifest["symbols"])
    funding_indexes = dict.fromkeys(manifest["symbols"], 0)
    known_funding_indexes = dict.fromkeys(manifest["symbols"], 0)
    for index, now in enumerate(range(start, end, MINUTE)):
        if now % (60 * MINUTE) == 0:
            last_hours.update({s: hour_rows[s].get(now) for s in manifest["symbols"]})
        current_prices = {s: float(prices[s][index - 1][3]) if index else float(prices[s][0][0]) for s in manifest["symbols"]}
        payments = []
        for symbol in manifest["symbols"]:
            rows = funding_rows[symbol]
            # A payment 5ms after the scan cannot be an input at that scan.
            while known_funding_indexes[symbol] < len(rows) and rows[known_funding_indexes[symbol]][0] <= now:
                _, rate = rows[known_funding_indexes[symbol]]
                last_funding[symbol] = rate
                known_funding_indexes[symbol] += 1
            while funding_indexes[symbol] < len(rows) and rows[funding_indexes[symbol]][0] < now + MINUTE:
                paid_at, rate = rows[funding_indexes[symbol]]
                payments.append((symbol, rate, paid_at))
                funding_indexes[symbol] += 1
        for account in accounts:
            interval = account.template["scan_interval_minutes"]
            if now % (interval * MINUTE) == 0:
                candidates = {s: signal(account.template, feature_rows[(s, interval)][now], last_hours.get(s), last_funding[s])
                              for s in manifest["symbols"] if now in feature_rows[(s, interval)]}
                account.decide(now, candidates, current_prices)
            # Account settlement within the minute uses exposure before fills;
            # the not-yet-known rate above never changes the signal inputs.
            for symbol, rate, paid_at in payments:
                account.pay_funding(symbol, rate, current_prices[symbol], paid_at)
            account.minute(now, {s: prices[s][index] for s in manifest["symbols"]})
        if progress and (now + MINUTE) % (30 * 1440 * MINUTE) == 0:
            progress({"evaluated_through": pd.Timestamp(now + MINUTE, unit="ms", tz="UTC").isoformat(),
                      "closed_trades": {a.template["template_id"]: len(a.trades) for a in accounts}})
    return {"schema_version": VERSION, "status": "COMPLETED", "ai_calls": 0,
            "decision_source": "DETERMINISTIC_TECHNICAL_PROXY_NOT_AI", "exchange": "BINANCE_UM_NOT_GATE",
            "config": config, "config_sha256": sha(config), "window_start": manifest["window_start"],
            "window_end": manifest["window_end"], "data_manifest": manifest,
            "results": [a.result() for a in accounts],
            "limitations": ["These fixed rules are explicit approximations of AI styles, not Bonsai decisions or NoFX measured returns.",
                            "News, historical OI/orderbook and dynamic AI stop/size/leverage decisions are absent.",
                            "Uses Binance USD-M price/base-volume history; does not establish Gate execution or Gate-specific profits.",
                            "Limit entries require strict crossing and enough 1% volume capacity; queues and partial fills are not simulated.",
                            "Maker/taker classification uses prior-close submission proxy; protective market exits apply adverse 2bps slippage.",
                            "Stop wins same-minute stop/target ambiguity; static ATR/structure exits replace autonomous management.",
                            "Settled funding is paid by exposure before that minute's fills, using last minute close proxy; millisecond/intrabar sequencing is unavailable.",
                            "Minute-close drawdown is not intraminute drawdown; historical risk tiers and exact cross-margin liquidation are unavailable."]}
