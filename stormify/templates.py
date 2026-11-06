"""Notification templates.

Android truncates collapsed notifications, so the defaults front-load what
matters: tags (EMERGENCY/PDS), event, office, then hail/wind and area.
Rules can override title_template / body_template using any variable below,
in Python format syntax, e.g. "{tags_prefix}{event} {hail_short} · {office}".
Unknown variables render as empty strings rather than erroring.

Variables:
  event, kind, office, sender, area, area_short, headline, nws_headline,
  severity, certainty, urgency, tags, tags_prefix, hail, hail_short, wind,
  wind_short, threat, status_prefix (UPDATE/CANCELLED/TEST), first_line,
  sent, expires (multi-format per your time display setting),
  sent_local, sent_event, sent_z, expires_local, expires_event, expires_z

NHC products add: storm (e.g. "Tropical Storm Isaias", or the basin for an
outlook), product ("Advisory 4A"), adv, basin, pressure, movement, location,
watches (watches/warnings in effect), storm_stats ("45 mph · 1000 mb · ENE at 8 mph").
They have their own defaults (NHC_TITLE / NHC_BODY).

SPC products add: risk (outlook category, e.g. "ENH"), md_number, concerning
("Severe potential...Tornado Watch likely"), watch_prob (percent).
SWPC messages add: scale ("G3"), scale_desc ("G3 (Strong)").
USGS earthquakes add: magnitude ("4.6"), mag_type ("ml"), depth ("8 km"), place,
pager (green/yellow/orange/red), felt (Did You Feel It? reports), mmi,
quake_stats ("Depth 8 km (5 mi) · PAGER yellow · Felt by 120"), and
quake_time (when it struck, in your time display formats).
Daily climate reports (CLI) add: climate (high, low, precip and snow with their
normals and records, one line each).
"""

from __future__ import annotations

import string

from . import climate
from .models import Alert
from .timefmt import fmt_event, fmt_local, fmt_multi, fmt_zulu, parse_iso

DEFAULT_TITLE = "{status_prefix}{tags_prefix}{event} · {office}"
DEFAULT_BODY = "{threat}{area_short}\nUntil {expires}\n{nws_headline}"
# Text products (AFD, HWO, ...) have no area or expiry.
PRODUCT_BODY = "{headline}\nIssued {sent}"
# Daily climate reports (CLI): the high/low/precip summary first.
CLIMATE_BODY = "{nws_headline}\n{climate}"
NHC_TITLE = "{storm} · {product}"
NHC_BODY = "{storm_stats}\n{watches}\n{nws_headline}"
SPC_TITLE = "{status_prefix}{headline}"
SPC_BODY = "{area}\n{threat}{nws_headline}\nUntil {expires}"
SWPC_TITLE = "{status_prefix}{headline}"
SWPC_BODY = "{scale_desc}\nUntil {expires}"
USGS_TITLE = "{status_prefix}{headline}"
USGS_BODY = "{quake_stats}\nAt {quake_time}"
SOURCE_DEFAULTS = {"nhc": (NHC_TITLE, NHC_BODY), "spc": (SPC_TITLE, SPC_BODY), "swpc": (SWPC_TITLE, SWPC_BODY),
                   "usgs": (USGS_TITLE, USGS_BODY)}
NHC_VARS = ("storm", "product", "adv", "basin", "pressure", "movement", "location", "watches", "storm_stats")
PARAM_VARS = ("risk", "md_number", "concerning", "watch_prob", "scale", "scale_desc")
USGS_VARS = ("magnitude", "mag_type", "depth", "place", "pager", "felt", "mmi", "quake_stats")

TAG_LABELS = {
    "emergency": "EMERGENCY",
    "pds": "PDS",
    "considerable": "CONSIDERABLE",
    "destructive": "DESTRUCTIVE",
    "observed": "OBSERVED",
    "tornado-possible": "TOR POSSIBLE",
    "test": "TEST",
}
TAG_ORDER = ["emergency", "pds", "destructive", "considerable", "observed", "tornado-possible"]


