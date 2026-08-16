"""Promotion of Incoming files into flat Tracks/ with name-collision resolution."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from djtool.audio import HAVE_MUTAGEN, read_tags
from djtool.console import Console, fmt_duration
from djtool.errors import NameCollision
from djtool.model import SOURCE_DIRS, Track, display_artist, display_title
from djtool.naming import _stem_base, canonicalize_version, derive_track_name
from djtool.quarantine import _ensure_within
from djtool.text import guess_from_filename


def promote_to_tracks(
    root: Path,
    track: Track,
    *,
    get_input: Callable[[str], str] | None = None,
    console: Console | None = None,
) -> tuple[Path, str]:
    """Move an Incoming file flat into Tracks/ as 'Title - Artist [Version].ext'.

    The Incoming/ folder structure is discarded — only the file itself moves,
    renamed from its tags (filename parsing as fallback). Name collisions are
    never auto-resolved with numbers: interactive resolution is required, or a
    NameCollision is raised for non-interactive callers (get_input=None).

    Returns (destination, action) with action in
    {'promoted', 'renamed', 'renamed-existing', 'renamed-both', 'skipped'}.
    """
    if track.source != "incoming":
        raise ValueError("only Incoming files can be promoted to Tracks")
    root = root.resolve()
    src = track.path.resolve()
    _ensure_within(src, root, "source")
    dest = root / SOURCE_DIRS["tracks"] / derive_track_name(track)
    if dest.exists():
        if get_input is None:
            raise NameCollision(track, dest)
        return resolve_name_collision(
            track, dest, console or Console(color=False), get_input
        )
    shutil.move(str(src), str(dest))
    return dest, "promoted"


def _ask_version(
    console: Console,
    get_input: Callable[[str], str],
    what: str,
) -> str | None:
    """Prompt for a canonical version label. Returns None when cancelled."""
    while True:
        raw = (
            get_input(f"Version for {what} (e.g. 'Radio Edit'; '.' to cancel): ") or ""
        ).strip()
        if raw in (".", "q"):
            return None
        if raw.lower() in ("none", "no version", "-", "n/a"):
            console.warn("a version label is required to distinguish the files")
            continue
        version = canonicalize_version(raw)
        if version:
            return version
        console.warn(
            "a version label is needed (e.g. 'Clean', 'Radio Edit', 'The Blessed Madonna Remix')"
        )


def _file_summary(path: Path) -> dict[str, str]:
    """Best-effort (artist, title, duration) for a path, for display only."""
    out = {"artist": "", "title": "", "duration": "unknown"}
    if HAVE_MUTAGEN:
        tags, _info = read_tags(path)
        if tags.get("artist"):
            out["artist"] = tags["artist"]
        if tags.get("title"):
            out["title"] = tags["title"]
        if tags.get("duration"):
            out["duration"] = fmt_duration(tags["duration"])
    fa, ft = guess_from_filename(path.stem)
    if not out["artist"] and fa:
        out["artist"] = f"{fa} (from filename)"
    if not out["title"] and ft:
        out["title"] = f"{ft} (from filename)"
    out["artist"] = out["artist"] or "(unknown)"
    out["title"] = out["title"] or "(unknown)"
    return out


def _show_collision(console: Console, track: Track, target: Path) -> None:
    existing = _file_summary(target)
    console.out()
    console.out(console.bold(f"Target filename already exists: {target.name}"))
    console.out()
    console.out(console.bold("Existing (Tracks/):"))
    console.out(f"  Artist:    {existing['artist']}")
    console.out(f"  Title:     {existing['title']}")
    console.out(f"  Duration:  {existing['duration']}")
    console.out()
    console.out(console.bold(f"Incoming ({track.rel}):"))
    console.out(f"  Artist:    {display_artist(track)}")
    console.out(f"  Title:     {display_title(track)}")
    console.out(f"  Duration:  {fmt_duration(track.duration)}")
    console.out(f"  Fingerprint: {'yes' if track.fingerprint else 'no'}")
    console.out()
    console.out("These are likely different versions of the same track — both can")
    console.out("be kept, but they need distinct names (never auto-numbered).")
    console.out("[v] Version the incoming file    [e] Version the existing file")
    console.out("[b] Version both                 [s] Skip (leave in Incoming/)")


def resolve_name_collision(
    track: Track,
    target: Path,
    console: Console,
    get_input: Callable[[str], str],
) -> tuple[Path, str]:
    """Interactively resolve a promotion collision; never appends numbers.

    'v' versions the incoming file, 'e' versions the existing Tracks file,
    'b' versions both, 's' leaves the incoming file in Incoming/.
    Returns (destination, action).
    """
    tracks_dir = target.parent
    ext = target.suffix
    src = track.path.resolve()
    base = _stem_base(target.stem)
    while True:
        _show_collision(console, track, target)
        choice = (get_input("Choice: ") or "").strip().lower()
        if choice in ("s", "q", "."):
            return target, "skipped"
        if choice == "v":
            version = _ask_version(console, get_input, "the incoming file")
            if version is None:
                continue
            dest = tracks_dir / f"{base} [{version}]{ext}"
            if dest.exists():
                console.warn(f"'{dest.name}' already exists — pick a different version")
                continue
            shutil.move(str(src), str(dest))
            return dest, "renamed"
        if choice in ("e", "b"):
            v_existing = _ask_version(console, get_input, "the existing file")
            if v_existing is None:
                continue
            new_existing = tracks_dir / f"{base} [{v_existing}]{ext}"
            if new_existing.exists():
                console.warn(f"'{new_existing.name}' already exists — pick a different version")
                continue
            if choice == "e":
                shutil.move(str(target), str(new_existing))
                shutil.move(str(src), str(target))
                return target, "renamed-existing"
            v_incoming = _ask_version(console, get_input, "the incoming file")
            if v_incoming is None:
                continue
            if v_incoming == v_existing:
                console.warn("both files need distinct version labels to coexist")
                continue
            dest = tracks_dir / f"{base} [{v_incoming}]{ext}"
            if dest.exists():
                console.warn(f"'{dest.name}' already exists — pick a different version")
                continue
            shutil.move(str(target), str(new_existing))
            shutil.move(str(src), str(dest))
            return dest, "renamed-both"
        console.out("choices: [v] version the incoming file  [e] version the existing file  "
                    "[b] version both  [s] skip (leave in Incoming/)")


def prune_empty_dirs(root: Path, source: str) -> int:
    """Remove now-empty subdirectories under <SourceDir>, bottom-up."""
    base = root / SOURCE_DIRS[source]
    if not base.is_dir():
        return 0
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(base, topdown=False):
        p = Path(dirpath)
        if p == base:
            continue
        try:
            p.rmdir()  # only succeeds when empty
            removed += 1
        except OSError:
            pass
    return removed

