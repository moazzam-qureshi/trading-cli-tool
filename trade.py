"""trade-cli: Binance Spot CLI with SMC analysis + OCO protection."""
import json
import os
import sys
from decimal import Decimal, ROUND_DOWN

# Force UTF-8 on Windows so rich/unicode output works without env vars
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import click
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

import analysis
import journal as jrnl
import display
import notify

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"


def get_client() -> Client:
    if not API_KEY or not API_SECRET:
        click.echo(json.dumps({"error": "Missing API keys in .env"}))
        sys.exit(1)
    return Client(API_KEY, API_SECRET, testnet=TESTNET)


def out(data) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def get_filters(client: Client, symbol: str) -> dict:
    info = client.get_symbol_info(symbol)
    if not info:
        raise click.ClickException(f"Unknown symbol: {symbol}")
    filters = {f["filterType"]: f for f in info["filters"]}
    return filters


def round_step(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    return (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def place_oco_sell(client: Client, symbol: str, quantity: str, target: str, stop: str, stop_limit: str) -> dict:
    """Place a SELL OCO using Binance's new orderList/oco endpoint (aboveType/belowType format)."""
    params = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": quantity,
        "aboveType": "LIMIT_MAKER",
        "abovePrice": target,
        "belowType": "STOP_LOSS_LIMIT",
        "belowStopPrice": stop,
        "belowPrice": stop_limit,
        "belowTimeInForce": "GTC",
    }
    return client._post("orderList/oco", True, data=params)


def fmt(value: Decimal, step: Decimal) -> str:
    decimals = max(0, -step.as_tuple().exponent)
    return f"{value:.{decimals}f}"


@click.group()
def cli() -> None:
    """trade-cli — Binance Spot trading helper."""


@cli.command()
def env() -> None:
    """Show which environment is active."""
    out({"testnet": TESTNET, "api_key_set": bool(API_KEY)})


@cli.command()
def balance() -> None:
    """Show non-zero balances."""
    client = get_client()
    acc = client.get_account()
    bals = [b for b in acc["balances"] if float(b["free"]) + float(b["locked"]) > 0]
    out({
        "testnet": TESTNET,
        "canTrade": acc["canTrade"],
        "balances": bals,
    })


@cli.command()
@click.argument("symbol")
def price(symbol: str) -> None:
    """Current ticker price."""
    client = get_client()
    p = client.get_symbol_ticker(symbol=symbol.upper())
    out(p)


@cli.command()
@click.argument("symbol")
@click.option("--usd", type=float, help="USDT amount to spend")
@click.option("--quantity", type=float, help="Exact base-asset quantity")
@click.option("--entry", type=float, default=None, help="LIMIT entry price (omit = MARKET)")
@click.option("--stop", type=float, required=True, help="Stop-loss trigger price")
@click.option("--target", type=float, required=True, help="Take-profit price")
@click.option("--slip", type=float, default=0.5, help="Stop-limit slippage % below stop (default 0.5)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
def buy(symbol, usd, quantity, entry, stop, target, slip, yes):
    """Buy + auto-attach OCO (stop + take-profit)."""
    client = get_client()
    symbol = symbol.upper()
    filters = get_filters(client, symbol)

    lot = Decimal(filters["LOT_SIZE"]["stepSize"])
    tick = Decimal(filters["PRICE_FILTER"]["tickSize"])
    min_notional = Decimal(filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {"minNotional": "5"}))["minNotional"])

    current = Decimal(client.get_symbol_ticker(symbol=symbol)["price"])

    if quantity is None:
        if usd is None:
            raise click.ClickException("Provide --usd or --quantity")
        ref_price = Decimal(str(entry)) if entry else current
        qty = Decimal(str(usd)) / ref_price
    else:
        qty = Decimal(str(quantity))

    qty = round_step(qty, lot)
    if qty * current < min_notional:
        raise click.ClickException(f"Order too small. Min notional ${min_notional}")

    stop_d = round_step(Decimal(str(stop)), tick)
    target_d = round_step(Decimal(str(target)), tick)
    stop_limit_d = round_step(stop_d * (Decimal("1") - Decimal(str(slip)) / Decimal("100")), tick)

    plan = {
        "symbol": symbol,
        "side": "BUY",
        "type": "LIMIT" if entry else "MARKET",
        "entry": fmt(Decimal(str(entry)), tick) if entry else f"~{current}",
        "quantity": fmt(qty, lot),
        "estimated_cost_usdt": fmt(qty * (Decimal(str(entry)) if entry else current), tick),
        "stop_loss": fmt(stop_d, tick),
        "stop_limit": fmt(stop_limit_d, tick),
        "take_profit": fmt(target_d, tick),
        "risk_usdt": fmt(qty * (current - stop_d), tick),
        "reward_usdt": fmt(qty * (target_d - current), tick),
        "testnet": TESTNET,
    }

    if not yes:
        click.echo(json.dumps(plan, indent=2))
        if not click.confirm("Place this trade?"):
            click.echo(json.dumps({"cancelled": True}))
            return

    if entry:
        entry_d = round_step(Decimal(str(entry)), tick)
        buy_order = client.order_limit_buy(
            symbol=symbol,
            quantity=fmt(qty, lot),
            price=fmt(entry_d, tick),
        )
    else:
        buy_order = client.order_market_buy(
            symbol=symbol,
            quantity=fmt(qty, lot),
        )

    result = {"buy": buy_order}

    if buy_order["status"] == "FILLED":
        try:
            base_asset = symbol.replace("USDT", "").replace("BUSD", "")
            free = Decimal(client.get_asset_balance(asset=base_asset)["free"])
            executed_qty = round_step(free, lot)
            oco = place_oco_sell(
                client,
                symbol=symbol,
                quantity=fmt(executed_qty, lot),
                target=fmt(target_d, tick),
                stop=fmt(stop_d, tick),
                stop_limit=fmt(stop_limit_d, tick),
            )
            result["oco"] = oco
            # Discord notification
            try:
                avg_fill = float(Decimal(buy_order["cummulativeQuoteQty"]) / Decimal(buy_order["executedQty"]))
                notify.trade_opened(
                    symbol=symbol, side="LONG", qty=float(executed_qty),
                    entry=avg_fill, stop=float(stop_d), target=float(target_d),
                    risk=float(executed_qty) * (avg_fill - float(stop_d)),
                    reward=float(executed_qty) * (float(target_d) - avg_fill),
                )
            except Exception:
                pass
        except BinanceAPIException as e:
            result["oco_error"] = str(e)
            result["note"] = "Buy filled but OCO failed. Run `trade.py protect` ASAP."
    else:
        result["note"] = "Limit order placed but not yet filled. Run `trade.py protect SYMBOL --stop X --target Y` after fill."

    out(result)


