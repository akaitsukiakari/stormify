"""USGS earthquakes: parsing, the feed, rules (kept apart from weather), updates, templates."""

import copy

from stormify.config import Config
from stormify.db import Database
from stormify.engine import Engine, is_significant, snapshot
from stormify.models import Alert
from stormify.poller import Poller
from stormify.rules import RuleError, evaluate, load_rules
from stormify.sources.usgs import USGSSource, miles_between, parse_feature, state_of

import pytest
from test_spc_swpc import FakeResp, FakeSession

T0 = 1762400000000  # 2025-11-06 03:33:20 UTC


def quake(eid="ci40123456", mag=4.6, place="12 km NE of Ridgecrest, CA", lon=-117.6, lat=35.7, depth=8.2,
          **props):
    p = {"mag": mag, "magType": "ml", "place": place, "time": T0, "updated": T0 + 300000,
         "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{eid}", "felt": 120, "cdi": 4.1,
         "mmi": 5.2, "alert": "green", "status": "reviewed", "tsunami": 0, "sig": 326, "type": "earthquake",
         "title": f"M {mag} - {place}"}
    p.update(props)
    return {"type": "Feature", "id": eid, "properties": p,
            "geometry": {"type": "Point", "coordinates": [lon, lat, depth]}}


def feed(*features):
    return {"type": "FeatureCollection", "metadata": {"generated": T0, "count": len(features)},
            "features": list(features)}


def test_parse_feature():
    a = parse_feature(quake())
    assert a.source == "usgs" and a.kind == "Earthquake" and a.event == "Earthquake" and a.office == "USGS"
    assert a.headline == "M4.6 Earthquake · 12 km NE of Ridgecrest, CA"
    assert a.geometry == {"type": "Point", "coordinates": [-117.6, 35.7]}
    assert a.states == ["CA"] and a.area_desc == "12 km NE of Ridgecrest, CA"
    assert set(a.tags) == {"m4", "pager-green", "felt", "us"}
    assert a.thread_key == "usgs:ci40123456" and a.id.startswith("usgs:ci40123456:")
    assert a.onset == "2025-11-06T03:33:20+00:00" and a.expires == "2025-11-07T03:33:20+00:00"
    assert a.params["mag"] == 4.6 and a.params["level"] == 4
    assert a.params["quake_stats"] == "Depth 8 km (5 mi) · PAGER green · Felt by 120 · Shaking MMI 5.2"
    assert "Did You Feel It? reports: 120" in a.description


def test_parse_feature_tags_and_severity():
    a = parse_feature(quake(mag=7.3, alert="orange", tsunami=1, sig=900, place="120 km S of Sand Point, Alaska"))
    assert {"m7", "pager-orange", "tsunami", "usgs-significant", "us"} <= set(a.tags)
    assert a.severity == "Extreme" and a.states == ["AK"]
    assert "Tsunami flag" in a.params["quake_stats"]
    world = parse_feature(quake(mag=5.2, alert=None, place="South of Fiji Islands", felt=None, mmi=None))
    assert world.states == [] and "us" not in world.tags and world.severity == "Moderate"
    blast = parse_feature(quake(type="quarry blast", mag=2.6))
    assert blast.event == "Quarry Blast" and blast.kind == "Earthquake" and "quarry-blast" in blast.tags
    assert parse_feature({"id": "x", "properties": {}, "geometry": None}) is None


def test_state_of():
    assert state_of("5 km SW of Searles Valley, CA") == "CA"
    assert state_of("Central Alaska") == "AK"
    assert state_of("10 km S of Volcano, Hawaii") == "HI"
    assert state_of("4 km SE of Cushing, Oklahoma") == "OK"
    assert state_of("Puerto Rico region") == "PR"
    assert state_of("Kermadec Islands, New Zealand") == ""


def test_new_felt_reports_alone_are_not_a_new_version():
    a = parse_feature(quake(felt=120))
    assert parse_feature(quake(felt=400, updated=T0 + 900000)).id == a.id
    assert parse_feature(quake(mag=4.8)).id != a.id
    assert parse_feature(quake(alert="yellow")).id != a.id


def test_significant_updates():
    a = parse_feature(quake(mag=5.9))
    assert not is_significant(snapshot(a), parse_feature(quake(mag=5.8)))
    assert is_significant(snapshot(a), parse_feature(quake(mag=6.1)))
    assert is_significant(snapshot(a), parse_feature(quake(mag=5.9, alert="yellow")))
    assert is_significant(snapshot(a), parse_feature(quake(mag=5.9, tsunami=1)))


