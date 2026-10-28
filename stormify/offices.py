"""Where each NWS forecast office sits, for telling which offices an SPC outlook covers.

Coordinates are the office's home city or site (lat, lon), to a tenth of a degree or so. That's
plenty for outlook areas, which span hundreds of miles. Lower-48 offices only: SPC outlooks
don't reach Alaska, Hawaii or the territories.
"""

from __future__ import annotations

OFFICE_POINTS: dict[str, tuple[float, float]] = {
    # Western Region
    "BOI": (43.57, -116.21), "BYZ": (45.75, -108.57), "EKA": (40.81, -124.16), "FGZ": (35.23, -111.82),
    "GGW": (48.21, -106.63), "HNX": (36.31, -119.63), "LKN": (40.86, -115.74), "LOX": (34.20, -119.18),
    "MFR": (42.38, -122.88), "MSO": (46.92, -114.09), "MTR": (36.59, -121.86), "OTX": (47.68, -117.63),
    "PDT": (45.69, -118.85), "PIH": (42.91, -112.60), "PQR": (45.56, -122.54), "PSR": (33.43, -112.02),
    "REV": (39.57, -119.80), "SEW": (47.69, -122.26), "SGX": (32.92, -117.06), "SLC": (40.77, -111.95),
    "STO": (38.61, -121.38), "TFX": (47.46, -111.38), "TWC": (32.23, -110.96), "VEF": (36.05, -115.18),
    # Central Region
    "ABR": (45.46, -98.41), "APX": (44.91, -84.72), "ARX": (43.82, -91.19), "BIS": (46.77, -100.76),
    "BOU": (39.99, -105.26), "CYS": (41.15, -104.81), "DDC": (37.76, -99.97), "DLH": (46.84, -92.21),
    "DMX": (41.73, -93.72), "DTX": (42.70, -83.47), "DVN": (41.61, -90.58), "EAX": (38.81, -94.26),
    "FGF": (47.92, -97.09), "FSD": (43.59, -96.73), "GID": (40.65, -98.38), "GJT": (39.12, -108.53),
    "GLD": (39.37, -101.70), "GRB": (44.50, -88.11), "GRR": (42.89, -85.54), "ICT": (37.65, -97.43),
    "ILX": (40.15, -89.34), "IND": (39.71, -86.28), "IWX": (41.36, -85.70), "JKL": (37.59, -83.32),
    "LBF": (41.13, -100.68), "LMK": (38.11, -85.65), "LOT": (41.60, -88.08), "LSX": (38.70, -90.68),
    "MKX": (42.97, -88.55), "MPX": (44.85, -93.57), "MQT": (46.53, -87.55), "OAX": (41.32, -96.37),
    "PAH": (37.07, -88.77), "PUB": (38.28, -104.52), "RIW": (43.06, -108.48), "SGF": (37.24, -93.40),
    "TOP": (39.07, -95.63), "UNR": (44.07, -103.21),
    # Eastern Region
    "AKQ": (36.98, -77.01), "ALY": (42.70, -73.83), "BGM": (42.21, -75.98), "BOX": (41.96, -71.14),
    "BTV": (44.47, -73.15), "BUF": (42.94, -78.72), "CAE": (33.94, -81.12), "CAR": (46.87, -68.02),
    "CHS": (32.89, -80.03), "CLE": (41.41, -81.86), "CTP": (40.79, -77.86), "GSP": (34.88, -82.22),
    "GYX": (43.89, -70.26), "ILM": (34.28, -77.91), "ILN": (39.42, -83.82), "LWX": (38.98, -77.49),
    "MHX": (34.78, -76.88), "OKX": (40.87, -72.86), "PBZ": (40.53, -80.22), "PHI": (39.94, -74.80),
    "RAH": (35.77, -78.68), "RLX": (38.31, -81.72), "RNK": (37.20, -80.41),
    # Southern Region
    "ABQ": (35.04, -106.62), "AMA": (35.23, -101.71), "BMX": (33.18, -86.78), "BRO": (25.92, -97.42),
    "CRP": (27.78, -97.51), "EPZ": (31.87, -106.70), "EWX": (29.70, -98.03), "FFC": (33.36, -84.57),
    "FWD": (32.83, -97.30), "HGX": (29.47, -95.08), "HUN": (34.72, -86.57), "JAN": (32.32, -90.08),
    "JAX": (30.49, -81.70), "KEY": (24.55, -81.79), "LCH": (30.13, -93.22), "LIX": (30.34, -89.83),
    "LUB": (33.53, -101.87), "LZK": (34.83, -92.26), "MAF": (31.94, -102.19), "MEG": (35.13, -89.80),
    "MFL": (25.75, -80.38), "MLB": (28.11, -80.65), "MOB": (30.68, -88.24), "MRX": (36.17, -83.40),
    "OHX": (36.25, -86.56), "OUN": (35.18, -97.44), "SHV": (32.45, -93.84), "SJT": (31.37, -100.49),
    "TAE": (30.45, -84.30), "TBW": (27.71, -82.40), "TSA": (36.15, -95.86),
}


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(lon: float, lat: float, rings: list) -> bool:
    if not rings or not _in_ring(lon, lat, rings[0]):
        return False
    return not any(_in_ring(lon, lat, hole) for hole in rings[1:])


def _polygons(geom: dict | None):
    if not geom:
        return
    t = geom.get("type")
    if t == "Polygon":
        yield geom.get("coordinates") or []
    elif t == "MultiPolygon":
        yield from geom.get("coordinates") or []
    elif t == "GeometryCollection":
        for g in geom.get("geometries") or []:
            yield from _polygons(g)


def offices_covered(geom: dict | None) -> list[str]:
    """Offices whose location falls inside any polygon of the geometry, sorted."""
    polys = list(_polygons(geom))
    if not polys:
        return []
    out = []
    for code, (lat, lon) in OFFICE_POINTS.items():
        if any(_in_polygon(lon, lat, rings) for rings in polys):
            out.append(code)
    return sorted(out)
