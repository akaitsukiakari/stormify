import sqlite3

from fixtures import collection
from test_spc_swpc import FakeResp, FakeSession

from stormify.db import Database
from stormify.engine import Engine
from stormify.poller import Poller
from stormify.sources.nws import parse_collection
from stormify.zones import ZoneShapes, simplify, simplify_ring, zone_path


def county(x, y):
    # A wiggly square: lots of nearly collinear points along each edge.
    edge = [[x + i / 50, y + (0.0001 if i % 2 else 0)] for i in range(51)]
    ring = edge + [[x + 1, y + 1], [x, y + 1], [x, y]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"timeZone": ["America/Chicago"]}}


def test_simplify_drops_collinear_points():
    ring = county(-97, 37)["geometry"]["coordinates"][0]
    out = simplify_ring(ring, 0.005)
    assert len(out) < 8 and out[0] == out[-1] and [-97.0, 37.0] in out
    assert simplify({"type": "Point", "coordinates": [0, 0]}) is None


def test_zone_path():
    assert zone_path("KSC173") == "/zones/county/KSC173"
    assert zone_path("COZ039") == "/zones/forecast/COZ039"
    assert zone_path("GMZ555") == "/zones/marine/GMZ555"
    assert zone_path("COZ214", {"COZ214": "https://api.weather.gov/zones/fire/COZ214"}) == "/zones/fire/COZ214"


def _db(tmp_path):
    d = Database(str(tmp_path / "z.db"))
    d.create_user("scott", None, {})
    return d


def test_fill_writes_shape_into_the_alert(tmp_path, cfg):
    db = _db(tmp_path)
    alerts = parse_collection(collection())
    Engine(db, cfg, channel_factory=lambda s: []).process(alerts)
    watch = next(a for a in alerts if a.event == "Tornado Watch")
    assert watch.geometry is None and watch.zones == ["KSC173", "KSC015"]
    sess = FakeSession({"/zones/county/KSC173": FakeResp(county(-97.5, 37.5)),
                        "/zones/county/KSC015": FakeResp(county(-97, 37.6))})
    zs = ZoneShapes(db, "https://api", "ua", session=sess)
    item = {"id": watch.id, "zones": watch.zones, "hints": {}}
    assert zs.fill([item], budget=1) == []  # only one lookup allowed: not finished yet
    assert zs.fill([item], budget=5) == [watch.id]
    stored = db.get_alert(watch.id)
    assert stored.geometry["type"] == "MultiPolygon" and len(stored.geometry["coordinates"]) == 2
    assert stored.params["geometry_source"] == "zones"
    assert db.zone_tz("KSC173") == "America/Chicago"
    assert len(sess.calls) == 2  # cached after that


def test_missing_zone_is_cached_as_no_shape(tmp_path):
    db = _db(tmp_path)
    zs = ZoneShapes(db, "https://api", "ua", session=FakeSession({}))
    assert zs.fill([{"id": "x", "zones": ["ZZZ999"], "hints": {}}], 5) == ["x"]
    assert db.zone_shapes(["ZZZ999"]) == {"ZZZ999": None}


def test_set_zone_tz_keeps_shape(tmp_path):
    db = _db(tmp_path)
    db.set_zone_shape("COZ039", [[[[0, 0], [1, 0], [1, 1], [0, 0]]]], None)
    db.set_zone_tz("COZ039", "America/Denver")
    assert db.zone_shapes(["COZ039"])["COZ039"] and db.zone_tz("COZ039") == "America/Denver"


def test_old_zones_table_is_migrated(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE zones (id TEXT PRIMARY KEY, tz TEXT, fetched_at TEXT NOT NULL)")
    c.execute("INSERT INTO zones VALUES ('COZ039', 'America/Denver', '2026-10-01T00:00:00Z')")
    c.commit()
    c.close()
    db = Database(path)
    assert db.zone_shapes(["COZ039"]) == {}  # never looked up yet
    assert db.zone_tz("COZ039") == "America/Denver"


def test_poller_queues_and_fills_shapes(tmp_path, cfg):
    db = _db(tmp_path)
    cfg.zone_fetch_per_poll = 50

    class Static:
        name = "nws"

        def poll(self):
            return parse_collection(collection())

    p = Poller(cfg, db, sources=[Static()], engine=Engine(db, cfg, channel_factory=lambda s: []))
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return FakeResp(county(-100, 38))

    p.zones.session = type("S", (), {"get": staticmethod(fake_get), "headers": {}})()
    p.poll_once()
    assert not p.shape_queue
    no_shape = [a for a in parse_collection(collection()) if not a.geometry]
    assert all(db.get_alert(a.id).geometry for a in no_shape)
    # A restarted poller has nothing left to do.
    assert Poller(cfg, db, sources=[]).shape_queue == {}
