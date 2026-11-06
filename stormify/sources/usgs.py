"""USGS earthquakes (earthquake.usgs.gov real-time GeoJSON summary feeds).

One small conditional request per poll. The feed lists every event in its window (the default,
2.5_day, is magnitude 2.5 and up worldwide over the last day) and USGS refreshes it about once a
minute. Each event is a point with magnitude, depth, a "12 km NE of Ridgecrest, CA" place, the
PAGER impact level, the "Did You Feel It?" count, peak shaking (MMI) and a tsunami flag.

USGS revises events after the fact (magnitude, location, PAGER). A revision that changes something
that matters becomes a new version in the same thread, so rules treat it like any other update;
felt-report counts creep up all day and don't count on their own.

Feature shape (abridged):

    {"id": "ci40123456", "geometry": {"type": "Point", "coordinates": [-117.6, 35.7, 8.2]},
     "properties": {"mag": 4.6, "magType": "ml", "place": "12 km NE of Ridgecrest, CA",
                    "time": 1762400000000, "updated": 1762400300000, "url": "https://...",
                    "felt": 120, "cdi": 4.1, "mmi": 5.2, "alert": "green", "status": "reviewed",
                    "tsunami": 0, "sig": 326, "type": "earthquake", "title": "M 4.6 - 12 km NE ..."}}
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timedelta, timezone

import requests

from ..models import Alert
from .base import Source

log = logging.getLogger(__name__)

FEED_BASE = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary"
PAGER_RANK = {"green": 1, "yellow": 2, "orange": 3, "red": 4}
PAGER_SEVERITY = {"green": "Minor", "yellow": "Moderate", "orange": "Severe", "red": "Extreme"}
SEVERITY_RANK = {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}
# How long a quake counts as "active" on the dashboard (and stays on the map by default).
ACTIVE_HOURS = 24

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "Puerto Rico": "PR", "U.S. Virgin Islands": "VI", "Guam": "GU",
    "American Samoa": "AS", "Northern Mariana Islands": "MP",
}
STATE_CODES = set(US_STATES.values())


def state_of(place: str) -> str:
    """'12 km NE of Ridgecrest, CA' -> 'CA'; 'Central Alaska' -> 'AK'; '' when it isn't in the US."""
    place = (place or "").strip()
    tail = place.rsplit(", ", 1)[-1].strip()
    if tail.upper() in STATE_CODES and len(tail) == 2:
        return tail.upper()
    if tail in US_STATES:
        return US_STATES[tail]
    for name, code in US_STATES.items():  # "Central Alaska", "Island of Hawaii, Hawaii", "offshore Oregon"
        if place.endswith(name) or place.endswith(name + " region"):
            return code
    return ""


