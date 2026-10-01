"""ntfy adapter (https://ntfy.sh). Works with ntfy.sh or a self-hosted server.

User settings:
  "channels": {"ntfy": {"server": "http://localhost:2586", "topic": "stormify-scott",
                         "token": null, "enabled": true}}
"""

from __future__ import annotations

import requests

from .base import Channel, DeliveryError, Notification

# ntfy emoji shortcodes shown next to the title.
PRIORITY_TAGS = {5: "rotating_light", 4: "warning", 3: "cloud_with_lightning", 2: "cloud", 1: "information_source"}


class NtfyChannel(Channel):
    name = "ntfy"

    def __init__(self, conf: dict, session: requests.Session | None = None):
        super().__init__(conf)
        if not conf.get("topic"):
            raise DeliveryError("ntfy channel needs a topic")
        self.server = (conf.get("server") or "https://ntfy.sh").rstrip("/")
        self.session = session or requests.Session()

    def payload(self, n: Notification) -> dict:
        tags = [PRIORITY_TAGS.get(n.priority, "cloud")] + list(n.tags)
        if n.is_test:
            tags.append("test_tube")
        body = {
            "topic": self.conf["topic"],
            "title": n.title[:250],
            "message": n.body[:4000] or n.title,
            "priority": max(1, min(5, int(n.priority))),
            "tags": tags,
        }
        if n.click_url:
            body["click"] = n.click_url
        if n.image_url:
            body["attach"] = n.image_url
        return body

    def send(self, n: Notification) -> None:
        headers = {}
        if self.conf.get("token"):
            headers["Authorization"] = f"Bearer {self.conf['token']}"
        try:
            resp = self.session.post(self.server, json=self.payload(n), headers=headers, timeout=15)
        except requests.RequestException as e:
            raise DeliveryError(f"ntfy unreachable: {e}") from e
        if resp.status_code >= 300:
            raise DeliveryError(f"ntfy returned {resp.status_code}: {resp.text[:200]}")
