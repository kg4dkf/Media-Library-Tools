# Movie Library Reorganizer

A Python pipeline for taking a large, messily-named movie collection and
reorganizing it into a clean structure that Plex, Radarr, Bazarr, and other
media tools recognize without manual fixup. Built for a real ~5,000-movie
library that had accumulated 15+ years of inconsistent naming from various
ripping tools.

The pipeline is conservative by design: read-only inventory first, manual
review in Excel, dry-run preview, journaled atomic moves with full rollback
capability, post-move verification, and ongoing intake for new disc rips.

This is not a GUI app. It is a set of CLI scripts you run from PowerShell,
with CSV outputs you review in Excel, a comprehensive journal of every
operation, and runbooks for each phase.

## Status & scope

This works for **movies on Windows**. TV-show support is a separate planned
phase — see [Roadmap](#roadmap).

Library size tested: 13,995 files across ~2,800 candidate title folders
(~5 TB). Smaller libraries work fine; significantly larger may need
performance tuning.

## License

This project is released under the **GNU General Public License v3.0**.
You are free to use, modify, and redistribute it under the terms of that
license. See the [`LICENSE`](LICENSE) file for the full text.

> **NO WARRANTY.** This program is distributed in the hope that it will be
> useful, but **WITHOUT ANY WARRANTY**; without even the implied warranty
> of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. **You run this
> against your own files at your own risk.** The author is not responsible
> for data loss, misnamed files, lost duplicates, or any other consequence
> of running this code, however caused. Always run `--dry-run` first. Always
> have backups. The script's `rollback.py` companion exists precisely
> because mistakes are inevitable; use it.

## Author and blog

Written by Brendan with significant collaboration from Anthropic's Claude
Sonnet. The full development conversation — including every dead end,
encoding gremlin, and 4-AM bug we chased — is the subject of a blog post
at: **`<your-blog-url-here>`**.

A companion pipeline for **TV shows** (Sonarr-flavored, with
season/episode awareness, TheTVDB integration, and a separate `H:\TV`
intake) is under design and will appear here when ready. Star or watch
the repo to be notified.

---

## Prerequisites

### What you need

**A movies-only directory.** This pipeline expects every video under your
`movies_root` to be a movie. If you have TV shows mixed in, separate them
first — physically move them to a different directory before running. The
[`runtime_scan.py`](#runtime_scanpy) script can help find stray TV
content; the `tv_check.py` PowerShell snippet (in
[Troubleshooting](#troubleshooting)) catches the obvious offenders by
filename pattern.

**Windows 10 or 11.** The code uses Windows path conventions, drive
letters, and the `\\?\` long-path prefix. It will not work on Linux or
macOS without modifications. PowerShell 5.1 or later for the helper
commands shown throughout.

**Long-path support enabled in Windows.** This is essential — Windows
defaults to a 260-character path limit which is shorter than many movie
folder paths. To enable:

```powershell
# Run in an elevated PowerShell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
    -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

Then reboot. Verify with: `(Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem").LongPathsEnabled` (should return `1`).

**Python 3.10 or later.** Tested on 3.13. Install from
[python.org](https://www.python.org/downloads/windows/) or via
[winget](https://learn.microsoft.com/en-us/windows/package-manager/):

```powershell
winget install Python.Python.3.13
```

Verify after install (open a fresh PowerShell):
```powershell
py --version
pip --version
```

**ffmpeg**, which provides `ffprobe` (used by `inventory.py` and
`runtime_scan.py`):

```powershell
winget install Gyan.FFmpeg
```

Add ffmpeg's `bin` directory to your `PATH` if the installer didn't.
Verify: `ffprobe -version`.

**A TMDB API key.** Free at
[themoviedb.org/settings/api](https://www.themoviedb.org/settings/api).
Get the **v3 auth key** (32-character hex string), not the v4 bearer
token. This pipeline does *not* use the v4 token.

**A Plex Media Server** (optional but expected). Used by
`plex_scan.py` to refresh the Movies library after changes. Configure
your Movies library to point at the same `movies_root` you'll set in
`config.json`.

**Radarr and Bazarr** (optional, for ongoing management). See
[`phase5_runbook.md`](phase5_runbook.md) for setup.

### Python packages

```powershell
pip install requests blake3 guessit
```

- **requests** — HTTP client for TMDB API
- **blake3** — fast cryptographic hash for duplicate detection (falls
  back to SHA-256 if not installed, but BLAKE3 is much faster)
- **guessit** — parses messy movie filenames into title + year

### Recommended setup

1. Pick a working directory: e.g. `H:\movie-cleanup\`
2. Copy all `.py` files plus `config.example.json` there
3. Copy `config.example.json` to `config.json` and edit it (see below)
4. Make sure your library is at the path in `movies_root` (e.g. `H:\Movies`)
5. Make sure `mediaprep_dir` (e.g. `H:\_mediaprep`) exists or can be
   created — this is where all CSV outputs, logs, and the journal live
6. **Pause Plex's automatic library scan** for the Movies library
   (Settings → Library → Update my library automatically: OFF) — phase 1
   reads files but you don't want Plex to be re-scanning during phase 3
7. Add Windows Defender exclusions for `movies_root` and `mediaprep_dir`
   so AV doesn't slow down the per-file moves to a crawl

### Configuration (`config.json`)

Copy [`config.example.json`](config.example.json) to `config.json` and
fill in your values. Key fields:

```json
{
  "tmdb_api_key": "REPLACE_ME",
  "movies_root":     "H:/Movies",
  "mediaprep_dir":   "H:/_mediaprep",
  "duplicates_dir":  "H:/duplicates",
  "language": "en-US",
  "hash_algo": "blake3",
  "confidence_auto":   75,
  "confidence_review": 30,
  "filename_template": "{title} ({year}) {{imdb-{imdb_id}}}",
  "extras_subfolder": "Extras",
  "nfo_richness": "rich",
  "intake_dir": "H:/Temp",
  "plex_url": "http://localhost:32400",
  "plex_token": "YOUR_PLEX_TOKEN",
  "plex_movies_section": "Movies"
}
```

The `tmdb_api_key`, `movies_root`, `mediaprep_dir`, and `duplicates_dir`
are required. The Plex keys are only needed if you'll use `plex_scan.py`.
Paths can use forward slashes (preferred) or escaped backslashes.

**Don't commit your `config.json` to git** — it has secrets. A
[`.gitignore`](#suggested-gitignore) example is below.

---

## The pipeline at a glance

```
                  +------------------+
   H:\Movies ---> | 1. inventory.py  |    (read-only scan, ~5-10 min for ~14k files)
                  +--------+---------+
                           v
                  H:\_mediaprep\plan.csv, inventory.csv,
                                duplicates.csv, unresolved.csv
                           |
                           v
                  +------------------+
                  | 2. Manual review |    (you, in Excel — flip rows AUTO/SKIP/REVIEW)
                  +--------+---------+
                           v
                  +------------------+
                  | 3. execute.py    |    (atomic moves, ~30 min - 2 hr)
                  +--------+---------+    plan.csv → moves.jsonl + new H:\Movies layout
                           v
                  +------------------+
                  | 4. verify.py     |    (post-move integrity check, ~5 min)
                  +--------+---------+
                           v
                  +------------------+
                  | 5. plex_scan.py  |    (refresh Plex Movies library)
                  |    + Radarr +    |
                  |      Bazarr      |
                  +--------+---------+
                           v
                       (Library is healthy)
                           ^
                           |
                  H:\Temp  +
                       |
                       v
                  +------------------+
                  | 6. intake.py     |    (ongoing: new disc rips → main library)
                  +------------------+
```

---

## Scripts, in the order you'll use them

### `inventory.py`

**Phase 1: read-only scan of the existing library.**

Walks `movies_root` recursively, classifies every file (video / nfo /
subtitle / poster / extra / other), uses `guessit` to extract a title and
year from each video filename, queries TMDB for matches, computes a
target folder path using the naming template, and detects duplicate
videos via size-grouped BLAKE3 hashes.

Outputs four CSVs to `mediaprep_dir`:

- **`inventory.csv`** — per-file: file_id, abs_path, parent_folder,
  filename, ext, size_bytes, mtime, file_hash, role
- **`plan.csv`** — per-title (resolved only): title_id, source_folder,
  main_video_file, tmdb_id, imdb_id, matched_title, matched_year,
  collection_name, target_folder, confidence, action, duplicate_role,
  flags, notes
- **`unresolved.csv`** — per-title (couldn't identify): title_id,
  source_folder, main_video_file, guessit_title, guessit_year, reason,
  flags, notes
- **`duplicates.csv`** — per-dup-group: group_id, match_type,
  keeper_title_id, duplicate_title_ids, keeper_path, duplicate_paths

Plus a log file: `inventory_<timestamp>.log`.

```powershell
py inventory.py --config config.json
py inventory.py --config config.json --skip-hash       # skip dup detection (faster)
py inventory.py --config config.json --resume          # resume from state.json after crash
```

**Run time:** ~5-10 minutes for the file walk; another 5-15 minutes for
the BLAKE3 hashing if duplicate detection is enabled. The script saves
incremental state to `state.json` and is fully resumable.

**Interpreting output:**

- `Summary: 2804 titles total | AUTO=2082  REVIEW=136  UNRESOLVED=586`
  means out of 2,804 candidate title folders, 2,082 had high-confidence
  TMDB matches (action=AUTO), 136 had ambiguous matches (action=REVIEW —
  human eye needed), and 586 couldn't be matched at all (in
  unresolved.csv). The exact split depends on your `confidence_auto` /
  `confidence_review` thresholds.
- `Found N duplicate groups` is a count of dup-relationships, not a count
  of duplicate files. Each group has one keeper and one or more losers.

**Common errors:**

- `TMDB API key invalid` — you used the v4 bearer token instead of v3.
  Re-grab the v3 key.
- `Connection timeout` — TMDB is briefly unreachable; the script
  retries. Persistent failures may indicate a regional block or
  rate-limiting from too many concurrent runs.
- `UnicodeDecodeError reading NFO` — fixed in current code, but pre-fix
  versions choked on weirdly-encoded NFOs. Update your scripts.

See [`phase1_runbook.md`](phase1_runbook.md) for the full operational
checklist.

---

### Phase 2: Manual review (you, in Excel)

Not a script — this is the human-judgment step.

Open `H:\_mediaprep\plan.csv` in Excel. Walk through every row with
`action = REVIEW`. For each, decide:

- **AUTO** — yes, process this row. Optionally fix `target_folder` if
  Plex's preferred name differs from what TMDB returned.
- **SKIP** — no, leave this title alone. The row stays in the CSV for
  the record.
- **(blank/REVIEW)** — defer; will be ignored by phase 3 just like SKIP.

You can also promote rows from `unresolved.csv` into `plan.csv` by
copying them in and manually filling `tmdb_id`, `imdb_id`,
`matched_title`, `matched_year`, and `target_folder`.

**Save plan.csv as "CSV UTF-8 (Comma delimited)"**, never as plain "CSV"
— the latter mangles accented characters on Windows.

---

### `execute.py`

**Phase 3: carry out the moves.**

Reads plan.csv, inventory.csv, and duplicates.csv. Runs a multi-stage
flow per title (mkdir target, move main video, move sidecars, write rich
NFO, move extras, rmdir empty source). Every operation is journaled to
`moves.jsonl` so any change can be reversed.

**Default mode is dry-run.** You must pass `--apply` for real moves.

```powershell
py execute.py --config config.json status              # show plan progress
py execute.py --config config.json                     # dry-run
py execute.py --config config.json --apply             # real run
py execute.py --config config.json --apply --limit 50  # smoke test 50 titles
py execute.py --config config.json --apply --titles 42,87  # specific titles
py execute.py --config config.json --apply --verify-hashes # cross-drive integrity check
```

**Run time:** Most moves are same-drive renames (atomic, instant). The
bottleneck is per-file mkdir/rename overhead and TMDB rich-NFO fetches.
Realistic: 30 minutes to 2 hours for ~2,000 titles. Plan.csv is flushed
every 50 successful titles, so progress is checkpointed.

**Interpreting output:**

- `Pre-flight ERRORS: N (aborting; see preflight_errors.csv)` — fix
  those, then re-run. Common causes: missing target_folder in a row you
  edited, target_folder outside movies_root, source files moved between
  phase 1 and now.
- `Pre-flight warnings: N` — non-fatal. The script continues. Common:
  `dup_role_mismatch` (plan.csv and duplicates.csv disagree;
  the script honors plan.csv), `cross_drive_volume` (some moves cross
  drives and will be slow).
- `Titles succeeded: 1764  Titles failed: 0` — clean run.
- `Titles failed: N` — read `execute_errors.csv` for per-title detail.
  Common: Stage C source-missing (file already moved in a previous
  partial run; resume should pick up automatically), Stage B mkdir
  failures (illegal characters in target_folder — fix in plan.csv).

**Outputs:**

- `moves.jsonl` (real run) or `moves_dryrun.jsonl` (preview) — append-only
  JSON Lines, one operation per row. Source of truth for rollback.
- `execute_report.txt` — human-readable summary
- `execute_errors.csv` — failed titles with stage and error message
- `large_extras.csv` — extras ≥ 500 MB that moved to `Extras/`, flagged
  for your review (might be alternate cuts you'd prefer as separate
  library entries)
- `orphans.csv` — files in source folders that didn't match the main
  video stem and weren't moved; their source folders aren't auto-rmdir'd

See [`phase3_runbook.md`](phase3_runbook.md) for the full operational
checklist and [`phase3_design.md`](phase3_design.md) for the design
doc explaining every per-title stage.

---

### `rollback.py`

**Reverse phase 3 operations using the journal.**

Reads `moves.jsonl` newest-first and undoes each operation. Use this if
you ran `--apply` and something went wrong.

```powershell
py rollback.py --config config.json --dry-run --run-id 2026-04-27T22-41-58
py rollback.py --config config.json --run-id 2026-04-27T22-41-58
py rollback.py --config config.json --title 42                    # one title only
py rollback.py --config config.json --since 2026-04-27T18:00:00   # everything since X
```

The `run_id` is in the journal (top-level field) and in execute.py's log
filename. **Always run `--dry-run` first** to preview what will be
reversed.

**Outputs:**

- `rollback.jsonl` — your re-do log; if you want to *re-do* an undo, you
  can replay this manually
- `rollback_<timestamp>.log` — verbose log

**Interpreting output:**

- `Rollback summary: ok=3802 skipped=202 failed=0` — clean rollback. The
  "skipped" count includes journal markers (run_start/run_end), already-
  reversed entries from prior rollbacks, and operations that were
  no-ops anyway (e.g., rmdir-not-empty cases that the original journal
  recorded as expected).
- `failed > 0` — the file system state has drifted since the original
  run. The rollback script bails out at the first state mismatch rather
  than blindly clobbering. Investigate per-line.

---

### `verify.py`

**Phase 4: post-move integrity check.**

Read-only sweep that confirms what plan.csv says was executed actually
exists on disk. For each AUTO row with `executed_at` set, checks: target
folder exists, expected main video file is there, file size matches
inventory, NFO is present and contains the right tmdbid/imdbid,
sidecars made it. Plus cross-library checks for duplicate IMDB IDs,
target path collisions, etc.

```powershell
py verify.py --config config.json                       # fast: structural checks only
py verify.py --config config.json --hash-verify         # also re-hash main videos (slow)
py verify.py --config config.json --titles 42,87,143    # specific titles
py verify.py --config config.json --include-skip        # also verify SKIP rows
```

**Run time:** Fast mode is ~5 minutes for ~2,000 titles. Hash-verify
re-reads every main video; expect 2-6 hours for a 5 TB library — best
run overnight.

**Outputs:**

- `verify_report.txt` — counts by category and the first 30 errors
- `verify_issues.csv` — every issue with title_id, severity, code,
  message, path
- `verify_extras.csv` — folders/files on disk that aren't accounted for
  in plan.csv (mostly franchise parents — informational)

**Interpreting output:**

- `Titles fully verified: 2099 of 2240` — clean ratio. Anything above
  ~95% is a successful migration.
- Issue codes:
  - `target_folder_missing` — folder doesn't exist; the move failed or
    something deleted it
  - `video_missing` — folder exists but no video inside
  - `video_name_mismatch` — video exists but with a different filename
    than the template would produce; harmless if Plex still finds it
  - `size_drift` — file is on disk but size differs from inventory.
    Almost always a real corruption signal in apply mode (in dry-run
    on a partial state, can be a stale comparison)
  - `nfo_missing` — NFO not next to video. If you ran with `--no-nfo`
    intentionally, ignore.
  - `nfo_tmdb_mismatch` / `nfo_imdb_mismatch` — NFO has different IDs
    than plan.csv. May indicate a swapped match.
  - `duplicate_imdb_id` — two folders share the same IMDB ID; one is
    bogus
  - `dup_loser_missing` — a duplicate-loser file isn't at duplicates_dir
    AND isn't at its original location (manually moved or deleted)
  - `dup_loser_still_at_source` — info-level; loser was skipped due to
    plan/dups disagreement, file stays put

---

### `plex_scan.py`

**Trigger Plex Media Server to refresh a library section via API.**

Useful at the end of phase 3 to make Plex pick up the reorganized
library. Reads `plex_url`, `plex_token`, and `plex_movies_section` from
config.json.

```powershell
py plex_scan.py --config config.json --list             # show your Plex sections
py plex_scan.py --config config.json                    # incremental scan (background)
py plex_scan.py --config config.json --wait             # incremental + block until done
py plex_scan.py --config config.json --force --wait     # full re-analyze, blocking
```

**Getting your Plex token:** log in to Plex Web → click any movie →
⋯ → "Get Info" → "View XML" → look at the URL bar; the
`X-Plex-Token=...` query parameter is your token.

**Run time:** Incremental scans are usually fast (1-15 minutes
depending on library size and what changed). Full re-analyze (`--force`)
re-reads every file and can take hours.

**Common errors:**

- `Could not reach Plex at http://localhost:32400` — Plex isn't running
  or the URL is wrong
- `No section matching 'Movies'` — your section is named differently;
  use `--list` to see available names and update config.json

---

### `runtime_scan.py`

**Find TV episodes mislabeled as movies, by file duration.**

Walks the migrated library, runs `ffprobe` on every main video, and
classifies each by length. Movies in the 25-50 minute range are very
likely TV episodes; 10-25 minutes are likely shorts or animated TV
specials.

```powershell
py runtime_scan.py --config config.json
py runtime_scan.py --config config.json --titles 42,87  # specific titles
py runtime_scan.py --config config.json --all-files     # also probe untracked files
```

**Run time:** ~30-90 minutes for ~2,000 videos. ffprobe is fast (~0.1-0.5s
per file) but it has to open every file.

**Outputs:**

- `runtime_scan.csv` — every probed video with classification and
  duration in minutes, sorted by severity
- `runtime_scan_report.txt` — aggregate counts by classification

**Interpreting output:**

| Class | Range | Action |
|---|---|---|
| `trivial` | < 10 min | Probably trailer or sample, already in `Extras\`. Investigate if at title level. |
| `tv_short` | 10-25 min | Strong TV-episode signal. Could also be a Disney short or animated TV special. |
| `tv_episode` | 25-50 min | Very likely a TV episode mislabeled as a movie. Move to TV. |
| `tv_special` | 50-75 min | Gray zone — concert film, comedy special, doc episode, miniseries chunk. |
| `movie` | 75-240 min | Normal feature length. |
| `oversized` | ≥ 240 min | Possibly full-season compilation or extended-extended cut. |

The hits in `tv_short` and `tv_episode` are the ones to actually
relocate. `tv_special` entries are usually legitimate "movie-format
media" your library *should* contain.

See also the `tv_check` PowerShell snippet in
[Troubleshooting](#troubleshooting) for filename-pattern-based
detection (cheaper, complementary).

---

### `intake.py`

**Ongoing: process newly-ripped movies from a staging directory.**

Streamlined version of inventory.py + execute.py, designed for one or a
few movies at a time rather than bulk migration. Default intake
directory is `H:\Temp` (override via config or `--intake-dir`).

```powershell
py intake.py --config config.json                       # interactive
py intake.py --config config.json --dry-run             # preview only
py intake.py --config config.json --yes                 # skip confirmation
py intake.py --config config.json --plex-scan           # also refresh Plex
py intake.py --config config.json --force-low-confidence # process <70 confidence too
```

**Workflow:**

1. Rip a disc (MakeMKV, HandBrake, etc.) → drop the resulting files
   (single .mkv, or a folder with .mkv + .nfo + posters) into `H:\Temp\`
2. Run `py intake.py --config config.json`
3. Script identifies each title via TMDB (using the cache from earlier
   phases), prints a preview, asks for confirmation
4. On approval, moves files to `H:\Movies\<Title> (<Year>) {imdb-tt#######}\`
   with the same naming and NFO conventions as the bulk migration
5. Optionally triggers a Plex scan

**Outputs:**

- `intake_<timestamp>.log` — operation log
- `intake_moves.jsonl` — journal entries for the new moves (rollback-
  compatible: `py rollback.py --config config.json --journal intake_moves.jsonl --run-id <id>`)

**Interpreting output:**

- `Summary: N processed, 0 failed (mode=apply)` — done
- `Summary: 0 processed, ... titles need REVIEW` — confidence too low.
  Either rename the source file to something cleaner before re-running,
  drop a sidecar `.nfo` with the IMDB ID, or pass
  `--force-low-confidence`

**Tips for clean intake:**

- Single `.mkv` files at the root of `H:\Temp` work fine
- Folders with `.mkv` + `.nfo` + posters work — the NFO's IMDB ID is the
  most accurate match path
- DVD/BD ripper artifacts in filenames (`_Title1`, `T00`, `_Playlist_0`,
  `nogrp`, etc.) are auto-stripped before TMDB lookup
- For ambiguous titles, dropping a one-line NFO with `<imdbid>tt1234567</imdbid>`
  next to the video is the easiest fix

---

## Project structure

Recommended layout in your repo:

```
movie-cleanup/
├── inventory.py
├── execute.py
├── rollback.py
├── verify.py
├── plex_scan.py
├── runtime_scan.py
├── intake.py
├── config.example.json
├── requirements.txt
├── LICENSE                 # GPL v3 text
├── README.md               # this file
├── phase1_runbook.md
├── phase3_design.md
├── phase3_runbook.md
├── phase5_runbook.md
└── .gitignore
```

### Suggested `.gitignore`

```gitignore
# Local config — has API keys, never commit
config.json

# Generated outputs
*.log
*.jsonl
state.json
state.json.tmp

# Cache
tmdb_cache.sqlite
tmdb_cache.sqlite-journal

# Library data — these are your private metadata
plan*.csv
inventory*.csv
duplicates*.csv
unresolved*.csv
preflight_errors*.csv
preflight_warnings*.csv
execute_errors*.csv
execute_report*.txt
large_extras*.csv
orphans*.csv
verify_*.csv
verify_*.txt
runtime_scan*.csv
runtime_scan*.txt
dryrun_report*.txt

# Python
__pycache__/
*.pyc
*.pyo
*.pyd
.venv/
venv/
*.egg-info/

# Editor
.vscode/
.idea/
*.swp
.DS_Store
```

### `requirements.txt`

```
requests>=2.28
blake3>=0.3
guessit>=3.7
```

---

## Troubleshooting

### Excel mangled my plan.csv

Excel is the most common source of CSV corruption in this pipeline.
Symptoms:

- Accented characters look wrong (`Alegría` becomes `AlegrÃ­a`)
- File ends with NUL bytes
- Quoted fields lose their quotes

**Always save as "CSV UTF-8 (Comma delimited)"**, not the default "CSV
(Comma delimited)". The reader in execute.py and verify.py is
encoding-resilient and tries UTF-8, then cp1252, then latin-1, then NFC
normalization, then mojibake repair, but the safest path is to never let
Excel re-encode your file in the first place.

### Detecting TV shows mistakenly in your Movies folder

Quick PowerShell scan for filename patterns indicating TV (run this
before phase 1 to flag obvious offenders):

```powershell
$inventory = Import-Csv H:\_mediaprep\inventory.csv | Where-Object { $_.role -eq 'main_video' }
$tvPatterns = 'S\d{1,2}E\d{1,3}','s\d{1,2}e\d{1,3}','\b\d{1,2}x\d{1,3}\b',
              'Season[\s_\.]+\d+','Series[\s_\.]+\d+','Episode[\s_\.]+\d+','Ep[\s_\.]+\d+'
$inventory | Where-Object {
    $name = $_.filename
    foreach ($p in $tvPatterns) { if ($name -match $p) { return $true } }
    return $false
} | Select-Object filename, parent_folder | Export-Csv H:\_mediaprep\tv_suspects.csv -NoTypeInformation -Encoding UTF8
```

Then `runtime_scan.py` catches the rest by duration.

### "could not create target folder" at Stage B

A colon (`:`) or other illegal Windows character is in the
`target_folder` value in plan.csv. The script's sanitization handles
matched titles, but if you manually edited a row during phase 2, the
edit is preserved as-is. Find the row in plan.csv, replace `:` with
` -`, save (UTF-8), re-run.

### "main video move failed" at Stage C

The source file isn't where inventory.csv says. Two common causes:

1. **Partial re-run after a previous apply.** The previous run already
   moved the file. Pre-flight should catch this with `source_missing`.
   If it didn't, you may have hit an idempotency edge case — see
   [phase3_design.md](phase3_design.md) §10.
2. **Pass 2 (duplicates) moved the file as a loser** but Pass 3 still
   tried to process the row as a regular AUTO. Fixed in current code
   via per-iteration `executed_at` re-check. If you see this on current
   code, send the journal entry for the title.

### Pre-flight aborts with `dup_role_mismatch` warnings

(Treated as warnings as of current code, not fatal — but worth
explaining.) Plan.csv and duplicates.csv disagree about which titles
are keepers vs. losers in a dup group. Most often this happens because
inventory.py was re-run between executions and duplicate detection
landed differently. Pass 2 silently skips disagreement-flagged entries
(your phase-2 edits in plan.csv are treated as the source of truth).

If you want a pristine state, regenerate both CSVs from a fresh phase 1
run before the next phase 3 — but that means redoing your phase 2
review.

### Slow performance on Windows

If `inventory.py` or `execute.py` is taking 10x longer than the runtime
estimates above:

- Check Windows Defender exclusions for `movies_root` and `mediaprep_dir`
- Check that long-path support is enabled (registry setting + reboot)
- Confirm no other process is reading the library (Plex during a scan,
  Sonarr/Radarr indexing, etc.)
- For external/USB drives, USB 3 vs. USB 2 makes a 10x difference

### Rollback says "destination exists with different size"

A file at the rollback target already exists but doesn't match what the
journal expects. Either you re-ran execute.py and it created new state
on top of the original, or something external (Plex thumbnail
generation, Bazarr writing subtitles) added files. Investigate
manually; rollback.py refuses to clobber.

---

## Roadmap

- **TV pipeline** (phase 6 in our development naming): same overall
  shape as the movie pipeline but with episode-aware naming, season
  folders, TheTVDB integration in addition to TMDB, and a Sonarr-
  compatible output format. Coming soon.
- **Cross-platform support** (Linux/macOS): would require abstracting
  the path handling. Not currently planned.
- **GUI**: not planned. The CSV-review-in-Excel workflow is intentional
  — it scales to large libraries in a way no purpose-built GUI does
  without significant engineering.

---

## Acknowledgements

This pipeline was developed iteratively in conversation with Anthropic's
Claude Sonnet, against a real library that exposed every possible edge
case (DVD-rip filenames with embedded `_Title1` markers, Excel
double-encoding accented characters, Windows long paths, multi-title
folders, badly-named source folders that match no TMDB record, etc.).
The development conversation is documented at the blog linked in the
[Author](#author-and-blog) section.

Tools that did the heavy lifting:

- [TMDB](https://www.themoviedb.org/) — movie metadata
- [guessit](https://github.com/guessit-io/guessit) — filename parsing
- [BLAKE3](https://github.com/BLAKE3-team/BLAKE3) — fast hashing
- [Plex](https://www.plex.tv/) — media server
- [Radarr](https://radarr.video/) — movie management
- [Bazarr](https://www.bazarr.media/) — subtitle management
- [ffmpeg](https://ffmpeg.org/) — video metadata via ffprobe

---

## Contributing

Issues and pull requests welcome at the GitHub repo. Particularly
useful:

- New filename patterns the de-noiser doesn't handle (paste examples
  from real libraries)
- Edge cases in TMDB matching that confuse the scoring logic
- Bugs in the rollback script (always include the journal lines that
  triggered the issue)
- Documentation improvements
