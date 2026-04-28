"""Pretty terminal output for trade-cli using rich + asciichart."""
import sys
from typing import Optional

import asciichartpy as ac
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

# Force UTF-8 / disable legacy Windows console for emoji & unicode safety
console = Console(legacy_windows=False, force_terminal=True)


def _color_pnl(value: float) -> str:
    if value > 0:
        return f"[bold green]+${value:.2f}[/]"
    if value < 0:
        return f"[bold red]${value:.2f}[/]"
    return f"[dim]${value:.2f}[/]"


def _color_pct(value: float) -> str:
    if value > 0:
        return f"[bold green]+{value:.2f}%[/]"
    if value < 0:
        return f"[bold red]{value:.2f}%[/]"
    return f"[dim]{value:.2f}%[/]"


def _trend_color(trend: str) -> str:
    colors = {"Bullish": "green", "Bearish": "red", "Ranging": "yellow", "Unknown": "dim"}
    return f"[{colors.get(trend, 'white')}]{trend}[/]"


# ──────────────────────────────────────────────────────────────────
# Status
# ──────────────────────────────────────────────────────────────────

def render_status(data: dict) -> None:
    usdt = data["usdt_free"]
    total = data["total_account_value_usdt"]
    invested = total - usdt

    header = Table.grid(padding=(0, 2))
    header.add_column(justify="left")
    header.add_column(justify="right")
    header.add_row(
        Text("ACCOUNT VALUE", style="bold cyan"),
        Text(f"${total:.2f}", style="bold white"),
    )
    header.add_row(
        Text("Free USDT", style="dim"),
        Text(f"${usdt:.2f}", style="white"),
    )
    header.add_row(
        Text("In Positions", style="dim"),
        Text(f"${invested:.2f}", style="white"),
    )
    console.print(Panel(header, title="Status", border_style="cyan"))

    if data["positions"]:
        t = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
        t.add_column("Asset")
        t.add_column("Qty", justify="right")
        t.add_column("Price", justify="right")
        t.add_column("Value", justify="right")
        t.add_column("Open Orders", justify="center")
        for p in data["positions"]:
            t.add_row(
                f"[bold]{p['asset']}[/]",
                f"{p['quantity']:.4f}",
                f"${p['price']:.6f}",
                f"${p['value_usdt']:.2f}",
                str(p["open_orders_for_symbol"]),
            )
        console.print(t)
    else:
        console.print("[dim]No open positions.[/]")


# ──────────────────────────────────────────────────────────────────
# Multi-TF
# ──────────────────────────────────────────────────────────────────

def render_multi_tf(data: dict) -> None:
    t = Table(title=f"Multi-Timeframe — {data['symbol']}", box=box.ROUNDED, header_style="bold cyan")
    t.add_column("TF", style="bold")
    t.add_column("Trend")
    t.add_column("RSI", justify="right")
    t.add_column("ADX", justify="right")
    t.add_column("MACD", justify="center")
    t.add_column("Pattern")
    t.add_column("Sweep?", justify="center")

    for tf in ("1d", "4h", "1h", "15m"):
        row = data[tf]
        rsi = row["rsi"]
        rsi_s = f"[red]{rsi:.0f}[/]" if rsi > 70 else f"[green]{rsi:.0f}[/]" if rsi < 30 else f"{rsi:.0f}"
        adx = row["adx"]
        adx_s = f"[bold]{adx:.0f}[/]" if adx > 25 else f"[dim]{adx:.0f}[/]"
        sweep = "[yellow]YES[/]" if row["recent_sweep"] else "[dim]—[/]"
        pattern = " ".join(row.get("swing_pattern") or [])
        t.add_row(tf.upper(), _trend_color(row["trend"]), rsi_s, adx_s, row["macd_cross"], pattern, sweep)

    console.print(t)

    # Bias call
    trends = [data[tf]["trend"] for tf in ("1d", "4h", "1h", "15m")]
    if trends.count("Bullish") >= 3:
        bias = "[bold green]BULLISH bias — look for longs only[/]"
    elif trends.count("Bearish") >= 3:
        bias = "[bold red]BEARISH bias — look for shorts only[/]"
    else:
        bias = "[bold yellow]MIXED — wait for alignment[/]"
    console.print(Panel(bias, border_style="cyan"))


