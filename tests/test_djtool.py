"""Tests for djtool. Run with: uv run pytest

All tests use temporary directories and generated audio fixtures — never the
real collection. Fingerprint tests either use crafted base64 payloads or skip
when fpcalc is absent.
"""

import argparse
import base64
import math
import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

import djtool as dt
from djtool.commands import cmd_dedupe

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A minimal DJ root with the three collection directories."""
    for d in ("Library", "Tracks", "Incoming"):
        (tmp_path / d).mkdir()
    return tmp_path


def make_wav(path: Path, seconds: float = 1.0, rate: int = 8000, seed: int = 0) -> Path:
    """Generate a small valid WAV (pure stdlib). Same params -> identical bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            sample = int(12000 * math.sin(2 * math.pi * (440 + seed) * i / rate))
            frames += (sample & 0xFFFF).to_bytes(2, "little")
        w.writeframes(bytes(frames))
    return path


def make_track(root: Path, source: str, rel: str, title: str = "", artist: str = "",
               duration: float | None = None, sha256: str | None = None,
               size: int = 1000, path: Path | None = None) -> dt.Track:
    p = path or (root / dt.SOURCE_DIRS[source] / rel)
    return dt.Track(path=p, rel=f"{dt.SOURCE_DIRS[source]}/{rel}", source=source, size=size,
                    mtime_ns=0, sha256=sha256, duration=duration,
                    title=title, artist=artist)


def _enc(words: list[int]) -> str:
    return base64.b64encode(b"".join(w.to_bytes(4, "big") for w in words)).decode()


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_unicode_and_case(self):
        assert dt.normalize_text("Why Don’t We Just Dance") == "why don't we just dance"
        assert dt.normalize_text("Café") == "café"
        assert dt.normalize_text("ＦＵＬＬＷＩＤＴＨ") == "fullwidth"

    def test_punctuation_and_whitespace(self):
        assert dt.normalize_text("  Album—Title (Deluxe)  ") == "album title deluxe"
        assert dt.normalize_text("Why-Don't-We") == "why don't we"
        assert dt.normalize_text("   spaced   out  ") == "spaced out"

    def test_ampersand(self):
        assert dt.normalize_text("Simon & Garfunkel") == "simon and garfunkel"

    def test_empty(self):
        assert dt.normalize_text(None) == ""
        assert dt.normalize_text("") == ""

    def test_feat_splitting(self):
        assert dt.split_feat("Artist feat. Someone") == ("Artist", "Someone")
        assert dt.split_feat("Artist featuring Someone") == ("Artist", "Someone")
        assert dt.split_feat("Artist ft. Someone") == ("Artist", "Someone")
        assert dt.split_feat("No Marker") == ("No Marker", "")

    def test_version_terms_detected(self):
        assert dt.version_terms_present("why don't we just dance radio edit") == {"radio edit"}
        assert dt.version_terms_present("plain song") == set()

    def test_core_strips_versions_and_feat(self):
        assert dt.core_of("why don't we just dance album version") == "why don't we just dance"
        assert dt.core_of("josh turner feat. someone") == "josh turner"

    def test_filename_guessing(self):
        assert dt.guess_from_filename("Josh Turner - Why Don't We Just Dance") == (
            "Josh Turner", "Why Don't We Just Dance")
        assert dt.guess_from_filename("01 - Josh Turner - Why Don't We Just Dance") == (
            "Josh Turner", "Why Don't We Just Dance")
        assert dt.guess_from_filename("Josh Turner – Song Name") == ("Josh Turner", "Song Name")
        assert dt.guess_from_filename("JustASong") == (None, "JustASong")
        assert dt.guess_from_filename("") == (None, None)

    def test_version_terms_distinguish_alternates(self):
        a = dt.core_of("song radio edit")
        b = dt.core_of("song album version")
        assert a == b == "song"
        assert dt.version_terms_present("song radio edit") != dt.version_terms_present("song album version")


# ---------------------------------------------------------------------------
# Candidate generation, classification, library preference
# ---------------------------------------------------------------------------


class TestCandidates:
    def test_block_key_groups_versions(self, root):
        a = make_track(root, "library", "a.flac", title="Why Don't We Just Dance (Radio Edit)", artist="Josh Turner")
        b = make_track(root, "incoming", "b.flac", title="Why Don't We Just Dance", artist="Josh Turner")
        assert a.block_key == b.block_key

    def test_candidate_pair_found(self, root):
        a = make_track(root, "library", "a.flac", title="Why Don't We Just Dance", artist="Josh Turner", duration=209.2)
        b = make_track(root, "incoming", "b.flac", title="Why Don't We Just Dance", artist="Josh Turner", duration=209.3)
        pairs = dt.find_candidates([a, b])
        assert len(pairs) == 1
        p = pairs[0]
        assert p.title_sim == pytest.approx(1.0)
        assert p.duration_diff == pytest.approx(0.1)
        assert p.category == "POSSIBLE_SAME_RECORDING"  # metadata-only without fpcalc

    def test_unrelated_tracks_not_candidates(self, root):
        a = make_track(root, "library", "a.flac", title="Song One", artist="Artist A")
        b = make_track(root, "incoming", "b.flac", title="Totally Different", artist="Artist B")
        assert dt.find_candidates([a, b]) == []

    def test_alternate_versions_still_candidates(self, root):
        a = make_track(root, "library", "a.flac", title="Song (Radio Edit)", artist="Artist")
        b = make_track(root, "incoming", "b.flac", title="Song (Album Version)", artist="Artist")
        pairs = dt.find_candidates([a, b])
        assert len(pairs) == 1
        assert pairs[0].title_sim >= 0.9  # core titles match

    def test_metadata_missing_uses_filename(self, root):
        a = make_track(root, "library", "Josh Turner - Song Name.flac")
        b = make_track(root, "incoming", "b.flac", title="Song Name", artist="Josh Turner")
        assert a.core_artist == "josh turner"
        assert a.core_title == "song name"
        assert len(dt.find_candidates([a, b])) == 1


