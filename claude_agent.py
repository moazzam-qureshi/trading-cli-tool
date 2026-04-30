"""Autonomous claude-code agent integration for the daemon.

Spawns `claude -p` subprocesses in response to live events (setup-scan score-9 hits,
whale-flow triggers, position-monitor adverse-signal reviews). Validates the agent's
verdict against hard-coded gates, then executes trades via `python trade.py` calls.

Tool access for the spawned claude is read-only — only chart PNGs and the prompt
context. The daemon (this module) is the only thing that ever invokes trade.py buy/sell,
*after* the verdict passes all gates.

Constraints encoded here (must match CLAUDE.md "Autonomous Agent Mode"):
  - Daily breaker: cumulative realized P&L for current UTC day ≥ -$10
  - Daily trade cap: ≤ 5 trades opened today (including re-entries)
  - No duplicate position on same symbol
  - ≤ 1 re-entry per symbol per UTC day
  - Risk = 1% of free USDT per trade
  - Spot longs only; no shorts, no leveraged tokens, no stablecoin pairs
  - R:R floor 1.5
  - Subprocess timeout 5min; SKIP on any error
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import notify
import agent_watch

log = logging.getLogger("daemon")

ROOT = Path(__file__).parent
CHARTS_DIR = ROOT / "charts"
RESULTS_DIR = ROOT / "agent_results"
RESULTS_DIR.mkdir(exist_ok=True)

# Hard limits — match CLAUDE.md "Autonomous Agent Mode"
DAILY_LOSS_LIMIT_USD = float(os.getenv("AGENT_DAILY_LOSS_LIMIT", 10.0))
DAILY_TRADE_CAP = int(os.getenv("AGENT_DAILY_TRADE_CAP", 5))
RISK_PCT_PER_TRADE = float(os.getenv("AGENT_RISK_PCT", 1.0))   # 1% of free USDT
RR_FLOOR = float(os.getenv("AGENT_RR_FLOOR", 1.5))
SUBPROCESS_TIMEOUT_SEC = int(os.getenv("AGENT_TIMEOUT_SEC", 300))
DRY_RUN = os.getenv("AGENT_DRY_RUN", "false").lower() == "true"

STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "RLUSD", "PYUSD",
               "USD1", "USDD", "USDP", "USDE", "USDS", "USD0", "USDX", "USDY",
               "EUR", "EURI", "EURS", "AEUR", "FDUSD", "GUSD", "LUSD", "USTC"}


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ──────────────────────────────────────────────────────────────────
# Daily counters (live in state.json under "agent_daily")
# ──────────────────────────────────────────────────────────────────

def _agent_daily(state: dict) -> dict:
    today = _today_utc()
    d = state.get("agent_daily")
    if not d or d.get("date") != today:
        d = {
            "date": today,
            "trades_opened": 0,
            "realized_pnl_usd": 0.0,
            "breaker_tripped": False,
            "reentries": {},          # symbol -> count
            "stopped_out_today": [],  # symbols that hit stop today (eligible for 1 re-entry)
        }
        state["agent_daily"] = d
    return d


def record_trade_opened(state: dict, symbol: str) -> None:
    d = _agent_daily(state)
    d["trades_opened"] += 1
    if symbol in d.get("stopped_out_today", []):
        d["reentries"][symbol] = d["reentries"].get(symbol, 0) + 1


def record_trade_closed(state: dict, symbol: str, outcome: str, pnl_usd: float) -> None:
    """Called by PositionMonitorJob when a position closes — feeds the breaker."""
    d = _agent_daily(state)
    d["realized_pnl_usd"] += pnl_usd
    if outcome == "LOSS" and symbol not in d["stopped_out_today"]:
        d["stopped_out_today"].append(symbol)
    if d["realized_pnl_usd"] <= -DAILY_LOSS_LIMIT_USD and not d["breaker_tripped"]:
        d["breaker_tripped"] = True
        notify.system_alert(
            "WARN", "Agent breaker tripped",
            f"Daily realized P&L ${d['realized_pnl_usd']:+.2f} ≤ -${DAILY_LOSS_LIMIT_USD}. "
            "Agent paused until 00:00 UTC."
        )
        log.warning(f"Agent breaker tripped: pnl ${d['realized_pnl_usd']:+.2f}")


# ──────────────────────────────────────────────────────────────────
# Pre-execution gates
# ──────────────────────────────────────────────────────────────────

def _is_excluded_symbol(symbol: str) -> Optional[str]:
    """Return a rejection reason if symbol is excluded, else None."""
    s = symbol.upper()
    if not s.endswith("USDT"):
        return "non-USDT pair"
    if s.endswith("UPUSDT") or s.endswith("DOWNUSDT") or s.endswith("BULLUSDT") or s.endswith("BEARUSDT"):
        return "leveraged token"
    base = s[:-4]
    if base in STABLECOINS:
        return "stablecoin pair"
    # Defensive: catch USD-pegged variants we haven't enumerated (USD1, USD2, USDx…).
    # Any base of the form USD followed by 1-2 alphanumerics is overwhelmingly a peg
    # in practice; no legitimate alt we'd trade matches this pattern.
    if len(base) <= 5 and base.startswith("USD"):
        return "stablecoin pair (USD-pegged)"
    return None


def _check_open_position(client, symbol: str) -> bool:
    try:
        return len(client.get_open_orders(symbol=symbol)) > 0
    except Exception as e:
        log.warning(f"open-orders check failed for {symbol}: {e}")
        return True  # fail-closed: assume there is one, skip this trade


def gates_for_buy(verdict: dict, state: dict, client) -> tuple[bool, str]:
    """Returns (allowed, reason). Caller skips trade if not allowed."""
    d = _agent_daily(state)

    if d["breaker_tripped"]:
        return False, f"breaker tripped today (pnl ${d['realized_pnl_usd']:+.2f})"
    if d["trades_opened"] >= DAILY_TRADE_CAP:
        return False, f"daily trade cap hit ({d['trades_opened']}/{DAILY_TRADE_CAP})"

    sym = verdict.get("symbol", "").upper()
    if not sym:
        return False, "no symbol"
    excl = _is_excluded_symbol(sym)
    if excl:
        return False, f"excluded symbol ({excl})"

    if verdict.get("direction", "").upper() != "LONG":
        return False, "non-long direction (spot can't short)"

    if _check_open_position(client, sym):
        return False, "already have open position on this symbol"

    # Re-entry check: if symbol stopped out today, allow at most 1 re-entry
    if sym in d.get("stopped_out_today", []):
        if d["reentries"].get(sym, 0) >= 1:
            return False, "already re-entered this symbol today"

    try:
        entry = float(verdict["entry"])
        stop = float(verdict["stop"])
        target = float(verdict["target"])
    except (KeyError, ValueError, TypeError) as e:
        return False, f"missing/bad levels: {e}"

    if not (stop < entry < target):
        return False, f"levels not LONG-shaped (stop {stop} entry {entry} target {target})"
    rr = (target - entry) / (entry - stop) if (entry - stop) > 0 else 0
    if rr < RR_FLOOR:
        return False, f"R:R {rr:.2f} below floor {RR_FLOOR}"

    # Pro-trader filters: late-entry (OTE) + target reachability.
    # Defense in depth — _suggest_levels already enforces these for setup_scan,
    # but whale-watch enqueues bypass that path, so re-check here.
    # Ceiling filter (target-reachability) on by default — backtest winner.
    # VSA up-thrust filter on by default — fakes are smart-money distribution.
    # OTE filter off by default — backtest didn't support it.
    enable_ceil = os.getenv("AGENT_ENABLE_CEILING", "true").lower() == "true"
    enable_vsa  = os.getenv("AGENT_ENABLE_VSA",     "false").lower() == "true"
    enable_ote  = os.getenv("AGENT_ENABLE_OTE",     "false").lower() == "true"
    if enable_ceil or enable_vsa or enable_ote:
        try:
            import analysis  # local import to avoid hard dep at module load
            mtf_df = analysis.fetch_klines(client, sym, "1h", 300)
            ltf_df = analysis.fetch_klines(client, sym, "15m", 300) if enable_vsa else None
            if enable_ote:
                mtf_swings = analysis.detect_swings(mtf_df)
                mtf_sweep = analysis.detect_sweep(mtf_df, mtf_swings)
                ote_top = float(os.getenv("AGENT_OTE_TOP", "0.62"))
                ote = analysis.ote_check("long", mtf_df, mtf_swings, mtf_sweep, entry, ote_top=ote_top)
                if ote.get("valid") is False:
                    return False, f"OTE late-entry: {ote.get('reason')}"
            if enable_ceil:
                lookback = int(os.getenv("AGENT_CEILING_LOOKBACK", "160"))
                tr = analysis.target_reachable("long", entry, target, mtf_df, lookback=lookback)
                if not tr["reachable"]:
                    return False, f"target unreachable: {tr.get('reason')}"
            if enable_vsa and ltf_df is not None:
                ltf_last = analysis.vsa_bar(ltf_df, len(ltf_df) - 1)
                if ltf_last == "up_thrust":
                    return False, "VSA up_thrust on LTF entry bar (smart-money distribution)"
                mtf_vsa = analysis.vsa_signature(mtf_df, lookback=5)
                if mtf_vsa.get("has_up_thrust"):
                    return False, "VSA up_thrust detected on recent MTF bars"
        except Exception as e:
            # If filter check itself fails, fail-open (don't block on data fetch failure)
            log.warning(f"agent filter check failed for {sym}: {e}")

    # ── Macro window gate ──────────────────────────────────────────
    try:
        import macro
        in_win, ev = macro.in_macro_window()
        if in_win and ev:
            ev_dt = ev["_dt_utc"].strftime("%H:%M UTC")
            return False, f"macro window: {ev.get('title','?')} @ {ev_dt} (±{macro.WINDOW_BEFORE_MIN}/{macro.WINDOW_AFTER_MIN}min)"
    except Exception as e:
        log.warning(f"macro window check failed: {e}")  # fail-open

    # ── Session-quality gate (soft: raise score bar in thin hours) ──
    try:
        import sessions
        quality = sessions.current_quality()
        if quality == "thin":
            need = sessions.required_min_score()
            score = int(verdict.get("score", 0) or 0)
            if score < need:
                return False, f"thin session — score {score} < required {need} during {quality} hours"
    except Exception as e:
        log.warning(f"session check failed: {e}")  # fail-open

    # ── Portfolio correlation cap ──────────────────────────────────
    try:
        import risk
        free, held = _account_components(client)
        account_value = free + held
        # Rough prospective notional: agent sizes at 1% risk; trade notional ≈ risk_usd × entry/(entry-stop).
        risk_usd = account_value * 0.01
        per_unit_risk = max(entry - stop, entry * 0.001)
        prospective = (risk_usd / per_unit_risk) * entry if per_unit_risk > 0 else 30.0
        prospective = max(10.0, min(prospective, account_value))
        ok, info = risk.concurrent_exposure_check(client, account_value=account_value, prospective_notional=prospective)
        if not ok:
            return False, (f"correlation cap: held ${info['current_held_usdt']} + new "
                           f"${info['prospective_notional']} > {info['cap_pct']}% of ${info['account_value']}")
    except Exception as e:
        log.warning(f"correlation check failed: {e}")  # fail-open

    return True, "ok"


def _account_components(client) -> tuple[float, float]:
    """Returns (free_usdt, held_usdt_equiv). Used for account_value = sum."""
    try:
        acc = client.get_account()
        free = 0.0
        held = 0.0
        for b in acc["balances"]:
            asset = b["asset"]
            qty = float(b["free"]) + float(b["locked"])
            if qty <= 0:
                continue
            if asset == "USDT":
                free = float(b["free"])
                continue
            if asset in ("BUSD", "USDC", "FDUSD"):
                continue
            try:
                px = float(client.get_symbol_ticker(symbol=f"{asset}USDT")["price"])
                held += qty * px
            except Exception:
                continue
        return free, held
    except Exception:
        return 0.0, 0.0


# ──────────────────────────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────────────────────────

PROMPT_HEADER = """You are the trading-agent for the trade-cli system. The CLAUDE.md in this repo
is your operating manual — assume the rules and 3-layer filter described there.

