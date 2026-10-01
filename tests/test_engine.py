import copy

from fixtures import by_id, collection, feature

from stormify.engine import Engine
from stormify.sources.nws import parse_collection, parse_feature


def run(engine, features, **kw):
    return engine.process(parse_collection({"features": features}), **kw)


def titles(sent):
    return [n.title for n in sent]


def test_full_collection_pushes_the_right_things(db, cfg, channels, sent):
    eng = Engine(db, cfg, channel_factory=channels)
    res = eng.process(parse_collection(collection()))
    t = titles(sent)
    assert res.new_alerts == len(collection()["features"])
    # Original tornado warning + its update in one poll: one buzz, with the newest info.
    tor_bou = [x for x in t if "Tornado Warning · BOU" in x]
    assert len(tor_bou) == 1 and "OBSERVED" in tor_bou[0] and not tor_bou[0].startswith("UPDATE")
    assert any(x.startswith("EMERGENCY") and "FWD" in x for x in t)
    assert any("Severe Thunderstorm Warning · OUN" in x for x in t)
    assert not any("DDC" in x for x in t)              # ordinary SVR, not my office
    assert any("Tornado Watch · ICT" in x for x in t)
    assert sum("Special Weather Statement" in x for x in t) == 1   # pea-size one filtered
    assert not any("Winter Weather Advisory" in x for x in t)       # log-only rule
    assert any("Flood Warning · LOT" in x for x in t)
    assert not any("TEST" in x for x in t)              # status=Test never pushes
    assert len(sent) == 6
    emergency = next(n for n in sent if n.title.startswith("EMERGENCY"))
    assert emergency.priority == 5


def test_everything_is_archived_with_decisions(db, cfg, channels):
    Engine(db, cfg, channel_factory=channels).process(parse_collection(collection()))
    uid = db.get_user(name="scott")["id"]
    feed = db.query_feed(uid, limit=100)
    assert len(feed) == len(collection()["features"])
    pushed = [r for r in feed if r["action"] == "push"]
    assert len(pushed) == 6
    sps = next(r for r in feed if r["id"].endswith(".sps1"))
    assert sps["action"] == "log" and "pea" in sps["reason"]


def test_seen_alerts_are_not_reprocessed(db, cfg, channels, sent):
    eng = Engine(db, cfg, channel_factory=channels)
    eng.process(parse_collection(collection()))
    n = len(sent)
    res = eng.process(parse_collection(collection()))
    assert res.new_alerts == 0 and len(sent) == n


def test_update_significance(db, cfg, channels, sent):
    eng = Engine(db, cfg, channel_factory=channels)
    run(eng, [by_id("tor1")])
    assert len(sent) == 1
    # Same storm, nothing new: suppressed.
    boring = feature("tor1b", "Tornado Warning", office_vtec="KBOU", phen="TO", sig="W", etn="0012",
                     action="CON", message_type="Update", sent="2026-06-01T16:50:00-06:00",
                     params={"tornadoDetection": ["RADAR INDICATED"], "maxHailSize": ["1.00"]})
    run(eng, [boring])
    assert len(sent) == 1
    uid = db.get_user(name="scott")["id"]
    row = next(r for r in db.query_feed(uid) if r["id"].endswith("tor1b"))
    assert row["reason"] == "update not significant"
    # Tornado confirmed: significant, pushes as UPDATE.
    run(eng, [by_id("tor1u")])
    assert len(sent) == 2 and sent[-1].title.startswith("UPDATE") and "OBSERVED" in sent[-1].title


def test_new_only_suppresses_watch_updates(db, cfg, channels, sent):
    eng = Engine(db, cfg, channel_factory=channels)
    run(eng, [by_id("toa1")])
    upd = feature("toa1x", "Tornado Watch", sender="NWS Wichita KS", office_vtec="KICT", phen="TO", sig="A",
                  etn="0412", action="EXT", message_type="Update", sent="2026-06-01T15:00:00-05:00",
                  severity="Extreme")
    run(eng, [upd])
    assert len(sent) == 1


def test_cancel_of_emergency_notifies(db, cfg, channels, sent):
    eng = Engine(db, cfg, channel_factory=channels)
    run(eng, [by_id("tor2")])
    cancel = feature("tor2c", "Tornado Warning", sender="NWS Fort Worth TX", office_vtec="KFWD", phen="TO",
                     sig="W", etn="0044", action="CAN", message_type="Cancel", area="Tarrant, TX",
                     ugc=("TXC439",), same=("048439",), sent="2026-06-01T18:00:00-05:00",
                     description="The tornado warning has been cancelled.")
    run(eng, [cancel])
    assert sent[-1].title.startswith("CANCELLED")


