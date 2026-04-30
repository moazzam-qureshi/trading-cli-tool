"""Macro / scheduled-event awareness — free Forex Factory weekly JSON feed.

Pulls https://nfs.faireconomy.media/ff_calendar_thisweek.json (no auth, no key).
Filters to USD high-impact events (FOMC, CPI, NFP, Core PCE, GDP, Fed speakers
flagged High). Used by:

  * `agent.in_macro_window()` — daemon gate refuses new entries 60min before /
    30min after a USD High event.
  * `trade.py macro` — ad-hoc CLI for the agent / human to inspect upcoming events.

Cached on disk for 6h to avoid hammering the endpoint; stale data fails open
(no gate) rather than crashes — safer to trade through a missed update than to
freeze on connectivity loss.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
CACHE_FILE = ROOT / ".macro_cache.json"
CACHE_TTL_SECONDS = int(os.getenv("MACRO_CACHE_TTL_SECONDS", 6 * 3600))

FF_URL = os.getenv("MACRO_FEED_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
USER_AGENT = "trade-cli/1.0 (+https://github.com/moazzam-qureshi/trading-cli-tool)"

WINDOW_BEFORE_MIN = int(os.getenv("MACRO_WINDOW_BEFORE_MIN", 60))
WINDOW_AFTER_MIN = int(os.getenv("MACRO_WINDOW_AFTER_MIN", 30))

# Event titles we treat as High-impact regardless of feed flag (belt-and-suspenders).
HIGH_IMPACT_KEYWORDS = (
    "fomc", "federal funds rate", "cpi", "core cpi", "core pce", "pce",
    "non-farm", "nonfarm", "nfp", "advance gdp", "unemployment rate",
    "fed chair", "powell speaks",
)


def _fetch_remote() -> Optional[list[dict]]:
    req = urllib.request.Request(FF_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list):
            log.warning("macro: unexpected feed shape (not a list)")
            return None
        return data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.warning(f"macro: feed fetch failed: {e}")
        return None


def _read_cache() -> Optional[dict]:
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_cache(events: list[dict]) -> None:
    CACHE_FILE.write_text(json.dumps({"fetched_at": time.time(), "events": events}), encoding="utf-8")


def get_events(force_refresh: bool = False) -> list[dict]:
    """Return the raw feed events. Uses 6h cache unless force_refresh."""
    cache = _read_cache()
    if not force_refresh and cache and time.time() - cache.get("fetched_at", 0) < CACHE_TTL_SECONDS:
        return cache.get("events", [])

    fresh = _fetch_remote()
    if fresh is not None:
        _write_cache(fresh)
        return fresh

    if cache:
        log.info("macro: using stale cache (fetch failed)")
        return cache.get("events", [])
    return []


def _parse_event_dt(e: dict) -> Optional[datetime]:
    """Forex Factory dates look like '2026-04-30T08:30:00-04:00'. Returns aware UTC."""
    raw = e.get("date")
    if not raw or "T" not in raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_high_impact_usd(e: dict) -> bool:
    if e.get("country") != "USD":
        return False
    if (e.get("impact") or "").lower() == "high":
        return True
    title = (e.get("title") or "").lower()
    return any(kw in title for kw in HIGH_IMPACT_KEYWORDS)


def upcoming_high_impact(within_hours: int = 48, now: Optional[datetime] = None) -> list[dict]:
    """USD high-impact events from now → now+within_hours, sorted ascending."""
    now = now or datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=within_hours)
    out = []
    for e in get_events():
        dt = _parse_event_dt(e)
        if dt is None or not _is_high_impact_usd(e):
            continue
        if now <= dt <= cutoff:
            out.append({**e, "_dt_utc": dt})
    out.sort(key=lambda x: x["_dt_utc"])
    return out


def in_macro_window(now: Optional[datetime] = None) -> tuple[bool, Optional[dict]]:
    """Are we currently inside a macro-event blackout window?
    Returns (in_window, event_dict_or_None). Fails open (False) if feed is empty.
    """
    now = now or datetime.now(timezone.utc)
    before = timedelta(minutes=WINDOW_BEFORE_MIN)
    after = timedelta(minutes=WINDOW_AFTER_MIN)
    for e in get_events():
        dt = _parse_event_dt(e)
        if dt is None or not _is_high_impact_usd(e):
            continue
        if (dt - before) <= now <= (dt + after):
            return True, {**e, "_dt_utc": dt}
    return False, None