Your job: given the event below, evaluate using the 3-layer framework and output
a STRICTLY-STRUCTURED JSON verdict in a fenced ```json``` block at the end of
your response. The daemon parses this JSON and only acts if all hard gates pass.

Be concise — one paragraph of reasoning per layer is plenty.

Required JSON schema:
{
  "decision": "BUY" | "SKIP" | "EARLY_EXIT" | "WATCH",
  "symbol": "...USDT",
  "direction": "LONG" | "SHORT",
  "score": <int 0-15>,
  "layer1_pass": <bool>,
  "layer2_pass": <bool>,
  "layer3_pass": <bool>,
  "entry": <float>,
  "stop": <float>,
  "target": <float>,
  "rr": <float>,
  "reasoning": "<≤500 chars why this verdict>",
  "primary_concern": "<≤200 chars what could invalidate this>",
  "watch_price_lte": <float|null>,
  "watch_price_gte": <float|null>,
  "watch_structure_flip": "Bullish" | "Bearish" | null,
  "watch_cvd_signal": "strong_distribution" | "net_distribution" | "strong_accumulation" | "net_accumulation" | null,
  "watch_sweep_printed": "bullish" | "bearish" | null,
  "watch_action": "reeval" | "notify",
  "watch_expires_hours": <float, default 12, max 24>
}

