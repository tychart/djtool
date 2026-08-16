# djtool

Small, maintainable CLI for managing a DJ music collection. Data safety is the
first priority: `Library/` is strictly read-only, and interactive removal only
quarantines files into `.Trash/YYYY-MM-DD/` (a hidden folder Mixxx never scans).

## Collection layout

```
DJ/                                  # location configured in djtool.toml
├── Library/             read-only Beets mirror — NEVER modified by djtool
├── Tracks/              canonical DJ tracks — flat, 'Title - Artist [Version].ext'
├── Incoming/            staging area, reviewed before promotion
└── .Trash/YYYY-MM-DD/   quarantine (flat), skipped by Mixxx library scans
```

djtool itself is a normal project — clone it anywhere (e.g. `~/programs/djtool`),
point `[collection] root` at your DJ folder, and run it from anywhere.

## Install

```sh
git clone <your-repo-url> ~/programs/djtool
cd ~/programs/djtool
uv sync                       # creates .venv, installs mutagen + rapidfuzz (+ pytest)
uv run djtool doctor          # environment & dependency check
```

`fpcalc` (chromaprint) is optional but strongly recommended:
`sudo dnf install chromaprint-tools`. Without it, duplicate detection degrades
to metadata-only and never claims "very likely same recording".

## Configure — djtool.toml

```toml
[collection]
root = "/home/you/Music/DJ"      # the folder containing Library/, Tracks/, Incoming/

[sync]
remote = "user@host-or-ip"       # any SSH target (IP or DNS name)
remote_dj = "/home/user/Music/DJ"
# local_mixxx = "/home/user/.mixxx"   # optional, synced as one coherent state
# remote_mixxx = "/home/user/.mixxx"
```

The DJ root is resolved in this order: `--root` flag, `$DJTOOL_ROOT`,
`[collection] root`, then auto-detection (walking up from the package — only
useful when the project still lives inside the DJ folder).

## Usage

```sh
uv run djtool scan                     # summary + candidate counts
uv run djtool dedupe                   # interactive duplicate review
uv run djtool ingest                   # Incoming/ review → promote to Tracks or quarantine
uv run djtool trash list               # inspect quarantine
uv run djtool trash empty --yes        # permanently empty quarantine
uv run djtool cache status | clear     # derived-data cache
uv run djtool sync status              # dry-run comparison both directions
uv run djtool sync push -n             # dry-run push (local → remote)
uv run djtool sync push                # push with confirmation
uv run djtool sync pull                # pull (remote → local)
```

All commands work without a terminal too; ANSI colors are disabled when stdout
is not a TTY (or with `--no-color`).

## Tracks/ is flat — 'Title - Artist [Version].ext'

Ingest only moves **files**, never folder structure. Every promoted file is
renamed from its embedded tags (falling back to best-effort filename parsing):

```
Dancing Queen - ABBA.flac
Levitating - Dua Lipa [Clean Radio Edit].flac
Cheerleader - OMI [Felix Jaehn Remix Radio Edit].flac
Rather Be - Clean Bandit [The Magician Remix].flac
```

* `[Version]` is omitted when there is nothing to distinguish.
* Version labels are normalized to a small vocabulary — `radio edit`,
  `RADIO EDIT` and `single version` become `Radio Edit` / `Single Version`;
  `clean version` becomes `Clean`. Named remixes stay descriptive.
* Combined qualifiers live inside one bracket: `[Clean Radio Edit]`,
  `[DJ Intro Clean]`.
* No album, year, track number, BPM, key or crate goes into the filename.
* Album, year, track numbers and crate/playlist source are never used.

**Collisions are never auto-renamed.** If a promoted file would collide with an
existing Tracks file, djtool stops and asks: version the incoming file, version
the existing one, version both, or skip. Files are never numbered
(`Song (2).flac`), which keeps `Tracks/` self-explanatory years later.

After promotion, now-empty Incoming subfolders are removed automatically.

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
it also promotes the Incoming copy to `Tracks/` — flat, renamed); `[p]`/`[o]`
play files via `ffplay`; `[c]` plays A then B; `[s]` defers; `[q]` quits safely.

## Mixxx

Add your DJ root (or just `Tracks/`) as a library directory in Mixxx. Mixxx's
library scanner skips dot-prefixed (hidden) folders, so `.Trash/` never shows
up in a library scan — no extra setup needed. If you don't want `Library/`
(Beets mirror) tracks in Mixxx, add only `Tracks/` (and `Incoming/`) as roots.

## Cache

`.djtool-cache.json` lives in the `djtool/` project folder and stores
**derived data only** (hashes, durations, parsed tags, fingerprints), invalidated
by file size + mtime. It is tagged with the DJ root it was built for. Deleting
it (`djtool cache clear`) can never lose collection state or review decisions.

## Sync

Configured in `djtool.toml` (`[sync]`). `push` treats the local machine as
authoritative (`local → remote`), `pull` the remote (`remote → local`); the
exact direction is always shown before anything happens. The remote is whatever
host you configure — an IP or DNS name. `--delete` is opt-in and still requires
confirmation. Mixxx settings are synced as one coherent state: the command
refuses to run when Mixxx appears to be running locally, and warns when the
remote state cannot be verified. SQLite databases are never merged — they are
copied whole.

The DJ sync excludes collection quarantine (`.Trash`) and project internals
(`.venv`, `.git`, `__pycache__`, the cache).

## Development

```sh
uv run pytest               # run the test suite (no real collection needed)
```

Design priorities: data safety, simplicity, maintainability, interactive UX,
correct duplicate detection, performance.
