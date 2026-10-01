"""National Weather Service alerts (api.weather.gov).

The /alerts/active endpoint returns every active watch, warning, advisory and
statement nationwide as GeoJSON, including tropical products issued through
local offices and tsunami products from the Tsunami Warning Centers.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

import requests

from ..models import Alert, kind_for_event
from .base import Source

log = logging.getLogger(__name__)

# /O.NEW.KBOU.TO.W.0042.261001T2310Z-261001T2345Z/
VTEC_RE = re.compile(
    r"/(?P<prod>[OTEX])\.(?P<action>[A-Z]{3})\.(?P<office>[A-Z]{4})\."
    r"(?P<phen>[A-Z]{2})\.(?P<sig>[A-Z])\.(?P<etn>\d{4})\."
)
EMERGENCY_RE = re.compile(r"\b(TORNADO|FLASH FLOOD|EXTREME WIND)\s+EMERGENCY\b", re.I)
PDS_RE = re.compile(r"\bPARTICULARLY\s+DANGEROUS\s+SITUATION\b", re.I)


def _first(params: dict, key: str) -> str:
    v = params.get(key)
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v) if v is not None else ""


def _office_from(vtec: list[str], params: dict, sender_name: str) -> str:
    for v in vtec:
        m = VTEC_RE.search(v)
        if m:
            o = m.group("office")
            return o[1:] if o[0] in "KP" else o
    awips = _first(params, "AWIPSidentifier")
    if len(awips) >= 6:
        return awips[-3:].upper()
    return ""


def derive_tags(event: str, text: str, params: dict, status: str) -> list[str]:
    tags: set[str] = set()
    tor = _first(params, "tornadoDamageThreat").upper()
    ffw = _first(params, "flashFloodDamageThreat").upper()
    tstm = _first(params, "thunderstormDamageThreat").upper()
    det = _first(params, "tornadoDetection").upper()
    if tor == "CATASTROPHIC" or ffw == "CATASTROPHIC" or EMERGENCY_RE.search(text):
        tags.add("emergency")
    if PDS_RE.search(text):
        tags.add("pds")
    if tor == "CONSIDERABLE" or ffw == "CONSIDERABLE":
        tags.add("considerable")
    if tstm == "DESTRUCTIVE":
        tags.add("destructive")
    if det == "OBSERVED":
        tags.add("observed")
    if "POSSIBLE" in det:  # severe thunderstorm warning with "TORNADO...POSSIBLE"
        tags.add("tornado-possible")
    if status and status != "Actual":
        tags.add("test")
    return sorted(tags)


def _to_float(s: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.]", "", s)) if s else None
    except ValueError:
        return None


def _to_int(s: str) -> int | None:
    f = _to_float(s)
    return int(f) if f is not None else None


def parse_feature(feature: dict) -> Alert:
    p: dict[str, Any] = feature.get("properties", {})
    params: dict = p.get("parameters") or {}
    geocode: dict = p.get("geocode") or {}
    vtec = list(params.get("VTEC") or [])
    event = p.get("event", "") or ""
    sender_name = p.get("senderName", "") or ""
    status = p.get("status", "Actual") or "Actual"
    nws_headline = _first(params, "NWSheadline")
    text = "\n".join(x for x in (p.get("headline"), nws_headline, p.get("description"), p.get("instruction")) if x)

    office = _office_from(vtec, params, sender_name)
    tags = derive_tags(event, text, params, status)

    vtec_action = ""
    thread_key = ""
    year = (p.get("sent") or "")[:4]
    for v in vtec:
        m = VTEC_RE.search(v)
        if m:
            vtec_action = m.group("action")
            thread_key = f"{m.group('office')}.{m.group('phen')}.{m.group('sig')}.{m.group('etn')}.{year}"
            break

    refs = [r.get("identifier") or r.get("@id", "") for r in (p.get("references") or [])]
    refs = [r for r in refs if r]
    alert_id = p.get("id") or feature.get("id", "")
    if not thread_key:
        # No VTEC (e.g. Special Weather Statement): thread off the oldest referenced alert.
        thread_key = refs[0] if refs else alert_id

    zones = list(geocode.get("UGC") or [])
    states = sorted({z[:2] for z in zones if len(z) >= 2})

    return Alert(
        id=alert_id,
        source="nws",
        event=event,
        headline=p.get("headline") or "",
        description=p.get("description") or "",
        instruction=p.get("instruction") or "",
        nws_headline=nws_headline,
        area_desc=p.get("areaDesc") or "",
        office=office,
        sender_name=sender_name,
        message_type=p.get("messageType") or "Alert",
        vtec_action=vtec_action,
        status=status,
        severity=p.get("severity") or "Unknown",
        certainty=p.get("certainty") or "",
        urgency=p.get("urgency") or "",
        sent=p.get("sent") or "",
        effective=p.get("effective") or "",
        onset=p.get("onset") or "",
        expires=p.get("expires") or "",
        ends=p.get("ends") or "",
        zones=zones,
        same=list(geocode.get("SAME") or []),
        states=states,
        vtec=vtec,
        thread_key=thread_key,
        references=refs,
        tags=tags,
        hail_in=_to_float(_first(params, "maxHailSize")),
        wind_mph=_to_int(_first(params, "maxWindGust")),
        kind=kind_for_event(event, set(tags)),
        url=feature.get("id") or "",
        geometry=feature.get("geometry"),
        params={k: v for k, v in params.items() if k not in ("VTEC",)},
    )


class NWSAlertsSource(Source):
    name = "nws"

    def __init__(self, base_url: str, user_agent: str, session: requests.Session | None = None,
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/geo+json"})
        self.timeout = timeout
        self._last_modified: str | None = None

    def poll(self) -> list[Alert] | None:
        headers = {}
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified
        resp = self.session.get(f"{self.base_url}/alerts/active", headers=headers, timeout=self.timeout)
        if resp.status_code == 304:
            return None
        resp.raise_for_status()
        self._last_modified = resp.headers.get("Last-Modified") or self._last_modified
        return parse_collection(resp.json())

    def zone_timezone(self, zone_id: str) -> str:
        """Look up the IANA time zone for a UGC zone (callers should cache the result)."""
        ztype = {"Z": "forecast", "C": "county"}.get(zone_id[2:3], "forecast")
        try:
            resp = self.session.get(f"{self.base_url}/zones/{ztype}/{zone_id}", timeout=self.timeout)
            if resp.ok:
                tz = (resp.json().get("properties") or {}).get("timeZone") or []
                return tz[0] if isinstance(tz, list) and tz else (tz or "")
        except requests.RequestException as e:
            log.warning("zone timezone lookup failed for %s: %s", zone_id, e)
        return ""


def parse_collection(data: dict, on_error: Callable[[Exception, dict], None] | None = None) -> list[Alert]:
    alerts = []
    for feature in data.get("features", []):
        try:
            alerts.append(parse_feature(feature))
        except Exception as e:  # one malformed alert must never stop the feed
            log.exception("failed to parse NWS feature %s", feature.get("id"))
            if on_error:
                on_error(e, feature)
    return alerts
