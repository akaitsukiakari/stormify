"""Rules engine.

A rule says: for these products (event names / kinds / tags), in this scope
(nationwide, or a set of offices, zones, states, counties), apply these text
filters, then push or just log.

Every alert is always archived. Rules only decide whether it also pushes.
An alert that matches a push rule's products and scope but fails its text
filters is logged, not pushed (e.g. "special weather statements from BOU,
but small-hail ones only go to the dashboard").

Rule JSON shape (all keys optional except name):

{
  "name": "Tornado anything, nationwide",
  "enabled": true,
  "events": ["Tornado Warning", "Tornado Watch"],   # exact names, case-insensitive; empty = any
  "exclude_events": ["Special Weather Statement"],  # never these, even if the rest matches
  "kinds": ["Warning"],                              # Emergency/Warning/Watch/Advisory/Statement/...
  "tags_any": ["pds", "emergency"],                 # derived tags; alert needs at least one
  "product_ids": ["CLIDEN"],                         # AWIPS IDs; of those types (CLI...), only these
  "scope": {"nationwide": true}                      # or {"offices": ["BOU"], "zones": [...],
                                                     #     "states": ["CO"], "same": ["008005"],
                                                     #     "groups": ["home"]}  (saved office groups)
  "filters": {
    "include_any": ["PDS", "particularly dangerous situation"],   # OR
    "include_all": [],                                            # AND
    "exclude_any": ["nickel"],
    "include_regex": [], "exclude_regex": [],
    "case_sensitive": false
  },
  "min_hail_in": 1.0,          # optional hail threshold (inches)
  "min_wind_mph": 70,          # optional wind threshold
  "min_magnitude": 4.5,        # earthquakes only (see below)
  "action": "push",            # push | log
  "priority": 4,               # 1 (min) .. 5 (max), maps to ntfy priority
  "on_update": "significant",  # new_only | significant | any
  "on_cancel": false,
  "fallback": false,           # catch-all: only for alerts no other rule covers
  "title_template": null, "body_template": null
}

A catch-all rule ("fallback": true) only applies to an alert when no regular
rule's products and scope fit it, e.g. "everything else from BOU goes to the
dashboard" or "everything else from BOU pushes quietly". An alert a regular
rule covers but its text filters stop still counts as covered, so a catch-all
never pushes something a filter was meant to keep off the phone.

Earthquakes (USGS, kind "Earthquake") are kept apart from weather: a quake only
matches a rule that asks for quakes, by naming the Earthquake event or kind,
setting min_magnitude, or giving a "near" scope. And a rule with min_magnitude
or only a "near" scope is an earthquake rule, so weather never matches it. A
"near" scope is a list of places, matched by distance from the quake:

  "scope": {"near": [{"name": "Home", "lat": 39.74, "lon": -104.99, "miles": 150}]}
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import lru_cache

from .models import Alert, kind_for_event
from .sources.usgs import miles_between

VALID_ACTIONS = {"push", "log"}
VALID_ON_UPDATE = {"new_only", "significant", "any"}


class RuleError(ValueError):
    pass


def _norm_office(o: str) -> str:
    o = o.strip().upper()
    return o[1:] if len(o) == 4 and o[0] in "KP" else o


def product_id(a: Alert) -> str:
    """The alert's AWIPS product ID (PIL), like CLIDEN or AFDBOU; "" if it has none."""
    params = a.params or {}
    pid = params.get("awipsId") or params.get("AWIPSidentifier") or ""
    if isinstance(pid, list):
        pid = pid[0] if pid else ""
    return str(pid).strip().upper()


@lru_cache(maxsize=1024)
def _compile(pattern: str, case_sensitive: bool) -> re.Pattern:
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


