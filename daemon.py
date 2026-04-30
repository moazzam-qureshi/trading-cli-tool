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
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

import analysis
import notify
import whale_flow
import claude_agent
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
            self._handle_close(client, closed_sym, positions_state[closed_sym], state)
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
                    # Adverse flip on an open long → ask agent to evaluate early-exit
                    if cur == "Bearish":
                        claude_agent.enqueue_event(state, {
                            "symbol": sym,
                            "trigger": f"adverse_structure_{ps['last_structure']}_to_{cur}",
                            "type": "position_review",
                            "current_price": price,
                            "direction": "long",
                        })
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

    def _handle_close(self, client: Client, sym: str, ps: dict, state: dict) -> None:
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
                # Prefer real net P&L from fills (includes fees)
                try:
                    net = jrnl.compute_net_pnl(client, sym, row["timestamp"],
                                                buy_order_id=row.get("buy_order_id", ""),
                                                oco_list_id=row.get("oco_list_id", ""))
                    pnl_net = net["net_pnl_usdt"]
                    cost_basis = net["buy_quote_usdt"] or (entry * qty)
                    pnl_pct_net = pnl_net / cost_basis * 100 if cost_basis else pnl_pct
                except Exception as e:
                    log.warning(f"compute_net_pnl failed for {sym}: {e}")
                    pnl_net, pnl_pct_net = pnl, pnl_pct
                jrnl.close_trade(row["trade_id"], outcome, exit_p, round(pnl_net, 4), round(pnl_pct_net, 2),
                                 lesson="(auto-closed by daemon — fill in manually)")
                pnl, pnl_pct = pnl_net, pnl_pct_net  # use net for the Discord post
                notify.journal_post(
                    trade_id=row["trade_id"], symbol=sym, outcome=outcome,
                    exit_price=exit_p, pnl=pnl, pnl_pct=pnl_pct,
                    setup=row.get("setup_type", "?"),
                    reasoning=row.get("reasoning", ""),
                    lesson="Auto-closed. Review in trade_notes/ and edit to capture what you learned.",
                )
            log.info(f"{sym} closed @ {exit_p} → {outcome} (P&L ${pnl:+.2f})")
            # Feed the agent's daily breaker
            try:
                claude_agent.record_trade_closed(state, sym, outcome, float(pnl))
            except Exception as e:
                log.warning(f"agent record_trade_closed failed for {sym}: {e}")
        except Exception as e:
            log.error(f"Close detection for {sym} failed: {e}")


# ──────────────────────────────────────────────────────────────────
# PartialTPJob — execute partial profit + move stop to BE when 1R hit
# ──────────────────────────────────────────────────────────────────

