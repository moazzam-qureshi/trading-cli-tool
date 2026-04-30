"""Agent-set conditional watches — V2.

The agent emits decision=WATCH with one or more trigger conditions and an action
("reeval" = full 3-layer re-eval, or "notify" = Discord ping only). AgentWatchJob
in the daemon polls these and fires when any condition is met.

Conditions (any true → fire; OR semantics):
  - price_lte / price_gte           : current ticker crosses bound
  - structure_flip                  : 1h structure summary now == "Bullish" | "Bearish"
  - cvd_signal                      : 4h CVD interpretation matches (e.g. "strong_distribution")
  - sweep_printed                   : fresh 15m sweep matches "bullish" | "bearish"

Per-condition intervals avoid hammering expensive endpoints (CVD aggregates,
kline fetches) at the fast price-poll cadence.

State lives in state["agent_watches"] as a list of dicts.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

log = logging.getLogger(__name__)

MAX_ACTIVE_WATCHES = int(os.getenv("AGENT_WATCH_MAX_ACTIVE", 10))
MAX_EXPIRY_HOURS = float(os.getenv("AGENT_WATCH_MAX_EXPIRY_HOURS", 24))
DEFAULT_EXPIRY_HOURS = float(os.getenv("AGENT_WATCH_DEFAULT_EXPIRY_HOURS", 12))

# Per-condition check intervals — agent_watch job ticks at price interval; the
# heavier checks are gated by their own timestamps inside the watch dict.
PRICE_INTERVAL = int(os.getenv("AGENT_WATCH_PRICE_INTERVAL", 180))
STRUCTURE_INTERVAL = int(os.getenv("AGENT_WATCH_STRUCTURE_INTERVAL", 600))
CVD_INTERVAL = int(os.getenv("AGENT_WATCH_CVD_INTERVAL", 900))
SWEEP_INTERVAL = int(os.getenv("AGENT_WATCH_SWEEP_INTERVAL", 600))

VALID_STRUCTURE = {"Bullish", "Bearish"}
VALID_CVD = {"strong_distribution", "net_distribution", "strong_accumulation", "net_accumulation"}
VALID_SWEEP = {"bullish", "bearish"}
VALID_ACTION = {"reeval", "notify"}


def _watches(state: dict) -> list[dict]:
    return state.setdefault("agent_watches", [])


def add_watch(state: dict, symbol: str, *,
              price_lte: Optional[float] = None,
              price_gte: Optional[float] = None,
              structure_flip: Optional[str] = None,
              cvd_signal: Optional[str] = None,
              sweep_printed: Optional[str] = None,
              action: str = "reeval",
              expires_in_hours: float = DEFAULT_EXPIRY_HOURS,
              thesis: str = "",
              original_trigger: str = "") -> tuple[bool, str, Optional[dict]]:
    """Add a new conditional watch. Returns (ok, reason, watch_dict)."""
    sym = symbol.upper()

    has_price = price_lte is not None or price_gte is not None
    has_struct = structure_flip is not None
    has_cvd = cvd_signal is not None
    has_sweep = sweep_printed is not None
    if not (has_price or has_struct or has_cvd or has_sweep):
        return False, "no condition (need at least one of price/structure/cvd/sweep)", None

    if price_lte is not None and price_gte is not None and price_lte <= price_gte:
        return False, f"contradictory price bounds (lte {price_lte} ≤ gte {price_gte})", None
    if structure_flip is not None and structure_flip not in VALID_STRUCTURE:
        return False, f"invalid structure_flip ({structure_flip}); must be one of {sorted(VALID_STRUCTURE)}", None
    if cvd_signal is not None and cvd_signal not in VALID_CVD:
        return False, f"invalid cvd_signal ({cvd_signal}); must be one of {sorted(VALID_CVD)}", None
    if sweep_printed is not None and sweep_printed not in VALID_SWEEP:
        return False, f"invalid sweep_printed ({sweep_printed}); must be one of {sorted(VALID_SWEEP)}", None
    if action not in VALID_ACTION:
        return False, f"invalid action ({action}); must be one of {sorted(VALID_ACTION)}", None

    expires_in_hours = max(0.5, min(float(expires_in_hours), MAX_EXPIRY_HOURS))

    cleanup_expired(state)
    watches = _watches(state)

    # Dedupe: same symbol + overlapping condition shape → replace
    for w in list(watches):
        if w["symbol"] != sym:
            continue
        same_price_lte = price_lte is not None and w.get("price_lte") is not None
        same_price_gte = price_gte is not None and w.get("price_gte") is not None
        same_struct = has_struct and w.get("structure_flip") is not None
        same_cvd = has_cvd and w.get("cvd_signal") is not None
        same_sweep = has_sweep and w.get("sweep_printed") is not None
        if same_price_lte or same_price_gte or same_struct or same_cvd or same_sweep:
            watches.remove(w)
            log.info(f"watch: replaced existing {sym} watch {w['id'][:8]}")
            break

    if len(watches) >= MAX_ACTIVE_WATCHES:
        return False, f"max active watches reached ({len(watches)}/{MAX_ACTIVE_WATCHES})", None

    now = time.time()
    watch = {
        "id": uuid.uuid4().hex,
        "symbol": sym,
        "set_at_ts": now,
        "expires_at_ts": now + expires_in_hours * 3600,
        "thesis": (thesis or "")[:500],
        "original_trigger": (original_trigger or "")[:120],
        "action": action,
        "price_lte": float(price_lte) if price_lte is not None else None,
        "price_gte": float(price_gte) if price_gte is not None else None,
        "structure_flip": structure_flip,
        "cvd_signal": cvd_signal,
        "sweep_printed": sweep_printed,
        "last_check": {"price": 0, "structure": 0, "cvd": 0, "sweep": 0},
    }
    watches.append(watch)
    log.info(f"watch: added {sym} action={action} conditions={_describe_conditions(watch)} expires_in={expires_in_hours}h")
    return True, "ok", watch


def _describe_conditions(w: dict) -> str:
    parts = []
    if w.get("price_lte") is not None:
        parts.append(f"price≤{w['price_lte']}")
    if w.get("price_gte") is not None:
        parts.append(f"price≥{w['price_gte']}")
    if w.get("structure_flip"):
        parts.append(f"struct={w['structure_flip']}")
    if w.get("cvd_signal"):
        parts.append(f"cvd={w['cvd_signal']}")
    if w.get("sweep_printed"):
        parts.append(f"sweep={w['sweep_printed']}")
    return ",".join(parts) if parts else "none"


def cleanup_expired(state: dict) -> int:
    watches = _watches(state)
    now = time.time()
    before = len(watches)
    state["agent_watches"] = [w for w in watches if w["expires_at_ts"] > now]
    removed = before - len(state["agent_watches"])
    if removed:
        log.info(f"watch: dropped {removed} expired")
    return removed


# ── Per-condition evaluators ─────────────────────────────────────────

def _check_price(client, sym: str, w: dict, prices: dict) -> Optional[tuple[str, float]]:
    """Returns (triggered_by, value) or None. prices is a pre-fetched ticker dict."""
    price = prices.get(sym)
    if price is None:
        return None
    if w.get("price_lte") is not None and price <= w["price_lte"]:
        return "price_lte", price
    if w.get("price_gte") is not None and price >= w["price_gte"]:
        return "price_gte", price
    return None


def _check_structure(client, sym: str, w: dict) -> Optional[tuple[str, str]]:
    target = w.get("structure_flip")
    if not target:
        return None
    try:
        import analysis
        df = analysis.fetch_klines(client, sym, "1h", 200)
        swings = analysis.detect_swings(df)
        price = float(df["close"].iloc[-1])
        summary = analysis.structure_summary(swings, price)
        if summary.get("trend") == target:
            return "structure_flip", target
    except Exception as e:
        log.debug(f"watch structure check {sym}: {e}")
    return None


def _check_cvd(client, sym: str, w: dict) -> Optional[tuple[str, str]]:
    target = w.get("cvd_signal")
    if not target:
        return None
    try:
        import whale_flow
        cvd = whale_flow.get_spot_cvd(client, sym, lookback_minutes=240)
        if cvd and cvd.get("interpretation") == target:
            return "cvd_signal", target
    except Exception as e:
        log.debug(f"watch cvd check {sym}: {e}")
    return None


def _check_sweep(client, sym: str, w: dict) -> Optional[tuple[str, str]]:
    target = w.get("sweep_printed")
    if not target:
        return None
    try:
        import analysis
        df = analysis.fetch_klines(client, sym, "15m", 100)
        swings = analysis.detect_swings(df)
        sweep = analysis.detect_sweep(df, swings, bars=10)
        if sweep and sweep.get("type") == f"{target}_sweep":
            return "sweep_printed", f"{target}_sweep"
    except Exception as e:
        log.debug(f"watch sweep check {sym}: {e}")
    return None


def evaluate_watches(client, state: dict) -> list[dict]:
    """Check every active watch's conditions. Returns triggered watches.
    Triggered watches are removed from state (one-shot)."""
    cleanup_expired(state)
    watches = _watches(state)
    if not watches:
        return []

    now = time.time()

    # Pre-fetch all prices in one ticker call
    needed = {w["symbol"] for w in watches}
    prices: dict[str, float] = {}
    try:
        for t in client.get_symbol_ticker():
            if t["symbol"] in needed:
                prices[t["symbol"]] = float(t["price"])
    except Exception as e:
        log.warning(f"watch: ticker fetch failed: {e}")

    triggered: list[dict] = []
    survivors: list[dict] = []

    for w in watches:
        sym = w["symbol"]
        last = w.setdefault("last_check", {"price": 0, "structure": 0, "cvd": 0, "sweep": 0})
        hit: Optional[tuple[str, object]] = None

        if w.get("price_lte") is not None or w.get("price_gte") is not None:
            if now - last.get("price", 0) >= PRICE_INTERVAL:
                last["price"] = now
                hit = _check_price(client, sym, w, prices)

        if hit is None and w.get("structure_flip") and now - last.get("structure", 0) >= STRUCTURE_INTERVAL:
            last["structure"] = now
            hit = _check_structure(client, sym, w)

        if hit is None and w.get("cvd_signal") and now - last.get("cvd", 0) >= CVD_INTERVAL:
            last["cvd"] = now
            hit = _check_cvd(client, sym, w)

        if hit is None and w.get("sweep_printed") and now - last.get("sweep", 0) >= SWEEP_INTERVAL:
            last["sweep"] = now
            hit = _check_sweep(client, sym, w)

        if hit is not None:
            trigger_kind, trigger_value = hit
            w_copy = dict(w)
            w_copy["triggered_at_ts"] = now
            w_copy["triggered_by"] = trigger_kind
            w_copy["triggered_value"] = trigger_value
            # Best-effort current price for display even when fired by non-price
            w_copy["triggered_price"] = prices.get(sym)
            triggered.append(w_copy)
        else:
            survivors.append(w)

    state["agent_watches"] = survivors
    if triggered:
        log.info(f"watch: {len(triggered)} triggered, {len(survivors)} still active")
    return triggered


def list_watches(state: dict) -> list[dict]:
    cleanup_expired(state)
    return list(_watches(state))


def remove_watch(state: dict, watch_id: str) -> bool:
    watches = _watches(state)
    before = len(watches)
    state["agent_watches"] = [w for w in watches if w["id"] != watch_id]
    return len(state["agent_watches"]) < before
