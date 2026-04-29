"""Whale-flow signals from free Binance public endpoints.

Pulls funding rate, open interest, spot CVD, and large-trade activity.
All endpoints are unauthenticated public — no API key needed.

Functions degrade gracefully: if a symbol has no perp listing, futures
calls return None and the caller decides how to handle.
"""
from __future__ import annotations

from typing import Optional
from datetime import datetime, timedelta, timezone

from binance.client import Client
from binance.exceptions import BinanceAPIException


# ── Funding rate ────────────────────────────────────────────────────

def get_funding(client: Client, symbol: str) -> Optional[dict]:
    """Current funding rate + 24h history. None if no perp."""
    try:
        mark = client.futures_mark_price(symbol=symbol)
        current = float(mark["lastFundingRate"])
        next_time = int(mark["nextFundingTime"])

        hist = client.futures_funding_rate(symbol=symbol, limit=8)  # ~last 64h
        avg_24h = sum(float(h["fundingRate"]) for h in hist[-3:]) / max(1, len(hist[-3:]))

        return {
            "current_rate": current,
            "current_pct": round(current * 100, 4),
            "avg_24h_pct": round(avg_24h * 100, 4),
            "next_funding_time": datetime.fromtimestamp(next_time / 1000, tz=timezone.utc).isoformat(),
            "interpretation": _funding_signal(current, avg_24h),
        }
    except BinanceAPIException:
        return None


def _funding_signal(current: float, avg_24h: float) -> str:
    if current < -0.0005:
        return "deeply_negative_retail_short"  # contrarian long signal
    if current < -0.0001:
        return "negative_retail_lean_short"
    if current > 0.0005:
        return "deeply_positive_retail_long"  # contrarian short signal
    if current > 0.0001:
        return "positive_retail_lean_long"
    return "neutral"


# ── Open Interest ────────────────────────────────────────────────────

def get_open_interest(client: Client, symbol: str) -> Optional[dict]:
    """Current OI + 24h delta. None if no perp."""
    try:
        current = client.futures_open_interest(symbol=symbol)
        oi_now = float(current["openInterest"])

        hist = client.futures_open_interest_hist(symbol=symbol, period="1h", limit=24)
        if not hist:
            return {"current": oi_now, "delta_24h_pct": None, "interpretation": "no_history"}

        oi_24h_ago = float(hist[0]["sumOpenInterest"])
        delta_pct = ((oi_now - oi_24h_ago) / oi_24h_ago) * 100 if oi_24h_ago else 0.0

        return {
            "current": oi_now,
            "oi_24h_ago": oi_24h_ago,
            "delta_24h_pct": round(delta_pct, 2),
            "interpretation": _oi_signal(delta_pct),
        }
    except BinanceAPIException:
        return None


def _oi_signal(delta_pct: float) -> str:
    if delta_pct > 10:
        return "rising_strongly_new_positions_opening"
    if delta_pct > 3:
        return "rising"
    if delta_pct < -10:
        return "dropping_strongly_positions_closing"
    if delta_pct < -3:
        return "dropping"
    return "flat"


# ── Spot CVD (Cumulative Volume Delta) ───────────────────────────────

def get_spot_cvd(client: Client, symbol: str, lookback_minutes: int = 240) -> dict:
    """Compute spot CVD from agg trades over lookback window.

    CVD = sum(buy_volume) - sum(sell_volume)
    Positive CVD = net spot accumulation (buyers aggressing).
    Returns total CVD, buy/sell breakdown, and interpretation.
    """
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(minutes=lookback_minutes)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    trades = []
    cursor_ms = start_ms
    while cursor_ms < end_ms:
        batch = client.get_aggregate_trades(
            symbol=symbol, startTime=cursor_ms, endTime=min(cursor_ms + 60 * 60 * 1000, end_ms), limit=1000
        )
        if not batch:
            cursor_ms += 60 * 60 * 1000
            continue
        trades.extend(batch)
        if len(batch) < 1000:
            cursor_ms += 60 * 60 * 1000
        else:
            cursor_ms = batch[-1]["T"] + 1
        if len(trades) > 20000:
            break

    if not trades:
        return {"cvd": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "trade_count": 0, "interpretation": "no_data"}

    buy_vol = 0.0
    sell_vol = 0.0
    for t in trades:
        qty = float(t["q"])
        price = float(t["p"])
        notional = qty * price
        if t["m"]:  # buyer was maker → taker was seller → market sell
            sell_vol += notional
        else:
            buy_vol += notional

    cvd = buy_vol - sell_vol
    total = buy_vol + sell_vol
    cvd_pct = (cvd / total * 100) if total else 0.0

    return {
        "lookback_minutes": lookback_minutes,
        "trade_count": len(trades),
        "buy_vol_usdt": round(buy_vol, 2),
        "sell_vol_usdt": round(sell_vol, 2),
        "cvd_usdt": round(cvd, 2),
        "cvd_pct_of_total": round(cvd_pct, 2),
        "interpretation": _cvd_signal(cvd_pct),
    }


def _cvd_signal(cvd_pct: float) -> str:
    if cvd_pct > 15:
        return "strong_accumulation"
    if cvd_pct > 5:
        return "net_accumulation"
    if cvd_pct < -15:
        return "strong_distribution"
    if cvd_pct < -5:
        return "net_distribution"
    return "balanced"


