# Phase 3 Runbook — `execute.py`

This is the operational checklist for actually moving files. Phase 1
(`inventory.py`) produced the plan; phase 2 was you reviewing the CSVs in
Excel; phase 3 is the script doing the work you approved.

The script is **dry-run by default**. You have to explicitly add `--apply`
to make it touch files. Treat that flag the way you'd treat `rm -rf` — it's
intentional, not a typo defense, and the journal exists so you can undo it,
but a thoughtless `--apply` on the wrong machine still costs you minutes.

## Prerequisites

Before you start, confirm:

- Phase 1 has completed and produced `H:\_mediaprep\plan.csv`,
  `inventory.csv`, `duplicates.csv`. Check timestamps match a recent run.
- You've reviewed `plan.csv` in Excel and either (a) flipped any `REVIEW`
  rows to `AUTO` (approve), `SKIP` (reject), or left as-is (defer), and
  (b) sanity-checked the `target_folder` column on a sampling of rows.
- `unresolved.csv` rows that you want included have been moved into
  `plan.csv` with `action=AUTO` and a manually-filled `tmdb_id`,
  `imdb_id`, `matched_title`, `matched_year`, and `target_folder`.
- Plex's auto-scan for the Movies library is paused (Settings → Library →
  Update Library Automatically: off).
- Anti-virus (Windows Defender) has `H:\Movies` and `H:\_mediaprep` in
  its exclusion list, or is set to a non-real-time mode for the duration.