@cli.command()
@click.argument("symbol")
@click.option("--quantity", type=float, help="Quantity to sell (omit = sell all free)")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
def sell(symbol, quantity, yes):
    """MARKET sell — emergency exit."""
    client = get_client()
    symbol = symbol.upper()
    filters = get_filters(client, symbol)
    lot = Decimal(filters["LOT_SIZE"]["stepSize"])

    base_asset = symbol.replace("USDT", "").replace("BUSD", "")

    # Check total balance (free + locked) so we account for OCO-locked qty
    bal = client.get_asset_balance(asset=base_asset)
    total_balance = Decimal(bal["free"]) + Decimal(bal["locked"])

    if quantity is None:
        qty = total_balance
    else:
        qty = Decimal(str(quantity))
    qty = round_step(qty, lot)

    if qty <= 0:
        raise click.ClickException("Nothing to sell")

    plan = {
        "symbol": symbol,
        "action": "CANCEL OPEN ORDERS + MARKET SELL",
        "quantity": fmt(qty, lot),
        "free": bal["free"],
        "locked": bal["locked"],
        "testnet": TESTNET,
    }
    if not yes:
        click.echo(json.dumps(plan, indent=2))
        if not click.confirm("Execute?"):
            click.echo(json.dumps({"cancelled": True}))
            return

    # 1) Cancel any open orders for this symbol (frees up locked balance)
    open_orders = client.get_open_orders(symbol=symbol)
    cancelled = []
    for o in open_orders:
        try:
            client.cancel_order(symbol=symbol, orderId=o["orderId"])
            cancelled.append(o["orderId"])
        except BinanceAPIException:
            pass

    # 2) Re-check free balance (orders cancelled, RIF should now be free)
    bal2 = client.get_asset_balance(asset=base_asset)
    free_now = round_step(Decimal(bal2["free"]), lot)
    sell_qty = min(qty, free_now)

    if sell_qty <= 0:
        raise click.ClickException(f"After cancel, no free balance to sell. Free: {bal2['free']}")

    order = client.order_market_sell(symbol=symbol, quantity=fmt(sell_qty, lot))
    out({"sell": order, "cancelled_open_orders": cancelled})


