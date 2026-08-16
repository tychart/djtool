# djtool

Small, maintainable CLI for managing a DJ music collection. Data safety is the
first priority: `Library/` is strictly read-only, and interactive removal only
quarantines files into `.Trash/YYYY-MM-DD/`.

```
DJ/
├── Library/                 # read-only Beets mirror — never modified
├── Tracks/                  # canonical DJ tracks (edits, remixes, clean versions…)
├── Incoming/                # staging area, reviewed before promotion
└── djtool/                  # the whole tool — self-contained (uv project + git repo)
    ├── djtool.toml          # your config ([sync], …)
    ├── .djtool-cache.json   # disposable derived-data cache
    └── …                    # package, tests, .venv (git-ignored)
```

The tool is fully self-contained: the config and the derived-data cache live
inside the `djtool/` project folder, so everything travels with your DJ folder
as one unit.

## Setup

```sh
cd djtool
uv sync                      # creates .venv, installs mutagen + rapidfuzz (+ pytest)
uv run djtool doctor         # environment & dependency check
```

`fpcalc` (chromaprint) is optional but strongly recommended:
`sudo dnf install chromaprint-tools`. Without it, duplicate detection degrades
to metadata-only and never claims "very likely same recording".

## Usage

```sh
uv run djtool scan                     # summary + candidate counts
uv run djtool dedupe                   # interactive duplicate review
uv run djtool ingest                   # Incoming/ review → promote to Tracks or quarantine
uv run djtool trash list               # inspect quarantine
uv run djtool trash empty --yes        # permanently empty quarantine
uv run djtool cache status | clear     # derived-data cache
uv run djtool sync status              # dry-run comparison both directions
uv run djtool sync push -n             # dry-run push (Desktop → Laptop)
uv run djtool sync push                # push with confirmation
uv run djtool sync pull                # pull (Laptop → Desktop)
```

All commands work without a terminal too; ANSI colors are disabled when stdout
is not a TTY (or with `--no-color`).

## How duplicate detection works

1. **Exact duplicates** — whole-file SHA-256, computed only for files whose
   size is shared by at least one other file.
2. **Candidates** — files are grouped by normalized artist/title (version
   terms like *remaster*, *radio edit*, *clean*, *explicit*, *live*,
   *extended*, *remix*… are down-weighted for matching but preserved for
   display), then scored with rapidfuzz.
3. **Chromaprint** — lazy `fpcalc` fingerprints as *evidence*, never an
   unquestionable deletion rule.

Categories: `EXACT_DUPLICATE`, `VERY_LIKELY_SAME_RECORDING`,
`POSSIBLE_SAME_RECORDING`, `POSSIBLE_ALTERNATE_VERSION`. Ambiguous cases are
always shown to you; nothing is ever auto-deleted.

In the review UI, `[l]` keeps the preferred file (Library wins by default) and
quarantines the other to `.Trash/YYYY-MM-DD/`; `[b]` keeps both (in `ingest`,
it also promotes the Incoming copy to `Tracks/`); `[p]`/`[o]` play files via
`ffplay`; `[c]` plays A then B; `[s]` defers; `[q]` quits safely.

## Cache

`.djtool-cache.json` lives inside the `djtool/` project folder and stores
**derived data only** (hashes, durations, parsed tags, fingerprints), invalidated
by file size + mtime. It is tagged with the DJ root it was built for, so a copy
of the project pointed at a different collection simply rebuilds it. Deleting it
(`djtool cache clear`) can never lose collection state or review decisions.

## Sync

Configured in `djtool/djtool.toml` (`[sync]`). `push` treats the Desktop as
authoritative, `pull` the Laptop; the exact direction is always shown before
anything happens. `--delete` is opt-in and still requires confirmation.
Mixxx settings are synced as one coherent state: the command refuses to run
when Mixxx appears to be running locally, and warns when the remote state
cannot be verified. SQLite databases are never merged — they are copied whole.

The DJ sync excludes collection quarantine (`.Trash`) and project internals
(`.venv`, `.git`, `__pycache__`, the cache) — the remote receives code and
config, not machinery.

## Development

```sh
uv run pytest               # run the test suite (no real collection needed)
```

Design priorities: data safety, simplicity, maintainability, interactive UX,
correct duplicate detection, performance.
