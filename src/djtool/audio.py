"""Audio metadata reading: mutagen with ffprobe fallback, format descriptions."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    import mutagen  # noqa: F401
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE

    HAVE_MUTAGEN = True
except ImportError:  # pragma: no cover - environment-dependent
    HAVE_MUTAGEN = False
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
AUDIO_LABELS = {
    ".flac": "FLAC",
    ".mp3": "MP3",
    ".m4a": "M4A",
    ".aac": "AAC",
    ".ogg": "OGG",
    ".opus": "Opus",
    ".wav": "WAV",
}
# Formats mutagen can read directly (.aac is not one of them).
MUTAGEN_READABLE = AUDIO_EXTS - {".aac"}
def _tag_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        if not v:
            return None
        v = v[0]
        if isinstance(v, (list, tuple)):  # MP4 trkn -> (number, total)
            v = v[0]
    s = str(v).strip()
    return s or None


def _first(tags: Any, *keys: str) -> str | None:
    for key in keys:
        try:
            v = tags.get(key)
        except Exception:  # noqa: BLE001, S112 - tag objects vary unpredictably per format
            continue
        s = _tag_str(v)
        if s:
            return s
    return None


def read_tags(path: Path) -> tuple[dict[str, Any], Any]:
    """Read tags + info via mutagen. Returns ({...}, info) — empty/None on failure.

    Keys: title, artist, album, track_no, duration. Never raises.
    """
    out: dict[str, Any] = {"title": None, "artist": None, "album": None, "track_no": None, "duration": None}
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            f = FLAC(str(path))
        elif ext == ".mp3":
            f = MP3(str(path))
        elif ext == ".m4a":
            f = MP4(str(path))
        elif ext == ".opus":
            f = OggOpus(str(path))
        elif ext == ".ogg":
            f = OggVorbis(str(path))
        elif ext == ".wav":
            f = WAVE(str(path))
        else:
            return out, None
        info = f.info
    except Exception:  # noqa: BLE001 - unreadable tags must never raise
        return out, None
    tags = getattr(f, "tags", None) or {}
    out["title"] = _first(tags, "title", "TIT2", "\xa9nam")
    out["artist"] = _first(tags, "artist", "TPE1", "\xa9ART")
    out["album"] = _first(tags, "album", "TALB", "\xa9alb")
    out["track_no"] = _first(tags, "tracknumber", "TRCK", "trkn")
    length = getattr(info, "length", None)
    if length:
        out["duration"] = float(length)
    return out, info


def ffprobe_info(path: Path) -> dict[str, Any]:
    """Best-effort audio properties via ffprobe (duration, sample rate, bits)."""
    exe = shutil.which("ffprobe")
    if exe is None:
        return {}
    try:
        p = subprocess.run(
            [
                exe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "format=duration:stream=sample_rate,bits_per_sample,codec_name",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if p.returncode != 0:
        return {}
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return {}
    out: dict[str, Any] = {}
    fmt = data.get("format", {}) or {}
    if fmt.get("duration"):
        out["duration"] = float(fmt["duration"])
    streams = data.get("streams") or []
    if streams:
        st = streams[0]
        if st.get("sample_rate"):
            out["sample_rate"] = int(st["sample_rate"])
        if st.get("bits_per_sample"):
            out["bits"] = int(st["bits_per_sample"])
        if st.get("codec_name"):
            out["codec"] = st["codec_name"]
    return out


def describe_format(path: Path, info: Any, probe: dict[str, Any] | None = None) -> str:
    """Human-readable format description: 'FLAC 44.1 kHz / 16 bit'."""
    probe = probe or {}
    ext = path.suffix.lower()
    label = AUDIO_LABELS.get(ext, (ext.lstrip(".") or "AUDIO").upper())
    sr = getattr(info, "sample_rate", None) if info else probe.get("sample_rate")
    bits = getattr(info, "bits_per_sample", None) if info else probe.get("bits")
    kbps = getattr(info, "bitrate", None) if info else None
    if sr and bits:
        return f"{label} {sr / 1000:.1f} kHz / {bits} bit"
    if sr and kbps:
        return f"{label} {sr / 1000:.1f} kHz / {kbps // 1000} kbps"
    if sr:
        return f"{label} {sr / 1000:.1f} kHz"
    if kbps:
        return f"{label} {kbps // 1000} kbps"
    return label


