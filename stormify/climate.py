"""Daily climate reports (NWS CLI products): a one-line summary and IEM's infographic.

Offices issue a CLI for each of their climate stations twice a day: early morning for
yesterday, and late afternoon for today so far. The Iowa Environmental Mesonet parses
every CLI and draws an infographic of it (autoplot 218), which is the picture IEMBOT
posts. Stormify links that picture straight from IEM, so the Pi never draws or stores it:
ntfy hands the link to the phone, and the dashboard shows it in the opened card.

The station id and date follow pyIEM's own CLI parser (source letter + the AWIPS id's
last three letters, e.g. KBOU + CLIDEN -> KDEN; the date from the report's headline).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from .models import Alert

IEM_PLOT = "https://mesonet.agron.iastate.edu/plotting/auto/plot/218/network:NWSCLI::station:{station}::date:{day}::_:{pid}.png"

HEADLINE_RE = re.compile(
    r"\.\.\.THE ([A-Z_.\-()/,\s]+?) CLIMATE SUMMARY (?:FOR|FROM)\s+([A-Z]+\s[0-9]+\s+[0-9]{4})(?: CORRECTION)?\.\.\.")
# pyIEM's one hard-coded station: Dodge City's report comes as CLIDGC.
HARDCODED = {"DGC": "KDDC"}
NUM = r"(-?\d+(?:\.\d+)?|T|MM)R?"


def is_cli(a: Alert) -> bool:
    awips = (a.params or {}).get("awipsId") or ""
    return a.source == "nws-product" and len(awips) == 6 and awips.startswith("CLI")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\r", ""))


def report_date(text: str) -> date | None:
    m = HEADLINE_RE.search(_flat(text).upper())
    if not m:
        return None
    raw = m.group(2).split()
    fmt = "%b %d %Y" if len(raw[0]) == 3 else "%B %d %Y"
    try:
        return datetime.strptime(" ".join(raw), fmt).date()
    except ValueError:
        return None


def station_name(text: str) -> str:
    """'DENVER CO' -> 'Denver CO'."""
    m = HEADLINE_RE.search(_flat(text).upper())
    if not m:
        return ""
    words = m.group(1).strip().split()
    out = [w.title() for w in words]
    if len(words) > 1 and len(words[-1]) == 2:
        out[-1] = words[-1]
    return " ".join(out)


def station_id(a: Alert) -> str:
    awips = (a.params or {}).get("awipsId") or ""
    loc = awips[3:]
    if loc in HARDCODED:
        return HARDCODED[loc]
    source = ((a.params or {}).get("issuingOffice") or "").upper()
    first = source[:1] or ("P" if (a.office or "") in {"AFC", "AFG", "AJK", "HFO", "GUM", "PPG"} else "K")
    return first + loc


def _section_value(lines: list[str], header: str, label_re: str) -> str | None:
    """First value on a `label_re` line that follows a line starting with `header`."""
    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith(header)), None)
    if start is None:
        return None
    rx = re.compile(rf"^\s*(?:{label_re})\s+{NUM}(?:\s|$)")
    for ln in lines[start + 1:start + 12]:
        if not ln.strip():
            continue
        if ln[:1].strip() and not ln.strip().startswith(("YESTERDAY", "TODAY")):
            break  # the next section header
        m = rx.match(ln)
        if m:
            return m.group(1)
    return None


def parse(text: str) -> dict:
    """High, low, precipitation and snowfall for the report's day (strings, as printed)."""
    lines = (text or "").replace("\r", "").upper().split("\n")
    out = {
        "high": _section_value(lines, "TEMPERATURE", "MAXIMUM"),
        "low": _section_value(lines, "TEMPERATURE", "MINIMUM"),
        "precip": _section_value(lines, "PRECIPITATION", "YESTERDAY|TODAY"),
        "snow": _section_value(lines, "SNOWFALL", "YESTERDAY|TODAY"),
    }
    flat = _flat(text).upper()
    out["partial"] = bool(re.search(r"VALID (?:TODAY )?AS OF", flat)) or bool(
        re.search(r"^\s*TODAY\s*$", (text or "").upper(), re.M))
    return out


