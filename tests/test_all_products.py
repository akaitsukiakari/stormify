"""Pulling every product: "*" per office, national products, SWPC text products, a failing source."""

from datetime import datetime, timedelta, timezone

import requests

from stormify.sources.nws_products import NWSProductsSource, awips_id, parse_product
from stormify.sources.swpc import SWPCSource, parse_message, parse_text_product

BASE = "https://api.weather.gov"


def iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def item(uid, code, office="KBOU", minutes_ago=5, wmo="FXUS65"):
    return {"@id": f"{BASE}/products/{uid}", "id": uid, "wmoCollectiveId": wmo, "issuingOffice": office,
            "issuanceTime": iso(minutes_ago), "productCode": code, "productName": f"{code} name"}


class Resp:
    def __init__(self, data=None, status=200, text="", headers=None):
        self.data, self.status_code, self.text, self.headers = data, status, text, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self):
        return self.data


class Session:
    """Routes by exact path (query string included); products are served from `items`."""

    def __init__(self, routes, items=()):
        self.routes, self.items = routes, {i["id"]: i for i in items}
        self.headers, self.calls = {}, []

    def get(self, url, headers=None, timeout=None):
        path = url[len(BASE):] if url.startswith(BASE) else url
        self.calls.append(path)
        if path in self.routes:
            r = self.routes[path]
            return r if isinstance(r, Resp) else Resp({"@graph": r})
        if path.startswith("/products/") and path.split("/")[2] in self.items:
            i = self.items[path.split("/")[2]]
            awips = i["productCode"] + ("MCD" if "MPD" not in i["id"] else "MPD")
            return Resp({**i, "productText": f"\n000\n{i['wmoCollectiveId']} {i['issuingOffice']} 301200\n"
                                             f"{i.get('awips', awips)}\n\nText {i['id']}\n{i.get('body', '')}"})
        return Resp({}, 404)


def test_star_uses_office_query():
    items = [item("a", "AFD"), item("b", "ZFP"), item("c", "RTP", minutes_ago=600)]
    sess = Session({"/products?office=KBOU&limit=50": items}, items)
    src = NWSProductsSource(BASE, "ua", ["BOU"], ["*"], interval=0, session=sess)
    got = src.poll()
    # The 10-hour-old one is left alone on the first look, so turning on "*" doesn't push old news.
    assert sorted(a.params["productCode"] for a in got) == ["AFD", "ZFP"]
    assert all(a.office == "BOU" for a in got)
    assert not any(c.startswith("/products/types/") for c in sess.calls)


def test_star_falls_back_when_office_filter_is_ignored():
    everyone = [item("a", "AFD"), item("x", "AFD", office="KLIX")]
    mine = [item("h", "HWO")]
    sess = Session({"/products?office=KBOU&limit=50": everyone,
                    "/products/locations/BOU/types": [{"productCode": "HWO"}, {"productCode": "AFD"}],
                    "/products/types/HWO/locations/BOU": mine,
                    "/products/types/AFD/locations/BOU": [everyone[0]]}, everyone + mine)
    src = NWSProductsSource(BASE, "ua", ["BOU"], ["*"], interval=0, session=sess)
    got = src.poll()
    assert sorted(a.params["productCode"] for a in got) == ["AFD", "HWO"]
    assert src._office_query is False
    sess.calls.clear()
    src.poll()
    assert "/products?office=KBOU&limit=50" not in sess.calls  # not asked again


def test_star_falls_back_on_400():
    sess = Session({"/products?office=KBOU&limit=50": Resp({}, 400),
                    "/products/locations/BOU/types": [{"productCode": "PNS"}],
                    "/products/types/PNS/locations/BOU": [item("p", "PNS")]}, [item("p", "PNS")])
    got = NWSProductsSource(BASE, "ua", ["BOU"], ["*"], interval=0, session=sess).poll()
    assert [a.params["productCode"] for a in got] == ["PNS"]


def test_national_products():
    mpd = {**item("MPD1", "FFG", office="KWNH", wmo="AWUS01"), "awips": "FFGMPD"}
    tsu = {**item("t1", "TSU", office="PAAQ", wmo="WEPA41"), "awips": "TSUWCA"}
    sel = {**item("s1", "SEL", office="KWNS", wmo="WWUS20"), "awips": "SEL5",
           "body": "THIS IS A PARTICULARLY DANGEROUS SITUATION"}
    sess = Session({"/products/types/FFG/locations/MPD": [mpd], "/products/types/TSU": [tsu],
                    "/products/types/SEL": [sel]}, [mpd, tsu, sel])
    src = NWSProductsSource(BASE, "ua", [], [], interval=0, session=sess, national=["FFG/MPD", "TSU", "SEL"])
    got = {a.params["awipsId"]: a for a in src.poll()}
    assert got["FFGMPD"].event == "WPC Mesoscale Precipitation Discussion"
    assert got["FFGMPD"].office == "WPC"
    assert got["TSUWCA"].office == "NTWC"
    assert got["SEL5"].event == "SPC Watch Text" and got["SEL5"].office == "SPC"
    assert "pds" in got["SEL5"].tags


