"""NWS text products (api.weather.gov/products).

Area Forecast Discussions, Hazardous Weather Outlooks, Record Event Reports,
Public Information Statements and the like are not CAP alerts, so they never
show up in /alerts/active. This source lists each configured product type for
each configured location, then fetches the full text of products it hasn't
archived yet.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

from ..models import Alert
from .base import Source

log = logging.getLogger(__name__)

DEFAULT_TYPES = ["AFD", "HWO", "RER", "PNS", "LSR"]
PRODUCT_KIND = "Product"


def _office(code: str) -> str:
    code = (code or "").strip().upper()
    return code[1:] if len(code) == 4 and code[0] in "KP" else code


def parse_product(p: dict, location: str = "") -> Alert:
    """Turn a /products/{id} document into an Alert."""
    url = p.get("@id") or ""
    pid = url or f"https://api.weather.gov/products/{p.get('id', '')}"
    code = (p.get("productCode") or "").upper()
    name = p.get("productName") or code or "Text Product"
    office = _office(location) or _office(p.get("issuingOffice", ""))
    sent = p.get("issuanceTime") or ""
    return Alert(
        id=pid,
        source="nws-product",
        event=name,
        headline=f"{name} ({code}{office})" if code else name,
        description=(p.get("productText") or "").strip("\n"),
        office=office,
        sender_name=_office(p.get("issuingOffice", "")),
        sent=sent,
        effective=sent,
        # Each issuance is its own product, not an update of the last one.
        thread_key=pid,
        kind=PRODUCT_KIND,
        url=url,
        params={"productCode": code, "wmoCollectiveId": p.get("wmoCollectiveId", "")},
    )


class NWSProductsSource(Source):
    name = "nws-products"

    def __init__(self, base_url: str, user_agent: str, locations: list[str], types: list[str],
                 interval: int = 300, known_ids: Callable[[list[str]], set[str]] | None = None,
                 max_per_type: int = 3, session: requests.Session | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.locations = [_office(x) for x in locations if x.strip()]
        self.types = [t.strip().upper() for t in types if t.strip()]
        self.interval = interval
        self.known_ids = known_ids or (lambda ids: set())
        self.max_per_type = max_per_type
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/ld+json"})
        self.timeout = timeout
        self._last_poll = 0.0

    def _get(self, path: str) -> dict:
        resp = self.session.get(f"{self.base_url}{path}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def poll(self) -> list[Alert] | None:
        if time.monotonic() - self._last_poll < self.interval and self._last_poll:
            return None
        alerts: list[Alert] = []
        errors: list[Exception] = []
        attempts = 0
        for loc in self.locations:
            for ptype in self.types:
                attempts += 1
                try:
                    graph = self._get(f"/products/types/{ptype}/locations/{loc}").get("@graph") or []
                except (requests.RequestException, ValueError) as e:
                    log.warning("listing %s%s failed: %s", ptype, loc, e)
                    errors.append(e)
                    continue
                # Newest few only: enough to catch every issuance between polls without
                # backfilling a week of products the first time a type is turned on.
                graph = sorted(graph, key=lambda g: g.get("issuanceTime") or "", reverse=True)
                recent = [g for g in graph[:self.max_per_type] if g.get("@id")]
                known = self.known_ids([g["@id"] for g in recent])
                for g in reversed(recent):
                    if g["@id"] in known:
                        continue
                    try:
                        alerts.append(parse_product(self._get(f"/products/{g['id']}"), loc))
                    except (requests.RequestException, ValueError, KeyError) as e:
                        log.warning("fetching product %s failed: %s", g.get("@id"), e)
                        errors.append(e)
        if errors and len(errors) >= attempts and not alerts:
            raise errors[-1]  # nothing worked: let the poller record the error
        self._last_poll = time.monotonic()
        return alerts
