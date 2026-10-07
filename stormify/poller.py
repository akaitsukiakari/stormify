"""The always-on loop: poll sources, run the engine, keep the heartbeat fresh."""

from __future__ import annotations

import logging
import signal
import time

import requests

from .config import Config
from .db import Database, utcnow
from .engine import Engine
from .sources import NHCSource, NWSAlertsSource, Source

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, cfg: Config, db: Database, sources: list[Source] | None = None,
                 engine: Engine | None = None):
        self.cfg = cfg
        self.db = db
        self.nws = NWSAlertsSource(cfg.nws_base_url, cfg.user_agent)
        if sources is None:
            sources = [self.nws]
            if cfg.nhc_enabled:
                sources.append(NHCSource(cfg.nhc_base_url, cfg.user_agent, cfg.nhc_basins))
        self.sources = sources
        self.engine = engine or Engine(db, cfg, tz_lookup=self.nws.zone_timezone)
        self._stop = False
        self._last_prune = 0.0

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
            res = self.engine.process(alerts)
            if src.name == "nws":
                self.db.update_heartbeat(active_alerts=len(alerts))
            if res.new_alerts:
                log.info("%s: %d new, %d pushed, %d logged, %d failed",
                         src.name, res.new_alerts, res.pushed, res.logged, res.failed)
        self.db.bump_heartbeat(polls=1)
        if errors:
            self.db.update_heartbeat(last_error="; ".join(errors)[:500])
        else:
            self.db.update_heartbeat(last_success_at=utcnow(), last_error=None)

        keep = int(self.cfg.extra.get("keep_days", 0) or 0)
        if keep and time.time() - self._last_prune > 86400:
            removed = self.db.prune(keep)
            self._last_prune = time.time()
            if removed:
                log.info("pruned %d alerts older than %d days", removed, keep)

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
