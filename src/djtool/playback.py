"""Playback via ffplay (subprocess — no in-Python audio)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from djtool.console import Console


def play_audio(path: Path, console: Console, label: str) -> None:
    if shutil.which("ffplay") is None:
        console.error("ffplay not found — cannot play audio")
        return
    console.info(f"Playing {label}: {path.name}")
    try:
        subprocess.run(["ffplay", "-autoexit", "-loglevel", "error", str(path)], check=False)
    except KeyboardInterrupt:
        pass

