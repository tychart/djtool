"""Filename scheme 'Title - Artist [Version].ext' and the version vocabulary."""

from __future__ import annotations

import re

from djtool.model import Track
from djtool.text import normalize_text

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


def derive_track_name(track: Track, version: str | None = None) -> str:
    """Derive the flat Tracks/ filename: 'Title - Artist [Version].ext'.

    Uses tags when present, otherwise best-effort filename parsing. When
    nothing can be derived the original filename is kept (still flattened).
    An explicit *version* qualifier (e.g. from a recorded Library rename
    decision) is combined with any version already extracted from the title.
    """
    ext = track.path.suffix.lower()
    title = (track.title or track.filename_title or "").strip()
    artist = (track.artist or track.filename_artist or "").strip()
    if not title and not artist:
        return track.path.name
    base_title, extracted = extract_version(title)
    parts: list[str] = []
    if base_title:
        parts.append(sanitize_filename_part(base_title))
    if artist:
        artist = simplify_artist(artist)
        if artist:
            parts.append(sanitize_filename_part(artist))
    base = " - ".join(parts)
    qualifier = canonicalize_version(extracted, version or "")
    if qualifier:
        base = f"{base} [{qualifier}]"
    base = sanitize_filename_part(base)
    return f"{base}{ext}" if base else track.path.name

