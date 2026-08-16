"""Quarantine (.Trash) — the only way djtool 'removes' files."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from djtool.errors import ConfirmationRequired, DjToolError
from djtool.model import Track

TRASH_DIR_NAME = ".Trash"


def _unique(path: Path) -> Path:
    """Return path, or path ' - 2', ' - 3', ... if it already exists."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} - {n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise DjToolError(f"cannot find a free name for {path}")


def _ensure_within(path: Path, root: Path, what: str) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        raise ValueError(f"{what} escapes the DJ root: {path}") from None


def trash_dir_for(root: Path, when: datetime | None = None) -> Path:
    # Local calendar day is intentional for quarantine folders (DTZ005).
    return root / TRASH_DIR_NAME / (when or datetime.now()).strftime("%Y-%m-%d")  # noqa: DTZ005


def quarantine_file(root: Path, track: Track) -> Path:
    """Move a non-Library file flat into .Trash/YYYY-MM-DD/<source>/<filename>.

    The quarantine is flattened like Tracks/ — Mixxx skips the hidden .Trash
    folder, so folder structure there has no scanning benefit. Numeric suffixes
    are acceptable here: .Trash is a temporary recovery area, not the canonical
    collection. Refuses Library files outright.
    """
    if track.source == "library":
        raise ValueError("refusing to quarantine a Library file (Library is read-only)")
    return _quarantine(root, track)


def quarantine_library_file(root: Path, track: Track) -> Path:
    """Move a Library file into .Trash/YYYY-MM-DD/library/.

    The read-only rule is waived only for recorded decisions: callers must
    have persisted the decision to the decisions file (see djtool.decisions),
    which is what makes the removal replayable after a Library reset.
    """
    if track.source != "library":
        raise ValueError("quarantine_library_file is only for Library files")
    return _quarantine(root, track)


def _quarantine(root: Path, track: Track) -> Path:
    root = root.resolve()
    src = track.path.resolve()
    _ensure_within(src, root, "source")
    dest = trash_dir_for(root) / track.source / track.path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = _unique(dest)
    shutil.move(str(src), str(dest))
    return dest


def trash_entries(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    trash = root / TRASH_DIR_NAME
    if not trash.is_dir():
        return entries
    for day in sorted(p for p in trash.iterdir() if p.is_dir()):
        for p in sorted(day.rglob("*")):
            if p.is_file():
                entries.append({
                    "path": p,
                    "day": day.name,
                    "rel": p.relative_to(day).as_posix(),
                    "size": p.stat().st_size,
                })
    return entries


def empty_trash(root: Path, yes: bool = False) -> int:
    """Permanently delete quarantined files. Requires explicit confirmation."""
    trash = root / TRASH_DIR_NAME
    if not trash.is_dir():
        return 0
    n = sum(1 for p in trash.rglob("*") if p.is_file())
    if n and not yes:
        raise ConfirmationRequired(
            f"{n} file(s) in {TRASH_DIR_NAME}/ will be permanently deleted — pass --yes to confirm"
        )
    shutil.rmtree(trash)
    return n

