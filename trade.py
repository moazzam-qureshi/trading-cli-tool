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
import charting
import journal as jrnl
import display
import notify
import risk
import whale_flow

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


_STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def _save_partial_intent(symbol: str, entry: float, stop: float, target: float,
                         qty: float, partial_pct: float, partial_at_r: float) -> None:
    """Daemon's PartialTPJob picks this up and acts when 1R is hit."""
    state = _load_state()
    intents = state.setdefault("partial_tp_intents", {})
    risk_per = entry - stop
    partial_price = entry + partial_at_r * risk_per
    intents[symbol] = {
        "entry": entry,
        "stop": stop,
        "target": target,
        "qty": qty,
        "partial_pct": partial_pct,
        "partial_at_r": partial_at_r,
        "partial_price": partial_price,
        "executed": False,
        "created_at": __import__("time").time(),
    }
    _save_state(state)


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
@click.option("--override-breaker", is_flag=True, help="Bypass the daily-loss circuit breaker (use with care)")
@click.option("--partial-pct", type=float, default=None, help="%% of position to close at 1R (enables partial-TP + BE move)")
@click.option("--partial-at-r", type=float, default=1.0, help="R-multiple at which to take partial (default 1.0)")
@click.option("--reason", default="", help="Reasoning for the trade (auto-filled from confluence_score if empty)")
@click.option("--setup", default="", help="Setup type label (e.g. 'OB+sweep'). Auto-filled if empty.")
@click.option("--no-journal", is_flag=True, help="Skip auto-journaling")
def buy(symbol, usd, quantity, entry, stop, target, slip, yes, override_breaker,
        partial_pct, partial_at_r, reason, setup, no_journal):
    """Buy + auto-attach OCO (stop + take-profit)."""
    client = get_client()
    symbol = symbol.upper()
    filters = get_filters(client, symbol)

    # Circuit breaker check
    acc = client.get_account()
    free_usdt = float(next((b for b in acc["balances"] if b["asset"] == "USDT"), {"free": "0"})["free"])
    # rough total (USDT + held positions estimated at last price) — use just free for breaker, conservative
    breaker = risk.check_trading_allowed(account_value=max(free_usdt, 1.0))
    if not breaker["allowed"] and not override_breaker:
        out({"error": "Trading paused by circuit breaker", **breaker,
             "hint": "Use --override-breaker to bypass (don't, unless you have a really good reason)."})
        sys.exit(2)

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

    # ── Sanity guards: catch order-of-magnitude typos BEFORE sending any order ──
    # Spot longs only: stop must be below current price, target above. Anything > 50%
    # off current price is almost certainly a copy-paste error (e.g. another symbol's
    # levels). Real framework stops/targets are within a few percent of price.
    cur_f = float(current)
    if float(stop_d) >= cur_f:
        raise click.ClickException(
            f"Refusing buy: stop {stop_d} is at/above current {current}. "
            f"For a long, stop must be below current price."
        )
    if float(target_d) <= cur_f:
        raise click.ClickException(
            f"Refusing buy: target {target_d} is at/below current {current}. "
            f"For a long, target must be above current price."
        )
    stop_dist_pct = (cur_f - float(stop_d)) / cur_f * 100
    target_dist_pct = (float(target_d) - cur_f) / cur_f * 100
    if stop_dist_pct > 50:
        raise click.ClickException(
            f"Refusing buy: stop {stop_d} is {stop_dist_pct:.1f}% below current {current}. "
            f"Almost certainly a typo (right symbol's stop pasted on wrong symbol?). "
            f"Re-check before retrying."
        )
    if target_dist_pct > 50:
        raise click.ClickException(
            f"Refusing buy: target {target_d} is {target_dist_pct:.1f}% above current {current}. "
            f"Almost certainly a typo. Re-check before retrying."
        )
    if stop_dist_pct < 0.05:
        raise click.ClickException(
            f"Refusing buy: stop {stop_d} is only {stop_dist_pct:.3f}% below current — "
            f"will be triggered by noise. Use a wider stop based on real structure."
        )

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
            # Persist partial-TP intent for daemon
            if partial_pct:
                try:
                    avg_fill_p = float(Decimal(buy_order["cummulativeQuoteQty"]) / Decimal(buy_order["executedQty"]))
                    _save_partial_intent(symbol, avg_fill_p, float(stop_d), float(target_d),
                                          float(executed_qty), partial_pct, partial_at_r)
                    result["partial_intent_saved"] = {"pct": partial_pct, "at_r": partial_at_r}
                except Exception as e:
                    result["partial_intent_error"] = str(e)
            # Auto-journal
            if not no_journal:
                try:
                    avg_fill_j = float(Decimal(buy_order["cummulativeQuoteQty"]) / Decimal(buy_order["executedQty"]))
                    qty_j = float(executed_qty)
                    risk_usdt = abs(avg_fill_j - float(stop_d)) * qty_j
                    reward_usdt = abs(float(target_d) - avg_fill_j) * qty_j
                    j_reason = reason
                    j_setup = setup
                    j_score = ""
                    if not j_reason or not j_setup:
                        try:
                            conf = analysis.confluence_score(client, symbol)
                            if not j_reason:
                                j_reason = " | ".join(conf.get("reasons", []))
                            if not j_setup:
                                # crude: setup label from highest-scoring confluences
                                tags = []
                                for r_text in conf.get("reasons", []):
                                    if "OB" in r_text or "order block" in r_text: tags.append("OB")
                                    if "FVG" in r_text: tags.append("FVG")
                                    if "sweep" in r_text: tags.append("sweep")
                                    if "Volume" in r_text or "volume" in r_text: tags.append("vol")
                                j_setup = "+".join(dict.fromkeys(tags)) or "confluence"
                            j_score = f"{conf.get('score','?')}/10"
                        except Exception:
                            pass
                    tid = jrnl.log_entry({
                        "symbol": symbol,
                        "side": "BUY",
                        "entry_price": avg_fill_j,
                        "quantity": qty_j,
                        "stop_loss": float(stop_d),
                        "take_profit": float(target_d),
                        "risk_usdt": round(risk_usdt, 4),
                        "reward_usdt": round(reward_usdt, 4),
                        "rr_ratio": round(reward_usdt / risk_usdt, 2) if risk_usdt else None,
                        "setup_type": j_setup,
                        "confluence_score": j_score,
                        "reasoning": j_reason or "(no reason captured)",
                        "buy_order_id": str(buy_order.get("orderId", "")),
                        "oco_list_id": str(oco.get("orderListId", "")),
                    })
                    result["journal_id"] = tid
                except Exception as e:
                    result["journal_error"] = str(e)
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

    # Same sanity guards as buy: catch order-of-magnitude typos
    current = float(client.get_symbol_ticker(symbol=symbol)["price"])
    if float(stop_d) >= current:
        raise click.ClickException(f"Refusing protect: stop {stop_d} ≥ current {current}.")
    if float(target_d) <= current:
        raise click.ClickException(f"Refusing protect: target {target_d} ≤ current {current}.")
    sd = (current - float(stop_d)) / current * 100
    td = (float(target_d) - current) / current * 100
    if sd > 50 or td > 50:
        raise click.ClickException(
            f"Refusing protect: stop {sd:.1f}% / target {td:.1f}% from current {current}. "
            f"Almost certainly a typo — re-check before retrying."
        )

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
@click.option("--whale", is_flag=True, help="Include whale-flow bonus stars (funding, OI, CVD, large trades)")
def confluence(symbol, json_out, whale):
    """Score the A+ setup confluence across HTF/MTF/LTF."""
    client = get_client()
    data = analysis.confluence_score(client, symbol.upper())

    if whale and data.get("direction"):
        flow = whale_flow.whale_flow_summary(client, symbol.upper())
        bonus, bonus_reasons = whale_flow.whale_bonus_stars(flow, data["direction"])
        data["whale_flow"] = flow
        data["whale_bonus_stars"] = bonus
        data["score"] = data["score"] + bonus
        data["reasons"] = data["reasons"] + bonus_reasons
        # Re-evaluate verdict with bonus
        s = data["score"]
        data["verdict"] = "A+ SETUP" if s >= 8 else "Decent" if s >= 5 else "Skip"

    if json_out:
        out(data)
    else:
        display.render_confluence(data)