class TestClassification:
    def _pair(self, root, title_a, title_b, fp_sim=None, artist="Artist", dur_a=200.0, dur_b=200.0, sha=None):
        a = make_track(root, "library", "a.flac", title=title_a, artist=artist, duration=dur_a, sha256=sha)
        b = make_track(root, "incoming", "b.flac", title=title_b, artist=artist, duration=dur_b, sha256=sha)
        p = dt.make_pair(a, b)
        if fp_sim is not None:
            p.fp_sim = fp_sim
            p.category, p.note = dt.classify(p)
        return p

    def test_exact_duplicate(self, root):
        p = self._pair(root, "Same Song", "Same Song", sha="abc123")
        assert p.category == "EXACT_DUPLICATE"
        assert "byte-identical" in p.note

    def test_fingerprint_agree_very_likely(self, root):
        p = self._pair(root, "Same Song", "Same Song", fp_sim=0.95)
        assert p.category == "VERY_LIKELY_SAME_RECORDING"

    def test_fingerprint_partial_possible(self, root):
        p = self._pair(root, "Same Song", "Same Song", fp_sim=0.70)
        assert p.category == "POSSIBLE_SAME_RECORDING"

    def test_same_title_fingerprint_differs_alternate(self, root):
        p = self._pair(root, "Same Song", "Same Song", fp_sim=0.10)
        assert p.category == "POSSIBLE_ALTERNATE_VERSION"

    def test_version_terms_fingerprint_differs_alternate(self, root):
        p = self._pair(root, "Song (Radio Edit)", "Song (Album Version)", fp_sim=0.10)
        assert p.category == "POSSIBLE_ALTERNATE_VERSION"
        assert "version term" in p.note

    def test_no_fingerprint_caps_at_possible(self, root, monkeypatch):
        # Without fpcalc the classifier must never rise above "possible".
        monkeypatch.setattr(dt.fingerprint, "FPCALC", None)
        p = self._pair(root, "Same Song", "Same Song")  # fp_sim None
        assert p.category == "POSSIBLE_SAME_RECORDING"
        assert "no fpcalc" in p.note

    def test_duration_gap_plus_terms_alternate(self, root):
        p = self._pair(root, "Song (Radio Edit)", "Song (Album Version)", dur_a=200.0, dur_b=300.0)
        assert p.category == "POSSIBLE_ALTERNATE_VERSION"


class TestLibraryPreference:
    def test_order_pair_library_first(self, root):
        inc = make_track(root, "incoming", "b.flac")
        lib = make_track(root, "library", "a.flac")
        a, b = dt.order_pair(inc, lib)
        assert a.source == "library" and b.source == "incoming"

    def test_order_pair_tracks_before_incoming(self, root):
        inc = make_track(root, "incoming", "b.flac")
        trk = make_track(root, "tracks", "a.flac")
        a, b = dt.order_pair(inc, trk)
        assert a.source == "tracks" and b.source == "incoming"

    def test_keep_one_refuses_library_removal_in_cross_source_pair(self, root):
        libf = root / "Library" / "Song.flac"
        libf.write_bytes(b"x")
        trkf = root / "Tracks" / "Song.flac"
        trkf.write_bytes(b"y")
        lib = make_track(root, "library", "Song.flac", path=libf, size=1,
                         title="Song", artist="Artist")
        trk = make_track(root, "tracks", "Song.flac", path=trkf, size=1,
                         title="Song", artist="Artist")
        pair = dt.make_pair(lib, trk)  # A = Library copy
        # [l] keeps the Library copy and removes the other side
        assert dt.action_keep_one(root, pair, keep_a=True) == "quarantined"
        assert libf.exists() and not trkf.exists()
        # [j] would remove the Library copy — refused (Library is read-only
        # unless the pair is Library-internal and the decision is recorded)
        assert dt.action_keep_one(root, pair, keep_a=False) == "refused"
        assert libf.exists()

    def test_keep_one_records_library_internal_removals(self, root):
        l1f = root / "Library" / "Album A" / "Song.flac"
        l2f = root / "Library" / "Album B" / "Song.flac"
        l1f.parent.mkdir(parents=True)
        l1f.write_bytes(b"x")
        l2f.parent.mkdir(parents=True)
        l2f.write_bytes(b"y")
        l1 = make_track(root, "library", "Album A/Song.flac", path=l1f, size=1,
                        title="Song", artist="Artist")
        l2 = make_track(root, "library", "Album B/Song.flac", path=l2f, size=1,
                        title="Song", artist="Artist")
        pair = dt.make_pair(l1, l2)
        assert dt.action_keep_one(root, pair, keep_a=True) == "quarantined"
        assert l1f.exists() and not l2f.exists()
        d = dt.load_decisions(root)["decisions"][0]
        assert d["action"] == "remove" and d["kept"] == pair.a.rel and d["removed"] == pair.b.rel


# ---------------------------------------------------------------------------
# Path safety & quarantine
# ---------------------------------------------------------------------------