For SKIP, EARLY_EXIT, and WATCH, entry/stop/target/rr can be 0. Direction MUST be
"LONG" for any BUY or WATCH (spot-only — no shorts).

Decision rules:
- BUY only if all three layers pass AND R:R ≥ 1.5 AND clean LTF structure.
- SKIP if the setup is fundamentally broken (wrong direction, distribution, no edge).
- EARLY_EXIT only used on position-review events where the open trade's setup is invalidated.
- WATCH when the situation is "right thesis, wrong moment" — Layers 1-2 strong but
  Layer 3 visual rejects, OR the setup needs *something to happen* (pullback,
  structure flip, distribution flush, fresh sweep) before it becomes tradeable.
  The daemon polls watches and re-runs a fresh 3-layer eval (or just notifies)
  when ANY condition fires. Use INSTEAD of SKIP whenever a pro trader would
  set an alert and walk away. Conditions are OR'd — one match triggers.

  **Conditions** (set one or more, leave others null):
    * `watch_price_lte` — fires when current price ≤ this level. Use for "buy the
      pullback" — set to the prior sweep low, 1h EMA, or order-block top.
    * `watch_price_gte` — fires when current price ≥ this. Use for breakout
      confirmation — set above a key resistance shelf.
    * `watch_structure_flip` — fires when 1h structure summary becomes Bullish or
      Bearish. Use when waiting for HTF alignment (e.g. setup is good but 1h is
      still bearish — set "Bullish" to alert when MTF flips with us).
    * `watch_cvd_signal` — fires when 4h CVD interpretation hits this. Use for
      flow-driven re-entry: e.g. setup needs whales to start accumulating
      ("net_accumulation"), or to wait out distribution ("strong_distribution"
      → notify so you can re-eval after the flush).
    * `watch_sweep_printed` — fires when a fresh 15m bullish/bearish sweep
      prints. Use when waiting for the trigger candle on an otherwise-aligned setup.

  **Action mode:**
    * `watch_action: "reeval"` (default) — daemon re-runs full 3-layer agent eval
      when condition fires. Use when you want to actually trade if conditions
      still hold at trigger time.
    * `watch_action: "notify"` — daemon posts a Discord ping only, no re-eval.
      Use for thesis-decay alerts on positions you already hold, or when the
      condition is just informational and doesn't warrant a fresh agent burn.
