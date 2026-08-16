"""Collection scan: walk Library/, Tracks/, Incoming/ and build Track objects."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from djtool.audio import (
    AUDIO_EXTS,
    HAVE_MUTAGEN,
    MUTAGEN_READABLE,
    describe_format,
    ffprobe_info,
    read_tags,
)
from djtool.model import SOURCE_DIRS, SOURCES, Track
from djtool.state import cache_valid, load_cache


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

@dataclass
class ScanStats:
    counts: dict[str, int] = field(default_factory=lambda: {s: 0 for s in SOURCES})
    cached: int = 0
    new: int = 0
    stale: int = 0
    ignored_non_audio: int = 0
    warnings: list[str] = field(default_factory=list)


def _track_from_cache(path: Path, rel: str, source: str, st: os.stat_result, entry: dict) -> Track:
    t = Track(path=path, rel=rel, source=source, size=st.st_size, mtime_ns=st.st_mtime_ns)
    t.sha256 = entry.get("sha256")
    t.duration = entry.get("duration")
    t.title = entry.get("title") or ""
    t.artist = entry.get("artist") or ""
    t.album = entry.get("album") or ""
    t.track_no = entry.get("track_no") or ""
    t.format_desc = entry.get("format_desc") or ""
    t.fingerprint = entry.get("fingerprint")
    t.fp_duration = entry.get("fp_duration")
    return t


def _track_from_disk(path: Path, rel: str, source: str, st: os.stat_result, stats: ScanStats) -> Track:
    t = Track(path=path, rel=rel, source=source, size=st.st_size, mtime_ns=st.st_mtime_ns)
    if HAVE_MUTAGEN:
        tags, info = read_tags(path)
        t.title = tags.get("title") or ""
        t.artist = tags.get("artist") or ""
        t.album = tags.get("album") or ""
        t.track_no = tags.get("track_no") or ""
        t.duration = tags.get("duration")
        probe: dict[str, Any] = {}
        if t.duration is None or info is None:
            probe = ffprobe_info(path)
            if t.duration is None:
                t.duration = probe.get("duration")
        if info is None and not probe and path.suffix.lower() in MUTAGEN_READABLE:
            stats.warnings.append(f"could not read audio info: {rel}")
        t.format_desc = describe_format(path, info, probe)
    else:
        # Without mutagen: duration via ffprobe only, no tags.
        probe = ffprobe_info(path)
        t.duration = probe.get("duration")
        t.format_desc = describe_format(path, None, probe)
    return t


def _hash_size_groups(tracks: list[Track], stats: ScanStats) -> None:
    """Hash only files whose size is shared by at least one other file.

    Byte-identical files always have identical sizes, so this cannot miss an
    exact duplicate while avoiding hashing the entire library.
    """
    by_size: dict[int, list[Track]] = {}
    for t in tracks:
        by_size.setdefault(t.size, []).append(t)
    for group in by_size.values():
        if len(group) < 2:
            continue
        for t in group:
            if t.sha256 is None:
                try:
                    t.sha256 = sha256_file(t.path)
                except OSError as e:
                    stats.warnings.append(f"could not read {t.rel}: {e}")


def scan_collection(root: Path, use_cache: bool = True) -> tuple[list[Track], ScanStats]:
    """Walk Library/, Tracks/, Incoming/ and build Track objects.

    Uses .djtool-cache.json for derived data (tags, hashes, fingerprints),
    invalidating entries whose size or mtime changed.
    """
    entries = load_cache(root).get("entries", {}) if use_cache else {}
    tracks: list[Track] = []
    stats = ScanStats()
    for source in SOURCES:
        srcdir = root / SOURCE_DIRS[source]
        if not srcdir.is_dir():
            continue
        for path in sorted(srcdir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(part.startswith(".") for part in rel.split("/")):
                continue  # hidden files/dirs are not collection content
            if path.suffix.lower() not in AUDIO_EXTS:
                stats.ignored_non_audio += 1
                continue
            st = path.stat()
            entry = entries.get(rel)
            if cache_valid(entry, st.st_size, st.st_mtime_ns):
                stats.cached += 1
                tracks.append(_track_from_cache(path, rel, source, st, entry))
            else:
                stats.new += 1
                if entry is not None:
                    stats.stale += 1
                tracks.append(_track_from_disk(path, rel, source, st, stats))
            stats.counts[source] += 1
    _hash_size_groups(tracks, stats)
    return tracks, stats