def test_cancel_of_regular_warning_is_quiet(db, cfg, channels, sent):
    eng = Engine(db, cfg, channel_factory=channels)
    run(eng, [by_id("tor1")])
    cancel = feature("tor1c", "Tornado Warning", office_vtec="KBOU", phen="TO", sig="W", etn="0012",
                     action="CAN", message_type="Cancel", sent="2026-06-01T17:05:00-06:00")
    run(eng, [cancel])
    assert len(sent) == 1


def test_quiet_first_run(db, cfg, channels, sent):
    cfg.quiet_first_run = True
    eng = Engine(db, cfg, channel_factory=channels)
    eng.process(parse_collection(collection()))
    assert sent == []
    uid = db.get_user(name="scott")["id"]
    assert all(r["action"] == "log" for r in db.query_feed(uid, limit=100))
    # Next poll is live.
    run(eng, [feature("new1", "Tornado Warning", office_vtec="KBOU", phen="TO", sig="W", etn="0099")])
    assert len(sent) == 1


def test_outbreak_cap_sends_summary(db, cfg, channels, sent):
    cfg.max_push_per_poll = 3
    eng = Engine(db, cfg, channel_factory=channels)
    feats = [feature(f"o{i}", "Tornado Warning", office_vtec="KOUN", phen="TO", sig="W", etn=f"{i:04d}",
                     sender="NWS Norman OK") for i in range(10)]
    res = run(eng, feats)
    assert len(sent) == 4  # 3 alerts + 1 summary
    assert "7 more alerts" in sent[-1].title
    assert res.logged >= 7


def test_dry_run_collects_without_sending(db, cfg, channels, sent):
    res = Engine(db, cfg, channel_factory=channels).process(parse_collection(collection()), dry_run=True)
    assert sent == [] and len(res.notifications) == 6


def test_event_timezone_lookup_is_cached(db, cfg, channels):
    calls = []

    def lookup(zone):
        calls.append(zone)
        return "America/Denver"

    eng = Engine(db, cfg, channel_factory=channels, tz_lookup=lookup)
    run(eng, [by_id("tor1"), by_id("sps1")])  # both start with COC005
    assert calls == ["COC005"]
    assert db.get_alert(parse_feature(by_id("tor1")).id).event_tz == "America/Denver"


def test_body_front_loads_threat_and_times(db, cfg, channels, sent):
    eng = Engine(db, cfg, channel_factory=channels)
    run(eng, [by_id("svr1")])
    n = sent[0]
    assert n.title.startswith("DESTRUCTIVE Severe Thunderstorm Warning")
    first = n.body.splitlines()[0]
    assert first.startswith('2.75" hail · 80 mph')
    assert "Z" in n.body  # Zulu time present


def test_rule_templates_override(db, cfg, channels, sent):
    uid = db.get_user(name="scott")["id"]
    db.replace_rules(uid, [{"name": "custom", "events": ["Tornado Warning"], "scope": {"nationwide": True},
                            "title_template": "{office} {event}{missing}", "body_template": "{area_short}"}])
    run(Engine(db, cfg, channel_factory=channels), [by_id("tor1")])
    assert sent[0].title == "BOU Tornado Warning"
    assert sent[0].body == "Arapahoe, Douglas CO"


def test_area_short():
    from stormify.templates import area_short
    assert area_short("Tarrant, TX") == "Tarrant TX"
    assert area_short("A, KS; B, KS; C, KS; D, KS; E, KS") == "A, B, C KS +2"
    assert area_short("Cook, IL; Lake, IN") == "Cook IL, Lake IN"


def test_invalid_rules_do_not_crash(db, cfg, channels, sent):
    uid = db.get_user(name="scott")["id"]
    with db.tx() as c:
        c.execute("UPDATE rules SET body_json='{\"bogus\": 1}' WHERE user_id=?", (uid,))
    res = Engine(db, cfg, channel_factory=channels).process(parse_collection(collection()))
    assert res.new_alerts > 0 and sent == []


def test_multi_user_independent(db, cfg, channels, sent):
    uid2 = db.create_user("friend", None, {"channels": {"ntfy": {"topic": "y"}}})
    db.replace_rules(uid2, [{"name": "TX only", "kinds": ["Warning"], "scope": {"states": ["TX"]}}])
    run(Engine(db, cfg, channel_factory=channels), copy.deepcopy(collection()["features"]))
    feed2 = db.query_feed(uid2, limit=100, action="push")
    assert [r["office"] for r in feed2] == ["FWD"]