# ──────────────────────────────────────────────────────────────────
# Confluence
# ──────────────────────────────────────────────────────────────────

def render_confluence(data: dict) -> None:
    score = data["score"]
    verdict = data["verdict"]
    verdict_color = "green" if verdict == "A+ SETUP" else "yellow" if verdict == "Decent" else "red"

    body = Table.grid(padding=(0, 2))
    body.add_column()
    body.add_column()
    body.add_row("[bold]Symbol[/]", data["symbol"])
    body.add_row("[bold]Direction[/]", str(data["direction"]).upper() if data["direction"] else "—")
    body.add_row("[bold]Score[/]", f"[bold {verdict_color}]{score}/10[/]")
    body.add_row("[bold]Verdict[/]", f"[bold {verdict_color}]{verdict}[/]")
    body.add_row("[bold]HTF (4H)[/]", _trend_color(data["htf_trend"]))
    body.add_row("[bold]MTF (1H)[/]", _trend_color(data["mtf_trend"]))
    body.add_row("[bold]LTF (15m)[/]", _trend_color(data["ltf_trend"]))
    body.add_row("[bold]Price[/]", f"${data['current_price']}")

    console.print(Panel(body, title="Confluence Score", border_style=verdict_color))

    if data["reasons"]:
        for r in data["reasons"]:
            console.print(f"  [green]✓[/] {r}")
    if score < 8:
        console.print(f"\n[bold yellow]!Score below A+ threshold (8). Skip this setup.[/]")
    else:
        console.print(f"\n[bold green]✓ A+ setup confirmed. Run `size` next.[/]")


# ──────────────────────────────────────────────────────────────────
# Setup scan
# ──────────────────────────────────────────────────────────────────

def render_setup_scan(data: dict) -> None:
    t = Table(
        title=f"Setup Scan — {data['scanned']} scanned, {data['found']} A+ found",
        box=box.ROUNDED, header_style="bold cyan",
    )
    t.add_column("Symbol", style="bold")
    t.add_column("Score", justify="center")
    t.add_column("Verdict")
    t.add_column("Direction")
    t.add_column("HTF")
    t.add_column("MTF")
    t.add_column("LTF")
    t.add_column("Price", justify="right")

    for s in data["setups"]:
        if "error" in s:
            continue
        score = s.get("score", 0)
        verdict = s.get("verdict", "?")
        score_s = f"[bold green]{score}/10[/]" if score >= 8 else f"[yellow]{score}/10[/]"
        t.add_row(
            s["symbol"], score_s, verdict,
            (s.get("direction") or "—").upper(),
            _trend_color(s.get("htf_trend", "")),
            _trend_color(s.get("mtf_trend", "")),
            _trend_color(s.get("ltf_trend", "")),
            f"${s.get('current_price', 0)}",
        )
    if data["found"] == 0:
        console.print(Panel("[bold yellow]No A+ setups right now. Sit on hands. [/]", border_style="yellow"))
    else:
        console.print(t)


# ──────────────────────────────────────────────────────────────────
# Structure (ASCII chart)
# ──────────────────────────────────────────────────────────────────

