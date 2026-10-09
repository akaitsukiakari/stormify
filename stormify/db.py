"""SQLite storage.

One small database file holds users, rules, the alert archive, delivery history
and the heartbeat. WAL mode lets the poller and the web dashboard (separate
processes) share it safely. Everything user-facing is keyed by user_id from
day one so multi-user is a non-event later.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .models import Alert

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rules_user ON rules(user_id, position);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    event TEXT NOT NULL,
    kind TEXT NOT NULL,
    office TEXT,
    thread_key TEXT,
    message_type TEXT,
    severity TEXT,
    sent TEXT,
    expires TEXT,
    headline TEXT,
    area_desc TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    data_json TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_sent ON alerts(sent);
CREATE INDEX IF NOT EXISTS alerts_thread ON alerts(thread_key);
CREATE INDEX IF NOT EXISTS alerts_event ON alerts(event);
CREATE INDEX IF NOT EXISTS alerts_office ON alerts(office);
CREATE INDEX IF NOT EXISTS alerts_kind ON alerts(kind);
CREATE INDEX IF NOT EXISTS alerts_first_seen ON alerts(first_seen);
CREATE INDEX IF NOT EXISTS alerts_source ON alerts(source);

-- What each user's rules decided about each alert (drives the per-user feed).
CREATE TABLE IF NOT EXISTS decisions (
    alert_id TEXT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action TEXT NOT NULL,           -- push | log | suppressed
    reason TEXT,
    rules_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (alert_id, user_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    alert_id TEXT,
    thread_key TEXT,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,           -- sent | failed | test
    error TEXT,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS deliveries_thread ON deliveries(user_id, thread_key);

CREATE TABLE IF NOT EXISTS zones (
    id TEXT PRIMARY KEY,
    tz TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    polls INTEGER NOT NULL DEFAULT 0,
    active_alerts INTEGER NOT NULL DEFAULT 0,
    last_alert_id TEXT,
    last_alert_event TEXT,
    last_alert_at TEXT,
    last_push_at TEXT,
    pushes_total INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO heartbeat (id) VALUES (1);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self.conn.executescript(SCHEMA)  # executescript manages its own transaction

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")  # fewer fsyncs = less SD card wear
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self.conn
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    # ---- users ---------------------------------------------------------
    def create_user(self, name: str, password_hash: str | None = None, settings: dict | None = None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO users (name, password_hash, settings_json, created_at) VALUES (?,?,?,?)",
                (name, password_hash, json.dumps(settings or {}), utcnow()),
            )
            return cur.lastrowid

    def get_user(self, user_id: int | None = None, name: str | None = None) -> dict | None:
        if user_id is not None:
            row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        return self._user(row) if row else None

    def list_users(self) -> list[dict]:
        return [self._user(r) for r in self.conn.execute("SELECT * FROM users ORDER BY id")]

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))

    def update_settings(self, user_id: int, settings: dict) -> None:
        with self.tx() as c:
            c.execute("UPDATE users SET settings_json=? WHERE id=?", (json.dumps(settings), user_id))

    @staticmethod
    def _user(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["settings"] = json.loads(d.pop("settings_json") or "{}")
        return d

    # ---- rules ---------------------------------------------------------
    def list_rules(self, user_id: int, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM rules WHERE user_id=?"
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY position, id"
        out = []
        for r in self.conn.execute(sql, (user_id,)):
            body = json.loads(r["body_json"])
            body.update(id=r["id"], name=r["name"], enabled=bool(r["enabled"]))
            out.append(body)
        return out

    def replace_rules(self, user_id: int, rules: list[dict]) -> None:
        now = utcnow()
        with self.tx() as c:
            c.execute("DELETE FROM rules WHERE user_id=?", (user_id,))
            for i, rule in enumerate(rules):
                body = {k: v for k, v in rule.items() if k not in ("id", "name", "enabled")}
                c.execute(
                    "INSERT INTO rules (user_id, name, enabled, position, body_json, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (user_id, rule.get("name") or f"Rule {i + 1}", int(rule.get("enabled", True)), i,
                     json.dumps(body), now, now),
                )

    # ---- alerts --------------------------------------------------------
    def has_any_alerts(self) -> bool:
        return self.conn.execute("SELECT 1 FROM alerts LIMIT 1").fetchone() is not None

    def has_source(self, source: str) -> bool:
        return self.conn.execute("SELECT 1 FROM alerts WHERE source=? LIMIT 1", (source,)).fetchone() is not None

    def known_alert_ids(self, ids: list[str]) -> set[str]:
        known: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            known.update(r[0] for r in self.conn.execute(f"SELECT id FROM alerts WHERE id IN ({q})", chunk))
        return known

    def insert_alert(self, a: Alert, c: sqlite3.Connection | None = None) -> None:
        now = utcnow()
        (c or self.conn).execute(
            "INSERT OR IGNORE INTO alerts (id, source, event, kind, office, thread_key, message_type, severity,"
            " sent, expires, headline, area_desc, tags_json, data_json, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.id, a.source, a.event, a.kind, a.office, a.thread_key, a.message_type, a.severity,
             a.sent, a.expires, a.headline, a.area_desc, json.dumps(a.tags), json.dumps(a.to_dict()), now, now),
        )

    def touch_alerts(self, ids: list[str]) -> None:
        now = utcnow()
        with self.tx() as c:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                c.execute(f"UPDATE alerts SET last_seen=? WHERE id IN ({q})", [now, *chunk])

    def get_alert(self, alert_id: str) -> Alert | None:
        row = self.conn.execute("SELECT data_json FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return Alert.from_dict(json.loads(row[0])) if row else None

    def record_decision(self, c: sqlite3.Connection, alert_id: str, user_id: int, action: str,
                        reason: str, rule_names: list[str]) -> None:
        c.execute(
            "INSERT OR REPLACE INTO decisions (alert_id, user_id, action, reason, rules_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (alert_id, user_id, action, reason, json.dumps(rule_names), utcnow()),
        )

    # Long text and code lists the dashboard list and map never show. Dropped inside SQLite
    # (fast C) so the Pi doesn't parse and re-serialize every alert's full text on each refresh;
    # the dashboard fetches one alert's full text when you open it.
    LITE_DROP = ("description", "instruction", "nws_headline", "zones", "same", "vtec", "references",
                 "params", "certainty", "urgency", "effective", "onset", "states", "status", "url",
                 "thread_key", "severity", "vtec_action")
    # The only raw parameter the dashboard reads (an NHC storm's name). NWS parameters carry long
    # reference lists that used to be most of the payload, so lite rows keep just this one.
    LITE_PARAMS = ("storm",)

    def lite_dict(self, d: dict) -> dict:
        """Python version of the lite trim, for a row that didn't come through query_feed_json."""
        params = d.get("params") or {}
        d = {k: v for k, v in d.items() if k not in self.LITE_DROP}
        if d.get("source") != "nhc":
            d.pop("headline", None)
        d["params"] = {k: params.get(k) for k in self.LITE_PARAMS}
        return d

    def _feed_where(self, since: str | None, until: str | None, events: list[str] | None,
                    offices: list[str] | None, kinds: list[str] | None, action: str | None,
                    q: str | None, active_only: bool) -> tuple[list[str], list[Any]]:
        sql: list[str] = ["WHERE 1=1"]
        args: list[Any] = []
        if since:
            sql.append("AND a.first_seen >= ?")
            args.append(since)
        if until:
            sql.append("AND a.first_seen <= ?")
            args.append(until)
        for col, values in (("a.event", events), ("a.office", offices), ("a.kind", kinds)):
            if values:
                sql.append(f"AND {col} IN ({','.join('?' * len(values))})")
                args.extend(values)
        if action:
            sql.append("AND d.action = ?")
            args.append(action)
        if q:
            sql.append("AND a.data_json LIKE ?")
            args.append(f"%{q}%")
        if active_only:
            # Text products never expire, so they'd sit in "active" forever; leave them out.
            sql.append("AND a.kind != 'Product'"
                       " AND (a.expires IS NULL OR a.expires = '' OR julianday(a.expires) > julianday('now'))")
        return sql, args

    def query_feed(self, user_id: int, since: str | None = None, until: str | None = None,
                   events: list[str] | None = None, offices: list[str] | None = None,
                   kinds: list[str] | None = None, action: str | None = None, q: str | None = None,
                   active_only: bool = False, limit: int = 300) -> list[dict]:
        where, args = self._feed_where(since, until, events, offices, kinds, action, q, active_only)
        sql = ["SELECT a.data_json, a.first_seen, d.action, d.reason, d.rules_json FROM alerts a"
               " LEFT JOIN decisions d ON d.alert_id=a.id AND d.user_id=?", *where,
               "ORDER BY a.first_seen DESC, a.sent DESC LIMIT ?"]
        out = []
        for r in self.conn.execute(" ".join(sql), [user_id, *args, limit]):
            d = json.loads(r["data_json"])
            d["first_seen"] = r["first_seen"]
            d["action"] = r["action"] or "log"
            d["reason"] = r["reason"] or ""
            d["matched_rules"] = json.loads(r["rules_json"] or "[]")
            out.append(d)
        return out

    def query_feed_json(self, user_id: int, since: str | None = None, until: str | None = None,
                        events: list[str] | None = None, offices: list[str] | None = None,
                        kinds: list[str] | None = None, action: str | None = None, q: str | None = None,
                        active_only: bool = False, limit: int = 300) -> list[tuple[str, str]]:
        """Lite feed rows as (id, JSON text), built entirely inside SQLite.

        Same rows as query_feed minus the long text (fetched per alert when opened), but Python
        never parses or re-serializes them, which was most of the work on a Pi Zero once a busy
        day put a couple thousand alerts in the feed.
        """
        where, args = self._feed_where(since, until, events, offices, kinds, action, q, active_only)
        # Only NHC products are labeled by their headline; NWS headlines just repeat the card.
        drop = ", ".join([*(f"'$.{k}'" for k in self.LITE_DROP),
                          "CASE WHEN a.source = 'nhc' THEN '$.__keep' ELSE '$.headline' END"])
        keep = ", ".join(f"'{k}', json_extract(a.data_json, '$.params.{k}')" for k in self.LITE_PARAMS)
        data = (f"json_set(json_remove(a.data_json, {drop}), '$.params', json_object({keep}),"
                " '$.first_seen', a.first_seen, '$.action', coalesce(d.action, 'log'),"
                " '$.reason', coalesce(d.reason, ''), '$.matched_rules', json(coalesce(d.rules_json, '[]')))")
        sql = [f"SELECT a.id, {data} FROM alerts a"
               " LEFT JOIN decisions d ON d.alert_id=a.id AND d.user_id=?", *where,
               "ORDER BY a.first_seen DESC, a.sent DESC LIMIT ?"]
        return [(r[0], r[1]) for r in self.conn.execute(" ".join(sql), [user_id, *args, limit])]

    def count_feed(self, user_id: int, since: str | None = None, until: str | None = None,
                   events: list[str] | None = None, offices: list[str] | None = None,
                   kinds: list[str] | None = None, action: str | None = None, q: str | None = None,
                   active_only: bool = False) -> int:
        where, args = self._feed_where(since, until, events, offices, kinds, action, q, active_only)
        join = " LEFT JOIN decisions d ON d.alert_id=a.id AND d.user_id=?" if action else ""
        sql = " ".join(["SELECT COUNT(*) FROM alerts a" + join, *where])
        return self.conn.execute(sql, ([user_id] if action else []) + args).fetchone()[0]

    def distinct_values(self, column: str) -> list[str]:
        assert column in ("event", "office", "kind")
        return [r[0] for r in self.conn.execute(
            f"SELECT DISTINCT {column} FROM alerts WHERE {column} != '' ORDER BY {column}")]

    def prune(self, keep_days: int) -> int:
        with self.tx() as c:
            cur = c.execute("DELETE FROM alerts WHERE julianday('now') - julianday(first_seen) > ?", (keep_days,))
            return cur.rowcount

    # ---- deliveries ----------------------------------------------------
    def last_delivery(self, user_id: int, thread_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM deliveries WHERE user_id=? AND thread_key=? AND status='sent'"
            " ORDER BY id DESC LIMIT 1", (user_id, thread_key)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["snapshot"] = json.loads(d.pop("snapshot_json") or "{}")
        return d

    def record_delivery(self, user_id: int, alert_id: str | None, thread_key: str | None, channel: str,
                        status: str, error: str | None = None, snapshot: dict | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO deliveries (user_id, alert_id, thread_key, channel, status, error, snapshot_json, sent_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (user_id, alert_id, thread_key, channel, status, error, json.dumps(snapshot or {}), utcnow()),
            )

    def already_delivered(self, user_id: int, alert_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM deliveries WHERE user_id=? AND alert_id=? AND status='sent' LIMIT 1",
            (user_id, alert_id)).fetchone() is not None

    # ---- zones (time zone cache) ----------------------------------------
    def zone_tz(self, zone_id: str) -> str | None:
        row = self.conn.execute("SELECT tz FROM zones WHERE id=?", (zone_id,)).fetchone()
        return row[0] if row else None

    def set_zone_tz(self, zone_id: str, tz: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO zones (id, tz, fetched_at) VALUES (?,?,?)", (zone_id, tz, utcnow()))

    # ---- heartbeat -----------------------------------------------------
    def heartbeat(self) -> dict:
        return dict(self.conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone())

    def update_heartbeat(self, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE heartbeat SET {cols} WHERE id=1", list(fields.values()))

    def bump_heartbeat(self, polls: int = 0, pushes: int = 0) -> None:
        with self.tx() as c:
            c.execute("UPDATE heartbeat SET polls=polls+?, pushes_total=pushes_total+? WHERE id=1", (polls, pushes))