@cli.command()
@click.argument("symbol")
@click.option("--stop", type=float, required=True)
@click.option("--target", type=float, required=True)
@click.option("--quantity", type=float, help="Quantity to protect (omit = full free balance)")
@click.option("--slip", type=float, default=0.5)
def protect(symbol, stop, target, quantity, slip):
    """Attach OCO (stop + TP) to existing position."""
    client = get_client()
    symbol = symbol.upper()
    filters = get_filters(client, symbol)
    lot = Decimal(filters["LOT_SIZE"]["stepSize"])
    tick = Decimal(filters["PRICE_FILTER"]["tickSize"])

    if quantity is None:
        base_asset = symbol.replace("USDT", "").replace("BUSD", "")
        bal = client.get_asset_balance(asset=base_asset)
        qty = Decimal(bal["free"])
    else:
        qty = Decimal(str(quantity))
    qty = round_step(qty, lot)

    stop_d = round_step(Decimal(str(stop)), tick)
    target_d = round_step(Decimal(str(target)), tick)
    stop_limit_d = round_step(stop_d * (Decimal("1") - Decimal(str(slip)) / Decimal("100")), tick)

    oco = place_oco_sell(
        client,
        symbol=symbol,
        quantity=fmt(qty, lot),
        target=fmt(target_d, tick),
        stop=fmt(stop_d, tick),
        stop_limit=fmt(stop_limit_d, tick),
    )
    out(oco)


@cli.command()
@click.option("--symbol", default=None)
def orders(symbol):
    """List open orders (optionally filtered by symbol)."""
    client = get_client()
    kwargs = {"symbol": symbol.upper()} if symbol else {}
    out(client.get_open_orders(**kwargs))


@cli.command()
@click.argument("symbol")
@click.argument("order_id", type=int)
def cancel(symbol, order_id):
    """Cancel an order by ID."""
    client = get_client()
    out(client.cancel_order(symbol=symbol.upper(), orderId=order_id))


@cli.command()
@click.argument("symbol")
@click.option("--limit", default=20)
def history(symbol, limit):
    """Recent fills for a symbol."""
    client = get_client()
    out(client.get_my_trades(symbol=symbol.upper(), limit=limit))


# ──────────────────────────────────────────────────────────────────
# Analysis commands (SMC + indicators)
# ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("symbol")
@click.option("--tf", default="1h", help="Timeframe: 5m, 15m, 1h, 4h, 1d")
@click.option("--limit", default=300, help="Candles to fetch")
@click.option("--json", "json_out", is_flag=True, help="Raw JSON output")
def analyze(symbol, tf, limit, json_out):
    """Full analysis: indicators + structure + liquidity + OBs + FVGs."""
    client = get_client()
    data = analysis.analyze_symbol(client, symbol.upper(), tf, limit)
    if json_out:
        out(data)
    else:
        display.render_analyze(data)