- `H:\duplicates\` exists and is on the same drive as `H:\Movies\`.
- `config.json` has all the phase 3 keys (`extras_subfolder`,
  `nfo_richness`, `filename_template`, etc.). See `config.example.json`.
- Python 3.13 (`py --version`), `requests` and `blake3` packages installed
  (`pip install requests blake3`).
- Plex isn't actively playing a file from `H:\Movies\` (file locks).

## Step 1 — Status check

```
py execute.py status --config config.json
```

Prints the count of AUTO-pending, AUTO-done, REVIEW, and SKIP rows. The
AUTO-done count should be 0 on a fresh run; if it isn't, that's the
checkpoint of a previous interrupted run, and you can either let phase 3
continue from there or run rollback first.

## Step 2 — Dry-run (mandatory)

```
py execute.py --config config.json
```

No `--apply`, so nothing is written to disk except the journal
(`H:\_mediaprep\moves_dryrun.jsonl`) and the report
(`H:\_mediaprep\dryrun_report.txt`). Every move that *would* happen is
recorded. Pre-flight runs first; if it fails, the script exits with code 2
and writes `preflight_errors.csv` — fix those, re-run.

What to look at in the dry-run report:
- **Pre-flight warnings.** Cross-drive volume? Long paths? These won't
  fail the run but you should know about them.
- **Title count.** Does it match what you saw in `status`?
- **Bytes moved.** Should be roughly the size of your library if everything
  is a rename. Should be near-zero "cross-drive bytes" if all moves stay
  on `H:\`.
- **Errors / failures.** A title can fail in dry-run if its main video is
  missing on disk now (you moved it manually after phase 1?) or its
  `target_folder` collides with another title's. Those are the same errors
  that would block a real run; address them before `--apply`.
- **Large extras flagged.** A heads-up of which extras (≥ 500 MB) will
  move to `Extras\` and be logged for your post-run review.

## Step 3 — Spot-check a few titles

Optional but recommended. Pick 3–5 known-tricky titles (a franchise, one
with subtitles, one with a custom NFO) and run them in isolation:

```
py execute.py --config config.json --titles 42,87,143,901
```

Still dry-run. Look at the journal entries for those titles in
`moves_dryrun.jsonl` and verify the destination paths are exactly what
you'd type by hand.

## Step 4 — The real run

When the dry-run looks right:

```
py execute.py --config config.json --apply
```

Or, if you want the belt-and-suspenders cross-drive integrity check
(slow; off by default):

```
py execute.py --config config.json --apply --verify-hashes
```

The script processes titles serially, single-threaded, and writes the
journal to `H:\_mediaprep\moves.jsonl` (note: not `_dryrun`). Plan.csv
gets `executed_at` stamped on each successful row, flushed to disk every
50 titles. Don't open plan.csv in Excel during the run — the script's
write-to-temp fallback handles it but it's ugly.

If you Ctrl-C mid-run, plan.csv flushes before exit. The next run picks
up from the next pending AUTO row — already-executed rows are skipped
silently.

Expected duration: roughly the time of one full disk read of the library
(same-drive renames are near-instant; the bottleneck is mkdir, NFO
writes, and the per-title TMDB rich-data fetches). For 5,000 titles
where most are renames, a few hours is realistic. For mostly cross-drive
copies, multiply by your sustained drive throughput.

## Step 5 — Post-run review

Three CSVs to look at:

`H:\_mediaprep\large_extras.csv` — every extra video file ≥ 500 MB that
got moved into `<target>\Extras\`. Each is a candidate for promotion to
its own library entry (alternate cuts, full-length featurettes, etc.).
For each row, decide: leave as extra, move into the parent movie folder
as the new main video, or delete.

`H:\_mediaprep\orphans.csv` — every source folder that wasn't deleted
because it still contained orphan files (sidecars whose stems didn't
match the main video). Walk through these manually; either delete the
orphan, rename it to match its real movie, or move it where it belongs.
After cleanup the empty source folders can be deleted by hand.

`H:\_mediaprep\execute_errors.csv` — any title that failed mid-process.
Each row gives the title_id, the stage that failed, and the error
message. Most common causes: file locked (close Plex), path too long
(rename or move to a shorter parent), permission denied (run as
admin or fix ACL).

## Step 6 — Verify before phase 4

Spot-check 5–10 titles by opening the new folder structure in
Explorer. Confirm:
- Folder name matches the template (`<Title> (<Year>) {imdb-tt#######}`)
- Main video is renamed to match
- Sidecars (subs, posters) are alongside, also renamed
- `.nfo` exists and has the right tmdbid/imdbid
- For franchise members, the parent collection folder exists and groups
  them correctly
- `Extras\` subfolder exists where applicable

If any title looks wrong, undo it with rollback.py (see below) and figure
out why before proceeding.

When the spot-check passes, phase 3 is done. Phase 4 (`verify.py`) will
mass-check every title programmatically.

## Rollback

If something is wrong after a real run, undo it:

Undo the entire most recent run:
```
py rollback.py --config config.json --run-id <run_id>
```

The run_id is in `moves.jsonl` (top-level field) and in the log file's
header (`execute.py vX.Y starting; log file: ...execute_<run_id>.log`).

Undo a single misidentified title:
```
py rollback.py --config config.json --title 42
```

Find the title_id in plan.csv. The most recent run that touched it gets
unwound. Re-run with a fixed plan after.

Preview a rollback without doing it:
```
py rollback.py --config config.json --run-id <run_id> --dry-run
```

The rollback writes its own `rollback.jsonl` so you can re-do an undo if
you change your mind again.

## Troubleshooting

**Pre-flight fails with `target_outside_root`.** A `target_folder` column
points outside `H:\Movies\`. Search plan.csv for the title_id, fix it,
re-run.

**Pre-flight fails with `duplicate_target`.** Two AUTO rows want the same
final filename. Almost always means phase 1 missed a duplicate. Manually
investigate; most likely flip one row to `SKIP`.

**Pre-flight fails with `source_missing` or `size_drift`.** A file was
moved or modified between phase 1 and phase 3. Either re-run phase 1, or
fix the file and re-run phase 3.

**Title fails Stage C with FileExistsError.** Destination already has a
file with a different size — likely a previous run's partial result.
Investigate the path, manually clean it up, re-run.

**Title fails Stage E with NFO write failed.** Disk full, or a permission
issue on the target folder. Check, retry.

**A bunch of titles fail with `file locked`.** Plex or Windows Search is
holding the files open. Stop Plex, pause Search, retry.

**Long paths.** Pre-flight warns at 240+ chars. If a real run still hits
a Windows path-length wall, enable LongPathsEnabled in the registry
(see phase 1 prerequisites) and reboot, then retry.

**Re-running after a partial.** Just run again. The `executed_at` column
in plan.csv is the resume marker; the per-stage idempotency in execute.py
handles partials. No manual surgery on state files needed.
