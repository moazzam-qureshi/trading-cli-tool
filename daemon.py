"""trade-cli daemon — runs multiple monitoring jobs in one process.

Designed for VPS deployment via systemd or PM2.

Jobs:
  - PositionMonitorJob: watches all open SPOT positions with OCO; alerts on
    near-stop, near-target, structure change, position close.
  - SetupScannerJob: periodically scans top-volume coins for A+ setups,
    posts findings to Discord scanner channel.
  - HeartbeatJob: regular "still alive" pings per open position.

State persisted to state.json so alerts don't repeat across restarts.
Logs to logs/daemon.log (rotated daily).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

import analysis
import notify
from binance.client import Client

load_dotenv()

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    log = logging.getLogger("daemon")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = TimedRotatingFileHandler(LOG_DIR / "daemon.log", when="midnight", backupCount=14)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


log = setup_logging()


# ──────────────────────────────────────────────────────────────────
# State persistence (so alerts survive restarts)
# ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────────────────────────

def get_client() -> Client:
    api = os.getenv("BINANCE_API_KEY")
    sec = os.getenv("BINANCE_API_SECRET")
    testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    if not api or not sec:
        raise RuntimeError("Missing BINANCE_API_KEY/SECRET in env")
    return Client(api, sec, testnet=testnet)


# ──────────────────────────────────────────────────────────────────
# Job base
# ──────────────────────────────────────────────────────────────────

class Job:
    name: str = "Job"
    interval: int = 60  # seconds

    def __init__(self):
        self.last_run: float = 0.0

    def due(self) -> bool:
        return time.time() - self.last_run >= self.interval

    def run(self, ctx: dict) -> None:  # ctx: shared state, client
        raise NotImplementedError

    def safe_run(self, ctx: dict) -> None:
        try:
            self.run(ctx)
        except Exception as e:
            log.error(f"{self.name} failed: {e}\n{traceback.format_exc()}")
            try:
                notify.system_alert("ERROR", f"{self.name} failed", str(e)[:1000])
            except Exception:
                pass
        finally:
            self.last_run = time.time()


# ──────────────────────────────────────────────────────────────────
# PositionMonitorJob — watches every open OCO-protected position
# ──────────────────────────────────────────────────────────────────

class PositionMonitorJob(Job):
    name = "position_monitor"

    def __init__(self):
        super().__init__()
        self.interval = int(os.getenv("DAEMON_POSITION_INTERVAL", 60))
        self.near_pct = float(os.getenv("DAEMON_NEAR_PCT", 0.4))

    def run(self, ctx: dict) -> None:
        client: Client = ctx["client"]
        state: dict = ctx["state"]
        positions_state = state.setdefault("positions", {})

        all_orders = client.get_open_orders()
        # Group by symbol; for OCO we expect 2 orders (stop_loss_limit + limit_maker)
        by_symbol: dict[str, list[dict]] = {}
        for o in all_orders:
            by_symbol.setdefault(o["symbol"], []).append(o)

        # 1) Detect closed positions: previously tracked symbols no longer have orders
        active_symbols = set(by_symbol.keys())
        tracked = set(positions_state.keys())
        for closed_sym in tracked - active_symbols:
            self._handle_close(client, closed_sym, positions_state[closed_sym])
            positions_state.pop(closed_sym, None)

        # 2) For each active OCO-protected position
        for sym, orders in by_symbol.items():
            stop_order = next((o for o in orders if o["type"] == "STOP_LOSS_LIMIT"), None)
            tp_order = next((o for o in orders if o["type"] in ("LIMIT_MAKER", "LIMIT")), None)
            if not stop_order or not tp_order:
                continue  # not an OCO setup — skip

            stop_price = float(stop_order["stopPrice"])
            target_price = float(tp_order["price"])

            # First time seeing this position: record entry context
            if sym not in positions_state:
                base = sym.replace("USDT", "").replace("BUSD", "")
                bal = client.get_asset_balance(asset=base)
                qty = float(bal["free"]) + float(bal["locked"])
                # Use recent fill price as entry approximation
                fills = client.get_my_trades(symbol=sym, limit=10)
                buys = [f for f in fills if f["isBuyer"]]
                entry = float(buys[-1]["price"]) if buys else float(client.get_symbol_ticker(symbol=sym)["price"])
                positions_state[sym] = {
                    "entry": entry,
                    "qty": qty,
                    "stop": stop_price,
                    "target": target_price,
                    "opened_at": time.time(),
                    "alerted_near_stop": False,
                    "alerted_near_target": False,
                    "last_structure": None,
                    "last_heartbeat": 0,
                }
                log.info(f"Now tracking {sym}: entry ${entry} stop ${stop_price} target ${target_price}")

            ps = positions_state[sym]
            price = float(client.get_symbol_ticker(symbol=sym)["price"])
            elapsed_min = (time.time() - ps["opened_at"]) / 60

            # Near-stop alert (one-shot)
            dist_stop_pct = (price - stop_price) / stop_price * 100
            if dist_stop_pct < self.near_pct and not ps["alerted_near_stop"]:
                notify.price_alert(sym, "near_stop", price, stop_price, dist_stop_pct)
                ps["alerted_near_stop"] = True
                log.info(f"{sym} near stop ({dist_stop_pct:.2f}%)")

            # Near-target alert (one-shot)
            dist_tp_pct = (target_price - price) / price * 100
            if dist_tp_pct < self.near_pct and not ps["alerted_near_target"]:
                notify.price_alert(sym, "near_target", price, target_price, dist_tp_pct)
                ps["alerted_near_target"] = True
                log.info(f"{sym} near target ({dist_tp_pct:.2f}%)")

            # Structure change alert
            try:
                df = analysis.fetch_klines(client, sym, "1h", 200)
                swings = analysis.detect_swings(df)
                summary = analysis.structure_summary(swings, price)
                cur = summary["trend"]
                if ps["last_structure"] and cur != ps["last_structure"]:
                    notify.structure_change(sym, ps["last_structure"], cur, "1h")
                    log.info(f"{sym} structure {ps['last_structure']} -> {cur}")
                ps["last_structure"] = cur
            except Exception as e:
                log.warning(f"{sym} structure check failed: {e}")

            # Heartbeat
            hb_minutes = int(os.getenv("DAEMON_HEARTBEAT_MINUTES", 30))
            if elapsed_min - ps["last_heartbeat"] >= hb_minutes:
                notify.heartbeat(sym, price, ps["entry"], stop_price, target_price,
                                 int(elapsed_min), ps.get("last_structure") or "?")
                ps["last_heartbeat"] = elapsed_min
                log.info(f"{sym} heartbeat sent")

        save_state(state)

    def _handle_close(self, client: Client, sym: str, ps: dict) -> None:
        """A position previously tracked has no more open orders — find out how it closed and journal it."""
        try:
            fills = client.get_my_trades(symbol=sym, limit=10)
            sells = [f for f in fills if not f["isBuyer"]]
            if not sells:
                notify.send("signals", content=f"⚠ {sym} orders gone but no sell trade found.")
                log.warning(f"{sym} closed but no sell record")
                return
            last_sell = sells[-1]
            exit_p = float(last_sell["price"])
            stop = ps["stop"]
            target = ps["target"]
            entry = ps["entry"]
            qty = ps["qty"]
            pnl = (exit_p - entry) * qty
            pnl_pct = (exit_p - entry) / entry * 100

            # Decide outcome
            if exit_p >= target * 0.999:
                kind = "target_hit"
                outcome = "WIN"
                notify.price_alert(sym, kind, exit_p, target, pnl_pct)
            elif exit_p <= stop * 1.005:
                kind = "stop_hit"
                outcome = "LOSS"
                notify.price_alert(sym, kind, exit_p, stop, pnl_pct)
            else:
                outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BE"
                notify.trade_closed(sym, outcome, exit_p, pnl, pnl_pct)

            # Auto-update journal CSV + post markdown post-mortem to journal channel
            import journal as jrnl
            rows = jrnl.list_trades(10000)
            row = next((r for r in rows if r["symbol"] == sym and r["outcome"] == "OPEN"), None)
            if row:
                jrnl.close_trade(row["trade_id"], outcome, exit_p, round(pnl, 4), round(pnl_pct, 2),
                                 lesson="(auto-closed by daemon — fill in manually)")
                notify.journal_post(
                    trade_id=row["trade_id"], symbol=sym, outcome=outcome,
                    exit_price=exit_p, pnl=pnl, pnl_pct=pnl_pct,
                    setup=row.get("setup_type", "?"),
                    reasoning=row.get("reasoning", ""),
                    lesson="Auto-closed. Review in trade_notes/ and edit to capture what you learned.",
                )
            log.info(f"{sym} closed @ {exit_p} → {outcome} (P&L ${pnl:+.2f})")
        except Exception as e:
            log.error(f"Close detection for {sym} failed: {e}")


# ──────────────────────────────────────────────────────────────────
# SetupScannerJob — periodic A+ setup scans
# ──────────────────────────────────────────────────────────────────

class SetupScannerJob(Job):
    name = "setup_scanner"

    def __init__(self):
        super().__init__()
        self.interval = int(os.getenv("DAEMON_SCANNER_INTERVAL", 1800))
        self.top = int(os.getenv("DAEMON_SCAN_TOP", 30))
        self.min_score = int(os.getenv("DAEMON_SCAN_MIN_SCORE", 8))

    def run(self, ctx: dict) -> None:
        client: Client = ctx["client"]
        state: dict = ctx["state"]
        seen = state.setdefault("scanner_seen", {})  # symbol -> last_alert_ts

        log.info(f"Scanner running — top {self.top} pairs, min score {self.min_score}")
        tickers = client.get_ticker()
        candidates = sorted(
            [t for t in tickers if t["symbol"].endswith("USDT")],
            key=lambda t: float(t["quoteVolume"]),
            reverse=True,
        )[:self.top]

        results = []
        for t in candidates:
            try:
                r = analysis.confluence_score(client, t["symbol"])
                if r["score"] >= self.min_score:
                    results.append(r)
            except Exception as e:
                log.warning(f"Scan {t['symbol']} failed: {e}")

        # Re-alert each symbol at most every 4 hours
        now = time.time()
        new_setups = []
        for r in results:
            sym = r["symbol"]
            last = seen.get(sym, 0)
            if now - last >= 4 * 3600:
                new_setups.append(r)
                seen[sym] = now

        log.info(f"Scanner found {len(results)} setups, {len(new_setups)} are fresh alerts")

        if new_setups:
            lines = [f"**A+ Setup Scan — {datetime.utcnow().strftime('%H:%M UTC')}**"]
            for r in new_setups[:10]:
                arrow = "🟢" if r["direction"] == "long" else "🔴"
                lines.append(f"{arrow} **{r['symbol']}** — {r['score']}/10 — {r['direction']} @ ${r['current_price']}")
                for reason in r["reasons"][:3]:
                    lines.append(f"   · {reason}")
            notify.send("scanner", content="\n".join(lines)[:1900], fallback_to="signals")

        save_state(state)


# ──────────────────────────────────────────────────────────────────
# DailyReportJob — end-of-day P&L summary
# ──────────────────────────────────────────────────────────────────

class DailyReportJob(Job):
    name = "daily_report"

    def __init__(self):
        super().__init__()
        # Check every 5 minutes — only acts if it's the configured hour and we haven't sent today
        self.interval = 300
        self.target_hour = int(os.getenv("DAEMON_DAILY_REPORT_HOUR_UTC", 0))

    def run(self, ctx: dict) -> None:
        now = datetime.utcnow()
        state: dict = ctx["state"]
        last_sent_date = state.get("last_daily_report_date")
        today_str = now.strftime("%Y-%m-%d")

        # Only run at the configured hour, and only once per day
        if now.hour != self.target_hour:
            return
        if last_sent_date == today_str:
            return

        import journal as jrnl
        stats = jrnl.stats()
        # Find trades closed today
        all_trades = jrnl.list_trades(10000)
        today_trades = []
        for r in all_trades:
            if r["outcome"] in ("WIN", "LOSS", "BE"):
                # Use timestamp prefix from trade_id (T20260428-...)
                if r["trade_id"].startswith(f"T{now.strftime('%Y%m%d')}"):
                    today_trades.append(r)

        notify.daily_report(today_str, stats, today_trades)
        state["last_daily_report_date"] = today_str
        save_state(state)
        log.info(f"Daily report sent for {today_str} — {len(today_trades)} closes")


# ──────────────────────────────────────────────────────────────────
# StartupJob — sends one "daemon online" message
# ──────────────────────────────────────────────────────────────────

class StartupJob(Job):
    name = "startup"
    interval = 10**9  # never repeats

    def run(self, ctx: dict) -> None:
        client: Client = ctx["client"]
        try:
            acc = client.get_account()
            usdt = next((b for b in acc["balances"] if b["asset"] == "USDT"), {"free": "0"})
            open_orders = len(client.get_open_orders())
        except Exception:
            usdt = {"free": "?"}
            open_orders = "?"
        notify.system_alert("INFO",
                            f"trade-cli daemon online",
                            f"Started {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
                            f"USDT: ${usdt['free']}\nOpen orders: {open_orders}")
        log.info("Daemon startup notification sent")


# ──────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────

_running = True


def _shutdown(signum, frame):
    global _running
    log.info(f"Signal {signum} received, shutting down…")
    _running = False
    notify.system_alert("INFO", "trade-cli daemon stopping", f"Signal {signum}")


def main() -> None:
    log.info("=" * 60)
    log.info("trade-cli daemon starting")
    log.info(f"Testnet: {os.getenv('BINANCE_TESTNET', 'false')}")
    log.info(f"Webhook signals: {'set' if os.getenv('DISCORD_WEBHOOK_SIGNALS') else 'NOT SET'}")
    log.info(f"Webhook scanner: {'set' if os.getenv('DISCORD_WEBHOOK_SCANNER') else 'using signals fallback'}")
    log.info("=" * 60)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    client = get_client()
    state = load_state()

    jobs: list[Job] = [
        StartupJob(),
        PositionMonitorJob(),
        SetupScannerJob(),
        DailyReportJob(),
    ]

    ctx = {"client": client, "state": state}

    # Run startup once
    jobs[0].safe_run(ctx)

    while _running:
        for job in jobs[1:]:
            if job.due():
                job.safe_run(ctx)
        time.sleep(5)

    save_state(state)
    log.info("Daemon stopped.")


if __name__ == "__main__":
    main()
