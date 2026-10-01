"""Realistic NWS /alerts/active features for tests (shape matches api.weather.gov GeoJSON)."""

from __future__ import annotations

import copy

POLY_BOU = {"type": "Polygon", "coordinates": [[[-104.9, 39.6], [-104.6, 39.6], [-104.6, 39.8], [-104.9, 39.8], [-104.9, 39.6]]]}
POLY_FWD = {"type": "Polygon", "coordinates": [[[-97.5, 32.7], [-97.2, 32.7], [-97.2, 32.9], [-97.5, 32.9], [-97.5, 32.7]]]}
POLY_OUN = {"type": "Polygon", "coordinates": [[[-98.0, 35.3], [-97.6, 35.3], [-97.6, 35.6], [-98.0, 35.6], [-98.0, 35.3]]]}


def feature(id_, event, *, sender="NWS Boulder CO", office_vtec=None, phen=None, sig=None, etn="0001",
            action="NEW", message_type="Alert", severity="Severe", sent="2026-06-01T16:42:00-06:00",
            expires="2026-06-01T17:15:00-06:00", area="Arapahoe, CO; Douglas, CO", ugc=("COC005", "COC035"),
            same=("008005", "008035"), description="", headline=None, instruction="", params=None,
            geometry=None, references=(), status="Actual", awips=None):
    p = dict(params or {})
    if office_vtec:
        p["VTEC"] = [f"/O.{action}.{office_vtec}.{phen}.{sig}.{etn}.260601T2242Z-260601T2315Z/"]
    if awips:
        p["AWIPSidentifier"] = [awips]
    full_id = f"urn:oid:2.49.0.1.840.0.{id_}"
    return {
        "id": f"https://api.weather.gov/alerts/{full_id}",
        "type": "Feature",
        "geometry": copy.deepcopy(geometry),
        "properties": {
            "@id": f"https://api.weather.gov/alerts/{full_id}",
            "id": full_id,
            "areaDesc": area,
            "geocode": {"SAME": list(same), "UGC": list(ugc)},
            "references": [{"identifier": f"urn:oid:2.49.0.1.840.0.{r}", "sender": "w-nws.webmaster@noaa.gov",
                            "sent": sent} for r in references],
            "sent": sent, "effective": sent, "onset": sent, "expires": expires, "ends": expires,
            "status": status, "messageType": message_type, "category": "Met",
            "severity": severity, "certainty": "Observed", "urgency": "Immediate",
            "event": event, "sender": "w-nws.webmaster@noaa.gov", "senderName": sender,
            "headline": headline or f"{event} issued June 1 at 4:42PM MDT by {sender}",
            "description": description, "instruction": instruction, "response": "Shelter",
            "parameters": p,
        },
    }


def collection() -> dict:
    feats = [
        feature("tor1", "Tornado Warning", office_vtec="KBOU", phen="TO", sig="W", etn="0012",
                description="At 442 PM MDT, a severe thunderstorm capable of producing a tornado was located near Parker.",
                params={"tornadoDetection": ["RADAR INDICATED"], "maxHailSize": ["1.00"],
                        "NWSheadline": ["TORNADO WARNING FOR ARAPAHOE AND DOUGLAS COUNTIES"]},
                geometry=POLY_BOU),
        feature("tor1u", "Tornado Warning", office_vtec="KBOU", phen="TO", sig="W", etn="0012", action="CON",
                message_type="Update", sent="2026-06-01T16:55:00-06:00", references=("tor1",),
                description="At 455 PM MDT, a confirmed tornado was located near Parker.",
                params={"tornadoDetection": ["OBSERVED"], "maxHailSize": ["1.50"]}, geometry=POLY_BOU),
        feature("tor2", "Tornado Warning", sender="NWS Fort Worth TX", office_vtec="KFWD", phen="TO", sig="W",
                etn="0044", sent="2026-06-01T17:42:00-05:00", expires="2026-06-01T18:15:00-05:00",
                area="Tarrant, TX", ugc=("TXC439",), same=("048439",), severity="Extreme",
                description="...TORNADO EMERGENCY FOR FORT WORTH...\nThis is a PARTICULARLY DANGEROUS SITUATION. TAKE COVER NOW!",
                params={"tornadoDetection": ["OBSERVED"], "tornadoDamageThreat": ["CATASTROPHIC"]},
                geometry=POLY_FWD),
        feature("svr1", "Severe Thunderstorm Warning", sender="NWS Norman OK", office_vtec="KOUN", phen="SV",
                sig="W", etn="0201", sent="2026-06-01T17:30:00-05:00", area="Oklahoma, OK", ugc=("OKC109",),
                same=("040109",),
                description="Destructive winds and baseball size hail expected.",
                params={"thunderstormDamageThreat": ["DESTRUCTIVE"], "maxHailSize": ["2.75"],
                        "maxWindGust": ["80 MPH"]}, geometry=POLY_OUN),
        feature("svr2", "Severe Thunderstorm Warning", sender="NWS Dodge City KS", office_vtec="KDDC", phen="SV",
                sig="W", etn="0090", sent="2026-06-01T17:10:00-05:00", area="Ford, KS", ugc=("KSC057",),
                same=("020057",), description="Quarter size hail and 60 mph winds.",
                params={"maxHailSize": ["1.00"], "maxWindGust": ["60 MPH"]}),
        feature("toa1", "Tornado Watch", sender="NWS Wichita KS", office_vtec="KICT", phen="TO", sig="A",
                etn="0412", severity="Severe", sent="2026-06-01T14:00:00-05:00",
                expires="2026-06-01T22:00:00-05:00", area="Sedgwick, KS; Butler, KS", ugc=("KSC173", "KSC015"),
                same=("020173", "020015"), description="Tornado Watch 412 remains in effect."),
        feature("sps1", "Special Weather Statement", awips="SPSBOU", severity="Moderate",
                description="Pea size hail and gusty winds to 40 mph are possible with this storm."),
        feature("sps2", "Special Weather Statement", awips="SPSBOU", severity="Moderate",
                sent="2026-06-01T16:50:00-06:00",
                description="Half dollar size hail is possible with this strong thunderstorm."),
        feature("wwy1", "Winter Weather Advisory", office_vtec="KBOU", phen="WW", sig="Y", etn="0003",
                severity="Minor", description="Snow expected above 10000 feet."),
        feature("flw1", "Flood Warning", sender="NWS Chicago IL", office_vtec="KLOT", phen="FL", sig="W",
                etn="0101", severity="Moderate", area="Cook, IL", ugc=("ILC031",), same=("017031",),
                description="The Des Plaines River near Riverside is expected to rise above flood stage."),
        feature("test1", "Tornado Warning", office_vtec="KBOU", phen="TO", sig="W", etn="9999", status="Test",
                description="THIS IS A TEST MESSAGE."),
    ]
    return {"type": "FeatureCollection", "features": feats}


def by_id(short: str) -> dict:
    for f in collection()["features"]:
        if f["properties"]["id"].endswith("." + short):
            return f
    raise KeyError(short)
