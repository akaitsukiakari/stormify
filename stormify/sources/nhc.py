"""National Hurricane Center products (www.nhc.noaa.gov RSS feeds).

NHC publishes one RSS feed per basin (index-at.xml, index-ep.xml, index-cp.xml)
holding the basin's Tropical Weather Outlook plus, for each active storm, the
latest public advisory, forecast advisory, discussion, wind speed probabilities
and any tropical cyclone update, each with its full text. One small request per
basin per poll covers everything, which suits a Pi Zero.

Coastal watches and warnings themselves (Hurricane Warning, Tropical Storm
Watch, ...) already arrive through the NWS alerts feed; this source adds the
storm products around them.
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from ..models import Alert, kind_for_event
from .base import Source

log = logging.getLogger(__name__)

NHC_NS = "{https://www.nhc.noaa.gov}"

BASINS = {
    "at": ("Atlantic", "atlantic"),
    "ep": ("Eastern Pacific", "east-pacific"),
    "cp": ("Central Pacific", "central-pacific"),
}

# AWIPS product category -> (event name, short label for notifications)
PRODUCTS = {
    "TCP": ("Tropical Cyclone Public Advisory", "Advisory"),
    "TCU": ("Tropical Cyclone Update", "Update"),
    "TCM": ("Tropical Cyclone Forecast Advisory", "Forecast Advisory"),
    "TCD": ("Tropical Cyclone Discussion", "Discussion"),
    "PWS": ("Tropical Cyclone Wind Speed Probabilities", "Wind Probabilities"),
    "TWO": ("Tropical Weather Outlook", "Tropical Weather Outlook"),
}

# Watches/warnings named in a public advisory's "IN EFFECT" summary -> tag.
WW_TAGS = {
    ("hurricane", "warning"): "hurricane-warning",
    ("hurricane", "watch"): "hurricane-watch",
    ("tropical storm", "warning"): "ts-warning",
    ("tropical storm", "watch"): "ts-watch",
    ("storm surge", "warning"): "surge-warning",
    ("storm surge", "watch"): "surge-watch",
}
WW_LABELS = {
    "hurricane-warning": "Hurricane Warning", "surge-warning": "Storm Surge Warning",
    "ts-warning": "TS Warning", "hurricane-watch": "Hurricane Watch",
    "surge-watch": "Storm Surge Watch", "ts-watch": "TS Watch",
}

CLASS_TAGS = (
    ("major hurricane", "major-hurricane"),
    ("hurricane", "hurricane"),
    ("subtropical storm", "subtropical-storm"),
    ("subtropical depression", "subtropical-depression"),
    ("tropical storm", "tropical-storm"),
    ("tropical depression", "tropical-depression"),
    ("potential tropical cyclone", "potential-tropical-cyclone"),
    ("post-tropical cyclone", "post-tropical"),
    ("remnants of", "remnants"),
)

# "WTNT34 KNHC 071453" or "ABNT20 KNHC 071136 CCA"
WMO_RE = re.compile(r"^([A-Z]{4}\d{2}) ([A-Z]{4}) (\d{6})(?: ([A-Z]{3}))?\s*$", re.M)
PIL_RE = re.compile(r"^(TCP|TCU|TCM|TCD|PWS|TWO)(AT|EP|CP)(\d?)\s*$", re.M)
ATCF_RE = re.compile(r"\b((?:AL|EP|CP)\d{6})\b")
ADV_RE = re.compile(r"\bNumber\s+(\d+[A-Z]?)\b", re.I)
# Header date line, e.g. "1000 AM CDT Wed Oct 07 2026" or "0900 UTC WED OCT 07 2026"
DATELINE_RE = re.compile(r"^\d{3,4}(?: [AP]M)? [A-Z]{3} [A-Za-z]{3} [A-Za-z]{3} +\d{1,2} \d{4}\s*$", re.M)
WIND_RE = re.compile(r"MAXIMUM SUSTAINED WINDS(?:\.\.\.| NEAR \d+ KTS\.\.\.)(\d+) MPH", re.I)
WIND_KT_RE = re.compile(r"^MAX SUSTAINED WINDS +(\d+) KT", re.M)
PRES_RE = re.compile(r"MINIMUM CENTRAL PRESSURE(?:\.\.\.| )(\d+) MB", re.I)
CENTER_RE = re.compile(r"CENTER LOCATED NEAR +(\d+\.\d)([NS]) +(\d+\.\d)([EW])")
INIT_RE = re.compile(r"^INIT +\d{2}/\d{4}Z +(\d+\.\d)([NS]) +(\d+\.\d)([EW]) +\d+ KT +(\d+) MPH", re.M)
MOVE_RE = re.compile(r"PRESENT MOVEMENT\.\.\.(.+?) OR \d+ DEGREES AT (\d+) MPH", re.I)
STATIONARY_RE = re.compile(r"PRESENT MOVEMENT\.\.\.(STATIONARY)", re.I)
LOC_RE = re.compile(r"^LOCATION\.\.\.(\d+\.\d)([NS]) +(\d+\.\d)([EW])\s*$", re.M)
ABOUT_RE = re.compile(r"^ABOUT (\d+) MI\.\.\.\d+ KM ([NSEW]{1,3}) OF (.+?)\s*$", re.M)
WW_RE = re.compile(r"^An? (Hurricane|Tropical Storm|Storm Surge) (Watch|Warning) is in effect", re.M | re.I)
# "INIT  07/0900Z 22.0N  94.1W   35 KT  40 MPH" / " 12H  07/1800Z 22.2N  92.9W   50 KT  60 MPH"
TRACK_RE = re.compile(r"^\s*(?:INIT|\d+H)\s+\d{2}/\d{4}Z\s+(\d+\.\d)([NS])\s+(\d+\.\d)([EW])", re.M)
NEXT_RE = re.compile(r"Next (?:complete|intermediate) advisory at (\d{3,4}) ([AP]M) ([A-Z]{3})", re.I)
FORMATION_RE = re.compile(
    r"Formation chance through (48 hours|7 days)\.\.\.(\w+)\.\.\.(?:near )?(\d+) percent", re.I)

# Header time zones -> IANA, so "event local time" reads like the product does.
TZ_NAMES = {"AST": "America/Puerto_Rico", "EDT": "America/New_York", "EST": "America/New_York",
            "CDT": "America/Chicago", "CST": "America/Chicago", "MDT": "America/Denver",
            "MST": "America/Denver", "PDT": "America/Los_Angeles", "PST": "America/Los_Angeles",
            "HST": "Pacific/Honolulu"}
TZ_OFFSETS = {"AST": -4, "EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7,
              "PDT": -7, "PST": -8, "HST": -10, "UTC": 0, "GMT": 0}


def clean_text(description: str) -> str:
    """RSS description (HTML in CDATA) -> the product's plain text, from its WMO header on."""
    s = re.sub(r"<br\s*/?>", "\n", description or "", flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = [ln.rstrip() for ln in s.replace("\r", "").splitlines()]
    s = "\n".join(lines)
    m = WMO_RE.search(s)
    if m:
        s = s[m.start():]
    return s.strip()


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _point(lat: str, ns: str, lon: str, ew: str) -> list[float]:
    return [round(float(lon) * (-1 if ew == "W" else 1), 2), round(float(lat) * (-1 if ns == "S" else 1), 2)]


def headline_of(text: str) -> str:
    """The '...BIG NEWS...' lines right after the header, joined onto one line."""
    m = DATELINE_RE.search(text)
    if not m:
        return ""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text[m.end():].strip()):
        p = " ".join(para.split())
        if not p.startswith("..."):
            break
        out.append(p)
    return " ".join(out)