"""


def build_prompt(event: dict, chart_paths: dict, confluence_text: str) -> str:
    """Assemble the prompt the daemon hands to claude -p."""
    symbol = event["symbol"]
    trigger = event.get("trigger", "?")
    price = event.get("current_price", "?")

    chart_lines = []
    for tf, path in chart_paths.items():
        chart_lines.append(f"  - {tf}: {path}")

    whale_lines = ""
    if event.get("whale_triggers"):
        whale_lines = "\n- Whale triggers: " + ", ".join(event["whale_triggers"])

    watch_lines = ""
    if event.get("type") == "watch_triggered":
        watch_lines = (
            f"\n\n## Re-eval context (this is a previously-set agent watch firing)"
            f"\n- Original thesis (from prior eval): {event.get('original_thesis','')[:500]}"
            f"\n- Original trigger: {event.get('original_trigger','')}"
            f"\n- Watch fired by: {event.get('triggered_by','?')} = {event.get('triggered_value','?')} (price now ${event.get('triggered_price','?')})"
            "\n- The prior thesis is *context*, not a green light. Run all 3 layers fresh — conditions may have decayed since the watch was set. Reject if the setup no longer holds."
        )

    return PROMPT_HEADER + f"""

## Event
- Symbol: {symbol}
- Trigger: {trigger}
- Current price: ${price}
- Type: {event.get("type", "setup")}{whale_lines}{watch_lines}

## Layer 1 + Layer 2 (`confluence --whale` already-run output)
```
{confluence_text}
```

## Recent trade history (use the Read tool to consult before deciding)
- Journal CSV: `/app/trades.csv` — read the LAST 10-15 closed trades. Look for patterns: which setups are losing? counter-MTF? specific symbols? thin-session entries? Note any 3-in-a-row losses on a setup type — if so, raise the bar on similar setups today.
- Quick stats: `journal stats` (already in pipeline output above when relevant).
- This is your post-mortem feedback loop — pro traders read their own journal before each entry. Skim it briefly; do not stuff it into your reasoning. If you see a relevant lesson, mention it in `primary_concern`.

## Layer 3 — chart PNGs to read with the Read tool

You MUST read each of these 5 charts before deciding. They live in `/app/charts/` inside this container:
{chr(10).join(chart_lines)}

Use the Read tool on each PNG. After reading all 5, integrate:
- 1d / 4h: HTF context, distribution wicks, support/resistance shelves
- 1h: MTF structure — higher highs/lows, sweep status, EMA20/50 location
- 15m / 5m: LTF structure — confirmation candle, recent sweep, volume

