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
  "kinds": ["Warning"],                              # Emergency/Warning/Watch/Advisory/Statement/...
  "tags_any": ["pds", "emergency"],                 # derived tags; alert needs at least one
  "scope": {"nationwide": true}                      # or {"offices": ["BOU"], "zones": [...],
                                                     #     "states": ["CO"], "same": ["008005"]}
  "filters": {
    "include_any": ["PDS", "particularly dangerous situation"],   # OR
    "include_all": [],                                            # AND
    "exclude_any": ["nickel"],
    "include_regex": [], "exclude_regex": [],
    "case_sensitive": false
  },
  "min_hail_in": 1.0,          # optional hail threshold (inches)
  "min_wind_mph": 70,          # optional wind threshold
  "action": "push",            # push | log
  "priority": 4,               # 1 (min) .. 5 (max), maps to ntfy priority
  "on_update": "significant",  # new_only | significant | any
  "on_cancel": false,
  "title_template": null, "body_template": null
}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from .models import Alert, kind_for_event

VALID_ACTIONS = {"push", "log"}
VALID_ON_UPDATE = {"new_only", "significant", "any"}


class RuleError(ValueError):
    pass


def _norm_office(o: str) -> str:
    o = o.strip().upper()
    return o[1:] if len(o) == 4 and o[0] in "KP" else o


@lru_cache(maxsize=1024)
def _compile(pattern: str, case_sensitive: bool) -> re.Pattern:
    return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)


@dataclass
class Rule:
    name: str
    enabled: bool = True
    events: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    tags_any: list[str] = field(default_factory=list)
    scope: dict = field(default_factory=lambda: {"nationwide": True})
    filters: dict = field(default_factory=dict)
    min_hail_in: float | None = None
    min_wind_mph: int | None = None
    action: str = "push"
    priority: int = 3
    on_update: str = "significant"
    on_cancel: bool = False
    title_template: str | None = None
    body_template: str | None = None
    id: int | None = None

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
        cs = bool(self.filters.get("case_sensitive", False))
        for key in ("include_regex", "exclude_regex"):
            for pat in self.filters.get(key, []):
                try:
                    _compile(pat, cs)
                except re.error as e:
                    raise RuleError(f"{self.name}: bad regex {pat!r}: {e}") from e

    # ---- matching ------------------------------------------------------
    def matches_product(self, a: Alert) -> bool:
        if self.events and a.event.lower() not in {e.lower() for e in self.events}:
            return False
        if self.kinds:
            # An emergency is still a warning: "Tornado Warning" tagged emergency matches both kinds.
            alert_kinds = {a.kind.lower(), kind_for_event(a.event).lower()}
            if not alert_kinds & {k.lower() for k in self.kinds}:
                return False
        if self.tags_any and not (a.tag_set & {t.lower() for t in self.tags_any}):
            return False
        return True

    def matches_scope(self, a: Alert) -> bool:
        s = self.scope or {}
        if s.get("nationwide"):
            return True
        offices = {_norm_office(o) for o in s.get("offices", [])}
        if offices and a.office.upper() in offices:
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


def load_rules(rule_dicts: list[dict]) -> list[Rule]:
    return [Rule.from_dict(d) for d in rule_dicts]


def evaluate(alert: Alert, rules: list[Rule]) -> Decision:
    push: list[Rule] = []
    matched: list[str] = []
    reasons: list[str] = []
    for r in rules:
        if not r.enabled or not r.matches_product(alert) or not r.matches_scope(alert):
            continue
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
