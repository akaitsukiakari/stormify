import pytest
from fixtures import collection
from werkzeug.security import generate_password_hash

from stormify.db import utcnow
from stormify.engine import Engine
from stormify.sources.nws import parse_collection
from stormify.web import create_app


@pytest.fixture
def client(db, cfg, channels):
    u = db.get_user(name="scott")
    db.set_password(u["id"], generate_password_hash("pw"))
    Engine(db, cfg, channel_factory=channels).process(parse_collection(collection()))
    app = create_app(cfg, db)
    app.testing = True
    return app.test_client()


def login(c):
    return c.post("/login", data={"username": "scott", "password": "pw"})


def test_requires_login(client):
    assert client.get("/").status_code == 302
    assert client.get("/api/alerts").status_code == 401


def test_bad_password(client):
    r = client.post("/login", data={"username": "scott", "password": "nope"})
    assert b"Wrong username or password" in r.data


def test_health_is_public_and_reports_staleness(client, db):
    r = client.get("/api/health")
    assert r.status_code == 503 and r.json["status"] == "stale"
    db.update_heartbeat(last_success_at=utcnow())
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json["status"] == "ok"


def test_feed_and_filters(client):
    login(client)
    assert client.get("/").status_code == 200
    all_ = client.get("/api/alerts?hours=0").json["alerts"]
    assert len(all_) == len(collection()["features"])
    pushed = client.get("/api/alerts?hours=0&action=push").json["alerts"]
    assert len(pushed) == 6
    bou = client.get("/api/alerts?hours=0&office=bou").json["alerts"]
    assert bou and all(a["office"] == "BOU" for a in bou)
    q = client.get("/api/alerts?hours=0&q=baseball").json["alerts"]
    assert len(q) == 1 and q[0]["office"] == "OUN"
    meta = client.get("/api/meta").json
    assert "BOU" in meta["offices"] and meta["time_display"] == ["local", "event", "zulu"]


def test_rules_roundtrip_and_validation(client):
    login(client)
    rules = client.get("/api/rules").json["rules"]
    assert rules
    assert client.put("/api/rules", json={"rules": rules}).json["count"] == len(rules)
    bad = client.put("/api/rules", json={"rules": [{"name": "x", "filters": {"include_regex": ["("]}}]})
    assert bad.status_code == 400
    dl = client.get("/api/rules?download=1")
    assert "attachment" in dl.headers["Content-Disposition"]


def test_settings_masks_token(client):
    login(client)
    r = client.put("/api/settings", json={"channels": {"ntfy": {"topic": "t", "token": "secret"}},
                                          "time_display": ["zulu", "bogus"]})
    s = r.json["settings"]
    assert s["channels"]["ntfy"]["token"] == "********"
    assert s["time_display"] == ["zulu"]


def test_map_key_reaches_dashboard(client, cfg):
    login(client)
    assert b'data-map-key=""' in client.get("/").data
    cfg.map_key = "abc123"
    assert b'data-map-key="abc123"' in client.get("/").data


def test_favicon_served_and_linked(client):
    assert b"favicon.svg" in client.get("/login").data
    r = client.get("/static/favicon.svg")
    assert r.status_code == 200 and b"<svg" in r.data
