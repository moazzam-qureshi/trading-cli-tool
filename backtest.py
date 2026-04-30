"""Historical backtest engine for the SMC confluence strategy.

Validates the 8/10 confluence rule against past data:
  1. Pre-fetch 4h / 1h / 15m klines for the symbol.
  2. Walk forward bar-by-bar on the 15m series (the entry timeframe).
  3. At each bar, slice all three dataframes to "as of that bar" (no lookahead).
  4. Score confluence. If >= min_score, simulate a trade:
       entry  = next 15m bar's open
       stop   = most recent opposing LTF swing (long: last LL/HL; short: last HH/LH)
                with a 0.1% buffer beyond it
       target = entry + RR * (entry - stop)   (or - for shorts)
  5. Walk forward bar-by-bar checking which is hit first using bar high/low.
       (Stop and target on the same bar => assume stop hit, conservative.)
  6. Block re-entry on the same symbol while a trade is open.
  7. Aggregate: trades, win-rate, avg R, profit factor, equity curve, max DD.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
from binance.client import Client

import analysis


# ──────────────────────────────────────────────────────────────────
# Data alignment
# ──────────────────────────────────────────────────────────────────

def _slice_until(df: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    """Return all bars in df with open_time <= ts."""
    return df.loc[df.index <= ts]


# ──────────────────────────────────────────────────────────────────
# Trade record
# ──────────────────────────────────────────────────────────────────

@dataclass
class SimTrade:
    entry_time: str
    direction: str
    entry: float
    stop: float
    target: float
    score: int
    risk_pct: float
    rr: float
    exit_time: str = ""
    exit: float = 0.0
    outcome: str = ""  # WIN / LOSS / OPEN / TIMEOUT / WIN_PARTIAL_BE / WIN_PARTIAL_TARGET
    r_multiple: float = 0.0
    bars_held: int = 0
    partial_taken: bool = False
    effective_stop: float = 0.0  # current stop (moves to BE after partial)


# ──────────────────────────────────────────────────────────────────
# Stop placement
# ──────────────────────────────────────────────────────────────────

def _pick_stop(direction: str, ltf_swings: list, current_price: float, atr_val: float) -> Optional[float]:
    """Stop = most recent opposing swing with small buffer. Falls back to ATR-based stop."""
    if direction == "long":
        for s in reversed(ltf_swings):
            if s.kind in ("HL", "LL", "L") and s.price < current_price:
                return s.price * 0.999  # 0.1% below
        return current_price - 1.5 * atr_val
    else:
        for s in reversed(ltf_swings):
            if s.kind in ("HH", "LH", "H") and s.price > current_price:
                return s.price * 1.001
        return current_price + 1.5 * atr_val


# ──────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────

def run_backtest(
    client: Client,
    symbol: str,
    bars_15m: int = 1500,
    min_score: int = 8,
    rr: float = 2.0,
    max_hold_bars: int = 96,  # 96 * 15m = 24h
    risk_pct: float = 2.0,
    starting_equity: float = 150.0,
    partial_pct: float = 0.0,       # 0 = disabled. Else % of position to close at partial_at_r
    partial_at_r: float = 1.0,
    score_every_n: int = 1,         # only score every Nth bar (4 = once per hour) — big speedup
    ote_filter: bool = False,       # if True, reject entries above OTE 62% retrace
    ote_top: float = 0.62,          # retrace threshold (0.62 = SMC-orthodox top of OTE)
    ceiling_filter: bool = False,   # if True, reject if 1.5R target > recent N-bar MTF high
    ceiling_lookback: int = 96,     # MTF (1H) bars
    vsa_filter: bool = False,       # if True, hard-reject entries on LTF up_thrust or recent MTF up_thrust
    vsa_mtf_lookback: int = 5,
    track_filter_stats: bool = True,
) -> dict:
    symbol = symbol.upper()

    # Fetch enough HTF data to cover the LTF window
    # 1500 * 15m ≈ 16 days; need >=4 days of 4h (roughly); fetch 500 of each HTF
    ltf_df = analysis.fetch_klines(client, symbol, "15m", bars_15m)
    mtf_df = analysis.fetch_klines(client, symbol, "1h", 500)
    htf_df = analysis.fetch_klines(client, symbol, "4h", 500)

    # Need enough warmup bars for swings/indicators (~50 bars)
    warmup = 50
    if len(ltf_df) < warmup + 10:
        return {"error": "Not enough 15m bars", "symbol": symbol}

    trades: list[SimTrade] = []
    open_trade: Optional[SimTrade] = None
    open_idx: int = -1
    filter_stats = {"rejected_ote": 0, "rejected_ceiling": 0, "rejected_vsa_upthrust": 0}

    for i in range(warmup, len(ltf_df) - 1):
        bar = ltf_df.iloc[i]
        ts = ltf_df.index[i]
        next_bar = ltf_df.iloc[i + 1]

        # ── if a trade is open, walk it forward ──
        if open_trade is not None:
            high = float(bar["high"])
            low = float(bar["low"])
            held = i - open_idx
            t = open_trade
            risk_per = abs(t.entry - t.stop)
            partial_trigger = (t.entry + partial_at_r * risk_per) if t.direction == "long" \
                              else (t.entry - partial_at_r * risk_per)
            partial_frac = partial_pct / 100.0
            remainder_frac = 1 - partial_frac

            def hit_long(level): return low <= level
            def hit_short(level): return high >= level
            def hit_above(level): return high >= level
            def hit_below(level): return low <= level

            if t.direction == "long":
                stop_hit = hit_long(t.effective_stop)
                target_hit = hit_above(t.target)
                partial_hit = hit_above(partial_trigger) if partial_pct > 0 and not t.partial_taken else False
            else:
                stop_hit = hit_short(t.effective_stop)
                target_hit = hit_below(t.target)
                partial_hit = hit_below(partial_trigger) if partial_pct > 0 and not t.partial_taken else False

            # Conservative same-bar ordering:
            #   pre-partial:  stop > partial > target  (worst case)
            #   post-partial: stop(BE) > target        (worst case)
            if not t.partial_taken:
                if stop_hit:
                    t.exit_time = ts.isoformat(); t.exit = t.effective_stop
                    t.outcome = "LOSS"; t.r_multiple = -1.0; t.bars_held = held
                    trades.append(t); open_trade = None; continue
                if partial_pct > 0 and partial_hit:
                    # realize partial at +partial_at_r R, move stop to BE, keep going same bar
                    t.partial_taken = True
                    t.effective_stop = t.entry  # break-even
                    # Same bar: did target also hit AFTER partial? (price went up to partial then to target)
                    if target_hit:
                        t.exit_time = ts.isoformat(); t.exit = t.target
                        t.outcome = "WIN_PARTIAL_TARGET"
                        t.r_multiple = round(partial_frac * partial_at_r + remainder_frac * t.rr, 3)
                        t.bars_held = held
                        trades.append(t); open_trade = None; continue
                    # else: continue holding remainder with stop @ BE
                    continue
                if target_hit:
                    t.exit_time = ts.isoformat(); t.exit = t.target
                    t.outcome = "WIN"; t.r_multiple = t.rr; t.bars_held = held
                    trades.append(t); open_trade = None; continue
            else:
                # partial already taken; stop is at BE, only target or BE-stop can close
                if stop_hit:  # BE
                    t.exit_time = ts.isoformat(); t.exit = t.effective_stop
                    t.outcome = "WIN_PARTIAL_BE"
                    t.r_multiple = round(partial_frac * partial_at_r, 3)  # remainder = 0R
                    t.bars_held = held
                    trades.append(t); open_trade = None; continue
                if target_hit:
                    t.exit_time = ts.isoformat(); t.exit = t.target
                    t.outcome = "WIN_PARTIAL_TARGET"
                    t.r_multiple = round(partial_frac * partial_at_r + remainder_frac * t.rr, 3)
                    t.bars_held = held
                    trades.append(t); open_trade = None; continue
            if held >= max_hold_bars:
                exit_p = float(bar["close"])
                risk0 = abs(t.entry - t.stop)  # original R denominator
                if t.direction == "long":
                    r_full = (exit_p - t.entry) / risk0
                else:
                    r_full = (t.entry - exit_p) / risk0
                if t.partial_taken:
                    r_total = partial_frac * partial_at_r + remainder_frac * r_full
                else:
                    r_total = r_full
                t.exit_time = ts.isoformat(); t.exit = exit_p
                t.outcome = "TIMEOUT"; t.r_multiple = round(r_total, 2); t.bars_held = held
                trades.append(t); open_trade = None
            continue

        # ── no open trade — score this bar ──
        if (i - warmup) % score_every_n != 0:
            continue
        try:
            ltf_slice = ltf_df.iloc[: i + 1]
            mtf_slice = _slice_until(mtf_df, ts)
            htf_slice = _slice_until(htf_df, ts)
            if len(mtf_slice) < 50 or len(htf_slice) < 50:
                continue
            r = analysis.score_from_dfs(htf_slice, mtf_slice, ltf_slice)
        except Exception:
            continue

        if r["score"] < min_score or not r["direction"]:
            continue

        entry = float(next_bar["open"])
        stop = _pick_stop(r["direction"], r["ltf_swings"], entry, r["ltf_atr"])
        if stop is None:
            continue
        if r["direction"] == "long":
            if stop >= entry:
                continue
            target = entry + rr * (entry - stop)
        else:
            if stop <= entry:
                continue
            target = entry - rr * (stop - entry)

        # sanity: stop distance must be at least 0.1% (avoid noise stops)
        if abs(entry - stop) / entry < 0.001:
            continue

        # ── filter: OTE late-entry rejection ──
        # Always recompute with the requested ote_top — score_from_dfs caches
        # ote with the default threshold so reusing it would silently ignore
        # the param. Recompute is cheap (one swings + sweep pass on mtf_slice).
        if ote_filter:
            mtf_swings_local = analysis.detect_swings(mtf_slice)
            mtf_sweep_local = analysis.detect_sweep(mtf_slice, mtf_swings_local)
            ote = analysis.ote_check(r["direction"], mtf_slice, mtf_swings_local,
                                     mtf_sweep_local, entry, ote_top=ote_top)
            if ote.get("valid") is False:
                filter_stats["rejected_ote"] += 1
                continue

        # ── filter: target reachability ──
        if ceiling_filter:
            tr = analysis.target_reachable(r["direction"], entry, target,
                                           mtf_slice, lookback=ceiling_lookback)
            if not tr["reachable"]:
                filter_stats["rejected_ceiling"] += 1
                continue

        # ── filter: VSA up-thrust hard reject ──
        # An up-thrust on the very last LTF bar OR within the last N MTF bars
        # means smart money distributed into a high-volume new high. Don't
        # take a long into that.
        if vsa_filter and r["direction"] == "long":
            ltf_last_sig = analysis.vsa_bar(ltf_slice, len(ltf_slice) - 1)
            if ltf_last_sig == "up_thrust":
                filter_stats["rejected_vsa_upthrust"] += 1
                continue
            mtf_vsa = analysis.vsa_signature(mtf_slice, lookback=vsa_mtf_lookback)
            if mtf_vsa.get("has_up_thrust"):
                filter_stats["rejected_vsa_upthrust"] += 1
                continue

        open_trade = SimTrade(
            entry_time=ltf_df.index[i + 1].isoformat(),
            direction=r["direction"],
            entry=entry,
            stop=stop,
            target=target,
            score=r["score"],
            risk_pct=risk_pct,
            rr=rr,
            effective_stop=stop,
        )
        open_idx = i + 1

    # ── compile stats ──
    WIN_OUTCOMES = ("WIN", "WIN_PARTIAL_BE", "WIN_PARTIAL_TARGET")
    closed = [t for t in trades if t.outcome in WIN_OUTCOMES + ("LOSS", "TIMEOUT")]
    wins = [t for t in closed if t.outcome in WIN_OUTCOMES]
    losses = [t for t in closed if t.outcome == "LOSS"]
    timeouts = [t for t in closed if t.outcome == "TIMEOUT"]
    win_full = len([t for t in closed if t.outcome == "WIN"])
    win_partial_target = len([t for t in closed if t.outcome == "WIN_PARTIAL_TARGET"])
    win_partial_be = len([t for t in closed if t.outcome == "WIN_PARTIAL_BE"])

    total_r = sum(t.r_multiple for t in closed)
    avg_r = total_r / len(closed) if closed else 0
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    gross_win_r = sum(t.r_multiple for t in wins)
    gross_loss_r = abs(sum(t.r_multiple for t in losses + timeouts if t.r_multiple < 0))
    profit_factor = gross_win_r / gross_loss_r if gross_loss_r > 0 else float("inf") if gross_win_r > 0 else 0

    # equity curve compounding at risk_pct per trade
    equity = starting_equity
    curve = [equity]
    peak = equity
    max_dd = 0.0
    for t in closed:
        equity *= (1 + (risk_pct / 100) * t.r_multiple)
        curve.append(equity)
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)

    return {
        "symbol": symbol,
        "config": {
            "bars_15m": bars_15m,
            "min_score": min_score,
            "rr": rr,
            "max_hold_bars": max_hold_bars,
            "risk_pct": risk_pct,
            "starting_equity": starting_equity,
            "partial_pct": partial_pct,
            "partial_at_r": partial_at_r,
            "ote_filter": ote_filter,
            "ote_top": ote_top,
            "ceiling_filter": ceiling_filter,
            "ceiling_lookback": ceiling_lookback,
            "vsa_filter": vsa_filter,
        },
        "filter_stats": filter_stats,
        "period": {
            "from": ltf_df.index[0].isoformat(),
            "to": ltf_df.index[-1].isoformat(),
            "days": round((ltf_df.index[-1] - ltf_df.index[0]).total_seconds() / 86400, 1),
        },
        "stats": {
            "total_signals": len(trades),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "timeouts": len(timeouts),
            "win_full": win_full,
            "win_partial_target": win_partial_target,
            "win_partial_be": win_partial_be,
            "win_rate_pct": round(win_rate, 1),
            "avg_r": round(avg_r, 2),
            "total_r": round(total_r, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
            "starting_equity": starting_equity,
            "final_equity": round(equity, 2),
            "return_pct": round((equity - starting_equity) / starting_equity * 100, 1),
            "max_drawdown_pct": round(max_dd, 1),
        },
        "trades": [asdict(t) for t in trades],
    }


def run_backtest_fast(
    client: Client,
    symbol: str,
    bars_15m: int = 17500,
    min_score: int = 8,
    rr: float = 2.0,
    max_hold_bars: int = 96,
    risk_pct: float = 2.0,
    starting_equity: float = 150.0,
    partial_pct: float = 0.0,
    partial_at_r: float = 1.0,
    score_every_n: int = 1,
    htf_bars: int = 5000,    # plenty for 6mo coverage
    mtf_bars: int = 5000,
    trail_mode: str = "off",     # "off" | "be" (move stop to entry at trail_at_r)
    trail_at_r: float = 1.0,
) -> dict:
    """Same engine as run_backtest but uses precompute() + score_at() — ~50× faster.

    Trade decisions should match run_backtest within rounding (same swings, same logic).
    """
    import numpy as np
    symbol = symbol.upper()

    ltf_df = analysis.fetch_klines(client, symbol, "15m", bars_15m)
    mtf_df = analysis.fetch_klines(client, symbol, "1h", mtf_bars)
    htf_df = analysis.fetch_klines(client, symbol, "4h", htf_bars)

    warmup = 50
    if len(ltf_df) < warmup + 10:
        return {"error": "Not enough 15m bars", "symbol": symbol}

    # Precompute once — this is where the big win is.
    htf_pre = analysis.precompute(htf_df, "4h")
    mtf_pre = analysis.precompute(mtf_df, "1h")
    ltf_pre = analysis.precompute(ltf_df, "15m")

    # Map every LTF bar to its corresponding MTF/HTF bar via timestamp searchsorted.
    ltf_times = ltf_df.index.values
    mtf_times = mtf_df.index.values
    htf_times = htf_df.index.values
    mtf_idx_for_ltf = np.searchsorted(mtf_times, ltf_times, side="right") - 1
    htf_idx_for_ltf = np.searchsorted(htf_times, ltf_times, side="right") - 1

    trades: list[SimTrade] = []
    open_trade: Optional[SimTrade] = None
    open_idx = -1

    closes_l = ltf_df["close"].values
    highs_l = ltf_df["high"].values
    lows_l = ltf_df["low"].values

    for i in range(warmup, len(ltf_df) - 1):
        ts = ltf_df.index[i]

        # ── walk open trade forward (identical math to slow path) ──
        if open_trade is not None:
            high = float(highs_l[i]); low = float(lows_l[i])
            held = i - open_idx
            t = open_trade
            risk_per = abs(t.entry - t.stop)
            partial_trigger = (t.entry + partial_at_r * risk_per) if t.direction == "long" \
                              else (t.entry - partial_at_r * risk_per)
            partial_frac = partial_pct / 100.0
            remainder_frac = 1 - partial_frac

            # BE-trail check: if armed and price has reached trail_at_r, ratchet stop to entry.
            # Conservative ordering: stop check uses the new (tighter) stop ONLY if the bar's
            # high reached the trigger BEFORE the bar's low touched the original stop. Since
            # we can't know intra-bar order, we apply the conservative rule:
            #   - if both old-stop AND trail-trigger could have hit → assume old-stop first (lose)
            #   - if only trail-trigger could have hit → arm BE (move effective_stop to entry)
            if trail_mode == "be" and t.effective_stop < t.entry:
                trail_trigger = (t.entry + trail_at_r * risk_per) if t.direction == "long" \
                                else (t.entry - trail_at_r * risk_per)
                if t.direction == "long":
                    trail_armed = high >= trail_trigger
                    bar_might_have_stopped = low <= t.effective_stop
                else:
                    trail_armed = low <= trail_trigger
                    bar_might_have_stopped = high >= t.effective_stop
                if trail_armed and not bar_might_have_stopped:
                    t.effective_stop = t.entry  # arm BE

            if t.direction == "long":
                stop_hit = low <= t.effective_stop
                target_hit = high >= t.target
                partial_hit = (partial_pct > 0 and not t.partial_taken and high >= partial_trigger)
            else:
                stop_hit = high >= t.effective_stop
                target_hit = low <= t.target
                partial_hit = (partial_pct > 0 and not t.partial_taken and low <= partial_trigger)

            if not t.partial_taken:
                if stop_hit:
                    t.exit_time = ts.isoformat(); t.exit = t.effective_stop
                    # R against ORIGINAL risk: BE-trailed stop hits at 0R, original stop = -1R
                    if t.direction == "long":
                        r = (t.effective_stop - t.entry) / risk_per
                    else:
                        r = (t.entry - t.effective_stop) / risk_per
                    t.outcome = "BE_STOP" if abs(r) < 0.001 else "LOSS"
                    t.r_multiple = round(r, 3); t.bars_held = held
                    trades.append(t); open_trade = None; continue
                if partial_pct > 0 and partial_hit:
                    t.partial_taken = True; t.effective_stop = t.entry
                    if target_hit:
                        t.exit_time = ts.isoformat(); t.exit = t.target
                        t.outcome = "WIN_PARTIAL_TARGET"
                        t.r_multiple = round(partial_frac * partial_at_r + remainder_frac * t.rr, 3)
                        t.bars_held = held; trades.append(t); open_trade = None; continue
                    continue
                if target_hit:
                    t.exit_time = ts.isoformat(); t.exit = t.target
                    t.outcome = "WIN"; t.r_multiple = t.rr; t.bars_held = held
                    trades.append(t); open_trade = None; continue
            else:
                if stop_hit:
                    t.exit_time = ts.isoformat(); t.exit = t.effective_stop
                    t.outcome = "WIN_PARTIAL_BE"
                    t.r_multiple = round(partial_frac * partial_at_r, 3)
                    t.bars_held = held; trades.append(t); open_trade = None; continue
                if target_hit:
                    t.exit_time = ts.isoformat(); t.exit = t.target
                    t.outcome = "WIN_PARTIAL_TARGET"
                    t.r_multiple = round(partial_frac * partial_at_r + remainder_frac * t.rr, 3)
                    t.bars_held = held; trades.append(t); open_trade = None; continue

            if held >= max_hold_bars:
                exit_p = float(closes_l[i])
                risk0 = abs(t.entry - t.stop)
                r_full = ((exit_p - t.entry) if t.direction == "long" else (t.entry - exit_p)) / risk0
                r_total = (partial_frac * partial_at_r + remainder_frac * r_full) if t.partial_taken else r_full
                t.exit_time = ts.isoformat(); t.exit = exit_p
                t.outcome = "TIMEOUT"; t.r_multiple = round(r_total, 2); t.bars_held = held
                trades.append(t); open_trade = None
            continue

        # ── scoring (the hot path that just got 50× faster) ──
        if (i - warmup) % score_every_n != 0:
            continue
        mtf_i = int(mtf_idx_for_ltf[i])
        htf_i = int(htf_idx_for_ltf[i])
        if mtf_i < warmup or htf_i < 20:
            continue
        try:
            r = analysis.score_at(htf_pre, mtf_pre, ltf_pre, htf_i, mtf_i, i)
        except Exception:
            continue

        if r["score"] < min_score or not r["direction"]:
            continue

        next_bar = ltf_df.iloc[i + 1]
        entry = float(next_bar["open"])
        stop = _pick_stop(r["direction"], r["ltf_swings"], entry, r["ltf_atr"])
        if stop is None:
            continue
        if r["direction"] == "long" and stop >= entry:
            continue
        if r["direction"] == "short" and stop <= entry:
            continue
        if abs(entry - stop) / entry < 0.001:
            continue
        target = entry + rr * (entry - stop) if r["direction"] == "long" else entry - rr * (stop - entry)

        open_trade = SimTrade(
            entry_time=ltf_df.index[i + 1].isoformat(),
            direction=r["direction"], entry=entry, stop=stop, target=target,
            score=r["score"], risk_pct=risk_pct, rr=rr, effective_stop=stop,
        )
        open_idx = i + 1

    # ── stats (same as run_backtest) ──
    WIN_OUTCOMES = ("WIN", "WIN_PARTIAL_BE", "WIN_PARTIAL_TARGET")
    closed = [t for t in trades if t.outcome in WIN_OUTCOMES + ("LOSS", "TIMEOUT", "BE_STOP")]
    be_stops = len([t for t in closed if t.outcome == "BE_STOP"])
    wins = [t for t in closed if t.outcome in WIN_OUTCOMES]
    losses = [t for t in closed if t.outcome == "LOSS"]
    timeouts = [t for t in closed if t.outcome == "TIMEOUT"]
    total_r = sum(t.r_multiple for t in closed)
    avg_r = total_r / len(closed) if closed else 0
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    gross_win_r = sum(t.r_multiple for t in wins)
    gross_loss_r = abs(sum(t.r_multiple for t in losses + timeouts if t.r_multiple < 0))
    profit_factor = gross_win_r / gross_loss_r if gross_loss_r > 0 else (float("inf") if gross_win_r > 0 else 0)

    equity = starting_equity; curve = [equity]; peak = equity; max_dd = 0.0
    for t in closed:
        equity *= (1 + (risk_pct / 100) * t.r_multiple)
        curve.append(equity); peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    return {
        "symbol": symbol,
        "engine": "fast",
        "config": {
            "bars_15m": bars_15m, "min_score": min_score, "rr": rr,
            "max_hold_bars": max_hold_bars, "risk_pct": risk_pct,
            "starting_equity": starting_equity,
            "partial_pct": partial_pct, "partial_at_r": partial_at_r,
            "score_every_n": score_every_n,
        },
        "period": {
            "from": ltf_df.index[0].isoformat(), "to": ltf_df.index[-1].isoformat(),
            "days": round((ltf_df.index[-1] - ltf_df.index[0]).total_seconds() / 86400, 1),
        },
        "stats": {
            "total_signals": len(trades), "closed": len(closed),
            "wins": len(wins), "losses": len(losses), "timeouts": len(timeouts),
            "be_stops": be_stops,
            "win_rate_pct": round(win_rate, 1),
            "avg_r": round(avg_r, 2), "total_r": round(total_r, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
            "starting_equity": starting_equity, "final_equity": round(equity, 2),
            "return_pct": round((equity - starting_equity) / starting_equity * 100, 1),
            "max_drawdown_pct": round(max_dd, 1),
        },
        "trades": [asdict(t) for t in trades],
    }


def run_multi_backtest_fast(client: Client, symbols: list[str], **kwargs) -> dict:
    by_symbol = {}; all_trades = []
    for s in symbols:
        try:
            r = run_backtest_fast(client, s, **kwargs)
            if "error" in r:
                by_symbol[s] = {"error": r["error"]}; continue
            by_symbol[s] = r["stats"]
            for t in r["trades"]:
                t["symbol"] = s; all_trades.append(t)
        except Exception as e:
            by_symbol[s] = {"error": str(e)}
    closed = [t for t in all_trades if t["outcome"] in ("WIN", "WIN_PARTIAL_BE", "WIN_PARTIAL_TARGET", "LOSS", "TIMEOUT")]
    wins = [t for t in closed if t["outcome"] in ("WIN", "WIN_PARTIAL_BE", "WIN_PARTIAL_TARGET")]
    losses = [t for t in closed if t["outcome"] == "LOSS"]
    total_r = sum(t["r_multiple"] for t in closed)
    return {
        "symbols_tested": len(symbols), "by_symbol": by_symbol,
        "aggregate": {
            "total_trades": len(closed), "wins": len(wins), "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "avg_r": round(total_r / len(closed), 2) if closed else 0,
            "total_r": round(total_r, 2),
        },
    }


def run_score_sweep_fast(client: Client, symbols: list[str], scores: list[int],
                          bars_15m: int = 17500, **kwargs) -> dict:
    out = {"per_score": {}}
    for sc in scores:
        agg = run_multi_backtest_fast(client, symbols, bars_15m=bars_15m, min_score=sc, **kwargs)
        out["per_score"][str(sc)] = agg["aggregate"]
        out["per_score"][str(sc)]["per_symbol"] = {
            s: {k: v for k, v in stats.items() if k != "trades"}
            for s, stats in agg["by_symbol"].items()
        }
    return out


def run_score_sweep(client: Client, symbols: list[str], scores: list[int],
                     bars_15m: int = 17500, **kwargs) -> dict:
    """Run the same backtest config across multiple min_score thresholds and aggregate.

    Useful for finding the score where edge appears.
    """
    out = {"per_score": {}}
    for sc in scores:
        agg = run_multi_backtest(client, symbols, bars_15m=bars_15m, min_score=sc, **kwargs)
        out["per_score"][str(sc)] = agg["aggregate"]
        out["per_score"][str(sc)]["per_symbol"] = {
            s: {k: v for k, v in stats.items() if k != "trades"}
            for s, stats in agg["by_symbol"].items()
        }
    return out


def run_multi_backtest(client: Client, symbols: list[str], **kwargs) -> dict:
    """Run backtest across multiple symbols and aggregate results."""
    by_symbol = {}
    all_trades = []
    for s in symbols:
        try:
            r = run_backtest(client, s, **kwargs)
            if "error" in r:
                by_symbol[s] = {"error": r["error"]}
                continue
            by_symbol[s] = r["stats"]
            for t in r["trades"]:
                t["symbol"] = s
                all_trades.append(t)
        except Exception as e:
            by_symbol[s] = {"error": str(e)}

    closed = [t for t in all_trades if t["outcome"] in ("WIN", "LOSS", "TIMEOUT")]
    wins = [t for t in closed if t["outcome"] == "WIN"]
    losses = [t for t in closed if t["outcome"] == "LOSS"]
    total_r = sum(t["r_multiple"] for t in closed)
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    return {
        "symbols_tested": len(symbols),
        "by_symbol": by_symbol,
        "aggregate": {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "avg_r": round(total_r / len(closed), 2) if closed else 0,
            "total_r": round(total_r, 2),
        },
    }
