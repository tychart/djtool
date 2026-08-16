"""CLI command implementations: doctor, scan, dedupe, ingest, trash, cache, decisions."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from djtool.audio import HAVE_MUTAGEN
from djtool.candidates import CATEGORIES, find_candidates
from djtool.config import CONFIG_FILE_NAME, config_path, load_config
from djtool.console import Console, fmt_bytes
from djtool.decisions import (
    clear_decisions,
    decisions_path,
    delete_decision,
    describe_decision,
    load_decisions,
    replay_decisions,
    resolved_pairs,
)
from djtool.errors import ConfigError, NameCollision
from djtool.model import SOURCE_DIRS, SOURCES, Track
from djtool.promote import promote_to_tracks, prune_empty_dirs
from djtool.quarantine import TRASH_DIR_NAME, empty_trash, trash_entries
from djtool.review import ReviewStats, review_pairs
from djtool.scan import ScanStats, scan_collection
from djtool.state import (
    cache_path,
    clear_cache,
    load_cache,
    project_dir,
    save_cache_from_tracks,
)
from djtool.text import HAVE_RAPIDFUZZ


def _tool_version(exe: str, args: list[str]) -> str:
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "(version unknown)"
    text = (p.stdout + p.stderr).splitlines()
    return next((ln.strip() for ln in text if ln.strip()), "(version unknown)")


def cmd_doctor(args: argparse.Namespace, console: Console, root: Path) -> int:
    console.out(f"DJ root:          {root}")
    console.out(f"Python:           {sys.version.split()[0]}  ({sys.executable})")
    console.out(f"Project:          {project_dir()}  (config + cache live here)")
    console.out("")
    console.out(f"mutagen:          {'installed' if HAVE_MUTAGEN else console.red('MISSING')}"
                f" — {'tag metadata available' if HAVE_MUTAGEN else 'install with: uv sync (in djtool/); tags fall back to filenames'}")
    console.out(f"rapidfuzz:        {'installed' if HAVE_RAPIDFUZZ else console.red('MISSING')}"
                f" — {'fuzzy matching' if HAVE_RAPIDFUZZ else 'falling back to difflib'}")
    for name, ver_args in (("fpcalc", ["-version"]), ("ffprobe", ["-version"]),
                           ("ffplay", ["-version"]), ("rsync", ["--version"])):
        exe = shutil.which(name)
        if exe:
            console.out(f"{name:<17}{_tool_version(exe, ver_args)}")
        else:
            hint = {
                "fpcalc": "install chromaprint-tools (dnf) — fingerprints make dedupe far more reliable",
                "ffprobe": "install ffmpeg — used for audio properties when tags are missing",
                "ffplay": "install ffmpeg — used for [p]/[o]/[c] listening",
                "rsync": "install rsync — required for 'sync'",
            }[name]
            console.out(f"{name:<17}{console.red('MISSING')}  ({hint})")
    console.out("")
    console.out("Directories:")
    for source in SOURCES:
        d = root / SOURCE_DIRS[source]
        state = "exists" if d.is_dir() else console.yellow("missing (created on first scan)")
        console.out(f"  {SOURCE_DIRS[source]:<10}{state}")
    lib = root / SOURCE_DIRS["library"]
    if lib.is_dir():
        writable = os.access(lib, os.W_OK)
        if writable:
            console.out(f"  {'Library':<10}{console.yellow('WRITABLE — djtool only modifies it by applying recorded decisions')}")
        else:
            console.out(f"  {'Library':<10}not writable (good)")
    console.out("")
    dpath = decisions_path()
    if dpath.exists():
        n = len(load_decisions(root).get("decisions", []))
        console.out(f"Decisions:        {dpath} ({n} recorded; replayed on 'dedupe')")
    else:
        console.out(f"Decisions:        {dpath} not present (created when a pair is resolved)")
    console.out("")
    cfg_file = config_path()
    if cfg_file.exists():
        try:
            cfg = load_config()
        except ConfigError as e:
            console.out(f"Config:           {console.red(str(e))}")
            return 0
        sync = cfg.get("sync") or {}
        parts = [f"{CONFIG_FILE_NAME} OK"]
        if sync.get("remote") and sync.get("remote_dj"):
            parts.append("[sync] configured")
            if sync.get("local_mixxx"):
                parts.append(f"Mixxx settings: {sync['local_mixxx']}")
        else:
            parts.append("[sync] not configured (optional)")
        console.out("Config:           " + ", ".join(parts))
    else:
        console.out(f"Config:           {cfg_file} not present (optional; needed for sync)")
    console.out("")
    console.out("Reminder: Library/ is read-only — djtool modifies it only by applying")
    console.out("recorded decisions (djtool decisions list). Tracks/ and Incoming/ are writable.")
    return 0


def ensure_dirs(root: Path, console: Console) -> None:
    for source in SOURCES:
        d = root / SOURCE_DIRS[source]
        if not d.is_dir():
            d.mkdir(parents=True)
            console.info(f"created {d.relative_to(root)}/")


def _print_scan_summary(console: Console, tracks: list[Track], stats: ScanStats) -> None:
    console.out("Scanned:")
    for source in SOURCES:
        console.out(f"  {SOURCE_DIRS[source]:<10}{stats.counts[source]} audio file(s)")
    if stats.ignored_non_audio:
        console.out(console.dim(f"  ({stats.ignored_non_audio} non-audio file(s) ignored)"))
    console.out(f"Cache: {stats.cached} reused, {stats.new} new, {stats.stale} stale")
    for w in stats.warnings:
        console.out(console.dim("  warn: " + w))


def cmd_scan(args: argparse.Namespace, console: Console, root: Path) -> int:
    ensure_dirs(root, console)
    tracks, stats = scan_collection(root, use_cache=not args.no_cache)
    _print_scan_summary(console, tracks, stats)
    resolved = resolved_pairs(root)
    pairs = find_candidates(tracks, resolved=resolved)
    skipped = len(find_candidates(tracks)) - len(pairs)

    counts = Counter(p.category for p in pairs)
    if pairs:
        console.out("")
        for cat in CATEGORIES:
            if counts[cat]:
                console.out(f"  {cat}: {counts[cat]} pair(s)")
        if skipped:
            console.out(console.dim(f"  ({skipped} pair(s) already resolved by recorded decisions — skipped)"))
        console.out(console.dim("run 'djtool dedupe' to review, 'djtool ingest' for the Incoming workflow"))
    elif skipped:
        console.out("")
        console.info(f"no new candidates — {skipped} pair(s) already resolved by recorded decisions")
    else:
        console.out("")
        console.info("no duplicates or candidates found")
    save_cache_from_tracks(root, tracks)
    return 0


def cmd_dedupe(args: argparse.Namespace, console: Console, root: Path) -> int:
    ensure_dirs(root, console)
    if not args.no_replay:
        replay_decisions(root, console)
    tracks, stats = scan_collection(root, use_cache=not args.no_cache)
    resolved = resolved_pairs(root)
    pairs = find_candidates(tracks, resolved=resolved)
    skipped = len(find_candidates(tracks)) - len(pairs)
    header = (f"Library {stats.counts['library']} · Tracks {stats.counts['tracks']} · "
              f"Incoming {stats.counts['incoming']} — {len(pairs)} pair(s) for review")
    if skipped:
        header += f" ({skipped} already resolved — skipped)"
    console.out(console.dim(header))
    for w in stats.warnings:
        console.out(console.dim("  warn: " + w))
    if not pairs:
        console.info("no duplicates or candidates found")
        save_cache_from_tracks(root, tracks)
        return 0
    rstats = review_pairs(root, pairs, console, mode="dedupe")
    console.out("")
    console.out(f"Quarantined: {rstats.quarantined}   Kept both: {rstats.kept}   "
                f"Renamed: {rstats.renamed}   Promoted: {rstats.promoted}   Skipped: {rstats.skipped}")
    if rstats.quarantined:
        console.info(f"removed files are in {TRASH_DIR_NAME}/ — inspect with 'djtool trash list', "
                     f"empty with 'djtool trash empty --yes'")
    if rstats.remaining:
        console.warn(f"{len(rstats.remaining)} pair(s) left unresolved — rerun 'djtool dedupe' to review them")
    save_cache_from_tracks(root, tracks)
    return 0


def cmd_ingest(args: argparse.Namespace, console: Console, root: Path) -> int:
    ensure_dirs(root, console)
    tracks, _ = scan_collection(root, use_cache=not args.no_cache)
    incoming = [t for t in tracks if t.source == "incoming"]
    if not incoming:
        console.info("nothing in Incoming/")
        save_cache_from_tracks(root, tracks)
        return 0
    pairs = find_candidates(tracks, resolved=resolved_pairs(root))
    cross = [p for p in pairs if (p.a.source == "incoming") != (p.b.source == "incoming")]
    console.out(console.dim(
        f"Incoming: {len(incoming)} file(s) · {len(cross)} candidate(s) vs Library/Tracks"
    ))
    if cross:
        rstats = review_pairs(root, cross, console, mode="ingest")
        console.out("")
        console.out(f"Quarantined: {rstats.quarantined}   Promoted to Tracks: {rstats.promoted}   "
                    f"Kept both: {rstats.kept}   Skipped: {rstats.skipped}")
    else:
        console.out("no duplicate or alternate-version candidates in Library/Tracks")
        rstats = ReviewStats()
    remaining = [t for t in incoming if t.path.exists()]
    if remaining:
        console.out("")
        console.out("New files (no candidate in Library/Tracks):")
        for t in remaining:
            console.out("  " + t.rel)
        promote = args.promote_new
        if not promote:
            answer = input(f"Promote all {len(remaining)} file(s) to Tracks/? [y/N] ").strip().lower()
            promote = answer in ("y", "yes")
        if promote:
            moved = skipped = 0
            for t in remaining:
                try:
                    dest, action = promote_to_tracks(root, t, get_input=input, console=console)
                except NameCollision as e:
                    console.warn(f"collision not resolved — left in Incoming/: {t.rel} ({e.target.name})")
                    skipped += 1
                    continue
                if action == "skipped":
                    console.warn(f"skipped (collision) — left in Incoming/: {t.rel}")
                    skipped += 1
                    continue
                moved += 1
                console.out(f"  moved: {t.rel}  ->  {dest.relative_to(root).as_posix()}")
            if skipped:
                console.info(f"{skipped} file(s) left in Incoming/ (collision skipped)")
        else:
            console.info("leaving files in Incoming/")
    pruned = prune_empty_dirs(root, "incoming")
    if pruned:
        console.info(f"removed {pruned} now-empty folder(s) under Incoming/")
    save_cache_from_tracks(root, tracks)
    return 0


def cmd_trash(args: argparse.Namespace, console: Console, root: Path) -> int:
    if args.trash_action == "empty":
        n = empty_trash(root, yes=args.yes)
        console.info(f"emptied {TRASH_DIR_NAME}/ — {n} file(s) permanently deleted")
        return 0
    entries = trash_entries(root)
    if not entries:
        console.info(f"{TRASH_DIR_NAME}/ is empty")
        return 0
    by_day: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        by_day.setdefault(e["day"], []).append(e)
    for day in sorted(by_day):
        group = by_day[day]
        total = sum(e["size"] for e in group)
        console.out(console.bold(f"{day}  —  {len(group)} file(s), {fmt_bytes(total)}"))
        for e in sorted(group, key=lambda e: e["rel"]):
            console.out("  " + e["rel"])
    console.out("")
    console.warn("quarantined files are not deleted — 'djtool trash empty --yes' removes them permanently")
    return 0


def cmd_cache(args: argparse.Namespace, console: Console, root: Path) -> int:
    p = cache_path()
    if args.cache_action == "clear":
        clear_cache()
        console.info("cache cleared — nothing was lost; the next scan will just be slower")
        return 0
    if not p.exists():
        console.out(f"cache: {p} — not present")
        return 0
    data = load_cache(root)
    console.out(f"cache: {p}")
    console.out(f"entries: {len(data.get('entries', {}))}   size: {fmt_bytes(p.stat().st_size)}")
    console.out("the cache holds derived data only (hashes, durations, fingerprints);")
    console.out("deleting it can never lose collection state or review decisions")
    return 0


def cmd_decisions(args: argparse.Namespace, console: Console, root: Path) -> int:
    """List, remove or clear recorded pair decisions."""
    data = load_decisions(root)
    decisions = data.get("decisions", [])
    if args.decisions_action == "list":
        p = decisions_path()
        if not decisions:
            console.info(f"no recorded decisions — the file {p} is empty or not present")
            return 0
        console.out(f"{len(decisions)} recorded decision(s) in {p}")
        console.out("each is replayed automatically at the start of every 'djtool dedupe' run:")
        for d in decisions:
            console.out("  " + describe_decision(d))
        return 0
    if args.decisions_action == "clear":
        n = clear_decisions(root)
        console.info(f"cleared {n} recorded decision(s) — they will no longer be replayed")
        return 0
    # remove <id>
    if delete_decision(root, args.id):
        console.info(f"removed decision {args.id}")
        return 0
    console.warn(f"no recorded decision with id '{args.id}'")
    return 1