def test_source_conditional_and_feed_name():
    def route(headers):
        if headers.get("If-Modified-Since") == "m1":
            return FakeResp(None, 304)
        return FakeResp(feed(quake(), quake("us7000abcd", 5.1, "South of Fiji Islands"), {"id": "junk"}),
                        headers={"Last-Modified": "m1"})

    sess = FakeSession({"/summary/2.5_day.geojson": route})
    src = USGSSource("2.5_day", "ua", session=sess)
    assert [a.params["usgs_id"] for a in src.poll()] == ["ci40123456", "us7000abcd"]
    assert src.poll() is None
    assert USGSSource("https://example.com/q.geojson", "ua").url == "https://example.com/q.geojson"


def test_miles_between():
    assert miles_between(39.74, -104.99, 39.74, -104.99) == 0
    assert 800 < miles_between(39.74, -104.99, 34.05, -118.24) < 860  # Denver to Los Angeles


# ---- rules ----------------------------------------------------------------------------------

def weather(**kw):
    d = {"id": "w1", "source": "nws", "event": "Tornado Warning", "kind": "Warning", "office": "BOU",
         "states": ["CO"], "zones": ["COZ039"]}
    d.update(kw)
    return Alert(**d)


def test_quakes_only_match_rules_that_ask_for_them():
    q = parse_feature(quake())
    rules = load_rules([
        {"name": "Everything nationwide", "scope": {"nationwide": True}},
        {"name": "Everything CA", "scope": {"states": ["CA"]}},
        {"name": "Warnings nationwide", "kinds": ["Warning"], "scope": {"nationwide": True}},
    ])
    assert evaluate(q, rules).action == "log" and evaluate(q, rules).matched == []
    assert evaluate(weather(), rules).action == "push"
    for asks in ({"events": ["Earthquake"], "scope": {"states": ["CA"]}},
                 {"kinds": ["Earthquake"], "scope": {"offices": ["USGS"]}},
                 {"min_magnitude": 4, "scope": {"nationwide": True}},
                 {"scope": {"near": [{"lat": 35.6, "lon": -117.7, "miles": 50}]}}):
        assert evaluate(q, load_rules([{"name": "q", **asks}])).action == "push", asks


def test_quake_rules_never_match_weather():
    for rule in ({"min_magnitude": 4, "scope": {"nationwide": True}},
                 {"scope": {"near": [{"lat": 39.7, "lon": -105, "miles": 500}]}}):
        assert evaluate(weather(), load_rules([{"name": "q", **rule}])).matched == [], rule
    # A rule that names quakes and weather events, with offices too, still gets the weather.
    mixed = load_rules([{"name": "m", "events": ["Earthquake", "Tornado Warning"],
                         "scope": {"offices": ["BOU"], "near": [{"lat": 39.7, "lon": -105, "miles": 300}]}}])
    assert evaluate(weather(), mixed).action == "push"


def test_near_scope_and_magnitude():
    near_home = [{"name": "Home", "lat": 39.74, "lon": -104.99, "miles": 150}]
    rules = load_rules([{"name": "Near home M3+", "events": ["Earthquake"], "min_magnitude": 3,
                         "scope": {"near": near_home}}])
    springs = parse_feature(quake(mag=3.4, lat=38.83, lon=-104.82, place="10 km W of Colorado Springs, Colorado"))
    small = parse_feature(quake(mag=2.6, lat=39.9, lon=-105.1, place="5 km N of Boulder, Colorado"))
    far = parse_feature(quake())
    assert evaluate(springs, rules).action == "push"
    d = evaluate(small, rules)
    assert d.action == "log" and "magnitude below 3" in d.reason
    assert evaluate(far, rules).matched == [] and "Near home" not in evaluate(far, rules).reason


def test_rule_validation():
    with pytest.raises(RuleError):
        load_rules([{"name": "x", "scope": {"near": [{"lat": 39.7, "lon": -105}]}}])
    with pytest.raises(RuleError):
        load_rules([{"name": "x", "scope": {"near": [{"lat": 99, "lon": -105, "miles": 10}]}}])
    with pytest.raises(RuleError):
        load_rules([{"name": "x", "min_magnitude": "big"}])


def test_example_rules_load():
    from stormify.cli import example_rules
    rules = load_rules(example_rules("quake"))
    big = parse_feature(quake(mag=6.8, place="Off the coast of Chile"))
    assert evaluate(big, rules).action == "push"
    flagged = parse_feature(quake(mag=5.0, tsunami=1, place="Off the coast of Chile"))
    assert evaluate(flagged, rules).priority == 4
    d = evaluate(parse_feature(quake(mag=3.0, place="Off the coast of Chile")), rules)
    assert d.action == "log" and "magnitude below 6.5" in d.reason


# ---- engine and poller ----------------------------------------------------------------------

