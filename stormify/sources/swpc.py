"""Space Weather Prediction Center alerts (services.swpc.noaa.gov).

SWPC's alerts.json lists every watch, warning, alert and summary from roughly
the last few days, each as the full text message. One small conditional request
per poll covers geomagnetic storms (G scale), solar radiation storms (S scale)
and radio blackouts (R scale).

Message shape (abridged):

    Space Weather Message Code: WATA50
    Serial Number: 123
    Issue Time: 2026 Oct 09 1230 UTC

    WATCH: Geomagnetic Storm Category G3 Predicted
    ...
    NOAA Scale: G3 - Strong
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from ..models import Alert
from .base import Source

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"Space Weather Message Code:\s*(\w+)", re.I)
SERIAL_RE = re.compile(r"Serial Number:\s*(\d+)", re.I)
ISSUE_RE = re.compile(r"Issue Time:\s*(\d{4} \w{3} \d{1,2} \d{4}) UTC", re.I)
HEAD_RE = re.compile(r"^(CANCEL (?:WATCH|WARNING|ALERT|SUMMARY)|EXTENDED WARNING|WATCH|WARNING|ALERT|SUMMARY):\s*(.+?)\s*$",
                     re.M)
REF_RE = re.compile(r"(?:Extension to|Cancel) Serial Number:\s*(\d+)", re.I)
VALID_TO_RE = re.compile(r"(?:Now Valid Until|Valid To):\s*(\d{4} \w{3} \d{1,2} \d{4}) UTC", re.I)
SCALE_RE = re.compile(r"NOAA Scale:\s*([GSR])([1-5])\b")
CATEGORY_RE = re.compile(r"\bCategory ([GSR])([1-5])\b")
DAY_LEVEL_RE = re.compile(r"\b([GSR])([1-5]) \(")
KINDEX_RE = re.compile(r"K-index of (\d)")

SCALE_NAMES = {"G": "Geomagnetic Storm", "S": "Solar Radiation Storm", "R": "Radio Blackout"}
SCALE_WORDS = {1: "Minor", 2: "Moderate", 3: "Strong", 4: "Severe", 5: "Extreme"}
SEVERITY = {1: "Minor", 2: "Moderate", 3: "Severe", 4: "Extreme", 5: "Extreme"}
TYPE_KIND = {"WATCH": "Watch", "WARNING": "Warning", "EXTENDED WARNING": "Warning", "ALERT": "Warning",
             "SUMMARY": "Statement"}


def _swpc_time(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), "%Y %b %d %H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def topic_of(text: str, scale: str) -> str:
    t = text.lower()
    if scale == "G" or "geomagnetic" in t or "k-index" in t or "magnetic sudden impulse" in t:
        return "geomagnetic"
    if scale == "S" or "proton" in t or "electron" in t:
        return "radiation"
    if scale == "R" or "x-ray" in t or "radio" in t:
        return "radio"
    return "space"


def parse_message(item: dict) -> Alert | None:
    text = (item.get("message") or "").replace("\r", "").strip()
    head = HEAD_RE.search(text)
    if not head:
        return None
    code_m = CODE_RE.search(text)
    code = code_m.group(1).upper() if code_m else (item.get("product_id") or "").upper()
    serial = (SERIAL_RE.search(text) or [None, ""])[1]
    issued = None
    if im := ISSUE_RE.search(text):
        issued = _swpc_time(im.group(1))
    if not issued:
        try:
            issued = datetime.fromisoformat(str(item.get("issue_datetime", "")).replace(" ", "T")).replace(
                tzinfo=timezone.utc)
        except ValueError:
            issued = datetime.now(timezone.utc)

    mtype, what = head.group(1).upper(), head.group(2)
    cancel = mtype.startswith("CANCEL")
    base_type = mtype.replace("CANCEL ", "") if cancel else mtype

    # Highest level named anywhere: "NOAA Scale: G3", "Category G3", or a watch's per-day "G3 (Strong)".
    levels = [(s, int(n)) for rx in (SCALE_RE, CATEGORY_RE, DAY_LEVEL_RE) for s, n in rx.findall(text)]
    if not levels and (k := KINDEX_RE.search(what)) and int(k.group(1)) >= 5:
        levels = [("G", int(k.group(1)) - 4)]  # Kp 5 = G1 ... Kp 9 = G5
    scale, level = max(levels, key=lambda x: x[1]) if levels else ("", 0)
    topic = topic_of(what, scale)

    tags = [topic]
    if scale:
        tags.append(f"{scale.lower()}{level}")
    if base_type == "ALERT":
        tags.append("observed")
    if level >= 4:
        tags.append("severe-space")

    kind_word = {"WATCH": "Watch", "WARNING": "Warning", "EXTENDED WARNING": "Warning",
                 "ALERT": "Alert", "SUMMARY": "Summary"}[base_type]
    family = SCALE_NAMES.get(scale) or {"geomagnetic": "Geomagnetic", "radiation": "Solar Radiation",
                                        "radio": "Radio Blackout", "space": "Space Weather"}[topic]
    event = f"{family} {kind_word}"

    expires = None
    if vm := VALID_TO_RE.search(text):
        expires = _swpc_time(vm.group(1))
    if not expires:
        # Watches name the days they cover; otherwise give it a day on the dashboard's "active" list.
        expires = issued + (timedelta(days=3) if base_type == "WATCH" else timedelta(hours=24))

    ref = REF_RE.search(text)
    # Codes look like WARK05, ALTK05, EXTK05, WATA50: the part after the 3-letter type is the product family.
    family_code = code[3:] if len(code) > 3 else code
    thread_key = f"swpc:{family_code}:{ref.group(1) if ref else serial}"

    params: dict = {"code": code, "serial": serial, "scale": f"{scale}{level}" if scale else "",
                    "level": level}
    if scale:
        params["scale_desc"] = f"{scale}{level} ({SCALE_WORDS[level]})"
    return Alert(
        id=f"swpc:{code}:{serial or issued.strftime('%Y%m%d%H%M')}",
        source="swpc",
        event=event,
        headline=f"{mtype.title()}: {what}",
        description=text,
        nws_headline=what,
        area_desc="Earth" if topic == "geomagnetic" else "",
        office="SWPC",
        sender_name="NOAA Space Weather Prediction Center",
        message_type="Cancel" if cancel else ("Update" if ref else "Alert"),
        severity=SEVERITY.get(level, "Minor") if scale else "Minor",
        sent=_ts(issued),
        effective=_ts(issued),
        expires=_ts(expires),
        thread_key=thread_key,
        references=[ref.group(1)] if ref else [],
        tags=sorted(set(tags)),
        kind=TYPE_KIND[base_type],
        event_tz="UTC",
        url="https://www.swpc.noaa.gov/products/alerts-watches-and-warnings",
        params=params,
    )


class SWPCSource(Source):
    name = "swpc"

    def __init__(self, url: str, user_agent: str, session: requests.Session | None = None,
                 timeout: float = 30.0):
        self.url = url
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
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
        items = resp.json()
        if not isinstance(items, list):
            raise ValueError("SWPC alerts.json is not a list")
        self._validators = {"etag": resp.headers.get("ETag", ""), "modified": resp.headers.get("Last-Modified", "")}
        out = []
        for item in items:
            try:
                a = parse_message(item)
            except Exception:  # one odd message must never stop the feed
                log.exception("failed to parse SWPC message %s", item.get("product_id"))
                continue
            if a:
                out.append(a)
        return out