Then output the JSON verdict. The reasoning field should reference what you saw on the charts, not generic SMC theory.
"""


# ──────────────────────────────────────────────────────────────────
# Subprocess + decision parsing
# ──────────────────────────────────────────────────────────────────

JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def parse_decision(claude_output_text: str) -> Optional[dict]:
    """Extract the JSON verdict from claude's output. Returns None if malformed."""
    m = JSON_BLOCK_RE.search(claude_output_text)
    if not m:
        # Fallback: greedy first {...} block
        start = claude_output_text.find("{")
        end = claude_output_text.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = claude_output_text[start:end + 1]
    else:
        candidate = m.group(1)
    try:
        d = json.loads(candidate)
        if "decision" not in d:
            return None
        return d
    except json.JSONDecodeError:
        return None


def spawn_claude(prompt: str, event_id: str) -> subprocess.Popen:
    """Start a claude -p subprocess. Caller polls .poll() and reads stdout when done."""
    out_path = RESULTS_DIR / f"{event_id}.out"
    err_path = RESULTS_DIR / f"{event_id}.err"
    prompt_path = RESULTS_DIR / f"{event_id}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--allowed-tools", "Read",
    ]
    log.info(f"agent: spawning claude for {event_id}")
    proc = subprocess.Popen(
        cmd,
        stdout=open(out_path, "wb"),
        stderr=open(err_path, "wb"),
        cwd=str(ROOT),
    )
    return proc


def read_subprocess_output(event_id: str) -> tuple[Optional[str], Optional[str]]:
    """After subprocess exits, read stdout text. Returns (assistant_text, raw_json)."""
    out_path = RESULTS_DIR / f"{event_id}.out"
    if not out_path.exists():
        return None, None
    raw = out_path.read_text(encoding="utf-8", errors="replace")
    # claude --output-format json wraps the assistant output in {"result": "...", ...}
    try:
        envelope = json.loads(raw)
        return envelope.get("result", ""), raw
    except json.JSONDecodeError:
        # fallback: treat raw as plain assistant text
        return raw, raw


# ──────────────────────────────────────────────────────────────────
# Trade execution
# ──────────────────────────────────────────────────────────────────

def _free_usdt(client) -> float:
    try:
        bal = client.get_asset_balance(asset="USDT")
        return float(bal["free"])
    except Exception as e:
        log.warning(f"free USDT fetch failed: {e}")
        return 0.0