@cli.command()
@click.argument("symbol")
@click.option("--tf", default="15m", help="Timeframe: 5m, 15m, 1h, 4h, 1d (default 15m)")
@click.option("--bars", default=150, help="Number of candles to render (default 150)")
@click.option("--out", default=None, help="Output path (default /app/charts/<SYM>_<TF>.png)")
def chart(symbol, tf, bars, out):
    """Render a candlestick chart with SMC overlays (OBs, FVGs, swings, sweep)."""
    client = get_client()
    path = charting.render_chart(client, symbol.upper(), tf=tf, bars=bars, out_path=out)
    click.echo(json.dumps({"symbol": symbol.upper(), "tf": tf, "bars": bars, "path": path}))


@cli.command(name="chart-multi")
@click.argument("symbol")
@click.option("--tfs", default="1d,4h,1h,15m,5m", help="Comma-separated timeframes (pro-trader standard: 1d,4h,1h,15m,5m)")
def chart_multi(symbol, tfs):
    """Render charts at multiple pro-trader timeframes for a full top-down read."""
    client = get_client()
    tf_list = [t.strip() for t in tfs.split(",") if t.strip()]
    bars_for = {"1d": 120, "4h": 150, "1h": 150, "15m": 150, "5m": 100, "30m": 150, "2h": 150}
    paths = {}
    errors = {}
    for tf in tf_list:
        try:
            paths[tf] = charting.render_chart(
                client, symbol.upper(), tf=tf, bars=bars_for.get(tf, 150)
            )
        except Exception as e:
            errors[tf] = str(e)
    click.echo(json.dumps({"symbol": symbol.upper(), "rendered": paths, "errors": errors}))