# ── Large trades ─────────────────────────────────────────────────────

def get_large_trades(client: Client, symbol: str, threshold_usdt: float = 50_000,
                     lookback_minutes: int = 60) -> dict:
    """Recent spot trades above a USDT-notional threshold."""
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(minutes=lookback_minutes)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    trades = []
    cursor_ms = start_ms
    while cursor_ms < end_ms:
        batch = client.get_aggregate_trades(
            symbol=symbol, startTime=cursor_ms, endTime=min(cursor_ms + 60 * 60 * 1000, end_ms), limit=1000
        )
        if not batch:
            cursor_ms += 60 * 60 * 1000
            continue
        trades.extend(batch)
        if len(batch) < 1000:
            cursor_ms += 60 * 60 * 1000
        else:
            cursor_ms = batch[-1]["T"] + 1
        if len(trades) > 20000:
            break

    large = []
    buy_count = 0
    sell_count = 0
    buy_notional = 0.0
    sell_notional = 0.0
    for t in trades:
        qty = float(t["q"])
        price = float(t["p"])
        notional = qty * price
        if notional >= threshold_usdt:
            side = "sell" if t["m"] else "buy"
            large.append({
                "time": datetime.fromtimestamp(t["T"] / 1000, tz=timezone.utc).isoformat(),
                "side": side,
                "price": price,
                "qty": qty,
                "notional_usdt": round(notional, 2),
            })
            if side == "buy":
                buy_count += 1
                buy_notional += notional
            else:
                sell_count += 1
                sell_notional += notional

    return {
        "threshold_usdt": threshold_usdt,
        "lookback_minutes": lookback_minutes,
        "total_large_trades": len(large),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_notional_usdt": round(buy_notional, 2),
        "sell_notional_usdt": round(sell_notional, 2),
        "net_notional_usdt": round(buy_notional - sell_notional, 2),
        "samples": large[-10:],  # last 10 only
    }


# ── Aggregate ────────────────────────────────────────────────────────

def whale_flow_summary(client: Client, symbol: str) -> dict:
    """Pull funding + OI + CVD + large trades in one call."""
    return {
        "symbol": symbol.upper(),
        "funding": get_funding(client, symbol),
        "open_interest": get_open_interest(client, symbol),
        "spot_cvd_4h": get_spot_cvd(client, symbol, lookback_minutes=240),
        "large_trades_1h": get_large_trades(client, symbol, threshold_usdt=50_000, lookback_minutes=60),
    }


# ── Confluence bonus scoring ─────────────────────────────────────────

def whale_bonus_stars(flow: dict, direction: str) -> tuple[int, list[str]]:
    """Translate whale-flow into bonus confluence stars + reasons.

    direction: 'long' or 'short' (or None — returns empty).
    Caps total bonus at +4 stars to avoid drowning the base score.
    """
    if not direction:
        return 0, []

    bonus = 0
    reasons: list[str] = []

    f = flow.get("funding")
    if f:
        sig = f["interpretation"]
        if direction == "long" and sig in ("deeply_negative_retail_short", "negative_retail_lean_short"):
            stars = 2 if "deeply" in sig else 1
            bonus += stars
            reasons.append(f"{'⭐' * stars} Funding {f['current_pct']}% → retail short, whales fading")
        elif direction == "short" and sig in ("deeply_positive_retail_long", "positive_retail_lean_long"):
            stars = 2 if "deeply" in sig else 1
            bonus += stars
            reasons.append(f"{'⭐' * stars} Funding {f['current_pct']}% → retail long, whales fading")

    oi = flow.get("open_interest")
    if oi and oi.get("delta_24h_pct") is not None:
        sig = oi["interpretation"]
        if direction == "long" and sig in ("dropping", "dropping_strongly_positions_closing"):
            bonus += 1
            reasons.append(f"⭐ OI {oi['delta_24h_pct']}% / 24h → shorts capitulating")
        elif direction == "short" and sig in ("rising", "rising_strongly_new_positions_opening"):
            bonus += 1
            reasons.append(f"⭐ OI +{oi['delta_24h_pct']}% / 24h → longs piling in (squeeze fuel)")

    cvd = flow.get("spot_cvd_4h")
    if cvd:
        sig = cvd["interpretation"]
        if direction == "long" and sig in ("net_accumulation", "strong_accumulation"):
            stars = 2 if "strong" in sig else 1
            bonus += stars
            reasons.append(f"{'⭐' * stars} Spot CVD +{cvd['cvd_pct_of_total']}% / 4h → accumulation")
        elif direction == "short" and sig in ("net_distribution", "strong_distribution"):
            stars = 2 if "strong" in sig else 1
            bonus += stars
            reasons.append(f"{'⭐' * stars} Spot CVD {cvd['cvd_pct_of_total']}% / 4h → distribution")

    large = flow.get("large_trades_1h")
    if large and large["total_large_trades"] > 0:
        net = large["net_notional_usdt"]
        if direction == "long" and net > 100_000:
            bonus += 1
            reasons.append(f"⭐ Large-trade net buy +${net:,.0f} / 1h")
        elif direction == "short" and net < -100_000:
            bonus += 1
            reasons.append(f"⭐ Large-trade net sell ${net:,.0f} / 1h")

    return min(bonus, 4), reasons