def render_structure(data: dict, df_close: Optional[list[float]] = None) -> None:
    s = data["summary"]
    body = Table.grid(padding=(0, 2))
    body.add_column()
    body.add_column()
    body.add_row("[bold]Trend[/]", _trend_color(s["trend"]))
    body.add_row("[bold]Current Price[/]", f"${s.get('current_price', '?')}")
    body.add_row("[bold]Last Swing[/]", f"{s['last_swing']['kind']} @ ${s['last_swing']['price']}")
    body.add_row("[bold]Prev High[/]", f"${s.get('prev_high')}")
    body.add_row("[bold]Prev Low[/]", f"${s.get('prev_low')}")
    body.add_row("[bold]Swing Pattern[/]", " → ".join(s.get("swing_pattern") or []))

    console.print(Panel(body, title=f"Structure — {data['symbol']} {data['interval']}", border_style="cyan"))

    if s.get("events"):
        for e in s["events"]:
            color = "green" if "bullish" in e else "red"
            console.print(f"  [bold {color}]!{e}[/]")

    # Recent swings table
    t = Table(title="Recent Swings", box=box.SIMPLE, header_style="bold")
    t.add_column("Time", style="dim")
    t.add_column("Kind")
    t.add_column("Price", justify="right")
    for sw in data.get("recent_swings", []):
        kind_color = "green" if sw["kind"] in ("HH", "HL") else "red" if sw["kind"] in ("LL", "LH") else "white"
        t.add_row(sw["time"][:16], f"[{kind_color}]{sw['kind']}[/]", f"${sw['price']}")
    console.print(t)

    # ASCII chart of recent prices if provided
    if df_close and len(df_close) > 5:
        chart = ac.plot(df_close[-60:], {"height": 10, "format": "{:>10.6f}"})
        console.print(Panel(chart, title="Price (last 60 candles)", border_style="dim"))


# ──────────────────────────────────────────────────────────────────
# Analyze (the comprehensive view)
# ──────────────────────────────────────────────────────────────────

def render_analyze(data: dict) -> None:
    ind = data["indicators"]
    struct = data["structure"]

    # Header
    header = Table.grid(padding=(0, 2))
    header.add_column()
    header.add_column()
    header.add_row("[bold]Symbol[/]", f"{data['symbol']} ({data['interval']})")
    header.add_row("[bold]Price[/]", f"${data['current_price']}")
    header.add_row("[bold]Trend[/]", _trend_color(struct["trend"]))
    header.add_row("[bold]Pattern[/]", " → ".join(struct.get("swing_pattern") or []))
    console.print(Panel(header, title="Analysis", border_style="cyan"))

    # Indicators table
    t = Table(box=box.ROUNDED, header_style="bold magenta")
    t.add_column("Indicator")
    t.add_column("Value", justify="right")
    t.add_column("Signal")

    rsi = ind["rsi"]
    rsi_color = "red" if rsi > 70 else "green" if rsi < 30 else "white"
    t.add_row("RSI(14)", f"[{rsi_color}]{rsi}[/]", ind["rsi_signal"])

    adx = ind["adx"]
    adx_color = "bold" if adx > 25 else "dim"
    t.add_row("ADX(14)", f"[{adx_color}]{adx}[/]", f"{ind['adx_strength']} / DI: {ind['trend_di']}")

    macd_color = "green" if ind["macd_cross"] == "Bullish" else "red" if ind["macd_cross"] == "Bearish" else "dim"
    t.add_row("MACD", f"hist {ind['macd']['hist']:.6f}", f"[{macd_color}]{ind['macd_cross']}[/]")

    t.add_row("EMA20", f"${ind['ema20']:.6f}", "")
    t.add_row("EMA50", f"${ind['ema50']:.6f}", "")
    if ind.get("ema200"):
        t.add_row("EMA200", f"${ind['ema200']:.6f}", "")

    t.add_row("ATR", f"{ind['atr']:.6f} ({ind['atr_pct']}%)", "")

    vol_ratio = ind.get("volume_ratio_vs_20avg")
    vol_signal = ""
    if vol_ratio:
        vol_signal = "[bold yellow]SPIKE[/]" if vol_ratio > 1.5 else "[dim]Normal[/]"
    t.add_row("Volume vs 20avg", f"{vol_ratio}x" if vol_ratio else "—", vol_signal)

    console.print(t)

    # Liquidity
    liq = data["liquidity"]
    if liq["equal_highs"] or liq["equal_lows"]:
        lt = Table(title="Liquidity Zones", box=box.SIMPLE)
        lt.add_column("Type")
        lt.add_column("Price", justify="right")
        lt.add_column("Touches", justify="center")
        for h in liq["equal_highs"]:
            lt.add_row("[red]Equal Highs[/]", f"${h['price']:.6f}", str(h["count"]))
        for l in liq["equal_lows"]:
            lt.add_row("[green]Equal Lows[/]", f"${l['price']:.6f}", str(l["count"]))
        console.print(lt)

    if data.get("recent_sweep"):
        sw = data["recent_sweep"]
        color = "green" if "bullish" in sw["type"] else "red"
        console.print(Panel(
            f"[bold {color}]!{sw['type'].upper()} detected[/] — level ${sw['level']}",
            border_style=color,
        ))

    # Order blocks
    obs = data["order_blocks"]
    price = data["current_price"]
    in_ob = []
    for ob in obs.get("bullish", []):
        if ob["low"] <= price <= ob["high"]:
            in_ob.append(("Bullish OB", ob, "green"))
    for ob in obs.get("bearish", []):
        if ob["low"] <= price <= ob["high"]:
            in_ob.append(("Bearish OB", ob, "red"))
    if in_ob:
        for label, ob, color in in_ob:
            console.print(Panel(
                f"[bold {color}]Price IN {label}[/] — zone ${ob['low']:.6f} → ${ob['high']:.6f}",
                border_style=color,
            ))