class PartialTPJob(Job):
    """Watches state.partial_tp_intents. When current price reaches the partial
    trigger, cancels OCO, sells partial_pct%, re-places OCO with stop at BE."""
    name = "partial_tp"

    def __init__(self):
        super().__init__()
        self.interval = int(os.getenv("DAEMON_PARTIAL_TP_INTERVAL", 30))

    def run(self, ctx: dict) -> None:
        client: Client = ctx["client"]
        state: dict = ctx["state"]
        intents: dict = state.setdefault("partial_tp_intents", {})
        if not intents:
            return

        from decimal import Decimal, ROUND_DOWN

        for sym in list(intents.keys()):
            intent = intents[sym]
            if intent.get("executed"):
                continue

            open_orders = client.get_open_orders(symbol=sym)
            if not open_orders:
                # position is closed already (TP/stop hit before partial trigger)
                log.info(f"PartialTP: {sym} no longer has open orders — clearing intent")
                intents.pop(sym, None)
                continue

            try:
                price = float(client.get_symbol_ticker(symbol=sym)["price"])
            except Exception as e:
                log.warning(f"PartialTP: price fetch failed for {sym}: {e}")
                continue

            trigger = float(intent["partial_price"])
            entry = float(intent["entry"])
            is_long = entry < trigger  # we only support spot longs
            hit = price >= trigger if is_long else price <= trigger
            if not hit:
                continue

            log.info(f"PartialTP: {sym} hit partial trigger {trigger} (price {price})")
            try:
                self._execute(client, sym, intent)
                intent["executed"] = True
                intents[sym] = intent
                save_state(state)
                notify.send("signals",
                            content=f"💰 **{sym}** partial TP @ ${price:.4f} — {intent['partial_pct']}% closed, "
                                    f"stop moved to break-even (${entry:.4f}).")
            except Exception as e:
                log.error(f"PartialTP execution failed for {sym}: {e}\n{traceback.format_exc()}")
                notify.system_alert("ERROR", f"Partial TP failed: {sym}", str(e)[:800])

        save_state(state)

    @staticmethod
    def _execute(client: Client, sym: str, intent: dict) -> None:
        from decimal import Decimal, ROUND_DOWN

        info = client.get_symbol_info(sym)
        filt = {f["filterType"]: f for f in info["filters"]}
        lot = Decimal(filt["LOT_SIZE"]["stepSize"])
        tick = Decimal(filt["PRICE_FILTER"]["tickSize"])

        def round_step(value: Decimal, step: Decimal) -> Decimal:
            if step == 0:
                return value
            return (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step

        def fmt(v: Decimal, step: Decimal) -> str:
            decimals = max(0, -step.as_tuple().exponent)
            return f"{v:.{decimals}f}"

        entry = Decimal(str(intent["entry"]))
        target = Decimal(str(intent["target"]))
        partial_pct = Decimal(str(intent["partial_pct"]))

        # 1) Cancel current OCO (both legs)
        for o in client.get_open_orders(symbol=sym):
            try:
                client.cancel_order(symbol=sym, orderId=o["orderId"])
            except Exception:
                pass

        # 2) Determine free balance now
        base = sym.replace("USDT", "").replace("BUSD", "")
        bal = client.get_asset_balance(asset=base)
        free_qty = round_step(Decimal(bal["free"]), lot)
        if free_qty <= 0:
            raise RuntimeError(f"No free {base} after cancel")

        partial_qty = round_step(free_qty * partial_pct / Decimal("100"), lot)
        remaining = round_step(free_qty - partial_qty, lot)

        # 3) Market sell the partial portion
        if partial_qty > 0:
            client.order_market_sell(symbol=sym, quantity=fmt(partial_qty, lot))

        # 4) Re-place OCO on remainder with stop at BE (entry) and original target
        if remaining > 0:
            be_stop = round_step(entry, tick)
            be_stop_limit = round_step(be_stop * (Decimal("1") - Decimal("0.005")), tick)  # 0.5% slip
            tp = round_step(target, tick)
            params = {
                "symbol": sym,
                "side": "SELL",
                "quantity": fmt(remaining, lot),
                "aboveType": "LIMIT_MAKER",
                "abovePrice": fmt(tp, tick),
                "belowType": "STOP_LOSS_LIMIT",
                "belowStopPrice": fmt(be_stop, tick),
                "belowPrice": fmt(be_stop_limit, tick),
                "belowTimeInForce": "GTC",
            }
            client._post("orderList/oco", True, data=params)


# ──────────────────────────────────────────────────────────────────
# SetupScannerJob — periodic A+ setup scans
# ──────────────────────────────────────────────────────────────────

PRIMARY_SYMBOLS = {"BTCUSDT", "SOLUSDT", "BNBUSDT"}  # validated edge in 6mo backtest
TARGET_RR = float(os.getenv("DAEMON_TARGET_RR", 1.5))
PRIMARY_MIN_SCORE = int(os.getenv("DAEMON_PRIMARY_MIN_SCORE", 9))   # tradeable bar
# Non-primary symbols (not in our 6mo validation set) need to clear a higher bar
# AND have all 3 timeframes structurally aligned. Bumped from 10→11 after MASK
# (score 12 with mtf_trend "Bearish" → stopped out as expected, but the mixed
# TF signal made it lower-confidence than the score implied).
SECONDARY_MIN_SCORE = int(os.getenv("DAEMON_SECONDARY_MIN_SCORE", 11))
SECONDARY_REQUIRE_TF_ALIGN = os.getenv("DAEMON_SECONDARY_REQUIRE_TF_ALIGN", "true").lower() == "true"

# Pro-trader filters applied at level-suggestion time. Tunable via env so we can
# disable in-flight if backtest results disagree.
#
# Backtest results (4 OOS symbols, 1500 bars × 15m, score≥9, RR=1.5):
#   baseline       avgR=+0.148  171 trades  WR=44.4%
#   ceiling@160    avgR=+0.182  185 trades  WR=46.5%   ← shipped default
#   OTE@0.62       avgR=+0.122  127 trades  WR=44.9%   ← regression, off by default
#   OTE+ceiling    avgR=+0.115  130 trades  WR=44.6%   ← OTE dominates and hurts
OTE_TOP = float(os.getenv("DAEMON_OTE_TOP", 0.62))                  # SMC-orthodox; only used if enabled
CEILING_LOOKBACK = int(os.getenv("DAEMON_CEILING_LOOKBACK", 160))   # 160 × 1H ≈ 6.7 days
ENABLE_OTE_FILTER = os.getenv("DAEMON_ENABLE_OTE", "false").lower() == "true"      # off by default
ENABLE_CEILING_FILTER = os.getenv("DAEMON_ENABLE_CEILING", "true").lower() == "true"


def _tf_aligned(r: dict) -> bool:
    """All 3 TFs structurally agree with the trade direction."""
    direction = r.get("direction")
    if not direction:
        return False
    target = "Bullish" if direction == "long" else "Bearish"
    return r.get("htf_trend") == target and r.get("mtf_trend") == target and r.get("ltf_trend") == target


def _suggest_levels(client: Client, symbol: str, direction: str, current_price: float) -> Optional[dict]:
    """Suggest entry/stop/target from current LTF structure. Stop = recent opposing swing.

    Applies pro-trader filters before returning levels:
      - OTE late-entry rejection (price must have retraced ≥62% of impulse leg)
      - Target reachability (1.5R target must sit at or below recent MTF high)

    Returns None if any filter rejects — caller treats as "no tradeable setup."
    """
    try:
        ltf_df = analysis.fetch_klines(client, symbol, "15m", 200)
        mtf_df = analysis.fetch_klines(client, symbol, "1h", 300)
    except Exception:
        return None
    swings = analysis.detect_swings(ltf_df)
    atr_v = float(analysis.atr(ltf_df).iloc[-1])
    entry = current_price
    stop = None
    if direction == "long":
        for s in reversed(swings):
            if s.kind in ("HL", "LL", "L") and s.price < entry:
                stop = s.price * 0.999  # 0.1% buffer
                break
        if stop is None:
            stop = entry - 1.5 * atr_v
        if stop >= entry:
            return None
        target = entry + TARGET_RR * (entry - stop)
    else:
        for s in reversed(swings):
            if s.kind in ("HH", "LH", "H") and s.price > entry:
                stop = s.price * 1.001
                break
        if stop is None:
            stop = entry + 1.5 * atr_v
        if stop <= entry:
            return None
        target = entry - TARGET_RR * (stop - entry)

    if abs(entry - stop) / entry < 0.001:
        return None  # noise stop

    # OTE late-entry filter — uses MTF impulse leg
    ote = None
    if ENABLE_OTE_FILTER:
        mtf_swings = analysis.detect_swings(mtf_df)
        mtf_sweep = analysis.detect_sweep(mtf_df, mtf_swings)
        ote = analysis.ote_check(direction, mtf_df, mtf_swings, mtf_sweep, entry,
                                 ote_top=OTE_TOP)
        if ote.get("valid") is False:
            log.info(f"{symbol}: rejected by OTE filter ({ote.get('reason')})")
            return None

    # Target reachability filter
    tr = None
    if ENABLE_CEILING_FILTER:
        tr = analysis.target_reachable(direction, entry, target, mtf_df,
                                       lookback=CEILING_LOOKBACK)
        if not tr["reachable"]:
            log.info(f"{symbol}: rejected by ceiling filter ({tr.get('reason')})")
            return None

    return {"entry": entry, "stop": stop, "target": target, "rr": TARGET_RR,
            "ote": ote, "ceiling": tr}


class SetupScannerJob(Job):
    name = "setup_scanner"

    def __init__(self):
        super().__init__()
        self.interval = int(os.getenv("DAEMON_SCANNER_INTERVAL", 1800))
        self.top = int(os.getenv("DAEMON_SCAN_TOP", 30))
        # Lowest score we BOTHER to score-fully (saves API). The actual alert filter
        # is applied per-symbol via PRIMARY/SECONDARY_MIN_SCORE.
        self.scan_floor = int(os.getenv("DAEMON_SCAN_FLOOR", 8))

    def run(self, ctx: dict) -> None:
        client: Client = ctx["client"]
        state: dict = ctx["state"]
        seen = state.setdefault("scanner_seen", {})

        log.info(f"Scanner running — top {self.top}, primary≥{PRIMARY_MIN_SCORE}, "
                 f"others≥{SECONDARY_MIN_SCORE}, RR target {TARGET_RR}")
        tickers = client.get_ticker()
        candidates = sorted(
            [t for t in tickers if t["symbol"].endswith("USDT")],
            key=lambda t: float(t["quoteVolume"]),
            reverse=True,
        )[:self.top]
        # Always include primary symbols even if outside top-N
        symbols = {t["symbol"] for t in candidates} | PRIMARY_SYMBOLS

        tradeable: list[dict] = []
        watching: list[dict] = []
        for sym in symbols:
            try:
                r = analysis.confluence_score(client, sym)
            except Exception as e:
                log.warning(f"Scan {sym} failed: {e}")
                continue
            if r["score"] < self.scan_floor:
                continue
            is_primary = sym in PRIMARY_SYMBOLS
            min_for_alert = PRIMARY_MIN_SCORE if is_primary else SECONDARY_MIN_SCORE
            r["primary"] = is_primary
            r["tf_aligned"] = _tf_aligned(r)
            if r["score"] >= min_for_alert:
                # Non-primary symbols additionally require all 3 TFs aligned (HTF=MTF=LTF
                # in trade direction). Catches the MASK case: score 12 with MTF Bearish.
                if not is_primary and SECONDARY_REQUIRE_TF_ALIGN and not r["tf_aligned"]:
                    log.info(f"{sym}: score {r['score']} but TFs not aligned "
                             f"(htf={r.get('htf_trend')} mtf={r.get('mtf_trend')} "
                             f"ltf={r.get('ltf_trend')}) — demoting to watching")
                    watching.append(r)
                else:
                    tradeable.append(r)
            else:
                watching.append(r)

        # Re-alert each symbol at most every 4 hours
        now = time.time()
        fresh_tradeable = []
        for r in tradeable:
            sym = r["symbol"]
            if now - seen.get(sym, 0) >= 4 * 3600:
                fresh_tradeable.append(r)
                seen[sym] = now

        log.info(f"Scanner: tradeable={len(tradeable)} (fresh={len(fresh_tradeable)}), "
                 f"watching={len(watching)}")

        # Send rich per-symbol alerts for fresh tradeable setups
        for r in fresh_tradeable:
            sym = r["symbol"]
            levels = _suggest_levels(client, sym, r["direction"], r["current_price"])
            if not levels:
                log.info(f"{sym}: skipping alert (couldn't compute levels)")
                continue
            try:
                notify.setup_alert(
                    symbol=sym, score=r["score"], direction=r["direction"],
                    current_price=r["current_price"],
                    suggested_entry=levels["entry"], suggested_stop=levels["stop"],
                    suggested_target=levels["target"], rr=levels["rr"],
                    primary=r["primary"], reasons=r.get("reasons", []),
                    tf_aligned=r.get("tf_aligned", False),
                )
            except Exception as e:
                log.error(f"setup_alert failed for {sym}: {e}")

            # Hand fresh tradeable setups to the autonomous agent for 3-layer evaluation
            if r["direction"] == "long":  # spot-only — agent rejects non-longs anyway, but skip the noise
                claude_agent.enqueue_event(state, {
                    "symbol": sym,
                    "trigger": f"setup_scan_score_{r['score']}",
                    "type": "setup",
                    "current_price": r["current_price"],
                    "direction": r["direction"],
                })

        # Compact roll-up of "watching" list if non-empty (informational)
        if watching:
            watching.sort(key=lambda r: r["score"], reverse=True)
            lines = [f"**Watching ({utcnow().strftime('%H:%M UTC')}):**"]
            for r in watching[:8]:
                arrow = "🟢" if r["direction"] == "long" else "🔴"
                tag = " ⭐" if r["primary"] else ""
                lines.append(f"{arrow} {r['symbol']}{tag} — {r['score']}/10 {r['direction']} @ ${r['current_price']}")
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
        now = utcnow()
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

class WhaleWatchJob(Job):
    """Scans top USDT pairs periodically; alerts on whale-flow triggers via #whale channel.

    Dedupes: a given (symbol, trigger) pair will not re-alert within DEDUPE_MINUTES.
    """
    name = "whale_watch"

    DEDUPE_MINUTES = 60
    THRESHOLDS = {
        "funding_deeply": True,           # funding interp starts with 'deeply_'
        "oi_strongly": True,               # oi interp contains 'strongly'
        "cvd_strong": True,                # cvd interp starts with 'strong_'
        "large_net_usdt": 250_000,
    }

    def __init__(self):
        super().__init__()
        self.interval = int(os.getenv("DAEMON_WHALE_INTERVAL", 1800))  # 30min default
        self.top_n = int(os.getenv("DAEMON_WHALE_TOP_N", 20))

    def _trigger_fields(self, flow: dict) -> tuple[list[dict], list[str]]:
        fields, triggers = [], []

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

        cvd = flow.get("spot_cvd_4h")
        if cvd:
            fields.append({"name": "Spot CVD 4h", "value": f"{cvd['cvd_pct_of_total']:+.2f}% ({cvd['interpretation']})", "inline": True})
            if "strong" in cvd["interpretation"]:
                triggers.append(f"cvd_{cvd['interpretation']}")

        lt = flow.get("large_trades_1h")
        if lt and lt["total_large_trades"] > 0:
            fields.append({"name": "Large trades 1h", "value": f"{lt['total_large_trades']} trades, net ${lt['net_notional_usdt']:+,.0f}", "inline": False})
            if abs(lt["net_notional_usdt"]) >= self.THRESHOLDS["large_net_usdt"]:
                triggers.append(f"large_net_{'buy' if lt['net_notional_usdt']>0 else 'sell'}")

        return fields, triggers

    def run(self, ctx: dict) -> None:
        client: Client = ctx["client"]
        state: dict = ctx["state"]
        whale_seen = state.setdefault("whale_seen", {})  # key: "SYMBOL|trigger" -> ts
        now = time.time()
        dedupe_window = self.DEDUPE_MINUTES * 60

        # Pick top-volume USDT pairs
        try:
            tickers = client.get_ticker()
        except Exception as e:
            log.warning(f"whale_watch: ticker fetch failed: {e}")
            return
        usdts = [t for t in tickers if t["symbol"].endswith("USDT") and not t["symbol"].endswith("UPUSDT") and not t["symbol"].endswith("DOWNUSDT")]
        usdts.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
        symbols = [t["symbol"] for t in usdts[: self.top_n]]

        alerts_sent = 0
        for sym in symbols:
            try:
                flow = whale_flow.whale_flow_summary(client, sym)
                fields, triggers = self._trigger_fields(flow)
                # filter dedupe
                fresh = [t for t in triggers if (now - whale_seen.get(f"{sym}|{t}", 0)) > dedupe_window]
                if not fresh:
                    continue
                color = "green" if any("buy" in t or "accumulation" in t for t in fresh) else \
                        "red" if any("sell" in t or "distribution" in t for t in fresh) else "purple"
                if notify.whale_event(sym, ", ".join(fresh), fields, color=color):
                    alerts_sent += 1
                    for t in fresh:
                        whale_seen[f"{sym}|{t}"] = now
                    try:
                        current_price = float(client.get_symbol_ticker(symbol=sym)["price"])
                    except Exception:
                        current_price = None

                    has_open_position = sym in state.get("positions", {})

                    if has_open_position:
                        # Whale alert on a symbol we already hold → ask agent to review
                        # (it may decide EARLY_EXIT if the signal contradicts the trade,
                        # or HOLD if it confirms / is neutral). Doctrine in CLAUDE.md.
                        claude_agent.enqueue_event(state, {
                            "symbol": sym,
                            "trigger": f"whale_on_open_position_{','.join(fresh)[:80]}",
                            "type": "position_review",
                            "current_price": current_price,
                            "direction": "long",
                            "whale_triggers": fresh,
                        })
                    elif any(("buy" in t) or ("accumulation" in t) or ("deeply_negative" in t) for t in fresh):
                        # No open position — hand long-favoring whale events to agent for new entry
                        claude_agent.enqueue_event(state, {
                            "symbol": sym,
                            "trigger": f"whale_{','.join(fresh)[:80]}",
                            "type": "whale",
                            "current_price": current_price,
                            "direction": "long",
                            "whale_triggers": fresh,
                        })
            except Exception as e:
                log.debug(f"whale_watch {sym}: {e}")

        # Trim old dedupe entries
        cutoff = now - 24 * 3600
        for k in list(whale_seen.keys()):
            if whale_seen[k] < cutoff:
                del whale_seen[k]

        save_state(state)
        log.info(f"whale_watch: scanned {len(symbols)} symbols, sent {alerts_sent} alerts")


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
                            f"Started {utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
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

    jobs: list = [
        StartupJob(),
        PositionMonitorJob(),
        PartialTPJob(),
        SetupScannerJob(),
        WhaleWatchJob(),
        DailyReportJob(),
        claude_agent.ClaudeAgentJob(),
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
