from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class DeliveryError(RuntimeError):
    pass


@dataclass
class Notification:
    title: str
    body: str
    priority: int = 3                 # 1 (min) .. 5 (urgent)
    tags: list[str] = field(default_factory=list)
    click_url: str | None = None      # opens when the notification is tapped
    image_url: str | None = None      # map of the alert's shape (attached by ntfy)
    group: str | None = None          # thread key, for channels that can group/replace
    is_test: bool = False


class Channel(ABC):
    name = "channel"

    def __init__(self, conf: dict):
        self.conf = conf

    @abstractmethod
    def send(self, n: Notification) -> None:
        """Deliver one notification or raise DeliveryError."""
