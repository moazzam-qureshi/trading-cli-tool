"""SMC + indicator analysis engine for trade-cli."""
from __future__ import annotations

import bisect
from dataclasses import dataclass, asdict, field
from typing import Optional

import numpy as np
import pandas as pd
from binance.client import Client

# ──────────────────────────────────────────────────────────────────
# Klines fetching
# ──────────────────────────────────────────────────────────────────

INTERVAL_MAP = {
    "1m": Client.KLINE_INTERVAL_1MINUTE,
    "5m": Client.KLINE_INTERVAL_5MINUTE,
    "15m": Client.KLINE_INTERVAL_15MINUTE,
    "30m": Client.KLINE_INTERVAL_30MINUTE,
    "1h": Client.KLINE_INTERVAL_1HOUR,
    "4h": Client.KLINE_INTERVAL_4HOUR,
    "1d": Client.KLINE_INTERVAL_1DAY,
    "1w": Client.KLINE_INTERVAL_1WEEK,
}


def fetch_klines(client: Client, symbol: str, interval: str, limit: int = 300) -> pd.DataFrame:
    """Fetch klines. Paginates with endTime cursor when limit > 1000 (Binance per-call cap)."""
    iv = INTERVAL_MAP.get(interval.lower())
    if iv is None:
        raise ValueError(f"Bad interval: {interval}. Use {list(INTERVAL_MAP)}")

    columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "_",
    ]

    if limit <= 1000:
        raw = client.get_klines(symbol=symbol.upper(), interval=iv, limit=limit)
    else:
        # Walk backwards from "now" in 1000-bar chunks until we have `limit` bars
        raw: list = []
        end_time: Optional[int] = None
        remaining = limit
        while remaining > 0:
            chunk_size = min(1000, remaining)
            kwargs = {"symbol": symbol.upper(), "interval": iv, "limit": chunk_size}
            if end_time is not None:
                kwargs["endTime"] = end_time
            chunk = client.get_klines(**kwargs)
            if not chunk:
                break
            raw = chunk + raw  # prepend (we're going backwards)
            # Next page ends 1ms before this chunk's first bar
            end_time = int(chunk[0][0]) - 1
            remaining -= len(chunk)
            if len(chunk) < chunk_size:
                break  # exchange returned less than asked → no more history

    df = pd.DataFrame(raw, columns=columns)
    for c in ["open", "high", "low", "close", "volume", "quote_volume", "taker_base", "taker_quote"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    df = df.set_index("open_time")
    # Dedup in case overlapping chunks; keep first
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


# ──────────────────────────────────────────────────────────────────
# Indicators
# ──────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h_l = df["high"] - df["low"]
    h_pc = (df["high"] - df["close"].shift()).abs()
    l_pc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def bollinger(close: pd.Series, period: int = 20, std: float = 2.0):
    mid = sma(close, period)
    s = close.rolling(period).std()
    return mid + std * s, mid, mid - std * s


def adx(df: pd.DataFrame, period: int = 14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr_v = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_v
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_v
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False).mean(), plus_di, minus_di


def vwap(df: pd.DataFrame) -> pd.Series:
    typ = (df["high"] + df["low"] + df["close"]) / 3
    return (typ * df["volume"]).cumsum() / df["volume"].cumsum()


# ──────────────────────────────────────────────────────────────────
# Market Structure (swings, BOS, CHoCH)
# ──────────────────────────────────────────────────────────────────

@dataclass
class Swing:
    index: int
    timestamp: str
    price: float
    kind: str  # "HH", "HL", "LH", "LL", or initial "H"/"L"


def detect_swings(df: pd.DataFrame, lookback: int = 3) -> list[Swing]:
    """Fractal swing detection: a high is a swing if higher than N bars left & right."""
    swings: list[Swing] = []
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    raw: list[tuple[int, float, str]] = []
    for i in range(lookback, n - lookback):
        if highs[i] == max(highs[i - lookback:i + lookback + 1]):
            raw.append((i, highs[i], "H"))
        if lows[i] == min(lows[i - lookback:i + lookback + 1]):
            raw.append((i, lows[i], "L"))
    raw.sort(key=lambda x: x[0])

    # Alternate: enforce H,L,H,L pattern (collapse runs by keeping max H or min L)
    cleaned: list[tuple[int, float, str]] = []
    for s in raw:
        if not cleaned or cleaned[-1][2] != s[2]:
            cleaned.append(s)
        else:
            if s[2] == "H" and s[1] > cleaned[-1][1]:
                cleaned[-1] = s
            elif s[2] == "L" and s[1] < cleaned[-1][1]:
                cleaned[-1] = s

    # Classify HH/HL/LH/LL
    last_high: Optional[float] = None
    last_low: Optional[float] = None
    for idx, price, kind in cleaned:
        ts = df.index[idx].isoformat()
        if kind == "H":
            label = "HH" if last_high is not None and price > last_high else ("LH" if last_high is not None else "H")
            last_high = price
        else:
            label = "LL" if last_low is not None and price < last_low else ("HL" if last_low is not None else "L")
            last_low = price
        swings.append(Swing(index=idx, timestamp=ts, price=price, kind=label))
    return swings


def structure_summary(swings: list[Swing], current_price: float) -> dict:
    """Determine trend bias + recent BOS/CHoCH events."""
    if len(swings) < 4:
        return {"trend": "Unknown", "reason": "Not enough swings"}

    recent = swings[-6:]
    highs = [s for s in recent if s.kind in ("HH", "LH", "H")]
    lows = [s for s in recent if s.kind in ("LL", "HL", "L")]

    last_two_h = [s.kind for s in recent if s.kind in ("HH", "LH")][-2:]
    last_two_l = [s.kind for s in recent if s.kind in ("HL", "LL")][-2:]

    if last_two_h == ["HH", "HH"] or (recent[-1].kind == "HH" and any(s.kind == "HL" for s in recent[-3:])):
        trend = "Bullish"
    elif last_two_l == ["LL", "LL"] or (recent[-1].kind == "LL" and any(s.kind == "LH" for s in recent[-3:])):
        trend = "Bearish"
    elif "HH" in [s.kind for s in recent] and "HL" in [s.kind for s in recent]:
        trend = "Bullish"
    elif "LL" in [s.kind for s in recent] and "LH" in [s.kind for s in recent]:
        trend = "Bearish"
    else:
        trend = "Ranging"

    # BOS / CHoCH detection
    last_swing = swings[-1]
    prev_high = next((s.price for s in reversed(swings[:-1]) if s.kind in ("HH", "LH", "H")), None)
    prev_low = next((s.price for s in reversed(swings[:-1]) if s.kind in ("HL", "LL", "L")), None)

    events = []
    if prev_high and current_price > prev_high:
        events.append(f"BOS bullish (broke {prev_high:.6f})")
    if prev_low and current_price < prev_low:
        events.append(f"BOS bearish (broke {prev_low:.6f})")

    return {
        "trend": trend,
        "current_price": current_price,
        "last_swing": {"kind": last_swing.kind, "price": last_swing.price, "time": last_swing.timestamp},
        "prev_high": prev_high,
        "prev_low": prev_low,
        "events": events,
        "swing_pattern": [s.kind for s in recent],
    }


# ──────────────────────────────────────────────────────────────────
# Liquidity (equal highs/lows, sweeps)
# ──────────────────────────────────────────────────────────────────

def find_liquidity(swings: list[Swing], tolerance_pct: float = 0.3) -> dict:
    """Detect equal highs/lows within tolerance %, and recent sweeps."""
    highs = [s for s in swings if s.kind in ("HH", "LH", "H")]
    lows = [s for s in swings if s.kind in ("HL", "LL", "L")]

    def equal_levels(items: list[Swing]) -> list[dict]:
        out = []
        for i in range(len(items)):
            cluster = [items[i]]
            for j in range(i + 1, len(items)):
                if abs(items[j].price - items[i].price) / items[i].price * 100 <= tolerance_pct:
                    cluster.append(items[j])
            if len(cluster) >= 2:
                out.append({
                    "price": float(np.mean([s.price for s in cluster])),
                    "count": len(cluster),
                    "indices": [s.index for s in cluster],
                })
        # de-dup by price
        seen = set()
        unique = []
        for o in out:
            key = round(o["price"], 6)
            if key not in seen:
                seen.add(key)
                unique.append(o)
        return unique

    return {
        "equal_highs": equal_levels(highs),
        "equal_lows": equal_levels(lows),
        "buy_side_liquidity": [{"price": h.price, "time": h.timestamp} for h in highs[-3:]],
        "sell_side_liquidity": [{"price": l.price, "time": l.timestamp} for l in lows[-3:]],
    }


def detect_sweep(df: pd.DataFrame, swings: list[Swing], bars: int = 10) -> Optional[dict]:
    """Did price recently spike past a swing then close back? = liquidity sweep."""
    if len(swings) < 2 or len(df) < bars:
        return None
    recent = df.tail(bars)
    last_close = float(df["close"].iloc[-1])

    for s in reversed(swings[-6:]):
        if s.kind in ("HH", "LH", "H"):
            spike = recent["high"].max()
            if spike > s.price and last_close < s.price:
                return {"type": "bearish_sweep", "level": s.price, "spike": float(spike), "back_below": last_close}
        elif s.kind in ("HL", "LL", "L"):
            dip = recent["low"].min()
            if dip < s.price and last_close > s.price:
                return {"type": "bullish_sweep", "level": s.price, "dip": float(dip), "back_above": last_close}
    return None


# ──────────────────────────────────────────────────────────────────
# OTE (Optimal Trade Entry) — late-entry filter
# ──────────────────────────────────────────────────────────────────
# SMC-orthodox check: a trend-continuation entry should sit inside the
# 62–79% Fib retrace of the most recent impulse leg. If entry is above
# the 62% retrace (long) it means price hasn't pulled back enough — the
# trade is "chasing" the move. Reject regardless of confluence score.

def _impulse_leg(direction: str, mtf_df: pd.DataFrame, swings: list[Swing],
                 sweep: Optional[dict]) -> Optional[tuple[float, float]]:
    """Return (origin, extreme) for the most recent impulse leg.
    Long:  origin = sweep dip (or most recent LL/HL/L swing), extreme = highest high since.
    Short: origin = sweep spike (or most recent HH/LH/H), extreme = lowest low since.
    """
    if len(mtf_df) < 5:
        return None

    if direction == "long":
        if sweep and sweep.get("type") == "bullish_sweep":
            origin = float(sweep["dip"])
            origin_idx = mtf_df["low"].tail(20).idxmin()
        else:
            lows = [s for s in swings if s.kind in ("HL", "LL", "L")]
            if not lows:
                return None
            last_low = lows[-1]
            origin = float(last_low.price)
            origin_idx = mtf_df.index[last_low.index] if last_low.index < len(mtf_df) else mtf_df.index[-5]
        post = mtf_df.loc[origin_idx:]
        if len(post) < 2:
            return None
        extreme = float(post["high"].max())
        if extreme <= origin:
            return None
        return (origin, extreme)
    else:
        if sweep and sweep.get("type") == "bearish_sweep":
            origin = float(sweep["spike"])
            origin_idx = mtf_df["high"].tail(20).idxmax()
        else:
            highs = [s for s in swings if s.kind in ("HH", "LH", "H")]
            if not highs:
                return None
            last_high = highs[-1]
            origin = float(last_high.price)
            origin_idx = mtf_df.index[last_high.index] if last_high.index < len(mtf_df) else mtf_df.index[-5]
        post = mtf_df.loc[origin_idx:]
        if len(post) < 2:
            return None
        extreme = float(post["low"].min())
        if extreme >= origin:
            return None
        return (origin, extreme)


def ote_check(direction: str, mtf_df: pd.DataFrame, swings: list[Swing],
              sweep: Optional[dict], current_price: float,
              ote_top: float = 0.62) -> dict:
    """Reject if entry is above the OTE top (long) — i.e., price hasn't retraced
    at least `ote_top` of the impulse leg.

    Returns: {valid: bool, retrace: float, origin: float, extreme: float, reason: str}
    valid=True means the entry is inside (or below) the OTE zone.
    """
    leg = _impulse_leg(direction, mtf_df, swings, sweep)
    if leg is None:
        # No impulse identifiable → fail-open (don't block on insufficient data)
        return {"valid": True, "retrace": None, "reason": "no_impulse_leg"}
    origin, extreme = leg
    # retrace = how far back toward origin we've come from extreme. 0 = at extreme, 1 = back at origin.
    if direction == "long":
        retrace = (extreme - current_price) / (extreme - origin)
        ote_top_price = extreme - ote_top * (extreme - origin)
        valid = current_price <= ote_top_price
        reason = (f"in_OTE retrace={retrace:.2f}" if valid
                  else f"extended retrace={retrace:.2f}<{ote_top:.2f}")
    else:
        retrace = (current_price - extreme) / (origin - extreme)
        ote_top_price = extreme + ote_top * (origin - extreme)
        valid = current_price >= ote_top_price
        reason = (f"in_OTE retrace={retrace:.2f}" if valid
                  else f"extended retrace={retrace:.2f}<{ote_top:.2f}")
    return {"valid": valid, "retrace": round(retrace, 3),
            "origin": round(origin, 6), "extreme": round(extreme, 6),
            "ote_top": round(ote_top_price, 6), "reason": reason}


# ──────────────────────────────────────────────────────────────────
# Target reachability — overhead supply / floor demand check
# ──────────────────────────────────────────────────────────────────
# Pro target placement: targets sit AT magnets (prior swing highs / supply
# zones), not floating beyond untested overhead. Reject any setup whose 1.5R
# target lands above the recent N-bar MTF high.

def target_reachable(direction: str, entry: float, target: float,
                     mtf_df: pd.DataFrame, lookback: int = 96,
                     buffer: float = 1.002) -> dict:
    """For longs: target must sit at-or-below max(MTF high, last `lookback` bars)
    × buffer. For shorts: target must sit at-or-above min(MTF low) / buffer.

    Returns: {reachable: bool, ceiling: float, headroom_pct: float, reason: str}
    """
    if len(mtf_df) < lookback:
        lookback = len(mtf_df)
    window = mtf_df.tail(lookback)
    if direction == "long":
        ceiling = float(window["high"].max())
        limit = ceiling * buffer
        reachable = target <= limit
        headroom = (ceiling - entry) / entry * 100.0
        reason = (f"target_below_ceiling ceil={ceiling:.6f}" if reachable
                  else f"target_{target:.6f}_above_ceil_{ceiling:.6f}")
        return {"reachable": reachable, "ceiling": round(ceiling, 6),
                "headroom_pct": round(headroom, 2), "reason": reason}
    else:
        floor = float(window["low"].min())
        limit = floor / buffer
        reachable = target >= limit
        headroom = (entry - floor) / entry * 100.0
        reason = (f"target_above_floor floor={floor:.6f}" if reachable
                  else f"target_{target:.6f}_below_floor_{floor:.6f}")
        return {"reachable": reachable, "ceiling": round(floor, 6),
                "headroom_pct": round(headroom, 2), "reason": reason}


# ──────────────────────────────────────────────────────────────────
# Volume Spread Analysis (VSA / Tom Williams / Wyckoff)
# ──────────────────────────────────────────────────────────────────
# Price alone can be faked by smart money pushing thin orderbooks; the
# VOLUME signature is much harder to disguise because real positioning
# requires real paper changing hands. These four patterns flag whether
# what we're seeing is real demand/supply or a trap.
#
#   spring          — broke recent low + closed back above + LOW volume
#                     = stop-hunt with no real selling pressure (bullish)
#   no_supply       — down bar on shrinking volume
#                     = no sellers showing up; pullback has no force (bullish)
#   up_thrust       — new high + close in lower half + HIGH volume
#                     = breakout trap, smart money sold into it (BEARISH — hard reject longs)
#   stopping_volume — down bar w/ high volume but close in upper half / narrow range
#                     = absorption, big buyer eating the supply (bullish)

def vsa_bar(df: pd.DataFrame, i: int, vol_avg_period: int = 20,
            high_vol_mult: float = 1.5, low_vol_mult: float = 0.7,
            extreme_vol_mult: float = 1.8, swing_lookback: int = 10) -> str:
    """Classify bar at positional index i. Returns one of:
    'spring' | 'no_supply' | 'up_thrust' | 'stopping_volume' | 'none'.
    """
    if i < 1 or i >= len(df):
        return "none"
    bar = df.iloc[i]
    high = float(bar["high"]); low = float(bar["low"])
    open_ = float(bar["open"]); close = float(bar["close"])
    vol = float(bar["volume"])
    rng = high - low
    if rng <= 0:
        return "none"
    body = close - open_
    close_pos = (close - low) / rng  # 0 = closed at low, 1 = closed at high

    start = max(0, i - vol_avg_period)
    vol_window = df["volume"].iloc[start:i]
    if len(vol_window) < 5:
        return "none"
    vol_avg = float(vol_window.mean())
    if vol_avg <= 0:
        return "none"
    vol_ratio = vol / vol_avg

    sw_start = max(0, i - swing_lookback)
    prior_highs = df["high"].iloc[sw_start:i]
    prior_lows = df["low"].iloc[sw_start:i]
    if len(prior_highs) < 3:
        return "none"
    prior_high_max = float(prior_highs.max())
    prior_low_min = float(prior_lows.min())

    # Up-thrust: took out recent high, closed in lower half, with high volume.
    # The up wick + weak close + heavy volume is the smart-money distribution
    # signature. Check this FIRST so it overrides anything else.
    if high > prior_high_max and close_pos < 0.5 and vol_ratio > 1.3:
        return "up_thrust"

    # Spring: took out recent low, closed back above it, on LOW volume.
    if low < prior_low_min and close > prior_low_min and vol_ratio < low_vol_mult:
        return "spring"

    # Stopping volume: down bar, high volume, but close in upper half (absorption).
    if body < 0 and vol_ratio > extreme_vol_mult and close_pos > 0.5:
        return "stopping_volume"

    # No-supply: down bar on shrinking volume.
    if body < 0 and vol_ratio < 0.6:
        return "no_supply"

    return "none"


def vsa_signature(df: pd.DataFrame, lookback: int = 5) -> dict:
    """Return VSA reads for the most recent `lookback` bars on this dataframe.

    Output:
        {
          "latest_bar": "spring" | "up_thrust" | ... | "none",
          "recent_signals": [{"offset": int (0=newest), "signal": str}, ...],
          "has_up_thrust": bool,        # any of the recent bars
          "has_spring": bool,
          "has_no_supply": bool,
          "has_stopping_volume": bool,
        }
    """
    n = len(df)
    if n < 25:
        return {"latest_bar": "none", "recent_signals": [],
                "has_up_thrust": False, "has_spring": False,
                "has_no_supply": False, "has_stopping_volume": False}

    last_sig = vsa_bar(df, n - 1)
    recent: list[dict] = []
    for k in range(lookback):
        idx = n - 1 - k
        if idx < 1:
            break
        s = vsa_bar(df, idx)
        if s != "none":
            recent.append({"offset": k, "signal": s})

    sigs = {r["signal"] for r in recent}
    return {
        "latest_bar": last_sig,
        "recent_signals": recent,
        "has_up_thrust": "up_thrust" in sigs,
        "has_spring": "spring" in sigs,
        "has_no_supply": "no_supply" in sigs,
        "has_stopping_volume": "stopping_volume" in sigs,
    }


# ──────────────────────────────────────────────────────────────────
# Order Blocks
# ──────────────────────────────────────────────────────────────────

def detect_order_blocks(df: pd.DataFrame, lookback: int = 50, move_threshold: float = 1.5) -> dict:
    """OB = last opposing candle before a strong directional move (>= move_threshold * ATR)."""
    if len(df) < lookback + 5:
        return {"bullish": [], "bearish": []}

    a = atr(df, 14)
    bullish, bearish = [], []
    df_tail = df.tail(lookback).copy()
    df_tail["body"] = df_tail["close"] - df_tail["open"]

    for i in range(2, len(df_tail) - 2):
        # Look at this candle and 1-3 candles after
        run_size = df_tail["close"].iloc[i + 1:i + 4].max() - df_tail["open"].iloc[i + 1]
        run_size_down = df_tail["open"].iloc[i + 1] - df_tail["close"].iloc[i + 1:i + 4].min()
        atr_here = a.iloc[df_tail.index.get_loc(df_tail.index[i])]
        if pd.isna(atr_here) or atr_here == 0:
            continue

        # Bullish OB: a red candle followed by strong up-move
        if df_tail["body"].iloc[i] < 0 and run_size >= move_threshold * atr_here:
            bullish.append({
                "time": df_tail.index[i].isoformat(),
                "low": float(df_tail["low"].iloc[i]),
                "high": float(df_tail["high"].iloc[i]),
                "open": float(df_tail["open"].iloc[i]),
                "close": float(df_tail["close"].iloc[i]),
            })
        # Bearish OB: green candle followed by strong down-move
        if df_tail["body"].iloc[i] > 0 and run_size_down >= move_threshold * atr_here:
            bearish.append({
                "time": df_tail.index[i].isoformat(),
                "low": float(df_tail["low"].iloc[i]),
                "high": float(df_tail["high"].iloc[i]),
                "open": float(df_tail["open"].iloc[i]),
                "close": float(df_tail["close"].iloc[i]),
            })

    return {"bullish": bullish[-3:], "bearish": bearish[-3:]}


# ──────────────────────────────────────────────────────────────────
# Fair Value Gaps
# ──────────────────────────────────────────────────────────────────

def detect_fvg(df: pd.DataFrame, lookback: int = 50) -> dict:
    """FVG: 3-candle gap where candle1.high < candle3.low (bull) or candle1.low > candle3.high (bear)."""
    if len(df) < 3:
        return {"bullish": [], "bearish": []}
    bull, bear = [], []
    tail = df.tail(lookback)
    for i in range(len(tail) - 2):
        c1, c3 = tail.iloc[i], tail.iloc[i + 2]
        current = float(df["close"].iloc[-1])
        if c1["high"] < c3["low"]:
            # bullish FVG: zone is [c1.high, c3.low]
            if current > c1["high"]:  # not yet filled below
                bull.append({
                    "time": tail.index[i].isoformat(),
                    "low": float(c1["high"]),
                    "high": float(c3["low"]),
                    "filled": current < c3["low"],
                })
        if c1["low"] > c3["high"]:
            if current < c1["low"]:
                bear.append({
                    "time": tail.index[i].isoformat(),
                    "low": float(c3["high"]),
                    "high": float(c1["low"]),
                    "filled": current > c3["high"],
                })
    return {"bullish": bull[-3:], "bearish": bear[-3:]}


# ──────────────────────────────────────────────────────────────────
# Combined analysis
# ──────────────────────────────────────────────────────────────────

def analyze_symbol(client: Client, symbol: str, interval: str = "1h", limit: int = 300) -> dict:
    df = fetch_klines(client, symbol, interval, limit)
    close = df["close"]
    last = float(close.iloc[-1])

    rsi_v = float(rsi(close).iloc[-1])
    macd_l, sig_l, hist = macd(close)
    bb_u, bb_m, bb_l = bollinger(close)
    atr_v = float(atr(df).iloc[-1])
    adx_v, p_di, m_di = adx(df)
    ema20 = float(ema(close, 20).iloc[-1])
    ema50 = float(ema(close, 50).iloc[-1])
    ema200 = float(ema(close, 200).iloc[-1]) if len(df) >= 200 else None

    swings = detect_swings(df)
    structure = structure_summary(swings, last)
    liquidity = find_liquidity(swings)
    sweep = detect_sweep(df, swings)
    obs = detect_order_blocks(df)
    fvgs = detect_fvg(df)

    # Volume signal
    vol_avg20 = float(df["volume"].rolling(20).mean().iloc[-1])
    vol_now = float(df["volume"].iloc[-1])
    vol_ratio = round(vol_now / vol_avg20, 2) if vol_avg20 else None

    return {
        "symbol": symbol.upper(),
        "interval": interval,
        "current_price": last,
        "indicators": {
            "rsi": round(rsi_v, 2),
            "rsi_signal": "Overbought" if rsi_v > 70 else "Oversold" if rsi_v < 30 else "Neutral",
            "macd": {"line": float(macd_l.iloc[-1]), "signal": float(sig_l.iloc[-1]), "hist": float(hist.iloc[-1])},
            "macd_cross": "Bullish" if hist.iloc[-1] > 0 and hist.iloc[-2] < 0 else
                          "Bearish" if hist.iloc[-1] < 0 and hist.iloc[-2] > 0 else "None",
            "ema20": ema20,
            "ema50": ema50,
            "ema200": ema200,
            "bb_upper": float(bb_u.iloc[-1]),
            "bb_lower": float(bb_l.iloc[-1]),
            "atr": atr_v,
            "atr_pct": round(atr_v / last * 100, 2),
            "adx": round(float(adx_v.iloc[-1]), 2),
            "adx_strength": "Strong" if adx_v.iloc[-1] > 25 else "Weak",
            "trend_di": "Bullish" if p_di.iloc[-1] > m_di.iloc[-1] else "Bearish",
            "volume_ratio_vs_20avg": vol_ratio,
        },
        "structure": structure,
        "liquidity": liquidity,
        "recent_sweep": sweep,
        "order_blocks": obs,
        "fvg": fvgs,
    }


# ──────────────────────────────────────────────────────────────────
# Confluence score (the A+ setup grader)
# ──────────────────────────────────────────────────────────────────

def _analyze_df(df: pd.DataFrame, interval: str) -> dict:
    """Same as analyze_symbol but operates on a pre-fetched dataframe (for backtests)."""
    close = df["close"]
    last = float(close.iloc[-1])

    rsi_v = float(rsi(close).iloc[-1])
    macd_l, sig_l, hist = macd(close)
    bb_u, bb_m, bb_l = bollinger(close)
    atr_v = float(atr(df).iloc[-1])
    adx_v, p_di, m_di = adx(df)

    swings = detect_swings(df)
    structure = structure_summary(swings, last)
    liquidity = find_liquidity(swings)
    sweep = detect_sweep(df, swings)
    obs = detect_order_blocks(df)
    fvgs = detect_fvg(df)

    vol_avg20 = float(df["volume"].rolling(20).mean().iloc[-1])
    vol_now = float(df["volume"].iloc[-1])
    vol_ratio = round(vol_now / vol_avg20, 2) if vol_avg20 else None

    return {
        "interval": interval,
        "current_price": last,
        "indicators": {
            "rsi": round(rsi_v, 2),
            "atr": atr_v,
            "adx": round(float(adx_v.iloc[-1]), 2),
            "volume_ratio_vs_20avg": vol_ratio,
        },
        "structure": structure,
        "swings": swings,
        "liquidity": liquidity,
        "recent_sweep": sweep,
        "order_blocks": obs,
        "fvg": fvgs,
    }


def score_from_dfs(htf_df: pd.DataFrame, mtf_df: pd.DataFrame, ltf_df: pd.DataFrame) -> dict:
    """Confluence scoring against pre-sliced dataframes — used by backtest + live scoring."""
    htf = _analyze_df(htf_df, "4h")
    mtf = _analyze_df(mtf_df, "1h")
    ltf = _analyze_df(ltf_df, "15m")

    score = 0
    reasons: list[str] = []
    direction = None

    htf_trend = htf["structure"]["trend"]
    if htf_trend == "Bullish":
        direction = "long"
        score += 3
        reasons.append("HTF (4H) bullish")
    elif htf_trend == "Bearish":
        direction = "short"
        score += 3
        reasons.append("HTF (4H) bearish")

    if direction:
        sweep = mtf.get("recent_sweep")
        if sweep:
            want = "bullish_sweep" if direction == "long" else "bearish_sweep"
            if sweep["type"] == want:
                score += 3
                reasons.append(f"MTF {sweep['type']}")

        obs = mtf["order_blocks"]
        bucket = obs["bullish"] if direction == "long" else obs["bearish"]
        price = mtf["current_price"]
        for ob in bucket:
            if ob["low"] <= price <= ob["high"]:
                score += 2
                reasons.append("Inside OB on 1H")
                break

        fvgs = mtf["fvg"]
        bucket_fvg = fvgs["bullish"] if direction == "long" else fvgs["bearish"]
        for fvg in bucket_fvg:
            if fvg["low"] <= price <= fvg["high"] and not fvg["filled"]:
                score += 2
                reasons.append("Inside unfilled FVG on 1H")
                break

        ltf_struct = ltf["structure"]["trend"]
        if (direction == "long" and ltf_struct == "Bullish") or (direction == "short" and ltf_struct == "Bearish"):
            score += 2
            reasons.append(f"LTF aligned ({ltf_struct})")

        vol = ltf["indicators"]["volume_ratio_vs_20avg"]
        if vol and vol > 1.5:
            score += 1
            reasons.append(f"Volume spike ({vol}x)")

        rsi_ltf = ltf["indicators"]["rsi"]
        if direction == "long" and 30 < rsi_ltf < 60:
            score += 1
            reasons.append(f"RSI healthy long ({rsi_ltf})")
        elif direction == "short" and 40 < rsi_ltf < 70:
            score += 1
            reasons.append(f"RSI healthy short ({rsi_ltf})")

    # OTE + reachability fields (informational; filters applied at trade-open)
    ote = None
    if direction:
        ote = ote_check(direction, mtf_df, mtf["swings"], mtf.get("recent_sweep"),
                        ltf["current_price"])

    # VSA signature on LTF (entry timeframe) and MTF (context timeframe)
    vsa_ltf = vsa_signature(ltf_df, lookback=3)   # 3 most recent 15m bars
    vsa_mtf = vsa_signature(mtf_df, lookback=5)   # 5 most recent 1h bars

    return {
        "direction": direction,
        "score": score,
        "reasons": reasons,
        "htf_trend": htf_trend,
        "mtf_trend": mtf["structure"]["trend"],
        "ltf_trend": ltf["structure"]["trend"],
        "current_price": ltf["current_price"],
        "ltf_atr": ltf["indicators"]["atr"],
        "ltf_swings": ltf["swings"],
        "ote": ote,
        "vsa_ltf": vsa_ltf,
        "vsa_mtf": vsa_mtf,
    }


def confluence_score(client: Client, symbol: str) -> dict:
    """Score a symbol for A+ setup probability across multiple TFs."""
    htf = analyze_symbol(client, symbol, "4h", 300)
    mtf = analyze_symbol(client, symbol, "1h", 300)
    ltf = analyze_symbol(client, symbol, "15m", 300)

    score = 0
    reasons: list[str] = []
    direction = None

    htf_trend = htf["structure"]["trend"]
    if htf_trend == "Bullish":
        direction = "long"
        score += 3
        reasons.append("⭐⭐⭐ HTF (4H) bullish structure")
    elif htf_trend == "Bearish":
        direction = "short"
        score += 3
        reasons.append("⭐⭐⭐ HTF (4H) bearish structure")
    else:
        reasons.append("HTF unclear — skip")

    if direction:
        sweep = mtf.get("recent_sweep")
        if sweep:
            want = "bullish_sweep" if direction == "long" else "bearish_sweep"
            if sweep["type"] == want:
                score += 3
                reasons.append(f"⭐⭐⭐ MTF (1H) {sweep['type']} detected")

        obs = mtf["order_blocks"]
        bucket = obs["bullish"] if direction == "long" else obs["bearish"]
        price = mtf["current_price"]
        for ob in bucket:
            if ob["low"] <= price <= ob["high"]:
                score += 2
                reasons.append(f"⭐⭐ Price inside {direction} order block on 1H")
                break

        fvgs = mtf["fvg"]
        bucket_fvg = fvgs["bullish"] if direction == "long" else fvgs["bearish"]
        for fvg in bucket_fvg:
            if fvg["low"] <= price <= fvg["high"] and not fvg["filled"]:
                score += 2
                reasons.append(f"⭐⭐ Price inside unfilled {direction} FVG on 1H")
                break

        ltf_struct = ltf["structure"]["trend"]
        if (direction == "long" and ltf_struct == "Bullish") or (direction == "short" and ltf_struct == "Bearish"):
            score += 2
            reasons.append(f"⭐⭐ LTF (15m) structure aligned ({ltf_struct})")

        vol = ltf["indicators"]["volume_ratio_vs_20avg"]
        if vol and vol > 1.5:
            score += 1
            reasons.append(f"⭐ Volume spike on 15m ({vol}x avg)")

        rsi_ltf = ltf["indicators"]["rsi"]
        if direction == "long" and 30 < rsi_ltf < 60:
            score += 1
            reasons.append(f"⭐ LTF RSI in healthy long zone ({rsi_ltf})")
        elif direction == "short" and 40 < rsi_ltf < 70:
            score += 1
            reasons.append(f"⭐ LTF RSI in healthy short zone ({rsi_ltf})")

    verdict = "A+ SETUP" if score >= 8 else "Decent" if score >= 5 else "Skip"

    # OTE filter info + VSA reads (informational on alert; enforced at trade open)
    ote = None
    vsa_ltf = None
    vsa_mtf = None
    if direction:
        try:
            mtf_df = fetch_klines(client, symbol, "1h", 300)
            ltf_df = fetch_klines(client, symbol, "15m", 300)
            ote = ote_check(direction, mtf_df, mtf["swings"], mtf.get("recent_sweep"),
                            ltf["current_price"])
            vsa_ltf = vsa_signature(ltf_df, lookback=3)
            vsa_mtf = vsa_signature(mtf_df, lookback=5)
        except Exception:
            pass

    return {
        "symbol": symbol.upper(),
        "direction": direction,
        "score": score,
        "verdict": verdict,
        "reasons": reasons,
        "htf_trend": htf_trend,
        "mtf_trend": mtf["structure"]["trend"],
        "ltf_trend": ltf["structure"]["trend"],
        "current_price": ltf["current_price"],
        "ote": ote,
        "vsa_ltf": vsa_ltf,
        "vsa_mtf": vsa_mtf,
    }


# ──────────────────────────────────────────────────────────────────
# Vectorized precompute + score_at — for FAST backtests.
# Old confluence_score / score_from_dfs untouched (used live + by current sweeps).
# ──────────────────────────────────────────────────────────────────

SWING_LOOKBACK = 3


@dataclass
class Precomputed:
    df: pd.DataFrame
    interval: str
    rsi_arr: np.ndarray
    atr_arr: np.ndarray
    vol_ratio_arr: np.ndarray
    swings: list  # list[Swing]
    swing_confirm_idx: list  # parallel: bar index at which swing[k] becomes detectable
    obs_bull: list[dict]
    obs_bear: list[dict]
    obs_bull_confirm: list[int]
    obs_bear_confirm: list[int]
    fvgs_bull: list[dict]
    fvgs_bear: list[dict]
    fvgs_bull_confirm: list[int]
    fvgs_bear_confirm: list[int]


def _detect_obs_full(df: pd.DataFrame, move_threshold: float = 1.5) -> tuple[list, list]:
    """Full-series order-block detection. Returns (bullish, bearish) lists with confirm_idx."""
    a = atr(df, 14).values
    closes = df["close"].values
    opens = df["open"].values
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    bull, bear = [], []
    for i in range(2, n - 4):
        atr_here = a[i]
        if not np.isfinite(atr_here) or atr_here == 0:
            continue
        run_up = closes[i + 1:i + 4].max() - opens[i + 1]
        run_dn = opens[i + 1] - closes[i + 1:i + 4].min()
        body = closes[i] - opens[i]
        if body < 0 and run_up >= move_threshold * atr_here:
            bull.append({"confirm_idx": i + 3, "idx": i,
                         "low": float(lows[i]), "high": float(highs[i])})
        if body > 0 and run_dn >= move_threshold * atr_here:
            bear.append({"confirm_idx": i + 3, "idx": i,
                         "low": float(lows[i]), "high": float(highs[i])})
    return bull, bear


def _detect_fvgs_full(df: pd.DataFrame) -> tuple[list, list]:
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    bull, bear = [], []
    for i in range(n - 2):
        if highs[i] < lows[i + 2]:
            bull.append({"confirm_idx": i + 2, "idx": i,
                         "low": float(highs[i]), "high": float(lows[i + 2])})
        if lows[i] > highs[i + 2]:
            bear.append({"confirm_idx": i + 2, "idx": i,
                         "low": float(highs[i + 2]), "high": float(lows[i])})
    return bull, bear


def precompute(df: pd.DataFrame, interval: str) -> Precomputed:
    """Run all vectorizable analysis once on the full series."""
    rsi_arr = rsi(df["close"]).values
    atr_arr = atr(df).values
    vol_avg = df["volume"].rolling(20).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio_arr = df["volume"].values / np.where(vol_avg > 0, vol_avg, np.nan)

    swings = detect_swings(df, lookback=SWING_LOOKBACK)
    swing_confirm = [s.index + SWING_LOOKBACK for s in swings]

    obs_bull, obs_bear = _detect_obs_full(df)
    fvgs_bull, fvgs_bear = _detect_fvgs_full(df)

    return Precomputed(
        df=df, interval=interval,
        rsi_arr=rsi_arr, atr_arr=atr_arr, vol_ratio_arr=vol_ratio_arr,
        swings=swings, swing_confirm_idx=swing_confirm,
        obs_bull=obs_bull, obs_bear=obs_bear,
        obs_bull_confirm=[o["confirm_idx"] for o in obs_bull],
        obs_bear_confirm=[o["confirm_idx"] for o in obs_bear],
        fvgs_bull=fvgs_bull, fvgs_bear=fvgs_bear,
        fvgs_bull_confirm=[f["confirm_idx"] for f in fvgs_bull],
        fvgs_bear_confirm=[f["confirm_idx"] for f in fvgs_bear],
    )


def _swings_up_to(pre: Precomputed, i: int) -> list:
    """O(log n) slice of swings detectable by bar i."""
    cut = bisect.bisect_right(pre.swing_confirm_idx, i)
    return pre.swings[:cut]


def _detect_sweep_fast(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                       i: int, swings: list, bars: int = 10) -> Optional[dict]:
    """Sweep detection using arrays + already-filtered swings."""
    if len(swings) < 2 or i < bars - 1:
        return None
    start = i - bars + 1
    last_close = float(closes[i])
    win_high = float(highs[start:i + 1].max())
    win_low = float(lows[start:i + 1].min())
    for s in reversed(swings[-6:]):
        if s.kind in ("HH", "LH", "H"):
            if win_high > s.price and last_close < s.price:
                return {"type": "bearish_sweep", "level": s.price}
        elif s.kind in ("HL", "LL", "L"):
            if win_low < s.price and last_close > s.price:
                return {"type": "bullish_sweep", "level": s.price}
    return None


def score_at(htf_pre: Precomputed, mtf_pre: Precomputed, ltf_pre: Precomputed,
             htf_i: int, mtf_i: int, ltf_i: int) -> dict:
    """Fast equivalent of score_from_dfs — uses precomputed arrays + bisect lookups."""
    # HTF trend
    htf_swings_now = _swings_up_to(htf_pre, htf_i)
    htf_close = float(htf_pre.df["close"].values[htf_i])
    htf_struct = structure_summary(htf_swings_now, htf_close)
    htf_trend = htf_struct["trend"]

    direction = "long" if htf_trend == "Bullish" else "short" if htf_trend == "Bearish" else None
    score = 3 if direction else 0
    ltf_close = float(ltf_pre.df["close"].values[ltf_i])

    if not direction:
        return {"direction": None, "score": 0, "reasons": [],
                "current_price": ltf_close, "ltf_atr": float(ltf_pre.atr_arr[ltf_i]),
                "ltf_swings": []}

    # MTF sweep
    mtf_swings_now = _swings_up_to(mtf_pre, mtf_i)
    sweep = _detect_sweep_fast(
        mtf_pre.df["close"].values, mtf_pre.df["high"].values, mtf_pre.df["low"].values,
        mtf_i, mtf_swings_now,
    )
    if sweep:
        want = "bullish_sweep" if direction == "long" else "bearish_sweep"
        if sweep["type"] == want:
            score += 3

    mtf_close = float(mtf_pre.df["close"].values[mtf_i])

    # OBs — match slow path: scan only the last 50 MTF bars (lookback window)
    OB_FVG_LOOKBACK = 50
    min_idx_ob = mtf_i - OB_FVG_LOOKBACK
    obs_bucket = mtf_pre.obs_bull if direction == "long" else mtf_pre.obs_bear
    obs_confirm = mtf_pre.obs_bull_confirm if direction == "long" else mtf_pre.obs_bear_confirm
    cut = bisect.bisect_right(obs_confirm, mtf_i)
    recent_obs = [ob for ob in obs_bucket[:cut] if ob["idx"] >= min_idx_ob][-3:]
    for ob in recent_obs:
        if ob["low"] <= mtf_close <= ob["high"]:
            score += 2
            break

    # FVGs — same lookback constraint
    fvgs_bucket = mtf_pre.fvgs_bull if direction == "long" else mtf_pre.fvgs_bear
    fvgs_confirm = mtf_pre.fvgs_bull_confirm if direction == "long" else mtf_pre.fvgs_bear_confirm
    cut = bisect.bisect_right(fvgs_confirm, mtf_i)
    recent_fvgs = [fvg for fvg in fvgs_bucket[:cut] if fvg["idx"] >= min_idx_ob][-3:]
    for fvg in recent_fvgs:
        if fvg["low"] <= mtf_close <= fvg["high"]:
            score += 2
            break

    # LTF structure
    ltf_swings_now = _swings_up_to(ltf_pre, ltf_i)
    ltf_struct = structure_summary(ltf_swings_now, ltf_close)
    ltf_trend = ltf_struct["trend"]
    if (direction == "long" and ltf_trend == "Bullish") or (direction == "short" and ltf_trend == "Bearish"):
        score += 2

    # Volume
    vol = ltf_pre.vol_ratio_arr[ltf_i]
    if np.isfinite(vol) and vol > 1.5:
        score += 1

    # RSI
    rsi_v = ltf_pre.rsi_arr[ltf_i]
    if np.isfinite(rsi_v):
        if direction == "long" and 30 < rsi_v < 60:
            score += 1
        elif direction == "short" and 40 < rsi_v < 70:
            score += 1

    return {
        "direction": direction,
        "score": score,
        "reasons": [],
        "current_price": ltf_close,
        "ltf_atr": float(ltf_pre.atr_arr[ltf_i]),
        "ltf_swings": ltf_swings_now,
    }