@cli.command()
@click.argument("symbol")
@click.option("--tf", default="4h")
@click.option("--json", "json_out", is_flag=True)
def structure(symbol, tf, json_out):
    """Show market structure (swings, BOS, CHoCH, trend bias)."""
    client = get_client()
    df = analysis.fetch_klines(client, symbol.upper(), tf)
    swings = analysis.detect_swings(df)
    summary = analysis.structure_summary(swings, float(df["close"].iloc[-1]))
    data = {
        "symbol": symbol.upper(),
        "interval": tf,
        "summary": summary,
        "recent_swings": [
            {"time": s.timestamp, "kind": s.kind, "price": s.price}
            for s in swings[-10:]
        ],
    }
    if json_out:
        out(data)
    else:
        display.render_structure(data, df_close=df["close"].tolist())


@cli.command()
@click.argument("symbol")
@click.option("--tf", default="1h")
def liquidity(symbol, tf):
    """Show liquidity zones (equal highs/lows, recent sweeps)."""
    client = get_client()
    df = analysis.fetch_klines(client, symbol.upper(), tf)
    swings = analysis.detect_swings(df)
    out({
        "symbol": symbol.upper(),
        "interval": tf,
        "current_price": float(df["close"].iloc[-1]),
        "liquidity": analysis.find_liquidity(swings),
        "recent_sweep": analysis.detect_sweep(df, swings),
    })


@cli.command(name="order-blocks")
@click.argument("symbol")
@click.option("--tf", default="1h")
def order_blocks(symbol, tf):
    """Show recent valid order blocks."""
    client = get_client()
    df = analysis.fetch_klines(client, symbol.upper(), tf)
    out({
        "symbol": symbol.upper(),
        "interval": tf,
        "current_price": float(df["close"].iloc[-1]),
        "order_blocks": analysis.detect_order_blocks(df),
        "fvg": analysis.detect_fvg(df),
    })


@cli.command(name="multi-tf")
@click.argument("symbol")
@click.option("--json", "json_out", is_flag=True)
def multi_tf(symbol, json_out):
    """Combined view: 1D / 4H / 1H / 15m structure + key indicators."""
    client = get_client()
    data = {
        "symbol": symbol.upper(),
        "1d": _short_tf(client, symbol, "1d"),
        "4h": _short_tf(client, symbol, "4h"),
        "1h": _short_tf(client, symbol, "1h"),
        "15m": _short_tf(client, symbol, "15m"),
    }
    if json_out:
        out(data)
    else:
        display.render_multi_tf(data)


def _short_tf(client, symbol, tf):
    a = analysis.analyze_symbol(client, symbol.upper(), tf, 200)
    return {
        "trend": a["structure"]["trend"],
        "rsi": a["indicators"]["rsi"],
        "adx": a["indicators"]["adx"],
        "macd_cross": a["indicators"]["macd_cross"],
        "swing_pattern": a["structure"].get("swing_pattern"),
        "recent_sweep": bool(a["recent_sweep"]),
    }


@cli.command()
@click.argument("symbol")
@click.option("--json", "json_out", is_flag=True)
def confluence(symbol, json_out):
    """Score the A+ setup confluence across HTF/MTF/LTF."""
    client = get_client()
    data = analysis.confluence_score(client, symbol.upper())
    if json_out:
        out(data)
    else:
        display.render_confluence(data)


