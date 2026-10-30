"""Dashboard: alert feed + polygon map, rule import/export, settings, health."""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .. import __version__, images
from ..config import Config, cache_dir
from ..db import Database
from ..delivery import DeliveryError, Notification, channels_for_user
from ..engine import Engine
from ..health import health
from ..models import Alert
from ..rules import RuleError, clean_groups, evaluate, load_rules
from ..timefmt import DEFAULT_DISPLAY
from ..zones import ZoneShapes, pending_from_alert

# Most alerts one /api/alerts call returns. A busy 24 h nationwide is several hundred; this keeps
# "Last 7 days" or "All time" from asking the Pi to serialize tens of thousands at once.
FEED_LIMIT = 2000
# Alerts a rule preview runs over (each needs its full text parsed, which is slow on a Pi Zero).
PREVIEW_LIMIT = 1500
MATCH_DAYS = 14        # how far back "recent matches" looks
MATCH_SCAN = 20000     # most archived alerts it reads doing so


def create_app(cfg: Config, db: Database) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )

    def current_user() -> dict | None:
        uid = session.get("uid")
        return db.get_user(user_id=uid) if uid else None

    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                if request.path.startswith("/api/"):
                    return jsonify(error="login required"), 401
                return redirect(url_for("login", next=request.full_path))
            return view(user, *args, **kwargs)
        return wrapped

    # ---- auth ------------------------------------------------------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            user = db.get_user(name=request.form.get("username", "").strip())
            if user and user["password_hash"] and check_password_hash(user["password_hash"],
                                                                     request.form.get("password", "")):
                session.clear()
                session.permanent = True
                session["uid"] = user["id"]
                nxt = request.args.get("next") or "/"
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            error = "Wrong username or password."
        return render_template("login.html", error=error, version=__version__)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- pages -------------------------------------------------------------
    @app.route("/")
    @login_required
    def feed(user):
        return render_template("feed.html", user=user, version=__version__, map_key=cfg.map_key)

    @app.route("/rules")
    @login_required
    def rules_page(user):
        return render_template("rules.html", user=user, version=__version__)

    # ---- health (no login: Home Assistant polls this) ---------------------
    @app.route("/api/health")
    def api_health():
        h = health(db, cfg.heartbeat_stale_seconds)
        return jsonify(h), (200 if h["status"] == "ok" else 503)

    # ---- notification map images (no login: ntfy fetches them for the phone) ------
    renderer = None

    @app.route("/img/<token>.png")
    def notification_image(token):
        nonlocal renderer
        alert_id = db.alert_for_image(token)
        a = db.get_alert(alert_id) if alert_id else None
        if not a or not images.available():
            return Response("not found", status=404, mimetype="text/plain")
        geom, complete = a.geometry, True
        if not geom:
            # The poller may not have reached this alert's zones yet; look them up now.
            zs = ZoneShapes(db, cfg.nws_base_url, cfg.user_agent)
            item = pending_from_alert(a)
            if item:
                zs.fill([item], budget=60)
                geom, missing = zs.shape_for(item["zones"])
                complete = not missing
        if not geom:
            return Response("no shape", status=404, mimetype="text/plain")
        if renderer is None:
            renderer = images.Renderer(cache_dir(cfg), cfg.user_agent, cfg.map_key)
        data = renderer.cached_render(a, geom) if complete else renderer.render(a, geom)
        resp = Response(data, mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=86400" if complete else "no-cache"
        return resp

    # ---- feed API ------------------------------------------------------------
    zone_cache: dict[str, tuple] = {}  # prefix -> (version, etag, json, gzipped json)

    def _list_arg(name: str) -> list[str]:
        out: list[str] = []
        for v in request.args.getlist(name):
            out.extend(x.strip() for x in v.split(",") if x.strip())
        return out

    @app.route("/api/alerts")
    @login_required
    def api_alerts(user):
        hours = request.args.get("hours", type=float, default=24.0)
        since = None
        if hours and hours > 0:
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        action = request.args.get("action") or None
        filters = dict(
            since=since,
            events=_list_arg("event"),
            offices=[o.upper() for o in _list_arg("office")],
            kinds=_list_arg("kind"),
            action=action if action in ("push", "log", "failed") else None,
            q=(request.args.get("q") or "").strip() or None,
            active_only=request.args.get("active") == "1",
        )
        limit = max(1, min(request.args.get("limit", type=int, default=FEED_LIMIT), FEED_LIMIT))
        alert_id = request.args.get("id")
        # The dashboard asks for lite rows (no long text) and fetches full text per alert on demand.
        if request.args.get("lite") != "1":
            rows = db.query_feed(user["id"], limit=limit, **filters)
            total = len(rows) if len(rows) < limit else db.count_feed(user["id"], **filters)
            if alert_id and not any(r["id"] == alert_id for r in rows):
                extra = db.get_alert(alert_id)
                if extra:
                    rows.insert(0, {**extra.to_dict(), "first_seen": "", "action": "log",
                                    "reason": "", "matched_rules": []})
            return jsonify(alerts=rows, total=total, limit=limit)

        # Lite rows arrive from SQLite as finished JSON text; just stitch them together.
        lite = db.query_feed_json(user["id"], limit=limit, **filters)
        total = len(lite) if len(lite) < limit else db.count_feed(user["id"], **filters)
        parts = [text for _, text in lite]
        if alert_id and not any(i == alert_id for i, _ in lite):
            extra = db.get_alert(alert_id)
            if extra:
                d = db.lite_dict(extra.to_dict())
                d.update(first_seen="", action="log", reason="", matched_rules=[])
                parts.insert(0, json.dumps(d))
        resp = Response(f'{{"alerts":[{",".join(parts)}],"total":{total},"limit":{limit}}}',
                        mimetype="application/json")
        # The dashboard refreshes every minute and usually nothing has changed: answer those with a
        # bodyless 304 so the Pi isn't pushing megabytes over Wi-Fi and the tunnel for nothing.
        resp.headers["Cache-Control"] = "private, no-cache"
        resp.add_etag()
        return resp.make_conditional(request)

    @app.route("/api/zones/<prefix>")
    @login_required
    def api_zones(user, prefix):
        """Zone outlines for one state ("COZ" forecast zones, "KSC" counties). The lite feed lists an
        alert's zones instead of repeating their outlines, and the dashboard draws them from these."""
        if not re.fullmatch(r"[A-Z]{2}[CZ]", prefix):
            return jsonify(error="bad prefix"), 400
        # Built and gzipped once per change: all of them together run to megabytes over the Pi's Wi-Fi.
        version = db.zone_shapes_version(prefix)
        hit = zone_cache.get(prefix)
        if not hit or hit[0] != version:
            body = db.zone_shapes_json(prefix).encode()
            hit = zone_cache[prefix] = (version, hashlib.sha1(body).hexdigest(), body,
                                        gzip.compress(body, compresslevel=6, mtime=0))
        _, etag, body, packed = hit
        gz = "gzip" in (request.headers.get("Accept-Encoding") or "")
        resp = Response(packed if gz else body, mimetype="application/json")
        if gz:
            resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
        # Zones almost never change; a zone looked up after this was cached makes the dashboard ask again.
        resp.headers["Cache-Control"] = "private, max-age=86400"
        resp.set_etag(etag)
        return resp.make_conditional(request)

    @app.route("/api/alert")
    @login_required
    def api_alert(user):
        a = db.get_alert(request.args.get("id") or "")
        if not a:
            return jsonify(error="not found"), 404
        return jsonify(alert=a.to_dict())

    @app.route("/api/meta")
    @login_required
    def api_meta(user):
        s = user["settings"]
        return jsonify(
            user=user["name"],
            timezone=s.get("timezone", "America/Denver"),
            time_display=s.get("time_display", DEFAULT_DISPLAY),
            events=db.distinct_values("event"),
            offices=db.distinct_values("office"),
            kinds=db.distinct_values("kind"),
        )

    # ---- rules: export / import (JSON, shareable) ---------------------------
    @app.route("/api/rules", methods=["GET"])
    @login_required
    def api_rules_get(user):
        rules = db.list_rules(user["id"])
        for r in rules:
            r.pop("id", None)
        if request.args.get("download"):
            return Response(json.dumps({"stormify_rules": 1, "rules": rules}, indent=2),
                            mimetype="application/json",
                            headers={"Content-Disposition": "attachment; filename=stormify-rules.json"})
        return jsonify(rules=rules)

    @app.route("/api/rules", methods=["PUT"])
    @login_required
    def api_rules_put(user):
        payload = request.get_json(silent=True) or {}
        rules = payload.get("rules") if isinstance(payload, dict) else payload
        if not isinstance(rules, list):
            return jsonify(error="expected {\"rules\": [...]}"), 400
        try:
            load_rules([{k: v for k, v in r.items() if k != "id"} for r in rules])
        except (RuleError, TypeError) as e:
            return jsonify(error=str(e)), 400
        db.replace_rules(user["id"], rules)
        return jsonify(ok=True, count=len(rules))

    @app.route("/api/rules/preview", methods=["POST"])
    @login_required
    def api_rules_preview(user):
        """Run draft rules over recent alerts: what each would have pushed or logged."""
        payload = request.get_json(silent=True) or {}
        hours = max(1.0, min(float(payload.get("hours") or 48), 24 * 7))
        try:
            rules = load_rules([{k: v for k, v in r.items() if k != "id"} for r in payload.get("rules") or []],
                               payload.get("office_groups") or user["settings"].get("office_groups"))
        except (RuleError, TypeError, AttributeError) as e:
            return jsonify(error=str(e)), 400
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = db.query_feed(user["id"], since=since, limit=PREVIEW_LIMIT)
        per = [{"push": 0, "log": 0, "events": {}} for _ in rules]
        pushes = 0
        for row in rows:
            a = Alert.from_dict(row)
            decision = evaluate(a, rules)
            if decision.action == "push":
                pushes += 1
            for i, r in enumerate(rules):
                if r.name in decision.matched and r.enabled:
                    key = "push" if r.action == "push" else "log"
                    per[i][key] += 1
                    per[i]["events"][a.event] = per[i]["events"].get(a.event, 0) + 1
        for p in per:
            p["events"] = sorted(p["events"].items(), key=lambda kv: -kv[1])[:5]
        return jsonify(alerts=len(rows), hours=hours, pushes=pushes, rules=per,
                       truncated=len(rows) >= PREVIEW_LIMIT)

    # ---- one rule: recent matches and test pushes -----------------------------
    def _one_rule(payload: dict, user: dict):
        r = payload.get("rule")
        if not isinstance(r, dict):
            raise RuleError("expected {\"rule\": {...}}")
        r = {k: v for k, v in r.items() if k != "id"}
        r["enabled"] = True  # a rule that's switched off still gets tested
        return load_rules([r], payload.get("office_groups") or user["settings"].get("office_groups"))[0]

    def _send(user: dict, n: Notification):
        channels = channels_for_user(user["settings"])
        if not channels:
            return jsonify(error="No delivery channel configured. Set up ntfy first."), 400
        errors = []
        for ch in channels:
            try:
                ch.send(n)
                db.record_delivery(user["id"], None, None, ch.name, "test")
            except DeliveryError as e:
                errors.append(f"{ch.name}: {e}")
        if errors:
            return jsonify(error="; ".join(errors)), 502
        return jsonify(ok=True, title=n.title, body=n.body)

    def _test_notification(user: dict, rule, a: Alert) -> Notification:
        n = Engine(db, cfg).build_notification(a, rule, user, "TEST", rule.priority)
        n.is_test = True
        return n

    def _matches(user: dict, rule, limit: int):
        since = (datetime.now(timezone.utc) - timedelta(days=MATCH_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out, scanned = [], 0
        pre = lambda event, kind, tags: rule.matches_product(Alert(id="", source="", event=event, kind=kind, tags=tags))
        for a, first_seen in db.iter_alerts(since, MATCH_SCAN, pre):
            scanned += 1
            if a is None or not (rule.matches_product(a) and rule.matches_scope(a)):
                continue
            ok, why = rule.passes_filters(a)
            out.append((a, first_seen, "" if ok else why))
            if len(out) >= limit:
                break
        return out, scanned

    @app.route("/api/rules/matches", methods=["POST"])
    @login_required
    def api_rule_matches(user):
        """The newest archived alerts one (draft) rule matches, with the push each would send."""
        payload = request.get_json(silent=True) or {}
        try:
            rule = _one_rule(payload, user)
        except (RuleError, TypeError, AttributeError) as e:
            return jsonify(error=str(e)), 400
        limit = max(1, min(int(payload.get("limit") or 40), 100))
        found, scanned = _matches(user, rule, limit)
        eng = Engine(db, cfg)
        rows = []
        for a, first_seen, filtered in found:
            n = eng.build_notification(a, rule, user, "", rule.priority)
            rows.append({"id": a.id, "event": a.event, "office": a.office, "kind": a.kind, "sent": a.sent,
                         "first_seen": first_seen, "cancel": a.is_cancel, "filtered": filtered,
                         "title": n.title, "body": n.body})
        return jsonify(matches=rows, scanned=scanned, days=MATCH_DAYS, action=rule.action,
                       truncated=scanned >= MATCH_SCAN)

    @app.route("/api/rules/test-push", methods=["POST"])
    @login_required
    def api_rule_test_push(user):
        """Push one alert to the phone the way this rule would format it (marked TEST).

        Without an alert id it uses the rule's newest match, or a made-up sample if nothing matches yet."""
        payload = request.get_json(silent=True) or {}
        try:
            rule = _one_rule(payload, user)
        except (RuleError, TypeError, AttributeError) as e:
            return jsonify(error=str(e)), 400
        a = None
        sample = False
        if payload.get("alert_id"):
            a = db.get_alert(str(payload["alert_id"]))
            if not a:
                return jsonify(error="That alert isn't in the archive any more."), 404
        else:
            found, _ = _matches(user, rule, 1)
            if found:
                a = found[0][0]
        if a is None:
            sample = True
            offices = list(rule.scope.get("offices") or []) + sorted(rule.group_offices)
            office = str(offices[0]).upper() if offices else "BOU"
            event = rule.events[0] if rule.events else "Sample Alert"
            a = Alert(id="stormify:test-sample", source="test", event=event,
                      headline=f"Sample {event}", office=office, area_desc="Sample County",
                      nws_headline="Nothing has matched this rule yet, so this is a made-up sample.",
                      sent=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                      expires=(datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                      kind="Other", tags=sorted(rule.tags_any))
        res = _send(user, _test_notification(user, rule, a))
        if isinstance(res, tuple):
            return res
        body = res.get_json()
        body.update(sample=sample, alert_id=None if sample else a.id, event=a.event)
        return jsonify(body)

    # ---- settings ------------------------------------------------------------
    @app.route("/api/settings", methods=["GET", "PUT"])
    @login_required
    def api_settings(user):
        s = dict(user["settings"])
        if request.method == "PUT":
            body = request.get_json(silent=True) or {}
            if "timezone" in body:
                s["timezone"] = str(body["timezone"])
            if "time_display" in body:
                s["time_display"] = [x for x in body["time_display"] if x in ("local", "event", "zulu")]
            if "channels" in body and isinstance(body["channels"], dict):
                s["channels"] = body["channels"]
            if "office_groups" in body:
                try:
                    s["office_groups"] = clean_groups(body["office_groups"])
                except RuleError as e:
                    return jsonify(error=str(e)), 400
            db.update_settings(user["id"], s)
        safe = json.loads(json.dumps(s))
        for ch in (safe.get("channels") or {}).values():
            if isinstance(ch, dict) and ch.get("token"):
                ch["token"] = "********"
        return jsonify(settings=safe)

    @app.route("/api/test-notification", methods=["POST"])
    @login_required
    def api_test(user):
        n = Notification(title="TEST Tornado Warning · BOU",
                         body='2" hail · Arapahoe, Douglas\nThis is a Stormify test notification.',
                         priority=4, is_test=True, click_url=cfg.public_url or None)
        channels = channels_for_user(user["settings"])
        if not channels:
            return jsonify(error="No delivery channel configured. Set up ntfy first."), 400
        errors = []
        for ch in channels:
            try:
                ch.send(n)
                db.record_delivery(user["id"], None, None, ch.name, "test")
            except DeliveryError as e:
                errors.append(f"{ch.name}: {e}")
        if errors:
            return jsonify(error="; ".join(errors)), 502
        return jsonify(ok=True)

    return app