def watches_in_effect(text: str) -> list[str]:
    tags: list[str] = []
    if "IN EFFECT:" not in text.upper():
        return tags
    section = re.split(r"SUMMARY OF WATCHES AND WARNINGS IN EFFECT:", text, flags=re.I)[-1]
    section = re.split(r"\n\s*(?:DISCUSSION AND OUTLOOK|HAZARDS AFFECTING LAND)", section)[0]
    for what, level in WW_RE.findall(section):
        tag = WW_TAGS.get((what.lower(), level.lower()))
        if tag and tag not in tags:
            tags.append(tag)
    order = list(WW_LABELS)
    return sorted(tags, key=order.index)


def forecast_track(text: str) -> list[list[float]]:
    return [_point(*m) for m in TRACK_RE.findall(text)]


def next_advisory(text: str, sent: datetime) -> datetime | None:
    """'Next complete advisory at 400 PM CDT.' -> the next such UTC time after sent."""
    best = None
    for hhmm, ampm, tz in NEXT_RE.findall(text):
        off = TZ_OFFSETS.get(tz.upper())
        if off is None:
            continue
        h, mnt = int(hhmm[:-2]), int(hhmm[-2:])
        h = h % 12 + (12 if ampm.upper() == "PM" else 0)
        local = sent.astimezone(timezone(timedelta(hours=off)))
        cand = local.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if cand <= local:
            cand += timedelta(days=1)
        best = cand if best is None or cand < best else best
    return best


