"""SPC outlooks / mesoscale discussions and SWPC space weather."""

import copy

from stormify.config import Config
from stormify.db import Database
from stormify.engine import Engine, is_significant, snapshot
from stormify.poller import Poller
from stormify.rules import load_rules
from stormify.sources.spc import SPCSource, parse_md, parse_outlook
from stormify.sources.swpc import SWPCSource, parse_message


def square(x, y, d=1.0):
    return {"type": "MultiPolygon", "coordinates": [[[[x, y], [x + d, y], [x + d, y + d], [x, y + d], [x, y]]]]}


def outlook_fc(labels=("TSTM", "MRGL", "SLGT", "ENH"), issue="202610091250"):
    feats = []
    for i, lab in enumerate(labels):
        feats.append({"type": "Feature", "geometry": square(-100 + i, 35 + i * 0.5, 4 - i),
                      "properties": {"DN": i + 2, "VALID": "202610091300", "EXPIRE": "202610101200",
                                     "ISSUE": issue, "FORECASTER": "Smith", "LABEL": lab,
                                     "LABEL2": lab + " Risk", "stroke": "#000", "fill": "#fff"}})
    return {"type": "FeatureCollection", "features": feats}


OUTLOOK_TEXT = """ACUS01 KWNS 091250
SWODY1
SPC AC 091250

Day 1 Convective Outlook
NWS Storm Prediction Center Norman OK
0750 AM CDT Fri Oct 09 2026

Valid 091300Z - 101200Z

...THERE IS AN ENHANCED RISK OF SEVERE THUNDERSTORMS ACROSS PARTS OF
THE SOUTHERN PLAINS...

...SUMMARY...
Severe storms are expected this afternoon.
"""

MD_TEXT = """ACUS11 KWNS 091845
SWOMCD
SPC MCD 091845
OKZ000-TXZ000-092045-

Mesoscale Discussion 1234
NWS Storm Prediction Center Norman OK
0145 PM CDT Fri Oct 09 2026

Areas affected...Western Oklahoma...Northwest Texas

Concerning...Severe potential...Tornado Watch likely

Valid 091845Z - 092045Z

Probability of Watch Issuance...80 percent

SUMMARY...Supercells capable of tornadoes and very large hail are
expected to develop over the next hour or two.

DISCUSSION...Storms are forming along the dryline.

..Smith.. 10/09/2026

...Please see www.spc.noaa.gov for graphic product...

ATTN...WFO...OUN...LUB...AMA...

LAT...LON   34539939 35289907 36130012 35010132 34539939

MOST PROBABLE PEAK TORNADO INTENSITY...95-120 MPH
MOST PROBABLE PEAK WIND GUST...65-80 MPH
MOST PROBABLE PEAK HAIL SIZE...1.50-2.50 IN
"""


def md_product(text=MD_TEXT, pid="abc-123"):
    return {"@id": f"https://api.weather.gov/products/{pid}", "id": pid, "productCode": "SWO",
            "issuanceTime": "2026-10-09T18:45:00+00:00", "productText": text}


# ---- SPC ------------------------------------------------------------------

def test_outlook_parse():
    a = parse_outlook(outlook_fc(), 1, OUTLOOK_TEXT)
    assert a.id == "spc:day1otlk:202610091250"
    assert a.event == "Day 1 Convective Outlook" and a.kind == "Outlook" and a.office == "SPC"
    assert a.headline == "Day 1: Enhanced risk"
    assert a.tags == ["risk-enh"] and a.params["level"] == 3 and a.params["risk_labels"][-1] == "ENH"
    assert a.thread_key == "spc:day1:20261010"
    assert a.expires.startswith("2026-10-10T12:00")
    assert len(a.geometry["geometries"]) == 4
    assert "ENHANCED RISK" in a.nws_headline and "Severe storms" in a.description


def test_outlook_without_storms():
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": None, "properties": {
        "DN": 0, "VALID": "202610091300", "EXPIRE": "202610101200", "ISSUE": "202610091250", "LABEL": ""}}]}
    a = parse_outlook(fc, 2)
    assert a.headline == "Day 2: no thunderstorms forecast" and a.tags == [] and a.geometry is None


def test_md_parse():
    a = parse_md(md_product())
    assert a.event == "Mesoscale Discussion" and a.kind == "Discussion"
    assert a.headline == "Mesoscale Discussion 1234 · Severe potential...Tornado Watch likely"
    assert a.area_desc == "Western Oklahoma...Northwest Texas"
    assert a.attn_offices == ["OUN", "LUB", "AMA"]
    assert a.states == ["OK", "TX"]
    assert {"tornado-watch", "watch-likely"} <= set(a.tags)
    assert a.params["watch_prob"] == 80 and a.hail_in == 2.5 and a.wind_mph == 80
    assert a.expires.startswith("2026-10-09T20:45")
    ring = a.geometry["coordinates"][0]
    assert ring[0] == [-99.39, 34.53] and ring[3] == [-101.32, 35.01] and ring[0] == ring[-1]
    assert a.thread_key == "spc:md:2026:1234"
    assert "Watch probability 80%" in a.nws_headline