def test_push_text_and_updates(tmp_path, channels, sent):
    cfg = Config(db_path=str(tmp_path / "q.db"), quiet_first_run=False, public_url="https://wx.example")
    db = Database(cfg.db_path)
    uid = db.create_user("scott", None, {"timezone": "America/Denver", "channels": {"ntfy": {"topic": "x"}}})
    db.replace_rules(uid, [{"name": "CA quakes", "events": ["Earthquake"], "min_magnitude": 4,
                            "scope": {"states": ["CA"]}, "on_update": "significant"}])
    engine = Engine(db, cfg, channel_factory=channels)
    engine.process([parse_feature(quake())])
    assert [n.title for n in sent] == ["M4.6 Earthquake · 12 km NE of Ridgecrest, CA"]
    body = sent[0].body
    assert body.startswith("Depth 8 km (5 mi) · PAGER green · Felt by 120")
    assert "At 8:33 PM MST" in body  # 03:33 UTC, the evening before in Denver
    assert sent[0].click_url.startswith("https://wx.example/?alert=usgs")
    # A small revision is archived but doesn't buzz; a PAGER upgrade does, as an update.
    engine.process([parse_feature(quake(mag=4.7))])
    assert len(sent) == 1
    engine.process([parse_feature(quake(mag=4.7, alert="yellow"))])
    assert len(sent) == 2 and sent[1].title.startswith("UPDATE M4.7")


def test_lite_feed_keeps_headline_and_magnitude(tmp_path):
    db = Database(str(tmp_path / "l.db"))
    uid = db.create_user("scott", None, {})
    db.insert_alert(parse_feature(quake()))
    import json
    (_, text), = db.query_feed_json(uid)
    row = json.loads(text)
    assert row["headline"].startswith("M4.6") and row["params"]["mag"] == 4.6
    assert row["geometry"]["type"] == "Point"


def test_poller_runs_usgs_and_first_poll_is_quiet(tmp_path, channels, sent):
    cfg = Config(db_path=str(tmp_path / "p.db"), quiet_first_run=True)
    db = Database(cfg.db_path)
    uid = db.create_user("scott", None, {"channels": {"ntfy": {"topic": "x"}}})
    db.replace_rules(uid, [{"name": "Any quake", "kinds": ["Earthquake"], "scope": {"offices": ["USGS"]}}])
    engine = Engine(db, cfg, channel_factory=channels)
    # An existing install: weather already archived.
    from fixtures import collection
    from stormify.sources.nws import parse_collection
    engine.process(parse_collection(collection()))
    names = [s.name for s in Poller(cfg, db, engine=engine).sources]
    assert "usgs" in names
    assert "usgs" not in [s.name for s in Poller(Config(db_path=cfg.db_path, usgs_enabled=False), db).sources]

    class Static:
        name = "usgs"

        def __init__(self, feats):
            self.feats = feats

        def poll(self):
            return [parse_feature(f) for f in self.feats]

    p = Poller(cfg, db, sources=[Static([quake()])], engine=engine)
    p.poll_once()
    assert sent == []  # a day of past quakes on first turn-on: archived, not pushed
    p.sources = [Static([quake(), quake("us7000new", 5.5, "South of Fiji Islands")])]
    p.poll_once()
    assert [n.title for n in sent] == ["M5.5 Earthquake · South of Fiji Islands"]


def test_images_draw_quakes(tmp_path):
    from stormify import images
    a = parse_feature(quake(mag=6.2))
    assert images.drawable(a) and images.color_for(a) == "#e0301e"
    if not images.available():
        return

    class NoTiles(images.Renderer):
        def _tile(self, z, x, y):
            return None

    png = NoTiles(str(tmp_path), "ua").render(a, a.geometry)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_web_rule_matches_find_quakes(db, cfg, channels):
    from werkzeug.security import generate_password_hash
    from stormify.web import create_app
    u = db.get_user(name="scott")
    db.set_password(u["id"], generate_password_hash("pw"))
    Engine(db, cfg, channel_factory=channels).process([parse_feature(quake())])
    app = create_app(cfg, db)
    app.testing = True
    c = app.test_client()
    c.post("/login", data={"username": "scott", "password": "pw"})
    rule = {"name": "q", "events": ["Earthquake"], "min_magnitude": 4,
            "scope": {"near": [{"name": "Ridgecrest", "lat": 35.6, "lon": -117.7, "miles": 30}]}}
    m = c.post("/api/rules/matches", json={"rule": rule}).json
    assert [x["event"] for x in m["matches"]] == ["Earthquake"]
    assert m["matches"][0]["title"].startswith("M4.6 Earthquake")
    weather_rule = {"name": "w", "scope": {"nationwide": True}}
    assert all(x["kind"] != "Earthquake" for x in c.post("/api/rules/matches", json={"rule": weather_rule}).json["matches"])
    assert c.put("/api/rules", json={"rules": [rule]}).status_code in (200, 204)
