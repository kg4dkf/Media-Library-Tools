# Movie Library Reorganization — Design Document

**Project:** `H:\Movies` cleanup and standardization
**Owner:** Brendan
**Date:** 2026-04-23
**Status:** Draft for approval
**Platform:** Windows 11, Python 3.13, PowerShell 5.1

---

## 1. Goals and Non-Goals

**Goals**

- Rename every identifiable movie to a consistent convention.
- Reorganize into a standardized folder structure.
- Identify duplicates and move them (unmodified) to `H:\duplicates\` for later manual deletion.
- Preserve and complete existing `.nfo` metadata.
- Fetch English subtitles for foreign-language films (primarily anime).
- Stand up Radarr + Bazarr for ongoing maintenance.
- Maintain full rollback capability from start to finish.
- Produce an explicit review queue for anything the script is not confident about.

**Non-Goals (explicitly out of scope)**

- Deleting duplicates — they go to `H:\duplicates\` untouched.
- Re-encoding, transcoding, or format conversion.
- Processing TV shows, music, or any non-movie media.
- Resolving movies that appear in multiple franchises automatically — these go to the review queue.

---

## 2. Naming Convention

### 2.1 Canonical filename format

Standalone movie:

```
Movie Name (YEAR) {imdb-ttXXXXXXX}\Movie Name (YEAR) {imdb-ttXXXXXXX}.<ext>
```

Franchise movie:

```
Franchise Name\Movie Name (YEAR) {imdb-ttXXXXXXX}\Movie Name (YEAR) {imdb-ttXXXXXXX}.<ext>
```

### 2.2 Naming source

- **Movie title and year:** pulled from TMDB's English-locale record for the matched `imdb_id`/`tmdb_id`. TMDB's English title is effectively the same as IMDb's canonical title in ~99% of cases.
- **IMDb ID:** `tt` + 7 or 8 digits. Pulled from the TMDB record (TMDB returns it for every movie).
- **Franchise name:** TMDB `belongs_to_collection.name`, with the word "Collection" stripped if present (e.g., "Fast & Furious Collection" → "Fast & Furious"). Stripping is a script-time transform, not a TMDB API thing.
- **Language:** English titles only, as specified. TMDB is queried with `language=en-US`.

### 2.3 Filesystem sanitization

Windows disallows `< > : " / \ | ? *` in filenames. Transformation rules:

| Source character | Replacement |
|---|---|
| `:` | ` - ` (space-hyphen-space) |
| `/` | ` ` (space) |
| `?`, `*` | removed |
| `<`, `>` | removed |
| `"` | `'` |
| `\|` | `-` |
| Trailing `.` or space | removed |

Examples:
- `Blade Runner 2049: The Final Cut` → `Blade Runner 2049 - The Final Cut`
- `M*A*S*H` → `MASH`
- `WALL·E` → `WALL-E` (middle dot normalized to hyphen)