def test_md_matches_office_scope_through_attn():
    a = parse_md(md_product())
    rules = load_rules([{"name": "MDs near Norman", "events": ["Mesoscale Discussion"],
                         "scope": {"offices": ["OUN"]}}])
    assert rules[0].matches_scope(a)
    assert not load_rules([{"name": "x", "scope": {"offices": ["BOU"]}}])[0].matches_scope(a)


def test_outlook_upgrade_is_significant():
    slight = parse_outlook(outlook_fc(("TSTM", "MRGL", "SLGT")), 1)
    enh = parse_outlook(outlook_fc(("TSTM", "MRGL", "SLGT", "ENH"), issue="202610091630"), 1)
    assert slight.thread_key == enh.thread_key
    assert is_significant(snapshot(slight), enh)
    assert not is_significant(snapshot(enh), slight)


class FakeResp:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}
        self.ok = status < 400

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, routes):
        self.routes, self.headers, self.calls = routes, {}, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for key, val in self.routes.items():
            if url.endswith(key):
                return val(headers or {}) if callable(val) else val
        return FakeResp({}, 404)


def test_spc_source_polls_outlooks_and_mds():
    routes = {
        "day1otlk_cat.lyr.geojson": FakeResp(outlook_fc(), headers={"ETag": "x"}),
        "/products/types/SWO/locations/DY1": FakeResp({"@graph": [
            {"id": "o1", "@id": "https://api.weather.gov/products/o1", "issuanceTime": "2026-10-09T12:50:00+00:00"}]}),
        "/products/o1": FakeResp({"productText": OUTLOOK_TEXT}),
        "/products/types/SWO/locations/MCD": FakeResp({"@graph": [
            {"id": "abc-123", "@id": "https://api.weather.gov/products/abc-123", "issuanceTime": "2026-10-09T18:45:00+00:00"}]}),
        "/products/abc-123": FakeResp(md_product()),
    }
    src = SPCSource("https://spc", "https://nws", "ua", days=(1,), session=FakeSession(routes))
    alerts = src.poll()
    assert [a.event for a in alerts] == ["Day 1 Convective Outlook", "Mesoscale Discussion"]
    assert "Severe storms" in alerts[0].description


def test_spc_source_skips_known():
    routes = {"/products/types/SWO/locations/MCD": FakeResp({"@graph": [
        {"id": "abc-123", "@id": "https://api.weather.gov/products/abc-123", "issuanceTime": "x"}]})}
    sess = FakeSession(routes)
    src = SPCSource("https://spc", "https://nws", "ua", days=(), session=sess,
                    known_ids=lambda ids: set(ids))
    assert src.poll() == []
    assert not any(c.endswith("/products/abc-123") for c in sess.calls)


# ---- SWPC -----------------------------------------------------------------

WATCH = {"product_id": "A50F", "issue_datetime": "2026-10-09 12:30:00.000", "message":
         "Space Weather Message Code: WATA50\r\nSerial Number: 123\r\nIssue Time: 2026 Oct 09 1230 UTC\r\n\r\n"
         "WATCH: Geomagnetic Storm Category G3 Predicted\r\n\r\nHighest Storm Level Predicted by Day:\r\n"
         "Oct 10:  G3 (Strong)   Oct 11:  G1 (Minor)   Oct 12:  None (Below G1)\r\n\r\n"
         "THIS SUPERSEDES ANY/ALL PRIOR WATCHES IN EFFECT\r\n\r\nNOAA Space Weather Scale descriptions can be found at\r\n"
         "www.swpc.noaa.gov/noaa-scales-explanation"}
WARNING = {"product_id": "K05W", "issue_datetime": "2026-10-10 03:00:00.000", "message":
           "Space Weather Message Code: WARK05\r\nSerial Number: 900\r\nIssue Time: 2026 Oct 10 0300 UTC\r\n\r\n"
           "WARNING: Geomagnetic K-index of 5 expected\r\nValid From: 2026 Oct 10 0300 UTC\r\n"
           "Valid To: 2026 Oct 10 1200 UTC\r\nWarning Condition: Onset\r\nNOAA Scale: G1 - Minor"}
