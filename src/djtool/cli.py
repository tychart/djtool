"""Command-line entry point: argument parsing and dispatch."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from pathlib import Path

from djtool import __version__
from djtool.commands import (
    cmd_cache,
    cmd_decisions,
    cmd_dedupe,
    cmd_doctor,
    cmd_ingest,
    cmd_scan,
    cmd_trash,
)
from djtool.config import CONFIG_FILE_NAME, load_config
from djtool.console import Console
from djtool.errors import ConfigError, DjToolError
from djtool.model import SOURCE_DIRS, SOURCES
from djtool.sync import cmd_sync


def detect_root(script_dir: Path) -> Path:
    """DJ root = nearest ancestor (or self) containing Library/, Tracks/ or Incoming/."""
    for d in (script_dir, *script_dir.parents):
        if any((d / SOURCE_DIRS[s]).is_dir() for s in SOURCES):
            return d
    return script_dir


def resolve_root(override: str | None) -> Path:
    """Resolve the DJ root: --root, $DJTOOL_ROOT, [collection] root, then autodetect.

    With the project living outside the collection (e.g. ~/programs/djtool) the
    config setting is the normal path; auto-detection only helps when the
    project is inside the DJ folder.
    """
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("DJTOOL_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    cfg = load_config()
    configured = (cfg.get("collection") or {}).get("root")
    if configured:
        return Path(configured).expanduser().resolve()
    detected = detect_root(Path(__file__).resolve().parent)
    if not any((detected / SOURCE_DIRS[s]).is_dir() for s in SOURCES):
        raise ConfigError(
            "could not locate the DJ collection — add a [collection] root to "
            f"{CONFIG_FILE_NAME} (or pass --root / set DJTOOL_ROOT)"
        )
    return detected


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="djtool",
        description="Manage a DJ music collection: duplicate review, Incoming ingestion, sync.",
        epilog="Run 'djtool <command> -h' for command help. DJ root is auto-detected "
               "as the directory containing Library/, Tracks/ and Incoming/.",
    )
    p.add_argument("--version", action="version", version=f"djtool {__version__}")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument("--root", metavar="DIR", help="DJ root (default: auto-detected)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, help_: str, func: Callable[..., int]) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=func)
        return sp

    add("doctor", "Check environment, dependencies and DJ root", cmd_doctor)
    sp = add("scan", "Scan the collection and summarize duplicates/candidates", cmd_scan)
    sp.add_argument("--no-cache", action="store_true", help="ignore the derived-data cache")
    sp = add("dedupe", "Interactive duplicate review (quarantine goes to .Trash)", cmd_dedupe)
    sp.add_argument("--no-cache", action="store_true", help="ignore the derived-data cache")
    sp.add_argument("--no-replay", action="store_true",
                    help="do not replay recorded decisions")
    sp = add("ingest", "Review Incoming/ against Library/ and Tracks/, then promote or quarantine", cmd_ingest)
    sp.add_argument("--no-cache", action="store_true", help="ignore the derived-data cache")
    sp.add_argument("--promote-new", action="store_true", help="promote new files without asking")

    sp = sub.add_parser("trash", help="Inspect or empty the quarantine")
    tr = sp.add_subparsers(dest="trash_action", metavar="ACTION", required=True)
    tr.add_parser("list", help="list quarantined files").set_defaults(func=cmd_trash)
    empty = tr.add_parser("empty", help="permanently delete quarantined files")
    empty.add_argument("--yes", action="store_true", help="confirm permanent deletion")
    empty.set_defaults(func=cmd_trash)

    sp = sub.add_parser("cache", help="Inspect or clear the derived-data cache")
    ca = sp.add_subparsers(dest="cache_action", metavar="ACTION", required=True)
    ca.add_parser("status", help="show cache path, size, entry count").set_defaults(func=cmd_cache)
    ca.add_parser("clear", help="delete the cache (safe: only derived data)").set_defaults(func=cmd_cache)

    sp = sub.add_parser("decisions", help="List, remove or clear recorded pair decisions")
    de = sp.add_subparsers(dest="decisions_action", metavar="ACTION", required=True)
    de.add_parser("list", help="list recorded decisions").set_defaults(func=cmd_decisions)
    rem = de.add_parser("remove", help="remove one decision by id")
    rem.add_argument("id", metavar="ID", help="decision id (from 'djtool decisions list')")
    rem.set_defaults(func=cmd_decisions)
    de.add_parser("clear", help="remove all recorded decisions").set_defaults(func=cmd_decisions)

    sp = add("sync", "Synchronize with a remote DJ root via rsync over SSH", cmd_sync)
    syn = sp.add_subparsers(dest="sync_action", metavar="ACTION", required=True)
    syn.add_parser("status", help="dry-run comparison of push and pull").set_defaults(func=cmd_sync)
    push = syn.add_parser("push", help="local machine is authoritative: local -> remote")
    push.add_argument("-n", "--dry-run", action="store_true", help="show what would change")
    push.add_argument("--delete", action="store_true", help="delete remote files that no longer exist locally")
    push.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    push.set_defaults(func=cmd_sync)
    pull = syn.add_parser("pull", help="remote machine is authoritative: remote -> local")
    pull.add_argument("-n", "--dry-run", action="store_true", help="show what would change")
    pull.add_argument("--delete", action="store_true", help="delete local files that no longer exist remotely")
    pull.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    pull.set_defaults(func=cmd_sync)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(color=False if getattr(args, "no_color", False) else None)
    root = resolve_root(getattr(args, "root", None))
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return int(args.func(args, console, root))
    except (DjToolError, ConfigError) as e:
        console.error(str(e))
        return 2
    except KeyboardInterrupt:
        console.out()
        console.info("interrupted — nothing was changed")
        return 130
    except OSError as e:
        console.error(str(e))
        return 1


