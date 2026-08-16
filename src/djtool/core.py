"""djtool — manage a DJ music collection.

Filesystem model
----------------
    DJ/                        location configured in djtool.toml ([collection] root)
    ├── Library/             read-only Beets mirror — NEVER modified by djtool
    ├── Tracks/              canonical DJ tracks — flat, 'Title - Artist [Version].ext'
    ├── Incoming/            staging area, reviewed before promotion
    └── .Trash/YYYY-MM-DD/   quarantine (flattened; hidden folder Mixxx never scans)

Duplicate-detection pipeline (progressively more expensive)
------------------------------------------------------------
    1. exact duplicates  — whole-file SHA-256, computed only for files whose
                           size is shared by at least one other file
    2. candidate pairs   — files grouped ("blocked") by normalized
                           artist/title, then scored with rapidfuzz
    3. Chromaprint       — lazy fpcalc fingerprints as *evidence*, never an
                           unquestionable deletion rule; ambiguous cases are
                           shown to the human

Safety rules
------------
    * Never modify, move, retag or delete anything under Library/.
    * Interactive removal only quarantines files into .Trash/YYYY-MM-DD/.
    * The cache stores derived data only; deleting it only slows the next scan.
    * No network is required for duplicate detection.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
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

try:
    from rapidfuzz import fuzz

    HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - environment-dependent
    HAVE_RAPIDFUZZ = False

try:
    import tomllib

    HAVE_TOML = True
except ImportError:  # pragma: no cover - Python < 3.11
    HAVE_TOML = False

__version__ = "0.1.0"

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Supported formats. Extend this set to support more formats.
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

TRASH_DIR_NAME = ".Trash"
CACHE_FILE_NAME = ".djtool-cache.json"
CONFIG_FILE_NAME = "djtool.toml"
CACHE_VERSION = 2

# rsync excludes: collection state plus Python/venv artifacts that live inside
# the djtool/ project folder (which is under the DJ root and gets synced too).
SYNC_EXCLUDES = [
    TRASH_DIR_NAME,
    CACHE_FILE_NAME,
    ".venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "*.egg-info",
]

SOURCES = ("library", "tracks", "incoming")
SOURCE_DIRS = {"library": "Library", "tracks": "Tracks", "incoming": "Incoming"}
SOURCE_RANK = {"library": 0, "tracks": 1, "incoming": 2}

CATEGORIES = (
    "EXACT_DUPLICATE",
    "VERY_LIKELY_SAME_RECORDING",
    "POSSIBLE_SAME_RECORDING",
    "POSSIBLE_ALTERNATE_VERSION",
)

# Fingerprint similarity thresholds. Tuned conservatively: anything ambiguous
# is presented to the user instead of being auto-classified with confidence.
FP_SAME = 0.90   # >= this: "very likely same recording"
FP_MED = 0.65    # >= this: "possibly same recording"
TITLE_CANDIDATE = 0.72   # minimum core-title similarity to form a candidate pair
TITLE_STRONG = 0.90      # title similarity considered "same title"
DUR_TOLERANCE_S = 2.0    # absolute duration tolerance (seconds)
DUR_TOLERANCE_FRAC = 0.05  # ...plus 5% of the longer duration

# Words that usually indicate an *alternate version* rather than a different
# recording. They are down-weighted when grouping/blocking and matching, but
# preserved and displayed during review because they can mean genuinely
# different versions.
VERSION_TERMS = [
    "album version",
    "radio edit",
    "radio mix",
    "single version",
    "original mix",
    "extended mix",
    "extended edit",
    "club mix",
    "dub mix",
    "lp version",
    "single edit",
    "remaster",
    "remastered",
    "reissue",
    "re-recorded",
    "re-recording",
    "rework",
    "remix",
    "clean",
    "explicit",
    "live",
    "extended",
    "edit",
    "mix",
    "version",
    "acoustic",
    "unplugged",
    "instrumental",
    "karaoke",
    "reprise",
    "demo",
    "intro",
    "outro",
    "deluxe",
    "bonus",
    "radio",
    "orchestral",
    "12 inch",
    "7 inch",
]

_VERSION_RE = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in VERSION_TERMS) + r")\b")
_FEAT_RE = re.compile(r"\b(?:feat(?:uring)?(?:\.|\b)|ft(?:\.|\b))")

FPCALC = shutil.which("fpcalc")

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class DjToolError(Exception):
    """Base class for expected, user-facing errors."""


class ConfigError(DjToolError):
    """Bad or missing configuration."""


class ConfirmationRequired(DjToolError):
    """A destructive action needs explicit confirmation."""


class NameCollision(DjToolError):
    """Promotion would clash with an existing Tracks file and needs a decision.

    Raised only for non-interactive callers; the CLI resolves collisions
    interactively instead. Collisions are never resolved by appending numbers.
    """

    def __init__(self, track: Track, target: Path):
        self.track = track
        self.target = target
        super().__init__(
            f"'{track.rel}' would collide with existing '{target.name}' in Tracks/"
        )


# --------------------------------------------------------------------------
# Console (ANSI colors, usable without them)
# --------------------------------------------------------------------------


class Console:
    def __init__(self, color: bool | None = None):
        if color is None:
            color = (
                sys.stdout.isatty()
                and os.environ.get("NO_COLOR") is None
                and os.environ.get("TERM") != "dumb"
            )
        self.color = bool(color)

    def _style(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def bold(self, t: str) -> str:
        return self._style("1", t)

    def dim(self, t: str) -> str:
        return self._style("2", t)

    def red(self, t: str) -> str:
        return self._style("31", t)

    def green(self, t: str) -> str:
        return self._style("32", t)

    def yellow(self, t: str) -> str:
        return self._style("33", t)

    def cyan(self, t: str) -> str:
        return self._style("36", t)

    def magenta(self, t: str) -> str:
        return self._style("35", t)

    def out(self, text: str = "") -> None:
        print(text)

    def info(self, text: str) -> None:
        print(text)

    def warn(self, text: str) -> None:
        print(self.yellow("warning: ") + text)

    def error(self, text: str) -> None:
        print(self.red("error: ") + text, file=sys.stderr)


# --------------------------------------------------------------------------
# Text normalization (comparison only — actual tags/filenames are untouched)
# --------------------------------------------------------------------------


def normalize_text(s: str | None) -> str:
    """Normalize a string for comparison: unicode, case, punctuation, whitespace.

    Keeps letters, digits, whitespace and apostrophes. Ampersands become
    " and " (Simon & Garfunkel == Simon and Garfunkel).
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    for a, b in (
        ("\u2019", "'"), ("\u2018", "'"), ("\u201a", "'"), ("\u00b4", "'"),
        ("\u201c", '"'), ("\u201d", '"'),
        ("&", " and "),
    ):
        s = s.replace(a, b)
    s = re.sub(r"[^\w\s']", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def split_feat(s: str) -> tuple[str, str]:
    """Split 'Artist feat. Someone' -> ('Artist', 'Someone'). No marker -> (s, '')."""
    m = _FEAT_RE.search(s or "")
    if not m:
        return (s or "").strip(), ""
    return s[: m.start()].strip(), s[m.end():].strip(" .,()[]-–—")


def version_terms_present(normalized: str) -> set[str]:
    """Which version terms appear in a *normalized* string."""
    return set(_VERSION_RE.findall(normalized or ""))


def core_of(normalized: str) -> str:
    """Strip feat-clauses and version terms, leaving the 'core' of a title/artist."""
    s, _ = split_feat(normalized or "")
    s = _VERSION_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def guess_from_filename(stem: str) -> tuple[str | None, str | None]:
    """Guess (artist, title) from a filename stem. Heuristic, best-effort.

    Recognizes 'Artist - Title', '01 - Artist - Title', en/em dashes, and a
    leading numeric track prefix. Returns (None, None) when unsure.
    """
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", stem or "")]
    parts = [p for p in parts if p]
    if not parts:
        return None, None
    if re.fullmatch(r"\d{1,3}[._\-]?\d*", parts[0]):
        parts = parts[1:]
    if not parts:
        return None, None
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    # Three or more parts: 'Artist - Title - (Remainder)'
    return parts[0], " ".join(parts[1:])


def text_sim(a: str, b: str) -> float:
    """Fuzzy string similarity in [0, 1]. rapidfuzz when available, difflib otherwise."""
    if not a or not b:
        return 0.0
    if HAVE_RAPIDFUZZ:
        return max(fuzz.ratio(a, b) / 100.0, fuzz.token_sort_ratio(a, b) / 100.0)
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------
# Track filename scheme: 'Title - Artist [Version].ext'
# --------------------------------------------------------------------------

# Canonical spellings for common version labels (key = normalized phrase).
# A value of None means "drop this phrase" — 'original version' is the default
# state of a track, not a useful discriminator.
VERSION_CANON: dict[str, str | None] = {
    "clean version": "Clean",
    "clean": "Clean",
    "clean radio edit": "Clean Radio Edit",
    "explicit version": "Explicit",
    "explicit": "Explicit",
    "explicit radio edit": "Explicit Radio Edit",
    "radio edit": "Radio Edit",
    "radio mix": "Radio Mix",
    "radio version": "Radio Edit",
    "single edit": "Single Edit",
    "single version": "Single Version",
    "album version": "Album Version",
    "extended": "Extended",
    "extended mix": "Extended Mix",
    "extended edit": "Extended Edit",
    "extended intro": "Extended Intro",
    "extended club mix": "Extended Club Mix",
    "dj intro": "DJ Intro",
    "dj intro clean": "DJ Intro Clean",
    "dj edit": "DJ Edit",
    "instrumental": "Instrumental",
    "instrumental mix": "Instrumental",
    "acapella": "Acapella",
    "a capella": "Acapella",
    "a cappella": "Acapella",
    "live": "Live",
    "remix": "Remix",
    "remaster": "Remaster",
    "remastered": "Remaster",
    "remastered version": "Remaster",
    "original mix": "Original Mix",
    "original version": None,
    "club mix": "Club Mix",
    "dub mix": "Dub Mix",
    "rework": "Rework",
    "unplugged": "Unplugged",
    "acoustic": "Acoustic",
    "reprise": "Reprise",
    "demo": "Demo",
    "deluxe": "Deluxe",
    "bonus": "Bonus",
    "orchestral": "Orchestral",
    "12 inch": "12 Inch",
    "7 inch": "7 Inch",
}

# Words that mark a parenthesized/bracketed group as a *version* group rather
# than part of the title ("(Radio Edit)" vs. "('Til Monday)"). Word-boundary
# matched, so "(Cocktail Mixer)" is not mistaken for a version.
_VERSION_GROUP_MARKERS = (
    "12 inch", "7 inch", "acapella", "acoustic", "album", "bonus",
    "capella", "cappella", "clean", "club", "deluxe", "demo", "dj",
    "dub", "edit", "explicit", "extended", "inch", "instrumental",
    "intro", "karaoke", "live", "mix", "orchestral", "original", "radio",
    "reissue", "remaster", "remix", "reprise", "rework", "single",
    "unplugged", "version",
)
_VERSION_GROUP_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _VERSION_GROUP_MARKERS) + r")\b",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"[(\[][^()\[\]]*[)\]]")