EXTENDED = {"product_id": "K05W", "issue_datetime": "2026-10-10 11:50:00.000", "message":
            "Space Weather Message Code: WARK05\r\nSerial Number: 901\r\nIssue Time: 2026 Oct 10 1150 UTC\r\n\r\n"
            "EXTENDED WARNING: Geomagnetic K-index of 5 expected\r\nExtension to Serial Number: 900\r\n"
            "Valid From: 2026 Oct 10 0300 UTC\r\nNow Valid Until: 2026 Oct 10 2100 UTC\r\nNOAA Scale: G1 - Minor"}
ALERT = {"product_id": "K07A", "issue_datetime": "2026-10-10 06:00:00.000", "message":
         "Space Weather Message Code: ALTK07\r\nSerial Number: 55\r\nIssue Time: 2026 Oct 10 0600 UTC\r\n\r\n"
         "ALERT: Geomagnetic K-index of 7\r\nThreshold Reached: 2026 Oct 10 0559 UTC\r\nSynoptic Period: 0300-0600 UTC\r\n"
         "\r\nActive Warning: Yes\r\nNOAA Scale: G3 - Strong"}
XRAY = {"product_id": "XM5S", "issue_datetime": "2026-10-10 07:00:00.000", "message":
        "Space Weather Message Code: SUMX01\r\nSerial Number: 77\r\nIssue Time: 2026 Oct 10 0700 UTC\r\n\r\n"
        "SUMMARY: X-ray Event exceeded M5\r\nBegin Time: 2026 Oct 10 0640 UTC\r\nNOAA Scale: R2 - Moderate"}


def test_swpc_watch():
    a = parse_message(WATCH)
    assert a.event == "Geomagnetic Storm Watch" and a.kind == "Watch" and a.office == "SWPC"
    assert a.headline == "Watch: Geomagnetic Storm Category G3 Predicted"
    assert "g3" in a.tags and "geomagnetic" in a.tags and a.severity == "Severe"
    assert a.id == "swpc:WATA50:123" and a.params["scale_desc"] == "G3 (Strong)"


def test_swpc_warning_extension_threads():
    w, ext = parse_message(WARNING), parse_message(EXTENDED)
    assert w.thread_key == ext.thread_key == "swpc:K05:900"
    assert ext.message_type == "Update" and ext.expires.startswith("2026-10-10T21:00")
    assert w.expires.startswith("2026-10-10T12:00") and "g1" in w.tags


def test_swpc_alert_and_summary():
    a = parse_message(ALERT)
    assert a.event == "Geomagnetic Storm Alert" and "observed" in a.tags and "g3" in a.tags
    x = parse_message(XRAY)
    assert x.event == "Radio Blackout Summary" and x.kind == "Statement" and "r2" in x.tags and "radio" in x.tags


def test_swpc_kp_without_scale_line():
    msg = copy.deepcopy(WARNING)
    msg["message"] = msg["message"].replace("K-index of 5", "K-index of 6").replace("\r\nNOAA Scale: G1 - Minor", "")
    a = parse_message(msg)
    assert "g2" in a.tags


def test_swpc_source_conditional():
    calls = []

    def route(headers):
        calls.append(headers)
        if headers.get("If-None-Match") == "e1":
            return FakeResp(None, 304)
        return FakeResp([WATCH, WARNING, {"product_id": "junk", "message": "nothing useful"}], headers={"ETag": "e1"})

    src = SWPCSource("https://swpc/alerts.json", "ua", session=FakeSession({"alerts.json": route}))
    assert [a.event for a in src.poll()] == ["Geomagnetic Storm Watch", "Geomagnetic Storm Warning"]
    assert src.poll() is None


def test_swpc_push_rule_and_first_poll_quiet(tmp_path, channels, sent):
    cfg = Config(db_path=str(tmp_path / "s.db"), quiet_first_run=True)
    db = Database(cfg.db_path)
    uid = db.create_user("scott", None, {"channels": {"ntfy": {"topic": "x"}}})
    db.replace_rules(uid, [{"name": "G3+", "tags_any": ["g3", "g4", "g5"], "scope": {"offices": ["SWPC"]}}])
    engine = Engine(db, cfg, channel_factory=channels)
    # Existing install: the NWS feed has been running.
    from fixtures import collection
    from stormify.sources.nws import parse_collection
    engine.process(parse_collection(collection()))

    class Static:
        name = "swpc"

        def __init__(self, items):
            self.items = items

        def poll(self):
            return [parse_message(i) for i in self.items]

    p = Poller(cfg, db, sources=[Static([WATCH])], engine=engine)
    p.poll_once()
    assert sent == []  # first SWPC poll on an existing install: archived, not pushed
    p.sources = [Static([WATCH, ALERT])]
    p.poll_once()
    assert [n.title for n in sent] == ["Alert: Geomagnetic K-index of 7"]
    assert "G3 (Strong)" in sent[0].body
