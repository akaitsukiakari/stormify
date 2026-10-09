"""The always-on loop: poll sources, run the engine, keep the heartbeat fresh."""

from __future__ import annotations

import logging
import signal
import time

import requests

from .config import Config
from .db import Database, utcnow
from .engine import Engine
from .sources import NHCSource, NWSAlertsSource, NWSProductsSource, SPCSource, Source, SWPCSource
from .sources.nws_products import DEFAULT_TYPES
from .zones import ZoneShapes, load_pending, pending_from_alert

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, cfg: Config, db: Database, sources: list[Source] | None = None,
                 engine: Engine | None = None):
        self.cfg = cfg
        self.db = db
        self.nws = NWSAlertsSource(cfg.nws_base_url, cfg.user_agent)
        if sources is None:
            sources = [self.nws]
            if cfg.products_locations:
                sources.append(NWSProductsSource(
                    cfg.nws_base_url, cfg.user_agent, cfg.products_locations,
                    cfg.products_types or DEFAULT_TYPES, cfg.products_interval, known_ids=db.known_alert_ids))
            if cfg.nhc_enabled:
                sources.append(NHCSource(cfg.nhc_base_url, cfg.user_agent, cfg.nhc_basins))
            if cfg.spc_enabled:
                sources.append(SPCSource(cfg.spc_base_url, cfg.nws_base_url, cfg.user_agent,
                                         tuple(cfg.spc_outlook_days), cfg.spc_mds, known_ids=db.known_alert_ids))
            if cfg.swpc_enabled:
                sources.append(SWPCSource(cfg.swpc_url, cfg.user_agent))
        self.sources = sources
        self.engine = engine or Engine(db, cfg, tz_lookup=self.nws.zone_timezone)
        self._stop = False
        self._last_prune = 0.0
        self.zones = ZoneShapes(db, cfg.nws_base_url, cfg.user_agent) if cfg.zone_shapes else None
        # Alerts still waiting for a zone outline, oldest first; seeded from the archive on start.
        self.shape_queue: dict[str, dict] = {}
        if self.zones:
            for item in reversed(load_pending(db)):
                self.shape_queue[item["id"]] = item

    def poll_once(self) -> None:
        self.db.update_heartbeat(last_poll_at=utcnow())
        errors = []
        for src in self.sources:
            try:
                alerts = src.poll()
            except (requests.RequestException, ValueError) as e:
                errors.append(f"{src.name}: {e}")
                log.warning("poll of %s failed: %s", src.name, e)
                continue
            if alerts is None:  # not modified
                continue
            # A source turned on for the first time (say SWPC on an existing install) hands back days
            # of past messages; archive those quietly rather than buzzing for old news.
            quiet = None
            if alerts and self.db.has_any_alerts() and not any(
                    self.db.has_source(s) for s in {a.source for a in alerts}):
                quiet = True
            res = self.engine.process(alerts, quiet=quiet)
            if src.name == "nws":
                self.db.update_heartbeat(active_alerts=len(alerts))
            if res.new_alerts:
                log.info("%s: %d new, %d pushed, %d logged, %d failed",
                         src.name, res.new_alerts, res.pushed, res.logged, res.failed)
            if self.zones:
                for a in res.new:
                    item = pending_from_alert(a)
                    if item:
                        self.shape_queue[item["id"]] = item
        self.fill_shapes()
        self.db.bump_heartbeat(polls=1)
        # The heartbeat (and Home Assistant's "Stormify is down") follows the NWS alerts feed. A hiccup
        # at SPC, SWPC or NHC is shown as the last error but doesn't mark the whole poller stale.
        names = [s.name for s in self.sources]
        core = "nws" if "nws" in names else None
        core_failed = any(e.startswith(f"{core}: ") for e in errors) if core else bool(errors)
        fields = {"last_error": "; ".join(errors)[:500] if errors else None}
        if not core_failed:
            fields["last_success_at"] = utcnow()
        self.db.update_heartbeat(**fields)

        keep = int(self.cfg.extra.get("keep_days", 0) or 0)
        if keep and time.time() - self._last_prune > 86400:
            removed = self.db.prune(keep)
            self._last_prune = time.time()
            if removed:
                log.info("pruned %d alerts older than %d days", removed, keep)

    def fill_shapes(self) -> None:
        """Look up a few zone outlines per poll, newest alerts first, so a big watch fills in over a
        couple of minutes instead of stalling the poll."""
        if not self.zones or not self.shape_queue:
            return
        items = list(self.shape_queue.values())[::-1]
        try:
            done = self.zones.fill(items, max(0, int(self.cfg.zone_fetch_per_poll)))
        except Exception:  # shapes are a nicety; never let them break polling
            log.exception("zone shape lookup failed")
            return
        for i in done:
            self.shape_queue.pop(i, None)
        # Don't let a queue of long-expired alerts grow forever.
        while len(self.shape_queue) > 2000:
            self.shape_queue.pop(next(iter(self.shape_queue)))

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        log.info("stormify poller started (every %ss)", self.cfg.poll_interval)
        while not self._stop:
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:  # never let one bad poll kill the service
                log.exception("unexpected error during poll")
                self.db.update_heartbeat(last_error="unexpected error, see logs")
            elapsed = time.monotonic() - started
            sleep_for = max(5.0, self.cfg.poll_interval - elapsed)
            while sleep_for > 0 and not self._stop:
                time.sleep(min(1.0, sleep_for))
                sleep_for -= 1.0
        log.info("stormify poller stopped")