def _canonical_phrases(s: str) -> list[str]:
    """Split a raw version string into canonical phrases, longest match first.

    Known phrases map onto the version vocabulary ('RADIO EDIT' -> 'Radio Edit'),
    unknown words are capitalized ('The Blessed Madonna Remix' keeps its name),
    and drop-marked phrases ('original version') are omitted.
    """
    words = normalize_text(s or "").split()
    out: list[str] = []
    i = 0
    while i < len(words):
        for n in range(min(4, len(words) - i), 0, -1):
            phrase = " ".join(words[i:i + n])
            if phrase in VERSION_CANON:
                canon = VERSION_CANON[phrase]
                if canon:
                    out.append(canon)
                i += n
                break
        else:
            w = words[i]
            out.append("DJ" if w == "dj" else w.capitalize())
            i += 1
    return out


def canonicalize_version(*parts: str) -> str:
    """Combine raw version fragments into one canonical label, deduplicated."""
    out: list[str] = []
    for p in parts:
        for phrase in _canonical_phrases(p):
            if phrase and phrase not in out:
                out.append(phrase)
    return " ".join(out)


def _extract_groups(t: str) -> tuple[list[str], str]:
    """Pull version-looking (…) / […] groups out of a title, leaving the rest."""
    groups: list[str] = []
    kept: list[str] = []
    pos = 0
    for m in _PAREN_RE.finditer(t):
        inner = m.group(0)[1:-1].strip()
        if inner and _VERSION_GROUP_RE.search(inner):
            groups.append(inner)
            kept.append(t[pos:m.start()])
            pos = m.end()
    kept.append(t[pos:])
    return groups, "".join(kept)


