"""Daily-loss circuit breaker — refuses new trades after the day's risk budget is spent.

Rules (defaults, overridable in .env):
  - Max 2 losing trades per UTC day
  - Max 4% account drawdown per UTC day
  - Auto-resets at UTC midnight

State is persisted in state.json under "risk_breaker".
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

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