def _ts(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def stats_line(p: dict) -> str:
    """'Depth 8 km (5 mi) · PAGER yellow · Felt by 120 · Shaking MMI 5.2 · Tsunami flag'."""
    parts = []
    if p.get("depth_km") is not None:
        parts.append(f"Depth {p['depth_km']:.0f} km ({p['depth_km'] * 0.621371:.0f} mi)")
    if p.get("pager"):
        parts.append(f"PAGER {p['pager']}")
    if p.get("felt"):
        parts.append(f"Felt by {p['felt']}")
    if p.get("mmi") is not None:
        parts.append(f"Shaking MMI {p['mmi']:.1f}")
    if p.get("tsunami"):
        parts.append("Tsunami flag: check tsunami.gov")
    return " · ".join(parts)


def parse_feature(f: dict) -> Alert | None:
    props = f.get("properties") or {}
    geom = f.get("geometry") or {}
    eid = str(f.get("id") or props.get("code") or "").strip()
    coords = geom.get("coordinates") or []
    if not eid or geom.get("type") != "Point" or len(coords) < 2:
        return None
    lon, lat = _num(coords[0]), _num(coords[1])
    if lon is None or lat is None:
        return None
    depth = _num(coords[2]) if len(coords) > 2 else None
    when = _ts(props.get("time"))
    if when is None:
        return None
    updated = _ts(props.get("updated")) or when
    mag = _num(props.get("mag"))
    mag_type = str(props.get("magType") or "")
    place = str(props.get("place") or props.get("title") or "Unknown location").strip()
    qtype = str(props.get("type") or "earthquake").strip().lower()
    pager = str(props.get("alert") or "").strip().lower()
    pager = pager if pager in PAGER_RANK else ""
    tsunami = bool(props.get("tsunami"))
    felt = int(_num(props.get("felt")) or 0)
    mmi = _num(props.get("mmi"))
    sig = int(_num(props.get("sig")) or 0)
    status = str(props.get("status") or "")
    state = state_of(place)

    event = qtype.title() if qtype != "earthquake" else "Earthquake"
    mag_s = f"M{mag:.1f}" if mag is not None else "M?"
    tags = []
    if mag is not None and mag >= 0:
        tags.append(f"m{min(9, int(math.floor(mag)))}")
    if pager:
        tags.append(f"pager-{pager}")
    if tsunami:
        tags.append("tsunami")
    if felt:
        tags.append("felt")
    if sig >= 600:  # what USGS puts in its "significant" feed
        tags.append("usgs-significant")
    if state:
        tags.append("us")
    if qtype != "earthquake":
        tags.append(qtype.replace(" ", "-"))

    severity = "Minor"
    if mag is not None:
        severity = "Extreme" if mag >= 7 else "Severe" if mag >= 6 else "Moderate" if mag >= 5 else "Minor"
    if pager and SEVERITY_RANK[PAGER_SEVERITY[pager]] > SEVERITY_RANK[severity]:
        severity = PAGER_SEVERITY[pager]

    params = {
        "mag": mag, "mag_type": mag_type, "magnitude": f"{mag:.1f}" if mag is not None else "",
        "depth_km": depth, "depth": f"{depth:.0f} km" if depth is not None else "",
        "place": place, "pager": pager, "felt": felt, "mmi": mmi, "tsunami": tsunami, "sig": sig,
        "status": status, "usgs_id": eid, "lat": lat, "lon": lon, "quake_type": qtype,
        "origin": _iso(when),
        # Whole magnitude, so M5.9 revised to M6.1 counts as a significant update.
        "level": int(math.floor(mag)) if mag is not None else None,
    }
    params["quake_stats"] = stats_line(params)
    # A new version only when something that matters changes (not every new felt report).
    sig_key = "|".join(str(x) for x in (
        f"{mag:.1f}" if mag is not None else "", mag_type, pager, tsunami, status, qtype, place,
        f"{lat:.2f},{lon:.2f}", round(mmi) if mmi is not None else ""))
    version = hashlib.sha1(sig_key.encode()).hexdigest()[:10]
    url = str(props.get("url") or f"https://earthquake.usgs.gov/earthquakes/eventpage/{eid}")

    lines = [f"{mag_s} {mag_type} {event.lower()}, {place}".replace("  ", " "),
             f"Time: {when.strftime('%Y-%m-%d %H:%M:%S')} UTC",
             f"Location: {lat:.3f}, {lon:.3f}" + (f", depth {depth:.1f} km" if depth is not None else "")]
    if pager:
        lines.append(f"PAGER alert level: {pager}")
    if felt:
        lines.append(f"Did You Feel It? reports: {felt}")
    if mmi is not None:
        lines.append(f"Peak shaking (MMI): {mmi:.1f}")
    if tsunami:
        lines.append("Tsunami flag set: tsunami information may exist. Check tsunami.gov for any warning.")
    if status:
        lines.append(f"Review status: {status}")
    lines.append(url)

    return Alert(
        id=f"usgs:{eid}:{version}",
        source="usgs",
        event=event,
        headline=f"{mag_s} {event} · {place}",
        description="\n".join(lines),
        nws_headline=params["quake_stats"],
        area_desc=place,
        office="USGS",
        sender_name="U.S. Geological Survey",
        severity=severity,
        sent=_iso(updated),
        effective=_iso(when),
        onset=_iso(when),
        expires=_iso(when + timedelta(hours=ACTIVE_HOURS)),
        states=[state] if state else [],
        thread_key=f"usgs:{eid}",
        tags=sorted(set(tags)),
        kind="Earthquake",
        event_tz="UTC",
        url=url,
        geometry={"type": "Point", "coordinates": [lon, lat]},
        params=params,
    )


class USGSSource(Source):
    name = "usgs"

    def __init__(self, feed: str, user_agent: str, session: requests.Session | None = None,
                 timeout: float = 30.0, base_url: str = FEED_BASE):
        feed = (feed or "2.5_day").strip()
        self.url = feed if feed.startswith("http") else f"{base_url.rstrip('/')}/{feed.removesuffix('.geojson')}.geojson"
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/geo+json, application/json"})
        self.timeout = timeout
        self._validators: dict[str, str] = {}

    def poll(self) -> list[Alert] | None:
        headers = {}
        if self._validators.get("etag"):
            headers["If-None-Match"] = self._validators["etag"]
        if self._validators.get("modified"):
            headers["If-Modified-Since"] = self._validators["modified"]
        resp = self.session.get(self.url, headers=headers, timeout=self.timeout)
        if resp.status_code == 304:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("features"), list):
            raise ValueError("USGS feed is not a GeoJSON FeatureCollection")
        self._validators = {"etag": resp.headers.get("ETag", ""), "modified": resp.headers.get("Last-Modified", "")}
        out = []
        for f in data["features"]:
            try:
                a = parse_feature(f)
            except Exception:  # one odd event must never stop the feed
                log.exception("failed to parse USGS event %s", (f or {}).get("id"))
                continue
            if a:
                out.append(a)
        return out
