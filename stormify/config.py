"""Configuration loading.

Settings come from a TOML file (path in $STORMIFY_CONFIG, else ./config.toml),
falling back to defaults. Per-user settings (rules, ntfy topic, time display)
live in the database, not here.
"""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    db_path: str = "stormify.db"
    # NWS asks every client to identify itself with contact info.
    user_agent: str = "(stormify, change-me@example.com)"
    poll_interval: int = 60
    nws_base_url: str = "https://api.weather.gov"
    # Heartbeat is considered stale (health endpoint returns 503) after this many seconds.
    heartbeat_stale_seconds: int = 300
    # Safety valve for outbreaks: beyond this many pushes in one poll, send one summary instead.
    max_push_per_poll: int = 20
    # On the very first poll (empty database), log everything and push nothing.
    quiet_first_run: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    secret_key: str = ""
    # Base URL of the dashboard, used for click-through links in notifications.
    public_url: str = ""
    # CARTO basemaps key (free at https://carto.com/basemaps/apikey/). Without it the map uses plain OpenStreetMap tiles.
    map_key: str = ""
    extra: dict = field(default_factory=dict)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = path or os.environ.get("STORMIFY_CONFIG") or "config.toml"
    cfg = Config()
    p = Path(path)
    if p.exists():
        with p.open("rb") as f:
            data = tomllib.load(f)
        general = data.get("general", {})
        web = data.get("web", {})
        for key, value in general.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
            else:
                cfg.extra[key] = value
        mapping = {"host": "web_host", "port": "web_port", "secret_key": "secret_key", "public_url": "public_url"}
        for key, value in web.items():
            setattr(cfg, mapping.get(key, key), value)
    if not cfg.secret_key:
        cfg.secret_key = os.environ.get("STORMIFY_SECRET_KEY") or secrets.token_hex(32)
    return cfg