Maximum component length: 180 chars. Full paths: long-path mode enabled (`\\?\` prefix) to bypass the 260-char MAX_PATH limit.

### 2.4 Associated file naming

Inside the movie folder:

| File type | Name |
|---|---|
| Main video file | `Movie Name (YEAR) {imdb-xxx}.<ext>` |
| NFO | `Movie Name (YEAR) {imdb-xxx}.nfo` |
| Poster | `poster.jpg` |
| Fanart/backdrop | `fanart.jpg` |
| Subtitle (English) | `Movie Name (YEAR) {imdb-xxx}.en.srt` |
| Subtitle (English, SDH) | `Movie Name (YEAR) {imdb-xxx}.en.sdh.srt` |
| Subtitle (English, forced) | `Movie Name (YEAR) {imdb-xxx}.en.forced.srt` |
| Subtitle (Japanese original) | `Movie Name (YEAR) {imdb-xxx}.ja.srt` |

Extras/bonus material goes in subfolders Plex and Kodi both recognize:

```
Movie Name (YEAR) {imdb-xxx}\
├── Movie Name (YEAR) {imdb-xxx}.mkv
├── Movie Name (YEAR) {imdb-xxx}.nfo
├── poster.jpg
├── fanart.jpg
├── Featurettes\
├── Behind The Scenes\
├── Deleted Scenes\
├── Interviews\
└── Trailers\
```

Existing extras files will be moved into the most likely subfolder based on filename heuristics (keywords like "deleted", "behind", "trailer", etc.). Ambiguous extras stay at the movie-folder root with their original names, flagged for review.

---

## 3. Pipeline Overview

The work is split into six phases. Each phase produces an artifact that is checked before the next phase runs.

| Phase | Name | Writes to disk? | Artifact |
|---|---|---|---|
| 1 | Inventory & Identification | No (read-only on `H:\Movies`; writes only to `H:\_mediaprep\`) | `inventory.csv`, `plan.csv`, `unresolved.csv` |
| 2 | Human Review | No | `plan.csv` (edited by user in Excel) |
| 3 | Execution | Yes (renames/moves on `H:\`) | `operations.log`, `rollback.ps1` |
| 4 | Verification | No (read-only) | `verify_report.csv` |
| 5 | Radarr + Bazarr Setup | Yes (app config) | Running services pointed at clean library |
| 6 | Ongoing | Yes (Bazarr pulls subtitles) | — |

Working directory for all phase artifacts: `H:\_mediaprep\`. Nothing written elsewhere on `H:\` until phase 3.

---

## 4. Phase 1: Inventory & Identification

### 4.1 What the script does

1. Walks `H:\Movies` recursively.
2. For each file, records: full path, size, extension, modification time.
3. Groups files by parent folder (the script's unit of work is a "title folder," not individual files).
4. For each title folder:
   - Identifies the main video file (largest file with a known video extension).
   - Parses any existing `.nfo` file for TMDB/IMDb IDs.
   - If no IDs found: parses folder name and filename with `guessit` to extract title + year.
   - Queries TMDB by ID (if found) or by title+year.
   - Uses `ffprobe` to read runtime and resolution from the video file.
   - Scores match confidence (see §4.3).
5. Hashes files for duplicate detection using the two-pass strategy in §4.4.
6. Emits three CSVs.

**Read-only guarantee:** Phase 1 makes zero modifications to `H:\Movies`. All script output lands in `H:\_mediaprep\`.

### 4.2 Video file extensions recognized

`.mkv .mp4 .avi .mov .m4v .wmv .mpg .mpeg .ts .m2ts .vob .iso .divx .flv .webm`

Files with other extensions inside a title folder are classified as "associated" (NFOs, subtitles, artwork, info files) and kept with the title.

### 4.3 Confidence scoring

Each title gets a score from 0 to 100:

| Signal | Points | Condition |
|---|---|---|
| IMDb/TMDB ID in existing NFO | +50 | Found and validated against TMDB |
| Title match | +25 | Guessit title matches TMDB canonical (fuzzy, Levenshtein >= 0.90) |
| Year match | +15 | Guessit year == TMDB release year |
| Year match (±1) | +8 | Year off by 1 (common for festival vs. wide-release dates) |
| Runtime match | +10 | ffprobe runtime within 5% of TMDB runtime |
| Single TMDB search result | +5 | Search returned exactly one candidate |
| Multiple TMDB results, same year | -10 | Ambiguity penalty |
| No year detectable | -15 | Have to search by title alone |
| File is in a subfolder suggesting extras | -20 | "Deleted Scenes", "Featurettes" in path |

Thresholds:

- **≥ 75: auto-apply.** Lands in `plan.csv` with `action=AUTO`.
- **50–74: review.** Lands in `plan.csv` with `action=REVIEW`, user must edit to `APPROVE` or `SKIP`.
- **< 50: unresolved.** Lands in `unresolved.csv` for manual investigation.

### 4.4 Duplicate detection (two-pass)

**Pass A — size grouping (fast).** Group all video files by exact byte size. Any file with a unique size is definitely not an exact duplicate of anything else.

**Pass B — hash verification.** For files sharing a size with at least one other file, compute BLAKE3 hash (~10x faster than SHA-256, collision-safe for this purpose). Matching hashes = exact binary duplicates.

**Pass C — content duplicates via TMDB.** After identification, any `imdb_id` mapped to more than one title folder is a content duplicate (same movie, different file). Ranking to choose which copy to keep:

1. Higher resolution (4K > 1080p > 720p > SD)
2. Higher bitrate (from ffprobe)
3. Larger file size (tiebreaker)
4. Shorter folder path (prefer cleaner-named copy)

The top-ranked copy is the "keeper." All others go to `H:\duplicates\` in phase 3, preserving their original folder structure as a subpath under `H:\duplicates\`.

### 4.5 Multi-franchise handling

When TMDB returns a movie with `belongs_to_collection` but the script detects (via additional API calls to `/collection/` search) that the movie is listed in multiple collections, the row in `plan.csv` is tagged `multi-franchise` with all candidate collection names in the `notes` column and `target_folder` left blank. User picks one during phase 2 review. Movies with only one collection are auto-populated.

### 4.6 Files the script will not touch

The script skips (and records in `skipped.csv`) any file or folder matching:

- Hidden files/folders (`.` prefix or Windows hidden attribute)
- System folders (`System Volume Information`, `$RECYCLE.BIN`, `_mediaprep`, `duplicates`)
- Zero-byte files
- Files currently open/locked by another process (retry once, then skip)

---

## 5. CSV Schemas

### 5.1 `inventory.csv`

One row per file. The raw manifest. Never edited by user; reference only.

| Column | Description |
|---|---|
| `file_id` | Incrementing integer |
| `abs_path` | Full path to file |
| `parent_folder` | Immediate parent folder name |
| `filename` | File name with extension |
| `ext` | Extension, lowercased |
| `size_bytes` | Exact size |
| `mtime` | Modification time (ISO 8601) |
| `blake3` | BLAKE3 hash (blank if single-size; only hashed in pass B) |
| `role` | `main_video`, `nfo`, `poster`, `fanart`, `subtitle`, `extra`, `other` |

### 5.2 `plan.csv`

One row per title folder. The document the user edits in Excel.

| Column | Description |
|---|---|
| `title_id` | Incrementing integer |
| `source_folder` | Current folder under `H:\Movies` (relative path) |
| `main_video_file` | Current filename of the main video |
| `tmdb_id` | Matched TMDB ID |
| `imdb_id` | Matched IMDb ID (`tt...`) |
| `matched_title` | TMDB English title |
| `matched_year` | TMDB release year |
| `collection_name` | Franchise name if any (cleaned) |
| `target_folder` | Full target path under `H:\Movies\` |
| `confidence` | 0–100 score |
| `action` | `AUTO`, `REVIEW`, `SKIP`, `APPROVE`, `OVERRIDE` |
| `duplicate_role` | `keeper`, `duplicate_of:<title_id>`, or blank |
| `flags` | Comma-separated: `multi-franchise`, `no-year`, `nfo-id-mismatch`, etc. |
| `notes` | Script-generated notes and space for user comments |

User edits this file: set `REVIEW` rows to `APPROVE` or `SKIP`, override any `AUTO` row by changing `action` to `OVERRIDE` and editing `target_folder` directly.

### 5.3 `unresolved.csv`

Same schema as `plan.csv` plus a `reason` column: `no-match`, `multiple-match-no-runtime`, `corrupt-video`, `zero-confidence`, etc.

### 5.4 `duplicates.csv`

One row per duplicate group.

| Column | Description |
|---|---|
| `group_id` | Group identifier |
| `match_type` | `exact-hash` or `same-tmdb-id` |
| `keeper_title_id` | The title_id from plan.csv that wins |
| `duplicate_title_ids` | Comma-separated losers |
| `keeper_path` | Current path of the keeper |
| `duplicate_paths` | Comma-separated current paths of losers |

---

## 6. Phase 2: Human Review

1. User opens `H:\_mediaprep\plan.csv` in Excel.
2. Filters by `action=REVIEW` and resolves each row (APPROVE or SKIP).
3. Filters by `flags` containing `multi-franchise` and picks a collection (fill in `target_folder`).
4. Reviews `unresolved.csv` separately. Can promote rows to `plan.csv` by editing them and changing `action` to `OVERRIDE`.
5. Spot-checks a random sample of `action=AUTO` rows for sanity.
6. Saves as-is (CSV format preserved).

No time pressure. No script runs during this phase.

---

## 7. Phase 3: Execution

### 7.1 Order of operations

For each `plan.csv` row where `action ∈ {AUTO, APPROVE, OVERRIDE}`:

1. Re-verify source path exists and file size matches inventory (guards against external changes between phase 1 and phase 3).
2. Create target parent folder if missing.
3. Move associated files (NFO, posters, subtitles, extras) to target folder, renaming per §2.4.
4. Move main video file, renaming per §2.1.
5. If the source folder is now empty, remove it.
6. If `duplicate_role` starts with `duplicate_of:`, the entire source folder is moved (not renamed) to `H:\duplicates\<original-relative-path>\`.
7. Append to `operations.log` and `rollback.ps1`.

### 7.2 Atomicity

- Source and destination are always on `H:\`. Moves use `MoveFile` (via `os.rename`), which is atomic on NTFS for same-volume operations.
- Never copy-then-delete. If a rename fails, the script aborts that title and moves on, logging the failure.
- Each title folder is processed as an all-or-nothing transaction. If any file in a title fails to move, the already-moved files are rolled back for that title before moving on.

### 7.3 Chunking

Processing happens in chunks of 250 titles. After each chunk:

- Summary printed to console (success count, skip count, failure count).
- `operations.log` flushed to disk.
- Optional pause for user to Ctrl-C if something looks wrong.

Script is resumable: a `state.json` file in `_mediaprep\` tracks which titles are done. Re-running the script picks up where it left off.

### 7.4 Rollback

`rollback.ps1` is built incrementally as operations succeed. Every forward operation emits its reverse. Running `rollback.ps1` later undoes every operation in reverse order. Example entries:

```powershell
# Undo: move main video
Move-Item -LiteralPath 'H:\Movies\Fast & Furious\2 Fast 2 Furious (2003) {imdb-tt0322259}\2 Fast 2 Furious (2003) {imdb-tt0322259}.mkv' -Destination 'H:\Movies\2Fast2Furious_2003.mkv'
# Undo: remove target folder (if empty)
Remove-Item -LiteralPath 'H:\Movies\Fast & Furious\2 Fast 2 Furious (2003) {imdb-tt0322259}' -ErrorAction SilentlyContinue
```

---

## 8. Phase 4: Verification

Read-only pass over `H:\Movies` after phase 3. Produces `verify_report.csv`:

- Every title folder matches the naming convention.
- Every folder contains a main video file with matching name.
- No orphan files (files whose name doesn't match their containing folder).
- No empty folders.
- `H:\duplicates\` contents count matches `duplicates.csv` expected count.
- Spot-checks: random sample of 50 titles, verify TMDB IDs still resolve.

Any anomalies flagged here are remediated manually before moving to phase 5.

---

## 9. Phase 5: Radarr + Bazarr Configuration

Only begins once phase 4 reports clean.

**Radarr:**

- Settings → Media Management → File Naming:
  - Standard Movie Format: `{Movie Title} ({Release Year}) {imdb-{ImdbId}}`
  - Movie Folder Format: `{Movie Title} ({Release Year}) {imdb-{ImdbId}}`
  - Rename Movies: OFF (we've already named them; don't let Radarr touch existing files)
- Settings → Media Management → Root Folders: add `H:\Movies`.
- Library → Import Existing Movies: let Radarr scan. It will recognize everything via the embedded IMDb IDs. No rename should occur because "Rename Movies" is off.
- Settings → Connect → Plex Media Server: add your Plex instance.

**Bazarr:**

- Settings → Languages: English (wanted), Japanese (optional if you want to keep originals).
- Settings → Providers: OpenSubtitles.com (requires free account), Subscene, Kitsunekko (for anime).
- Settings → Radarr: connect to `localhost:7878` with Radarr's API key.
- Run a manual scan. Bazarr will populate subtitles over time (rate-limited by providers).

**Plex:**

- Refresh the Movies library. Plex will re-scan with the new folder structure.
- Verify collections populate correctly from TMDB metadata.

---

## 10. Safety Measures Summary

| Measure | Purpose |
|---|---|
| Phase 1 is read-only | Nothing can go wrong during identification |
| All artifacts in `H:\_mediaprep\` | Easy to delete and restart |
| Human CSV review between identification and execution | No silent auto-apply on uncertain matches |
| Same-volume atomic renames only | No partial-write failures |
| Per-title transactions | One bad title doesn't corrupt others |
| Incremental `rollback.ps1` | Full undo capability at any point |
| Resumable via `state.json` | Survive interruption or reboot |
| Chunked processing (250/chunk) | Observable progress, easy Ctrl-C |
| BLAKE3 hash pre-flight check before each move | Detect any file changed between phases |
| Duplicates moved, never deleted | User reviews `H:\duplicates\` before any deletion |
| Long-path mode enabled | No silent MAX_PATH failures |
| TMDB response cache (`tmdb_cache.sqlite`) | Don't re-query; also makes re-runs free |
| API key in `config.json`, never in code | No credential leaks |

---

## 11. Windows-Specific Considerations

- **Path length:** Python scripts use `\\?\` prefix for all file operations. PowerShell rollback script uses `-LiteralPath` consistently.
- **Antivirus interference:** Windows Defender real-time scanning can slow hashing significantly. Recommend adding `H:\Movies` and `H:\_mediaprep` to Defender exclusions during the run.
- **Plex scanner:** Should be paused or disabled during phase 3 to avoid race conditions. Plex Settings → Library → Update my library automatically → OFF for the duration.
- **File locking:** If Plex or another app has a file open, the move fails. Phase 3 retries once after 5 seconds, then skips and logs.
- **Drive letter stability:** Script reads drive letter from config. If `H:` gets remapped mid-project, nothing good happens; drive letter should be fixed in Disk Management.

---

## 12. Prerequisites Checklist

Before phase 1 runs:

- [ ] Python 3.13 accessible via `py -3.13`
- [ ] `pip` packages: `requests`, `guessit`, `blake3`, `lxml`, `tqdm`, `pymediainfo`
- [ ] `ffprobe` installed and on PATH (ships with ffmpeg; `winget install Gyan.FFmpeg`)
- [ ] TMDB v3 API key (regenerated after the leak) stored in `H:\_mediaprep\config.json`
- [ ] Radarr installed (service stopped or root folder blank)
- [ ] Bazarr installed (service stopped)
- [ ] Plex auto-scan paused for `Movies` library
- [ ] Windows Defender exclusions added for `H:\Movies` and `H:\_mediaprep`
- [ ] Long-path support enabled (`HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1`)
- [ ] Drive letter `H:` confirmed stable

Before phase 3 runs (additional):

- [ ] `plan.csv` reviewed, saved, closed (not open in Excel during execution)
- [ ] `unresolved.csv` reviewed
- [ ] Backup of `\_mediaprep\` folder made to a second drive
- [ ] At least 10 GB free on `H:` (for transient state, not for file moves)

---

## 13. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| TMDB returns wrong match silently | Med | High | Confidence scoring + runtime verification + human review |
| Script crashes mid-execution | Low | Med | Per-title transactions + resumable state + rollback log |
| Plex re-scans mid-execution and locks files | Med | Low | Disable Plex auto-scan before phase 3 |
| Foreign-language title mismatches | Med | Low | Force `language=en-US` on TMDB queries; manual review for flagged rows |
| Filename contains characters Windows rejects | Low | Low | Sanitization table in §2.3 |
| Path exceeds 260 chars | Low | Med | Long-path mode enabled |
| Two movies legitimately share a TMDB ID | Low | Low | Duplicate resolution in §4.4, with keeper logic; user reviews `H:\duplicates\` |
| User edits `plan.csv` incorrectly | Med | Med | Phase 3 validates CSV schema; rows with malformed data skipped and logged |
| API key leaked in chat (happened) | — | Med | Regenerate before use; store locally only |
| Drive fails during execution | Low | Catastrophic | Phase 1 CSVs stored on H:\ — recommend copying `_mediaprep\` to a second drive before phase 3 |

---

## 14. Script Inventory

Five Python scripts, to be written in order:

| # | Script | Phase | Writes to `H:\Movies`? |
|---|---|---|---|
| 1 | `inventory.py` | 1 | No |
| 2 | `execute.py` | 3 | Yes |
| 3 | `verify.py` | 4 | No |
| 4 | `rollback.ps1` | on-demand | Yes (reverses #2) |
| 5 | `utils/` module | shared | — |

Script #1 is what we build next. It runs overnight, produces the three CSVs, and changes nothing on disk outside `H:\_mediaprep\`.

---

## 15. Open Questions for Approval

Please answer inline or reply with your answers:

1. **Franchise folder naming:** TMDB gives us `"Fast & Furious Collection"`. Do you want the trailing word "Collection" stripped (→ `Fast & Furious`) or kept (→ `Fast & Furious Collection`)? Default in this doc is **stripped**.

2. **Subtitles for anime:** Do you want Bazarr to also pull Japanese subtitles (for the originals) alongside English, or English only? Default is **English only** per your earlier answer, but worth confirming since you mentioned anime specifically.

3. **Existing NFOs:** If an existing NFO has a TMDB ID and TMDB returns a different canonical title than the one currently in the NFO, do you want the script to (a) update the NFO with TMDB's title and save a `.bak` of the old, or (b) leave the NFO as-is and only add missing fields? Default is **(a) — overwrite with backup**.

4. **Extras classification:** Some folders contain extras without clear labels (e.g., a file called `VTS_02_1.VOB`). Should these (a) stay in the movie folder root, (b) go into a generic `Extras\` subfolder, or (c) be flagged for review? Default is **(c)**.

5. **Hash algorithm:** I'm proposing BLAKE3 for speed. You have infosec bias toward SHA-256. Does BLAKE3 (non-cryptographic but collision-resistant and ~10x faster) work for you, or do you want SHA-256 despite the extra hours? Default is **BLAKE3**.

6. **Backup of `_mediaprep\` folder:** Before phase 3, I want to require a backup of the planning CSVs to a second drive. Do you have a drive available for this, or should we put it in a non-Plex-visible subfolder of `H:\`? Default is **on `H:\` in a hidden folder if no second drive**.

---

## 16. Timeline Estimate

| Phase | Time |
|---|---|
| 1. Inventory + identification | 6–12 hours (unattended, mostly I/O) |
| 2. Human review | Self-paced; expect 2–4 hours over several sittings |
| 3. Execution | 1–3 hours (mostly I/O; renames are fast) |
| 4. Verification | 20 minutes |
| 5. Radarr + Bazarr setup | 1–2 hours |
| Bazarr subtitle backfill | Weeks (rate-limited by providers; runs in background) |

Total active time: ~5–10 hours of attention over several days. Total elapsed: depends on how long phase 2 takes you.

---

## End of Design Document

Reply with answers to §15 and any objections or changes you want to anything in §§1–14. Once approved, I write `inventory.py`.
