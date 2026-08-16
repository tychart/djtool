"""Core data models: Track, Pair, and collection source ranks."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from djtool.text import core_of, guess_from_filename, normalize_text

SOURCES = ("library", "tracks", "incoming")
SOURCE_DIRS = {"library": "Library", "tracks": "Tracks", "incoming": "Incoming"}
SOURCE_RANK = {"library": 0, "tracks": 1, "incoming": 2}


@dataclass
class Track:
    path: Path
    rel: str  # path relative to DJ root, posix separators
    source: str  # one of SOURCES
    size: int
    mtime_ns: int
    sha256: str | None = None
    duration: float | None = None  # seconds
    title: str = ""
    artist: str = ""
    album: str = ""
    track_no: str = ""
    format_desc: str = ""
    fingerprint: str | None = None  # raw chromaprint (fpcalc -raw output)
    fp_duration: float | None = None

    # --- derived views (computed on demand, cached) -----------------------
    @cached_property
    def filename_artist(self) -> str:
        artist, _ = guess_from_filename(self.path.stem)
        return artist or ""

    @cached_property
    def filename_title(self) -> str:
        _, title = guess_from_filename(self.path.stem)
        return title or ""

    @cached_property
    def norm_title(self) -> str:
        return normalize_text(self.title or self.filename_title)

    @cached_property
    def norm_artist(self) -> str:
        return normalize_text(self.artist or self.filename_artist)

    @cached_property
    def core_title(self) -> str:
        s = core_of(self.norm_title)
        return s or self.norm_title

    @cached_property
    def core_artist(self) -> str:
        s = core_of(self.norm_artist)
        return s or self.norm_artist

    @cached_property
    def block_key(self) -> str:
        """Grouping key: normalized artist+title core. Empty when unknown."""
        return "|".join(p for p in (self.core_artist, self.core_title) if p)

    def rel_in_source(self) -> str:
        prefix = SOURCE_DIRS[self.source] + "/"
        return self.rel.removeprefix(prefix)


def display_artist(t: Track) -> str:
    if t.artist:
        return t.artist
    if t.filename_artist:
        return f"{t.filename_artist} (from filename)"
    return "(unknown)"


def display_title(t: Track) -> str:
    if t.title:
        return t.title
    if t.filename_title:
        return f"{t.filename_title} (from filename)"
    return "(unknown)"


@dataclass
class Pair:
    a: Track  # preferred member (Library first, then Tracks, then Incoming)
    b: Track
    category: str = ""
    title_sim: float = 0.0
    artist_sim: float = 0.0
    duration_diff: float | None = None
    dur_close: bool | None = None
    fp_sim: float | None = None
    note: str = ""


def order_pair(a: Track, b: Track) -> tuple[Track, Track]:
    """Put the preferred member first: Library > Tracks > Incoming."""
    if SOURCE_RANK[a.source] < SOURCE_RANK[b.source]:
        return a, b
    if SOURCE_RANK[b.source] < SOURCE_RANK[a.source]:
        return b, a
    return (a, b) if a.path < b.path else (b, a)

