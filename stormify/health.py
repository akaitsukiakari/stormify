"""Heartbeat status, shared by the web endpoint and the CLI."""

from __future__ import annotations

from datetime import datetime, timezone

from .db import Database


def _age(iso: str | None) -> int | None:
    if not iso:
        return None
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - dt).total_seconds())


def health(db: Database, stale_seconds: int) -> dict:
    hb = db.heartbeat()
    age = _age(hb.get("last_success_at"))
    ok = age is not None and age <= stale_seconds
    return {
        "status": "ok" if ok else "stale",
        "last_poll_at": hb.get("last_poll_at"),
        "last_success_at": hb.get("last_success_at"),
        "seconds_since_success": age,
        "last_error": hb.get("last_error"),
        "polls": hb.get("polls"),
        "active_alerts": hb.get("active_alerts"),
        "last_alert_event": hb.get("last_alert_event"),
        "last_alert_at": hb.get("last_alert_at"),
        "last_push_at": hb.get("last_push_at"),
        "pushes_total": hb.get("pushes_total"),
    }
