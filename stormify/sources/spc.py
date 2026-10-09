"""Storm Prediction Center products: convective outlooks and mesoscale discussions.

Outlooks come from SPC's categorical outlook GeoJSON (one small file per day,
fetched with conditional requests), so each issuance carries its risk areas for
the map. The narrative text is the matching SWODY1/2/3 product from
api.weather.gov, attached when its issuance time lines up.

Mesoscale discussions are SWOMCD products from api.weather.gov. Their text ends
with a LAT...LON polygon and an ATTN...WFO list, which become the map shape and
the offices the discussion concerns.

SPC watches themselves (Tornado Watch, Severe Thunderstorm Watch) already arrive
through the NWS alerts feed.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from ..models import Alert
from .base import Source

log = logging.getLogger(__name__)

SPC_URL = "https://www.spc.noaa.gov"
OUTLOOK_DAYS = (1, 2, 3)

# Categorical risk labels, lowest to highest. The index is the "level" used to spot upgrades.
RISKS = ["TSTM", "MRGL", "SLGT", "ENH", "MDT", "HIGH"]
RISK_NAMES = {"TSTM": "Thunderstorms", "MRGL": "Marginal", "SLGT": "Slight", "ENH": "Enhanced",
              "MDT": "Moderate", "HIGH": "High"}
RISK_SEVERITY = {"TSTM": "Minor", "MRGL": "Minor", "SLGT": "Moderate", "ENH": "Moderate",
                 "MDT": "Severe", "HIGH": "Extreme"}

MD_NUM_RE = re.compile(r"Mesoscale Discussion (\d+)", re.I)
MD_AREAS_RE = re.compile(r"^Areas affected\.\.\.(.+?)\s*$", re.M | re.I)
MD_CONCERNING_RE = re.compile(r"^Concerning\.\.\.(.+?)\s*$", re.M | re.I)
MD_VALID_RE = re.compile(r"^Valid (\d{2})(\d{2})(\d{2})Z - (\d{2})(\d{2})(\d{2})Z", re.M | re.I)
MD_PROB_RE = re.compile(r"Probability of Watch Issuance\.\.\.(\d+) percent", re.I)
MD_SUMMARY_RE = re.compile(r"^SUMMARY\.\.\.(.+?)(?:\n\s*\n|\Z)", re.M | re.S)
MD_ATTN_RE = re.compile(r"^ATTN\.\.\.WFO\.\.\.(.+?)\s*$", re.M)
MD_LATLON_RE = re.compile(r"LAT\.\.\.LON\s+((?:\d{8}\s*)+)")
MD_UGC_RE = re.compile(r"^((?:[A-Z]{2}Z000-)+)\d{6}-\s*$", re.M)
MD_TOR_RE = re.compile(r"MOST PROBABLE PEAK TORNADO INTENSITY\.\.\.(.+?)\s*$", re.M)
MD_WIND_RE = re.compile(r"MOST PROBABLE PEAK WIND GUST\.\.\.(?:UP TO )?(\d+)(?:-(\d+))? MPH", re.M)
MD_HAIL_RE = re.compile(r"MOST PROBABLE PEAK HAIL SIZE\.\.\.(?:UP TO )?([\d.]+)(?:-([\d.]+))? IN", re.M)


def _parse_stamp(s: str) -> datetime | None:
    """'202610091300' or an ISO string -> aware UTC datetime."""
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{12}", s):
        return datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _prop(p: dict, *names: str) -> str:
    for n in names:
        if p.get(n):
            return str(p[n])
    return ""


def parse_outlook(data: dict, day: int, text: str = "") -> Alert | None:
    """A categorical outlook GeoJSON FeatureCollection -> one Alert, or None if it's empty."""
    feats = [f for f in data.get("features") or [] if (f.get("properties") or {}).get("LABEL") in RISKS]
    p0 = (data.get("features") or [{}])[0].get("properties") or {}
    issue = _parse_stamp(_prop(p0, "ISSUE_ISO", "ISSUE"))
    valid = _parse_stamp(_prop(p0, "VALID_ISO", "VALID"))
    expire = _parse_stamp(_prop(p0, "EXPIRE_ISO", "EXPIRE"))
    if not issue:
        return None
    # Lowest risk first, so higher risks draw on top.
    feats.sort(key=lambda f: RISKS.index(f["properties"]["LABEL"]))
    labels = [f["properties"]["LABEL"] for f in feats]
    top = labels[-1] if labels else ""
    risk = RISK_NAMES.get(top, "")
    event = f"Day {day} Convective Outlook"
    if top and top != "TSTM":
        headline = f"Day {day}: {risk} risk"
    elif top == "TSTM":
        headline = f"Day {day}: general thunderstorms"
    else:
        headline = f"Day {day}: no thunderstorms forecast"
    # "...THERE IS AN ENHANCED RISK OF SEVERE THUNDERSTORMS ACROSS ..." paragraphs, each on one line.
    paras = (" ".join(p.split()) for p in re.split(r"\n\s*\n", text))
    risk_lines = " ".join(p.strip(". ") + "." for p in paras if p.upper().startswith("...THERE "))
    geometry = None
    if feats:
        geometry = {"type": "GeometryCollection", "geometries": [f["geometry"] for f in feats if f.get("geometry")]}
    params = {"risk": top, "risk_labels": labels, "level": RISKS.index(top) if top else -1,
              "day": day, "forecaster": _prop(p0, "FORECASTER")}
    tags = [f"risk-{top.lower()}"] if top else []
    return Alert(
        id=f"spc:day{day}otlk:{issue.strftime('%Y%m%d%H%M')}",
        source="spc",
        event=event,
        headline=headline,
        description=text,
        nws_headline=risk_lines,
        area_desc="",
        office="SPC",
        sender_name="NWS Storm Prediction Center",
        severity=RISK_SEVERITY.get(top, "Unknown"),
        sent=_ts(issue),
        effective=_ts(valid or issue),
        expires=_ts(expire) if expire else "",
        # Every update for the same convective day threads together (they share an expiry).
        thread_key=f"spc:day{day}:{(expire or issue).strftime('%Y%m%d')}",
        tags=tags,
        kind="Outlook",
        event_tz="America/Chicago",
        url=f"{SPC_URL}/products/outlook/day{day}otlk.html",
        geometry=geometry,
        params=params,
    )