def formation_summary(text: str) -> str:
    """'48h low 10% · 7d medium 40%' per disturbance, or a note that none is expected."""
    parts: list[str] = []
    found = FORMATION_RE.findall(text)
    for i in range(0, len(found), 2):
        pair = found[i:i + 2]
        parts.append(" · ".join(f"{'48h' if span.startswith('48') else '7d'} {lvl.lower()} {pct}%"
                                for span, lvl, pct in pair))
    if parts:
        return "; ".join(parts)
    if re.search(r"(?:formation is|cyclones are) not expected", text, re.I):
        return "No formation expected in the next 7 days"
    return ""


def vitals(text: str) -> dict:
    """Wind (mph), pressure (mb), movement and center from whichever storm product this is."""
    v: dict = {}
    if m := WIND_RE.search(text):
        v["wind"] = int(m.group(1))
    elif m := INIT_RE.search(text):          # discussion's forecast table
        v["wind"] = int(m.group(5))
    elif m := WIND_KT_RE.search(text):       # forecast advisory, knots; NHC rounds mph to 5
        v["wind"] = int(5 * round(int(m.group(1)) * 1.15078 / 5))
    if m := PRES_RE.search(text):
        v["pressure"] = m.group(1)
    if m := MOVE_RE.search(text):
        v["movement"] = f"{m.group(1).strip().upper()} at {m.group(2)} mph"
    elif STATIONARY_RE.search(text):
        v["movement"] = "stationary"
    m = LOC_RE.search(text) or CENTER_RE.search(text) or INIT_RE.search(text)
    if m:
        v["center"] = _point(*m.groups()[:4])
    return v


def _class_tag(storm_type: str) -> str:
    t = storm_type.lower()
    for needle, tag in CLASS_TAGS:
        if needle in t:
            return tag
    return ""


def _cyclones(channel: ET.Element) -> dict[str, dict]:
    """ATCF id -> fields from the feed's 'Summary for ...' items."""
    out: dict[str, dict] = {}
    for item in channel.iter("item"):
        c = item.find(f"{NHC_NS}Cyclone")
        if c is None:
            continue
        d = {child.tag.replace(NHC_NS, ""): (child.text or "").strip() for child in c}
        if d.get("atcf"):
            out[d["atcf"].upper()] = d
    return out


