"""Trading-session quality buckets.

Crypto is 24/7 but liquidity quality is not uniform. Empirically, post-NY-close
through pre-London-open hours produce thinner books, more retail-driven moves,
more LTF fakeouts. Rather than refusing to trade then, we raise the bar: a
"thin" session requires a higher confluence score.

Buckets:
  - prime  : London + NY overlap (07:00-21:00 UTC) — best liquidity
  - thin   : everything else (22:00-06:00 UTC + weekends past 21:00 Fri)

Tunables in env:
  AGENT_THIN_HOURS_UTC=22-7   (start-end inclusive; wraps midnight)
  AGENT_THIN_MIN_SCORE=11     (score required during thin hours; default 11 vs prime 9)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

THIN_HOURS_RAW = os.getenv("AGENT_THIN_HOURS_UTC", "22-7")
THIN_MIN_SCORE = int(os.getenv("AGENT_THIN_MIN_SCORE", 11))
PRIME_MIN_SCORE = int(os.getenv("AGENT_PRIME_MIN_SCORE", 9))


def _parse_window(s: str) -> tuple[int, int]:
    try:
        a, b = s.split("-")
        return int(a) % 24, int(b) % 24
    except (ValueError, AttributeError):
        return 22, 7


def current_quality(now: Optional[datetime] = None) -> str:
    """Return 'prime' or 'thin' for the given UTC time."""
    now = now or datetime.now(timezone.utc)
    h = now.hour
    start, end = _parse_window(THIN_HOURS_RAW)
    if start <= end:
        in_thin = start <= h < end
    else:
        # Wraps midnight, e.g. 22-7
        in_thin = h >= start or h < end
    return "thin" if in_thin else "prime"


def required_min_score(now: Optional[datetime] = None) -> int:
    return THIN_MIN_SCORE if current_quality(now) == "thin" else PRIME_MIN_SCORE


def next_prime_window_start(now: Optional[datetime] = None) -> datetime:
    """Next UTC datetime when the session transitions from thin → prime."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    _, end = _parse_window(THIN_HOURS_RAW)
    candidate = now.replace(hour=end, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate
