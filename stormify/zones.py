"""Map shapes for alerts that only list zones.

Most watches, advisories and many statements arrive with no polygon of their own,
just UGC zone codes (COZ039 forecast zones, KSC173 counties). This looks each
zone's outline up on api.weather.gov once, simplifies it so the Pi and the
browser aren't hauling survey-grade borders around, caches it in the database
forever (zones almost never change), and writes the combined shape into the
archived alert so the dashboard and notification images can draw it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable

import requests

from .db import Database

log = logging.getLogger(__name__)

# Two-letter prefixes that are real states/territories; anything else with a Z is a marine zone (GMZ, ANZ, ...).
STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI".split())
UGC_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}$")


def zone_path(zone: str, hints: dict[str, str] | None = None) -> str:
    """API path for a UGC code, preferring the exact URL the alert listed (fire zones, marine zones)."""
    if hints and zone in hints:
        m = re.search(r"(/zones/\w+/[A-Z]{2}[CZ]\d{3})$", hints[zone])
        if m:
            return m.group(1)
    if zone[2] == "C":
        return f"/zones/county/{zone}"
    return f"/zones/{'forecast' if zone[:2] in STATES else 'marine'}/{zone}"


def _perp(p, a, b) -> float:
    (x, y), (x1, y1), (x2, y2) = p, a, b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / (dx * dx + dy * dy) ** 0.5


def simplify_ring(ring: list, tol: float) -> list:
    """Douglas-Peucker on one closed ring, keeping it closed and at least a triangle."""
    if len(ring) <= 4:
        return [[round(x, 3), round(y, 3)] for x, y, *_ in ring]
    pts = [(p[0], p[1]) for p in ring]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _perp(pts[k], pts[i], pts[j])
            if d > best:
                best, idx = d, k
        if idx >= 0 and best > tol:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    out = [[round(p[0], 3), round(p[1], 3)] for p, k in zip(pts, keep) if k]
    if len(out) < 4:  # collapsed: fall back to a coarse sample of the original
        step = max(1, len(pts) // 4)
        out = [[round(p[0], 3), round(p[1], 3)] for p in pts[::step]] + [[round(pts[0][0], 3), round(pts[0][1], 3)]]
    return out


def simplify(geometry: dict | None, tol: float = 0.005) -> list | None:
    """A Polygon/MultiPolygon -> list of polygons (each a list of rings), simplified."""
    if not geometry:
        return None
    t = geometry.get("type")
    if t == "Polygon":
        polys = [geometry["coordinates"]]
    elif t == "MultiPolygon":
        polys = geometry["coordinates"]
    elif t == "GeometryCollection":
        polys = []
        for g in geometry.get("geometries") or []:
            polys.extend(simplify(g, tol) or [])
        return polys or None
    else:
        return None
    out = []
    for poly in polys:
        rings = [simplify_ring(r, tol) for r in poly if len(r) >= 4]
        if rings:
            out.append(rings)
    return out or None


def combine(shapes: Iterable[list]) -> dict | None:
    polys = [p for s in shapes if s for p in s]
    if not polys:
        return None
    return {"type": "MultiPolygon", "coordinates": polys}


class ZoneShapes:
    def __init__(self, db: Database, base_url: str, user_agent: str, session: requests.Session | None = None,
                 timeout: float = 20.0):
        self.db = db
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/geo+json"})
        self.timeout = timeout

    def fetch(self, zone: str, hints: dict[str, str] | None = None) -> bool:
        """Look one zone up and cache it. False only on a network error worth retrying later."""
        try:
            resp = self.session.get(self.base_url + zone_path(zone, hints), timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("zone shape %s failed: %s", zone, e)
            return False
        if resp.status_code >= 500:
            log.warning("zone shape %s: HTTP %s", zone, resp.status_code)
            return False
        shape, tz = None, None
        if resp.ok:
            try:
                data = resp.json()
                shape = simplify(data.get("geometry"))
                tzs = (data.get("properties") or {}).get("timeZone") or []
                tz = tzs[0] if isinstance(tzs, list) and tzs else (tzs or None)
            except ValueError:
                pass
        # A 404 or empty geometry is cached too (as "no shape") so it isn't asked for every poll.
        self.db.set_zone_shape(zone, shape, tz)
        return True

    def shape_for(self, zones: list[str]) -> tuple[dict | None, list[str]]:
        """(combined shape from cache, zones still unknown)."""
        cached = self.db.zone_shapes(zones)
        missing = [z for z in zones if z not in cached]
        return combine(cached.get(z) for z in zones), missing

    def fill(self, alerts: list[dict], budget: int) -> list[str]:
        """Give shapes to these alerts ({id, zones, hints}) as far as `budget` lookups allow.

        Returns the ids that are finished (shape written, or nothing to draw)."""
        done: list[str] = []
        for a in alerts:
            zones = [z for z in a["zones"] if UGC_RE.match(z)]
            shape, missing = self.shape_for(zones)
            for z in missing:
                if budget <= 0:
                    break
                budget -= 1
                self.fetch(z, a.get("hints"))
            if missing:
                shape, missing = self.shape_for(zones)
            if missing:
                continue
            if shape:
                self.db.set_alert_geometry(a["id"], shape, "zones")
            done.append(a["id"])
        return done


def hints_from(params: dict) -> dict[str, str]:
    urls = params.get("affected_zones") or []
    return {u.rsplit("/", 1)[-1]: u for u in urls if isinstance(u, str)}


def pending_from_alert(alert) -> dict | None:
    """The fill() work item for an Alert that has zones but no shape of its own."""
    if alert.geometry or not alert.zones or alert.source != "nws":
        return None
    return {"id": alert.id, "zones": list(alert.zones), "hints": hints_from(alert.params)}


def load_pending(db: Database, limit: int = 500) -> list[dict]:
    """Active NWS alerts still waiting for a shape (for a poller that just restarted)."""
    rows = db.conn.execute(
        "SELECT id, json_extract(data_json, '$.zones'), json_extract(data_json, '$.params.affected_zones')"
        " FROM alerts WHERE source='nws' AND json_extract(data_json, '$.geometry') IS NULL"
        " AND json_array_length(json_extract(data_json, '$.zones')) > 0"
        " AND (expires IS NULL OR expires = '' OR julianday(expires) > julianday('now'))"
        " ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        urls = json.loads(r[2]) if r[2] else []
        out.append({"id": r[0], "zones": json.loads(r[1] or "[]"), "hints": hints_from({"affected_zones": urls})})
    return out
