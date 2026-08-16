"""Project state directory.

The config, the derived-data cache, and the recorded-decisions file all live
with the tool (project dir), not with the DJ collection.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from djtool.model import Track

CACHE_FILE_NAME = ".djtool-cache.json"
CONFIG_FILE_NAME = "djtool.toml"
CACHE_VERSION = 2
def project_dir() -> Path:
    """Root of the djtool project itself (holds djtool.toml and the cache).

    djtool is run from its own uv project (editable install), so the package
    lives at <project>/src/djtool/. Falls back to the package dir otherwise.
    """
    return Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------
# Cache (.djtool-cache.json) — derived data only, invalidated by size+mtime
# --------------------------------------------------------------------------


def cache_path() -> Path:
    return project_dir() / CACHE_FILE_NAME


def load_cache(root: Path) -> dict[str, Any]:
    """Load the cache; entries are only usable when tagged with this DJ root."""
    p = cache_path()
    if not p.exists():
        return {"version": CACHE_VERSION, "root": str(root), "entries": {}}
    try:
        data = json.loads(p.read_text())
        if (
            isinstance(data, dict)
            and data.get("root") == str(root)
            and isinstance(data.get("entries"), dict)
        ):
            return data
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "root": str(root), "entries": {}}


def cache_valid(entry: Any, size: int, mtime_ns: int) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("size") == size
        and entry.get("mtime_ns") == mtime_ns
    )


def save_cache(root: Path, entries: dict[str, Any]) -> None:
    data = {"version": CACHE_VERSION, "root": str(root), "entries": entries}
    tmp = cache_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, cache_path())


def clear_cache() -> None:
    try:
        cache_path().unlink()
    except FileNotFoundError:
        pass


def save_cache_from_tracks(root: Path, tracks: list[Track]) -> None:
    entries: dict[str, Any] = {}
    for t in tracks:
        entries[t.rel] = {
            "size": t.size,
            "mtime_ns": t.mtime_ns,
            "sha256": t.sha256,
            "duration": t.duration,
            "title": t.title,
            "artist": t.artist,
            "album": t.album,
            "track_no": t.track_no,
            "format_desc": t.format_desc,
            "fingerprint": t.fingerprint,
            "fp_duration": t.fp_duration,
        }
    save_cache(root, entries)