def summary(text: str) -> str:
    """'Denver CO, Nov 1: High 68 · Low 35 · Precip 0.00" · Snow 0.0"' (empty if it can't be read)."""
    day = report_date(text)
    if not day:
        return ""
    v = parse(text)
    bits = []
    if v["high"] and v["high"] != "MM":
        bits.append(f"High {v['high']}")
    if v["low"] and v["low"] != "MM":
        bits.append(f"Low {v['low']}")
    if v["precip"] and v["precip"] != "MM":
        bits.append(f"Precip {v['precip']}" + ('"' if v["precip"] != "T" else ""))
    if v["snow"] and v["snow"] not in ("MM",):
        bits.append(f"Snow {v['snow']}" + ('"' if v["snow"] != "T" else ""))
    name = station_name(text)
    when = f"{day.strftime('%b')} {day.day}" + (" so far" if v["partial"] else "")
    head = f"{name}, {when}" if name else when
    return f"{head}: " + " · ".join(bits) if bits else head


V = r"-?\d+(?:\.\d+)?|T|MM"
TEMP_ROW = re.compile(
    rf"^\s*(?P<item>MAXIMUM|MINIMUM)\s+(?P<obs>{V})(?P<new>R)?\s+(?:\d{{1,4}}\s+[AP]M\s+|MM\s+)?"
    rf"(?P<rec>{V})R?\s+(?P<year>\d{{4}}|MM)\s+(?P<normal>{V})\s+(?P<dep>{V})")
DAY_ROW = re.compile(
    rf"^\s*(?:YESTERDAY|TODAY)\s+(?P<obs>{V})(?P<new>R)?\s+(?P<rec>{V})R?\s+(?P<year>\d{{4}}|MM)\s+"
    rf"(?P<normal>{V})\s+(?P<dep>{V})")


def _row(lines: list[str], header: str, rx: re.Pattern, item: str | None = None) -> dict | None:
    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith(header)), None)
    if start is None:
        return None
    for ln in lines[start + 1:start + 12]:
        if ln[:1].strip():
            break  # the next section header
        m = rx.match(ln)
        if m and (item is None or m.group("item") == item):
            return m.groupdict()
    return None


def _ok(x: str | None) -> bool:
    return bool(x) and x != "MM"


def _line(label: str, row: dict | None, unit: str = "") -> str:
    """'High 68 (normal 59, +9 · record 81 in 1950)', with NEW RECORD when the report flags one."""
    if not row or not _ok(row["obs"]):
        return ""
    u = lambda x: x + (unit if x != "T" else "")  # noqa: E731
    out = f"{label} {u(row['obs'])}"
    extra = []
    if _ok(row["normal"]):
        dep = row["dep"] if _ok(row["dep"]) else ""
        if dep and not dep.startswith("-") and dep != "T" and float(dep) != 0:
            dep = "+" + dep
        extra.append(f"normal {u(row['normal'])}" + (f", {dep}" if dep else ""))
    if _ok(row["rec"]):
        extra.append(f"record {u(row['rec'])}" + (f" in {row['year']}" if _ok(row["year"]) else ""))
    if extra:
        out += " (" + " · ".join(extra) + ")"
    if row.get("new"):
        out = "NEW RECORD " + out
    return out


def details(text: str) -> str:
    """Normals and records for the day, one line each (empty if the report can't be read)."""
    lines = (text or "").replace("\r", "").upper().split("\n")
    rows = [
        _line("High", _row(lines, "TEMPERATURE", TEMP_ROW, "MAXIMUM")),
        _line("Low", _row(lines, "TEMPERATURE", TEMP_ROW, "MINIMUM")),
        _line("Precip", _row(lines, "PRECIPITATION", DAY_ROW), '"'),
        _line("Snow", _row(lines, "SNOWFALL", DAY_ROW), '"'),
    ]
    return "\n".join(r for r in rows if r)


