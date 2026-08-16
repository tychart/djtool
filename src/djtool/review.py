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


def action_keep_one(root: Path, pair: Pair, *, keep_a: bool) -> str:
    """[l]/[j]: keep one member, quarantine the other.

    Works for every pair type. A Library file is only removable when the pair
    is Library-internal (the removal is recorded to the decisions file so it
    can be replayed after a Library reset. In cross-source pairs the Library
    copy is protected and removal is refused.
    """
    keep, remove = (pair.a, pair.b) if keep_a else (pair.b, pair.a)
    if remove.source == "library":
        if not is_library_pair(pair):
            return "refused"
        quarantine_library_file(root, remove)
        record_remove_decision(root, kept_rel=keep.rel, removed_rel=remove.rel)
        return "quarantined"
    quarantine_file(root, remove)
    return "quarantined"


def action_keep_both(
    root: Path,
    pair: Pair,
    mode: str,
    *,
    get_input: Callable[[str], str] | None = None,
    console: Console | None = None,
) -> str:
    """[b] Keep both, and record the decision so the pair never re-asks.

    Applies to every pair type: a keep-both pair re-forms on the next scan,
    so it is recorded as resolved. In ingest mode the Incoming copy is
    promoted to Tracks (flat, renamed; a name collision triggers the
    interactive version/skip resolution) and the recorded paths follow the
    promoted file.
    """
    a_rel, b_rel = pair.a.rel, pair.b.rel
    if mode == "ingest":
        targets = [t for t in (pair.a, pair.b) if t.source == "incoming" and t.path.exists()]
        moved = False
        for t in targets:
            dest, action = promote_to_tracks(
                root, t, get_input=get_input, console=console
            )
            if action != "skipped":
                moved = True
                new_rel = dest.relative_to(root).as_posix()
                if t is pair.a:
                    a_rel = new_rel
                else:
                    b_rel = new_rel
        record_keep_both_decision(root, a_rel, b_rel)
        return "promoted" if moved else "kept"
    record_keep_both_decision(root, a_rel, b_rel)
    return "kept"


def is_library_pair(pair: Pair) -> bool:
    """Both members live in the read-only Library (the recorded-decisions case)."""
    return pair.a.source == "library" and pair.b.source == "library"


def action_version(
    root: Path,
    pair: Pair,
    console: Console,
    get_input: Callable[[str], str],
) -> str:
    """[v] Version/rename one or both files and record the decision.

    Works for every pair type. Reuses the Tracks naming scheme
    ('Title - Artist [Version].ext'), applies the rename in place, and records
    it: the pair is treated as resolved (like keep-both) and never re-asked,
    and the rename is re-applied by replay if the old name reappears.
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
    record_rename_decision(root, renames, pair.a.rel, pair.b.rel)
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
        console.out(f"[l] {keep}    [b] Keep both    [v] Version/rename    [p] Play A    [o] Play B")
        console.out(console.dim("    ([b] and [v] are recorded — resolved pairs won't be re-asked)"))
    else:
        keep = "Keep A / remove B"
        console.out(f"[l] {keep}    [j] Keep B / remove A    [b] Keep both    [v] Version/rename    [p] Play A    [o] Play B")
        console.out(console.dim("    ([b] and [v] are recorded — resolved pairs won't be re-asked)"))
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
            if choice == "q":
                quit_now = True
                quit_remaining = round_pairs[idx - 1:] + pending
                break
            if choice == "l":
                res = action_keep_one(root, pair, keep_a=True)
                if res == "quarantined":
                    stats.quarantined += 1
                elif res == "refused":
                    console.warn("both files are in the read-only Library — keeping both")
                    stats.kept += 1
                stats.processed += 1
            elif choice == "j":
                res = action_keep_one(root, pair, keep_a=False)
                if res == "quarantined":
                    stats.quarantined += 1
                elif res == "refused":
                    console.warn("Library/ is read-only — keep the Library copy instead ([l])")
                    stats.kept += 1
                stats.processed += 1
            elif choice == "v":
                res = action_version(root, pair, console, get_input=read)
                if res == "cancelled":
                    pending.append(pair)
                else:
                    stats.processed += 1
                    if res == "renamed":
                        stats.renamed += 1
            elif choice == "b":
                res = action_keep_both(root, pair, mode, get_input=read, console=console)
                if res == "promoted":
                    stats.promoted += 1
                else:
                    stats.kept += 1
                stats.processed += 1
            elif choice == "p":
                play_audio(pair.a.path, console, "A")
                idx -= 1  # auxiliary action: re-prompt the same pair
            elif choice == "o":
                play_audio(pair.b.path, console, "B")
                idx -= 1  # auxiliary action: re-prompt the same pair
            elif choice == "c":
                play_audio(pair.a.path, console, "A")
                play_audio(pair.b.path, console, "B")
                idx -= 1  # auxiliary action: re-prompt the same pair
            elif choice == "i":
                print_info(pair, console)
                idx -= 1  # auxiliary action: re-prompt the same pair
            elif choice == "s":
                stats.skipped += 1
                if round_no == 1:
                    pending.append(pair)  # one more chance in the next round
            else:
                console.out(console.dim("choices: [l] keep A / remove B  [j] keep B / remove A  [b] keep both  "
                                        "[v] version/rename  [p]/[o] play  [c] compare  [i] info  [s] skip  [q] quit"))
                pending.append(pair)
        round_no += 1
        if pending and not quit_now:
            console.out(console.dim(f"— {len(pending)} pair(s) deferred; reviewing again —"))
    stats.remaining = quit_remaining if quit_now else pending
    return stats