@dataclass
class Rule:
    name: str
    enabled: bool = True
    events: list[str] = field(default_factory=list)
    exclude_events: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    tags_any: list[str] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)
    scope: dict = field(default_factory=lambda: {"nationwide": True})
    filters: dict = field(default_factory=dict)
    min_hail_in: float | None = None
    min_wind_mph: int | None = None
    min_magnitude: float | None = None
    action: str = "push"
    priority: int = 3
    on_update: str = "significant"
    on_cancel: bool = False
    fallback: bool = False
    title_template: str | None = None
    body_template: str | None = None
    id: int | None = None

    # Offices from the saved groups the scope names; filled in by load_rules, never saved.
    group_offices = frozenset()
    # Catch-all only: the regular rules it yields to, when testing it on its own. Never saved.
    others: tuple = ()

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise RuleError(f"rule {d.get('name')!r}: unknown keys {sorted(unknown)}")
        r = cls(**d)
        r.validate()
        return r

    def validate(self) -> None:
        if not self.name:
            raise RuleError("rule needs a name")
        if self.action not in VALID_ACTIONS:
            raise RuleError(f"{self.name}: action must be one of {sorted(VALID_ACTIONS)}")
        if self.on_update not in VALID_ON_UPDATE:
            raise RuleError(f"{self.name}: on_update must be one of {sorted(VALID_ON_UPDATE)}")
        if not 1 <= int(self.priority) <= 5:
            raise RuleError(f"{self.name}: priority must be 1..5")
        if not isinstance(self.scope, dict):
            raise RuleError(f"{self.name}: scope must be an object")
        for key in ("product_ids", "exclude_events"):
            if not isinstance(getattr(self, key), list):
                raise RuleError(f"{self.name}: {key} must be a list")
        for key in ("offices", "zones", "states", "same", "groups", "near"):
            if not isinstance(self.scope.get(key, []), list):
                raise RuleError(f"{self.name}: scope {key} must be a list")
        for p in self.scope.get("near", []):
            try:
                lat, lon, miles = float(p["lat"]), float(p["lon"]), float(p["miles"])
            except (TypeError, KeyError, ValueError) as e:
                raise RuleError(f"{self.name}: each near place needs lat, lon and miles") from e
            if not (-90 <= lat <= 90 and -180 <= lon <= 180 and miles > 0):
                raise RuleError(f"{self.name}: near place {p.get('name') or ''} is out of range")
        if self.min_magnitude is not None:
            try:
                self.min_magnitude = float(self.min_magnitude)
            except (TypeError, ValueError) as e:
                raise RuleError(f"{self.name}: min_magnitude must be a number") from e
        cs = bool(self.filters.get("case_sensitive", False))
        for key in ("include_regex", "exclude_regex"):
            for pat in self.filters.get(key, []):
                try:
                    _compile(pat, cs)
                except re.error as e:
                    raise RuleError(f"{self.name}: bad regex {pat!r}: {e}") from e

    # ---- matching ------------------------------------------------------
    @property
    def wants_quakes(self) -> bool:
        """Asks for earthquakes: names the event or kind, sets a magnitude, or has a near scope."""
        return ("earthquake" in {e.lower() for e in self.events} or "earthquake" in {k.lower() for k in self.kinds}
                or self.min_magnitude is not None or bool((self.scope or {}).get("near")))

    @property
    def quakes_only(self) -> bool:
        """An earthquake rule: a magnitude threshold, or a near scope and no other places."""
        s = self.scope or {}
        other = s.get("nationwide") or any(s.get(k) for k in ("offices", "zones", "states", "same", "groups"))
        return self.min_magnitude is not None or (bool(s.get("near")) and not other)

    def matches_product(self, a: Alert) -> bool:
        if a.kind == "Earthquake":
            if not self.wants_quakes:
                return False
        elif self.quakes_only:
            return False
        if self.events and a.event.lower() not in {e.lower() for e in self.events}:
            return False
        if self.exclude_events and a.event.lower() in {e.lower() for e in self.exclude_events}:
            return False
        if self.kinds:
            # An emergency is still a warning: "Tornado Warning" tagged emergency matches both kinds.
            alert_kinds = {a.kind.lower(), kind_for_event(a.event).lower()}
            if not alert_kinds & {k.lower() for k in self.kinds}:
                return False
        if self.tags_any and not (a.tag_set & {t.lower() for t in self.tags_any}):
            return False
        if self.product_ids:
            # Narrows only the product types it names: CLIDEN keeps CLIDEN out of all CLI reports,
            # and everything else the rule matches (warnings, AFDs, ...) is left alone.
            wanted = {str(p).strip().upper() for p in self.product_ids if str(p).strip()}
            pid = product_id(a)
            if len(pid) >= 4 and pid[:3] in {w[:3] for w in wanted} and pid not in wanted:
                return False
        return True

    def matches_scope(self, a: Alert) -> bool:
        s = self.scope or {}
        if s.get("nationwide"):
            return True
        offices = {_norm_office(o) for o in s.get("offices", [])} | self.group_offices
        if offices and (a.office.upper() in offices or offices.intersection(a.attn_offices)):
            return True
        zones = {z.upper() for z in s.get("zones", [])}
        if zones and zones.intersection(a.zones):
            return True
        states = {st.upper() for st in s.get("states", [])}
        if states and states.intersection(a.states):
            return True
        same = {str(x).zfill(6) for x in s.get("same", [])}
        if same and same.intersection(a.same):
            return True
        near = s.get("near") or []
        if near and a.kind == "Earthquake":
            lat, lon = (a.params or {}).get("lat"), (a.params or {}).get("lon")
            if lat is not None and lon is not None and any(
                    miles_between(float(p["lat"]), float(p["lon"]), lat, lon) <= float(p["miles"]) for p in near):
                return True
        return False

    def passes_filters(self, a: Alert) -> tuple[bool, str]:
        f = self.filters or {}
        cs = bool(f.get("case_sensitive", False))
        text = a.search_text
        hay = text if cs else text.casefold()

        def has(term: str) -> bool:
            return (term if cs else term.casefold()) in hay

        inc_any = f.get("include_any") or []
        if inc_any and not any(has(t) for t in inc_any):
            return False, "no include_any term matched"
        inc_all = f.get("include_all") or []
        missing = [t for t in inc_all if not has(t)]
        if missing:
            return False, f"missing include_all term {missing[0]!r}"
        for t in f.get("exclude_any") or []:
            if has(t):
                return False, f"excluded by {t!r}"
        inc_re = f.get("include_regex") or []
        if inc_re and not any(_compile(p, cs).search(text) for p in inc_re):
            return False, "no include_regex matched"
        for p in f.get("exclude_regex") or []:
            if _compile(p, cs).search(text):
                return False, f"excluded by regex {p!r}"
        if self.min_hail_in is not None and (a.hail_in or 0) < float(self.min_hail_in):
            return False, f"hail below {self.min_hail_in} in"
        if self.min_wind_mph is not None and (a.wind_mph or 0) < int(self.min_wind_mph):
            return False, f"wind below {self.min_wind_mph} mph"
        if self.min_magnitude is not None:
            mag = (a.params or {}).get("mag")
            if mag is None or float(mag) < float(self.min_magnitude):
                return False, f"magnitude below {self.min_magnitude:g}"
        return True, ""


