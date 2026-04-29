# Phase 3 Design — `execute.py`

This is the script that actually moves and renames files. Phase 1 (`inventory.py`) was read-only and produced a plan; phase 3 carries out the plan you've reviewed and approved. Phase 2 (CSV review) is what you do between them — it's not a script, it's you in Excel.

The design priorities, in order: **safety**, **reversibility**, **idempotency**, **resumability**, then speed. If any of those conflict, the earlier one wins.

---

## 1. Inputs and contract with phase 2

`execute.py` reads four files from `H:\_mediaprep\`:

`plan.csv` is the truth-of-record. The `action` column drives behavior. Only rows where `action == "AUTO"` are processed; anything else (`REVIEW`, `SKIP`, blank) is silently ignored. This is a deliberate one-way ratchet — the human's job during phase 2 is to walk through every `REVIEW` row and either flip it to `AUTO` (approve) or `SKIP` (reject). New `SKIP` is also valid for a row that came in as `AUTO` but the human spotted as wrong.

The other plan columns drive the moves. `target_folder` is the destination folder (must be non-empty for AUTO rows), `tmdb_id` and `imdb_id` are required (used for NFO and naming template), and `duplicate_role` tells us whether this title is the keeper of a dup group, a duplicate of another keeper, or standalone.

`duplicates.csv` lists the dup groups. Each row has a `keeper_title_id` and a list of `duplicate_title_ids`. Losers get moved to `H:\duplicates\` regardless of whether their `action` in plan.csv is AUTO or not — being a duplicate-of overrides the per-title action. This avoids the case where a human accidentally leaves a known duplicate in place.

`inventory.csv` is the file-level manifest from phase 1. Phase 3 uses it to enumerate sidecar files (subtitles, NFOs, posters, extras) that travel with each title's main video file.

`config.json` is the same file phase 1 used. Phase 3 adds a few keys (see §16).

The contract going forward: **phase 3 trusts plan.csv**. If you edit a column manually — change `target_folder`, fill in a `tmdb_id` for an unresolved title, flip `action` to `AUTO` — phase 3 honors it. The `target_folder` you write is the destination it uses; phase 3 does not recompute it from `tmdb_id`. This makes manual overrides predictable.

---

## 2. The action vocabulary

Plan rows have `action` set to one of:

- `AUTO` — process this title now.
- `REVIEW` — human hasn't decided. Skip silently.
- `SKIP` — human decided no. Skip silently. The row stays in the CSV as a record. Use `notes` for context if you want to remember why.
- empty — same as REVIEW, skip.

Phase 3 treats anything other than `AUTO` as "leave alone." It does not warn or fail; it just doesn't act. This is so a re-run after triaging more rows just picks up the new AUTOs without noise.

There's a corresponding column we'll add: `executed_at`. When phase 3 successfully completes a row, it stamps the timestamp into this column and writes the CSV back. On the next run, rows with a non-empty `executed_at` are skipped (already done). This is the primary idempotency mechanism. See §10.

---

## 3. Processing order

Order matters. The script processes in three passes:

**Pass 1: pre-flight.** Validate every AUTO row before touching disk. Check the source files exist, the target folder isn't on a different drive (or warn if it is — cross-drive moves are slow), no two AUTO rows want the same target path, the destination drive has enough free space, no plan.csv row references a `target_folder` outside `movies_root`. If any preflight check fails, dump a report and exit without touching anything. The user fixes the CSV and re-runs.

**Pass 2: duplicates.** For every dup group, move losers to `H:\duplicates\<relative-path-from-movies-root>\`. This happens before the keeper plan-row processes, so by the time we move the keeper to its target folder, nothing else conflicts with the source. The move-to-duplicates preserves the original folder layout under `H:\duplicates\`, which makes manual restore trivial if you change your mind.

**Pass 3: titles.** For each AUTO row whose `executed_at` is empty, run the per-title flow (§5). On success, stamp `executed_at` and append a journal entry. On failure, attempt rollback for that title only, log the error, and continue with the next title.

The rationale for this order: if a duplicate-of relationship exists, we want the loser out of the way before the keeper moves to its new target. Otherwise a same-folder rename of the keeper might tread on a loser's files.

---

## 4. Pre-flight checks (Pass 1 detail)

The pre-flight is the hardest single piece to get right because it's the only line of defense against catastrophic mistakes. The checks, in order:

Every AUTO row must have a non-empty `target_folder`, `tmdb_id`, `imdb_id`, and `matched_title`. Missing values mean the human edited something incompletely.

Every `target_folder` must be a child of `movies_root` (e.g. `H:\Movies\...`). Anything outside is rejected — we won't move files off the configured library root by accident.

Every source file referenced in inventory.csv must still exist on disk and have the same size. If the user moved/deleted/edited files between the phase 1 run and phase 3, we abort and ask them to re-run phase 1.

No two AUTO rows may resolve to the same final video path. Two titles wanting the same destination folder is fine if they're a franchise (different filenames), but two titles wanting the same exact filename is a conflict — likely a duplicate that phase 1 missed. Report and abort.

`H:\duplicates\` must exist and be writable. Same for the parent of every distinct `target_folder`.

Free space on `H:\` must exceed the total bytes that will be cross-drive-copied (which is zero if everything stays on `H:\`). On same-drive renames, no extra space is needed.

If pre-flight fails, the script writes `H:\_mediaprep\preflight_errors.csv` (one row per problem) and exits with code 2. No file operations have happened yet.

A `--skip-preflight` flag exists for the case where you've fixed something the script couldn't auto-detect, but using it is on you.

---

## 5. Per-title processing flow

For one AUTO title, the flow has six stages. Each stage is journaled. If any stage fails, the rollback unwinds all stages already done for that title.

**Stage A: classify the source files.** Look up every file in inventory.csv whose `parent_folder` is under the title's source folder. Classify each: main video (already known from inventory), subtitle (`.srt .ass .ssa .sub .idx .vtt`), nfo (`.nfo`), poster/fanart (`poster.jpg`, `fanart.jpg`, `<videoStem>-thumb.jpg`, `folder.jpg`, `cover.jpg`), extra-video (additional video files — both small and large, both move to `Extras/`), and miscellaneous (`.txt .url .lnk .par2 .sfv` etc.). Files in the source folder whose stem does *not* match the main video's stem and aren't a known special name (poster.jpg, etc.) are classified as **orphans** and are not moved (see §6).

**Stage B: ensure target folder.** Create `target_folder` if it doesn't exist. If the target_folder's parent is a franchise folder that doesn't exist yet (e.g. `H:\Movies\James Bond\` for a Bond movie), create that too. Use the long-path-aware mkdir.

**Stage C: move main video.** Compute the destination filename from the naming template: `<MatchedTitle> (<Year>) {imdb-tt#######}<ext>` where `<ext>` is the source's extension preserved in lowercase (`.mkv`, `.mp4`, `.avi`). Sanitize the title for Windows-forbidden chars (`< > : " / \ | ? *`) — replace each with a configurable substitute (default: nothing for the colon and quote, `-` for `/` and `\`, single-space-collapsed). Move the file. On same drive, this is `os.rename` (atomic). On cross-drive, it's `shutil.move` (copy+delete, not atomic — journal the copy and the delete separately).

**Stage D: move sidecars.** For every subtitle, NFO, and poster/fanart file in the source title folder whose stem matches the main video's stem (or whose name is a known special like `poster.jpg`), move it to the target folder with a filename derived from the new video's stem. Subtitles preserve any language suffix (`movie.en.srt` → `<NewName>.en.srt`). Multiple subs in the same language get a numeric suffix. Posters keep their conventional name (`poster.jpg`, `fanart.jpg`, `banner.jpg`).

**Stage E: write/update NFO.** Generate a rich Kodi-compatible `.nfo` (XML) with `<movie>` root containing `tmdbid`, `imdbid`, `title`, `originaltitle`, `year`, `runtime`, `plot`, `tagline`, `genre` (multiple), `studio` (multiple), `country`, `mpaa`, `rating`, `votes`, `premiered`, and a `<set>` entry (with `<name>` and `<overview>`) if the title belongs to a TMDB collection. Filename: `<NewVideoStem>.nfo`. If a `.nfo` is already in the target folder (from a sidecar move in Stage D), compare contents. If they encode the same `tmdbid`/`imdbid`/`title`/`year`, leave the existing one alone. If they conflict, rename the existing one to `<stem>.nfo.bak` and write the new one. Plex prefers `<videoStem>.nfo`; we standardize on that. The TMDB metadata for the rich fields is fetched at execute time (cached in the existing TMDB SQLite cache from phase 1, so re-runs are free).

**Stage F: move extras.** All extra video files move to `<target_folder>\Extras\` with their original filenames preserved (sanitized for Windows, but no rename to the new naming template — extras keep their identity). Large extras (≥ 500MB) move identically *and* get an entry in `H:\_mediaprep\large_extras.csv` for you to review later — these are the ones likely to be alternate cuts or full-length featurettes you might want to promote to a separate library entry. Reviewing that CSV is post-run housekeeping; phase 3 doesn't block on it.

**Stage G: clean up source folder.** If the source folder is now empty (all known files moved, no orphans remaining), `rmdir` it. If orphan files remain (random sidecars whose stems didn't match the main video — see §6), leave the folder in place with the orphans inside, and log the folder path to `H:\_mediaprep\orphans.csv` so you can review later. We never delete a non-empty source folder.

After all stages succeed, stamp `executed_at` in the in-memory plan row and add the title to the "completed in this run" set. The plan.csv gets written back to disk in batches (every 50 titles or at exit) so a crash doesn't lose more than a batch's worth of timestamps.

---

## 6. Sidecar matching

The hardest classification call is "which files in this folder belong to this video?" The rule:

Files whose stem (filename without extension) starts with the main video's stem — case-insensitive, after stripping common quality tags like `1080p`, `720p`, `BluRay`, `x264`, etc. — are sidecars. So `21 (2008) 480p MPEG Audio.srt` matches `21 (2008) 480p MPEG Audio.avi` because the stems are identical.

Files with conventional special names (`poster.jpg`, `fanart.jpg`, `folder.jpg`, `<videoStem>-thumb.jpg`, `<videoStem>-poster.jpg`) are sidecars regardless of stem.

`*.nfo` files in the title folder are sidecars (there's typically exactly one).

Subtitle language detection: if the stem matches but there are extra dot-segments before the extension (`movie.en.srt`, `movie.eng.forced.srt`), those segments are preserved. We don't try to interpret them — they pass through untouched.

`.idx` and `.sub` files come in pairs (DVD subtitles); we move them together.

For DVD/Blu-ray rips with multiple `Title*.mkv` files in one folder, only the file marked as `main_video` in inventory moves to the title's renamed video path. The others classify as extras and move to `Extras/`. If one of them is the same size as the main, it's almost certainly a duplicate and phase 1 should have caught it — but defensively, we log a warning if we see ≥2 video files of the same size in one source folder.

**Orphan files.** Files in a title's source folder that don't match the main video's stem and aren't a known special name (poster.jpg, fanart.jpg, etc.) are *orphans*. Per your decision, orphans stay where they are — phase 3 does not move them, rename them, or move them to a separate folder. Stage G then declines to rmdir the source folder (since it's not empty), and the source folder + its orphan(s) are logged to `H:\_mediaprep\orphans.csv` for your manual review. This means after a successful run, any leftover source folder under `H:\Movies\` that isn't a target_folder for some other AUTO row contains orphans you should look at.

---

## 7. Naming and sanitization

The full naming template is `<MatchedTitle> (<Year>) {imdb-tt#######}` with the extension appended for files. Folder names omit the extension.

Windows-forbidden characters get the following treatment: `<` → `(`, `>` → `)`, `:` → ` -` (space-dash), `"` → `'`, `/` → `-`, `\` → `-`, `|` → `-`, `?` → (removed), `*` → (removed). Trailing periods and spaces are stripped (Windows silently drops them anyway). Multiple spaces collapse to one. Reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`) get a trailing underscore — extremely unlikely to hit, but defensive.

The `imdb-tt#######` segment uses the IMDb ID exactly as TMDB returned it, which includes the `tt` prefix. This is the format Plex's "agent" parser recognizes for unambiguous matching.

For collection/franchise folders (`<franchise>\<movie>\<file>`), the franchise folder name gets the same sanitization. The TMDB collection's "Collection" suffix is stripped per your earlier decision (already done in phase 1; phase 3 just uses the `target_folder` value verbatim).

The naming choices are deliberately the same as Radarr's default — when you wire up Radarr in phase 5 and point it at this library, it'll recognize these folder/filename patterns natively without you having to retrain it.

---

## 8. Path handling on Windows

Every filesystem call goes through a wrapper that adds the `\\?\` long-path prefix for paths over 240 characters and converts forward slashes to backslashes. Drive-letter normalization too — `H:/Movies/x` and `H:\Movies\x` should resolve identically.

For network or external drives we don't handle anything special; the long-path prefix works on UNC paths too (`\\?\UNC\server\share\...`).

`shutil.move` is the workhorse for cross-drive moves. For same-drive moves we prefer `os.rename` because it's atomic and instant. The script detects this by comparing the source and target's drive letters before calling.

A pathological case: the user's plan.csv has `target_folder` on a different drive than the source. We don't refuse this, but the pre-flight will warn (and add to a slow-operations summary) so the user can decide whether to wait for terabytes of copying. If they really want to spread the library across drives, that's their call.

---

## 9. The moves journal

Every disk operation gets one line in `H:\_mediaprep\moves.jsonl`, appended atomically, flushed after every line. Format is one JSON object per line, fields:

```
{
  "ts": "2026-04-26T20:00:00.123Z",
  "run_id": "2026-04-26T19-55-00",
  "title_id": 42,
  "stage": "C",
  "op": "rename",
  "from": "H:\\Movies\\21 (2008)\\21 (2008) 480p MPEG Audio.avi",
  "to":   "H:\\Movies\\21 (2008) {imdb-tt0478087}\\21 (2008) {imdb-tt0478087}.avi",
  "size": 734003200,
  "drive_same": true,
  "result": "ok"
}
```

`op` is one of: `mkdir`, `rename`, `copy`, `delete`, `rmdir`, `write_file` (for NFO writes), `backup` (for `.nfo` → `.nfo.bak` renames), `chmod` (rare, for read-only file handling). 

`result` is `ok`, `error`, or `skipped` (for idempotent no-ops where the source already lived at the target). On error, an additional `error` field has the exception message and class. The journal grows monotonically — it's never rewritten or compacted. After a typical run for your library, it'll be roughly 10–20 MB of JSONL.

The journal is the source of truth for rollback (§13). It's also useful as an audit trail — you can trivially answer "where did file X end up" or "what got moved on day Y" by grepping it.

`run_id` is a UTC ISO timestamp set at script startup. Every operation in a single execution shares one run_id. This makes per-run rollback (rollback only the most recent run) easy.

---

## 10. Idempotency

Re-running phase 3 should be safe. The mechanisms:

The primary check is the `executed_at` column in plan.csv. Rows with a non-empty timestamp are skipped silently. This means the human can re-run after triaging more REVIEW rows and only the newly-AUTO'd titles will get processed.

The secondary check is per-stage existence verification. If Stage C is told to rename file X to Y, and Y already exists with the right size, we treat that as success (`result: "skipped"` in the journal) and continue to Stage D. This handles partial-completion cases — a crash in Stage E means Stages A-D already happened and shouldn't repeat.

The tertiary check is content hashing. For high-stakes operations (NFO write where the existing file already has content), we compare a quick hash before deciding to overwrite. If the existing NFO is byte-identical to what we'd write, we skip the write entirely.

What we explicitly don't try to do: detect that a file was moved by some other process to a different location and we should "find" it. If the source file in inventory.csv is gone and the target file doesn't exist, we log an error for that title and move on — no auto-recovery.

---

## 11. Resume / partial completion

`--resume` is implicit — the script always reads the in-progress `executed_at` column on startup. There's no explicit resume mode; every run is "process whatever isn't done yet."

A partial title (some stages done, some not) is detected by looking at the journal: the last `run_id` that touched this title_id, what stages succeeded under that run_id, and whether `executed_at` is set. If the journal shows Stages A-D succeeded and there's no E entry, on the next run the script picks up at Stage E for that title.

To be very concrete about what "picks up at Stage E" means: Stage E checks whether the target NFO exists with the right content (idempotency check from §10), and if not, writes it. So even without explicit stage-tracking in plan.csv, the per-stage idempotency makes restart-from-anywhere correct. The journal-based tracking is a belt-and-suspenders detail for diagnostics.

Crash recovery is therefore: re-run, and let the per-stage existence checks figure it out. No manual surgery on state files needed.

---

## 12. Dry-run mode

The default mode (no `--apply`) does the full pre-flight, then walks the per-title flow but replaces every disk-mutating operation with a journal entry that has `result: "dry-run"` and the path/op it *would* have done. Reads are real; writes are pretended.

The journal in dry-run mode goes to `H:\_mediaprep\moves_dryrun.jsonl` (separate file) so it doesn't pollute the real audit log.

A diff-style summary report goes to `H:\_mediaprep\dryrun_report.txt`: one line per title showing source → target, plus a summary at the bottom (total titles, total bytes moved, total cross-drive bytes, number of NFO writes, number of dup-relocations, large-extras flagged for review, orphan folders that will be left in place).

You should always run without `--apply` first, then add `--apply` once the report looks right. The runbook will spell this out.

---

## 13. Rollback

Rollback is a separate small companion script, `rollback.py`, that reads `moves.jsonl` in reverse and undoes operations.

`py rollback.py --run-id 2026-04-26T19-55-00` undoes every operation tagged with that run_id, in reverse order. It validates as it goes — if the "destination" of the original operation isn't where the journal says it is (someone moved it manually), it stops and asks. It does not blindly clobber.

`py rollback.py --title 42` undoes operations for a single title (the most recent run that touched it). Useful when you've run, noticed one title got mismatched, and want to put just that one back.

`py rollback.py --since 2026-04-26T18:00:00` is the nuclear option — undo everything since a timestamp.

Rollback writes its own `rollback.jsonl` so you can trivially re-do an undo if you change your mind again.

The rollback script handles `rename` (reverse it), `copy`+`delete` pairs (reverse the delete first, then re-run the copy in reverse — i.e., copy back from target to source, then delete the target), `mkdir` (rmdir if empty), `rmdir` (mkdir), `write_file` (delete the file we wrote; but if a `.bak` was created in the same operation pair, restore the .bak), and `backup` (rename the .bak back).

There are operations rollback can't perfectly undo: an NFO that was overwritten without a `.bak` (didn't happen in our flow because Stage E always backs up conflicts), or a cross-drive move where the source drive has since had bytes overwritten in the freed space (recovery still works because we journaled the full path; we just copy back). In practice, the moves we do are simple enough that rollback is reliable.

---

## 14. Error handling

Errors are categorized:

**Per-title errors** (file locked, individual permission denied, target folder hit a path-length wall) get logged with the title_id, the stage that failed, and the error message. The script attempts per-title rollback (undo whatever stages already ran for this title), then continues with the next title. End-of-run summary tells you which titles failed.

**Global errors** (config invalid, drive not present, journal can't be written, pre-flight failures) abort the whole run with a clear message. No partial work happens before pre-flight passes.

**Inconsistencies** (a source file referenced in inventory.csv has a different size than recorded — meaning someone modified it between phase 1 and 3) abort that one title with an "inventory drift" error. The user is prompted to re-run phase 1 or manually verify.

Every error includes: title_id, source path, target path (if known), stage, error class, error message. Errors go to both the log file and a CSV summary at exit (`H:\_mediaprep\execute_errors.csv`).

The script exit codes: 0 = full success, 1 = some titles failed (rest succeeded), 2 = pre-flight failed (no work done), 3 = global error (config, etc.), 130 = user Ctrl-C.

---

## 15. CLI

```
py execute.py --config config.json [options]

Options:
  --apply                REQUIRED for real run. Without this flag, the script
                         dry-runs and writes dryrun_report.txt. No files are
                         touched without --apply.
  --limit N              Process only N AUTO titles (smoke testing)
  --titles ID,ID,ID      Process only specific title_ids
  --skip-duplicates      Skip dup pass (testing only)
  --skip-preflight       Bypass pre-flight (use after manual fixes)
  --no-nfo               Skip NFO generation (you write your own)
  --keep-source-empty-dirs   Don't rmdir empty source folders
  --verify-hashes        After cross-drive copies, hash both ends before
                         deleting source. Slow; off by default.
```

Subcommands for housekeeping (could be the same script or a sister `tools.py`):

```
py execute.py status     # Print: AUTO ready, AUTO done, REVIEW pending, errors
py execute.py undo --run-id <id>
py execute.py undo --title <title_id>
```

Defaults are conservative: with no flags, the script dry-runs. The real run requires explicit `--apply`. There's no `--dry-run` flag because dry-run is the default — you opt into the destructive behavior, never the safe one.

---

## 16. Config additions

Three new keys added to `config.json`:

```json
{
  "extras_subfolder": "Extras",
  "duplicates_root": "H:/duplicates",
  "nfo_format": "kodi",
  "nfo_overwrite_policy": "backup-on-conflict",
  "nfo_richness": "rich",
  "filename_template": "{title} ({year}) {{imdb-{imdb_id}}}",
  "long_path_prefix": true,
  "rmdir_empty_sources": true,
  "verify_hashes_default": false,
  "large_extra_threshold_mb": 500
}
```

The `filename_template` lets you customize without code changes — for example, you might prefer `{title} ({year}) [tmdb-{tmdb_id}]` if you switch to TMDB IDs as the canonical key. Built-in placeholders: `{title}`, `{year}`, `{imdb_id}`, `{tmdb_id}`, `{matched_title_lowercase}`. Curly-brace literals require `{{` and `}}`.

`nfo_format: "kodi"` for now; `"plex"` would emit a slightly different XML (Plex agents prefer fewer fields). I'd start with kodi since it's a superset and Plex reads it. `nfo_richness: "rich"` is your choice — embeds plot, cast, ratings, genres, etc. `"minimal"` would emit only the IDs.

`verify_hashes_default: false` keeps cross-drive verification off unless you pass `--verify-hashes` on the command line.

---

## 17. Verification report (post-run)

After a real (non-dry) run completes, the script writes `H:\_mediaprep\execute_report.txt` summarizing:

- Total AUTO titles processed (succeeded / failed / skipped-already-done).
- Total dup groups handled.
- Total bytes moved (split by same-drive-rename vs cross-drive-copy).
- Total NFOs written, total NFOs backed up.
- Total source folders rmdir'd.
- Total large-extras flagged for review.
- Wall-clock time per phase.
- List of error title_ids with one-line summaries.

This is the document you'd send me when reporting how phase 3 went.

A separate machine-readable summary goes to `execute_summary.json` for any subsequent tooling (phase 4, phase 5).

---

## 18. Decisions locked in

The eight open questions are resolved as follows. All locked in by user as of 2026-04-27.

**D1. Default execute mode.** Dry-run by default. Real run requires explicit `--apply`. There is no `--dry-run` flag — its absence *is* dry-run. This is the safest possible default and matches the project priority ranking (safety > reversibility > idempotency > resumability > speed).

**D2. Action vocabulary.** Only `AUTO`, `REVIEW`, `SKIP`, and empty (= REVIEW). No `HOLD`. The `notes` column in plan.csv captures any context the user wants to remember.

**D3. NFO content depth.** Rich Kodi-compatible NFO. Includes IDs, title, originaltitle, year, runtime, plot, tagline, genre(s), studio(s), country, MPAA, rating, votes, premiered, and `<set>` for collection members. TMDB metadata for the rich fields is fetched at execute time (cached in the existing TMDB SQLite cache, so repeated runs are free).

**D4. Extras handling.** All extra video files (regardless of size) move to `<target_folder>\Extras\` along with their sidecars. Files ≥ 500 MB additionally get logged to `H:\_mediaprep\large_extras.csv` for post-run review — these are the candidates you'll eyeball to decide whether any deserve promotion to a separate library entry. Threshold is configurable via `large_extra_threshold_mb`.

**D5. Empty source folders.** Auto-rmdir on success (default `true`). Override via `rmdir_empty_sources: false` if you ever want to keep the breadcrumb trail.

**D6. Orphan files.** A file in the source folder whose stem does not match the main video's stem and isn't a recognized special name (poster.jpg, fanart.jpg, etc.) is an *orphan*. Orphans **stay where they are** — phase 3 does not move, rename, or relocate them. Stage G then declines to rmdir the (now non-empty) source folder. The folder + its orphans are logged to `H:\_mediaprep\orphans.csv` for manual review. Files that *do* match the video stem move with it as sidecars under the new naming.

**D7. Plex library scan.** Manual. Phase 3 does not call the Plex API. Triggering the scan moves to phase 5, where we'll have wired up Radarr/Bazarr too and can decide on a coordinated post-migration scan strategy.

**D8. Hash verification on cross-drive moves.** Off by default. The user can opt in per-run with `--verify-hashes`. Rationale: virtually all moves in this migration are same-drive renames within `H:\Movies` (atomic via `os.rename`, no integrity risk). Cross-drive moves are rare and only happen if the user chose a target on a different drive. When they do happen, `shutil.move` uses Python's chunked copy which already validates each block at the OS level; a post-copy hash adds extra confidence at the cost of doubling the I/O. The flag exists; the default is off.

---

## 19. What I'm NOT designing for phase 3

Out of scope and intentionally so:

Subtitle download — Bazarr's job, phase 5.
Plex metadata refresh beyond the optional library-scan trigger.
Re-encoding or transcoding — never.
Anti-malware / file integrity — Defender exclusion is enough.
Making this work for TV shows or anime — different naming convention, separate phase if you ever do that.
Concurrent processing — all moves serial, single-threaded. Even if 8x faster were possible, the safety/auditability trade-off is not worth it for a one-time migration.
Pretty progress UI — tqdm progress bar like phase 1, that's it. Not a TUI.

---

## 20. Risks ranked

**High.** Misidentified AUTO row that the human didn't catch in phase 2 review. A movie with high confidence but wrong TMDB match gets moved to the wrong target folder under the wrong name. The journal+rollback covers this — undo, re-classify, re-run — but the user-experience impact is "I just lost track of a movie for a few minutes."

**High.** Power loss / Windows update reboot mid-run on a cross-drive copy. Depending on timing, you can end up with the source deleted but the target only partially written, or two copies (source intact, target partial). The journal records the copy-then-delete pair separately so rollback can recover, but it's the messiest crash mode. Mitigation: same-drive renames (which is the default for `H:\Movies` → `H:\Movies\...`) are atomic and immune to this.

**Medium.** Long-path edge cases. Some Windows API calls don't honor `\\?\` even with the registry setting. Mitigation: the long-path-aware wrapper, plus pre-flight rejecting any target path > 240 characters as a final guard.

**Medium.** A file open in Plex (someone is watching a movie) at the moment we try to move it. Windows file-locking is harsh. Mitigation: pre-flight tries to open each source file briefly with read-share; if the open fails, defer that title to end-of-run with a single retry. Final retry failure → that title becomes an error, others continue.

**Low.** Disk full mid-cross-drive-copy. Mitigation: pre-flight space check covers same-run cross-drive moves. Edge case if other processes write to the drive concurrently — out of our control.

**Low.** Plan.csv corruption (the CSV writer locks file open in Excel during a re-run). Mitigation: the resilient CSV writer from phase 1 is already in place.

**Low.** Filename collision after sanitization. Two different TMDB titles that sanitize to the same string. Mitigation: pre-flight catches duplicate target paths.

---

## 21. Implementation plan (after design sign-off)

In the order I'd write the code:

1. Path/sanitization library (small, standalone, unit-testable).
2. Journal append-only writer with run_id tagging.
3. NFO XML generator.
4. Per-title state-machine driver, with stages as separate functions returning success/failure cleanly.
5. Pre-flight checks.
6. Main loop, dry-run mode.
7. Real-mode execution.
8. Companion `rollback.py`.
9. Verification report writer.
10. Phase 3 runbook (analogous to `phase1_runbook.md`).

Estimated 1,000–1,500 lines of Python total. Should be quicker than phase 1 because the patterns are simpler — no API calls, no fuzzy matching, just disciplined file moving with transactional logging.

---

## Status

Design signed off 2026-04-27. Implementation order per §21.
