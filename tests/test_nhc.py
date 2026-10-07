from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from stormify.cli import example_rules, main
from stormify.config import Config
from stormify.db import Database
from stormify.engine import Engine
from stormify.sources.nhc import NHCSource, formation_summary, next_advisory, parse_feed

DATA = Path(__file__).parent / "data"
AT = (DATA / "nhc_at.xml").read_bytes()
CP = (DATA / "nhc_cp.xml").read_bytes()


def by_pil(alerts):
    return {a.params["pil"]: a for a in alerts}


def tcp(adv: str, wind: int, *, wmo_time: str, pub: str, ww: str = "", changes: str = "None.",
        headline: str = "...ISAIAS STRENGTHENING...", storm: str = "Tropical Storm Isaias") -> str:
    """A minimal Atlantic feed holding one public advisory."""
    summary = f"SUMMARY OF WATCHES AND WARNINGS IN EFFECT:\n\n{ww}\n" if ww else \
        "There are no coastal watches or warnings in effect.\n"
    kind = "Intermediate Advisory" if adv[-1].isalpha() else "Advisory"
    text = f"""000
WTNT34 KNHC {wmo_time}
TCPAT4

BULLETIN
{storm} {kind} Number  {adv}
NWS National Hurricane Center Miami FL       AL092026
400 PM CDT Wed Oct 07 2026

{headline}

SUMMARY OF 400 PM CDT...2100 UTC...INFORMATION
----------------------------------------------
LOCATION...23.0N 92.0W
ABOUT 200 MI...320 KM NW OF PROGRESO MEXICO
MAXIMUM SUSTAINED WINDS...{wind} MPH...100 KM/H
PRESENT MOVEMENT...NE OR 45 DEGREES AT 9 MPH...15 KM/H
MINIMUM CENTRAL PRESSURE...994 MB...29.35 INCHES

WATCHES AND WARNINGS
--------------------
CHANGES WITH THIS ADVISORY:

{changes}

{summary}
DISCUSSION AND OUTLOOK
----------------------
Isaias is strengthening.

NEXT ADVISORY
-------------
Next complete advisory at 1000 PM CDT.

$$
Forecaster Blake"""
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:nhc="https://www.nhc.noaa.gov"><channel><title>NHC Atlantic</title>
<item><title>{storm} Public Advisory Number {adv}</title>
<description>Issued at 400 PM CDT <![CDATA[<pre>{text}</pre>]]></description>
<pubDate>{pub}</pubDate><link>https://www.nhc.noaa.gov/text/MIATCPAT4.shtml</link></item>
</channel></rss>"""


# ---- parsing ---------------------------------------------------------------

def test_atlantic_feed_products():
    alerts = by_pil(parse_feed(AT, "at"))
    # Storm summary and the graphics item are not products.
    assert set(alerts) == {"TWOAT", "TCPAT4", "TCMAT4", "TCDAT4"}

    a = alerts["TCPAT4"]
    assert a.id == "nhc:TCPAT4:202610071453"
    assert a.source == "nhc" and a.office == "NHC" and a.kind == "Advisory"
    assert a.event == "Tropical Cyclone Public Advisory"
    assert a.headline == "Tropical Storm Isaias Public Advisory Number 4"
    assert a.nws_headline == ("...HURRICANE AND STORM SURGE WATCHES ISSUED FOR PORTIONS OF THE "
                              "NORTHERN GULF COAST FOR STRENGTHENING ISAIAS...")
    assert a.thread_key == "NHC.AL092026.TCP"
    assert a.wind_mph == 45
    assert a.area_desc == "22.4N 93.6W, 260 mi WNW of Progreso Mexico"
    assert a.sent == "2026-10-07T14:53:10+00:00" and a.event_tz == "America/Chicago"
    assert a.expires == "2026-10-07T18:00:00+00:00"   # next intermediate advisory, 100 PM CDT
    assert set(a.tags) == {"atlantic", "tropical-storm", "hurricane-watch", "surge-watch", "ts-watch",
                           "ww-changes"}
    assert a.params["storm"] == "Tropical Storm Isaias"
    assert a.params["product"] == "Advisory 4"
    assert a.params["storm_stats"] == "45 mph · 1000 mb · ENE at 8 mph"
    assert a.params["watches"] == "Hurricane Watch, Storm Surge Watch, TS Watch"
    assert a.description.startswith("WTNT34 KNHC 071453\nTCPAT4")

    point, track = a.geometry["geometries"]
    assert point == {"type": "Point", "coordinates": [-93.6, 22.4]}
    # Forecast track from the discussion, starting at the current center.
    assert track["coordinates"][0] == [-93.6, 22.4]
    assert track["coordinates"][-1] == [-87.5, 38.2]

    # The older forecast advisory and discussion carry their own vitals, not the summary's.
    assert alerts["TCMAT4"].wind_mph == 40 and alerts["TCMAT4"].params["pressure"] == "1004 mb"
    assert alerts["TCDAT4"].wind_mph == 40 and alerts["TCDAT4"].kind == "Other"
    assert alerts["TCDAT4"].geometry is None

    two = alerts["TWOAT"]
    assert two.event == "Tropical Weather Outlook" and two.kind == "Outlook"
    assert two.thread_key == "NHC.TWOAT"
    assert two.nws_headline == "No formation expected in the next 7 days"
    assert two.params["storm"] == "Atlantic"


def test_central_pacific_feed_with_no_storms():
    alerts = parse_feed(CP, "cp")
    assert [a.id for a in alerts] == ["nhc:TWOCP:202610071137"]
    assert alerts[0].office == "CPHC" and alerts[0].tags == ["central-pacific"]


def test_bad_xml_is_a_value_error():
    with pytest.raises(ValueError):
        parse_feed(b"<rss><channel>", "at")


def test_formation_summary():
    text = """South of Southern Mexico (EP92):
