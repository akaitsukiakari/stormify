import pytest
from fixtures import collection
from werkzeug.security import generate_password_hash

from stormify.db import utcnow
from stormify.engine import Engine
from stormify.models import Alert
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


def test_feed_total_lite_and_single_alert(client):
    login(client)
    n = len(collection()["features"])
    full = client.get("/api/alerts?hours=0").json
    assert full["total"] == n and any(a.get("description") for a in full["alerts"])
    # A limit below the match count still reports how many matched.
    capped = client.get("/api/alerts?hours=0&limit=2").json
    assert len(capped["alerts"]) == 2 and capped["total"] == n
    pushed = client.get("/api/alerts?hours=0&action=push&limit=1").json
    assert pushed["total"] == 6
    lite = client.get("/api/alerts?hours=0&lite=1").json["alerts"]
    assert len(lite) == n
    assert all("description" not in a and "zones" not in a for a in lite)
    assert all("geometry" in a and "area_desc" in a for a in lite)
    one = client.get("/api/alert", query_string={"id": lite[0]["id"]}).json["alert"]
    assert one["id"] == lite[0]["id"] and "description" in one
    assert client.get("/api/alert?id=nope").status_code == 404


def _add_nhc(db):
    db.insert_alert(Alert(id="nhc1", source="nhc", event="Tropical Cyclone Public Advisory", kind="Advisory",
                          office="NHC", headline="Hurricane Test Public Advisory Number 4",
                          sent="2026-10-01T21:00:00-04:00", description="long text",
                          params={"storm": "Hurricane Test", "atcf": "AL092026"}))


def test_lite_feed_matches_full_feed_without_the_bulk(client, db):
    _add_nhc(db)
    login(client)
    full = client.get("/api/alerts?hours=0").json
    lite = client.get("/api/alerts?hours=0&lite=1").json
    assert [a["id"] for a in lite["alerts"]] == [a["id"] for a in full["alerts"]]
    assert lite["total"] == full["total"]
    for f, a in zip(full["alerts"], lite["alerts"]):
        for k in ("event", "office", "kind", "tags", "geometry", "area_desc", "sent", "expires", "action", "reason"):
            assert a[k] == f[k], k
    nws = next(a for a in lite["alerts"] if a["source"] == "nws")
    assert nws["params"] == {"storm": None} and "headline" not in nws
    nhc = next(a for a in lite["alerts"] if a["id"] == "nhc1")
    assert nhc["params"] == {"storm": "Hurricane Test"}
    assert nhc["headline"] == "Hurricane Test Public Advisory Number 4"


def test_lite_feed_answers_304_when_nothing_changed(client):
    login(client)
    r = client.get("/api/alerts?hours=0&lite=1")
    assert r.headers["ETag"]
    again = client.get("/api/alerts?hours=0&lite=1", headers={"If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304 and again.data == b""


def test_selected_alert_outside_the_filters_is_added_trimmed(client, db):
    _add_nhc(db)
    login(client)
    first = client.get("/api/alerts?hours=0&lite=1&kind=Watch&id=nhc1").json["alerts"][0]
    assert first["id"] == "nhc1" and "description" not in first
    assert first["params"] == {"storm": "Hurricane Test"}


def test_offices_script_loads_before_the_dashboard(client):
    login(client)
    page = client.get("/").data
    assert page.index(b"offices.js") < page.index(b"app.js")
    assert b"BOU:" in client.get("/static/offices.js").data
