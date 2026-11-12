"""`--ttl` and `--until` parsing. Pure: the clock is a parameter."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_ttl(text: str) -> int:
    """`90s`, `15m`, `2h` -> seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smh])\s*", str(text))
    if not m or int(m.group(1)) == 0:
        raise ValueError(f"invalid ttl {text!r} (use e.g. 90s, 15m, 2h)")
    return int(m.group(1)) * _UNITS[m.group(2)]


def parse_until(text: str, now: float) -> float:
    """`07:00 Europe/Berlin` (or `07:00` in the host's zone) -> epoch seconds of the next occurrence.

    The wall-clock time is attached to the date in the target zone, so a DST change between now and
    then is handled by the zone rules.
    """
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?:\s+(\S+))?\s*", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"invalid until {text!r} (use e.g. '07:00 Europe/Berlin')")
    hh, mm = int(m.group(1)), int(m.group(2))
    if m.group(3):
        try:
            tz = ZoneInfo(m.group(3))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(f"unknown time zone {m.group(3)!r}") from None
        local = datetime.fromtimestamp(now, tz)
        target = datetime(local.year, local.month, local.day, hh, mm, tzinfo=tz)
        if target.timestamp() <= now:
            nxt = local.date() + timedelta(days=1)
            target = datetime(nxt.year, nxt.month, nxt.day, hh, mm, tzinfo=tz)
        return target.timestamp()
    # No zone: naive local datetimes use the host's real zone rules (including DST) via mktime.
    local = datetime.fromtimestamp(now)
    target = datetime(local.year, local.month, local.day, hh, mm)
    if target.timestamp() <= now:
        nxt = local.date() + timedelta(days=1)
        target = datetime(nxt.year, nxt.month, nxt.day, hh, mm)
    return target.timestamp()