@cli.command(name="setup-scan")
@click.option("--quote", default="USDT", help="Quote asset filter")
@click.option("--top", default=20, help="Scan top N coins by volume")
@click.option("--min-score", default=8)
@click.option("--json", "json_out", is_flag=True)
def setup_scan(quote, top, min_score, json_out):
    """Scan top-volume coins for A+ confluence setups."""
    client = get_client()
    tickers = client.get_ticker()
    candidates = sorted(
        [t for t in tickers if t["symbol"].endswith(quote)],
        key=lambda t: float(t["quoteVolume"]),
        reverse=True,
    )[:top]

    if not json_out:
        display.console.print(f"[dim]Scanning top {len(candidates)} {quote} pairs by volume…[/]")

    results = []
    for t in candidates:
        try:
            r = analysis.confluence_score(client, t["symbol"])
            if r["score"] >= min_score:
                results.append(r)
        except Exception as e:
            results.append({"symbol": t["symbol"], "error": str(e)})

    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    data = {"scanned": len(candidates), "found": len([r for r in results if "error" not in r]), "setups": results}
    if json_out:
        out(data)
    else:
        display.render_setup_scan(data)


# ──────────────────────────────────────────────────────────────────
# Risk / Position sizing
# ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--account", type=float, help="Account size USDT (omit = fetch live USDT balance)")
@click.option("--risk", type=float, default=2.0, help="Risk % of account (default 2)")
@click.option("--entry", type=float, required=True)
@click.option("--stop", type=float, required=True)
@click.option("--target", type=float, default=None)
def size(account, risk, entry, stop, target):
    """Position size calculator — never break the risk rule."""
    if account is None:
        client = get_client()
        bal = client.get_asset_balance(asset="USDT")
        account = float(bal["free"])

    risk_usdt = account * risk / 100
    distance = abs(entry - stop)
    if distance == 0:
        raise click.ClickException("Entry and stop cannot be equal")
    qty = risk_usdt / distance
    cost = qty * entry

    res = {
        "account_usdt": round(account, 2),
        "risk_pct": risk,
        "risk_usdt": round(risk_usdt, 2),
        "entry": entry,
        "stop": stop,
        "stop_distance_pct": round(distance / entry * 100, 2),
        "quantity": round(qty, 6),
        "position_cost_usdt": round(cost, 2),
        "position_pct_of_account": round(cost / account * 100, 1),
    }
    if target:
        reward = abs(target - entry) * qty
        res["target"] = target
        res["reward_usdt"] = round(reward, 2)
        res["rr_ratio"] = round(reward / risk_usdt, 2)
    out(res)


# ──────────────────────────────────────────────────────────────────
# Position status
# ──────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--json", "json_out", is_flag=True)
def status(json_out):
    """Live positions + unrealized P&L + open orders."""
    client = get_client()
    acc = client.get_account()
    open_orders = client.get_open_orders()

    positions = []
    for b in acc["balances"]:
        free = float(b["free"])
        locked = float(b["locked"])
        total = free + locked
        if total <= 0 or b["asset"] in ("USDT", "BUSD", "USDC", "USD", "DAI"):
            continue
        symbol = b["asset"] + "USDT"
        try:
            price = float(client.get_symbol_ticker(symbol=symbol)["price"])
            value = total * price
            if value < 1:
                continue
            positions.append({
                "asset": b["asset"],
                "quantity": total,
                "price": price,
                "value_usdt": round(value, 2),
                "open_orders_for_symbol": len([o for o in open_orders if o["symbol"] == symbol]),
            })
        except Exception:
            pass

    usdt = next((b for b in acc["balances"] if b["asset"] == "USDT"), {"free": "0"})
    total_value = sum(p["value_usdt"] for p in positions) + float(usdt["free"])

    data = {
        "usdt_free": float(usdt["free"]),
        "positions": positions,
        "open_orders_total": len(open_orders),
        "total_account_value_usdt": round(total_value, 2),
    }
    if json_out:
        out(data)
    else:
        display.render_status(data)


# ──────────────────────────────────────────────────────────────────
# Journal
# ──────────────────────────────────────────────────────────────────

@cli.group()
def journal():
    """Trade journal commands."""


@journal.command(name="list")
@click.option("--limit", default=20)
@click.option("--json", "json_out", is_flag=True)
def journal_list(limit, json_out):
    """List recent trades."""
    rows = jrnl.list_trades(limit)
    if json_out:
        out(rows)
    else:
        display.render_journal_list(rows)


@journal.command(name="stats")
@click.option("--json", "json_out", is_flag=True)
def journal_stats(json_out):
    """Show win-rate + P&L stats."""
    data = jrnl.stats()
    if json_out:
        out(data)
    else:
        display.render_journal_stats(data)


@journal.command(name="log")
@click.option("--symbol", required=True)
@click.option("--side", default="BUY")
@click.option("--entry", type=float, required=True)
@click.option("--quantity", type=float, required=True)
@click.option("--stop", type=float, required=True)
@click.option("--target", type=float, required=True)
@click.option("--setup", default="manual")
@click.option("--score", default="")
@click.option("--reason", default="")
@click.option("--buy-id", default="")
@click.option("--oco-id", default="")
def journal_log(symbol, side, entry, quantity, stop, target, setup, score, reason, buy_id, oco_id):
    """Manually log a trade entry."""
    risk = abs(entry - stop) * quantity
    reward = abs(target - entry) * quantity
    tid = jrnl.log_entry({
        "symbol": symbol.upper(),
        "side": side,
        "entry_price": entry,
        "quantity": quantity,
        "stop_loss": stop,
        "take_profit": target,
        "risk_usdt": round(risk, 4),
        "reward_usdt": round(reward, 4),
        "rr_ratio": round(reward / risk, 2) if risk else None,
        "setup_type": setup,
        "confluence_score": score,
        "reasoning": reason,
        "buy_order_id": buy_id,
        "oco_list_id": oco_id,
    })
    out({"logged": tid, "note_file": f"trade_notes/{tid}.md"})


@journal.command(name="close")
@click.argument("trade_id")
@click.option("--outcome", type=click.Choice(["WIN", "LOSS", "BE"]), required=True)
@click.option("--exit", "exit_price", type=float, required=True)
@click.option("--lesson", default="")
def journal_close(trade_id, outcome, exit_price, lesson):
    """Close a trade and update journal."""
    rows = jrnl.list_trades(10000)
    row = next((r for r in rows if r["trade_id"] == trade_id), None)
    if not row:
        raise click.ClickException(f"Trade {trade_id} not found")
    qty = float(row["quantity"])
    entry = float(row["entry_price"])
    pnl = (exit_price - entry) * qty if row["side"] == "BUY" else (entry - exit_price) * qty
    pnl_pct = pnl / (entry * qty) * 100
    jrnl.close_trade(trade_id, outcome, exit_price, round(pnl, 4), round(pnl_pct, 2), lesson)
    out({"trade_id": trade_id, "outcome": outcome, "pnl_usdt": round(pnl, 4), "pnl_pct": round(pnl_pct, 2)})


