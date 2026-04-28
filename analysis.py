"""SMC + indicator analysis engine for trade-cli."""
from __future__ import annotations

from dataclasses import dataclass, asdict
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
    iv = INTERVAL_MAP.get(interval.lower())
    if iv is None:
        raise ValueError(f"Bad interval: {interval}. Use {list(INTERVAL_MAP)}")
    raw = client.get_klines(symbol=symbol.upper(), interval=iv, limit=limit)
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "_",
        ],
    )
    for c in ["open", "high", "low", "close", "volume", "quote_volume", "taker_base", "taker_quote"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    return df.set_index("open_time")


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
    }
