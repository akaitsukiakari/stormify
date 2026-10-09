"""Source-independent alert model.

Every source (NWS alerts and text products, NHC, SPC, SWPC) produces Alert objects,
and everything downstream (rules, storage, delivery, dashboard) only sees these.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Product "kinds" (tiers), derived from the event name.
# "Product" is set directly on NWS text products (AFD, HWO, ...), never derived from an event name.
KINDS = ("Emergency", "Warning", "Watch", "Advisory", "Statement", "Outlook", "Discussion", "Message",
         "Product", "Other")

SEVERITY_RANK = {"Unknown": 0, "Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}

# Tags that make an update "significant" when they newly appear. The NHC ones
# are watches/warnings in effect in a public advisory, and the major-hurricane class.
SIGNIFICANT_TAGS = {"emergency", "pds", "considerable", "destructive", "observed",
                    "hurricane-warning", "hurricane-watch", "ts-warning", "ts-watch",
                    "surge-warning", "surge-watch", "major-hurricane"}


def kind_for_event(event: str, tags: set[str] | None = None) -> str:
    if tags and "emergency" in tags:
        return "Emergency"
    e = (event or "").lower()
    for suffix, kind in (
        ("emergency", "Emergency"),
        ("warning", "Warning"),
        ("watch", "Watch"),
        ("advisory", "Advisory"),
        ("statement", "Statement"),
        ("outlook", "Outlook"),
        ("message", "Message"),
    ):
        if e.endswith(suffix):
            return kind
    return "Other"


@dataclass
class Alert:
    id: str
    source: str
    event: str
    headline: str = ""
    description: str = ""
    instruction: str = ""
    nws_headline: str = ""
    area_desc: str = ""
    office: str = ""            # 3-letter office id, e.g. "BOU"
    sender_name: str = ""       # e.g. "NWS Boulder CO"
    attn_offices: list[str] = field(default_factory=list)  # other offices it concerns (SPC MD "ATTN...WFO")
    message_type: str = "Alert"  # Alert | Update | Cancel
    vtec_action: str = ""       # NEW, CON, EXT, UPG, CAN, EXP, ...
    status: str = "Actual"      # Actual | Test | Exercise | ...
    severity: str = "Unknown"
    certainty: str = ""
    urgency: str = ""
    sent: str = ""              # ISO 8601 with the issuing office's UTC offset
    effective: str = ""
    onset: str = ""
    expires: str = ""
    ends: str = ""
    zones: list[str] = field(default_factory=list)   # UGC codes, e.g. COZ039, COC005
    same: list[str] = field(default_factory=list)    # SAME/FIPS codes
    states: list[str] = field(default_factory=list)
    vtec: list[str] = field(default_factory=list)
    thread_key: str = ""        # groups an alert with its updates/cancellations
    references: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    hail_in: float | None = None
    wind_mph: int | None = None
    kind: str = "Other"
    event_tz: str = ""          # IANA tz of the affected area, if known
    url: str = ""
    geometry: dict | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def tag_set(self) -> set[str]:
        return set(self.tags)

    @property
    def severity_rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 0)

    @property
    def is_cancel(self) -> bool:
        return self.message_type == "Cancel" or self.vtec_action in ("CAN", "EXP")

    @property
    def search_text(self) -> str:
        return "\n".join(
            x for x in (self.event, self.headline, self.nws_headline, self.description,
                        self.instruction, self.area_desc, self.sender_name) if x
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Alert":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
