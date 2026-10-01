"""Time display: your local time, the event's local time, and Zulu."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DISPLAY = ["local", "event", "zulu"]


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _zone(name: str | None):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_local(dt: datetime, tz_name: str) -> str:
    z = _zone(tz_name)
    d = dt.astimezone(z) if z else dt
    return f"{_clock(d)} {d.strftime('%Z') or ''}".strip()


def fmt_event(dt: datetime, event_tz: str | None) -> str:
    z = _zone(event_tz)
    if z:
        d = dt.astimezone(z)
        return f"{_clock(d)} {d.strftime('%Z')}"
    # Fall back to the offset the issuing office stamped on the product.
    off = dt.utcoffset()
    if off is None:
        return _clock(dt)
    total = int(off.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{_clock(dt)} UTC{sign}{total // 60}" + (f":{total % 60:02d}" if total % 60 else "")


def fmt_zulu(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H%MZ")


def fmt_multi(iso: str, user_tz: str, event_tz: str | None, display: list[str] | None = None,
              sep: str = " / ") -> str:
    dt = parse_iso(iso)
    if not dt:
        return ""
    display = display or DEFAULT_DISPLAY
    parts: list[str] = []
    for kind in display:
        if kind == "local":
            parts.append(fmt_local(dt, user_tz))
        elif kind == "event":
            parts.append(fmt_event(dt, event_tz))
        elif kind == "zulu":
            parts.append(fmt_zulu(dt))
    # Drop exact duplicates (e.g. you and the event are in the same zone).
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return sep.join(seen)
