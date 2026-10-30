"""NWS text products (api.weather.gov/products).

Area Forecast Discussions, Hazardous Weather Outlooks, Record Event Reports,
Public Information Statements and the like are not CAP alerts, so they never
show up in /alerts/active. This source lists each configured product type for
each configured location, then fetches the full text of products it hasn't
archived yet.

Two ways to say what to pull:

- `types = ["*"]` takes every product an office issues. It asks for the office's newest
  products in one request (`/products?office=KBOU`), and if the API ignores or rejects that,
  falls back to listing the types the office has issued (`/products/locations/BOU/types`)
  and polling each one.
- `national` names products that aren't tied to a forecast office, as "TYPE" (every
  location, e.g. "TSU") or "TYPE/LOC" (one AWIPS location, e.g. "FFG/MPD" for WPC's
  Mesoscale Precipitation Discussions).
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

DEFAULT_TYPES = ["AFD", "HWO", "RER", "PNS", "LSR"]
# Not tied to a forecast office. SPC outlooks and MDs (SWO) come from the SPC source, which adds
# their maps; watches also come through the alerts feed, but the SEL text has the watch details.
DEFAULT_NATIONAL = ["SEL", "WWP", "FWD", "SWO/D48", "FFG/MPD", "TSU", "TIB"]
PRODUCT_KIND = "Product"

# National centers' issuing offices, named the way the rest of Stormify names them.
CENTERS = {"KWNS": "SPC", "KWNH": "WPC", "KWBC": "WPC", "KWNP": "SWPC", "KNHC": "NHC", "PHFO": "CPHC",
           "PAAQ": "NTWC", "PHEB": "PTWC"}
# Clearer names for national products whose productName is generic.
AWIPS_NAMES = [
    (re.compile(r"^SEL\d$"), "SPC Watch Text"),
    (re.compile(r"^WWP\d$"), "SPC Watch Probabilities"),
    (re.compile(r"^FWDDY1$"), "SPC Day 1 Fire Weather Outlook Discussion"),
    (re.compile(r"^FWDDY2$"), "SPC Day 2 Fire Weather Outlook Discussion"),
    (re.compile(r"^FWDD38$"), "SPC Day 3-8 Fire Weather Outlook Discussion"),
    (re.compile(r"^FFGMPD$"), "WPC Mesoscale Precipitation Discussion"),
    (re.compile(r"^SWOD48$"), "SPC Day 4-8 Convective Outlook Discussion"),
]
AWIPS_RE = re.compile(r"^[A-Z0-9]{4,6}$")
PDS_RE = re.compile(r"PARTICULARLY\s+DANGEROUS\s+SITUATION", re.I)
# Products issued longer ago than this aren't fetched the first time a listing is seen, so turning
# on "*" or a national type doesn't backfill (and push) a day of old products.
FIRST_LOOK = timedelta(hours=2)
# Alaska and Pacific offices' ids start with P (PAFC), everyone else's with K (KBOU).
P_OFFICES = {"AFC", "AFG", "AJK", "HFO", "GUM", "PPG"}


def _office(code: str) -> str:
    code = (code or "").strip().upper()
    return code[1:] if len(code) == 4 and code[0] in "KP" else code


def _issuer(code: str) -> str:
    code = (code or "").strip().upper()
    return CENTERS.get(code) or _office(code)


def awips_id(text: str) -> str:
    """The AWIPS id line (e.g. AFDBOU, SWOMCD) that follows the WMO header in a product's text."""
    lines = [x.strip() for x in (text or "").replace("\r", "").split("\n") if x.strip()][:4]
    for i, line in enumerate(lines):
        if re.match(r"^[A-Z]{4}\d{2} [A-Z]{4} \d{6}", line) and i + 1 < len(lines):
            nxt = lines[i + 1]
            return nxt if AWIPS_RE.match(nxt) else ""
    return ""


def parse_product(p: dict, location: str = "") -> Alert:
    """Turn a /products/{id} document into an Alert."""
    url = p.get("@id") or ""
    pid = url or f"https://api.weather.gov/products/{p.get('id', '')}"
    code = (p.get("productCode") or "").upper()
    text = p.get("productText") or ""
    awips = awips_id(text)
    name = p.get("productName") or code or "Text Product"
    for rx, nice in AWIPS_NAMES:
        if awips and rx.match(awips):
            name = nice
            break
    office = _office(location) or _issuer(p.get("issuingOffice", ""))
    sent = p.get("issuanceTime") or ""
    label = awips or (f"{code}{office}" if code else "")
    tags = ["pds"] if PDS_RE.search(text) else []
    return Alert(
        id=pid,
        source="nws-product",
        event=name,
        headline=f"{name} ({label})" if label else name,
        description=(p.get("productText") or "").strip("\n"),
        office=office,
        sender_name=_issuer(p.get("issuingOffice", "")),
        sent=sent,
        effective=sent,
        # Each issuance is its own product, not an update of the last one.
        thread_key=pid,
        kind=PRODUCT_KIND,
        url=url,
        tags=tags,
        params={"productCode": code, "wmoCollectiveId": p.get("wmoCollectiveId", ""), "awipsId": awips},
    )