def _whale_event_fields(flow: dict) -> tuple[list[dict], list[str]]:
    """Build Discord embed fields + summary triggers from a whale-flow snapshot."""
    fields = []
    triggers = []

    f = flow.get("funding")
    if f is not None:
        fields.append({"name": "Funding", "value": f"{f['current_pct']:+.4f}% ({f['interpretation']})", "inline": True})
        if f["interpretation"].startswith("deeply"):
            triggers.append(f"funding_{f['interpretation']}")

    oi = flow.get("open_interest")
    if oi is not None and oi.get("delta_24h_pct") is not None:
        fields.append({"name": "OI 24h Δ", "value": f"{oi['delta_24h_pct']:+.2f}% ({oi['interpretation']})", "inline": True})
        if "strongly" in oi["interpretation"]:
            triggers.append(f"oi_{oi['interpretation']}")

    cvd = flow.get("spot_cvd_4h") or flow.get("spot_cvd")
    if cvd:
        fields.append({"name": "Spot CVD 4h", "value": f"{cvd['cvd_pct_of_total']:+.2f}% ({cvd['interpretation']})", "inline": True})
        if "strong" in cvd["interpretation"]:
            triggers.append(f"cvd_{cvd['interpretation']}")

    lt = flow.get("large_trades_1h") or flow.get("large_trades")
    if lt and lt["total_large_trades"] > 0:
        fields.append({"name": "Large trades 1h", "value": f"{lt['total_large_trades']} trades, net ${lt['net_notional_usdt']:+,.0f}", "inline": False})
        if abs(lt["net_notional_usdt"]) > 250_000:
            triggers.append(f"large_net_{'buy' if lt['net_notional_usdt']>0 else 'sell'}")

    return fields, triggers


