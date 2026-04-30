"""Daily-loss circuit breaker + volatility-aware sizing.

Breaker rules (defaults, overridable in .env):
  - Max 2 losing trades per UTC day
  - Max 4% account drawdown per UTC day
  - Auto-resets at UTC midnight

Volatility sizing: 1h ATR/price ratio determines a sizing multiplier on the base
risk %. Normal vol = 1×; choppy/blow-off vol = 0.5×. Never scales up (avoid
over-confidence in low-vol grinds that can break violently).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import journal as jrnl

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"

MAX_DAILY_LOSSES = int(os.getenv("RISK_MAX_DAILY_LOSSES", 2))
MAX_DAILY_DD_PCT = float(os.getenv("RISK_MAX_DAILY_DD_PCT", 4.0))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _todays_closes() -> list[dict]:
    rows = jrnl.list_trades(10000)
    today = _today()
    out = []
    for r in rows:
        if r["outcome"] not in ("WIN", "LOSS", "BE"):
            continue
        if r["trade_id"].startswith(f"T{today}"):
            out.append(r)
    return out


def _todays_pnl() -> float:
    return sum(float(r.get("pnl_usdt") or 0) for r in _todays_closes())


def check_trading_allowed(account_value: float) -> dict:
    """Return {'allowed': bool, 'reason': str, ...}.

    account_value: current total account size in USDT (used for DD %).
    """
    closes = _todays_closes()
    losses = [r for r in closes if r["outcome"] == "LOSS"]
    pnl = sum(float(r.get("pnl_usdt") or 0) for r in closes)
    dd_pct = (-pnl / account_value * 100) if pnl < 0 and account_value > 0 else 0.0

    info = {
        "today": _today(),
        "trades_closed_today": len(closes),
        "losses_today": len(losses),
        "pnl_today_usdt": round(pnl, 2),
        "drawdown_today_pct": round(dd_pct, 2),
        "max_losses": MAX_DAILY_LOSSES,
        "max_dd_pct": MAX_DAILY_DD_PCT,
    }

    if len(losses) >= MAX_DAILY_LOSSES:
        return {**info, "allowed": False,
                "reason": f"Hit daily loss limit ({len(losses)}/{MAX_DAILY_LOSSES}). Wait for UTC midnight."}
    if dd_pct >= MAX_DAILY_DD_PCT:
        return {**info, "allowed": False,
                "reason": f"Hit daily drawdown limit ({dd_pct:.2f}% >= {MAX_DAILY_DD_PCT}%). Wait for UTC midnight."}
    return {**info, "allowed": True, "reason": "ok"}


# ── Volatility-aware sizing ─────────────────────────────────────────
# ATR%/price baseline for crypto majors over 1h ≈ 1-2%. >4% means the symbol is
# in a blow-off / news-driven regime where 1.5R targets get whipsawed before they
# resolve. Halve risk in those conditions; refuse to scale up in low-vol regimes
# (false confidence — vol mean-reverts violently).
ATR_HIGH_VOL_PCT = float(os.getenv("ATR_HIGH_VOL_PCT", 4.0))
ATR_EXTREME_VOL_PCT = float(os.getenv("ATR_EXTREME_VOL_PCT", 7.0))


def vol_sizing_multiplier(client, symbol: str) -> tuple[float, dict]:
    """Returns (multiplier, info_dict). Multiplier ∈ {1.0, 0.5, 0.25}.
    1.0 = normal, 0.5 = high vol, 0.25 = extreme (consider skipping entirely).
    Fails open (1.0) on any data error — don't block trading on indicator failure.
    """
    try:
        import analysis
        df = analysis.fetch_klines(client, symbol, "1h", 100)
        a = analysis.atr(df, period=14)
        atr_now = float(a.iloc[-1])
        price = float(df["close"].iloc[-1])
        if price <= 0:
            return 1.0, {"error": "bad_price"}
        atr_pct = (atr_now / price) * 100
        info = {"atr_pct_of_price": round(atr_pct, 3), "regime": "normal"}
        if atr_pct >= ATR_EXTREME_VOL_PCT:
            info["regime"] = "extreme"
            return 0.25, info
        if atr_pct >= ATR_HIGH_VOL_PCT:
            info["regime"] = "high"
            return 0.5, info
        return 1.0, info
    except Exception as e:
        return 1.0, {"error": str(e)[:120]}


# ── Portfolio correlation gate ──────────────────────────────────────
# All alts move together with BTC at high R² intraday. Three concurrent alt
# longs with "1% risk each" is closer to 2-2.5% effective beta when BTC moves.
# Cap concurrent open-long notional as % of account value.
CONCURRENT_NOTIONAL_CAP_PCT = float(os.getenv("CONCURRENT_NOTIONAL_CAP_PCT", 50.0))


def concurrent_exposure_check(client, account_value: float, prospective_notional: float) -> tuple[bool, dict]:
    """Are we under the concurrent-exposure cap if we add this trade?
    Returns (allowed, info)."""
    try:
        acc = client.get_account()
        balances = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in acc["balances"]}
        # Sum USDT-denominated value of all non-USDT, non-zero balances
        held_value = 0.0
        for asset, qty in balances.items():
            if asset in ("USDT", "BUSD", "USDC", "FDUSD") or qty <= 0:
                continue
            try:
                px = float(client.get_symbol_ticker(symbol=f"{asset}USDT")["price"])
                held_value += qty * px
            except Exception:
                continue
        total_after = held_value + prospective_notional
        cap = account_value * CONCURRENT_NOTIONAL_CAP_PCT / 100.0
        info = {
            "current_held_usdt": round(held_value, 2),
            "prospective_notional": round(prospective_notional, 2),
            "total_after": round(total_after, 2),
            "cap_usdt": round(cap, 2),
            "cap_pct": CONCURRENT_NOTIONAL_CAP_PCT,
            "account_value": round(account_value, 2),
        }
        return total_after <= cap, info
    except Exception as e:
        # Fail-open on data error — don't block trading on lookup failures
        return True, {"error": str(e)[:120]}
