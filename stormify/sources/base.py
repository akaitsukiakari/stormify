from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Alert


class Source(ABC):
    """A pollable feed of alerts.

    Future sources (SPC outlooks/MDs, NHC, SWPC space weather, hail) implement
    this same interface, so the rules engine and delivery never change.
    """

    name: str = "source"

    @abstractmethod
    def poll(self) -> list[Alert] | None:
        """Return the current alerts, or None if nothing changed since the last poll."""