def parse_feed(xml_text: str | bytes, basin: str) -> list[Alert]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"NHC {basin} feed is not valid XML: {e}") from e
    channel = root.find("channel")
    if channel is None:
        return []
    cyclones = _cyclones(channel)
    items = []
    for item in channel.findall("item"):
        if item.find(f"{NHC_NS}Cyclone") is not None:
            continue
        text = clean_text(item.findtext("description") or "")
        pm = PIL_RE.search(text)
        if not pm:
            continue  # graphics, storm surge maps, "no tropical cyclones at this time"
        items.append((item, text, pm))

    # Forecast tracks come from each storm's discussion (or forecast advisory).
    tracks: dict[str, list[list[float]]] = {}
    for _, text, pm in items:
        if pm.group(1) in ("TCD", "TCM"):
            am = ATCF_RE.search(text)
            pts = forecast_track(text)
            if am and pts and (pm.group(1) == "TCD" or am.group(1) not in tracks):
                tracks[am.group(1)] = pts

    alerts = []
    for item, text, pm in items:
        try:
            alerts.append(_alert(item, text, pm, basin, cyclones, tracks))
        except Exception:  # one odd product must never stop the feed
            log.exception("failed to parse NHC item %s", item.findtext("title"))
    return alerts


def _alert(item: ET.Element, text: str, pm: re.Match, basin: str, cyclones: dict,
           tracks: dict) -> Alert:
    cat, pil = pm.group(1), "".join(pm.groups())
    event, label = PRODUCTS[cat]
    basin_name, basin_tag = BASINS.get(basin, (basin.upper(), basin))
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()

    pub = item.findtext("pubDate")
    try:
        sent = parsedate_to_datetime(pub) if pub else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        sent = datetime.now(timezone.utc)
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)

    wmo = WMO_RE.search(text)
    office = "NHC"
    if wmo:
        office = wmo.group(2)[1:] if wmo.group(2)[0] in "KP" else wmo.group(2)
        if office == "HFO":
            office = "CPHC"
        stamp = sent.strftime("%Y%m") + wmo.group(3) + (wmo.group(4) or "")
    else:
        stamp = sent.strftime("%Y%m%d%H%M%S")
    am = ATCF_RE.search(text)
    atcf = am.group(1) if am else ""
    sender = "NWS Central Pacific Hurricane Center" if office == "CPHC" else "NWS National Hurricane Center"

    params: dict = {"product": label, "pil": pil, "basin": basin_name}
    tags = [basin_tag]
    geometry = None
    wind = None
    area = basin_name
    headline = headline_of(text)
    expires = sent + timedelta(hours=6)
    dl = DATELINE_RE.search(text)
    event_tz = TZ_NAMES.get(dl.group(0).split()[-5], "") if dl else ""

    if cat == "TWO":
        storm = basin_name
        headline = formation_summary(text)
        thread_key = f"{office}.TWO{basin.upper()}"
    else:
        cyc = cyclones.get(atcf, {})
        hm = re.search(r"^(.+?) (?:Intermediate |Special )?(?:Public |Forecast )?(?:Advisory|Discussion|"
                       r"Wind Speed Probabilities|Tropical Cyclone Update|Update)", title, re.I)
        storm = f"{cyc['type']} {cyc['name']}" if cyc.get("type") and cyc.get("name") else (
            hm.group(1).strip() if hm else title)
        params["storm"] = storm
        if atcf:
            params["atcf"] = atcf
        adv = ADV_RE.search(title) or ADV_RE.search(text[:600])
        if adv:
            params["adv"] = adv.group(1)
            params["product"] = f"{label} {adv.group(1)}"
            if cat == "TCP" and adv.group(1)[-1].isalpha():
                tags.append("intermediate")
        thread_key = f"{office}.{atcf or pil}.{cat}"

        # Storm vitals from the product's own text, else the feed's storm summary.
        v = vitals(text)
        cw = re.match(r"(\d+)", cyc.get("wind", ""))
        wind = v.get("wind") or (int(cw.group(1)) if cw else None)
        cp = re.match(r"(\d+)", cyc.get("pressure", ""))
        if v.get("pressure") or cp:
            params["pressure"] = f"{v.get('pressure') or cp.group(1)} mb"
        if v.get("movement") or cyc.get("movement"):
            params["movement"] = v.get("movement") or cyc["movement"]
        if cls := _class_tag(storm):
            tags.append(cls)
        if cls == "hurricane" and wind and wind >= 111:
            tags.append("major-hurricane")

        center = v.get("center")
        if not center and cyc.get("center"):
            try:
                lat, lon = (float(x) for x in cyc["center"].split(","))
                center = [round(lon, 2), round(lat, 2)]
            except ValueError:
                center = None
        if center:
            ns = f"{abs(center[1]):.1f}{'N' if center[1] >= 0 else 'S'}"
            ew = f"{abs(center[0]):.1f}{'W' if center[0] < 0 else 'E'}"
            params["location"] = f"{ns} {ew}"
            ab = ABOUT_RE.search(text)
            area = f"{ns} {ew}" + (f", {ab.group(1)} mi {ab.group(2)} of {ab.group(3).title()}"
                                   if ab else f" · {basin_name}")
        if cat in ("TCP", "TCU"):
            ww = watches_in_effect(text)
            tags.extend(ww)
            if ww:
                params["watches"] = ", ".join(WW_LABELS[t] for t in ww)
            if re.search(r"CHANGES WITH THIS ADVISORY:\s*\n\s*(?!None)\S", text, re.I):
                tags.append("ww-changes")
        if cat == "TCP" and center:
            # Draw the storm on the map: its center plus NHC's forecast track.
            geoms: list[dict] = [{"type": "Point", "coordinates": center}]
            track = tracks.get(atcf) or []
            if len(track) > 1:
                geoms.append({"type": "LineString", "coordinates": [center] + track[1:]})
            geometry = {"type": "GeometryCollection", "geometries": geoms}
        if cat == "TCP":
            nxt = next_advisory(text, sent)
            if nxt:
                expires = nxt
        elif cat == "TCU":
            expires = sent + timedelta(hours=3)

    params["storm"] = storm
    stats = [f"{wind} mph" if wind else "", params.get("pressure", ""), params.get("movement", "")]
    params["storm_stats"] = " · ".join(s for s in stats if s)

    return Alert(
        id=f"nhc:{pil}:{stamp}",
        source="nhc",
        event=event,
        headline=title,
        description=text,
        nws_headline=headline,
        area_desc=area,
        office=office,
        sender_name=sender,
        message_type="Alert",
        status="Actual",
        sent=_ts(sent),
        effective=_ts(sent),
        expires=_ts(expires),
        event_tz=event_tz,
        thread_key=thread_key,
        tags=sorted(set(tags)),
        wind_mph=wind,
        kind=kind_for_event(event),
        url=link,
        geometry=geometry,
        params=params,
    )