def _when(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class NWSProductsSource(Source):
    name = "nws-products"

    def __init__(self, base_url: str, user_agent: str, locations: list[str], types: list[str],
                 interval: int = 300, known_ids: Callable[[list[str]], set[str]] | None = None,
                 max_per_type: int = 3, session: requests.Session | None = None, timeout: float = 30.0,
                 national: list[str] | None = None, max_all: int = 50, max_national: int = 10,
                 rule_offices: Callable[[], list[str]] | None = None):
        self.base_url = base_url.rstrip("/")
        # Offices named in anyone's rules: every product they issue, whatever `types` says.
        self.rule_offices = rule_offices or (lambda: [])
        self.locations = [_office(x) for x in locations if x.strip()]
        self.types = [t.strip().upper() for t in types if t.strip()]
        self.all_types = "*" in self.types
        self.types = [t for t in self.types if t != "*"]
        self.national = [n.strip().upper() for n in (national or []) if n.strip()]
        self.interval = interval
        self.known_ids = known_ids or (lambda ids: set())
        self.max_per_type = max_per_type
        self.max_all = max_all
        self.max_national = max_national
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/ld+json"})
        self.timeout = timeout
        self._last_poll = 0.0
        self._seen: set[str] = set()       # listings looked at since start (see FIRST_LOOK)
        self._office_query: bool | None = None  # does /products?office= work? None = not tried yet
        self._loc_types: dict[str, tuple[float, list[str]]] = {}

    def _get(self, path: str) -> dict:
        resp = self.session.get(f"{self.base_url}{path}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _graph(self, path: str) -> list[dict]:
        graph = self._get(path).get("@graph") or []
        return sorted((g for g in graph if g.get("@id")), key=lambda g: g.get("issuanceTime") or "", reverse=True)

    def _take(self, key: str, graph: list[dict], limit: int, location: str, out: list[Alert],
              errors: list[Exception]) -> None:
        """Fetch the products in a listing that aren't archived yet, oldest first."""
        recent = graph[:limit]
        if key not in self._seen:
            self._seen.add(key)
            cutoff = datetime.now(timezone.utc) - FIRST_LOOK
            # The newest one always counts, so a first look at a type still shows something.
            recent = [g for i, g in enumerate(recent) if i == 0 or (_when(g.get("issuanceTime")) or cutoff) > cutoff] \
                if limit > self.max_per_type else recent
        known = self.known_ids([g["@id"] for g in recent])
        for g in reversed(recent):
            if g["@id"] in known:
                continue
            try:
                out.append(parse_product(self._get(f"/products/{g['id']}"), location))
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("fetching product %s failed: %s", g.get("@id"), e)
                errors.append(e)

    def _office_products(self, loc: str) -> list[dict] | None:
        """An office's newest products in one request, or None if the API doesn't filter by office."""
        if self._office_query is False:
            return None
        prefix = "P" if loc in P_OFFICES else "K"
        try:
            graph = self._graph(f"/products?office={prefix}{loc}&limit={self.max_all}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 404):
                log.info("api.weather.gov rejected /products?office=; listing types per office instead")
                self._office_query = False
                return None
            raise
        mine = [x for x in graph if _office(x.get("issuingOffice", "")) == loc]
        if len(mine) < len(graph):
            # The filter was ignored (we got everyone's products).
            log.info("api.weather.gov ignored /products?office=; listing types per office instead")
            self._office_query = False
            return None
        if self._office_query is None:
            log.info("pulling every product type via /products?office=")
            self._office_query = True
        return mine

    def _types_for(self, loc: str) -> list[str]:
        cached = self._loc_types.get(loc)
        if cached and time.monotonic() - cached[0] < 6 * 3600:
            return cached[1]
        graph = self._get(f"/products/locations/{loc}/types").get("@graph") or []
        codes = sorted({(g.get("productCode") or "").upper() for g in graph} - {""})
        self._loc_types[loc] = (time.monotonic(), codes)
        return codes

    def poll(self) -> list[Alert] | None:
        if time.monotonic() - self._last_poll < self.interval and self._last_poll:
            return None
        alerts: list[Alert] = []
        errors: list[Exception] = []
        attempts = 0

        def listing(key: str, path: str, limit: int, location: str) -> None:
            nonlocal attempts
            attempts += 1
            try:
                graph = self._graph(path)
            except (requests.RequestException, ValueError) as e:
                log.warning("listing %s failed: %s", key, e)
                errors.append(e)
                return
            self._take(key, graph, limit, location, alerts, errors)

        try:
            from_rules = {_office(x) for x in self.rule_offices() if x and x.strip()}
        except Exception:  # a bad rule mustn't stop the configured offices
            log.exception("reading offices from rules failed")
            from_rules = set()
        from_rules -= set(CENTERS.values()) | {"WNS", "WNH", "WNP", "NHC", "SPC", "SWPC", ""}
        plan = [(loc, self.all_types or loc in from_rules) for loc in self.locations]
        plan += [(loc, True) for loc in sorted(from_rules) if loc not in self.locations]
        for loc, everything in plan:
            types = list(self.types) if loc in self.locations else []
            if everything:
                attempts += 1
                try:
                    graph = self._office_products(loc)
                    if graph is not None:
                        self._take(f"*{loc}", graph, self.max_all, loc, alerts, errors)
                    else:
                        types = sorted(set(types) | set(self._types_for(loc)))
                except (requests.RequestException, ValueError) as e:
                    log.warning("listing every product for %s failed: %s", loc, e)
                    errors.append(e)
                if self._office_query:
                    types = []  # already covered
            for ptype in types:
                # A type turned on through "*" may be new to us, so it gets the first-look window too.
                listing(f"{ptype}{loc}", f"/products/types/{ptype}/locations/{loc}",
                        self.max_per_type if ptype in self.types else self.max_all, loc)
        for item in self.national:
            ptype, _, loc = item.partition("/")
            path = f"/products/types/{ptype}" + (f"/locations/{loc}" if loc else "")
            listing(f"national:{item}", path, self.max_national, "")
        if errors and len(errors) >= attempts and not alerts:
            raise errors[-1]  # nothing worked: let the poller record the error
        self._last_poll = time.monotonic()
        return alerts