* Formation chance through 48 hours...high...90 percent.
* Formation chance through 7 days...high...90 percent.
South of Guatemala:
* Formation chance through 48 hours...low...near 0 percent.
* Formation chance through 7 days...low...30 percent."""
    assert formation_summary(text) == "48h high 90% · 7d high 90%; 48h low 0% · 7d low 30%"


def test_next_advisory_rolls_past_midnight():
    sent = datetime(2026, 10, 8, 3, 50, tzinfo=timezone.utc)   # 10:50 PM CDT on the 7th
    nxt = next_advisory("Next complete advisory at 400 AM CDT.", sent)
    assert nxt == datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)


def test_intermediate_and_major_hurricane_tags():
    a = parse_feed(tcp("7A", 125, wmo_time="081750", pub="Thu, 08 Oct 2026 17:50:00 GMT",
                       storm="Hurricane Isaias"), "at")[0]
    assert a.id == "nhc:TCPAT4:202610081750"
    assert {"intermediate", "hurricane", "major-hurricane"} <= set(a.tags)
    assert "ww-changes" not in a.tags
    assert a.params["product"] == "Advisory 7A"


def test_tropical_cyclone_update():
    text = """000
WTNT64 KNHC 090300
TCUAT4

Hurricane Isaias Tropical Cyclone Update
NWS National Hurricane Center Miami FL       AL092026
1000 PM CDT Thu Oct 08 2026

...ISAIAS BECOMES A MAJOR HURRICANE...

Data from an Air Force Reserve Hurricane Hunter aircraft indicate
that maximum sustained winds have increased to near 115 mph.

SUMMARY OF 1000 PM CDT...0300 UTC...INFORMATION
-----------------------------------------------
LOCATION...25.4N 88.6W
MAXIMUM SUSTAINED WINDS...115 MPH...185 KM/H
PRESENT MOVEMENT...N OR 360 DEGREES AT 10 MPH...17 KM/H
MINIMUM CENTRAL PRESSURE...958 MB...28.29 INCHES

$$
Forecaster Blake"""
    xml = f"""<rss version="2.0"><channel>
