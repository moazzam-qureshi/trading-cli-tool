"""Render candlestick charts with SMC overlays.

Produces a PNG showing:
  - Candles (mplfinance)
  - Swing high/low markers
  - Bullish/bearish order blocks as colored zones
  - Unfilled FVGs as colored boxes
  - Recent sweep marker
  - EMA20/50 + volume

Output: writes a PNG and returns the path.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
from binance.client import Client

import analysis


CHART_DIR = Path(os.getenv("CHART_DIR", "/app/charts"))


def _interval_to_kline(tf: str) -> str:
    """Map 5m, 15m, 1h, 4h, 1d to Binance interval strings."""
    tf = tf.lower()
    valid = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
    if tf in valid:
        return tf
    raise ValueError(f"Unsupported timeframe: {tf}")


def render_chart(client: Client, symbol: str, tf: str = "15m", bars: int = 150,
                 out_path: Optional[str] = None) -> str:
    """Render a candlestick chart with SMC overlays. Returns the path to the saved PNG."""
    interval = _interval_to_kline(tf)
    df = analysis.fetch_klines(client, symbol, interval, bars)
    if df is None or df.empty:
        raise RuntimeError(f"No klines for {symbol} {tf}")

    # Detect SMC primitives
    swings = analysis.detect_swings(df, lookback=3)
    obs = analysis.detect_order_blocks(df, lookback=min(50, len(df)))
    fvgs = analysis.detect_fvg(df, lookback=min(50, len(df)))
    sweep = analysis.detect_sweep(df, swings, bars=10)

    # mplfinance expects DatetimeIndex named Date with OHLCV columns
    chart_df = df[["open", "high", "low", "close", "volume"]].copy()
    chart_df.columns = ["Open", "High", "Low", "Close", "Volume"]

    # Add EMAs
    ema20 = analysis.ema(chart_df["Close"], 20)
    ema50 = analysis.ema(chart_df["Close"], 50)

    addplots = [
        mpf.make_addplot(ema20, color="#2196F3", width=0.9),
        mpf.make_addplot(ema50, color="#FF9800", width=0.9),
    ]

    # Swing markers — separate scatter series for HH/HL vs LH/LL
    swing_high_pts = pd.Series([float("nan")] * len(chart_df), index=chart_df.index)
    swing_low_pts = pd.Series([float("nan")] * len(chart_df), index=chart_df.index)
    for sw in swings[-20:]:  # last 20 swings to avoid clutter
        try:
            ts = pd.to_datetime(sw.time)
            # find nearest index
            if ts in chart_df.index:
                idx = ts
            else:
                # tolerant match
                nearest = chart_df.index.get_indexer([ts], method="nearest")[0]
                idx = chart_df.index[nearest]
            if sw.kind in ("HH", "LH"):
                swing_high_pts.loc[idx] = sw.price * 1.002
            else:
                swing_low_pts.loc[idx] = sw.price * 0.998
        except Exception:
            continue
    if swing_high_pts.notna().any():
        addplots.append(mpf.make_addplot(swing_high_pts, type="scatter", marker="v", markersize=40, color="red"))
    if swing_low_pts.notna().any():
        addplots.append(mpf.make_addplot(swing_low_pts, type="scatter", marker="^", markersize=40, color="green"))

    # Render
    if out_path is None:
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        out_path = str(CHART_DIR / f"{symbol}_{tf}.png")

    title = f"{symbol} {tf} — last {len(chart_df)} candles"
    if sweep:
        title += f"  |  recent {sweep['type']} @ {sweep['level']}"

    fig, axes = mpf.plot(
        chart_df,
        type="candle",
        style="charles",
        addplot=addplots,
        volume=True,
        title=title,
        figsize=(12, 7),
        returnfig=True,
    )
    ax_price = axes[0]

    # Overlay OBs as colored zones
    x_first = chart_df.index[0]
    x_last = chart_df.index[-1]
    for ob in obs.get("bullish", [])[:5]:
        try:
            ax_price.axhspan(ob["low"], ob["high"], color="#4CAF50", alpha=0.15, zorder=0)
        except Exception:
            pass
    for ob in obs.get("bearish", [])[:5]:
        try:
            ax_price.axhspan(ob["low"], ob["high"], color="#F44336", alpha=0.15, zorder=0)
        except Exception:
            pass

    # Overlay unfilled FVGs as boxes
    for fvg in fvgs.get("bullish", [])[:5]:
        if not fvg.get("filled"):
            ax_price.axhspan(fvg["low"], fvg["high"], color="#00BCD4", alpha=0.15, zorder=0)
    for fvg in fvgs.get("bearish", [])[:5]:
        if not fvg.get("filled"):
            ax_price.axhspan(fvg["low"], fvg["high"], color="#FF5722", alpha=0.15, zorder=0)

    # Sweep annotation
    if sweep:
        try:
            ax_price.axhline(sweep["level"], color="purple", linestyle="--", linewidth=0.8, alpha=0.7)
            ax_price.text(x_last, sweep["level"], f" sweep {sweep['level']}", color="purple",
                          fontsize=9, va="center")
        except Exception:
            pass

    # Current price line
    try:
        last_close = chart_df["Close"].iloc[-1]
        ax_price.axhline(last_close, color="black", linestyle=":", linewidth=0.6, alpha=0.5)
        ax_price.text(x_last, last_close, f" {last_close}", color="black", fontsize=9, va="center")
    except Exception:
        pass

    fig.savefig(out_path, dpi=100)
    plt.close(fig)

    return out_path
