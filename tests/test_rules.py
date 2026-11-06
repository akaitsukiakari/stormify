import pytest
from fixtures import by_id

from stormify.cli import example_rules
from stormify.rules import Rule, RuleError, evaluate, load_rules
from stormify.sources.nws import parse_feature


def alert(short):
    return parse_feature(by_id(short))


def rule(**kw):
    kw.setdefault("name", "t")
    return Rule.from_dict(kw)


def test_example_rules_are_valid():
    assert len(load_rules(example_rules())) >= 5


def test_event_names_case_insensitive():
    assert rule(events=["tornado warning"]).matches_product(alert("tor1"))
    assert not rule(events=["Tornado Watch"]).matches_product(alert("tor1"))


def test_kinds_and_emergency_counts_as_warning():
    assert rule(kinds=["Warning"]).matches_product(alert("tor2"))
    assert rule(kinds=["Emergency"]).matches_product(alert("tor2"))
    assert not rule(kinds=["Watch"]).matches_product(alert("tor2"))


def test_tags_any():
    assert rule(tags_any=["emergency"]).matches_product(alert("tor2"))
    assert not rule(tags_any=["emergency"]).matches_product(alert("tor1"))


@pytest.mark.parametrize("scope,expected", [
    ({"nationwide": True}, True),
    ({"offices": ["BOU"]}, True),
    ({"offices": ["KBOU"]}, True),
    ({"offices": ["FWD"]}, False),
    ({"zones": ["coc005"]}, True),
    ({"states": ["CO"]}, True),
    ({"states": ["TX"]}, False),
    ({"same": ["8005"]}, True),
    ({}, False),
])
def test_scope(scope, expected):
    assert rule(scope=scope).matches_scope(alert("tor1")) is expected


def test_include_any_or():
    r = rule(filters={"include_any": ["tornado emergency", "nonsense"]})
    assert r.passes_filters(alert("tor2"))[0]
    assert not r.passes_filters(alert("tor1"))[0]


def test_include_all_and():
    r = rule(filters={"include_all": ["tornado emergency", "particularly dangerous"]})
    assert r.passes_filters(alert("tor2"))[0]
    r2 = rule(filters={"include_all": ["tornado emergency", "nonsense"]})
    ok, why = r2.passes_filters(alert("tor2"))
    assert not ok and "nonsense" in why


def test_exclude_any():
    r = rule(filters={"exclude_any": ["pea size"]})
    assert not r.passes_filters(alert("sps1"))[0]
    assert r.passes_filters(alert("sps2"))[0]


def test_case_sensitive_toggle():
    assert rule(filters={"include_any": ["PEA SIZE"]}).passes_filters(alert("sps1"))[0]
    assert not rule(filters={"include_any": ["PEA SIZE"], "case_sensitive": True}).passes_filters(alert("sps1"))[0]


def test_regex_include_and_exclude():
    assert rule(filters={"include_regex": [r"\bhalf\s+dollar\b"]}).passes_filters(alert("sps2"))[0]
    assert not rule(filters={"exclude_regex": [r"(pea|nickel)\s+size"]}).passes_filters(alert("sps1"))[0]


def test_hail_and_wind_thresholds():
    assert rule(min_hail_in=2).passes_filters(alert("svr1"))[0]
    assert not rule(min_hail_in=2).passes_filters(alert("svr2"))[0]
    assert rule(min_wind_mph=75).passes_filters(alert("svr1"))[0]
    assert not rule(min_wind_mph=75).passes_filters(alert("svr2"))[0]


def test_bad_regex_rejected():
    with pytest.raises(RuleError):
        rule(filters={"include_regex": ["(unclosed"]})


def test_unknown_key_rejected():
    with pytest.raises(RuleError):
        Rule.from_dict({"name": "x", "bogus": 1})


def test_bad_action_rejected():
    with pytest.raises(RuleError):
        rule(action="shout")


def test_filter_fail_means_log_not_push():
    rules = [rule(events=["Special Weather Statement"], scope={"offices": ["BOU"]},
                  filters={"exclude_any": ["pea size"]})]
    d = evaluate(alert("sps1"), rules)
    assert d.action == "log" and "pea size" in d.reason
    assert evaluate(alert("sps2"), rules).action == "push"


def test_log_only_rule():
    d = evaluate(alert("wwy1"), load_rules(example_rules()))
    assert d.action == "log" and d.matched


def test_priority_takes_highest():
    rules = [rule(name="a", priority=2), rule(name="b", priority=5)]
    d = evaluate(alert("tor1"), rules)
    assert d.priority == 5 and d.lead_rule.name == "b"


def test_scope_groups_follow_the_saved_group():
    from stormify.rules import clean_groups
    a = alert("tor1")  # BOU
    rules = load_rules([{"name": "home", "scope": {"groups": ["home"]}}], {"home": ["kbou", "PUB"]})
    assert rules[0].matches_scope(a)
    assert not load_rules([{"name": "home", "scope": {"groups": ["home"]}}], {"home": ["PUB"]})[0].matches_scope(a)
    assert not load_rules([{"name": "home", "scope": {"groups": ["gone"]}}], {})[0].matches_scope(a)
    assert clean_groups({" home ": ["bou", "KPUB", "bou"]}) == {"home": ["BOU", "PUB"]}
    with pytest.raises(RuleError):
        clean_groups({"x": "BOU"})
    with pytest.raises(RuleError):
        Rule.from_dict({"name": "bad", "scope": {"offices": "BOU"}})


