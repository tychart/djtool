"""Review actions, rendering, and the interactive pair-review loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from djtool import fingerprint
from djtool.candidates import ensure_fingerprints
from djtool.console import Console, fmt_bytes, fmt_duration
from djtool.decisions import (
    record_keep_both_decision,
    record_remove_decision,
    record_rename_decision,
)
from djtool.model import SOURCE_DIRS, Pair, Track, display_artist, display_title
from djtool.naming import derive_track_name
from djtool.playback import play_audio
from djtool.promote import _ask_version, promote_to_tracks
from djtool.quarantine import quarantine_file, quarantine_library_file


def action_keep_preferred(root: Path, pair: Pair, mode: str) -> str:
    """[l] Keep the preferred file (A), quarantine B. Returns an action name."""
    if pair.b.source == "library":
        return "refused"  # both files are in the read-only Library
    quarantine_file(root, pair.b)
    return "quarantined"


def action_keep_both(
    root: Path,
    pair: Pair,
    mode: str,
    *,
    get_input: Callable[[str], str] | None = None,
    console: Console | None = None,
) -> str:
    """[b] Keep both. In ingest mode the Incoming copy is promoted to Tracks.

    Promotion flattens and renames the file ('Title - Artist [Version].ext');
    a name collision triggers the interactive resolution (version / skip).
    """
    if mode == "ingest":
        targets = [t for t in (pair.a, pair.b) if t.source == "incoming" and t.path.exists()]
        moved = False
        for t in targets:
            _, action = promote_to_tracks(
                root, t, get_input=get_input, console=console
            )
            if action != "skipped":
                moved = True
        return "promoted" if moved else "kept"
    return "kept"


def is_library_pair(pair: Pair) -> bool:
    """Both members live in the read-only Library (the recorded-decisions case)."""
    return pair.a.source == "library" and pair.b.source == "library"


def action_keep_preferred_library(root: Path, pair: Pair, console: Console) -> str:
    """[l] for Library-internal pairs: keep A, remove B, record the decision."""
    quarantine_library_file(root, pair.b)
    record_remove_decision(root, kept_rel=pair.a.rel, removed_rel=pair.b.rel)
    return "quarantined"


def action_keep_b_library(root: Path, pair: Pair, console: Console) -> str:
    """[j] for Library-internal pairs: keep B, remove A, record the decision."""
    quarantine_library_file(root, pair.a)
    record_remove_decision(root, kept_rel=pair.b.rel, removed_rel=pair.a.rel)
    return "quarantined"


def action_keep_both_library(root: Path, pair: Pair, console: Console) -> str:
    """[b] for Library-internal pairs: keep both, record so it never re-asks."""
    record_keep_both_decision(root, pair.a.rel, pair.b.rel)
    return "kept"


def action_version_library(
    root: Path,
    pair: Pair,
    console: Console,
    get_input: Callable[[str], str],
) -> str:
    """[v] for Library-internal pairs: version/rename one or both files.

    Reuses the Tracks naming scheme ('Title - Artist [Version].ext'), applies
    the rename immediately, and records it so it is re-applied after a
    Library reset.
    """
    choice = (get_input("Version A, B, or both? [a/b/x] (. to cancel): ") or "").strip().lower()
    if choice in (".", "q"):
        return "cancelled"
    if choice == "a":
        selected = [pair.a]
    elif choice == "b":
        selected = [pair.b]
    elif choice in ("x", "both", "ab"):
        selected = [pair.a, pair.b]
    else:
        console.out(console.dim("choices: [a] version A  [b] version B  [x] version both  [.] cancel"))
        return "cancelled"

    planned: list[tuple[Track, Path, Path]] = []  # (track, source path, target path)
    for t in selected:
        version = _ask_version(console, get_input, f"the {'A' if t is pair.a else 'B'} file")
        if version is None:
            console.out(console.dim("cancelled"))
            return "cancelled"
        dst = t.path.with_name(derive_track_name(t, version=version))
        if dst.exists() or any(dst == p[2] for p in planned):
            console.warn(f"'{dst.name}' already exists in that folder — pick a different version")
            return "cancelled"
        planned.append((t, t.path, dst))

    renames: list[tuple[str, str]] = []
    for t, src, dst in planned:
        src.rename(dst)
        renames.append((t.rel, dst.relative_to(root).as_posix()))
    record_rename_decision(root, renames)
    return "renamed"


@dataclass
class ReviewStats:
    processed: int = 0
    quarantined: int = 0
    promoted: int = 0
    renamed: int = 0
    kept: int = 0
    skipped: int = 0
    played: int = 0
    remaining: list[Pair] = field(default_factory=list)


SEVERITY_STYLE = {
    "EXACT_DUPLICATE": "magenta",
    "VERY_LIKELY_SAME_RECORDING": "yellow",
    "POSSIBLE_SAME_RECORDING": "cyan",
    "POSSIBLE_ALTERNATE_VERSION": "dim",
}


def render_track(console: Console, label: str, track: Track, preferred: bool) -> None:
    tag = f"{label} [{SOURCE_DIRS[track.source].upper()}"
    if preferred and track.source == "library":
        tag += " — PREFERRED"
    tag += "]"
    console.out(console.bold(tag))
    console.out(f"Artist:   {display_artist(track)}")
    console.out(f"Title:    {display_title(track)}")
    console.out(f"Duration: {fmt_duration(track.duration)}")
    console.out(f"Format:   {track.format_desc or 'unknown'}")
    console.out(f"Path:     {track.rel}")


def render_pair(pair: Pair, index: int, total: int, round_no: int, console: Console, mode: str) -> None:
    style = getattr(console, SEVERITY_STYLE.get(pair.category, "cyan"))
    header = f"[{index}/{total}] {style(pair.category)}"
    if round_no > 1:
        header += "  (deferred)"
    console.out(console.bold(header))
    console.out()
    render_track(console, "A", pair.a, preferred=True)
    console.out()
    render_track(console, "B", pair.b, preferred=False)
    console.out()
    console.out(f"{'Title similarity:':<26}{pair.title_sim * 100:5.1f}%")
    console.out(f"{'Artist similarity:':<26}{pair.artist_sim * 100:5.1f}%")
    if pair.duration_diff is not None:
        console.out(f"{'Duration difference:':<26}{pair.duration_diff:5.1f} s")
    else:
        console.out(f"{'Duration difference:':<26}  n/a")
    if pair.fp_sim is not None:
        console.out(f"{'Chromaprint similarity:':<26}{pair.fp_sim * 100:5.1f}%")
    elif fingerprint.FPCALC is None:
        console.out(f"{'Chromaprint similarity:':<26}  n/a (fpcalc not installed)")
    else:
        console.out(f"{'Chromaprint similarity:':<26}  n/a")
    if pair.note:
        console.out(console.dim("Note: " + pair.note))
    console.out()
    if is_library_pair(pair):
        console.out(console.dim("both files are in Library/ — your choice is recorded in the decisions file"))
        console.out(console.dim("and replayed automatically if the Library folder is reset (djtool decisions list)"))
        console.out("[l] Keep A / remove B    [j] Keep B / remove A    [b] Keep both    [v] Version/rename")
        console.out("    (all four are recorded)    [p] Play A    [o] Play B")
    elif pair.a.source == "library":
        keep = "Keep Library / remove B"
        console.out(f"[l] {keep}    [b] Keep both    [p] Play A    [o] Play B")
    else:
        keep = "Keep A / remove B"
        console.out(f"[l] {keep}    [b] Keep both    [p] Play A    [o] Play B")
    console.out("[c] Compare audio (A then B)    [i] More info    [s] Skip for now    [q] Quit safely")
    if mode == "ingest":
        console.out(console.dim("[b] in ingest mode promotes the Incoming copy to Tracks/ (flat, renamed)."))


def print_info(pair: Pair, console: Console) -> None:
    for label, t in (("A", pair.a), ("B", pair.b)):
        console.out(console.bold(f"--- {label} ---"))
        console.out(f"Path:       {t.path}")
        console.out(f"Size:       {fmt_bytes(t.size)}")
        console.out(f"Modified:   {datetime.fromtimestamp(t.mtime_ns / 1e9).isoformat(timespec='seconds')}")  # noqa: DTZ006 - local time display
        console.out(f"SHA-256:    {t.sha256 or '(not computed)'}")
        console.out(f"Tags:       title={t.title or '—'} artist={t.artist or '—'} "
                    f"album={t.album or '—'} track={t.track_no or '—'}")
        if t.filename_artist and t.filename_title:
            console.out(f"Filename:   {t.filename_artist} - {t.filename_title}")
        else:
            console.out(f"Filename:   {t.filename_title or t.path.name}")
        console.out(f"Duration:   {fmt_duration(t.duration)}")
        console.out(f"Fingerprint: {'yes' if t.fingerprint else 'no'}")


def pair_obsolete(pair: Pair) -> bool:
    return not pair.a.path.exists() or not pair.b.path.exists()


def review_pairs(
    root: Path,
    pairs: list[Pair],
    console: Console,
    *,
    mode: str,
    get_input: Callable[[str], str] | None = None,
) -> ReviewStats:
    """Present one pair at a time; a pair gets at most two chances.

    Quarantine is the only removal action, and it goes to .Trash. 'q' quits
    safely at any time.
    """
    stats = ReviewStats()
    read = get_input if get_input is not None else input
    fp_note_shown = False
    pending: list[Pair] = list(pairs)
    round_no = 1
    quit_now = False
    quit_remaining: list[Pair] | None = None
    while pending and not quit_now:
        round_pairs, pending = pending, []
        idx = 0
        while idx < len(round_pairs):
            pair = round_pairs[idx]
            idx += 1
            if pair_obsolete(pair):
                continue
            if fingerprint.FPCALC and not fp_note_shown:
                console.out(console.dim("computing Chromaprint fingerprints…"))
                fp_note_shown = True
            ensure_fingerprints(pair)
            render_pair(pair, idx, len(round_pairs), round_no, console, mode)
            choice = (read("Choice: ") or "").strip().lower()
            lib_pair = is_library_pair(pair)
            if choice == "q":
                quit_now = True
                quit_remaining = round_pairs[idx - 1:] + pending
                break
            if choice == "l":
                if lib_pair:
                    res = action_keep_preferred_library(root, pair, console)
                    stats.quarantined += 1
                else:
                    res = action_keep_preferred(root, pair, mode)
                    if res == "quarantined":
                        stats.quarantined += 1
                    elif res == "refused":
                        console.warn("both files are in the read-only Library — keeping both")
                        stats.kept += 1
                stats.processed += 1
            elif choice == "j":
                if not lib_pair:
                    console.out(console.dim("[j] is only available when both files are in Library/"))
                    pending.append(pair)
                    continue
                action_keep_b_library(root, pair, console)
                stats.quarantined += 1
                stats.processed += 1
            elif choice == "v":
                if not lib_pair:
                    console.out(console.dim("[v] is only available when both files are in Library/"))
                    pending.append(pair)
                    continue
                res = action_version_library(root, pair, console, get_input=read)
                if res == "cancelled":
                    pending.append(pair)
                else:
                    stats.processed += 1
                    if res == "renamed":
                        stats.renamed += 1
            elif choice == "b":
                if lib_pair:
                    action_keep_both_library(root, pair, console)
                    stats.kept += 1
                else:
                    res = action_keep_both(root, pair, mode, get_input=read, console=console)
                    if res == "promoted":
                        stats.promoted += 1
                    else:
                        stats.kept += 1
                stats.processed += 1
            elif choice == "p":
                play_audio(pair.a.path, console, "A")
                pending.append(pair)
            elif choice == "o":
                play_audio(pair.b.path, console, "B")
                pending.append(pair)
            elif choice == "c":
                play_audio(pair.a.path, console, "A")
                play_audio(pair.b.path, console, "B")
                pending.append(pair)
            elif choice == "i":
                print_info(pair, console)
                pending.append(pair)
            elif choice == "s":
                stats.skipped += 1
                if round_no == 1:
                    pending.append(pair)  # one more chance in the next round
            else:
                console.out(console.dim("choices: [l] keep A / remove B  [b] keep both  [p]/[o] play  "
                                        "[c] compare  [i] info  [s] skip  [q] quit"))
                pending.append(pair)
        round_no += 1
        if pending and not quit_now:
            console.out(console.dim(f"— {len(pending)} pair(s) deferred; reviewing again —"))
    stats.remaining = quit_remaining if quit_now else pending
    return stats