class _Safe(dict):
    def __missing__(self, key):
        return ""


class _SafeFormatter(string.Formatter):
    def get_field(self, field_name, args, kwargs):
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, AttributeError, IndexError):
            return "", field_name


_fmt = _SafeFormatter()


def area_short(area_desc: str, max_items: int = 3) -> str:
    """'Arapahoe, CO; Douglas, CO; Elbert, CO' -> 'Arapahoe, Douglas, Elbert CO'."""
    parts = [p.strip() for p in area_desc.split(";") if p.strip()]
    suffix = ""
    split = [p.rsplit(", ", 1) for p in parts]
    states = {s[1] for s in split if len(s) == 2}
    if len(states) == 1 and all(len(s) == 2 for s in split):
        parts = [s[0] for s in split]
        suffix = " " + states.pop()
    else:
        parts = [" ".join(s) for s in split]
    if len(parts) <= max_items:
        return ", ".join(parts) + suffix
    return ", ".join(parts[:max_items]) + suffix + f" +{len(parts) - max_items}"


def build_vars(a: Alert, user_tz: str = "America/Denver", display: list[str] | None = None,
               status: str = "") -> dict:
    tags = [t for t in TAG_ORDER if t in a.tag_set]
    tag_labels = [TAG_LABELS[t] for t in tags]
    hail = f'{a.hail_in:g}" hail' if a.hail_in else ""
    wind = f"{a.wind_mph} mph" if a.wind_mph else ""
    threat = " · ".join(x for x in (hail, wind) if x)
    v = {
        "event": a.event,
        "kind": a.kind,
        "office": a.office or a.sender_name,
        "sender": a.sender_name,
        "area": a.area_desc,
        "area_short": area_short(a.area_desc),
        "headline": a.headline,
        "nws_headline": a.nws_headline,
        "severity": a.severity,
        "certainty": a.certainty,
        "urgency": a.urgency,
        "tags": " ".join(tag_labels),
        "tags_prefix": (" ".join(tag_labels) + " ") if tag_labels else "",
        "hail": hail,
        "hail_short": f'{a.hail_in:g}"' if a.hail_in else "",
        "wind": wind,
        "wind_short": f"{a.wind_mph}mph" if a.wind_mph else "",
        "threat": (threat + " · ") if threat else "",
        "status_prefix": f"{status} " if status else "",
        "first_line": (a.description.strip().splitlines() or [""])[0],
    }
    # Daily climate reports: normals and records, one line each.
    v["climate"] = climate.details(a.description) if climate.is_cli(a) else ""
    for name in NHC_VARS:
        v[name] = str(a.params.get(name, "")) if a.source == "nhc" else ""
    for name in PARAM_VARS:
        v[name] = str(a.params.get(name, "")) if a.source in ("spc", "swpc") else ""
    for name in USGS_VARS:
        val = a.params.get(name) if a.source == "usgs" else None
        v[name] = "" if val in (None, 0) else (f"{val:.1f}" if isinstance(val, float) else str(val))
    origin = parse_iso(a.params.get("origin") or "") if a.source == "usgs" else None
    v["quake_time"] = fmt_multi(a.params["origin"], user_tz, a.event_tz, display) if origin else ""
    for name in ("sent", "expires"):
        iso = getattr(a, name) or ""
        dt = parse_iso(iso)
        v[name] = fmt_multi(iso, user_tz, a.event_tz, display) if dt else ""
        v[f"{name}_local"] = fmt_local(dt, user_tz) if dt else ""
        v[f"{name}_event"] = fmt_event(dt, a.event_tz) if dt else ""
        v[f"{name}_z"] = fmt_zulu(dt) if dt else ""
    return v


def render(template: str, variables: dict) -> str:
    out = _fmt.vformat(template, (), _Safe(variables))
    # Tidy up lines left empty by missing variables.
    lines = [ln.rstrip(" ·") for ln in out.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip()).strip()
