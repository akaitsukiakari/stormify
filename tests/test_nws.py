from fixtures import by_id, collection

from stormify.sources.nws import parse_collection, parse_feature


def test_parses_whole_collection():
    alerts = parse_collection(collection())
    assert len(alerts) == len(collection()["features"])


def test_tornado_warning_basics():
    a = parse_feature(by_id("tor1"))
    assert a.event == "Tornado Warning"
    assert a.office == "BOU"
    assert a.kind == "Warning"
    assert a.vtec_action == "NEW"
    assert a.thread_key == "KBOU.TO.W.0012.2026"
    assert a.hail_in == 1.0
    assert a.states == ["CO"]
    assert a.geometry["type"] == "Polygon"
    assert a.tags == []


def test_update_shares_thread_and_gets_observed_tag():
    a = parse_feature(by_id("tor1u"))
    assert a.thread_key == "KBOU.TO.W.0012.2026"
    assert a.message_type == "Update"
    assert a.vtec_action == "CON"
    assert "observed" in a.tags


def test_tornado_emergency_tags():
    a = parse_feature(by_id("tor2"))
    assert a.office == "FWD"
    assert {"emergency", "pds", "observed"} <= set(a.tags)
    assert a.kind == "Emergency"


def test_destructive_svr_with_hail_and_wind():
    a = parse_feature(by_id("svr1"))
    assert "destructive" in a.tags
    assert a.hail_in == 2.75
    assert a.wind_mph == 80


def test_office_from_awips_when_no_vtec():
    a = parse_feature(by_id("sps1"))
    assert a.office == "BOU"
    assert a.thread_key == a.id  # no VTEC, no references


def test_test_status_tagged():
    assert "test" in parse_feature(by_id("test1")).tags


def test_bad_feature_does_not_break_collection():
    data = collection()
    data["features"].insert(0, {"properties": None})
    errors = []
    alerts = parse_collection(data, on_error=lambda e, f: errors.append(e))
    assert len(alerts) == len(collection()["features"])
    assert errors
