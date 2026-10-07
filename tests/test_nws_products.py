import pytest
import requests

from stormify.config import load_config
from stormify.engine import Engine
from stormify.poller import Poller
from stormify.sources.nws_products import NWSProductsSource, parse_product

BASE = "https://api.weather.gov"


def summary(uid, code, when, name):
    return {"@id": f"{BASE}/products/{uid}", "id": uid, "wmoCollectiveId": "FXUS65",
            "issuingOffice": "KBOU", "issuanceTime": when, "productCode": code, "productName": name}


def product(uid, code, when, name, text="\n000\nFXUS65 KBOU 071530\nAFDBOU\n\nArea Forecast Discussion\n"):
    return {**summary(uid, code, when, name), "productText": text}


NAMES = {"AFD": "Area Forecast Discussion", "HWO": "Hazardous Weather Outlook", "RER": "Record Event Report"}


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.data


class FakeSession:
    """Serves listings and products from a dict of {code: [(uid, issuanceTime), ...]}."""

    def __init__(self, listing, fail=()):
        self.listing, self.fail = listing, set(fail)
        self.headers, self.calls = {}, []

    def get(self, url, timeout=None):
        path = url[len(BASE):]
        self.calls.append(path)
        if path.startswith("/products/types/"):
            code = path.split("/")[3]
            if code in self.fail:
                return FakeResp({}, 500)
            return FakeResp({"@graph": [summary(u, code, t, NAMES.get(code, code))
                                        for u, t in self.listing.get(code, [])]})
        uid = path.rsplit("/", 1)[1]
        for code, items in self.listing.items():
            for u, t in items:
                if u == uid:
                    return FakeResp(product(u, code, t, NAMES.get(code, code)))
        return FakeResp({}, 404)


def source(sess, types=("AFD", "HWO"), known=None, **kw):
    return NWSProductsSource(BASE, "(test)", ["KBOU"], list(types), interval=0, session=sess,
                             known_ids=known, **kw)


def test_parse_product():
    a = parse_product(product("u1", "AFD", "2026-10-07T15:30:00+00:00", "Area Forecast Discussion"), "BOU")
    assert a.id == f"{BASE}/products/u1"
    assert a.event == "Area Forecast Discussion"
    assert a.kind == "Product"
    assert a.office == "BOU"
    assert a.headline == "Area Forecast Discussion (AFDBOU)"
    assert a.thread_key == a.id
    assert a.description.startswith("000")
    assert not a.is_cancel and a.expires == ""


def test_lists_each_type_and_fetches_new_products():
    sess = FakeSession({"AFD": [("a1", "2026-10-07T15:30:00+00:00")], "HWO": [("h1", "2026-10-07T10:00:00+00:00")]})
    alerts = source(sess).poll()
    assert sorted(a.event for a in alerts) == ["Area Forecast Discussion", "Hazardous Weather Outlook"]
    assert "/products/types/AFD/locations/BOU" in sess.calls
    assert "/products/types/HWO/locations/BOU" in sess.calls


def test_skips_known_and_caps_backfill():
    items = [(f"a{i}", f"2026-10-0{i}T12:00:00+00:00") for i in range(1, 8)]
    sess = FakeSession({"AFD": items})
    known = {f"{BASE}/products/a7"}
    alerts = source(sess, types=["AFD"], known=lambda ids: known & set(ids), max_per_type=3).poll()
    # Newest three are a7, a6, a5; a7 is already archived. Oldest first.
    assert [a.id.rsplit("/", 1)[1] for a in alerts] == ["a5", "a6"]
    assert "/products/a7" not in sess.calls


def test_one_failing_type_does_not_block_others():
    sess = FakeSession({"HWO": [("h1", "2026-10-07T10:00:00+00:00")]}, fail={"AFD"})
    alerts = source(sess).poll()
    assert [a.event for a in alerts] == ["Hazardous Weather Outlook"]


def test_all_failing_raises_for_the_heartbeat():
    with pytest.raises(requests.HTTPError):
        source(FakeSession({}, fail={"AFD", "HWO"})).poll()


def test_respects_interval():
    sess = FakeSession({"AFD": [("a1", "2026-10-07T15:30:00+00:00")]})
    src = NWSProductsSource(BASE, "(test)", ["BOU"], ["AFD"], interval=300, session=sess)
    assert len(src.poll()) == 1
    assert src.poll() is None


def test_products_log_by_default_and_push_with_a_product_rule(db, cfg, channels, sent):
    eng = Engine(db, cfg, channel_factory=channels)
    afd = parse_product(product("a1", "AFD", "2026-10-07T15:30:00+00:00", "Area Forecast Discussion"), "BOU")
    eng.process([afd])
    assert sent == []  # example rules never ask for products
    uid = db.get_user(name="scott")["id"]
    assert [r["event"] for r in db.query_feed(uid, kinds=["Product"])] == ["Area Forecast Discussion"]
    assert db.query_feed(uid, active_only=True) == []

    db.replace_rules(uid, [{"name": "BOU products", "kinds": ["Product"], "scope": {"offices": ["BOU"]},
                            "priority": 2}])
    hwo = parse_product(product("h1", "HWO", "2026-10-07T16:00:00+00:00", "Hazardous Weather Outlook"), "BOU")
    hwo2 = parse_product(product("h2", "HWO", "2026-10-08T10:00:00+00:00", "Hazardous Weather Outlook"), "BOU")
    eng.process([hwo])
    eng.process([hwo2])
    # Each issuance is its own product, so the next day's HWO isn't swallowed as an "update".
    assert [n.title for n in sent] == ["Hazardous Weather Outlook · BOU"] * 2
    assert sent[0].body.startswith("Hazardous Weather Outlook (HWOBOU)\nIssued ")
    assert "Until" not in sent[0].body


def test_config_and_poller_wiring(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(f'[general]\ndb_path = "{tmp_path / "x.db"}"\n\n[nws_products]\noffices = ["BOU"]\n')
    cfg = load_config(p)
    assert cfg.products_locations == ["BOU"] and cfg.products_types == [] and cfg.products_interval == 300
    from stormify.db import Database
    poller = Poller(cfg, Database(cfg.db_path))
    src = [s for s in poller.sources if s.name == "nws-products"]
    assert src and src[0].types == ["AFD", "HWO", "RER", "PNS", "LSR"]


def test_products_off_by_default(tmp_path, cfg):
    from stormify.db import Database
    assert "nws-products" not in [s.name for s in Poller(cfg, Database(cfg.db_path)).sources]
