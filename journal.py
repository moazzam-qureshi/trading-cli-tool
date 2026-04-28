"""Trade journal — auto-logs every trade with reasoning + post-mortem template."""
import csv
import json
from datetime import datetime
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
    if not JOURNAL_PATH.exists():
        with open(JOURNAL_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def log_entry(data: dict) -> str:
    """Log a new trade entry. Returns trade_id."""
    _ensure_csv()
    trade_id = data.get("trade_id") or datetime.utcnow().strftime("T%Y%m%d-%H%M%S")
    row = {f: "" for f in FIELDS}
    row.update(data)
    row["trade_id"] = trade_id
    row["timestamp"] = datetime.utcnow().isoformat()
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


def list_trades(limit: int = 20) -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []
    rows = list(csv.DictReader(open(JOURNAL_PATH, encoding="utf-8")))
    return rows[-limit:]


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