def extract_version(title: str) -> tuple[str, str]:
    """Split a title into (base title, version label).

    Recognizes parenthesized/bracketed groups that look like versions and a
    trailing version phrase: 'Song (Clean Radio Edit)' -> ('Song', 'Clean Radio Edit'),
    'Song - Radio Edit' -> ('Song', 'Radio Edit'), 'Song' -> ('Song', '').
    Groups that do not look like versions stay in the base title.
    """
    if not title:
        return "", ""
    t = title.strip()
    groups, t = _extract_groups(t)
    parts = re.split(r"\s+", t.strip())
    trailing: list[str] = []
    while True:
        for n in range(min(4, len(parts)), 0, -1):
            phrase = " ".join(parts[-n:])
            if normalize_text(phrase) in VERSION_CANON:
                trailing = parts[-n:] + trailing
                parts = parts[:-n]
                break
        else:
            break
    base = re.sub(r"[\s\-–—]+$", "", " ".join(parts)).strip()
    version = canonicalize_version(*groups, *trailing)
    return base, version


_FILENAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename_part(s: str, fallback: str = "") -> str:
    """Make a string safe to use as one filename component (best-effort)."""
    s = _FILENAME_BAD.sub("", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or fallback


def simplify_artist(artist: str) -> str:
    """Normalize messy multi-artist credits for filenames, never for tags.

    'A, B, A feat. B' -> 'A, B' (deduplicated, order preserved); overlong
    credits are truncated.
    """
    parts = [p.strip() for p in re.split(r"\s*,\s*", artist or "") if p.strip()]
    seen: list[str] = []
    for p in parts:
        if normalize_text(p) not in {normalize_text(s) for s in seen}:
            seen.append(p)
    out = ", ".join(seen) if len(seen) > 1 else (seen[0] if seen else (artist or "").strip())
    if len(out) > 80:
        out = out[:77].rstrip() + "…"
    return out


def _stem_base(stem: str) -> str:
    """Strip a trailing '[Version]' group from a filename stem."""
    return re.sub(r"\s*\[[^\[\]]*\]\s*$", "", stem or "").strip()


def derive_track_name(track: Track) -> str:
    """Derive the flat Tracks/ filename: 'Title - Artist [Version].ext'.

    Uses tags when present, otherwise best-effort filename parsing. When
    nothing can be derived the original filename is kept (still flattened).
    """
    ext = track.path.suffix.lower()
    title = (track.title or track.filename_title or "").strip()
    artist = (track.artist or track.filename_artist or "").strip()
    if not title and not artist:
        return track.path.name
    base_title, version = extract_version(title)
    parts: list[str] = []
    if base_title:
        parts.append(sanitize_filename_part(base_title))
    if artist:
        artist = simplify_artist(artist)
        if artist:
            parts.append(sanitize_filename_part(artist))
    base = " - ".join(parts)
    if version:
        base = f"{base} [{version}]"
    base = sanitize_filename_part(base)
    return f"{base}{ext}" if base else track.path.name


# --------------------------------------------------------------------------
# Track model
# --------------------------------------------------------------------------


@dataclass
class Track:
    path: Path
    rel: str  # path relative to DJ root, posix separators
    source: str  # one of SOURCES
    size: int
    mtime_ns: int
    sha256: str | None = None
    duration: float | None = None  # seconds
    title: str = ""
    artist: str = ""
    album: str = ""
    track_no: str = ""
    format_desc: str = ""
    fingerprint: str | None = None  # raw chromaprint (fpcalc -raw output)
    fp_duration: float | None = None

    # --- derived views (computed on demand, cached) -----------------------
    @cached_property
    def filename_artist(self) -> str:
        artist, _ = guess_from_filename(self.path.stem)
        return artist or ""

    @cached_property
    def filename_title(self) -> str:
        _, title = guess_from_filename(self.path.stem)
        return title or ""

    @cached_property
    def norm_title(self) -> str:
        return normalize_text(self.title or self.filename_title)

    @cached_property
    def norm_artist(self) -> str:
        return normalize_text(self.artist or self.filename_artist)

    @cached_property
    def core_title(self) -> str:
        s = core_of(self.norm_title)
        return s or self.norm_title

    @cached_property
    def core_artist(self) -> str:
        s = core_of(self.norm_artist)
        return s or self.norm_artist

    @cached_property
    def block_key(self) -> str:
        """Grouping key: normalized artist+title core. Empty when unknown."""
        return "|".join(p for p in (self.core_artist, self.core_title) if p)

    def rel_in_source(self) -> str:
        prefix = SOURCE_DIRS[self.source] + "/"
        return self.rel.removeprefix(prefix)


def display_artist(t: Track) -> str:
    if t.artist:
        return t.artist
    if t.filename_artist:
        return f"{t.filename_artist} (from filename)"
    return "(unknown)"


def display_title(t: Track) -> str:
    if t.title:
        return t.title
    if t.filename_title:
        return f"{t.filename_title} (from filename)"
    return "(unknown)"


# --------------------------------------------------------------------------
# Audio metadata: mutagen with ffprobe fallback
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Project state directory (config + cache live with the tool, not the library)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Collection scan
# --------------------------------------------------------------------------


@dataclass
class ScanStats:
    counts: dict[str, int] = field(default_factory=lambda: {s: 0 for s in SOURCES})
    cached: int = 0
    new: int = 0
    stale: int = 0
    ignored_non_audio: int = 0
    warnings: list[str] = field(default_factory=list)


def _track_from_cache(path: Path, rel: str, source: str, st: os.stat_result, entry: dict) -> Track:
    t = Track(path=path, rel=rel, source=source, size=st.st_size, mtime_ns=st.st_mtime_ns)
    t.sha256 = entry.get("sha256")
    t.duration = entry.get("duration")
    t.title = entry.get("title") or ""
    t.artist = entry.get("artist") or ""
    t.album = entry.get("album") or ""
    t.track_no = entry.get("track_no") or ""
    t.format_desc = entry.get("format_desc") or ""
    t.fingerprint = entry.get("fingerprint")
    t.fp_duration = entry.get("fp_duration")
    return t


def _track_from_disk(path: Path, rel: str, source: str, st: os.stat_result, stats: ScanStats) -> Track:
    t = Track(path=path, rel=rel, source=source, size=st.st_size, mtime_ns=st.st_mtime_ns)
    if HAVE_MUTAGEN:
        tags, info = read_tags(path)
        t.title = tags.get("title") or ""
        t.artist = tags.get("artist") or ""
        t.album = tags.get("album") or ""
        t.track_no = tags.get("track_no") or ""
        t.duration = tags.get("duration")
        probe: dict[str, Any] = {}
        if t.duration is None or info is None:
            probe = ffprobe_info(path)
            if t.duration is None:
                t.duration = probe.get("duration")
        if info is None and not probe and path.suffix.lower() in MUTAGEN_READABLE:
            stats.warnings.append(f"could not read audio info: {rel}")
        t.format_desc = describe_format(path, info, probe)
    else:
        # Without mutagen: duration via ffprobe only, no tags.
        probe = ffprobe_info(path)
        t.duration = probe.get("duration")
        t.format_desc = describe_format(path, None, probe)
    return t


def _hash_size_groups(tracks: list[Track], stats: ScanStats) -> None:
    """Hash only files whose size is shared by at least one other file.

    Byte-identical files always have identical sizes, so this cannot miss an
    exact duplicate while avoiding hashing the entire library.
    """
    by_size: dict[int, list[Track]] = {}
    for t in tracks:
        by_size.setdefault(t.size, []).append(t)
    for group in by_size.values():
        if len(group) < 2:
            continue
        for t in group:
            if t.sha256 is None:
                try:
                    t.sha256 = sha256_file(t.path)
                except OSError as e:
                    stats.warnings.append(f"could not read {t.rel}: {e}")


def scan_collection(root: Path, use_cache: bool = True) -> tuple[list[Track], ScanStats]:
    """Walk Library/, Tracks/, Incoming/ and build Track objects.

    Uses .djtool-cache.json for derived data (tags, hashes, fingerprints),
    invalidating entries whose size or mtime changed.
    """
    entries = load_cache(root).get("entries", {}) if use_cache else {}
    tracks: list[Track] = []
    stats = ScanStats()
    for source in SOURCES:
        srcdir = root / SOURCE_DIRS[source]
        if not srcdir.is_dir():
            continue
        for path in sorted(srcdir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(part.startswith(".") for part in rel.split("/")):
                continue  # hidden files/dirs are not collection content
            if path.suffix.lower() not in AUDIO_EXTS:
                stats.ignored_non_audio += 1
                continue
            st = path.stat()
            entry = entries.get(rel)
            if cache_valid(entry, st.st_size, st.st_mtime_ns):
                stats.cached += 1
                tracks.append(_track_from_cache(path, rel, source, st, entry))
            else:
                stats.new += 1
                if entry is not None:
                    stats.stale += 1
                tracks.append(_track_from_disk(path, rel, source, st, stats))
            stats.counts[source] += 1
    _hash_size_groups(tracks, stats)
    return tracks, stats


# --------------------------------------------------------------------------
# Chromaprint (fpcalc) fingerprints
# --------------------------------------------------------------------------

MAX_FP_OFFSET = 32  # frames; ~0.123s each, covers small offsets between recordings


def _fp_decode(fp: str) -> list[int]:
    """Decode a stored fingerprint into 32-bit chromaprint words.

    Accepts the raw fpcalc -raw format (comma-separated decimal words, which
    compute_fingerprint stores) and base64-encoded big-endian words (legacy
    cache entries / test fixtures). Returns [] on corrupt input.
    """
    s = (fp or "").strip()
    if not s:
        return []
    if "," in s:
        # raw fpcalc -raw output: "3560557156,3560627184,..."
        out: list[int] = []
        for part in s.split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return out
    try:
        data = base64.b64decode(s)
    except Exception:  # noqa: BLE001 - corrupt cached fingerprints are skipped
        return []
    words = len(data) // 4
    return [int.from_bytes(data[i * 4:i * 4 + 4], "big") for i in range(words)]


def fp_similarity(a: str | None, b: str | None) -> float | None:
    """Similarity in [0,1] between two raw chromaprint fingerprints.

    Tries small alignments and reports the best matching-bit fraction.
    Returns None when either fingerprint is unavailable.
    """
    if not a or not b:
        return None
    fa, fb = _fp_decode(a), _fp_decode(b)
    if not fa or not fb:
        return None
    if len(fa) > len(fb):
        fa, fb = fb, fa
    best = 0.0
    max_off = min(len(fb) - len(fa), MAX_FP_OFFSET)
    total_bits = 32.0 * len(fb)
    for off in range(max_off + 1):
        mismatch = sum((x ^ y).bit_count() for x, y in zip(fa, fb[off:]))
        score = 1.0 - mismatch / total_bits
        best = max(best, score)
    return best


def compute_fingerprint(path: Path) -> tuple[str, float] | None:
    """Run fpcalc, return (raw fingerprint, duration) or None."""
    if FPCALC is None:
        return None
    try:
        p = subprocess.run(
            [FPCALC, "-raw", str(path)],
            capture_output=True, text=True, timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    fp: str | None = None
    dur: float | None = None
    for line in p.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "FINGERPRINT":
            fp = value.strip() or None
        elif key == "DURATION":
            try:
                dur = float(value.strip())
            except ValueError:
                pass
    return (fp, dur) if fp else None


def ensure_fingerprints(pair: Pair) -> None:
    """Compute fingerprints for a pair (lazily, cached) and re-classify."""
    if pair.category == "EXACT_DUPLICATE" or FPCALC is None:
        return
    for t in (pair.a, pair.b):
        if t.fingerprint is None:
            res = compute_fingerprint(t.path)
            if res:
                t.fingerprint, t.fp_duration = res
                if t.duration is None and t.fp_duration:
                    t.duration = t.fp_duration
    pair.fp_sim = fp_similarity(pair.a.fingerprint, pair.b.fingerprint)
    pair.category, pair.note = classify(pair)


# --------------------------------------------------------------------------
# Candidate generation & classification
# --------------------------------------------------------------------------


@dataclass
class Pair:
    a: Track  # preferred member (Library first, then Tracks, then Incoming)
    b: Track
    category: str = ""
    title_sim: float = 0.0
    artist_sim: float = 0.0
    duration_diff: float | None = None
    dur_close: bool | None = None
    fp_sim: float | None = None
    note: str = ""


def order_pair(a: Track, b: Track) -> tuple[Track, Track]:
    """Put the preferred member first: Library > Tracks > Incoming."""
    if SOURCE_RANK[a.source] < SOURCE_RANK[b.source]:
        return a, b
    if SOURCE_RANK[b.source] < SOURCE_RANK[a.source]:
        return b, a
    return (a, b) if a.path < b.path else (b, a)


def make_pair(a: Track, b: Track, exact: bool = False) -> Pair:
    a, b = order_pair(a, b)
    title_sim = text_sim(a.core_title, b.core_title) if (a.core_title and b.core_title) else 0.0
    artist_sim = text_sim(a.core_artist, b.core_artist) if (a.core_artist and b.core_artist) else 0.0
    duration_diff: float | None = None
    dur_close: bool | None = None
    if a.duration is not None and b.duration is not None:
        duration_diff = abs(a.duration - b.duration)
        dur_close = duration_diff <= max(DUR_TOLERANCE_S, DUR_TOLERANCE_FRAC * max(a.duration, b.duration))
    p = Pair(a=a, b=b, title_sim=title_sim, artist_sim=artist_sim,
             duration_diff=duration_diff, dur_close=dur_close)
    if exact:
        p.category, p.note = "EXACT_DUPLICATE", "byte-identical files"
    else:
        p.category, p.note = classify(p)
    return p


def _note(notes: list[str], default: str) -> str:
    return "; ".join(notes) if notes else default


def classify(p: Pair) -> tuple[str, str]:
    """Conservative classification. Fingerprints are evidence, not a deletion rule.

    Without a fingerprint the classification never rises above "possible".
    Ambiguous cases (alternate versions, low fingerprint agreement) stay in
    categories that force human review.
    """
    a, b = p.a, p.b
    if a.sha256 and a.sha256 == b.sha256:
        return "EXACT_DUPLICATE", "byte-identical files"

    same_title = p.title_sim >= TITLE_STRONG
    va, vb = version_terms_present(a.norm_title), version_terms_present(b.norm_title)
    terms_differ = va != vb
    notes: list[str] = []
    if terms_differ:
        if vb - va:
            notes.append("B extra version term(s): " + ", ".join(sorted(vb - va)))
        if va - vb:
            notes.append("A extra version term(s): " + ", ".join(sorted(va - vb)))

    fp = p.fp_sim
    if fp is None:
        if not FPCALC:
            notes.append("no fpcalc installed — metadata-only classification")
        if same_title:
            if terms_differ and p.dur_close is False:
                return "POSSIBLE_ALTERNATE_VERSION", _note(
                    notes, "title matches but duration and version terms differ"
                )
            return "POSSIBLE_SAME_RECORDING", _note(notes, "matching title/artist metadata")
        return "POSSIBLE_SAME_RECORDING", _note(notes, "weak metadata match")

    if fp >= FP_SAME:
        if not same_title:
            notes.append("fingerprints agree but titles differ")
        return "VERY_LIKELY_SAME_RECORDING", _note(notes, "fingerprints agree")
    if fp >= FP_MED:
        if same_title and terms_differ:
            return "POSSIBLE_ALTERNATE_VERSION", _note(notes, "fingerprints partially agree")
        return "POSSIBLE_SAME_RECORDING", _note(notes, "fingerprints partially agree")
    if same_title:
        return "POSSIBLE_ALTERNATE_VERSION", _note(notes, "same core title, fingerprints differ")
    return "POSSIBLE_SAME_RECORDING", _note(notes, "weak fingerprint match")


def is_candidate(p: Pair) -> bool:
    """Whether a pair is worth showing for review (recall-friendly threshold)."""
    return (
        p.title_sim >= TITLE_CANDIDATE
        or (p.artist_sim >= 0.9 and p.title_sim >= 0.6)
        or (p.dur_close and p.title_sim >= 0.6 and p.artist_sim >= 0.6)
    )


def find_candidates(tracks: list[Track]) -> list[Pair]:
    """Generate pairs for review: exact duplicates first, then fuzzy candidates.

    Avoids quadratic comparison by grouping on hash (exact) and on the
    normalized artist+title block key (fuzzy).
    """
    pairs: list[Pair] = []
    seen: set[frozenset[str]] = set()

    # 1. exact duplicates — global, via shared hashes
    by_hash: dict[str, list[Track]] = {}
    for t in tracks:
        if t.sha256:
            by_hash.setdefault(t.sha256, []).append(t)
    for group in by_hash.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                p = make_pair(group[i], group[j], exact=True)
                pairs.append(p)
                seen.add(frozenset((p.a.rel, p.b.rel)))

    # 2. fuzzy candidates — within artist/title blocks
    blocks: dict[str, list[Track]] = {}
    for t in tracks:
        if t.block_key:
            blocks.setdefault(t.block_key, []).append(t)
    for group in blocks.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if frozenset((group[i].rel, group[j].rel)) in seen:
                    continue
                p = make_pair(group[i], group[j])
                if is_candidate(p):
                    pairs.append(p)

    severity = {name: i for i, name in enumerate(CATEGORIES)}
    pairs.sort(key=lambda p: (
        severity.get(p.category, 99),
        0 if "library" in (p.a.source, p.b.source) else 1,
    ))
    return pairs


# --------------------------------------------------------------------------
# Quarantine (.Trash) — the only way files are "removed" interactively
# --------------------------------------------------------------------------


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


def promote_to_tracks(
    root: Path,
    track: Track,
    *,
    get_input: Callable[[str], str] | None = None,
    console: Console | None = None,
) -> tuple[Path, str]:
    """Move an Incoming file flat into Tracks/ as 'Title - Artist [Version].ext'.

    The Incoming/ folder structure is discarded — only the file itself moves,
    renamed from its tags (filename parsing as fallback). Name collisions are
    never auto-resolved with numbers: interactive resolution is required, or a
    NameCollision is raised for non-interactive callers (get_input=None).

    Returns (destination, action) with action in
    {'promoted', 'renamed', 'renamed-existing', 'renamed-both', 'skipped'}.
    """
    if track.source != "incoming":
        raise ValueError("only Incoming files can be promoted to Tracks")
    root = root.resolve()
    src = track.path.resolve()
    _ensure_within(src, root, "source")
    dest = root / SOURCE_DIRS["tracks"] / derive_track_name(track)
    if dest.exists():
        if get_input is None:
            raise NameCollision(track, dest)
        return resolve_name_collision(
            track, dest, console or Console(color=False), get_input
        )
    shutil.move(str(src), str(dest))
    return dest, "promoted"


def _ask_version(
    console: Console,
    get_input: Callable[[str], str],
    what: str,
) -> str | None:
    """Prompt for a canonical version label. Returns None when cancelled."""
    while True:
        raw = (
            get_input(f"Version for {what} (e.g. 'Radio Edit'; '.' to cancel): ") or ""
        ).strip()
        if raw in (".", "q"):
            return None
        if raw.lower() in ("none", "no version", "-", "n/a"):
            console.warn("a version label is required to distinguish the files")
            continue
        version = canonicalize_version(raw)
        if version:
            return version
        console.warn(
            "a version label is needed (e.g. 'Clean', 'Radio Edit', 'The Blessed Madonna Remix')"
        )


def _file_summary(path: Path) -> dict[str, str]:
    """Best-effort (artist, title, duration) for a path, for display only."""
    out = {"artist": "", "title": "", "duration": "unknown"}
    if HAVE_MUTAGEN:
        tags, _info = read_tags(path)
        if tags.get("artist"):
            out["artist"] = tags["artist"]
        if tags.get("title"):
            out["title"] = tags["title"]
        if tags.get("duration"):
            out["duration"] = fmt_duration(tags["duration"])
    fa, ft = guess_from_filename(path.stem)
    if not out["artist"] and fa:
        out["artist"] = f"{fa} (from filename)"
    if not out["title"] and ft:
        out["title"] = f"{ft} (from filename)"
    out["artist"] = out["artist"] or "(unknown)"
    out["title"] = out["title"] or "(unknown)"
    return out


def _show_collision(console: Console, track: Track, target: Path) -> None:
    existing = _file_summary(target)
    console.out()
    console.out(console.bold(f"Target filename already exists: {target.name}"))
    console.out()
    console.out(console.bold("Existing (Tracks/):"))
    console.out(f"  Artist:    {existing['artist']}")
    console.out(f"  Title:     {existing['title']}")
    console.out(f"  Duration:  {existing['duration']}")
    console.out()
    console.out(console.bold(f"Incoming ({track.rel}):"))
    console.out(f"  Artist:    {display_artist(track)}")
    console.out(f"  Title:     {display_title(track)}")
    console.out(f"  Duration:  {fmt_duration(track.duration)}")
    console.out(f"  Fingerprint: {'yes' if track.fingerprint else 'no'}")
    console.out()
    console.out("These are likely different versions of the same track — both can")
    console.out("be kept, but they need distinct names (never auto-numbered).")
    console.out("[v] Version the incoming file    [e] Version the existing file")
    console.out("[b] Version both                 [s] Skip (leave in Incoming/)")


def resolve_name_collision(
    track: Track,
    target: Path,
    console: Console,
    get_input: Callable[[str], str],
) -> tuple[Path, str]:
    """Interactively resolve a promotion collision; never appends numbers.

    'v' versions the incoming file, 'e' versions the existing Tracks file,
    'b' versions both, 's' leaves the incoming file in Incoming/.
    Returns (destination, action).
    """
    tracks_dir = target.parent
    ext = target.suffix
    src = track.path.resolve()
    base = _stem_base(target.stem)
    while True:
        _show_collision(console, track, target)
        choice = (get_input("Choice: ") or "").strip().lower()
        if choice in ("s", "q", "."):
            return target, "skipped"
        if choice == "v":
            version = _ask_version(console, get_input, "the incoming file")
            if version is None:
                continue
            dest = tracks_dir / f"{base} [{version}]{ext}"
            if dest.exists():
                console.warn(f"'{dest.name}' already exists — pick a different version")
                continue
            shutil.move(str(src), str(dest))
            return dest, "renamed"
        if choice in ("e", "b"):
            v_existing = _ask_version(console, get_input, "the existing file")
            if v_existing is None:
                continue
            new_existing = tracks_dir / f"{base} [{v_existing}]{ext}"
            if new_existing.exists():
                console.warn(f"'{new_existing.name}' already exists — pick a different version")
                continue
            if choice == "e":
                shutil.move(str(target), str(new_existing))
                shutil.move(str(src), str(target))
                return target, "renamed-existing"
            v_incoming = _ask_version(console, get_input, "the incoming file")
            if v_incoming is None:
                continue
            if v_incoming == v_existing:
                console.warn("both files need distinct version labels to coexist")
                continue
            dest = tracks_dir / f"{base} [{v_incoming}]{ext}"
            if dest.exists():
                console.warn(f"'{dest.name}' already exists — pick a different version")
                continue
            shutil.move(str(target), str(new_existing))
            shutil.move(str(src), str(dest))
            return dest, "renamed-both"
        console.out("choices: [v] version the incoming file  [e] version the existing file  "
                    "[b] version both  [s] skip (leave in Incoming/)")


def prune_empty_dirs(root: Path, source: str) -> int:
    """Remove now-empty subdirectories under <SourceDir>, bottom-up."""
    base = root / SOURCE_DIRS[source]
    if not base.is_dir():
        return 0
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(base, topdown=False):
        p = Path(dirpath)
        if p == base:
            continue
        try:
            p.rmdir()  # only succeeds when empty
            removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# Playback (ffplay via subprocess — no in-Python audio)
# --------------------------------------------------------------------------


def play_audio(path: Path, console: Console, label: str) -> None:
    if shutil.which("ffplay") is None:
        console.error("ffplay not found — cannot play audio")
        return
    console.info(f"Playing {label}: {path.name}")
    try:
        subprocess.run(["ffplay", "-autoexit", "-loglevel", "error", str(path)], check=False)
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------
# Review actions & interactive UI
# --------------------------------------------------------------------------


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


@dataclass
class ReviewStats:
    processed: int = 0
    quarantined: int = 0
    promoted: int = 0
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


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "unknown"
    total_tenths = max(0, round(sec * 10))
    minutes, rest = divmod(total_tenths, 600)
    seconds, tenths = divmod(rest, 10)
    return f"{minutes}:{seconds:02d}.{tenths}"


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TiB"


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
    elif FPCALC is None:
        console.out(f"{'Chromaprint similarity:':<26}  n/a (fpcalc not installed)")
    else:
        console.out(f"{'Chromaprint similarity:':<26}  n/a")
    if pair.note:
        console.out(console.dim("Note: " + pair.note))
    console.out()
    if pair.a.source == "library":
        keep = "Keep Library / remove B"
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
            if FPCALC and not fp_note_shown:
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
                res = action_keep_preferred(root, pair, mode)
                if res == "quarantined":
                    stats.quarantined += 1
                elif res == "refused":
                    console.warn("both files are in the read-only Library — keeping both")
                    stats.kept += 1
                stats.processed += 1
            elif choice == "b":
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


# --------------------------------------------------------------------------
# Config (djtool.toml)
# --------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    """Load <project>/djtool.toml. Returns {} when the file is absent."""
    p = config_path()
    if not p.exists():
        return {}
    if not HAVE_TOML:
        raise ConfigError("djtool.toml needs Python 3.11+ (tomllib) to parse")
    try:
        return tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:  # type: ignore[attr-defined]
        raise ConfigError(f"invalid {CONFIG_FILE_NAME}: {e}") from None


def config_path() -> Path:
    return project_dir() / CONFIG_FILE_NAME


@dataclass
class SyncConfig:
    remote: str
    remote_dj: str
    local_mixxx: str | None = None
    remote_mixxx: str | None = None


def load_sync_config(root: Path) -> tuple[SyncConfig | None, str]:
    cfg = load_config()
    section = cfg.get("sync") or {}
    missing = [k for k in ("remote", "remote_dj") if not section.get(k)]
    if missing:
        return None, f"[sync] in {CONFIG_FILE_NAME} ({config_path()}) is missing: {', '.join(missing)}"
    return SyncConfig(
        remote=section["remote"],
        remote_dj=section["remote_dj"],
        local_mixxx=section.get("local_mixxx") or None,
        remote_mixxx=section.get("remote_mixxx") or None,
    ), ""


# --------------------------------------------------------------------------
# rsync synchronization
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Commands: doctor, scan, dedupe, ingest, trash, cache
# --------------------------------------------------------------------------


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
            console.out(f"  {'Library':<10}{console.yellow('WRITABLE — djtool must never modify it (it will not)')}")
        else:
            console.out(f"  {'Library':<10}not writable (good)")
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
    console.out("Reminder: Library/ is strictly read-only. Tracks/ and Incoming/ are writable.")
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
    pairs = find_candidates(tracks)
    from collections import Counter

    counts = Counter(p.category for p in pairs)
    if pairs:
        console.out("")
        for cat in CATEGORIES:
            if counts[cat]:
                console.out(f"  {cat}: {counts[cat]} pair(s)")
        console.out(console.dim("run 'djtool dedupe' to review, 'djtool ingest' for the Incoming workflow"))
    else:
        console.out("")
        console.info("no duplicates or candidates found")
    save_cache_from_tracks(root, tracks)
    return 0


def cmd_dedupe(args: argparse.Namespace, console: Console, root: Path) -> int:
    ensure_dirs(root, console)
    tracks, stats = scan_collection(root, use_cache=not args.no_cache)
    pairs = find_candidates(tracks)
    console.out(console.dim(
        f"Library {stats.counts['library']} · Tracks {stats.counts['tracks']} · "
        f"Incoming {stats.counts['incoming']} — {len(pairs)} pair(s) for review"
    ))
    for w in stats.warnings:
        console.out(console.dim("  warn: " + w))
    if not pairs:
        console.info("no duplicates or candidates found")
        save_cache_from_tracks(root, tracks)
        return 0
    rstats = review_pairs(root, pairs, console, mode="dedupe")
    console.out("")
    console.out(f"Quarantined: {rstats.quarantined}   Kept both: {rstats.kept}   "
                f"Promoted: {rstats.promoted}   Skipped: {rstats.skipped}")
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
    pairs = find_candidates(tracks)
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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


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


if __name__ == "__main__":
    sys.exit(main())