def test_product_ids():
    from stormify.models import Alert

    def product(pil):
        return Alert(id=pil, source="nws-product", event="Climatological Report (Daily)",
                     office="BOU", kind="Product", params={"awipsId": pil})

    r = rule(events=["Climatological Report (Daily)"], product_ids=["cliden"], scope={"offices": ["BOU"]})
    assert r.matches_product(product("CLIDEN"))
    assert not r.matches_product(product("CLIGXY"))
    # Other product types on the same rule are untouched.
    assert rule(product_ids=["CLIDEN"]).matches_product(alert("tor1"))
    assert rule(product_ids=["CLIDEN"]).matches_product(product("AFDBOU"))
    assert rule(product_ids=["CLIDEN", "CLMDEN"]).matches_product(product("CLMDEN"))
    assert not rule(product_ids=["CLIDEN", "CLMDEN"]).matches_product(product("CLMGXY"))
    assert rule(product_ids=[]).matches_product(product("CLIGXY"))
    with pytest.raises(RuleError):
        rule(product_ids="CLIDEN")


def test_catch_all_only_gets_what_other_rules_leave():
    rules = load_rules([
        {"name": "BOU warnings", "kinds": ["Warning"], "scope": {"offices": ["BOU"]}},
        {"name": "Small hail SPS stays quiet", "events": ["Special Weather Statement"],
         "scope": {"offices": ["BOU"]}, "filters": {"exclude_any": ["nickel"]}},
        {"name": "Everything else from BOU", "scope": {"offices": ["BOU"]}, "action": "log", "fallback": True},
    ])
    d = evaluate(alert("tor1"), rules)
    assert d.action == "push" and d.matched == ["BOU warnings"]
    d = evaluate(alert("wwy1"), rules)
    assert d.action == "log" and d.matched == ["Everything else from BOU"]
    # Outside BOU: nothing covers it, the catch-all included.
    assert evaluate(alert("svr1"), rules).matched == []


def test_catch_all_leaves_alerts_a_filter_stopped():
    rules = load_rules([
        {"name": "BOU SPS", "events": ["Special Weather Statement"], "scope": {"offices": ["BOU"]},
         "filters": {"include_any": ["no such phrase"]}},
        {"name": "Rest of BOU, quietly", "scope": {"offices": ["BOU"]}, "priority": 1, "fallback": True},
    ])
    d = evaluate(alert("sps1"), rules)
    assert d.action == "log" and d.matched == [] and "no include_any" in d.reason
    # A turned-off rule doesn't count, so the catch-all takes it.
    rules[0].enabled = False
    d = evaluate(alert("sps1"), rules)
    assert d.action == "push" and d.matched == ["Rest of BOU, quietly"] and d.priority == 1


# ---- exclude_events and "Remove from my alerts" ------------------------------------
from stormify.rules import apply_removal, removal_options  # noqa: E402


def test_exclude_events():
    r = rule(scope={"offices": ["BOU"]}, exclude_events=["tornado warning"])
    assert not r.matches_product(alert("tor1"))
    assert r.matches_product(alert("wwy1"))
    with pytest.raises(RuleError):
        rule(exclude_events="Tornado Warning")


def test_removal_from_an_any_event_rule_adds_an_exclusion():
    rules = [{"name": "BOU all", "scope": {"offices": ["BOU", "PUB"]}},
             {"name": "BOU log", "scope": {"offices": ["BOU"]}, "action": "log"}]
    opts = removal_options(alert("tor1"), rules)
    assert opts == [{"index": 0, "name": "BOU all", "event": "Tornado Warning", "turns_off": False, "office": "BOU"}]
    new, change, still = apply_removal(rules, alert("tor1"), 0, "event")
    assert change == "excluded" and still == []
    assert new[0]["exclude_events"] == ["Tornado Warning"] and new[1] == rules[1]
    assert rules[0].get("exclude_events") is None  # the input isn't changed
    assert removal_options(alert("tor1"), new) == []
    assert removal_options(alert("wwy1"), new)  # the rest of the rule still works


def test_removal_drops_a_listed_event_or_turns_the_rule_off():
    rules = [{"name": "two", "events": ["Tornado Warning", "Winter Weather Advisory"], "scope": {"offices": ["BOU"]}},
             {"name": "one", "events": ["Tornado Warning"], "scope": {"nationwide": True}}]
    opts = removal_options(alert("tor1"), rules)
    assert [(o["name"], o["turns_off"], o["office"]) for o in opts] == [("two", False, None), ("one", True, None)]
    new, change, still = apply_removal(rules, alert("tor1"), 0, "event")
    assert change == "removed" and new[0]["events"] == ["Winter Weather Advisory"] and still == ["one"]
    new, change, still = apply_removal(new, alert("tor1"), 1, "event")
    assert change == "turned_off" and new[1]["enabled"] is False and still == []


def test_removal_by_office():
    rules = [{"name": "home", "scope": {"offices": ["KBOU", "PUB"]}}]
    new, change, still = apply_removal(rules, alert("tor1"), 0, "office")
    assert change == "office_removed" and new[0]["scope"]["offices"] == ["PUB"] and still == []
    with pytest.raises(RuleError):
        apply_removal(new, alert("tor1"), 0, "office")
    with pytest.raises(RuleError):
        apply_removal(rules, alert("tor1"), 5, "event")
