"""Discord webhook notifications for trade-cli.

Multiple named channels supported via .env:
  DISCORD_WEBHOOK_SIGNALS  -> active trade signals (entries/exits/alerts)
  DISCORD_WEBHOOK_SCANNER  -> setup-scan results (future)
  DISCORD_WEBHOOK_REPORTS  -> daily/weekly reports (future)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()


COLORS = {
    "green": 0x2ECC71,
    "red": 0xE74C3C,
    "yellow": 0xF1C40F,
    "blue": 0x3498DB,
    "purple": 0x9B59B6,
    "grey": 0x95A5A6,
}


def _webhook(channel: str) -> Optional[str]:
    return os.getenv(f"DISCORD_WEBHOOK_{channel.upper()}")


def send(channel: str, content: str = "", embed: Optional[dict] = None, fallback_to: Optional[str] = "signals") -> bool:
    """Post to a Discord channel; fall back to SIGNALS channel if requested channel isn't configured."""
    url = _webhook(channel) or (_webhook(fallback_to) if fallback_to and fallback_to != channel else None)
    if not url:
        return False
    payload: dict = {}
    if content:
        payload["content"] = content[:2000]
    if embed:
        payload["embeds"] = [embed]
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception:
        return False


def _embed(title: str, description: str = "", color: str = "blue", fields: Optional[list[dict]] = None) -> dict:
    return {
        "title": title[:256],
        "description": description[:2000],
        "color": COLORS.get(color, COLORS["blue"]),
        "fields": fields or [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "trade-cli"},
    }


def trade_opened(symbol: str, side: str, qty: float, entry: float, stop: float, target: float,
                 risk: float, reward: float, score: str = "", reason: str = "") -> None:
    rr = reward / risk if risk else 0
    fields = [
        {"name": "Entry", "value": f"${entry}", "inline": True},
        {"name": "Quantity", "value": f"{qty}", "inline": True},
        {"name": "Cost", "value": f"${qty * entry:.2f}", "inline": True},
        {"name": "Stop Loss", "value": f"${stop}", "inline": True},
        {"name": "Take Profit", "value": f"${target}", "inline": True},
        {"name": "R:R", "value": f"{rr:.2f}:1", "inline": True},
        {"name": "Risk", "value": f"${risk:.2f}", "inline": True},
        {"name": "Reward", "value": f"${reward:.2f}", "inline": True},
    ]
    if score:
        fields.append({"name": "Confluence", "value": score, "inline": True})
    embed = _embed(
        title=f"TRADE OPENED — {symbol} {side}",
        description=reason[:500] if reason else "",
        color="blue",
        fields=fields,
    )
    send("signals", embed=embed)


def trade_closed(symbol: str, outcome: str, exit_price: float, pnl: float, pnl_pct: float, lesson: str = "") -> None:
    color = "green" if outcome == "WIN" else "red" if outcome == "LOSS" else "yellow"
    fields = [
        {"name": "Outcome", "value": outcome, "inline": True},
        {"name": "Exit", "value": f"${exit_price}", "inline": True},
        {"name": "P&L", "value": f"${pnl:+.2f} ({pnl_pct:+.2f}%)", "inline": True},
    ]
    embed = _embed(
        title=f"TRADE CLOSED — {symbol}",
        description=lesson[:500] if lesson else "",
        color=color,
        fields=fields,
    )
    send("signals", embed=embed)


def price_alert(symbol: str, kind: str, price: float, level: float, distance_pct: float) -> None:
    color = "yellow" if kind == "near_stop" else "green" if kind == "near_target" else "blue"
    title_map = {
        "near_stop": f"{symbol} approaching STOP",
        "near_target": f"{symbol} approaching TARGET",
        "stop_hit": f"{symbol} STOP HIT",
        "target_hit": f"{symbol} TARGET HIT",
        "structure_change": f"{symbol} STRUCTURE CHANGED",
    }
    fields = [
        {"name": "Price", "value": f"${price}", "inline": True},
        {"name": "Level", "value": f"${level}", "inline": True},
        {"name": "Distance", "value": f"{distance_pct:+.2f}%", "inline": True},
    ]
    embed = _embed(title=title_map.get(kind, kind), color=color, fields=fields)
    send("signals", embed=embed)


def structure_change(symbol: str, old: str, new: str, tf: str = "1h") -> None:
    color = "green" if new == "Bullish" else "red" if new == "Bearish" else "yellow"
    embed = _embed(
        title=f"{symbol} {tf} structure: {old} -> {new}",
        color=color,
        fields=[
            {"name": "Timeframe", "value": tf, "inline": True},
            {"name": "Was", "value": old, "inline": True},
            {"name": "Now", "value": new, "inline": True},
        ],
    )
    send("signals", embed=embed)


def journal_post(trade_id: str, symbol: str, outcome: str, exit_price: float, pnl: float, pnl_pct: float,
                 setup: str, reasoning: str, lesson: str) -> None:
    """Post a full markdown post-mortem to the journal channel."""
    color = "green" if outcome == "WIN" else "red" if outcome == "LOSS" else "yellow"
    body = (
        f"**Setup:** {setup}\n\n"
        f"**Reasoning:**\n{reasoning[:600]}\n\n"
        f"**Lesson:**\n{lesson[:600]}"
    )
    fields = [
        {"name": "Outcome", "value": outcome, "inline": True},
        {"name": "Exit", "value": f"${exit_price}", "inline": True},
        {"name": "P&L", "value": f"${pnl:+.2f} ({pnl_pct:+.2f}%)", "inline": True},
    ]
    embed = _embed(
        title=f"Post-Mortem — {symbol} ({trade_id})",
        description=body,
        color=color,
        fields=fields,
    )
    send("journal", embed=embed, fallback_to="signals")


