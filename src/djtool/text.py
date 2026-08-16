"""Text normalization and similarity.

Comparison only — actual tags and filenames are never modified.
"""

from __future__ import annotations

import re
import unicodedata

try:
    from rapidfuzz import fuzz

    HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - environment-dependent
    HAVE_RAPIDFUZZ = False
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