def _latlon(digits: str) -> list[list[float]]:
    """'34539939 35289907 ...' -> [[lon, lat], ...]. Longitudes under 50 are past 100W."""
    pts = []
    for tok in digits.split():
        if len(tok) != 8:
            continue
        lat = int(tok[:4]) / 100
        lon = int(tok[4:]) / 100
        if lon < 50:
            lon += 100
        pts.append([-lon, lat])
    if pts and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def parse_md(product: dict) -> Alert | None:
    """An api.weather.gov SWOMCD product -> Alert."""
    text = (product.get("productText") or "").replace("\r", "")
    m = MD_NUM_RE.search(text)
    if not m:
        return None
    num = int(m.group(1))
    sent = _parse_stamp(product.get("issuanceTime") or "") or datetime.now(timezone.utc)
    concerning = (MD_CONCERNING_RE.search(text) or [None, ""])[1]
    areas = (MD_AREAS_RE.search(text) or [None, ""])[1]
    prob_m = MD_PROB_RE.search(text)
    summary = MD_SUMMARY_RE.search(text)
    tags: list[str] = []
    c = concerning.lower()
    if "tornado watch" in c:
        tags.append("tornado-watch")
    if "severe thunderstorm watch" in c:
        tags.append("svr-watch")
    if "watch likely" in c or "watches likely" in c:
        tags.append("watch-likely")
    elif "watch possible" in c:
        tags.append("watch-possible")
    elif "watch unlikely" in c:
        tags.append("watch-unlikely")
    for word, tag in (("blizzard", "winter"), ("snow", "winter"), ("freezing rain", "winter"),
                      ("heavy rain", "heavy-rain"), ("flash flood", "heavy-rain")):
        if word in c and tag not in tags:
            tags.append(tag)

    expires = sent + timedelta(hours=2)
    vm = MD_VALID_RE.search(text)
    if vm:
        # "Valid 091845Z - 092045Z": day of month + hhmm, in the issuance month.
        try:
            end = sent.replace(day=int(vm.group(4)), hour=int(vm.group(5)), minute=int(vm.group(6)), second=0)
            if end < sent - timedelta(days=1):  # crossed into the next month
                end = (end.replace(day=1) + timedelta(days=32)).replace(day=int(vm.group(4)))
            expires = end
        except ValueError:
            pass

    geometry = None
    ll = MD_LATLON_RE.search(text)
    if ll:
        ring = _latlon(ll.group(1))
        if len(ring) >= 4:
            geometry = {"type": "Polygon", "coordinates": [ring]}
    attn = MD_ATTN_RE.search(text)
    offices = [o for o in (attn.group(1).split("...") if attn else []) if re.fullmatch(r"[A-Z]{3}", o)]
    ugc = MD_UGC_RE.search(text)
    states = sorted({z[:2] for z in ugc.group(1).split("-") if z}) if ugc else []

    params: dict = {"md_number": num, "concerning": concerning}
    if prob_m:
        params["watch_prob"] = int(prob_m.group(1))
    hail = wind = None
    if hm := MD_HAIL_RE.search(text):
        hail = float(hm.group(2) or hm.group(1))
    if wm := MD_WIND_RE.search(text):
        wind = int(wm.group(2) or wm.group(1))
    if tm := MD_TOR_RE.search(text):
        params["tornado_intensity"] = tm.group(1)

    headline = f"Mesoscale Discussion {num}"
    if concerning:
        headline += f" · {concerning}"
    nws_headline = " ".join(summary.group(1).split()) if summary else ""
    if prob_m:
        nws_headline = f"Watch probability {prob_m.group(1)}%. " + nws_headline
    return Alert(
        id=product.get("@id") or f"spc:md:{sent.year}:{num}",
        source="spc",
        event="Mesoscale Discussion",
        headline=headline,
        description=text.strip("\n"),
        nws_headline=nws_headline.strip(),
        area_desc=areas,
        office="SPC",
        attn_offices=offices,
        sender_name="NWS Storm Prediction Center",
        sent=_ts(sent),
        effective=_ts(sent),
        expires=_ts(expires),
        states=states,
        thread_key=f"spc:md:{sent.year}:{num}",
        tags=tags,
        hail_in=hail,
        wind_mph=wind,
        kind="Discussion",
        event_tz="America/Chicago",
        url=f"{SPC_URL}/products/md/md{num:04d}.html",
        geometry=geometry,
        params=params,
    )