@dataclass
class Decision:
    action: str                       # push | log
    reason: str = ""
    push_rules: list[Rule] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)

    @property
    def priority(self) -> int:
        return max((r.priority for r in self.push_rules), default=3)

    @property
    def lead_rule(self) -> Rule | None:
        """The highest-priority push rule; its templates and update policy win."""
        return max(self.push_rules, key=lambda r: r.priority, default=None)


def load_rules(rule_dicts: list[dict], groups: dict[str, list[str]] | None = None) -> list[Rule]:
    """Rules from their JSON. `groups` is the user's saved office groups ({"home": ["BOU", ...]})."""
    rules = [Rule.from_dict(d) for d in rule_dicts]
    for r in rules:
        names = (r.scope or {}).get("groups") or []
        if names and groups:
            r.group_offices = frozenset(_norm_office(o) for n in names for o in groups.get(n, []))
    return rules


def clean_groups(raw) -> dict[str, list[str]]:
    """Validate saved office groups from the dashboard: {name: [office codes]}."""
    if not isinstance(raw, dict):
        raise RuleError("office groups must be an object")
    out: dict[str, list[str]] = {}
    for name, offices in raw.items():
        name = str(name).strip()
        if not name or not isinstance(offices, list):
            raise RuleError(f"office group {name!r} needs a name and a list of offices")
        out[name] = sorted({_norm_office(str(o)) for o in offices if str(o).strip()})
    return out


def covering(alert: Alert, rules: list[Rule]) -> list[Rule]:
    """The turned-on rules whose products and scope fit the alert (filters not checked yet).

    Catch-all rules are only included when no regular rule fits."""
    fits = [r for r in rules if r.enabled and r.matches_product(alert) and r.matches_scope(alert)]
    regular = [r for r in fits if not r.fallback]
    return regular or fits


