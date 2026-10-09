"""Core pipeline: archive new alerts, run each user's rules, handle updates, deliver."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Callable
from urllib.parse import quote

from .config import Config
from .db import Database, utcnow
from . import images
from .delivery import Channel, DeliveryError, Notification, channels_for_user
from .models import SIGNIFICANT_TAGS, Alert
from .rules import Rule, RuleError, evaluate, load_rules
from .templates import DEFAULT_BODY, DEFAULT_TITLE, PRODUCT_BODY, SOURCE_DEFAULTS, build_vars, render

log = logging.getLogger(__name__)


@dataclass
class Pending:
    user: dict
    alert: Alert
    notification: Notification
    rule_names: list[str]
    reason: str


@dataclass
class Result:
    new_alerts: int = 0
    seen_alerts: int = 0
    pushed: int = 0
    logged: int = 0
    failed: int = 0
    notifications: list[tuple[str, Notification]] = field(default_factory=list)  # (user, n) when dry_run
    new: list[Alert] = field(default_factory=list)


def snapshot(a: Alert) -> dict:
    return {"severity_rank": a.severity_rank, "tags": a.tags, "hail_in": a.hail_in,
            "wind_mph": a.wind_mph, "event": a.event, "level": a.params.get("level")}


def is_significant(prior: dict, a: Alert) -> bool:
    if a.vtec_action == "UPG":
        return True
    if a.severity_rank > prior.get("severity_rank", 0):
        return True
    if (a.tag_set & SIGNIFICANT_TAGS) - set(prior.get("tags", [])):
        return True
    if (a.hail_in or 0) > (prior.get("hail_in") or 0):
        return True
    if (a.wind_mph or 0) > (prior.get("wind_mph") or 0):
        return True
    # SPC outlook risk category or SWPC scale level going up (Slight -> Enhanced, G2 -> G3).
    level = a.params.get("level")
    if isinstance(level, int) and isinstance(prior.get("level"), int) and level > prior["level"]:
        return True
    return a.event != prior.get("event", a.event)


class Engine:
    def __init__(self, db: Database, cfg: Config,
                 channel_factory: Callable[[dict], list[Channel]] = channels_for_user,
                 tz_lookup: Callable[[str], str] | None = None):
        self.db = db
        self.cfg = cfg
        self.channel_factory = channel_factory
        self.tz_lookup = tz_lookup

    # ---- helpers -------------------------------------------------------
    def _rules_for(self, user_id: int) -> list[Rule]:
        try:
            return load_rules(self.db.list_rules(user_id, enabled_only=True))
        except RuleError as e:
            log.error("user %s has an invalid rule, skipping their rules: %s", user_id, e)
            return []

    def _fill_tz(self, a: Alert) -> None:
        if a.event_tz or not a.zones:
            return
        zone = a.zones[0]
        tz = self.db.zone_tz(zone)
        if tz is None and self.tz_lookup:
            tz = self.tz_lookup(zone) or ""
            self.db.set_zone_tz(zone, tz)
        a.event_tz = tz or ""

    def build_notification(self, a: Alert, rule: Rule | None, user: dict, status: str,
                           priority: int) -> Notification:
        settings = user.get("settings", {})
        v = build_vars(a, settings.get("timezone", "America/Denver"), settings.get("time_display"), status)
        default_title, default_body = SOURCE_DEFAULTS.get(a.source, (DEFAULT_TITLE, DEFAULT_BODY))
        if a.kind == "Product" and a.source not in SOURCE_DEFAULTS:
            default_body = PRODUCT_BODY
        title = render((rule.title_template if rule else None) or default_title, v)
        body = render((rule.body_template if rule else None) or default_body, v)
        if "emergency" in a.tag_set:
            priority = 5
        base = self.cfg.public_url.rstrip("/")
        click = f"{base}/?alert={quote(a.id, safe='')}" if base else None
        image = None
        # The phone fetches the picture from the dashboard, so it needs a public address to reach.
        if base and self.cfg.notification_images and images.available() and images.drawable(a) and status != "CANCELLED":
            image = f"{base}/img/{self.db.image_token(a.id)}.png"
        return Notification(title=title, body=body, priority=priority, click_url=click, image_url=image,
                            group=a.thread_key, is_test="test" in a.tag_set,
                            tags=[t for t in a.tags if t != "test"])

    def _decide_push(self, a: Alert, user: dict, rules: list[Rule],
                     batch_prior: dict | None = None) -> tuple[str, str, list[str], Notification | None]:
        """Return (action, reason, rule_names, notification).

        batch_prior is the snapshot of an alert in the same thread that is queued
        to push in this same poll (so an original + its update don't both buzz).
        """
        uid = user["id"]
        decision = evaluate(a, rules)
        prior = self.db.last_delivery(uid, a.thread_key) if a.thread_key else None
        from_batch = False
        if prior is None and batch_prior is not None:
            prior, from_batch = {"snapshot": batch_prior}, True

        if a.status != "Actual":
            return "log", f"not an actual alert (status {a.status})", decision.matched, None

        if a.is_cancel:
            if prior:
                # Cancellations are terse; match them against what the original looked like.
                view = replace(a, tags=sorted(a.tag_set | set(prior["snapshot"].get("tags", []))))
                cancel_rules = [r for r in rules if r.action == "push" and r.on_cancel
                                and r.matches_product(view) and r.matches_scope(view)]
                if cancel_rules:
                    lead = max(cancel_rules, key=lambda r: r.priority)
                    n = self.build_notification(a, lead, user, "CANCELLED", min(lead.priority, 3))
                    return "push", "cancellation of a pushed alert", [r.name for r in cancel_rules], n
            return "log", "cancellation" + ("" if prior else " (never pushed)"), decision.matched, None

        if decision.action != "push":
            return "log", decision.reason, decision.matched, None

        lead = decision.lead_rule
        names = [r.name for r in decision.push_rules]
        if from_batch:
            # The earlier version never reached the phone; send this one as the fresh alert.
            status = "TEST" if "test" in a.tag_set else ""
            return "push", decision.reason, names, self.build_notification(a, lead, user, status, decision.priority)
        if prior:
            if lead.on_update == "new_only":
                return "log", "update suppressed (new only)", names, None
            if lead.on_update == "significant" and not is_significant(prior["snapshot"], a):
                return "log", "update not significant", names, None
            n = self.build_notification(a, lead, user, "UPDATE", decision.priority)
            return "push", "update: " + decision.reason, names, n
        status = "TEST" if "test" in a.tag_set else ""
        return "push", decision.reason, names, self.build_notification(a, lead, user, status, decision.priority)

    # ---- main entry ----------------------------------------------------
    def process(self, alerts: list[Alert], dry_run: bool = False, quiet: bool | None = None) -> Result:
        res = Result()
        if not alerts:
            return res
        if quiet is None:
            quiet = self.cfg.quiet_first_run and not self.db.has_any_alerts()

        ids = [a.id for a in alerts]
        known = self.db.known_alert_ids(ids)
        if known:
            self.db.touch_alerts(list(known))
        new = [a for a in alerts if a.id not in known]
        res.seen_alerts = len(alerts) - len(new)
        res.new_alerts = len(new)
        res.new = new
        if not new:
            return res

        # Oldest first so originals are archived/pushed before their updates.
        new.sort(key=lambda a: a.sent or "")
        for a in new:
            self._fill_tz(a)

        users = self.db.list_users()
        rules_by_user = {u["id"]: self._rules_for(u["id"]) for u in users}
        # Per user: thread_key -> queued push. A newer alert in the same thread
        # replaces the queued one so a single storm buzzes once per poll.
        pending: dict[int, dict[str, Pending]] = {u["id"]: {} for u in users}

        for a in new:
            with self.db.tx() as c:
                self.db.insert_alert(a, c)
            for u in users:
                queue = pending[u["id"]]
                queued = queue.get(a.thread_key)
                action, reason, names, n = self._decide_push(
                    a, u, rules_by_user[u["id"]], snapshot(queued.alert) if queued else None)
                if action == "push" and quiet:
                    action, reason, n = "log", "first run (quiet): " + reason, None
                if action == "push" and n is not None:
                    if queued:
                        with self.db.tx() as c:
                            self.db.record_decision(c, queued.alert.id, u["id"], "log",
                                                    "superseded by a newer update in the same poll",
                                                    queued.rule_names)
                        res.logged += 1
                    queue[a.thread_key or a.id] = Pending(u, a, n, names, reason)
                else:
                    with self.db.tx() as c:
                        self.db.record_decision(c, a.id, u["id"], "log", reason, names)
                    res.logged += 1

        for u in users:
            items = list(pending[u["id"]].values())
            if not items:
                continue
            limit = max(1, int(self.cfg.max_push_per_poll))
            items.sort(key=lambda p: p.notification.priority, reverse=True)
            send, overflow = items[:limit], items[limit:]
            for p in overflow:
                with self.db.tx() as c:
                    self.db.record_decision(c, p.alert.id, u["id"], "log",
                                            "over per-poll push limit: " + p.reason, p.rule_names)
                res.logged += 1
            for p in send:
                ok = self._deliver(u, p.alert, p.notification, dry_run, res)
                with self.db.tx() as c:
                    self.db.record_decision(c, p.alert.id, u["id"], "push" if ok else "failed",
                                            p.reason, p.rule_names)
            if overflow:
                summary = Notification(
                    title=f"{len(overflow)} more alerts held back",
                    body="Busy weather. Lower-priority alerts went to the dashboard feed.",
                    priority=3,
                    click_url=self.cfg.public_url or None,
                )
                self._deliver(u, None, summary, dry_run, res)

        if new:
            last = new[-1]
            self.db.update_heartbeat(last_alert_id=last.id, last_alert_event=last.event, last_alert_at=utcnow())
        return res

    def _deliver(self, user: dict, a: Alert | None, n: Notification, dry_run: bool, res: Result) -> bool:
        if dry_run:
            res.notifications.append((user["name"], n))
            res.pushed += 1
            return True
        channels = self.channel_factory(user.get("settings", {}))
        if not channels:
            log.warning("user %s has no delivery channel configured", user["name"])
        ok_any = False
        for ch in channels:
            try:
                ch.send(n)
                ok_any = True
                self.db.record_delivery(user["id"], a.id if a else None, a.thread_key if a else None,
                                        ch.name, "sent", snapshot=snapshot(a) if a else None)
            except DeliveryError as e:
                log.error("delivery via %s failed: %s", ch.name, e)
                self.db.record_delivery(user["id"], a.id if a else None, a.thread_key if a else None,
                                        ch.name, "failed", error=str(e))
        if ok_any:
            res.pushed += 1
            self.db.update_heartbeat(last_push_at=utcnow())
            self.db.bump_heartbeat(pushes=1)
        else:
            res.failed += 1
        return ok_any