@cli.command(name="whale-alert")
@click.argument("symbol")
@click.option("--force", is_flag=True, help="Send even if no notable trigger")
def whale_alert_cmd(symbol, force):
    """Run whale-flow on a symbol; post snapshot to #whale channel if notable."""
    client = get_client()
    sym = symbol.upper()
    flow = whale_flow.whale_flow_summary(client, sym)
    fields, triggers = _whale_event_fields(flow)

    if not triggers and not force:
        click.echo(json.dumps({"symbol": sym, "triggers": [], "sent": False, "reason": "no_notable_event"}))
        return

    headline = ", ".join(triggers) if triggers else "snapshot"
    color = "green" if any("buy" in t or "accumulation" in t for t in triggers) else \
            "red" if any("sell" in t or "distribution" in t for t in triggers) else "purple"
    sent = notify.whale_event(sym, headline, fields, color=color)
    click.echo(json.dumps({"symbol": sym, "triggers": triggers, "sent": sent}))


@cli.command(name="whale-watch")
@click.option("--symbols", default="", help="Comma-separated symbols (default: top 20 by USDT volume)")
@click.option("--top", default=20, help="If --symbols not given, scan this many top-volume USDT pairs")
def whale_watch(symbols, top):
    """Scan symbols for whale-flow triggers; post notable ones to #whale."""
    client = get_client()

    if symbols:
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        tickers = client.get_ticker()
        usdts = [t for t in tickers if t["symbol"].endswith("USDT")]
        usdts.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
        sym_list = [t["symbol"] for t in usdts[:top]]

    results = []
    for sym in sym_list:
        try:
            flow = whale_flow.whale_flow_summary(client, sym)
            fields, triggers = _whale_event_fields(flow)
            if triggers:
                color = "green" if any("buy" in t or "accumulation" in t for t in triggers) else \
                        "red" if any("sell" in t or "distribution" in t for t in triggers) else "purple"
                notify.whale_event(sym, ", ".join(triggers), fields, color=color)
                results.append({"symbol": sym, "triggers": triggers, "sent": True})
            else:
                results.append({"symbol": sym, "triggers": [], "sent": False})
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})

    out({"scanned": len(sym_list), "alerts_sent": sum(1 for r in results if r.get("sent")), "results": results})


