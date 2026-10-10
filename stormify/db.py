"""SQLite storage.

One small database file holds users, rules, the alert archive, delivery history
and the heartbeat. WAL mode lets the poller and the web dashboard (separate
processes) share it safely. Everything user-facing is keyed by user_id from
day one so multi-user is a non-event later.
"""

from __future__ import annotations

import json
import secrets
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

-- The dashboard's trimmed copy of each alert, worked out once when the alert is stored. Building it
-- from data_json on every refresh meant parsing megabytes of JSON a minute on a Pi Zero.
CREATE TABLE IF NOT EXISTS alert_lite (
    id TEXT PRIMARY KEY REFERENCES alerts(id) ON DELETE CASCADE,
    json TEXT NOT NULL
);

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
    fetched_at TEXT NOT NULL,
    shape_json TEXT,                -- simplified outline: list of polygons, or NULL if none
    shape_at TEXT                   -- when the outline was looked up (NULL = never)
);

-- Unguessable links to notification map images (the phone fetches them without logging in).
CREATE TABLE IF NOT EXISTS image_tokens (
    token TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    created_at TEXT NOT NULL
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
        self._migrate()

    def _migrate(self) -> None:
        """Columns added after v0.1, for databases created before them."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(zones)")}
        for col in ("shape_json", "shape_at"):
            if col not in have:
                self.conn.execute(f"ALTER TABLE zones ADD COLUMN {col} TEXT")
        # user_version 1 = every alert has its alert_lite row. A new database starts there; an older
        # one gets there through backfill_lite() and reads the slow way until then.
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 1 and not self.has_any_alerts():
            self.conn.execute("PRAGMA user_version=1")

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
        (c or self.conn).execute(f"INSERT OR IGNORE INTO alert_lite (id, json) SELECT id, {self._lite_sql()}"
                                 " FROM alerts WHERE id=?", (a.id,))

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
    # The only raw parameters the dashboard reads (an NHC storm's name, where a shape came from, an SPC
    # outlook's risk areas). NWS parameters carry long reference lists that used to be most of the
    # payload, so lite rows keep just these.
    LITE_PARAMS = ("storm", "geometry_source", "risk_labels", "day")

    def lite_dict(self, d: dict) -> dict:
        """Python version of the lite trim, for a row that didn't come through query_feed_json."""
        params = d.get("params") or {}
        zoned = params.get("geometry_source") == "zones"
        d = {k: v for k, v in d.items() if k not in self.LITE_DROP or (zoned and k == "zones")}
        if zoned:
            d.pop("geometry", None)
        if d.get("source") not in ("nhc", "spc", "swpc"):
            d.pop("headline", None)
        d["params"] = {k: params[k] for k in self.LITE_PARAMS if params.get(k) is not None}
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

    def _lite_sql(self) -> str:
        """SQL that trims an alerts row's data_json to the dashboard's lite JSON."""
        # NHC, SPC and SWPC products are labeled by their headline; NWS headlines just repeat the card.
        # A shape built from zone outlines is left out and its zone list kept: the dashboard draws it
        # from /api/zones, so the same county borders aren't resent with every update of every advisory.
        drop = ", ".join([*(f"'$.{k}'" for k in self.LITE_DROP if k != "zones"),
                          "CASE WHEN source IN ('nhc', 'spc', 'swpc') THEN '$.__keep' ELSE '$.headline' END",
                          "CASE WHEN json_extract(data_json, '$.params.geometry_source') = 'zones'"
                          " THEN '$.geometry' ELSE '$.zones' END"])
        keep = ", ".join(f"'{k}', json_extract(data_json, '$.params.{k}')" for k in self.LITE_PARAMS)
        # json_patch drops the null ones, so a plain NWS alert carries "params": {}.
        return f"json_set(json_remove(data_json, {drop}), '$.params', json_patch('{{}}', json_object({keep})))"

    def lite_ready(self) -> bool:
        return self.conn.execute("PRAGMA user_version").fetchone()[0] >= 1

    def backfill_lite(self, batch: int = 300) -> int:
        """Give a batch of older alerts (newest first) their alert_lite row; 0 once all have one."""
        if self.lite_ready():
            return 0
        with self.tx() as c:
            n = c.execute(f"INSERT OR IGNORE INTO alert_lite (id, json) SELECT id, {self._lite_sql()} FROM alerts"
                          " WHERE id NOT IN (SELECT id FROM alert_lite) ORDER BY first_seen DESC LIMIT ?",
                          (batch,)).rowcount
            if n == 0:
                c.execute("PRAGMA user_version=1")
        return n

    def query_feed_json(self, user_id: int, since: str | None = None, until: str | None = None,
                        events: list[str] | None = None, offices: list[str] | None = None,
                        kinds: list[str] | None = None, action: str | None = None, q: str | None = None,
                        active_only: bool = False, limit: int = 300) -> list[tuple[str, str]]:
        """Lite feed rows as (id, JSON text), built entirely inside SQLite.

        Same rows as query_feed minus the long text (fetched per alert when opened) and minus shapes
        built from zones (fetched per state from /api/zones). Python never parses or re-serializes
        them, and the trimming itself was done when each alert was stored.
        """
        where, args = self._feed_where(since, until, events, offices, kinds, action, q, active_only)
        lite = "l.json" if self.lite_ready() else self._lite_sql()
        data = (f"json_set({lite}, '$.first_seen', a.first_seen, '$.action', coalesce(d.action, 'log'),"
                " '$.reason', coalesce(d.reason, ''), '$.matched_rules', json(coalesce(d.rules_json, '[]')))")
        join = " JOIN alert_lite l ON l.id=a.id" if self.lite_ready() else ""
        sql = [f"SELECT a.id, {data} FROM alerts a{join}"
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
            c.execute("DELETE FROM image_tokens WHERE alert_id NOT IN (SELECT id FROM alerts)")
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

    # ---- zones (time zone + outline cache) ------------------------------
    def zone_tz(self, zone_id: str) -> str | None:
        row = self.conn.execute("SELECT tz FROM zones WHERE id=?", (zone_id,)).fetchone()
        return row[0] if row else None

    def set_zone_tz(self, zone_id: str, tz: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO zones (id, tz, fetched_at) VALUES (?,?,?)"
                      " ON CONFLICT(id) DO UPDATE SET tz=excluded.tz, fetched_at=excluded.fetched_at",
                      (zone_id, tz, utcnow()))

    def set_zone_shape(self, zone_id: str, shape: list | None, tz: str | None = None) -> None:
        now = utcnow()
        with self.tx() as c:
            c.execute("INSERT INTO zones (id, tz, fetched_at, shape_json, shape_at) VALUES (?,?,?,?,?)"
                      " ON CONFLICT(id) DO UPDATE SET shape_json=excluded.shape_json, shape_at=excluded.shape_at,"
                      " tz=coalesce(zones.tz, excluded.tz)",
                      (zone_id, tz, now, json.dumps(shape) if shape else None, now))

    def zone_shapes(self, zone_ids: list[str]) -> dict[str, list | None]:
        """Looked-up outlines by zone (None = looked up, no outline). Zones never looked up are absent."""
        out: dict[str, list | None] = {}
        for i in range(0, len(zone_ids), 500):
            chunk = zone_ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                    f"SELECT id, shape_json FROM zones WHERE shape_at IS NOT NULL AND id IN ({q})", chunk):
                out[r[0]] = json.loads(r[1]) if r[1] else None
        return out

    def zone_shapes_json(self, prefix: str) -> str:
        """Every looked-up outline for one state's zones or counties ("COZ", "KSC") as one JSON object
        (null = no outline), stitched from the stored text without parsing it."""
        hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        rows = self.conn.execute("SELECT id, shape_json FROM zones WHERE shape_at IS NOT NULL AND id >= ? AND id < ?"
                                 " ORDER BY id", (prefix, hi))
        return "{" + ",".join(f'"{r[0]}":{r[1] or "null"}' for r in rows) + "}"

    def set_alert_geometry(self, alert_id: str, geometry: dict, source: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE alerts SET data_json=json_set(data_json, '$.geometry', json(?),"
                      " '$.params.geometry_source', ?) WHERE id=?", (json.dumps(geometry), source, alert_id))
            c.execute(f"REPLACE INTO alert_lite (id, json) SELECT id, {self._lite_sql()} FROM alerts WHERE id=?",
                      (alert_id,))

    # ---- notification image links --------------------------------------
    def image_token(self, alert_id: str) -> str:
        row = self.conn.execute("SELECT token FROM image_tokens WHERE alert_id=? LIMIT 1", (alert_id,)).fetchone()
        if row:
            return row[0]
        token = secrets.token_urlsafe(18)
        with self.tx() as c:
            c.execute("INSERT INTO image_tokens (token, alert_id, created_at) VALUES (?,?,?)",
                      (token, alert_id, utcnow()))
        return token

    def alert_for_image(self, token: str) -> str | None:
        row = self.conn.execute("SELECT alert_id FROM image_tokens WHERE token=?", (token,)).fetchone()
        return row[0] if row else None

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
