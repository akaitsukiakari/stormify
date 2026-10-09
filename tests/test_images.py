import io

import pytest
from fixtures import collection
from test_spc_swpc import FakeResp, FakeSession, outlook_fc, parse_outlook
from werkzeug.security import generate_password_hash

from stormify import images
from stormify.engine import Engine
from stormify.sources.nws import parse_collection
from stormify.web import create_app

PIL = pytest.importorskip("PIL.Image")


def tile_bytes(color=(90, 110, 90)):
    out = io.BytesIO()
    PIL.new("RGB", (256, 256), color).save(out, "PNG")
    return out.getvalue()


class TileResp(FakeResp):
    def __init__(self):
        super().__init__(None)
        self.content = tile_bytes()


def renderer(tmp_path):
    sess = FakeSession({".png": TileResp()})
    return images.Renderer(str(tmp_path / "cache"), "ua", session=sess), sess


def test_pick_view_fits_small_and_large_shapes():
    small = {"type": "Polygon", "coordinates": [[[-104.9, 39.6], [-104.6, 39.6], [-104.6, 39.8], [-104.9, 39.6]]]}
    big = {"type": "Polygon", "coordinates": [[[-110, 30], [-85, 30], [-85, 45], [-110, 30]]]}
    assert pick_z(small) == 10 and pick_z(big) <= 5


def pick_z(g):
    return images.pick_view(g)[0]


def test_render_polygon_and_tile_cache(tmp_path):
    a = next(x for x in parse_collection(collection()) if x.event == "Tornado Warning")
    r, sess = renderer(tmp_path)
    png = r.cached_render(a, a.geometry)
    img = PIL.open(io.BytesIO(png))
    assert img.size == (images.WIDTH, images.HEIGHT)
    fetched = len(sess.calls)
    assert fetched >= 1
    # The shape's red shows up in the middle of the map.
    px = img.convert("RGB").getpixel((images.WIDTH // 2, images.HEADER + (images.HEIGHT - images.HEADER) // 2))
    assert px[0] > px[1] and px[0] > px[2]
    # Second render: image cache. A different alert in the same area: tile cache.
    assert r.cached_render(a, a.geometry) == png
    r.render(a, a.geometry)
    assert len(sess.calls) == fetched


def test_render_outlook_and_tile_failure(tmp_path):
    o = parse_outlook(outlook_fc(), 1)
    r = images.Renderer(str(tmp_path / "c"), "ua", session=FakeSession({}))  # every tile 404s
    png = r.render(o, o.geometry)
    assert PIL.open(io.BytesIO(png)).size == (images.WIDTH, images.HEIGHT)


def test_drawable():
    alerts = {a.event: a for a in parse_collection(collection())}
    assert images.drawable(alerts["Tornado Warning"]) and images.drawable(alerts["Tornado Watch"])


def test_engine_attaches_image_link_only_with_public_url(db, cfg, channels, sent):
    Engine(db, cfg, channel_factory=channels).process(parse_collection(collection()))
    assert sent and all(n.image_url is None for n in sent)
    sent.clear()
    db2_alerts = parse_collection(collection())
    for a in db2_alerts:
        a.id += "-2"
        a.thread_key += "-2"
    cfg.public_url = "https://wx.example.com"
    Engine(db, cfg, channel_factory=channels).process(db2_alerts)
    with_img = [n for n in sent if n.image_url]
    assert with_img and all(n.image_url.startswith("https://wx.example.com/img/") for n in with_img)


def test_image_route(db, cfg, channels, sent, tmp_path, monkeypatch):
    cfg.public_url = "https://wx.example.com"
    cfg.cache_dir = str(tmp_path / "cache")
    Engine(db, cfg, channel_factory=channels).process(parse_collection(collection()))
    n = next(n for n in sent if n.image_url and "Tornado Warning" in n.title)
    token = n.image_url.rsplit("/", 1)[-1]
    monkeypatch.setattr(images.Renderer, "_tile", lambda self, z, x, y: None)
    u = db.get_user(name="scott")
    db.set_password(u["id"], generate_password_hash("pw"))
    app = create_app(cfg, db)
    app.testing = True
    c = app.test_client()
    r = c.get("/img/" + token)  # no login needed
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert PIL.open(io.BytesIO(r.data)).size == (images.WIDTH, images.HEIGHT)
    assert c.get("/img/not-a-token.png").status_code == 404
