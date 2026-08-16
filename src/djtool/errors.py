"""Expected, user-facing errors for djtool."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from djtool.model import Track
class DjToolError(Exception):
    """Base class for expected, user-facing errors."""


class ConfigError(DjToolError):
    """Bad or missing configuration."""


class ConfirmationRequired(DjToolError):
    """A destructive action needs explicit confirmation."""


class NameCollision(DjToolError):
    """Promotion would clash with an existing Tracks file and needs a decision.

    Raised only for non-interactive callers; the CLI resolves collisions
    interactively instead. Collisions are never resolved by appending numbers.
    """

    def __init__(self, track: Track, target: Path):
        self.track = track
        self.target = target
        super().__init__(
            f"'{track.rel}' would collide with existing '{target.name}' in Tracks/"
        )

