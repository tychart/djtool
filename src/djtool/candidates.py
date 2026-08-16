"""Candidate-pair generation, classification, and fingerprint enrichment."""

from __future__ import annotations

from djtool import fingerprint
from djtool.model import Pair, Track, order_pair
from djtool.text import text_sim, version_terms_present

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
def ensure_fingerprints(pair: Pair) -> None:
    """Compute fingerprints for a pair (lazily, cached) and re-classify."""
    if pair.category == "EXACT_DUPLICATE" or fingerprint.FPCALC is None:
        return
    for t in (pair.a, pair.b):
        if t.fingerprint is None:
            res = fingerprint.compute_fingerprint(t.path)
            if res:
                t.fingerprint, t.fp_duration = res
                if t.duration is None and t.fp_duration:
                    t.duration = t.fp_duration
    pair.fp_sim = fingerprint.fp_similarity(pair.a.fingerprint, pair.b.fingerprint)
    pair.category, pair.note = classify(pair)

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
        if not fingerprint.FPCALC:
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


def find_candidates(tracks: list[Track], resolved: set[frozenset[str]] | None = None) -> list[Pair]:
    """Generate pairs for review: exact duplicates first, then fuzzy candidates.

    Avoids quadratic comparison by grouping on hash (exact) and on the
    normalized artist+title block key (fuzzy). A *resolved* set of pair keys
    (frozensets of two relative paths, from decisions.resolved_pairs) skips
    pairs the user has already decided on (keep-both / version-rename /
    recorded removal).
    """
    resolved = resolved or set()
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
                key = frozenset((group[i].rel, group[j].rel))
                if key in resolved:
                    continue
                p = make_pair(group[i], group[j], exact=True)
                pairs.append(p)
                seen.add(key)

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
                key = frozenset((group[i].rel, group[j].rel))
                if key in seen or key in resolved:
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

