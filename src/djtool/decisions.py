"""Recorded pair decisions and their replay.

Some review outcomes are expected to recur, so djtool records them in
.djtool-decisions.json (project dir, so NAS syncs never touch it):

  * keep-both ([b]) and version/rename ([v]) — the pair re-forms on every
    scan, so the decision marks it *resolved* and candidate generation skips
    it from then on;
  * removal of a Library file — the Library folder is often replaced wholesale
    from a NAS, which silently undoes any cleanup, so every `djtool dedupe`
    run replays the removal first (re-quarantine, re-apply renames).

Removals from Tracks/ or Incoming/ are permanent and are not recorded.
Recorded decisions are the only way djtool modifies Library/.

Decisions are matched to files by their relative path; rename decisions track
members through their old -> new names so a pair stays resolved after a
[Version] rename (and after replays / Library resets).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from djtool.console import Console
from djtool.model import SOURCES, Track
from djtool.quarantine import quarantine_file, quarantine_library_file
from djtool.state import project_dir

DECISIONS_FILE_NAME = ".djtool-decisions.json"
DECISIONS_VERSION = 1

VALID_ACTIONS = ("remove", "rename", "keep_both")


# --------------------------------------------------------------------------
# Storage (.djtool-decisions.json) — derived/user-intent data only
# --------------------------------------------------------------------------


def decisions_path() -> Path:
    return project_dir() / DECISIONS_FILE_NAME


def _default_data(root: Path) -> dict[str, Any]:
    return {"version": DECISIONS_VERSION, "root": str(root), "decisions": []}


def load_decisions(root: Path) -> dict[str, Any]:
    """Load the decisions file; entries are only usable for this DJ root."""
    p = decisions_path()
    if not p.exists():
        return _default_data(root)
    try:
        data = json.loads(p.read_text())
        if (
            isinstance(data, dict)
            and data.get("version") == DECISIONS_VERSION
            and data.get("root") == str(root)
            and isinstance(data.get("decisions"), list)
        ):
            return data
    except (OSError, ValueError):
        pass
    return _default_data(root)


def _save(data: dict[str, Any]) -> None:
    tmp = decisions_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, decisions_path())


def _load_or_new(root: Path) -> dict[str, Any]:
    data = load_decisions(root)
    # A file that belongs to a different root (or is corrupt) must never be
    # overwritten: start from a fresh record set instead.
    if data.get("root") != str(root):
        data = _default_data(root)
    return data


def _decision_id(a_rel: str, b_rel: str) -> str:
    """Stable id per pair (order-independent): one decision per pair."""
    key = "|".join(sorted((a_rel, b_rel)))
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def _replace_pair(data: dict[str, Any], a_rel: str, b_rel: str, entry: dict[str, Any]) -> None:
    """Record one decision per pair; a newer decision replaces the old one."""
    did = _decision_id(a_rel, b_rel)
    entry["id"] = did
    kept = [d for d in data["decisions"] if d.get("id") != did]
    kept.append(entry)
    data["decisions"] = kept


# --------------------------------------------------------------------------
# Recording (called from the review flow)
# --------------------------------------------------------------------------


def record_remove_decision(root: Path, kept_rel: str, removed_rel: str) -> None:
    """Record 'keep one file, remove the other'.

    Only used when the removed file lives in Library/ — a Library file would
    reappear on the next NAS reset, so the removal must be replayable.
    Removals from Tracks/ or Incoming/ are permanent and are not recorded.
    """
    data = _load_or_new(root)
    _replace_pair(data, kept_rel, removed_rel, {
        "created": _now_iso(),
        "action": "remove",
        "kept": kept_rel,
        "removed": removed_rel,
    })
    _save(data)


def record_keep_both_decision(root: Path, a_rel: str, b_rel: str) -> None:
    """Record 'keep both' for any pair type.

    The pair re-forms on every scan, so the decision marks it resolved and
    candidate generation skips it from then on.
    """
    data = _load_or_new(root)
    _replace_pair(data, a_rel, b_rel, {
        "created": _now_iso(),
        "action": "keep_both",
        "a": a_rel,
        "b": b_rel,
    })
    _save(data)


def record_rename_decision(root: Path, renames: list[tuple[str, str]], a_rel: str, b_rel: str) -> None:
    """Record renames applied to a pair (version qualifiers, etc.).

    Works for any pair type: the pair is treated as resolved (like keep-both)
    and the rename is re-applied by replay when the old name reappears. The
    pair identity (a_rel, b_rel — the rels at decision time) is stored so the
    decision can be matched even when only one member was renamed.
    """
    if not renames:
        return
    data = _load_or_new(root)
    _replace_pair(data, a_rel, b_rel, {
        "created": _now_iso(),
        "action": "rename",
        "a": a_rel,
        "b": b_rel,
        "renames": [{"from": frm, "to": to} for frm, to in renames],
    })
    _save(data)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Management
# --------------------------------------------------------------------------


def delete_decision(root: Path, decision_id: str) -> int:
    """Remove one recorded decision by id. Returns 1 when removed."""
    data = _load_or_new(root)
    before = len(data["decisions"])
    data["decisions"] = [d for d in data["decisions"] if d.get("id") != decision_id]
    if len(data["decisions"]) == before:
        return 0
    _save(data)
    return 1


def clear_decisions(root: Path) -> int:
    """Remove every recorded decision. Returns the number removed."""
    data = _load_or_new(root)
    n = len(data["decisions"])
    if n:
        _save(_default_data(root))
    return n


# --------------------------------------------------------------------------
# Replay (runs at the start of every `djtool dedupe`)
# --------------------------------------------------------------------------


@dataclass
class ReplayStats:
    removed: int = 0
    renamed: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def _track_for(root: Path, path: Path) -> Track:
    st = path.stat()
    rel = path.relative_to(root).as_posix()
    source = rel.split("/", 1)[0].lower()
    if source not in SOURCES:
        source = "library"  # defensive: recorded paths are collection-relative
    return Track(
        path=path,
        rel=rel,
        source=source,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )


def replay_decisions(root: Path, console: Console | None = None) -> ReplayStats:
    """Apply recorded decisions to the current Library/ state.

    Called at the start of `djtool dedupe`, before any prompting. A decision
    is only applied when the recorded relative paths still exist (i.e. the
    Library was reset and the files are back); anything unexpected is
    reported and skipped.
    """
    stats = ReplayStats()
    console = console or Console(color=False)
    data = load_decisions(root)
    for d in data.get("decisions", []):
        action = d.get("action")
        if action not in VALID_ACTIONS:
            continue
        if action == "keep_both":
            continue  # recorded intent is 'leave them alone' — nothing to do
        if action == "remove":
            _replay_remove(root, d, stats)
        elif action == "rename":
            _replay_rename(root, d, stats)
    if stats.removed or stats.renamed:
        console.out(console.dim(
            f"replayed {stats.removed + stats.renamed} recorded decision(s): "
            f"{stats.removed} file(s) removed, {stats.renamed} renamed"
        ))
    for w in stats.warnings:
        console.warn(w)
    return stats


def _replay_remove(root: Path, d: dict[str, Any], stats: ReplayStats) -> None:
    kept = root / d["kept"]
    removed = root / d["removed"]
    if not removed.exists():
        return  # nothing to do — file not back yet (or already handled)
    if not kept.exists():
        stats.skipped += 1
        stats.warnings.append(
            f"recorded decision {d['id']}: kept file is missing ({d['kept']}) — skipping"
        )
        return
    try:
        track = _track_for(root, removed)
        if track.source == "library":
            quarantine_library_file(root, track)
        else:  # defensive: recorded removals are Library-only in practice
            quarantine_file(root, track)
        stats.removed += 1
    except OSError as e:
        stats.skipped += 1
        stats.warnings.append(f"recorded decision {d['id']}: could not remove {d['removed']}: {e}")


def _replay_rename(root: Path, d: dict[str, Any], stats: ReplayStats) -> None:
    for r in d.get("renames", []):
        src = root / r["from"]
        dst = root / r["to"]
        if not src.exists():
            continue  # not back yet
        if dst.exists():
            stats.skipped += 1
            stats.warnings.append(
                f"recorded decision {d['id']}: '{r['to']}' already exists — skipping rename"
            )
            continue
        try:
            src.rename(dst)
            stats.renamed += 1
        except OSError as e:
            stats.skipped += 1
            stats.warnings.append(f"recorded decision {d['id']}: rename failed ({r['from']}): {e}")


def resolved_pairs(root: Path) -> set[frozenset[str]]:
    """Relative-path keys of pairs the user has already resolved.

    Any recorded decision — remove, rename, keep_both — marks its pair as
    resolved: candidate generation must not re-present it. Rename decisions
    track members through their old -> new names, so a pair stays suppressed
    after a [v] version-rename and after replays / Library resets.
    """
    data = load_decisions(root)
    out: set[frozenset[str]] = set()
    for d in data.get("decisions", []):
        action = d.get("action")
        if action == "remove":
            kept, removed = d.get("kept"), d.get("removed")
            if kept and removed:
                out.add(frozenset((kept, removed)))
        elif action == "rename":
            renames = [r for r in d.get("renames", []) if r.get("from") and r.get("to")]
            a, b = d.get("a"), d.get("b")
            if a and b:
                out.add(frozenset((_current_after_rename(root, a, renames),
                                   _current_after_rename(root, b, renames))))
            elif len(renames) >= 2:
                # legacy entries (pre pair-identity): both members renamed
                out.add(frozenset(
                    r["to"] if (root / r["to"]).exists() else r["from"]
                    for r in renames
                ))
        elif action == "keep_both":
            a, b = d.get("a"), d.get("b")
            if a and b:
                out.add(frozenset((a, b)))
    return out


def _current_after_rename(root: Path, rel: str, renames: list[dict[str, Any]]) -> str:
    """A member's current relative path after (possibly) a recorded rename.

    Returns the rename target when the member was renamed and the target
    exists; otherwise the member's rel as recorded.
    """
    for r in renames:
        if r.get("from") == rel:
            return r["to"] if (root / r["to"]).exists() else rel
    return rel


def describe_decision(d: dict[str, Any]) -> str:
    """One-line human summary of a recorded decision (for 'decisions list')."""
    did = d.get("id", "?")
    action = d.get("action", "?")
    created = d.get("created", "")
    if action == "remove":
        return f"{did}  {created}  remove {d.get('removed')}  (keep {d.get('kept')})"
    if action == "rename":
        parts = "; ".join(f"{r.get('from')} -> {r.get('to')}" for r in d.get("renames", []))
        return f"{did}  {created}  rename: {parts}"
    if action == "keep_both":
        return f"{did}  {created}  keep both ({d.get('a')}, {d.get('b')})"
    return f"{did}  {created}  unknown action {action}"