IEM_JSON = "https://mesonet.agron.iastate.edu/json/cli.py"


def iem_has_report(a: Alert, sess: requests.Session, user_agent: str, timeout: float = 20.0) -> bool | None:
    """Has IEM read this report yet? True/False, or None if IEM's listing couldn't be checked.

    IEM draws the picture from whatever report it has for that station and day, so asking before
    it has read the new one gets the old one (or nothing). Its CLI listing names the product
    each day's numbers came from.
    """
    day = report_date(a.description)
    if not day:
        return False
    try:
        resp = sess.get(IEM_JSON, params={"station": station_id(a), "year": day.year},
                        headers={"User-Agent": user_agent}, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json().get("results") or []
    except (requests.RequestException, ValueError, AttributeError):
        return None
    mine = product_id(a)
    st = station_id(a)
    rows = [r for r in rows if isinstance(r, dict) and str(r.get("station") or st).upper() in (st, st[1:])]
    if not rows:
        return None  # not a listing for this station: can't tell
    for row in rows:
        if str(row.get("valid", ""))[:10] != day.isoformat():
            continue
        theirs = str(row.get("product") or "")
        # Same report, or a newer one for that day (a correction, or the next morning's final).
        # IEM stamps a product with its header time, which can be a minute or two off the API's.
        if theirs == mine or _stamp(theirs) >= _stamp(mine) - SLACK:
            return True
    return False


SLACK = timedelta(minutes=10)


def _stamp(pid: str) -> datetime:
    try:
        return datetime.strptime(pid[:12], "%Y%m%d%H%M")
    except ValueError:
        return datetime.min + SLACK


def fetch_image(a: Alert, cache: str, user_agent: str, session=None, timeout: float = 20.0,
                tries: int = 3, wait: float = 5.0) -> bytes | None:
    """IEM's infographic for a CLI, kept on disk once IEM has read this report (None if not yet).

    The phone gets the picture from the dashboard rather than from IEM: a picture from our own
    server comes with a size, which ntfy needs to show it in the notification instead of a
    Download button. The push can beat IEM to a new report, so this checks IEM has it first and
    never keeps (or sends) the previous report's picture.
    """
    url = image_url(a)
    if not url:
        return None
    key = hashlib.sha1(url.encode()).hexdigest()
    path = Path(cache) / "climate" / key[:2] / f"{key}.png"
    if path.exists():
        return path.read_bytes()
    sess = session or requests.Session()
    ready = None
    for attempt in range(tries):
        if attempt:
            time.sleep(wait)
        ready = iem_has_report(a, sess, user_agent, timeout)
        if ready is not False:
            break
    if ready is False:
        return None  # IEM hasn't read it yet: no picture beats yesterday's picture
    try:
        resp = sess.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    except requests.RequestException:
        return None
    if not (resp.ok and resp.content.startswith(b"\x89PNG")):
        return None
    data = resp.content
    if ready:  # only keep a picture we know is this report's
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return data


def product_id(a: Alert) -> str:
    """IEM's id for the product, e.g. 202611042333-KBOU-CDUS45-CLIDEN."""
    try:
        sent = datetime.fromisoformat((a.sent or "").replace("Z", "+00:00")).astimezone(timezone.utc)
        stamp = sent.strftime("%Y%m%d%H%M")
    except ValueError:
        stamp = re.sub(r"\D", "", a.sent or "")[:12]
    params = a.params or {}
    return "-".join(x for x in (stamp, (params.get("issuingOffice") or "").upper(),
                                params.get("wmoCollectiveId") or "", params.get("awipsId") or "") if x)


def image_url(a: Alert) -> str | None:
    """IEM's infographic of a CLI report, or None for anything else."""
    if not is_cli(a):
        return None
    day = report_date(a.description)
    if not day:
        return None
    # IEM caches each picture by its URL. The afternoon "so far" report and next morning's final
    # one share a station and date, so the product id keeps the morning's from showing the afternoon's.
    return IEM_PLOT.format(station=station_id(a), day=day.isoformat(), pid=product_id(a))