@cli.command(name="whale-flow")
@click.argument("symbol")
@click.option("--cvd-minutes", default=240, help="CVD lookback window in minutes (default 240 = 4h)")
@click.option("--large-threshold", default=50000.0, help="Large-trade threshold in USDT (default 50k)")
@click.option("--large-minutes", default=60, help="Large-trade lookback window (default 60min)")
@click.option("--json", "json_out", is_flag=True)
def whale_flow_cmd(symbol, cvd_minutes, large_threshold, large_minutes, json_out):
    """Pull funding, OI, spot CVD, and large-trade activity for a symbol."""
    client = get_client()
    sym = symbol.upper()
    data = {
        "symbol": sym,
        "funding": whale_flow.get_funding(client, sym),
        "open_interest": whale_flow.get_open_interest(client, sym),
        "spot_cvd": whale_flow.get_spot_cvd(client, sym, lookback_minutes=cvd_minutes),
        "large_trades": whale_flow.get_large_trades(
            client, sym, threshold_usdt=large_threshold, lookback_minutes=large_minutes
        ),
    }
    if json_out:
        out(data)
        return

    # Pretty render
    click.echo(f"\n=== Whale-flow: {sym} ===\n")

    f = data["funding"]
    if f:
        click.echo(f"Funding:        {f['current_pct']:+.4f}%   (24h avg {f['avg_24h_pct']:+.4f}%)")
        click.echo(f"                → {f['interpretation']}")
    else:
        click.echo("Funding:        no perp listing")

    oi = data["open_interest"]
    if oi:
        d = oi.get("delta_24h_pct")
        click.echo(f"Open Interest:  {oi['current']:.2f}   24h Δ {d:+.2f}%   → {oi['interpretation']}")
    else:
        click.echo("Open Interest:  no perp listing")

    cvd = data["spot_cvd"]
    click.echo(f"\nSpot CVD ({cvd['lookback_minutes']}min, {cvd['trade_count']} trades):")
    click.echo(f"  Buy vol:   ${cvd['buy_vol_usdt']:>14,.0f}")
    click.echo(f"  Sell vol:  ${cvd['sell_vol_usdt']:>14,.0f}")
    click.echo(f"  CVD:       ${cvd['cvd_usdt']:>+14,.0f}   ({cvd['cvd_pct_of_total']:+.2f}%)")
    click.echo(f"  → {cvd['interpretation']}")

    lt = data["large_trades"]
    click.echo(f"\nLarge trades ≥${lt['threshold_usdt']:,.0f}, last {lt['lookback_minutes']}min:")
    click.echo(f"  Total:     {lt['total_large_trades']}   (buys {lt['buy_count']} / sells {lt['sell_count']})")
    click.echo(f"  Net:       ${lt['net_notional_usdt']:>+14,.0f}")
    if lt['samples']:
        click.echo("  Recent:")
        for s in lt['samples'][-5:]:
            click.echo(f"    {s['time'][11:19]}  {s['side']:>4}  ${s['notional_usdt']:>10,.0f}  @ {s['price']}")


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
# Backtest
# ──────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("symbol")
@click.option("--bars", default=1500, help="15m bars to test (1500 ≈ 16 days)")
@click.option("--min-score", default=8)
@click.option("--rr", default=2.0, help="Reward:risk multiple")
@click.option("--max-hold", default=96, help="Max bars to hold (96 = 24h on 15m)")
@click.option("--risk-pct", default=2.0)
@click.option("--equity", default=150.0, help="Starting equity for compounding sim")
@click.option("--partial-pct", default=0.0, help="%% of position to close at partial_at_r (0=disabled)")
@click.option("--partial-at-r", default=1.0)
@click.option("--trail-mode", default="off", type=click.Choice(["off", "be"]))
@click.option("--trail-at-r", default=1.0)
@click.option("--fast/--slow", default=True, help="Use vectorized engine (default: fast)")
@click.option("--show-trades", is_flag=True, help="Print every simulated trade")
@click.option("--json", "json_out", is_flag=True)
def backtest(symbol, bars, min_score, rr, max_hold, risk_pct, equity, partial_pct, partial_at_r,
             trail_mode, trail_at_r, fast, show_trades, json_out):
    """Backtest the confluence strategy on historical data."""
    import backtest as bt
    client = get_client()
    runner = bt.run_backtest_fast if fast else bt.run_backtest
    extra = {"trail_mode": trail_mode, "trail_at_r": trail_at_r} if fast else {}
    result = runner(
        client, symbol.upper(),
        bars_15m=bars, min_score=min_score, rr=rr,
        max_hold_bars=max_hold, risk_pct=risk_pct, starting_equity=equity,
        partial_pct=partial_pct, partial_at_r=partial_at_r,
        **extra,
    )
    if json_out:
        out(result)
        return
    if "error" in result:
        click.echo(json.dumps(result, indent=2))
        return
    s = result["stats"]
    p = result["period"]
    click.echo(f"\n=== Backtest {result['symbol']} ===")
    click.echo(f"Period: {p['from'][:10]} → {p['to'][:10]} ({p['days']} days)")
    click.echo(f"Config: min_score={min_score} RR={rr} risk={risk_pct}% max_hold={max_hold} bars\n")
    click.echo(f"Signals:        {s['total_signals']}")
    click.echo(f"Closed:         {s['closed']}  (W:{s['wins']} L:{s['losses']} T:{s['timeouts']})")
    click.echo(f"Win rate:       {s['win_rate_pct']}%")
    click.echo(f"Avg R:          {s['avg_r']}")
    click.echo(f"Total R:        {s['total_r']}")
    click.echo(f"Profit factor:  {s['profit_factor']}")
    click.echo(f"Equity:         ${s['starting_equity']} → ${s['final_equity']} ({s['return_pct']:+.1f}%)")
    click.echo(f"Max drawdown:   {s['max_drawdown_pct']}%\n")

    # Verdict
    if s["closed"] < 10:
        click.echo("⚠ Too few trades for statistical significance (need ≥ 30)")
    elif s["avg_r"] > 0.3 and s["win_rate_pct"] > 35:
        click.echo("✓ Edge looks real — strategy is profitable on this sample")
    elif s["avg_r"] > 0:
        click.echo("~ Marginal edge — try higher min_score or different symbols")
    else:
        click.echo("✗ No edge on this sample — current rules lose money here")

    if show_trades:
        click.echo("\n--- Trades ---")
        for t in result["trades"]:
            click.echo(f"  {t['entry_time'][:16]} {t['direction']:5s} score={t['score']} "
                       f"entry={t['entry']:.4f} stop={t['stop']:.4f} target={t['target']:.4f} "
                       f"→ {t['outcome']:7s} R={t['r_multiple']:+.2f}")