def execute_buy(verdict: dict, client, state: dict) -> tuple[bool, str]:
    """Run trade.py buy with the agent's levels. Returns (success, message)."""
    sym = verdict["symbol"].upper()
    entry = float(verdict["entry"])
    stop = float(verdict["stop"])
    target = float(verdict["target"])

    free = _free_usdt(client)

    # ── ATR-aware sizing: scale risk% down in choppy/blow-off vol regimes ──
    try:
        import risk as risk_mod
        vol_mult, vol_info = risk_mod.vol_sizing_multiplier(client, sym)
    except Exception as e:
        log.warning(f"agent: vol-sizing lookup failed for {sym}: {e}")
        vol_mult, vol_info = 1.0, {"error": str(e)[:80]}

    effective_risk_pct = RISK_PCT_PER_TRADE * vol_mult
    risk_usd = free * effective_risk_pct / 100.0
    if vol_mult < 1.0:
        log.info(f"agent: vol-sizing {sym} mult={vol_mult} regime={vol_info.get('regime')} "
                 f"atr%={vol_info.get('atr_pct_of_price')} → risk%={effective_risk_pct}")
    if risk_usd <= 0.5:
        return False, f"risk_usd ${risk_usd:.2f} too small (free USDT ${free:.2f}, vol_mult={vol_mult})"

    # USD to buy = risk_usd / (stop_distance%)
    stop_dist_pct = (entry - stop) / entry
    if stop_dist_pct <= 0:
        return False, f"bad stop distance {stop_dist_pct}"
    usd_to_buy = round(risk_usd / stop_dist_pct, 2)
    if usd_to_buy < 6.0:
        return False, f"position size ${usd_to_buy} below Binance min notional"

    reason = f"AGENT: {verdict.get('reasoning', '')[:300]}"

    if DRY_RUN:
        log.info(f"agent DRY-RUN: would buy {sym} ${usd_to_buy} stop {stop} target {target}")
        notify.send("signals",
                    content=f"🤖 [DRY-RUN] Agent would BUY **{sym}**: ${usd_to_buy:.2f} stop ${stop} target ${target}\n"
                            f"Reason: {verdict.get('reasoning', '')[:300]}")
        return True, "dry-run logged"

    cmd = [
        "python", "trade.py", "buy", sym,
        "--usd", str(usd_to_buy),
        "--stop", f"{stop:.8f}",
        "--target", f"{target:.8f}",
        "--yes",
        "--agent",
        "--reason", reason,
        "--setup", "agent",
    ]
    log.info(f"agent: executing {' '.join(shlex.quote(c) for c in cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return False, f"trade.py buy rc={proc.returncode} stderr={proc.stderr[:400]}"
        record_trade_opened(state, sym)
        return True, proc.stdout[-400:]
    except subprocess.TimeoutExpired:
        return False, "trade.py buy timed out"
    except Exception as e:
        return False, f"trade.py buy raised: {e}"


def execute_early_exit(verdict: dict, client) -> tuple[bool, str]:
    sym = verdict["symbol"].upper()
    if DRY_RUN:
        log.info(f"agent DRY-RUN: would early-exit {sym}")
        notify.send("signals",
                    content=f"🤖 [DRY-RUN] Agent would EARLY-EXIT **{sym}**: {verdict.get('reasoning', '')[:300]}")
        return True, "dry-run logged"
    cmd = ["python", "trade.py", "sell", sym, "--yes"]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return False, f"trade.py sell rc={proc.returncode} stderr={proc.stderr[:400]}"
        return True, proc.stdout[-400:]
    except Exception as e:
        return False, f"trade.py sell raised: {e}"


# ──────────────────────────────────────────────────────────────────
# Discord posting
# ──────────────────────────────────────────────────────────────────

def post_verdict(verdict: dict, gate_result: tuple[bool, str], action_result: Optional[tuple[bool, str]]) -> None:
    """Rich Discord embed of what the agent decided and what happened."""
    decision = verdict.get("decision", "?")
    sym = verdict.get("symbol", "?")
    color = {"BUY": "blue", "SKIP": "grey", "EARLY_EXIT": "yellow", "WATCH": "purple"}.get(decision, "purple")

    fields = [
        {"name": "Decision", "value": decision, "inline": True},
        {"name": "Symbol", "value": sym, "inline": True},
        {"name": "Score", "value": str(verdict.get("score", "?")), "inline": True},
    ]
    if decision == "BUY":
        fields.extend([
            {"name": "Entry", "value": f"${verdict.get('entry')}", "inline": True},
            {"name": "Stop", "value": f"${verdict.get('stop')}", "inline": True},
            {"name": "Target", "value": f"${verdict.get('target')}", "inline": True},
            {"name": "R:R", "value": f"{verdict.get('rr', 0):.2f}", "inline": True},
        ])
    elif decision == "WATCH":
        cond_parts = []
        if verdict.get("watch_price_lte"):
            cond_parts.append(f"price ≤ ${verdict['watch_price_lte']}")
        if verdict.get("watch_price_gte"):
            cond_parts.append(f"price ≥ ${verdict['watch_price_gte']}")
        if verdict.get("watch_structure_flip"):
            cond_parts.append(f"1h struct → {verdict['watch_structure_flip']}")
        if verdict.get("watch_cvd_signal"):
            cond_parts.append(f"CVD → {verdict['watch_cvd_signal']}")
        if verdict.get("watch_sweep_printed"):
            cond_parts.append(f"15m {verdict['watch_sweep_printed']} sweep")
        fields.append({"name": "Watch trigger (any)", "value": " · ".join(cond_parts) or "?", "inline": False})
        fields.append({"name": "Action", "value": verdict.get("watch_action", "reeval"), "inline": True})
        fields.append({"name": "Expires", "value": f"{verdict.get('watch_expires_hours', 12)}h", "inline": True})

    allowed, gate_msg = gate_result
    fields.append({"name": "Gate", "value": ("✅ " if allowed else "❌ ") + gate_msg, "inline": False})
    if action_result is not None:
        ok, msg = action_result
        fields.append({"name": "Execution", "value": ("✅ " if ok else "❌ ") + msg[:300], "inline": False})

    desc = verdict.get("reasoning", "")[:1500]
    if verdict.get("primary_concern"):
        desc += f"\n\n**Concern:** {verdict['primary_concern'][:300]}"

    embed = {
        "title": f"🤖 Agent Verdict — {sym} {decision}",
        "description": desc,
        "color": notify.COLORS.get(color, notify.COLORS["blue"]),
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "claude-agent" + (" [DRY-RUN]" if DRY_RUN else "")},
    }
    notify.send("signals", embed=embed)


# ──────────────────────────────────────────────────────────────────
# Pre-rendering layer 1+2 (confluence text) and layer 3 (chart PNGs)
# ──────────────────────────────────────────────────────────────────

