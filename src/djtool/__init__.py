"""djtool — manage a DJ music collection.

Filesystem model
----------------
    DJ/                        location configured in djtool.toml ([collection] root)
    ├── Library/             Beets mirror, the preferred home for every song —
    │                        read-only except for *recorded decisions* (below)
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

Recorded decisions (Library-internal duplicates)
------------------------------------------------
    When both files of a pair live in Library/, the review records the user's
    choice (keep one / version-rename / keep both) in .djtool-decisions.json
    (project dir, safe from NAS syncs). Because the Library folder is often
    replaced wholesale from a NAS, every `djtool dedupe` run *replays* those
    decisions first: losers are re-quarantined, renames re-applied — before
    the user is asked anything. This is the only way djtool modifies Library/.

Safety rules
------------
    * Library/ is never modified except by applying recorded decisions.
    * Interactive removal only quarantines files into .Trash/YYYY-MM-DD/.
    * The cache stores derived data only; deleting it only slows the next scan.
    * No network is required for duplicate detection.
"""

__version__ = "0.1.0"

from djtool.audio import (
    AUDIO_EXTS,
    AUDIO_LABELS,
    HAVE_MUTAGEN,
    MUTAGEN_READABLE,
    describe_format,
    ffprobe_info,
    read_tags,
)
from djtool.candidates import (
    CATEGORIES,
    classify,
    ensure_fingerprints,
    find_candidates,
    is_candidate,
    make_pair,
)
from djtool.cli import detect_root, main, resolve_root
from djtool.config import SyncConfig, config_path, load_config, load_sync_config
from djtool.console import Console, fmt_bytes, fmt_duration
from djtool.decisions import (
    clear_decisions,
    decisions_path,
    delete_decision,
    describe_decision,
    load_decisions,
    record_keep_both_decision,
    record_remove_decision,
    record_rename_decision,
    replay_decisions,
)
from djtool.errors import ConfigError, ConfirmationRequired, DjToolError, NameCollision
from djtool.fingerprint import FPCALC, compute_fingerprint, fp_similarity
from djtool.model import (
    SOURCE_DIRS,
    SOURCE_RANK,
    SOURCES,
    Pair,
    Track,
    display_artist,
    display_title,
    order_pair,
)
from djtool.naming import (
    canonicalize_version,
    derive_track_name,
    extract_version,
    sanitize_filename_part,
    simplify_artist,
)
from djtool.playback import play_audio
from djtool.promote import promote_to_tracks, prune_empty_dirs, resolve_name_collision
from djtool.quarantine import (
    empty_trash,
    quarantine_file,
    quarantine_library_file,
    trash_dir_for,
    trash_entries,
)
from djtool.review import (
    action_keep_both,
    action_keep_one,
    is_library_pair,
    print_info,
    render_pair,
    render_track,
    review_pairs,
)
from djtool.scan import ScanStats, scan_collection
from djtool.state import (
    cache_path,
    cache_valid,
    clear_cache,
    load_cache,
    project_dir,
    save_cache,
    save_cache_from_tracks,
)
from djtool.sync import (
    build_rsync_cmd,
    mixxx_running_local,
    plan_sync,
    requires_confirmation,
    rsync_dry_list,
)
from djtool.text import (
    core_of,
    guess_from_filename,
    normalize_text,
    split_feat,
    text_sim,
    version_terms_present,
)

__all__ = [
    "AUDIO_EXTS", "AUDIO_LABELS", "CATEGORIES", "ConfigError", "ConfirmationRequired",
    "Console", "DjToolError", "FPCALC", "HAVE_MUTAGEN", "MUTAGEN_READABLE", "NameCollision",
    "Pair", "SOURCES", "SOURCE_DIRS", "SOURCE_RANK", "ScanStats", "SyncConfig", "Track",
    "action_keep_both", "action_keep_one", "build_rsync_cmd", "cache_path", "cache_valid",
    "canonicalize_version", "classify", "clear_cache", "clear_decisions", "compute_fingerprint",
    "config_path", "core_of", "decisions_path", "delete_decision", "derive_track_name",
    "describe_decision", "describe_format", "detect_root", "display_artist", "display_title",
    "empty_trash", "ensure_fingerprints", "extract_version", "ffprobe_info", "find_candidates",
    "fmt_bytes", "fmt_duration", "fp_similarity", "guess_from_filename", "is_candidate",
    "is_library_pair", "load_cache", "load_config", "load_decisions", "load_sync_config",
    "main", "make_pair", "mixxx_running_local", "normalize_text", "order_pair", "plan_sync",
    "play_audio", "print_info", "project_dir", "promote_to_tracks", "prune_empty_dirs",
    "quarantine_file", "quarantine_library_file", "read_tags", "record_keep_both_decision", "record_remove_decision",
    "record_rename_decision", "render_pair", "render_track", "replay_decisions",
    "requires_confirmation", "resolve_name_collision", "resolve_root", "review_pairs",
    "rsync_dry_list", "sanitize_filename_part", "save_cache", "save_cache_from_tracks",
    "scan_collection", "simplify_artist", "split_feat", "text_sim", "trash_dir_for",
    "trash_entries", "version_terms_present",
]
