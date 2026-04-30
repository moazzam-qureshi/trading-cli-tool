"""Agent-set price watches — V1 (price-only conditions).

When the agent reads a setup as "skip now, but become interesting if price hits X",
it emits decision=WATCH with watch_price_lte / watch_price_gte. AgentWatchJob in the
daemon polls these every few minutes and enqueues a fresh agent eval when triggered.

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


def _watches(state: dict) -> list[dict]:
    return state.setdefault("agent_watches", [])


def add_watch(state: dict, symbol: str, *,
              price_lte: Optional[float] = None,
              price_gte: Optional[float] = None,
              expires_in_hours: float = DEFAULT_EXPIRY_HOURS,
              thesis: str = "",
              original_trigger: str = "") -> tuple[bool, str, Optional[dict]]:
    """Add a new price-watch. Returns (ok, reason, watch_dict)."""
    sym = symbol.upper()
    if price_lte is None and price_gte is None:
        return False, "no price condition (need at least one of price_lte / price_gte)", None
    if price_lte is not None and price_gte is not None and price_lte <= price_gte:
        return False, f"contradictory bounds (lte {price_lte} ≤ gte {price_gte})", None

    expires_in_hours = max(0.5, min(float(expires_in_hours), MAX_EXPIRY_HOURS))

    cleanup_expired(state)
    watches = _watches(state)

    # Dedupe: same symbol + same condition direction → replace
    for w in list(watches):
        if w["symbol"] == sym:
            same_lte = (price_lte is not None and w.get("price_lte") is not None)
            same_gte = (price_gte is not None and w.get("price_gte") is not None)
            if same_lte or same_gte:
                watches.remove(w)
                log.info(f"watch: replaced existing {sym} watch {w['id'][:8]}")
                break

    if len(watches) >= MAX_ACTIVE_WATCHES:
        return False, f"max active watches reached ({len(watches)}/{MAX_ACTIVE_WATCHES})", None

    now = time.time()
    watch = {
        "id": uuid.uuid4().hex,
        "symbol": sym,
        "price_lte": float(price_lte) if price_lte is not None else None,
        "price_gte": float(price_gte) if price_gte is not None else None,
        "set_at_ts": now,
        "expires_at_ts": now + expires_in_hours * 3600,
        "thesis": (thesis or "")[:500],
        "original_trigger": (original_trigger or "")[:120],
    }
    watches.append(watch)
    log.info(f"watch: added {sym} lte={price_lte} gte={price_gte} expires_in={expires_in_hours}h")
    return True, "ok", watch


def cleanup_expired(state: dict) -> int:
    """Drop expired watches in place. Returns count removed."""
    watches = _watches(state)
    now = time.time()
    before = len(watches)
    state["agent_watches"] = [w for w in watches if w["expires_at_ts"] > now]
    removed = before - len(state["agent_watches"])
    if removed:
        log.info(f"watch: dropped {removed} expired")
    return removed


def evaluate_watches(client, state: dict) -> list[dict]:
    """Check every active watch against current price. Returns triggered watches.
    Triggered watches are removed from state (one-shot)."""
    cleanup_expired(state)
    watches = _watches(state)
    if not watches:
        return []

    # Single ticker fetch for all symbols of interest
    needed = {w["symbol"] for w in watches}
    try:
        all_tickers = client.get_symbol_ticker()  # list[{symbol, price}]
        prices = {t["symbol"]: float(t["price"]) for t in all_tickers if t["symbol"] in needed}
    except Exception as e:
        log.warning(f"watch: ticker fetch failed: {e}")
        return []

    triggered: list[dict] = []
    survivors: list[dict] = []
    for w in watches:
        price = prices.get(w["symbol"])
        if price is None:
            survivors.append(w)
            continue
        hit_lte = w.get("price_lte") is not None and price <= w["price_lte"]
        hit_gte = w.get("price_gte") is not None and price >= w["price_gte"]
        if hit_lte or hit_gte:
            w_copy = dict(w)
            w_copy["triggered_at_ts"] = time.time()
            w_copy["triggered_price"] = price
            w_copy["triggered_by"] = "lte" if hit_lte else "gte"
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