def evaluate(alert: Alert, rules: list[Rule]) -> Decision:
    push: list[Rule] = []
    matched: list[str] = []
    reasons: list[str] = []
    for r in covering(alert, rules):
        ok, why = r.passes_filters(alert)
        if not ok:
            reasons.append(f"{r.name}: {why}")
            continue
        matched.append(r.name)
        if r.action == "push":
            push.append(r)
    if push:
        return Decision("push", "matched " + ", ".join(x.name for x in push), push, matched)
    if matched:
        return Decision("log", "log-only rule: " + ", ".join(matched), [], matched)
    return Decision("log", "; ".join(reasons) or "no rule matched", [], matched)


# ---- "Remove from my alerts" -------------------------------------------------
# From an opened alert, take it out of the rule that pushes it: drop its event type from the rule
# (or, for a rule that lists several offices, drop its office). The rest of the rule keeps working.

def pushing_rules(alert: Alert, rules: list[Rule]) -> list[Rule]:
    """The turned-on push rules that would push this alert now (scope, products and filters)."""
    return [r for r in covering(alert, rules) if r.action == "push" and r.passes_filters(alert)[0]]


def _scope_entries(scope: dict) -> int:
    return sum(len(scope.get(k) or []) for k in ("offices", "groups", "zones", "states", "same"))


def removal_options(alert: Alert, rule_dicts: list[dict], groups: dict | None = None) -> list[dict]:
    """For each rule that pushes the alert: what removing it would do.

    `index` is the rule's place in rule_dicts (the user's full list, in order), so the dashboard can
    name exactly which one to change even when two rules share a name."""
    rules = load_rules([{k: v for k, v in d.items() if k != "id"} for d in rule_dicts], groups)
    out = []
    for r in pushing_rules(alert, rules):
        i = rules.index(r)
        ev = alert.event.lower()
        rest = [e for e in r.events if e.lower() != ev]
        opts = {"index": i, "name": r.name, "event": alert.event,
                # The event is all the rule covers, so taking it out turns the rule off.
                "turns_off": bool(r.events) and not rest, "office": None}
        s = r.scope or {}
        office = _norm_office(alert.office or "")
        offices = {_norm_office(o) for o in s.get("offices") or []}
        if office and not s.get("nationwide") and office in offices and _scope_entries(s) > 1:
            opts["office"] = office
        out.append(opts)
    return out


def apply_removal(rule_dicts: list[dict], alert: Alert, index: int, how: str,
                  groups: dict | None = None) -> tuple[list[dict], str, list[str]]:
    """Take the alert out of rule `index`. Returns (new rule list, what changed, rules still pushing it).

    how: "event" drops the alert's event from the rule (or adds it to exclude_events when the rule
    matches any event); "office" drops the alert's office from the rule's offices."""
    if not 0 <= index < len(rule_dicts):
        raise RuleError("That rule isn't there any more. Reload and try again.")
    new = [{k: v for k, v in d.items() if k != "id"} for d in rule_dicts]
    d = copy.deepcopy(new[index])
    ev = alert.event
    if how == "event":
        evs = list(d.get("events") or [])
        if evs:
            rest = [e for e in evs if e.lower() != ev.lower()]
            if rest:
                d["events"], change = rest, "removed"
            else:
                d["enabled"], change = False, "turned_off"
        else:
            ex = list(d.get("exclude_events") or [])
            if ev.lower() not in {e.lower() for e in ex}:
                ex.append(ev)
            d["exclude_events"], change = ex, "excluded"
    elif how == "office":
        scope = dict(d.get("scope") or {})
        office = _norm_office(alert.office or "")
        rest = [o for o in scope.get("offices") or [] if _norm_office(o) != office]
        if not office or len(rest) == len(scope.get("offices") or []):
            raise RuleError(f"{d.get('name')} doesn't list {alert.office}.")
        scope["offices"] = rest
        d["scope"], change = scope, "office_removed"
        if not _scope_entries(scope):
            d["enabled"], change = False, "turned_off"
    else:
        raise RuleError("how must be event or office")
    new[index] = d
    still = [r.name for r in pushing_rules(alert, load_rules(new, groups))]
    return new, change, still