class NHCSource(Source):
    name = "nhc"

    def __init__(self, base_url: str, user_agent: str, basins: list[str] | None = None,
                 session: requests.Session | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.basins = [b.lower() for b in (basins or list(BASINS))]
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.timeout = timeout
        self._validators: dict[str, dict[str, str]] = {}

    def poll(self) -> list[Alert] | None:
        alerts: list[Alert] = []
        changed = False
        errors = []
        for basin in self.basins:
            headers = {}
            v = self._validators.get(basin, {})
            if v.get("etag"):
                headers["If-None-Match"] = v["etag"]
            if v.get("modified"):
                headers["If-Modified-Since"] = v["modified"]
            try:
                resp = self.session.get(f"{self.base_url}/index-{basin}.xml", headers=headers,
                                        timeout=self.timeout)
                if resp.status_code == 304:
                    continue
                resp.raise_for_status()
                parsed = parse_feed(resp.content, basin)
            except (requests.RequestException, ValueError) as e:
                errors.append(f"{basin}: {e}")
                continue
            self._validators[basin] = {k: resp.headers.get(h, "") for k, h in
                                       (("etag", "ETag"), ("modified", "Last-Modified"))}
            alerts.extend(parsed)
            changed = True
        if errors and not changed:
            raise requests.RequestException("; ".join(errors))
        for e in errors:
            log.warning("NHC feed %s", e)
        return alerts if changed else None