# ──────────────────────────────────────────────────────────────────
# Monitor — watch a position, send Discord alerts
# ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("symbol")
@click.option("--interval", default=60, help="Check interval in seconds (default 60)")
@click.option("--heartbeat", default=15, help="Heartbeat every N minutes (default 15)")
@click.option("--alert-near-pct", default=0.4, help="Alert when within N%% of stop/target (default 0.4)")
@click.option("--max-runtime", default=720, help="Max runtime in minutes (default 720 = 12h)")
def monitor(symbol, interval, heartbeat, alert_near_pct, max_runtime):
    """Watch an open position and send Discord alerts on key events."""
    import time
    client = get_client()
    symbol = symbol.upper()

    open_orders = client.get_open_orders(symbol=symbol)
    if not open_orders:
        click.echo(json.dumps({"error": f"No open orders for {symbol}. Nothing to monitor."}))
        return

    # Detect stop and target from open orders
    stop_price = None
    target_price = None
    for o in open_orders:
        if o["type"] == "STOP_LOSS_LIMIT":
            stop_price = float(o["stopPrice"])
        elif o["type"] in ("LIMIT_MAKER", "LIMIT"):
            target_price = float(o["price"])
    if not stop_price or not target_price:
        click.echo(json.dumps({"error": "Could not detect stop+target from open orders"}))
        return

    base = symbol.replace("USDT", "").replace("BUSD", "")
    bal = client.get_asset_balance(asset=base)
    qty_held = float(bal["free"]) + float(bal["locked"])
    entry_price = float(client.get_symbol_ticker(symbol=symbol)["price"])  # approximate entry

    start = time.time()
    last_heartbeat = 0.0
    last_structure = None
    alerted_near_stop = False
    alerted_near_target = False

    display.console.print(f"[bold cyan]Monitoring {symbol}[/] — stop ${stop_price} / target ${target_price}")
    display.console.print(f"[dim]Interval: {interval}s · Heartbeat: {heartbeat}m · Max: {max_runtime}m[/]")

    while True:
        elapsed_min = (time.time() - start) / 60
        if elapsed_min > max_runtime:
            display.console.print("[yellow]Max runtime reached. Stopping monitor.[/]")
            notify.send("signals", content=f"⏱ Monitor on {symbol} stopped (max runtime).")
            break

        # 1) Check if position closed (stop or TP filled)
        oo = client.get_open_orders(symbol=symbol)
        if not oo:
            # Position closed — figure out which side
            history = client.get_my_trades(symbol=symbol, limit=5)
            recent_sells = [t for t in history if not t["isBuyer"]]
            if recent_sells:
                last = recent_sells[-1]
                exit_p = float(last["price"])
                kind = "target_hit" if exit_p >= target_price * 0.999 else "stop_hit"
                pnl_pct = (exit_p - entry_price) / entry_price * 100
                notify.price_alert(symbol, kind, exit_p, target_price if kind == "target_hit" else stop_price, pnl_pct)
                display.console.print(f"[bold]{kind.upper()} at ${exit_p}[/]")
            else:
                notify.send("signals", content=f"⚠ {symbol} orders gone but no recent sell trade found.")
            break

        # 2) Get current price
        price = float(client.get_symbol_ticker(symbol=symbol)["price"])

        # 3) Near stop?
        dist_stop_pct = (price - stop_price) / stop_price * 100
        if dist_stop_pct < alert_near_pct and not alerted_near_stop:
            notify.price_alert(symbol, "near_stop", price, stop_price, dist_stop_pct)
            alerted_near_stop = True

        # 4) Near target?
        dist_target_pct = (target_price - price) / price * 100
        if dist_target_pct < alert_near_pct and not alerted_near_target:
            notify.price_alert(symbol, "near_target", price, target_price, dist_target_pct)
            alerted_near_target = True

        # 5) Structure change check (every iteration)
        try:
            df = analysis.fetch_klines(client, symbol, "1h", 200)
            swings = analysis.detect_swings(df)
            summary = analysis.structure_summary(swings, price)
            current_struct = summary["trend"]
            if last_structure is not None and current_struct != last_structure:
                notify.structure_change(symbol, last_structure, current_struct, "1h")
                display.console.print(f"[bold yellow]Structure changed: {last_structure} -> {current_struct}[/]")
            last_structure = current_struct
        except Exception:
            current_struct = last_structure or "?"

        # 6) Heartbeat
        if elapsed_min - last_heartbeat >= heartbeat:
            notify.heartbeat(symbol, price, entry_price, stop_price, target_price, int(elapsed_min), current_struct)
            last_heartbeat = elapsed_min
            display.console.print(f"[dim]Heartbeat sent — price ${price} / structure {current_struct}[/]")

        time.sleep(interval)


@cli.command()
def daemon():
    """Run the long-running monitoring daemon (use this on VPS)."""
    import daemon as daemon_module
    daemon_module.main()


if __name__ == "__main__":
    cli()
