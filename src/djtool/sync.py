"""rsync synchronization with a remote DJ root over SSH."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from djtool.config import SyncConfig, load_sync_config
from djtool.console import Console
from djtool.quarantine import TRASH_DIR_NAME
from djtool.state import CACHE_FILE_NAME

SYNC_EXCLUDES = [
    TRASH_DIR_NAME,
    CACHE_FILE_NAME,
    ".venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "*.egg-info",
]
def build_rsync_cmd(src: str, dst: str, dry_run: bool, delete: bool) -> list[str]:
    """Construct the rsync argument list (never shell=True)."""
    cmd = ["rsync", "-a", "-e", "ssh", "--partial"]
    if dry_run:
        cmd.append("-n")
    if delete:
        cmd.append("--delete")
    for pattern in SYNC_EXCLUDES:
        cmd += ["--exclude", pattern]
    cmd += [src, dst]
    return cmd


def plan_sync(root: Path, cfg: SyncConfig, direction: str) -> list[tuple[str, str, str]]:
    """Return [(label, src, dst), ...] for push or pull.

    push:  local machine is authoritative   (local -> remote)
    pull:  remote machine is authoritative  (remote -> local)
    The remote is whatever host you configure in [sync] (IP or DNS name).
    """
    plans: list[tuple[str, str, str]] = []
    if direction == "push":
        plans.append(("DJ", str(root) + "/", f"{cfg.remote}:{cfg.remote_dj}/"))
        if cfg.local_mixxx and cfg.remote_mixxx:
            plans.append((
                "Mixxx",
                cfg.local_mixxx.rstrip("/") + "/",
                f"{cfg.remote}:{cfg.remote_mixxx.rstrip('/')}/",
            ))
    else:
        plans.append(("DJ", f"{cfg.remote}:{cfg.remote_dj}/", str(root) + "/"))
        if cfg.local_mixxx and cfg.remote_mixxx:
            plans.append((
                "Mixxx",
                f"{cfg.remote}:{cfg.remote_mixxx.rstrip('/')}/",
                cfg.local_mixxx.rstrip("/") + "/",
            ))
    return plans


def rsync_dry_list(cmd: list[str]) -> tuple[int, list[str]]:
    """Run a dry-run rsync and return (count, sample of changed file names)."""
    # cmd ends with [src, dst]; keep options before the path arguments
    probe_cmd = cmd[:-2] + ["--out-format=%n"] + cmd[-2:]
    try:
        p = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 0, []
    names = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    return len(names), names[:25]


def requires_confirmation(dry_run: bool, yes: bool) -> bool:
    return not dry_run and not yes


def mixxx_running_local() -> bool:
    try:
        p = subprocess.run(["pgrep", "-x", "mixxx"], capture_output=True, check=False)
        return p.returncode == 0
    except OSError:
        return False


def mixxx_running_remote(cfg: SyncConfig) -> bool | None:
    """Best-effort remote check. None when the remote state cannot be determined."""
    if shutil.which("ssh") is None:
        return None
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", cfg.remote,
             "pgrep -x mixxx >/dev/null 2>&1 && echo RUNNING || echo NOT"],
            capture_output=True, text=True, timeout=25, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = p.stdout.strip()
    if out == "RUNNING":
        return True
    if out == "NOT":
        return False
    return None


def mixxx_guard(cfg: SyncConfig, console: Console) -> str | None:
    """Error string if Mixxx must not be synced; warns about unknown remote state."""
    if mixxx_running_local():
        return "Mixxx appears to be running locally — close it before syncing Mixxx settings"
    state = mixxx_running_remote(cfg)
    if state is True:
        return "Mixxx appears to be running on the remote host — close it before syncing Mixxx settings"
    if state is None:
        console.warn("could not verify Mixxx state on the remote host — make sure Mixxx is closed there")
    return None


def cmd_sync(args: argparse.Namespace, console: Console, root: Path) -> int:
    cfg, err = load_sync_config(root)
    if cfg is None:
        console.error(err or "sync not configured — add a [sync] section to djtool.toml")
        return 2

    if args.sync_action == "status":
        console.out("Comparing both directions (dry-run — nothing is changed)…")
        console.out()
        for direction in ("push", "pull"):
            arrow = "local → remote" if direction == "push" else "remote → local"
            console.out(console.bold(direction.upper()) + "  " + arrow)
            for label, src, dst in plan_sync(root, cfg, direction):
                n, sample = rsync_dry_list(build_rsync_cmd(src, dst, dry_run=True, delete=False))
                console.out(f"  {label:<8}{src}")
                console.out(f"  {'':<8}→ {dst}")
                console.out(f"  {'':<8}{n} file(s) would be transferred")
                for name in sample:
                    console.out(f"  {'':<8}{name}")
                console.out()
        return 0

    direction = args.sync_action  # push | pull
    plans = plan_sync(root, cfg, direction)
    arrow = "local → remote" if direction == "push" else "remote → local"
    console.out(console.bold(f">>> {direction.upper()}: {arrow} <<<"))
    for label, src, dst in plans:
        console.out(f"  {label}: {src}  →  {dst}")

    if any(label == "Mixxx" for label, _, _ in plans):
        err = mixxx_guard(cfg, console)
        if err:
            console.error(err)
            return 2

    if requires_confirmation(args.dry_run, args.yes):
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            console.info("aborted — nothing was changed")
            return 0

    for label, src, dst in plans:
        cmd = build_rsync_cmd(src, dst, dry_run=args.dry_run, delete=args.delete)
        if args.dry_run:
            console.out(console.dim(f"dry-run: {' '.join(cmd)}"))
        r = subprocess.run(cmd, check=False)
        if r.returncode != 0:
            console.error(f"rsync failed for {label} (exit {r.returncode})")
            return 1
    console.info(f"sync {direction} complete" + (" (dry-run)" if args.dry_run else ""))
    return 0

