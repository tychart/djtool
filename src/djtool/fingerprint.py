"""Chromaprint fingerprints via the fpcalc CLI."""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

FPCALC = shutil.which("fpcalc")
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

