"""Pluggable delivery channels.

The engine builds a Notification and hands it to whatever channels a user has
configured. ntfy is the first adapter; APNs, email, SMS or a branded app can be
added later by implementing Channel, without touching the rules engine.
"""

from .base import Channel, DeliveryError, Notification
from .ntfy import NtfyChannel

CHANNELS: dict[str, type[Channel]] = {"ntfy": NtfyChannel}


def channels_for_user(settings: dict) -> list[Channel]:
    out: list[Channel] = []
    for name, cls in CHANNELS.items():
        conf = (settings.get("channels") or {}).get(name)
        if conf and conf.get("enabled", True):
            out.append(cls(conf))
    return out


__all__ = ["Channel", "DeliveryError", "Notification", "NtfyChannel", "CHANNELS", "channels_for_user"]