class SPCSource(Source):
    name = "spc"

    def __init__(self, spc_url: str, nws_url: str, user_agent: str, days: tuple[int, ...] = OUTLOOK_DAYS,
                 mds: bool = True, outlook_interval: int = 300,
                 known_ids: Callable[[list[str]], set[str]] | None = None,
                 session: requests.Session | None = None, timeout: float = 30.0):
        self.spc_url = spc_url.rstrip("/")
        self.nws_url = nws_url.rstrip("/")
        self.days = tuple(days)
        self.mds = mds
        self.outlook_interval = outlook_interval
        self.known_ids = known_ids or (lambda ids: set())
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.timeout = timeout
        self._validators: dict[int, dict[str, str]] = {}
        self._last_outlooks = 0.0

    def _nws_json(self, path: str) -> dict:
        resp = self.session.get(f"{self.nws_url}{path}", timeout=self.timeout,
                                headers={"Accept": "application/ld+json"})
        resp.raise_for_status()
        return resp.json()

    def _outlook_text(self, day: int, issue: str) -> str:
        """The SWODY narrative issued closest to the GeoJSON's issue time (within 45 minutes)."""
        want = _parse_stamp(issue)
        if not want:
            return ""
        try:
            graph = self._nws_json(f"/products/types/SWO/locations/DY{day}").get("@graph") or []
        except (requests.RequestException, ValueError) as e:
            log.warning("SPC day %d outlook text listing failed: %s", day, e)
            return ""
        best, gap = None, timedelta(minutes=45)
        for g in graph[:6]:
            t = _parse_stamp(g.get("issuanceTime") or "")
            if t and abs(t - want) <= gap:
                best, gap = g, abs(t - want)
        if not best:
            return ""
        try:
            return (self._nws_json(f"/products/{best['id']}").get("productText") or "").replace("\r", "").strip("\n")
        except (requests.RequestException, ValueError, KeyError) as e:
            log.warning("SPC day %d outlook text failed: %s", day, e)
            return ""

    def _poll_outlooks(self, errors: list[str]) -> list[Alert]:
        out: list[Alert] = []
        for day in self.days:
            headers = {}
            v = self._validators.get(day, {})
            if v.get("etag"):
                headers["If-None-Match"] = v["etag"]
            if v.get("modified"):
                headers["If-Modified-Since"] = v["modified"]
            try:
                resp = self.session.get(f"{self.spc_url}/products/outlook/day{day}otlk_cat.lyr.geojson",
                                        headers=headers, timeout=self.timeout)
                if resp.status_code == 304:
                    continue
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                errors.append(f"day {day} outlook: {e}")
                continue
            self._validators[day] = {k: resp.headers.get(h, "") for k, h in
                                     (("etag", "ETag"), ("modified", "Last-Modified"))}
            a = parse_outlook(data, day)
            if not a or self.known_ids([a.id]):
                continue
            p0 = (data.get("features") or [{}])[0].get("properties") or {}
            text = self._outlook_text(day, _prop(p0, "ISSUE_ISO", "ISSUE"))
            out.append(parse_outlook(data, day, text) or a)
        return out

    def _poll_mds(self, errors: list[str]) -> list[Alert]:
        try:
            graph = self._nws_json("/products/types/SWO/locations/MCD").get("@graph") or []
        except (requests.RequestException, ValueError) as e:
            errors.append(f"mesoscale discussions: {e}")
            return []
        graph = sorted(graph, key=lambda g: g.get("issuanceTime") or "", reverse=True)
        recent = [g for g in graph[:8] if g.get("@id")]
        known = self.known_ids([g["@id"] for g in recent])
        out: list[Alert] = []
        for g in reversed(recent):
            if g["@id"] in known:
                continue
            try:
                a = parse_md(self._nws_json(f"/products/{g['id']}"))
            except (requests.RequestException, ValueError, KeyError) as e:
                errors.append(f"MD {g.get('id')}: {e}")
                continue
            if a:
                out.append(a)
        return out

    def poll(self) -> list[Alert] | None:
        errors: list[str] = []
        alerts: list[Alert] = []
        tried = 0
        if self.days and (not self._last_outlooks or time.monotonic() - self._last_outlooks >= self.outlook_interval):
            tried += len(self.days)
            alerts.extend(self._poll_outlooks(errors))
            self._last_outlooks = time.monotonic()
        if self.mds:
            tried += 1
            alerts.extend(self._poll_mds(errors))
        for e in errors:
            log.warning("SPC %s", e)
        if errors and len(errors) >= tried and not alerts:
            raise requests.RequestException("; ".join(errors))
        return alerts
