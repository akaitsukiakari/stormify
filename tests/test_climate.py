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
    def __init__(self, content, ok=True):
        self.content, self.ok = content, ok


def test_push_links_the_dashboard_copy_with_public_url(db, cfg, channels, sent, tmp_path, monkeypatch):
    from stormify.web.app import create_app

    cfg.public_url = "https://wx.example.com"
    cfg.cache_dir = str(tmp_path / "cache")
    uid = db.get_user(name="scott")["id"]
    db.replace_rules(uid, [{"name": "BOU products", "kinds": ["Product"], "scope": {"offices": ["BOU"]}}])
    Engine(db, cfg, channel_factory=channels).process([cli()])
    n = sent[0]
    assert n.image_url.startswith("https://wx.example.com/img/") and n.image_name == "climate.png"

    calls = []
    monkeypatch.setattr(climate.time, "sleep", lambda s: None)
    replies = iter([_Resp(b"not yet", ok=False), _Resp(PNG)])

    def fake_get(self, url, **kw):
        calls.append(url)
        return next(replies)
    monkeypatch.setattr(climate.requests.Session, "get", fake_get)
    app = create_app(cfg, db)
    app.testing = True
    c = app.test_client()
    path = "/img/" + n.image_url.rsplit("/", 1)[-1]
    r = c.get(path)  # no login needed; retried once, then cached
    assert r.status_code == 200 and r.data == PNG and r.mimetype == "image/png"
    assert c.get(path).data == PNG
    assert calls == [climate.image_url(cli())] * 2


def test_fetch_image_gives_up_without_a_png(tmp_path, monkeypatch):
    monkeypatch.setattr(climate.time, "sleep", lambda s: None)

    class S:
        def get(self, url, **kw):
            return _Resp(b"<html>error</html>")
    assert climate.fetch_image(cli(), str(tmp_path), "(test)", session=S()) is None