def daily_report(date_str: str, stats: dict, today_trades: list[dict]) -> None:
    """Post a daily P&L report."""
    pnl = stats.get("total_pnl_usdt", 0)
    color = "green" if pnl > 0 else "red" if pnl < 0 else "grey"
    fields = [
        {"name": "Trades Closed Today", "value": str(len(today_trades)), "inline": True},
        {"name": "Win Rate (all-time)", "value": f"{stats.get('win_rate_pct', 0)}%", "inline": True},
        {"name": "Total P&L (all-time)", "value": f"${pnl:+.2f}", "inline": True},
        {"name": "Wins", "value": f"{stats.get('wins', 0)}", "inline": True},
        {"name": "Losses", "value": f"{stats.get('losses', 0)}", "inline": True},
        {"name": "Breakeven", "value": f"{stats.get('breakeven', 0)}", "inline": True},
    ]
    description = ""
    if today_trades:
        lines = [
            f"- **{t['symbol']}** {t['outcome']} ${float(t.get('pnl_usdt') or 0):+.2f}"
            for t in today_trades[:10]
        ]
        description = "**Today's closes:**\n" + "\n".join(lines)
    embed = _embed(title=f"Daily Report — {date_str}", description=description, color=color, fields=fields)
    send("reports", embed=embed, fallback_to="signals")


def setup_alert(symbol: str, score: int, direction: str, current_price: float,
                suggested_entry: float, suggested_stop: float, suggested_target: float,
                rr: float, primary: bool, reasons: list[str], tf_aligned: bool = True) -> None:
    """Rich alert for an A+ setup found by the scanner. Includes the trade.py command for copy-paste."""
    color = "green" if direction == "long" else "red"
    badge = "⭐ PRIMARY" if primary else "FYI"
    align_badge = "✓ TF aligned" if tf_aligned else "⚠ TF mixed"
    risk_pct = abs(suggested_entry - suggested_stop) / suggested_entry * 100
    reward_pct = abs(suggested_target - suggested_entry) / suggested_entry * 100
    cmd = (f"`trade.py buy {symbol} --usd <SIZE> "
           f"--stop {suggested_stop:.6f} --target {suggested_target:.6f}`")
    fields = [
        {"name": "Score", "value": f"{score}/10 {badge}", "inline": True},
        {"name": "Direction", "value": direction.upper(), "inline": True},
        {"name": "Alignment", "value": align_badge, "inline": True},
        {"name": "Price", "value": f"${current_price:.6f}", "inline": True},
        {"name": "Entry", "value": f"${suggested_entry:.6f}", "inline": True},
        {"name": "Stop", "value": f"${suggested_stop:.6f} (-{risk_pct:.2f}%)", "inline": True},
        {"name": "Target", "value": f"${suggested_target:.6f} (+{reward_pct:.2f}%)", "inline": True},
        {"name": "R:R", "value": f"{rr:.2f}:1", "inline": True},
    ]
    desc = "**Confluences:**\n" + "\n".join(f"• {r}" for r in reasons[:6])
    desc += f"\n\n**Command (after your size decision):**\n{cmd}"
    embed = _embed(
        title=f"{'🟢' if direction == 'long' else '🔴'} {symbol} — A+ setup ({score}/10)",
        description=desc,
        color=color,
        fields=fields,
    )
    # Tradeable setups (primary + score 9+) go to scanner channel; others go too but tagged
    send("scanner", embed=embed, fallback_to="signals")


def system_alert(level: str, message: str, details: str = "") -> None:
    """System health alerts: errors, restarts, API outages."""
    color = "red" if level.upper() == "ERROR" else "yellow" if level.upper() == "WARN" else "blue"
    embed = _embed(title=f"[{level.upper()}] {message[:100]}", description=details[:1500], color=color)
    send("system", embed=embed, fallback_to="signals")


def heartbeat(symbol: str, price: float, entry: float, stop: float, target: float,
              minutes_open: int, structure: str) -> None:
    pnl_pct = (price - entry) / entry * 100
    color = "green" if pnl_pct > 0 else "red" if pnl_pct < 0 else "grey"
    embed = _embed(
        title=f"{symbol} heartbeat ({minutes_open}m open)",
        color=color,
        fields=[
            {"name": "Price", "value": f"${price}", "inline": True},
            {"name": "P&L", "value": f"{pnl_pct:+.2f}%", "inline": True},
            {"name": "1H Trend", "value": structure, "inline": True},
            {"name": "Stop", "value": f"${stop}", "inline": True},
            {"name": "Target", "value": f"${target}", "inline": True},
            {"name": "Entry", "value": f"${entry}", "inline": True},
        ],
    )
    send("signals", embed=embed)


def whale_event(symbol: str, headline: str, fields: list[dict], color: str = "purple") -> bool:
    """Post a whale-flow event (large trade, CVD divergence, funding flip, OI spike) to the WHALE channel."""
    embed = _embed(title=f"🐋 {headline} — {symbol}", color=color, fields=fields)
    return send("whale", embed=embed, fallback_to="signals")
