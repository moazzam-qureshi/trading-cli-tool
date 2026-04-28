"""Trade journal — auto-logs every trade with reasoning + post-mortem template."""
import csv
import json
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
from pathlib import Path

JOURNAL_PATH = Path(__file__).parent / "trades.csv"
NOTES_DIR = Path(__file__).parent / "trade_notes"
NOTES_DIR.mkdir(exist_ok=True)

FIELDS = [
    "trade_id", "timestamp", "symbol", "side", "entry_price", "quantity",
    "stop_loss", "take_profit", "risk_usdt", "reward_usdt", "rr_ratio",
    "setup_type", "confluence_score", "reasoning", "outcome", "exit_price",
    "pnl_usdt", "pnl_pct", "lesson", "buy_order_id", "oco_list_id",
]


def _ensure_csv() -> None:
    # Write header if file is missing OR empty (handles bind-mounted empty files)
    if not JOURNAL_PATH.exists() or JOURNAL_PATH.stat().st_size == 0:
        with open(JOURNAL_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def log_entry(data: dict) -> str:
    """Log a new trade entry. Returns trade_id."""
    _ensure_csv()
    trade_id = data.get("trade_id") or _utcnow().strftime("T%Y%m%d-%H%M%S")
    row = {f: "" for f in FIELDS}
    row.update(data)
    row["trade_id"] = trade_id
    row["timestamp"] = _utcnow().isoformat()
    row["outcome"] = "OPEN"

    with open(JOURNAL_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writerow(row)

    note_path = NOTES_DIR / f"{trade_id}.md"
    note_path.write_text(_entry_template(row), encoding="utf-8")
    return trade_id


def _entry_template(row: dict) -> str:
    return f"""# Trade {row['trade_id']} — {row['symbol']} {row['side']}

**Status:** OPEN
**Entered:** {row['timestamp']}
**Setup:** {row.get('setup_type', '?')}
**Confluence Score:** {row.get('confluence_score', '?')}

## Levels
- Entry: {row.get('entry_price', '?')}
- Quantity: {row.get('quantity', '?')}
- Stop Loss: {row.get('stop_loss', '?')}
- Take Profit: {row.get('take_profit', '?')}
- Risk: ${row.get('risk_usdt', '?')}
- Reward: ${row.get('reward_usdt', '?')}
- R:R: {row.get('rr_ratio', '?')}

## Reasoning at Entry
{row.get('reasoning', '(none)')}

## Order IDs
- Buy: {row.get('buy_order_id', '?')}
- OCO List: {row.get('oco_list_id', '?')}

---

## Post-Mortem (filled after exit)

**Outcome:** _pending_
**Exit Price:** _pending_
**P&L:** _pending_

### What Played Out
_to be filled_

### What I Got Right
_to be filled_

### What I Got Wrong
_to be filled_

### Lesson
_to be filled_
"""


def close_trade(trade_id: str, outcome: str, exit_price: float, pnl_usdt: float, pnl_pct: float, lesson: str = "") -> None:
    """Update an existing trade's outcome."""
    _ensure_csv()
    rows = list(csv.DictReader(open(JOURNAL_PATH, encoding="utf-8")))
    for r in rows:
        if r["trade_id"] == trade_id:
            r["outcome"] = outcome
            r["exit_price"] = str(exit_price)
            r["pnl_usdt"] = str(pnl_usdt)
            r["pnl_pct"] = str(pnl_pct)
            r["lesson"] = lesson
    with open(JOURNAL_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    note = NOTES_DIR / f"{trade_id}.md"
    if note.exists():
        text = note.read_text(encoding="utf-8")
        text = text.replace("**Status:** OPEN", f"**Status:** CLOSED ({outcome})")
        text = text.replace("**Outcome:** _pending_", f"**Outcome:** {outcome}")
        text = text.replace("**Exit Price:** _pending_", f"**Exit Price:** {exit_price}")
        text = text.replace("**P&L:** _pending_", f"**P&L:** ${pnl_usdt} ({pnl_pct}%)")
        if lesson:
            text = text.replace("### Lesson\n_to be filled_", f"### Lesson\n{lesson}")
        note.write_text(text, encoding="utf-8")


def compute_net_pnl(client, symbol: str, entry_ts_iso: str, exit_ts_iso: str | None = None) -> dict:
    """Compute net P&L by walking get_my_trades fills between entry and exit timestamps.

    Net = sum(sell quoteQty) - sum(buy quoteQty) - sum(commission in USDT-equivalent).
    Fees in the base asset are valued at the trade's price.
    Fees in BNB or other are best-effort (priced at current ticker).
    """
    entry_ms = int(datetime.fromisoformat(entry_ts_iso.replace("Z", "+00:00")).timestamp() * 1000)
    exit_ms = (int(datetime.fromisoformat(exit_ts_iso.replace("Z", "+00:00")).timestamp() * 1000)
               if exit_ts_iso else None)

    fills = client.get_my_trades(symbol=symbol, limit=200)
    base = symbol.replace("USDT", "").replace("BUSD", "").replace("USDC", "")
    quote = "USDT" if symbol.endswith("USDT") else "BUSD" if symbol.endswith("BUSD") else "USDC"

    relevant = []
    for f in fills:
        t = int(f["time"])
        if t < entry_ms - 5000:  # 5s grace
            continue
        if exit_ms and t > exit_ms + 5000:
            continue
        relevant.append(f)

    buys = [f for f in relevant if f["isBuyer"]]
    sells = [f for f in relevant if not f["isBuyer"]]

    buy_quote = sum(float(f["quoteQty"]) for f in buys)
    sell_quote = sum(float(f["quoteQty"]) for f in sells)

    fee_usdt = 0.0
    for f in relevant:
        c = float(f["commission"])
        if c == 0:
            continue
        asset = f["commissionAsset"]
        if asset == quote:
            fee_usdt += c
        elif asset == base:
            fee_usdt += c * float(f["price"])
        else:
            # e.g. BNB — best effort using current price
            try:
                p = float(client.get_symbol_ticker(symbol=f"{asset}{quote}")["price"])
                fee_usdt += c * p
            except Exception:
                pass

    gross = sell_quote - buy_quote
    net = gross - fee_usdt
    return {
        "buy_quote_usdt": round(buy_quote, 4),
        "sell_quote_usdt": round(sell_quote, 4),
        "gross_pnl_usdt": round(gross, 4),
        "fees_usdt": round(fee_usdt, 4),
        "net_pnl_usdt": round(net, 4),
        "buy_fills": len(buys),
        "sell_fills": len(sells),
    }


def list_trades(limit: int = 20) -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    rows = list(csv.DictReader(open(JOURNAL_PATH, encoding="utf-8")))
    return rows[-limit:]


def _agg(rows: list[dict]) -> dict:
    """Aggregate a list of closed-trade rows into win/loss/avg-R."""
    wins = [r for r in rows if r["outcome"] == "WIN"]
    losses = [r for r in rows if r["outcome"] == "LOSS"]
    bes = [r for r in rows if r["outcome"] == "BE"]
    decisive = len(wins) + len(losses)
    pnl = sum(float(r.get("pnl_usdt") or 0) for r in rows)
    # avg R = total P&L USDT divided by total risk USDT
    total_risk = sum(float(r.get("risk_usdt") or 0) for r in rows)
    avg_r = pnl / total_risk if total_risk > 0 else 0
    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "be": len(bes),
        "win_rate_pct": round(len(wins) / decisive * 100, 1) if decisive else 0,
        "total_pnl_usdt": round(pnl, 2),
        "avg_r": round(avg_r, 2),
    }


def stats_breakdown() -> dict:
    """Per-setup, per-symbol, per-hour breakdowns. Helps surface personal edge."""
    rows = list_trades(10000)
    closed = [r for r in rows if r["outcome"] in ("WIN", "LOSS", "BE")]
    if not closed:
        return {"closed": 0, "note": "No closed trades yet"}

    by_setup: dict[str, list[dict]] = {}
    by_symbol: dict[str, list[dict]] = {}
    by_hour: dict[int, list[dict]] = {}
    by_score: dict[str, list[dict]] = {}
    by_dow: dict[str, list[dict]] = {}

    DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for r in closed:
        by_setup.setdefault(r.get("setup_type") or "?", []).append(r)
        by_symbol.setdefault(r.get("symbol") or "?", []).append(r)
        score = r.get("confluence_score") or "?"
        by_score.setdefault(str(score), []).append(r)
        try:
            ts = r["timestamp"]
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
            if dt:
                by_hour.setdefault(dt.hour, []).append(r)
                by_dow.setdefault(DOW[dt.weekday()], []).append(r)
        except Exception:
            pass

    return {
        "closed": len(closed),
        "overall": _agg(closed),
        "by_setup": {k: _agg(v) for k, v in by_setup.items()},
        "by_symbol": {k: _agg(v) for k, v in by_symbol.items()},
        "by_score": {k: _agg(v) for k, v in by_score.items()},
        "by_hour_utc": {str(k): _agg(v) for k, v in sorted(by_hour.items())},
        "by_day_of_week": {k: _agg(v) for k, v in by_dow.items()},
    }


def stats() -> dict:
    rows = list_trades(10000)
    closed = [r for r in rows if r["outcome"] in ("WIN", "LOSS", "BE")]
    if not closed:
        return {"total": len(rows), "open": len([r for r in rows if r["outcome"] == "OPEN"]), "closed": 0}

    wins = [r for r in closed if r["outcome"] == "WIN"]
    losses = [r for r in closed if r["outcome"] == "LOSS"]
    bes = [r for r in closed if r["outcome"] == "BE"]
    pnl = sum(float(r["pnl_usdt"] or 0) for r in closed)
    decisive = len(wins) + len(losses)
    return {
        "total_trades": len(rows),
        "open": len([r for r in rows if r["outcome"] == "OPEN"]),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(bes),
        "win_rate_pct": round(len(wins) / decisive * 100, 1) if decisive else 0,
        "total_pnl_usdt": round(pnl, 2),
        "avg_pnl_per_trade": round(pnl / len(closed), 2) if closed else 0,
    }
