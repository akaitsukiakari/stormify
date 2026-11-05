from stormify import climate
from stormify.delivery.ntfy import NtfyChannel
from stormify.engine import Engine
from stormify.sources.nws_products import parse_product

BASE = "https://api.weather.gov"

MORNING = """
000
CDUS45 KBOU 020730
CLIDEN

CLIMATE REPORT
NATIONAL WEATHER SERVICE DENVER/BOULDER CO
130 AM MDT SUN NOV 02 2026

...................................

...THE DENVER CO CLIMATE SUMMARY FOR NOVEMBER 1 2026...

CLIMATE NORMAL PERIOD 1991 TO 2020
CLIMATE RECORD PERIOD 1872 TO 2026


WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
                VALUE   (LST)  VALUE       VALUE  FROM      YEAR
                                                  NORMAL
...................................................................
TEMPERATURE (F)
 YESTERDAY
  MAXIMUM         68    246 PM  81    1950  59      9       64
  MINIMUM         35    645 AM   5    1991  31      4       38
  AVERAGE         52                        45      7       51

PRECIPITATION (IN)
  YESTERDAY        0.00          0.88 1946   0.03  -0.03     0.00
  MONTH TO DATE    0.00                      0.03  -0.03     0.00

SNOWFALL (IN)
  YESTERDAY        0.0           6.5  1946   0.2  -0.2      0.0
  MONTH TO DATE    0.0                       0.2  -0.2      0.0
"""

AFTERNOON = """
000
CDUS45 KBOU 022230
CLIDEN

CLIMATE REPORT
NATIONAL WEATHER SERVICE DENVER/BOULDER CO
430 PM MST SUN NOV 2 2026

...THE DENVER CO CLIMATE SUMMARY FOR NOVEMBER 2 2026...
VALID TODAY AS OF 0400 PM LOCAL TIME.

TEMPERATURE (F)
 TODAY
  MAXIMUM         71R   210 PM  70    1950  58     13       60
  MINIMUM         -4    700 AM   5    1991  31      4       38

PRECIPITATION (IN)
  TODAY             T            0.88 1946   0.03  -0.03     0.00

SNOWFALL (IN)
  TODAY            MM            6.5  1946   0.2  -0.2      0.0
"""


def cli(uid="c1", text=MORNING, when="2026-11-02T07:30:00+00:00", awips="DEN"):
    return parse_product({"@id": f"{BASE}/products/{uid}", "id": uid, "wmoCollectiveId": "CDUS45",
                          "issuingOffice": "KBOU", "issuanceTime": when, "productCode": "CLI",
                          "productName": "Climatological Report (Daily)",
                          "productText": text.replace("CLIDEN", "CLI" + awips)}, "BOU")


def test_summary_and_date():
    assert climate.summary(MORNING) == 'Denver CO, Nov 1: High 68 · Low 35 · Precip 0.00" · Snow 0.0"'
    assert climate.summary(AFTERNOON) == "Denver CO, Nov 2 so far: High 71 · Low -4 · Precip T"
    assert climate.summary("nothing here") == ""


def test_parse_product_adds_summary():
    a = cli()
    assert climate.is_cli(a)
    assert a.nws_headline.startswith("Denver CO, Nov 1: High 68")
    assert a.params["issuingOffice"] == "KBOU"


def test_image_url_matches_iembot():
    assert climate.image_url(cli()) == (
        "https://mesonet.agron.iastate.edu/plotting/auto/plot/218/network:NWSCLI::station:KDEN::"
        "date:2026-11-01::_:202611020730-KBOU-CDUS45-CLIDEN.png")
    assert "station:KDDC::" in climate.image_url(cli(awips="DGC"))
    afd = parse_product({"@id": f"{BASE}/products/a", "id": "a", "issuingOffice": "KBOU", "productCode": "AFD",
                         "productText": "000\nFXUS65 KBOU 071530\nAFDBOU\n"}, "BOU")
    assert climate.image_url(afd) is None


def test_push_carries_the_infographic(db, cfg, channels, sent):
    uid = db.get_user(name="scott")["id"]
    db.replace_rules(uid, [{"name": "BOU products", "kinds": ["Product"], "scope": {"offices": ["BOU"]}}])
    Engine(db, cfg, channel_factory=channels).process([cli()])
    n = sent[0]
    assert n.image_url == climate.image_url(cli())
    assert n.image_name == "climate.png"
    assert n.body.splitlines()[0].startswith('Denver CO, Nov 1: High 68 · Low 35')
    assert "High 68 (normal 59, +9 · record 81 in 1950)" in n.body.splitlines()
    assert NtfyChannel({"topic": "t"}).payload(n)["filename"] == "climate.png"