# ──────────────────────────────────────────────────────────────────
# Journal stats
# ──────────────────────────────────────────────────────────────────

def render_journal_stats(data: dict) -> None:
    if data.get("closed", 0) == 0:
        console.print(Panel("[dim]No closed trades yet.[/]", title="Journal Stats"))
        return

    body = Table.grid(padding=(0, 2))
    body.add_column()
    body.add_column()
    body.add_row("[bold]Total Trades[/]", str(data.get("total_trades")))
    body.add_row("[bold]Open[/]", str(data.get("open")))
    body.add_row("[bold]Closed[/]", str(data.get("closed")))
    body.add_row("[bold]Wins[/]", f"[green]{data.get('wins')}[/]")
    body.add_row("[bold]Losses[/]", f"[red]{data.get('losses')}[/]")
    body.add_row("[bold]Breakeven[/]", f"[yellow]{data.get('breakeven', 0)}[/]")
    body.add_row("[bold]Win Rate[/]", f"{data.get('win_rate_pct')}%")
    body.add_row("[bold]Total P&L[/]", _color_pnl(data.get("total_pnl_usdt", 0)))
    body.add_row("[bold]Avg / Trade[/]", _color_pnl(data.get("avg_pnl_per_trade", 0)))

    console.print(Panel(body, title="Journal Stats", border_style="cyan"))


# ──────────────────────────────────────────────────────────────────
# Journal list
# ──────────────────────────────────────────────────────────────────

def render_journal_list(rows: list[dict]) -> None:
    if not rows:
        console.print("[dim]No trades logged.[/]")
        return
    t = Table(title="Recent Trades", box=box.ROUNDED, header_style="bold cyan")
    t.add_column("ID", style="dim")
    t.add_column("Symbol")
    t.add_column("Side")
    t.add_column("Entry", justify="right")
    t.add_column("Exit", justify="right")
    t.add_column("Outcome")
    t.add_column("P&L", justify="right")
    t.add_column("Setup")
    for r in rows:
        outcome = r.get("outcome", "")
        outcome_color = "green" if outcome == "WIN" else "red" if outcome == "LOSS" else "yellow" if outcome == "BE" else "dim"
        pnl = float(r.get("pnl_usdt") or 0)
        t.add_row(
            r.get("trade_id", ""),
            r.get("symbol", ""),
            r.get("side", ""),
            r.get("entry_price", ""),
            r.get("exit_price", "—"),
            f"[{outcome_color}]{outcome}[/]",
            _color_pnl(pnl) if pnl else "—",
            (r.get("setup_type") or "")[:30],
        )
    console.print(t)
