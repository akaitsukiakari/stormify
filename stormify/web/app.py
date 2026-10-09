"""Dashboard: alert feed + polygon map, rule import/export, settings, health."""

from __future__ import annotations

import functools
import json
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .. import __version__
from ..config import Config
from ..db import Database
from ..delivery import DeliveryError, Notification, channels_for_user
from ..health import health
from ..rules import RuleError, load_rules
from ..timefmt import DEFAULT_DISPLAY

# Most alerts one /api/alerts call returns. A busy 24 h nationwide is several hundred; this keeps
# "Last 7 days" or "All time" from asking the Pi to serialize tens of thousands at once.
FEED_LIMIT = 2000


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

    # ---- health (no login: Home Assistant polls this) ---------------------
    @app.route("/api/health")
    def api_health():
        h = health(db, cfg.heartbeat_stale_seconds)
        return jsonify(h), (200 if h["status"] == "ok" else 503)

    # ---- feed API ------------------------------------------------------------
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