def test_doc_rule_matches_only_that_station():
    from stormify.rules import load_rules
    rule = load_rules([{"name": "Denver", "kinds": ["Product"], "scope": {"offices": ["BOU"]},
                        "filters": {"include_any": ["(CLIDEN)"]}}])[0]
    assert rule.passes_filters(cli())[0]
    assert not rule.passes_filters(cli(awips="AKO"))[0]


def test_details_normals_and_records():
    assert climate.details(MORNING).splitlines() == [
        "High 68 (normal 59, +9 · record 81 in 1950)",
        "Low 35 (normal 31, +4 · record 5 in 1991)",
        'Precip 0.00" (normal 0.03", -0.03 · record 0.88" in 1946)',
        'Snow 0.0" (normal 0.2", -0.2 · record 6.5" in 1946)',
    ]
    lines = climate.details(AFTERNOON).splitlines()
    assert lines[0] == "NEW RECORD High 71 (normal 58, +13 · record 70 in 1950)"
    assert lines[2] == 'Precip T (normal 0.03", -0.03 · record 0.88" in 1946)'
    assert len(lines) == 3  # snowfall missing (MM)
    assert climate.details("nothing") == ""


PNG = b"\x89PNG\r\n\x1a\nfake"


class _Resp:
    def __init__(self, content=b"", ok=True, data=None):
        self.content, self.ok, self.data = content, ok, data

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError("bad")

    def json(self):
        return self.data


def listing(*rows):
    return _Resp(data={"results": [{"valid": v, "product": p} for v, p in rows]})


class FakeIEM:
    """IEM's CLI listing replies in turn (the last one repeats), and the picture."""

    def __init__(self, *listings):
        self.listings, self.calls = list(listings), []

    def get(self, url, params=None, **kw):
        self.calls.append(url if not params else f"{url}?{params['station']}/{params['year']}")
        if url == climate.IEM_JSON:
            return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
        return _Resp(PNG)


OLD = ("2026-11-01", "202611012300-KBOU-CDUS45-CLIDEN")      # yesterday's afternoon report
MINE = ("2026-11-01", "202611020730-KBOU-CDUS45-CLIDEN")     # the morning report the push is for


def test_push_links_the_dashboard_copy_with_public_url(db, cfg, channels, sent, tmp_path, monkeypatch):
    from stormify.web.app import create_app

    cfg.public_url = "https://wx.example.com"
    cfg.cache_dir = str(tmp_path / "cache")
    uid = db.get_user(name="scott")["id"]
    db.replace_rules(uid, [{"name": "BOU products", "kinds": ["Product"], "scope": {"offices": ["BOU"]}}])
    Engine(db, cfg, channel_factory=channels).process([cli()])
    n = sent[0]
    assert n.image_url.startswith("https://wx.example.com/img/") and n.image_name == "climate.png"

    monkeypatch.setattr(climate.time, "sleep", lambda s: None)
    fake = FakeIEM(listing(OLD), listing(MINE))
    monkeypatch.setattr(climate.requests.Session, "get", lambda self, url, **kw: fake.get(url, **kw))
    app = create_app(cfg, db)
    app.testing = True
    c = app.test_client()
    path = "/img/" + n.image_url.rsplit("/", 1)[-1]
    r = c.get(path)  # no login needed; waits for IEM to read the report, then caches the picture
    assert r.status_code == 200 and r.data == PNG and r.mimetype == "image/png"
    assert c.get(path).data == PNG
    j = f"{climate.IEM_JSON}?KDEN/2026"
    assert fake.calls == [j, j, climate.image_url(cli())]


def test_no_picture_until_iem_has_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(climate.time, "sleep", lambda s: None)
    fake = FakeIEM(listing(OLD))
    assert climate.fetch_image(cli(), str(tmp_path), "(test)", session=fake) is None
    assert climate.image_url(cli()) not in fake.calls  # never asks for (or keeps) the old picture
    # A minute or two off the API's issuance time, or a later correction, still counts.
    later = ("2026-11-01", "202611020732-KBOU-CDUS45-CLIDEN")
    assert climate.fetch_image(cli(), str(tmp_path), "(test)", session=FakeIEM(listing(OLD, later))) == PNG


def test_unreadable_listing_still_sends_but_does_not_keep(tmp_path):
    fake = FakeIEM(_Resp(ok=False))
    assert climate.fetch_image(cli(), str(tmp_path), "(test)", session=fake) == PNG
    assert not (tmp_path / "climate").exists()
    other = _Resp(data={"results": [{"station": "KDSM", "valid": "2026-11-01", "product": "x"}]})
    assert climate.iem_has_report(cli(), FakeIEM(other), "(test)") is None


def test_fetch_image_gives_up_without_a_png(tmp_path):
    class S(FakeIEM):
        def get(self, url, params=None, **kw):
            return listing(MINE) if url == climate.IEM_JSON else _Resp(b"<html>error</html>")
    assert climate.fetch_image(cli(), str(tmp_path), "(test)", session=S(listing(MINE))) is None