def test_awips_id():
    assert awips_id("\n000\nFXUS65 KBOU 301200\nAFDBOU\n\nArea Forecast Discussion") == "AFDBOU"
    assert awips_id("no header here") == ""
    a = parse_product({"@id": f"{BASE}/products/u", "productCode": "AFD", "productName": "Area Forecast Discussion",
                       "issuingOffice": "KBOU", "productText": "000\nFXUS65 KBOU 301200\nAFDBOU\n"}, "BOU")
    assert a.headline == "Area Forecast Discussion (AFDBOU)"


DISCUSSION = """:Product: Forecast Discussion
:Issued: 2026 Oct 30 1230 UTC
# Prepared by the U.S. Dept. of Commerce, NOAA, Space Weather Prediction Center
Solar Activity
G3 (Strong) storm levels are likely on 31 Oct.
"""


def test_swpc_text_product():
    a = parse_text_product(DISCUSSION, "discussion", "Space Weather Forecast Discussion", "Discussion", "u")
    assert a.id == "swpc-text:discussion:202610301230"
    assert a.office == "SWPC" and a.kind == "Discussion"
    assert "g3" not in a.tags and "forecast" in a.tags  # a forecast naming G3 isn't a G3 warning


def test_swpc_source_reads_text_files_and_skips_missing():
    routes = {"https://swpc/products/alerts.json": Resp([]),
              "https://swpc/text/discussion.txt": Resp(text=DISCUSSION)}
    sess = Session(routes)
    src = SWPCSource("https://swpc/products/alerts.json", "ua", session=sess)
    got = src.poll()
    assert [a.event for a in got] == ["Space Weather Forecast Discussion"]
    assert "weekly" in src._text_skip  # 404: tried again in an hour


def test_unknown_swpc_message_is_kept():
    a = parse_message({"product_id": "XYZ", "issue_datetime": "2026-10-30 12:00:00.000", "message":
                       "Space Weather Message Code: XYZK99\r\nSerial Number: 7\r\nIssue Time: 2026 Oct 30 1200 UTC\r\n\r\n"
                       "NOTICE: Something new\r\n"})
    assert a and a.kind == "Message" and a.event == "Space Weather Message"


def test_poller_keeps_going_after_a_buggy_source(tmp_path):
    from stormify.config import Config
    from stormify.db import Database
    from stormify.poller import Poller
    from stormify.sources.base import Source

    class Broken(Source):
        name = "broken"

        def poll(self):
            raise KeyError("oops")

    class Fine(Source):
        name = "fine"
        polled = False

        def poll(self):
            Fine.polled = True
            return []

    cfg = Config(db_path=str(tmp_path / "t.db"), zone_shapes=False)
    p = Poller(cfg, Database(cfg.db_path), sources=[Broken(), Fine()])
    p.poll_once()
    assert Fine.polled
    assert "broken" in p.db.heartbeat()["last_error"]


def test_offices_in_rules_get_every_product():
    items = [item("a", "AFD"), item("p", "PNS"), item("l", "AFD", office="KLIX")]
    sess = Session({"/products?office=KBOU&limit=50": items[:2], "/products?office=KLIX&limit=50": items[2:],
                    "/products/types/HWO/locations/GRR": []}, items)
    # GRR is in the config with explicit types; BOU and LIX are only in rules. Centers are ignored.
    src = NWSProductsSource(BASE, "ua", ["GRR"], ["HWO"], interval=0, session=sess,
                            rule_offices=lambda: ["bou", "KLIX", "SPC", "SWPC"])
    got = src.poll()
    assert sorted((a.office, a.params["productCode"]) for a in got) == [("BOU", "AFD"), ("BOU", "PNS"), ("LIX", "AFD")]
    assert "/products?office=KSPC&limit=50" not in sess.calls


def test_rule_offices_reads_rules_and_groups(tmp_path):
    from stormify.db import Database
    from stormify.poller import rule_offices
    db = Database(str(tmp_path / "t.db"))
    uid = db.create_user("scott", None, {"office_groups": {"home": ["BOU", "GJT"]}})
    db.replace_rules(uid, [{"name": "a", "scope": {"offices": ["lix"]}},
                           {"name": "b", "scope": {"groups": ["home"]}},
                           {"name": "off", "enabled": False, "scope": {"offices": ["OUN"]}}])
    assert rule_offices(db) == ["BOU", "GJT", "LIX"]