def prerender(symbol: str) -> tuple[Optional[str], Optional[dict]]:
    """Run confluence --whale and chart-multi for the symbol; return (text, chart_paths)."""
    try:
        confluence_proc = subprocess.run(
            ["python", "trade.py", "confluence", symbol, "--whale", "--json"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        # Confluence JSON output is preferred for parseability, but the prompt expects
        # human-readable. Re-run without --json if the flag isn't supported, but most
        # versions of trade.py only have --json on some commands. Use plain text as primary.
    except Exception as e:
        log.warning(f"prerender confluence(--json) failed for {symbol}: {e}")
        confluence_proc = None

    try:
        plain = subprocess.run(
            ["python", "trade.py", "confluence", symbol, "--whale"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        confluence_text = plain.stdout.strip() or (plain.stderr or "")
    except Exception as e:
        log.warning(f"prerender confluence failed for {symbol}: {e}")
        confluence_text = None

    try:
        chart_proc = subprocess.run(
            ["python", "trade.py", "chart-multi", symbol],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
        try:
            chart_data = json.loads(chart_proc.stdout)
            chart_paths = chart_data.get("rendered", {}) if isinstance(chart_data, dict) else {}
        except json.JSONDecodeError:
            chart_paths = {}
    except Exception as e:
        log.warning(f"prerender chart-multi failed for {symbol}: {e}")
        chart_paths = {}

    return confluence_text, chart_paths


# ──────────────────────────────────────────────────────────────────
# ClaudeAgentJob — drains state["agent_queue"], runs claude, executes
# ──────────────────────────────────────────────────────────────────

# Job is defined in daemon.py; this module is loaded after daemon imports it,
# so we can't subclass cleanly. Instead, daemon.py imports ClaudeAgentJob from
# this module and we re-define the Job interface (just `name`, `interval`,
# `due`, `run`, `safe_run`, `last_run`).

class ClaudeAgentJob:
    name = "claude_agent"

    def __init__(self):
        self.last_run = 0.0
        self.interval = int(os.getenv("AGENT_TICK_INTERVAL", 30))
        self.current_event_id: Optional[str] = None
        self.current_event: Optional[dict] = None
        self.current_proc: Optional[subprocess.Popen] = None
        self.current_started: float = 0.0

    def due(self) -> bool:
        return time.time() - self.last_run >= self.interval

    def safe_run(self, ctx: dict) -> None:
        try:
            self.run(ctx)
        except Exception as e:
            log.error(f"claude_agent failed: {e}\n{traceback.format_exc()}")
            try:
                notify.system_alert("ERROR", "claude_agent failed", str(e)[:1000])
            except Exception:
                pass
        finally:
            self.last_run = time.time()

    def run(self, ctx: dict) -> None:
        state: dict = ctx["state"]
        client = ctx["client"]
        # Roll over daily counters if date changed (reads + writes)
        _agent_daily(state)

        # 1) If there's a subprocess in flight, check on it
        if self.current_proc is not None:
            self._check_in_flight(ctx)
            return  # only handle one event per tick

        # 2) Otherwise pull next event from the queue
        queue = state.setdefault("agent_queue", [])
        if not queue:
            return

        d = _agent_daily(state)
        if d["breaker_tripped"]:
            log.info("agent: breaker tripped, draining queue without action")
            state["agent_queue"] = []
            return
        if d["trades_opened"] >= DAILY_TRADE_CAP:
            log.info(f"agent: daily cap {d['trades_opened']}/{DAILY_TRADE_CAP} hit, draining queue")
            state["agent_queue"] = []
            return

        event = queue.pop(0)
        # Persist queue mutation immediately so a crash doesn't replay
        from daemon import save_state  # local import to avoid circular
        save_state(state)

        symbol = event.get("symbol", "?")
        log.info(f"agent: starting analysis for {symbol} (trigger={event.get('trigger')})")

        # Pre-render charts + confluence
        confluence_text, chart_paths = prerender(symbol)
        if not chart_paths:
            log.warning(f"agent: no charts rendered for {symbol}, skipping")
            notify.send("signals",
                        content=f"🤖 Agent skipped **{symbol}** — chart-multi failed.")
            return

        prompt = build_prompt(event, chart_paths, confluence_text or "(confluence output unavailable)")

        event_id = f"{int(time.time())}_{symbol}"
        try:
            self.current_proc = spawn_claude(prompt, event_id)
            self.current_event_id = event_id
            self.current_event = event
            self.current_started = time.time()
        except FileNotFoundError:
            notify.system_alert("ERROR", "claude binary not found",
                                "Run `claude` once inside the container to login.")
            log.error("claude binary not found in PATH")

    def _check_in_flight(self, ctx: dict) -> None:
        elapsed = time.time() - self.current_started
        rc = self.current_proc.poll()

        if rc is None:
            if elapsed > SUBPROCESS_TIMEOUT_SEC:
                log.warning(f"agent: timeout after {elapsed:.0f}s — killing")
                try:
                    self.current_proc.kill()
                except Exception:
                    pass
                notify.send("signals",
                            content=f"🤖 Agent timed out on **{self.current_event.get('symbol')}** "
                                    f"({elapsed:.0f}s) — SKIP.")
                self._clear_in_flight()
            return

        # Subprocess exited
        log.info(f"agent: subprocess exited rc={rc} after {elapsed:.0f}s")
        assistant_text, _raw = read_subprocess_output(self.current_event_id)
        if rc != 0 or not assistant_text:
            err_path = RESULTS_DIR / f"{self.current_event_id}.err"
            err_snippet = err_path.read_text(errors="replace")[:500] if err_path.exists() else ""
            notify.send("signals",
                        content=f"🤖 Agent subprocess failed on **{self.current_event.get('symbol')}** "
                                f"(rc={rc}). {err_snippet}")
            self._clear_in_flight()
            return

        verdict = parse_decision(assistant_text)
        if not verdict:
            notify.send("signals",
                        content=f"🤖 Agent output unparseable on **{self.current_event.get('symbol')}** — SKIP.\n"
                                f"```\n{assistant_text[:1500]}\n```")
            self._clear_in_flight()
            return

        # Execute or skip per verdict
        decision = verdict.get("decision", "SKIP").upper()
        state = ctx["state"]
        client = ctx["client"]

        action_result: Optional[tuple[bool, str]] = None
        if decision == "BUY":
            allowed, reason = gates_for_buy(verdict, state, client)
            if allowed:
                action_result = execute_buy(verdict, client, state)
            else:
                action_result = (False, f"gate rejected: {reason}")
            post_verdict(verdict, (allowed, reason), action_result)
        elif decision == "WATCH":
            sym = verdict.get("symbol", "").upper()
            ok, reason, watch = agent_watch.add_watch(
                state, sym,
                price_lte=float(verdict["watch_price_lte"]) if verdict.get("watch_price_lte") else None,
                price_gte=float(verdict["watch_price_gte"]) if verdict.get("watch_price_gte") else None,
                structure_flip=verdict.get("watch_structure_flip") or None,
                cvd_signal=verdict.get("watch_cvd_signal") or None,
                sweep_printed=verdict.get("watch_sweep_printed") or None,
                action=verdict.get("watch_action") or "reeval",
                expires_in_hours=float(verdict.get("watch_expires_hours") or 12),
                thesis=verdict.get("reasoning", "")[:500],
                original_trigger=(self.current_event or {}).get("trigger", ""),
            )
            action_result = (ok, f"watch added (id={watch['id'][:8]})" if ok else f"watch rejected: {reason}")
            post_verdict(verdict, (True, "watch path"), action_result)
        elif decision == "EARLY_EXIT":
            # Only honored on position-review events; check there's an open position
            sym = verdict.get("symbol", "").upper()
            has_pos = _check_open_position(client, sym)
            if not has_pos:
                action_result = (False, "no open position to exit")
                post_verdict(verdict, (False, "no open position"), action_result)
            else:
                action_result = execute_early_exit(verdict, client)
                post_verdict(verdict, (True, "open position present"), action_result)
        else:
            # SKIP — just post the verdict, no action
            post_verdict(verdict, (True, "no action (SKIP)"), None)

        # Persist counter changes
        from daemon import save_state
        save_state(state)
        self._clear_in_flight()

    def _clear_in_flight(self) -> None:
        self.current_proc = None
        self.current_event = None
        self.current_event_id = None
        self.current_started = 0.0


# ──────────────────────────────────────────────────────────────────
# Queue helpers used by other jobs
# ──────────────────────────────────────────────────────────────────

def enqueue_event(state: dict, event: dict) -> None:
    """Other jobs call this to ask the agent to evaluate a symbol.

    Skips excluded symbols (stablecoins, leveraged tokens, non-USDT) at enqueue time
    to avoid burning ~$0.20/analysis on symbols that would be hard-rejected anyway.
    Dedupe: same (symbol, trigger) won't be enqueued twice within 1h.
    """
    sym = event.get("symbol", "?")
    trigger = event.get("trigger", "?")
    excl = _is_excluded_symbol(sym)
    if excl:
        log.info(f"agent: skipping enqueue for {sym} ({excl})")
        return
    key = f"{sym}|{trigger}"
    seen = state.setdefault("agent_enqueue_seen", {})
    now = time.time()
    if now - seen.get(key, 0) < 3600:
        return  # already enqueued this trigger recently
    seen[key] = now
    queue = state.setdefault("agent_queue", [])
    queue.append({**event, "enqueued_at": now})
    log.info(f"agent: enqueued event {key} (queue depth {len(queue)})")