@cli.command(name="backtest-multi")
@click.option("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
@click.option("--bars", default=1500)
@click.option("--min-score", default=8)
@click.option("--rr", default=2.0)
@click.option("--partial-pct", default=0.0)
@click.option("--partial-at-r", default=1.0)
@click.option("--json", "json_out", is_flag=True)
def backtest_multi(symbols, bars, min_score, rr, partial_pct, partial_at_r, json_out):
    """Backtest across multiple symbols and aggregate."""
    import backtest as bt
    client = get_client()
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    result = bt.run_multi_backtest(client, syms, bars_15m=bars, min_score=min_score, rr=rr,
                                    partial_pct=partial_pct, partial_at_r=partial_at_r)
    if json_out:
        out(result)
        return
    click.echo("\n=== Multi-symbol backtest ===")
    for sym, s in result["by_symbol"].items():
        if "error" in s:
            click.echo(f"  {sym:10s} ERROR {s['error']}")
            continue
        click.echo(f"  {sym:10s} closed={s['closed']:3d}  WR={s['win_rate_pct']:5.1f}%  "
                   f"avgR={s['avg_r']:+.2f}  totalR={s['total_r']:+.2f}  "
                   f"return={s['return_pct']:+.1f}%")
    a = result["aggregate"]
    click.echo(f"\nAggregate: {a['total_trades']} trades, WR={a['win_rate_pct']}%, "
               f"avgR={a['avg_r']}, totalR={a['total_r']}")


@cli.command(name="backtest-sweep")
@click.option("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT")
@click.option("--bars", default=17500, help="15m bars (17500 ≈ 6 months)")
@click.option("--scores", default="8,9,10")
@click.option("--rr", default=2.0)
@click.option("--partial-pct", default=0.0)
@click.option("--partial-at-r", default=1.0)
@click.option("--score-every-n", default=4, help="Score every Nth 15m bar (4 = hourly, default for speed)")
@click.option("--trail-mode", default="off", type=click.Choice(["off", "be"]), help="Stop-trail: 'be' = move stop to entry at trail_at_r")
@click.option("--trail-at-r", default=1.0, help="R-multiple at which to arm BE-trail")
@click.option("--fast/--slow", default=True, help="Use vectorized engine (default: fast)")
@click.option("--json", "json_out", is_flag=True)
def backtest_sweep(symbols, bars, scores, rr, partial_pct, partial_at_r, score_every_n,
                   trail_mode, trail_at_r, fast, json_out):
    """Sweep min_score thresholds to find where edge appears."""
    import backtest as bt
    client = get_client()
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    score_list = [int(s) for s in scores.split(",")]
    if not fast and trail_mode != "off":
        raise click.ClickException("trail-mode only supported on the fast engine")
    runner = bt.run_score_sweep_fast if fast else bt.run_score_sweep
    extra = {"trail_mode": trail_mode, "trail_at_r": trail_at_r} if fast else {}
    result = runner(client, syms, score_list,
                    bars_15m=bars, rr=rr,
                    partial_pct=partial_pct, partial_at_r=partial_at_r,
                    score_every_n=score_every_n, **extra)
    if json_out:
        out(result)
        return
    click.echo(f"\n=== Score sweep: {syms} ===")
    click.echo(f"Bars/symbol: {bars} (~{bars*15//60//24} days)  RR={rr}  "
               f"partial={partial_pct}%@{partial_at_r}R  score_every={score_every_n}\n")
    click.echo(f"{'Score':>5}  {'Trades':>6}  {'WR%':>6}  {'avgR':>7}  {'totalR':>8}")
    for sc, stats in result["per_score"].items():
        click.echo(f"{sc:>5}  {stats['total_trades']:>6}  {stats['win_rate_pct']:>6.1f}  "
                   f"{stats['avg_r']:>+7.2f}  {stats['total_r']:>+8.2f}")
    click.echo("\nPer-symbol breakdown (lowest score):")
    lowest = min(result["per_score"].keys(), key=int)
    for sym, s in result["per_score"][lowest]["per_symbol"].items():
        if "error" in s:
            click.echo(f"  {sym:10s}  ERROR {s['error']}")
            continue
        click.echo(f"  {sym:10s}  closed={s.get('closed', 0):3d}  "
                   f"WR={s.get('win_rate_pct', 0):5.1f}%  avgR={s.get('avg_r', 0):+.2f}  "
                   f"return={s.get('return_pct', 0):+.1f}%")


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


@journal.command(name="analyze")
@click.option("--json", "json_out", is_flag=True)
def journal_analyze(json_out):
    """Per-setup / symbol / hour-of-day / score breakdowns to surface edge."""
    data = jrnl.stats_breakdown()
    if json_out:
        out(data)
        return
    if data.get("closed", 0) == 0:
        click.echo("No closed trades yet.")
        return
    o = data["overall"]
    click.echo(f"\n=== Overall ({o['n']} closed) ===")
    click.echo(f"  Win rate: {o['win_rate_pct']}%   Avg R: {o['avg_r']:+.2f}   P&L: ${o['total_pnl_usdt']:+.2f}")

    def render(title: str, group: dict):
        click.echo(f"\n--- {title} ---")
        rows = sorted(group.items(), key=lambda kv: kv[1]["n"], reverse=True)
        for k, s in rows:
            if s["n"] == 0:
                continue
            click.echo(f"  {str(k):14s}  n={s['n']:3d}  W={s['wins']:2d} L={s['losses']:2d}  "
                       f"WR={s['win_rate_pct']:5.1f}%  avgR={s['avg_r']:+.2f}  "
                       f"P&L=${s['total_pnl_usdt']:+.2f}")

    render("By setup", data["by_setup"])
    render("By symbol", data["by_symbol"])
    render("By confluence score", data["by_score"])
    render("By hour of day (UTC)", data["by_hour_utc"])
    render("By day of week", data["by_day_of_week"])


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


@journal.command(name="recompute-pnl")
@click.argument("trade_id")
def journal_recompute_pnl(trade_id):
    """Recompute net P&L from real fills + update an already-closed trade row."""
    rows = jrnl.list_trades(10000)
    row = next((r for r in rows if r["trade_id"] == trade_id), None)
    if not row:
        raise click.ClickException(f"Trade {trade_id} not found")
    client = get_client()
    info = jrnl.compute_net_pnl(client, row["symbol"], row["timestamp"],
                                  buy_order_id=row.get("buy_order_id", ""),
                                  oco_list_id=row.get("oco_list_id", ""))
    pnl = info["net_pnl_usdt"]
    cost = info["buy_quote_usdt"]
    pnl_pct = pnl / cost * 100 if cost else 0
    jrnl.close_trade(trade_id, row["outcome"], float(row.get("exit_price") or 0),
                     round(pnl, 4), round(pnl_pct, 2), row.get("lesson", ""))
    out({"trade_id": trade_id, **info, "pnl_pct": round(pnl_pct, 2)})


@journal.command(name="close")
@click.argument("trade_id")
@click.option("--outcome", type=click.Choice(["WIN", "LOSS", "BE"]), required=True)
@click.option("--exit", "exit_price", type=float, required=True)
@click.option("--lesson", default="")
@click.option("--gross", is_flag=True, help="Use gross math instead of querying Binance fills for net")
def journal_close(trade_id, outcome, exit_price, lesson, gross):
    """Close a trade and update journal. Defaults to net P&L from real fills (incl. fees)."""
    rows = jrnl.list_trades(10000)
    row = next((r for r in rows if r["trade_id"] == trade_id), None)
    if not row:
        raise click.ClickException(f"Trade {trade_id} not found")

    if gross:
        qty = float(row["quantity"])
        entry = float(row["entry_price"])
        pnl = (exit_price - entry) * qty if row["side"] == "BUY" else (entry - exit_price) * qty
        pnl_pct = pnl / (entry * qty) * 100
        fee_info = {}
    else:
        client = get_client()
        try:
            fee_info = jrnl.compute_net_pnl(client, row["symbol"], row["timestamp"],
                                             buy_order_id=row.get("buy_order_id", ""),
                                             oco_list_id=row.get("oco_list_id", ""))
            pnl = fee_info["net_pnl_usdt"]
            cost_basis = fee_info["buy_quote_usdt"] or (float(row["entry_price"]) * float(row["quantity"]))
            pnl_pct = pnl / cost_basis * 100 if cost_basis else 0
        except Exception as e:
            click.echo(f"⚠ Net P&L lookup failed ({e}); falling back to gross math.", err=True)
            qty = float(row["quantity"]); entry = float(row["entry_price"])
            pnl = (exit_price - entry) * qty if row["side"] == "BUY" else (entry - exit_price) * qty
            pnl_pct = pnl / (entry * qty) * 100
            fee_info = {"error": str(e)}

    jrnl.close_trade(trade_id, outcome, exit_price, round(pnl, 4), round(pnl_pct, 2), lesson)
    out({"trade_id": trade_id, "outcome": outcome,
         "pnl_usdt": round(pnl, 4), "pnl_pct": round(pnl_pct, 2),
         "fee_breakdown": fee_info})


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


@cli.command(name="risk-check")
@click.option("--json", "json_out", is_flag=True)
def risk_check(json_out):
    """Show today's loss budget and circuit-breaker status."""
    client = get_client()
    bal = client.get_asset_balance(asset="USDT")
    free = float(bal["free"])
    info = risk.check_trading_allowed(account_value=max(free, 1.0))
    if json_out:
        out(info)
    else:
        click.echo(f"\nDate: {info['today']}")
        click.echo(f"Trades closed today: {info['trades_closed_today']}")
        click.echo(f"  · Losses: {info['losses_today']} / {info['max_losses']}")
        click.echo(f"  · P&L:    ${info['pnl_today_usdt']:+.2f}")
        click.echo(f"  · DD:     {info['drawdown_today_pct']}% / {info['max_dd_pct']}%")
        status = "✓ ALLOWED" if info["allowed"] else "✗ BLOCKED"
        click.echo(f"\nNew trades: {status}")
        if not info["allowed"]:
            click.echo(f"Reason: {info['reason']}")


@cli.command()
def daemon():
    """Run the long-running monitoring daemon (use this on VPS)."""
    import daemon as daemon_module
    daemon_module.main()


if __name__ == "__main__":
    cli()