class TestPathSafety:
    def test_ensure_within_rejects_escape(self, tmp_path: Path):
        from djtool.quarantine import _ensure_within

        dj = tmp_path / "dj"
        dj.mkdir()
        evil = tmp_path / "evil.flac"
        evil.write_bytes(b"x")
        with pytest.raises(ValueError):
            _ensure_within(evil.resolve(), dj.resolve(), "source")

    def test_quarantine_refuses_library(self, root):
        f = root / "Library" / "x.flac"
        f.write_bytes(b"x")
        track = make_track(root, "library", "x.flac", path=f, size=1)
        with pytest.raises(ValueError, match="read-only"):
            dt.quarantine_file(root, track)
        assert f.exists()

    def test_quarantine_flattens_relative_path(self, root):
        f = root / "Incoming" / "sub" / "x.flac"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"data")
        track = make_track(root, "incoming", "sub/x.flac", path=f, size=4)
        dest = dt.quarantine_file(root, track)
        assert not f.exists()
        assert dest == dt.trash_dir_for(root) / "incoming" / "x.flac"
        assert dest.read_bytes() == b"data"

    def test_quarantine_unique_name_on_collision(self, root):
        f = root / "Incoming" / "x.flac"
        f.write_bytes(b"data")
        clash = dt.trash_dir_for(root) / "incoming" / "x.flac"
        clash.parent.mkdir(parents=True)
        clash.write_bytes(b"other")
        track = make_track(root, "incoming", "x.flac", path=f, size=4)
        dest = dt.quarantine_file(root, track)
        assert dest.name == "x - 2.flac"
        assert dest.read_bytes() == b"data"


class TestTrash:
    def test_list_and_empty(self, root):
        for name in ("a.flac", "b.flac"):
            f = root / "Incoming" / name
            f.write_bytes(b"x")
            dt.quarantine_file(root, make_track(root, "incoming", name, path=f, size=1))
        entries = dt.trash_entries(root)
        assert len(entries) == 2
        with pytest.raises(dt.ConfirmationRequired):
            dt.empty_trash(root, yes=False)
        assert dt.empty_trash(root, yes=True) == 2
        assert dt.trash_entries(root) == []

    def test_empty_no_trash_dir(self, root):
        assert dt.empty_trash(root, yes=True) == 0