<item><title>Hurricane Isaias Tropical Cyclone Update</title>
<description><![CDATA[<pre>{text}</pre>]]></description>
<pubDate>Fri, 09 Oct 2026 03:00:12 GMT</pubDate></item></channel></rss>"""
    a = parse_feed(xml, "at")[0]
    assert a.event == "Tropical Cyclone Update" and a.thread_key == "NHC.AL092026.TCU"
    assert a.nws_headline == "...ISAIAS BECOMES A MAJOR HURRICANE..."
    assert a.params["storm"] == "Hurricane Isaias" and a.params["product"] == "Update"
    assert a.wind_mph == 115 and "major-hurricane" in a.tags
    assert a.geometry is None


# ---- rules + notifications -------------------------------------------------

@pytest.fixture
def tropical_db(cfg):
    d = Database(cfg.db_path)
    uid = d.create_user("scott", None, {"timezone": "America/Denver", "channels": {"ntfy": {"topic": "x"}}})
    d.replace_rules(uid, example_rules() + example_rules("tropical"))
    return d


def test_advisory_notification(tropical_db, cfg, channels, sent):
    eng = Engine(tropical_db, cfg, channel_factory=channels)
    eng.process(parse_feed(AT, "at") + parse_feed(CP, "cp"))
    assert len(sent) == 1   # the public advisory; outlooks, discussion, forecast advisory just log
    n = sent[0]
    assert n.title == "Tropical Storm Isaias · Advisory 4"
    assert n.body.splitlines() == [
        "45 mph · 1000 mb · ENE at 8 mph",
        "Hurricane Watch, Storm Surge Watch, TS Watch",
        "...HURRICANE AND STORM SURGE WATCHES ISSUED FOR PORTIONS OF THE NORTHERN GULF COAST FOR "
        "STRENGTHENING ISAIAS...",
    ]
    assert n.priority == 3 and n.group == "NHC.AL092026.TCP"


def test_later_advisories_push_only_when_significant(tropical_db, cfg, channels, sent):
    eng = Engine(tropical_db, cfg, channel_factory=channels)
    eng.process(parse_feed(AT, "at"))
    assert len(sent) == 1
    watches = ("A Hurricane Watch is in effect for...\n* Bay St. Louis to Indian Pass\n\n"
               "A Storm Surge Watch is in effect for...\n* Mouth of the Mississippi River to Yankeetown\n\n"
               "A Tropical Storm Watch is in effect for...\n* East of Indian Pass to Aucilla River\n")
    # Same winds, same watches: logged.
    eng.process(parse_feed(tcp("4A", 45, wmo_time="071755", pub="Wed, 07 Oct 2026 17:55:00 GMT",
                               ww=watches), "at"))
    assert len(sent) == 1
    # Stronger: pushes as an update.
    eng.process(parse_feed(tcp("5", 60, wmo_time="072055", pub="Wed, 07 Oct 2026 20:55:00 GMT",
                               ww=watches), "at"))
    assert len(sent) == 2 and sent[-1].title == "Tropical Storm Isaias · Advisory 5"
    # A new warning type: pushes even without stronger winds.
    warn = watches + "\nA Hurricane Warning is in effect for...\n* Bay St. Louis to Indian Pass\n"
    eng.process(parse_feed(tcp("6", 60, wmo_time="080255", pub="Thu, 08 Oct 2026 02:55:00 GMT",
                               ww=warn, changes="The Hurricane Watch has been upgraded."), "at"))
    assert len(sent) == 3 and "Hurricane Warning" in sent[-1].body
    # Seeing the same advisory again is a no-op.
    eng.process(parse_feed(tcp("6", 60, wmo_time="080255", pub="Thu, 08 Oct 2026 02:55:00 GMT",
                               ww=warn), "at"))
    assert len(sent) == 3


# ---- source polling --------------------------------------------------------

class FakeResp:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.headers = responses, [], {}

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        r = self.responses[url.rsplit("/", 1)[1]]
        return r.pop(0) if isinstance(r, list) else r


def test_poll_uses_conditional_requests():
    s = FakeSession({
        "index-at.xml": [FakeResp(200, AT, {"Last-Modified": "Wed, 07 Oct 2026 14:55:22 GMT", "ETag": '"a1"'}),
                         FakeResp(304)],
        "index-cp.xml": [FakeResp(200, CP), FakeResp(304)],
    })
    src = NHCSource("https://www.nhc.noaa.gov", "ua", ["at", "cp"], session=s)
    first = src.poll()
    assert len(first) == 5
    assert src.poll() is None
    assert s.calls[2] == ("https://www.nhc.noaa.gov/index-at.xml",
                          {"If-None-Match": '"a1"', "If-Modified-Since": "Wed, 07 Oct 2026 14:55:22 GMT"})


def test_poll_survives_one_basin_failing_but_not_all():
    s = FakeSession({"index-at.xml": FakeResp(200, AT), "index-ep.xml": FakeResp(503)})
    assert len(NHCSource("https://x", "ua", ["at", "ep"], session=s).poll()) == 4
    s = FakeSession({"index-at.xml": FakeResp(500), "index-ep.xml": FakeResp(503)})
    with pytest.raises(requests.RequestException):
        NHCSource("https://x", "ua", ["at", "ep"], session=s).poll()


def test_poller_includes_nhc_unless_disabled(tmp_path):
    from stormify.poller import Poller
    db = Database(str(tmp_path / "p.db"))
    assert [s.name for s in Poller(Config(), db).sources] == ["nws", "nhc"]
    assert [s.name for s in Poller(Config(nhc_enabled=False), db).sources] == ["nws"]


# ---- cli -------------------------------------------------------------------

def test_rules_add_example_appends_once(tmp_path, capsys):
    conf = tmp_path / "config.toml"
    conf.write_text(f'[general]\ndb_path = "{tmp_path / "c.db"}"\n')
    main(["-c", str(conf), "init", "--user", "scott", "--password", "pw"])
    before = len(example_rules())
    main(["-c", str(conf), "rules", "add", "--example", "tropical", "--user", "scott"])
    main(["-c", str(conf), "rules", "add", "--example", "tropical", "--user", "scott"])
    out = capsys.readouterr().out
    assert out.count("added   ") == 3 and out.count("skipped ") == 3
    rules = Database(str(tmp_path / "c.db")).list_rules(1)
    assert len(rules) == before + 3
    assert rules[-1]["name"] == "Tropical watches, nationwide"
