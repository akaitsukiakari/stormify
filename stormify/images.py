"""Small map images for notifications, Twitter-style: the alert's shape over a basemap.

The engine attaches a link (public_url/img/<token>.png) to a push; ntfy hands the
link to the phone, which fetches it from the dashboard. The image is drawn the
first time it's asked for and cached on disk, so a busy poll never waits on it.

Needs Pillow (`pip install stormify[images]`). Without it, pushes just go out
without a picture.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import time
from pathlib import Path

import requests

from .models import Alert

log = logging.getLogger(__name__)

try:  # optional dependency
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - exercised on installs without Pillow
    Image = ImageDraw = ImageFont = None

WIDTH, HEIGHT = 640, 400
HEADER = 30
TILE = 256
TILE_MAX_AGE = 30 * 86400
OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
CARTO_URL = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png?key={key}"

# Same palette as the dashboard.
EVENT_COLORS = {
    "Tornado Warning": "#ff1f1f", "Tornado Watch": "#ffd400", "Severe Thunderstorm Warning": "#ff9f1a",
    "Severe Thunderstorm Watch": "#db7093", "Flash Flood Warning": "#1fbf4a", "Flash Flood Emergency": "#8b0000",
    "Flood Warning": "#00b050", "Special Weather Statement": "#ffe4b5", "Extreme Wind Warning": "#ff8c00",
    "Hurricane Warning": "#dc143c", "Tropical Storm Warning": "#b22222", "Winter Storm Warning": "#ff69b4",
    "Blizzard Warning": "#ff4500", "Red Flag Warning": "#ff1493", "Tsunami Warning": "#fd6347",
    "Hurricane Watch": "#ff00ff", "Tropical Storm Watch": "#f08080", "Storm Surge Warning": "#b524f7",
    "Storm Surge Watch": "#db7ff7", "Tropical Cyclone Public Advisory": "#ff4fa3",
    "Tropical Cyclone Update": "#ff4fa3", "Mesoscale Discussion": "#4fc3f7",
}
KIND_COLORS = {"Emergency": "#ff2e63", "Warning": "#ff6b3d", "Watch": "#ffd23f", "Advisory": "#5eb3ff",
               "Statement": "#b4a7ff", "Outlook": "#7fd4c1", "Discussion": "#4fc3f7"}
RISK_COLORS = {"TSTM": "#c1e9c1", "MRGL": "#66a366", "SLGT": "#ffe066", "ENH": "#ffa366", "MDT": "#e06666",
               "HIGH": "#ee99ee"}


def available() -> bool:
    return Image is not None


def color_for(a: Alert) -> str:
    if "emergency" in a.tag_set:
        return "#ff2e63"
    if a.source == "spc" and a.kind == "Outlook" and a.params.get("risk") in RISK_COLORS:
        return RISK_COLORS[a.params["risk"]]
    return EVENT_COLORS.get(a.event) or KIND_COLORS.get(a.kind) or "#8b94a5"


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ---- web mercator ----------------------------------------------------------------

def world_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    lat = max(-85.0, min(85.0, lat))
    n = TILE * 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def _points(geom: dict | None):
    """Every [lon, lat] in a GeoJSON geometry."""
    if not geom:
        return
    t = geom.get("type")
    if t == "GeometryCollection":
        for g in geom.get("geometries") or []:
            yield from _points(g)
        return
    c = geom.get("coordinates")
    if t == "Point":
        yield c
    elif t in ("LineString", "MultiPoint"):
        yield from c
    elif t in ("Polygon", "MultiLineString"):
        for ring in c:
            yield from ring
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly:
                yield from ring


def pick_view(geom: dict, width: int = WIDTH, height: int = HEIGHT - HEADER, pad: int = 40,
              zmin: int = 3, zmax: int = 10) -> tuple[int, float, float]:
    """(zoom, center x, center y in world pixels) that fits the shape with some padding."""
    pts = list(_points(geom))
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    lo_lon, hi_lon, lo_lat, hi_lat = min(lons), max(lons), min(lats), max(lats)
    for z in range(zmax, zmin - 1, -1):
        x0, y1 = world_px(lo_lon, lo_lat, z)
        x1, y0 = world_px(hi_lon, hi_lat, z)
        if x1 - x0 <= width - 2 * pad and y1 - y0 <= height - 2 * pad:
            break
    x0, y1 = world_px(lo_lon, lo_lat, z)
    x1, y0 = world_px(hi_lon, hi_lat, z)
    return z, (x0 + x1) / 2, (y0 + y1) / 2


class Renderer:
    def __init__(self, cache_dir: str, user_agent: str, map_key: str = "",
                 session: requests.Session | None = None, timeout: float = 10.0):
        self.cache = Path(cache_dir)
        self.map_key = map_key
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.timeout = timeout

    # ---- tiles ------------------------------------------------------------------
    def _tile(self, z: int, x: int, y: int):
        n = 2 ** z
        if not 0 <= y < n:
            return None
        x %= n
        style = "carto-dark" if self.map_key else "osm"
        path = self.cache / "tiles" / style / str(z) / str(x) / f"{y}.png"
        if path.exists() and time.time() - path.stat().st_mtime < TILE_MAX_AGE:
            try:
                return Image.open(path).convert("RGB")
            except OSError:
                pass
        url = (CARTO_URL.format(z=z, x=x, y=y, key=self.map_key) if self.map_key
               else OSM_URL.format(z=z, x=x, y=y))
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        except (requests.RequestException, OSError) as e:
            log.warning("map tile %s/%s/%s failed: %s", z, x, y, e)
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        os.replace(tmp, path)
        return img

    def _basemap(self, z: int, cx: float, cy: float, w: int, h: int):
        left, top = cx - w / 2, cy - h / 2
        base = Image.new("RGB", (w, h), (32, 36, 44) if self.map_key else (222, 222, 222))
        for ty in range(int(top // TILE), int((top + h) // TILE) + 1):
            for tx in range(int(left // TILE), int((left + w) // TILE) + 1):
                tile = self._tile(z, tx, ty)
                if tile is not None:
                    base.paste(tile, (int(tx * TILE - left), int(ty * TILE - top)))
        return base, left, top

    # ---- drawing ------------------------------------------------------------------
    @staticmethod
    def _font(size: int):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1: fixed-size bitmap font
            return ImageFont.load_default()

    @staticmethod
    def _text(d, xy, text, fill, font):
        """Text vertically centered on xy (the old bitmap font can't anchor, so nudge it instead)."""
        try:
            d.text(xy, text, fill=fill, font=font, anchor="lm")
        except ValueError:
            d.text((xy[0], xy[1] - 6), text, fill=fill, font=font)

    def render(self, a: Alert, geom: dict) -> bytes:
        map_h = HEIGHT - HEADER
        # A storm center with a short track would otherwise zoom to street level.
        z, cx, cy = pick_view(geom, WIDTH, map_h, zmax=6 if a.source == "nhc" else 10)
        base, left, top = self._basemap(z, cx, cy, WIDTH, map_h)
        overlay = Image.new("RGBA", (WIDTH, map_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)

        def px(p):
            x, y = world_px(p[0], p[1], z)
            return x - left, y - top

        def draw(g: dict, color: str, fill_alpha: int, width: int):
            rgb = _rgb(color)
            t = g.get("type")
            polys = []
            if t == "Polygon":
                polys = [g["coordinates"]]
            elif t == "MultiPolygon":
                polys = g["coordinates"]
            for poly in polys:
                outer = [px(p) for p in poly[0]]
                if len(outer) >= 3:
                    d.polygon(outer, fill=rgb + (fill_alpha,))
            for poly in polys:
                for ring in poly:
                    d.line([px(p) for p in ring], fill=rgb + (255,), width=width, joint="curve")
            if t == "LineString":
                d.line([px(p) for p in g["coordinates"]], fill=rgb + (255,), width=width)
            if t == "Point":
                x, y = px(g["coordinates"])
                d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=rgb + (200,), outline=rgb + (255,), width=2)

        color = color_for(a)
        if a.source == "spc" and a.kind == "Outlook":
            labels = a.params.get("risk_labels") or []
            for i, g in enumerate((geom.get("geometries") or [])):
                draw(g, RISK_COLORS.get(labels[i] if i < len(labels) else "", color), 90, 2)
        elif geom.get("type") == "GeometryCollection":
            for g in geom.get("geometries") or []:
                draw(g, color, 70, 3)
        else:
            draw(geom, color, 70 if a.kind != "Watch" else 40, 3)

        img = Image.new("RGB", (WIDTH, HEIGHT), (21, 24, 31))
        img.paste(Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB"), (0, HEADER))
        hd = ImageDraw.Draw(img)
        hd.rectangle([0, 0, 6, HEADER], fill=_rgb(color))
        title = a.headline if a.source in ("nhc", "spc", "swpc") and a.headline else a.event
        office = a.office or a.sender_name
        self._text(hd, (14, HEADER // 2), f"{title} · {office}"[:70], (236, 239, 244), self._font(17))
        attrib = "© OpenStreetMap" + (" © CARTO" if self.map_key else "")
        small = self._font(11)
        tw = hd.textlength(attrib, font=small)
        hd.rectangle([WIDTH - tw - 10, HEIGHT - 16, WIDTH, HEIGHT], fill=(21, 24, 31))
        self._text(hd, (WIDTH - tw - 5, HEIGHT - 8), attrib, (200, 200, 200), small)
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        return out.getvalue()

    def cached_render(self, a: Alert, geom: dict) -> bytes:
        key = hashlib.sha1(a.id.encode()).hexdigest()
        path = self.cache / "img" / key[:2] / f"{key}.png"
        if path.exists():
            return path.read_bytes()
        data = self.render(a, geom)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return data


def drawable(a: Alert) -> bool:
    """Worth attaching a map to: it has a shape, or zones a shape can be built from."""
    if a.source == "swpc" or a.kind == "Product":
        return False
    return bool(a.geometry) or (a.source == "nws" and bool(a.zones))