class TestPromote:
    def test_promote_to_tracks_flat(self, root):
        f = root / "Incoming" / "new song.flac"
        f.write_bytes(b"data")
        track = make_track(root, "incoming", "new song.flac", path=f, size=4)
        dest, action = dt.promote_to_tracks(root, track)
        assert action == "promoted"
        assert dest == root / "Tracks" / "new song.flac"
        assert not f.exists()
        assert dest.read_bytes() == b"data"

    def test_promote_flattens_nested_folders(self, root):
        f = root / "Incoming" / "Messy Album 2024" / "Some Rip" / "Track.flac"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"data")
        track = make_track(root, "incoming", "Messy Album 2024/Some Rip/Track.flac", path=f, size=4)
        dest, _ = dt.promote_to_tracks(root, track)
        assert dest == root / "Tracks" / "Track.flac"
        assert not f.exists()

    def test_promote_renames_title_artist(self, root):
        f = root / "Incoming" / "whatever.flac"
        f.write_bytes(b"data")
        track = make_track(root, "incoming", "whatever.flac", path=f, size=4,
                           title="Yeah!", artist="Usher")
        dest, action = dt.promote_to_tracks(root, track)
        assert action == "promoted"
        assert dest == root / "Tracks" / "Yeah! - Usher.flac"

    def test_promote_adds_canonical_version(self, root):
        f = root / "Incoming" / "levitating.flac"
        f.write_bytes(b"data")
        track = make_track(root, "incoming", "levitating.flac", path=f, size=4,
                           title="Levitating (Clean Radio Edit)", artist="Dua Lipa")
        dest, _ = dt.promote_to_tracks(root, track)
        assert dest == root / "Tracks" / "Levitating - Dua Lipa [Clean Radio Edit].flac"

    def test_promote_collision_raises_without_input(self, root):
        f = root / "Incoming" / "x.flac"
        f.write_bytes(b"data")
        (root / "Tracks" / "x.flac").write_bytes(b"existing")
        track = make_track(root, "incoming", "x.flac", path=f, size=4)
        with pytest.raises(dt.NameCollision):
            dt.promote_to_tracks(root, track)
        assert f.exists()  # nothing was moved

    def test_promote_collision_resolved_with_version(self, root, monkeypatch):
        f = root / "Incoming" / "x.flac"
        f.write_bytes(b"data")
        (root / "Tracks" / "x.flac").write_bytes(b"existing")
        track = make_track(root, "incoming", "x.flac", path=f, size=4)
        answers = iter(["v", "radio edit"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        dest, action = dt.promote_to_tracks(root, track, get_input=input)
        assert action == "renamed"
        assert dest == root / "Tracks" / "x [Radio Edit].flac"
        assert dest.read_bytes() == b"data"
        assert (root / "Tracks" / "x.flac").read_bytes() == b"existing"

    def test_promote_collision_rename_existing(self, root, monkeypatch):
        f = root / "Incoming" / "x.flac"
        f.write_bytes(b"data")
        (root / "Tracks" / "x.flac").write_bytes(b"existing")
        track = make_track(root, "incoming", "x.flac", path=f, size=4)
        answers = iter(["e", "clean"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        dest, action = dt.promote_to_tracks(root, track, get_input=input)
        assert action == "renamed-existing"
        assert dest == root / "Tracks" / "x.flac"
        assert dest.read_bytes() == b"data"
        assert (root / "Tracks" / "x [Clean].flac").read_bytes() == b"existing"

    def test_promote_collision_rename_both(self, root, monkeypatch):
        f = root / "Incoming" / "x.flac"
        f.write_bytes(b"data")
        (root / "Tracks" / "x.flac").write_bytes(b"existing")
        track = make_track(root, "incoming", "x.flac", path=f, size=4)
        answers = iter(["b", "clean", "radio edit"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        dest, action = dt.promote_to_tracks(root, track, get_input=input)
        assert action == "renamed-both"
        assert dest == root / "Tracks" / "x [Radio Edit].flac"
        assert (root / "Tracks" / "x [Clean].flac").read_bytes() == b"existing"

    def test_promote_collision_skip(self, root, monkeypatch):
        f = root / "Incoming" / "x.flac"
        f.write_bytes(b"data")
        (root / "Tracks" / "x.flac").write_bytes(b"existing")
        track = make_track(root, "incoming", "x.flac", path=f, size=4)
        answers = iter(["s"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        _, action = dt.promote_to_tracks(root, track, get_input=input)
        assert action == "skipped"
        assert f.exists()
        assert (root / "Tracks" / "x.flac").read_bytes() == b"existing"

    def test_promote_refuses_library(self, root):
        track = make_track(root, "library", "x.flac")
        with pytest.raises(ValueError, match="Incoming"):
            dt.promote_to_tracks(root, track)


class TestNaming:
    def test_extract_version_groups(self):
        assert dt.extract_version("Song (Clean Radio Edit)") == ("Song", "Clean Radio Edit")
        assert dt.extract_version("Levitating [The Blessed Madonna Remix]") == (
            "Levitating", "The Blessed Madonna Remix")
        assert dt.extract_version("Song") == ("Song", "")
        assert dt.extract_version("I Don't Have To Be Me ('Til Monday)") == (
            "I Don't Have To Be Me ('Til Monday)", "")

    def test_extract_version_trailing_phrase(self):
        assert dt.extract_version("Song - Radio Edit") == ("Song", "Radio Edit")
        assert dt.extract_version("Why Don't We Just Dance") == ("Why Don't We Just Dance", "")

    def test_canonicalize_version(self):
        assert dt.canonicalize_version("RADIO EDIT") == "Radio Edit"
        assert dt.canonicalize_version("clean version") == "Clean"
        assert dt.canonicalize_version("The Blessed Madonna Remix") == "The Blessed Madonna Remix"
        assert dt.canonicalize_version("original version") == ""  # drop-marked

    def test_derive_track_name_fallback(self, root):
        t = make_track(root, "incoming", "nested/deep song.flac")
        assert dt.derive_track_name(t) == "deep song.flac"

    def test_derive_track_name_from_filename(self, root):
        t = make_track(root, "incoming", "Josh Turner - Why Don't We Just Dance.flac")
        assert dt.derive_track_name(t) == "Why Don't We Just Dance - Josh Turner.flac"

    def test_derive_track_name_remix(self, root):
        t = make_track(root, "incoming", "x.flac", title="Cheerleader (Felix Jaehn Remix Radio Edit)", artist="OMI")
        assert dt.derive_track_name(t) == "Cheerleader - OMI [Felix Jaehn Remix Radio Edit].flac"

    def test_sanitize_filename_part(self):
        assert dt.sanitize_filename_part('a/b:c*?"<>|\\') == "abc"

    def test_simplify_artist_dedupes(self):
        assert dt.simplify_artist("A, A, B") == "A, B"
        assert dt.simplify_artist("Mark Ronson feat. Bruno Mars") == "Mark Ronson feat. Bruno Mars"


class TestPrune:
    def test_prune_empty_dirs_bottom_up(self, root):
        (root / "Incoming" / "a" / "b").mkdir(parents=True)
        assert dt.prune_empty_dirs(root, "incoming") == 2
        assert not (root / "Incoming" / "a").exists()
        assert (root / "Incoming").exists()

    def test_prune_keeps_dirs_with_files(self, root):
        f = root / "Incoming" / "a" / "x.flac"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"data")
        assert dt.prune_empty_dirs(root, "incoming") == 0
        assert f.exists()

    def test_prune_missing_source_ok(self, root):
        assert dt.prune_empty_dirs(root, "tracks") == 0


# ---------------------------------------------------------------------------
# Scan, exact duplicates, cache
# ---------------------------------------------------------------------------


class TestScan:
    def test_exact_duplicates_found(self, root):
        one = make_wav(root / "Library" / "one.wav", seed=1)
        two = root / "Incoming" / "two.wav"
        shutil.copyfile(one, two)
        tracks, stats = dt.scan_collection(root)
        assert len(tracks) == 2
        assert stats.counts == {"library": 1, "tracks": 0, "incoming": 1}
        pairs = dt.find_candidates(tracks)
        assert len(pairs) == 1
        assert pairs[0].category == "EXACT_DUPLICATE"
        assert pairs[0].a.source == "library"  # Library preferred
        assert pairs[0].b.source == "incoming"

    def test_unique_size_files_not_hashed(self, root):
        make_wav(root / "Library" / "one.wav", seed=1)
        make_wav(root / "Tracks" / "two.wav", seed=2, seconds=2.0)  # different size
        tracks, _ = dt.scan_collection(root)
        assert all(t.sha256 is None for t in tracks)  # no size collisions -> no hashing

    def test_cache_reused_second_scan(self, root):
        make_wav(root / "Library" / "one.wav", seed=1)
        tracks, stats1 = dt.scan_collection(root)
        assert stats1.new == 1
        dt.save_cache_from_tracks(root, tracks)  # what the CLI does between runs
        _, stats2 = dt.scan_collection(root)
        assert stats2.cached == 1 and stats2.new == 0

    def test_cache_invalidated_by_mtime(self, root):
        f = make_wav(root / "Library" / "one.wav", seed=1)
        tracks, _ = dt.scan_collection(root)
        dt.save_cache_from_tracks(root, tracks)
        os.utime(f, (1, 1))
        _, stats = dt.scan_collection(root)
        assert stats.stale == 1 and stats.new == 1


class TestCache:
    def test_roundtrip_and_validity(self, tmp_path: Path):
        root = tmp_path / "dj"
        root.mkdir()
        entries = {"Incoming/a.flac": {"size": 5, "mtime_ns": 1, "sha256": "ab"}}
        dt.save_cache(root, entries)
        assert dt.load_cache(root)["entries"] == entries
        assert dt.cache_valid(entries["Incoming/a.flac"], 5, 1)
        assert not dt.cache_valid(entries["Incoming/a.flac"], 5, 2)
        dt.clear_cache()
        assert dt.load_cache(root)["entries"] == {}

    def test_cache_root_tagged(self, tmp_path: Path):
        root_a = tmp_path / "dj-a"
        root_a.mkdir()
        root_b = tmp_path / "dj-b"
        root_b.mkdir()
        dt.save_cache(root_a, {"Library/a.flac": {"size": 5, "mtime_ns": 1, "sha256": "aa"}})
        # A different collection (or a copied project) must not reuse the data
        assert dt.load_cache(root_b)["entries"] == {}
        assert dt.load_cache(root_a)["entries"] == {"Library/a.flac": {"size": 5, "mtime_ns": 1, "sha256": "aa"}}

    def test_cache_never_stores_decisions(self, tmp_path: Path):
        root = tmp_path / "dj"
        root.mkdir()
        dt.save_cache(root, {"Incoming/a.flac": {"size": 5, "mtime_ns": 1, "sha256": "ab"}})
        raw = dt.cache_path().read_text()
        assert "quarantine" not in raw and "decision" not in raw


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


class TestFingerprints:
    def test_similarity_identical(self):
        fp = _enc([0xDEADBEEF] * 8 + [0x12345678] * 8)
        assert dt.fp_similarity(fp, fp) == pytest.approx(1.0)

    def test_similarity_identical_raw_format(self):
        # fpcalc -raw output is comma-separated decimal words, not base64 —
        # this format must decode to the same values (regression: it used to
        # be base64-decoded into garbage, yielding None / "n/a").
        words = [0xDEADBEEF] * 8 + [0x12345678] * 8
        raw = ",".join(str(w) for w in words)
        assert dt.fp_similarity(raw, raw) == pytest.approx(1.0)
        other = ",".join(str(w) for w in [0x11111111] * 16)
        sim = dt.fp_similarity(raw, other)
        assert sim is not None and sim < 0.5

    def test_similarity_alignment(self):
        a = _enc([0xDEADBEEF] * 8)
        b = _enc([0x11111111] * 4 + [0xDEADBEEF] * 8)
        assert dt.fp_similarity(a, b) == pytest.approx(1.0)

    def test_similarity_different(self):
        a = _enc([0xDEADBEEF] * 8)
        b = _enc([0x12345678] * 8)
        sim = dt.fp_similarity(a, b)
        assert sim is not None and sim < 0.5

    def test_similarity_none_when_missing(self):
        assert dt.fp_similarity(None, _enc([1])) is None
        assert dt.fp_similarity("", _enc([1])) is None

    def test_compute_fingerprint_without_tool(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(dt, "FPCALC", None)
        f = make_wav(tmp_path / "x.wav")
        assert dt.compute_fingerprint(f) is None

    @pytest.mark.skipif(shutil.which("fpcalc") is None, reason="fpcalc not installed")
    def test_compute_fingerprint_with_tool(self, tmp_path: Path):
        f = make_wav(tmp_path / "x.wav", seconds=5.0)
        res = dt.compute_fingerprint(f)
        assert res is not None
        fp, dur = res
        assert fp and dur == pytest.approx(5.0, abs=0.5)
        # stored fingerprints must round-trip through the similarity scorer
        assert dt.fp_similarity(fp, fp) is not None
        assert dt.fp_similarity(fp, fp) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# rsync construction & dry-run behavior
# ---------------------------------------------------------------------------

rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


class TestSync:
    def test_rsync_cmd_flags(self):
        cmd = dt.build_rsync_cmd("a/", "b/", dry_run=True, delete=False)
        assert "-n" in cmd and "--delete" not in cmd
        assert cmd.index("a/") < cmd.index("b/")
        cmd2 = dt.build_rsync_cmd("a/", "b/", dry_run=False, delete=True)
        assert "--delete" in cmd2 and "-n" not in cmd2

    def test_rsync_cmd_excludes_state(self):
        cmd = dt.build_rsync_cmd("a/", "b/", dry_run=False, delete=False)
        assert "--exclude" in cmd
        for pattern in (".Trash", ".djtool-cache.json", ".venv", ".git", "__pycache__"):
            assert pattern in cmd

    def test_plan_sync_directions(self, root):
        cfg = dt.SyncConfig(
            remote="user@laptop",
            remote_dj="/home/user/Music/DJ",
            local_mixxx="/home/user/.mixxx",
            remote_mixxx="/home/user/.mixxx",
        )
        push = dt.plan_sync(root, cfg, "push")
        assert push[0][0] == "DJ"
        assert push[0][1] == str(root) + "/"
        assert push[0][2] == "user@laptop:/home/user/Music/DJ/"
        assert push[1][0] == "Mixxx"
        assert push[1][1] == "/home/user/.mixxx/"
        assert push[1][2] == "user@laptop:/home/user/.mixxx/"
        pull = dt.plan_sync(root, cfg, "pull")
        assert pull[0][1] == "user@laptop:/home/user/Music/DJ/"
        assert pull[0][2] == str(root) + "/"

    def test_plan_sync_no_mixxx_without_config(self, root):
        cfg = dt.SyncConfig(remote="user@laptop", remote_dj="/x")
        assert len(dt.plan_sync(root, cfg, "push")) == 1

    def test_requires_confirmation(self):
        assert dt.requires_confirmation(dry_run=True, yes=False) is False
        assert dt.requires_confirmation(dry_run=False, yes=True) is False
        assert dt.requires_confirmation(dry_run=False, yes=False) is True

    @rsync
    def test_dry_run_does_not_modify(self, tmp_path: Path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "a.flac").write_bytes(b"audio")
        cmd = dt.build_rsync_cmd(str(src) + "/", str(dst) + "/", dry_run=True, delete=False)
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert r.returncode == 0
        assert list(dst.iterdir()) == []

    @rsync
    def test_delete_removes_extraneous(self, tmp_path: Path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "a.flac").write_bytes(b"audio")
        (dst / "a.flac").write_bytes(b"audio")
        (dst / "stale.flac").write_bytes(b"old")
        cmd = dt.build_rsync_cmd(str(src) + "/", str(dst) + "/", dry_run=False, delete=True)
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert r.returncode == 0
        assert (dst / "a.flac").exists()
        assert not (dst / "stale.flac").exists()

    @rsync
    def test_dry_run_counts_changes(self, tmp_path: Path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "one.flac").write_bytes(b"audio")
        (src / "two.flac").write_bytes(b"more")
        cmd = dt.build_rsync_cmd(str(src) + "/", str(dst) + "/", dry_run=True, delete=False)
        n, sample = dt.rsync_dry_list(cmd)
        assert n == 2 and len(sample) == 2

    def test_mixxx_running_local_detection(self, monkeypatch):
        def fake_run(*args, **kwargs):
            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert dt.mixxx_running_local() is True

    def test_mixxx_not_running_when_pgrep_absent(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("pgrep not found")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert dt.mixxx_running_local() is False


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------


class TestReview:
    def _dup_pair(self, root) -> tuple[dt.Pair, Path, Path]:
        lib = make_wav(root / "Library" / "Song.flac", seed=5)
        inc = root / "Incoming" / "Song.flac"
        shutil.copyfile(lib, inc)
        tracks, _ = dt.scan_collection(root)
        pairs = dt.find_candidates(tracks)
        assert len(pairs) == 1
        return pairs[0], lib, inc

    def test_keep_library_quarantines_incoming(self, root, monkeypatch):
        pair, lib, inc = self._dup_pair(root)
        answers = iter(["l", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.quarantined == 1
        assert lib.exists()
        assert not inc.exists()

    def test_keep_both_promotes_flat_in_ingest_mode(self, root, monkeypatch):
        lib = make_wav(root / "Library" / "Song.flac", seed=5)
        inc = root / "Incoming" / "Rip Folder" / "Song.flac"
        inc.parent.mkdir(parents=True)
        shutil.copyfile(lib, inc)
        tracks, _ = dt.scan_collection(root)
        pairs = dt.find_candidates(tracks)
        assert len(pairs) == 1
        answers = iter(["b", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, pairs, dt.Console(color=False), mode="ingest")
        assert stats.promoted == 1
        assert (root / "Tracks" / "Song.flac").exists()  # flat, no 'Rip Folder'
        assert not inc.exists()

    def test_quit_leaves_everything(self, root, monkeypatch):
        pair, lib, inc = self._dup_pair(root)
        answers = iter(["q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.processed == 0
        assert lib.exists() and inc.exists()
        assert len(stats.remaining) == 1

    def test_skip_gets_second_chance(self, root, monkeypatch):
        pair, _lib, inc = self._dup_pair(root)
        answers = iter(["s", "l", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.quarantined == 1
        assert not inc.exists()


# ---------------------------------------------------------------------------
# Display helpers & root detection
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_fmt_duration(self):
        assert dt.fmt_duration(209.2) == "3:29.2"
        assert dt.fmt_duration(0) == "0:00.0"
        assert dt.fmt_duration(None) == "unknown"

    def test_detect_root_walks_up(self, tmp_path: Path):
        dj = tmp_path / "DJ"
        (dj / "Library").mkdir(parents=True)
        pkg = dj / "djtool" / "src" / "djtool"
        pkg.mkdir(parents=True)
        assert dt.detect_root(pkg) == dj

    def test_detect_root_fallback_to_script_dir(self, tmp_path: Path):
        pkg = tmp_path / "nowhere" / "src" / "djtool"
        pkg.mkdir(parents=True)
        assert dt.detect_root(pkg) == pkg


class TestResolveRoot:
    def test_config_root(self, tmp_path):
        dj = tmp_path / "dj"
        dj.mkdir()
        dt.config_path().write_text(f'[collection]\nroot = "{dj}"\n')
        assert dt.resolve_root(None) == dj.resolve()

    def test_override_wins(self, tmp_path):
        dj = tmp_path / "dj"
        dj.mkdir()
        elsewhere = tmp_path / "elsewhere"
        dt.config_path().write_text(f'[collection]\nroot = "{elsewhere}"\n')
        assert dt.resolve_root(str(dj)) == dj.resolve()

    def test_unconfigured_raises_clear_error(self, tmp_path, monkeypatch):
        # Project outside any collection and no [collection] root -> clear error.
        # Point __file__ somewhere with no Library/Tracks/Incoming ancestors.
        monkeypatch.setattr(
            dt, "__file__",
            str(tmp_path / "nowhere" / "src" / "djtool" / "__init__.py"),
        )
        with pytest.raises(dt.ConfigError, match="collection"):
            dt.resolve_root(None)


class TestConfig:
    def test_load_sync_config_missing(self, tmp_path: Path):
        dj = tmp_path / "dj"
        dj.mkdir()
        cfg, err = dt.load_sync_config(dj)
        assert cfg is None and "missing" in err

    def test_load_sync_config_present(self, tmp_path: Path):
        dj = tmp_path / "dj"
        dj.mkdir()
        dt.config_path().write_text(
            '[sync]\nremote = "user@laptop"\nremote_dj = "/home/user/Music/DJ"\n'
            'local_mixxx = "/home/user/.mixxx"\n'
        )
        cfg, err = dt.load_sync_config(dj)
        assert err == ""
        assert cfg.remote == "user@laptop"
        assert cfg.local_mixxx == "/home/user/.mixxx"
        assert cfg.remote_mixxx is None

    def test_bad_toml_raises(self, tmp_path: Path):
        dj = tmp_path / "dj"
        dj.mkdir()
        dt.config_path().write_text("[sync\nnope")
        with pytest.raises(dt.ConfigError):
            dt.load_config()


# ---------------------------------------------------------------------------
# Recorded Library-internal decisions & replay
# ---------------------------------------------------------------------------


class TestDecisions:
    def _lib_pair(self, root, title="Song", artist="Artist"):
        a = make_track(root, "library", "Album A/01 - Song.flac", title=title, artist=artist)
        b = make_track(root, "library", "Album B/02 - Song.flac", title=title, artist=artist)
        return dt.make_pair(a, b)

    def test_record_remove_one_decision_per_pair(self, root):
        p = self._lib_pair(root)
        dt.record_remove_decision(root, kept_rel=p.a.rel, removed_rel=p.b.rel)
        data = dt.load_decisions(root)
        assert len(data["decisions"]) == 1
        d = data["decisions"][0]
        assert d["action"] == "remove" and d["kept"] == p.a.rel and d["removed"] == p.b.rel
        # re-recording the same pair replaces the old decision (user changed mind)
        dt.record_keep_both_decision(root, p.a.rel, p.b.rel)
        data = dt.load_decisions(root)
        assert len(data["decisions"]) == 1
        assert data["decisions"][0]["action"] == "keep_both"

    def test_replay_remove_quarantines_and_survives_reset(self, root):
        fa = root / "Library" / "Album A" / "01 - Song.flac"
        fb = root / "Library" / "Album B" / "02 - Song.flac"
        fa.parent.mkdir(parents=True)
        fa.write_bytes(b"a")
        fb.parent.mkdir(parents=True)
        fb.write_bytes(b"b")
        rel_a, rel_b = "Library/Album A/01 - Song.flac", "Library/Album B/02 - Song.flac"
        dt.record_remove_decision(root, kept_rel=rel_a, removed_rel=rel_b)

        stats = dt.replay_decisions(root, dt.Console(color=False))
        assert stats.removed == 1 and stats.renamed == 0
        assert fa.exists() and not fb.exists()

        # simulate a NAS reset: the loser is back; replay removes it again
        fb.write_bytes(b"b2")
        stats = dt.replay_decisions(root, dt.Console(color=False))
        assert stats.removed == 1 and not fb.exists()

    def test_replay_skips_when_kept_missing(self, root):
        fb = root / "Library" / "Album B" / "02 - Song.flac"
        fb.parent.mkdir(parents=True)
        fb.write_bytes(b"b")
        rel_a, rel_b = "Library/Album A/01 - Song.flac", "Library/Album B/02 - Song.flac"
        dt.record_remove_decision(root, kept_rel=rel_a, removed_rel=rel_b)
        stats = dt.replay_decisions(root, dt.Console(color=False))
        assert stats.removed == 0 and stats.skipped == 1
        assert fb.exists()

    def test_replay_rename(self, root):
        fa = root / "Library" / "Album A" / "01 - Song.flac"
        fa.parent.mkdir(parents=True)
        fa.write_bytes(b"a")
        rel = "Library/Album A/01 - Song.flac"
        to_rel = "Library/Album A/Song - Artist [Live].flac"
        dt.record_rename_decision(root, [(rel, to_rel)])
        stats = dt.replay_decisions(root, dt.Console(color=False))
        assert stats.renamed == 1
        assert not fa.exists() and (root / to_rel).exists()

    def test_replay_rename_skips_when_target_exists(self, root):
        fa = root / "Library" / "Album A" / "01 - Song.flac"
        fa.parent.mkdir(parents=True)
        fa.write_bytes(b"a")
        to_rel = "Library/Album A/Song - Artist [Live].flac"
        (root / to_rel).parent.mkdir(parents=True, exist_ok=True)
        (root / to_rel).write_bytes(b"x")
        dt.record_rename_decision(root, [("Library/Album A/01 - Song.flac", to_rel)])
        stats = dt.replay_decisions(root, dt.Console(color=False))
        assert stats.renamed == 0 and stats.skipped == 1

    def test_replay_keep_both_is_noop(self, root):
        dt.record_keep_both_decision(root, "Library/A/a.flac", "Library/B/b.flac")
        stats = dt.replay_decisions(root, dt.Console(color=False))
        assert stats.removed == 0 and stats.renamed == 0 and stats.skipped == 0

    def test_decisions_management(self, root):
        p = self._lib_pair(root)
        dt.record_remove_decision(root, kept_rel=p.a.rel, removed_rel=p.b.rel)
        did = dt.load_decisions(root)["decisions"][0]["id"]
        assert dt.delete_decision(root, did) == 1
        assert dt.load_decisions(root)["decisions"] == []
        dt.record_remove_decision(root, kept_rel=p.a.rel, removed_rel=p.b.rel)
        assert dt.clear_decisions(root) == 1
        assert dt.load_decisions(root)["decisions"] == []

    def test_decision_file_ignored_for_other_root(self, root, tmp_path):
        p = self._lib_pair(root)
        dt.record_remove_decision(root, kept_rel=p.a.rel, removed_rel=p.b.rel)
        other = tmp_path / "other"
        other.mkdir()
        assert dt.load_decisions(other)["decisions"] == []

    def test_quarantine_library_file_moves_to_trash(self, root):
        f = root / "Library" / "Album A" / "01 - Song.flac"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"a")
        track = make_track(root, "library", "Album A/01 - Song.flac", path=f, size=1)
        dest = dt.quarantine_library_file(root, track)
        assert not f.exists() and dest.exists()
        assert ".Trash" in dest.parts

    def test_cmd_dedupe_replays_before_prompting(self, root, monkeypatch):
        fa = root / "Library" / "Album A" / "01 - Song.flac"
        fb = root / "Library" / "Album B" / "02 - Song.flac"
        fa.parent.mkdir(parents=True)
        fa.write_bytes(b"a")
        fb.parent.mkdir(parents=True)
        fb.write_bytes(b"b")
        rel_a, rel_b = "Library/Album A/01 - Song.flac", "Library/Album B/02 - Song.flac"
        dt.record_remove_decision(root, kept_rel=rel_a, removed_rel=rel_b)
        # input would blow up if the pair were still presented — replay must
        # resolve it before any prompting
        monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError("prompted")))
        args = argparse.Namespace(no_replay=False, no_cache=True)
        rc = cmd_dedupe(args, dt.Console(color=False), root)
        assert rc == 0
        assert fa.exists() and not fb.exists()


class TestLibraryPairReview:
    def _dup_library_pair(self, root):
        fa = make_wav(root / "Library" / "Album A" / "01 - Song.flac", seed=1)
        fb = make_wav(root / "Library" / "Album B" / "02 - Song.flac", seed=2)
        tracks, _ = dt.scan_collection(root)
        pairs = dt.find_candidates(tracks)
        assert len(pairs) == 1
        return pairs[0], fa, fb

    def test_l_removes_b_and_records(self, root, monkeypatch):
        pair, fa, fb = self._dup_library_pair(root)
        answers = iter(["l", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.quarantined == 1
        assert fa.exists() and not fb.exists()
        d = dt.load_decisions(root)["decisions"][0]
        assert d["action"] == "remove" and d["removed"] == pair.b.rel and d["kept"] == pair.a.rel

    def test_j_removes_a_and_records(self, root, monkeypatch):
        pair, fa, fb = self._dup_library_pair(root)
        answers = iter(["j", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.quarantined == 1
        assert not fa.exists() and fb.exists()
        d = dt.load_decisions(root)["decisions"][0]
        assert d["action"] == "remove" and d["removed"] == pair.a.rel and d["kept"] == pair.b.rel

    def test_b_records_keep_both(self, root, monkeypatch):
        pair, fa, fb = self._dup_library_pair(root)
        answers = iter(["b", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.kept == 1
        assert fa.exists() and fb.exists()
        d = dt.load_decisions(root)["decisions"][0]
        assert d["action"] == "keep_both"

    def test_v_renames_and_records(self, root, monkeypatch):
        pair, fa, _fb = self._dup_library_pair(root)
        answers = iter(["v", "a", "Live", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.renamed == 1
        assert (root / "Library" / "Album A" / "Song [Live].flac").exists()
        assert not fa.exists()
        d = dt.load_decisions(root)["decisions"][0]
        assert d["action"] == "rename"
        assert d["renames"][0]["from"] == pair.a.rel
        assert d["renames"][0]["to"] == "Library/Album A/Song [Live].flac"

    def test_second_round_sees_no_obsolete_pair(self, root, monkeypatch):
        # after [l], the pair must not be offered again in the same run
        pair, _fa, _fb = self._dup_library_pair(root)
        answers = iter(["s", "l", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.quarantined == 1
        assert len(stats.remaining) == 0

    def test_play_then_decide_stays_on_same_pair(self, root, monkeypatch):
        # [o] play is an auxiliary action: after listening, the SAME pair must
        # be re-prompted. The next decision must act on it, not the next pair.
        p1_lib = make_wav(root / "Library" / "One.flac", seed=1)
        p1_inc = root / "Incoming" / "One.flac"
        shutil.copyfile(p1_lib, p1_inc)
        p2_lib = make_wav(root / "Library" / "Two.flac", seed=2)
        p2_inc = root / "Incoming" / "Two.flac"
        shutil.copyfile(p2_lib, p2_inc)
        tracks, _ = dt.scan_collection(root)
        pairs = dt.find_candidates(tracks)
        assert len(pairs) == 2
        monkeypatch.setattr("djtool.review.play_audio", lambda path, console, label: None)
        answers = iter(["o", "l", "q"])  # play B of pair 1, then keep A
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, pairs, dt.Console(color=False), mode="dedupe")
        assert stats.quarantined == 1
        assert not p1_inc.exists()  # decision applied to the pair we listened to
        assert p2_inc.exists()      # next pair untouched

    def test_info_stays_on_same_pair(self, root, monkeypatch):
        pair, _fa, _fb = self._dup_library_pair(root)
        monkeypatch.setattr("djtool.review.play_audio", lambda path, console, label: None)
        answers = iter(["i", "s", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.skipped == 1  # 's' was consumed by the same pair after info

    def test_j_removes_a_on_tracks_pair(self, root, monkeypatch):
        # [j] keep B / remove A must work for any writable-writable pair
        ta = root / "Tracks" / "One.flac"
        ta.write_bytes(b"a")
        tb = root / "Tracks" / "Two.flac"
        tb.write_bytes(b"b")
        track_a = make_track(root, "tracks", "One.flac", path=ta, size=1,
                             title="Song", artist="Artist")
        track_b = make_track(root, "tracks", "Two.flac", path=tb, size=1,
                             title="Song", artist="Artist")
        pair = dt.make_pair(track_a, track_b)  # a = One.flac (path order)
        answers = iter(["j", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.quarantined == 1
        assert not ta.exists() and tb.exists()
        # writable-writable decisions are never recorded
        assert dt.load_decisions(root)["decisions"] == []

    def test_j_refused_when_a_is_library(self, root, monkeypatch):
        libf = root / "Library" / "Song.flac"
        libf.write_bytes(b"x")
        trkf = root / "Tracks" / "Song.flac"
        trkf.write_bytes(b"y")
        lib = make_track(root, "library", "Song.flac", path=libf, size=1,
                         title="Song", artist="Artist")
        trk = make_track(root, "tracks", "Song.flac", path=trkf, size=1,
                         title="Song", artist="Artist")
        pair = dt.make_pair(lib, trk)
        answers = iter(["j", "q"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        stats = dt.review_pairs(root, [pair], dt.Console(color=False), mode="dedupe")
        assert stats.quarantined == 0 and stats.kept == 1
        assert libf.exists() and trkf.exists()
        assert dt.load_decisions(root)["decisions"] == []
