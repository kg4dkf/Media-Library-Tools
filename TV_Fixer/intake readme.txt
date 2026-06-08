================================================================================
  TV INTAKE -- README
================================================================================

  WHAT THIS IS

  intake_tv.py is the "front door" for adding new TV episodes to your Plex-
  formatted library at G:\TV. You drop new files into an intake folder, run
  the tool, and they end up in the right show folder with the right name,
  with quality-comparison handling for duplicates and (optionally) artwork
  for any new show.

  Run-Intake.ps1 is a friendly Windows Explorer wrapper around it. Double-
  click to get a guided dry-run-then-confirm workflow.


================================================================================
  FIRST-TIME SETUP
================================================================================

  1. Python venv (one-time)
  ---------------------------
     Open PowerShell in H:\movie-cleanup\TV_Fixer\ and run:

         python -m venv .venv
         .\.venv\Scripts\Activate.ps1
         pip install -r requirements.txt
         pip install guessit send2trash yt-dlp openpyxl

  2. External tools (one-time, must be on PATH)
  ---------------------------------------------
     - ffmpeg + ffprobe   https://www.gyan.dev/ffmpeg/builds/
     - fpcalc             https://acoustid.org/chromaprint
     - mkvpropedit        ships with MKVToolNix
     - yt-dlp             installed by pip above

     Verify with:
         ffmpeg -version
         ffprobe -version
         fpcalc -version
         mkvpropedit --version

  3. API keys (one-time)
  ----------------------
     Get a free TMDB key:
         https://www.themoviedb.org/settings/api

     Get an OpenSubtitles key (with paid tier if you want >5/day):
         https://www.opensubtitles.com/  ->  Profile -> Consumers

     Create .env-tv.ps1 next to Run-Intake.ps1 with two lines:

         $env:TMDB_API_KEY = "your_tmdb_v3_key"
         $env:OPENSUBTITLES_API_KEY = "your_opensubtitles_key"

     The wrapper sources this file automatically each run.

  4. Configure paths (one-time)
  -----------------------------
     Open Run-Intake.ps1 in a text editor and adjust these lines if your
     paths differ from the defaults:

         $IntakeFolder = "F:\intake"
         $MediaRoot    = "G:\TV"

  5. PowerShell execution policy (one-time, if needed)
  ----------------------------------------------------
     If Windows refuses to run the .ps1 with "execution of scripts is
     disabled", open an admin PowerShell and run:

         Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

     Or just launch the script with -ExecutionPolicy Bypass each time:

         powershell.exe -ExecutionPolicy Bypass -File "H:\movie-cleanup\TV_Fixer\Run-Intake.ps1"


================================================================================
  RUNNING INTAKE
================================================================================

  Daily / weekly use
  ------------------
     1. Drop new TV episode files into F:\intake (filenames can be anything
        guessit can parse: "Friends.S01E01.HDTV.x264-LOL.mkv",
        "The.Office.US.S03E16.720p.WEB-DL.mp4", etc.)

     2. Right-click Run-Intake.ps1 -> Run with PowerShell.
        (Or pin a shortcut to your taskbar -- see the readme footer.)

     3. Read the dry-run output. Verify that the planned destination
        folder, season, and episode look right. Open intake_report.csv
        for the full plan.

     4. Type 'y' to commit. Files get moved into G:\TV with Plex-format
        naming and (with --download-art, default in the wrapper) any new
        show gets posters + theme + trailers downloaded.

  Status values you'll see in the report
  --------------------------------------
     move           File is new -- moved to destination.
     replace        Destination already had a file; the incoming file
                    won an ffprobe-based quality compare (height ->
                    bitrate -> size). Loser sent to Recycle Bin.
     discard_new    Destination already had a HIGHER quality file. The
                    incoming file went to Recycle Bin.
     tie            Quality was identical. Existing wins; incoming
                    recycled (avoids dupe accumulation).
     unparseable    guessit couldn't extract show/season/episode from
                    the filename. File stays put.
     no_tmdb_match  Filename parsed but TMDB had no matching show. File
                    stays put.


================================================================================
  WHAT INTAKE DOES *NOT* DO -- THE FULL PIPELINE
================================================================================

  Intake handles the "new file -> right place with right name" half of the
  pipeline. Several other steps run on their own schedule.

  -----------------------------------------------------------------------
  STEP 1: Download subtitles                       (paced over days)
  -----------------------------------------------------------------------
  OpenSubtitles enforces a daily quota (5/day free, 1000/day VIP, 2000/day
  premium tier). Run subtitle fetching periodically -- it'll resume if
  interrupted and skip shows whose subs are already on disk.

  For a whole spreadsheet of shows:
      python fetch_subtitles.py `
          --from-xlsx "H:\_mediaprep\series_stats_details.xlsx" `
          --sheet series_stats_summary `
          --title-col title --tmdb-col tmdb_id `
          --media-root "G:\TV" --out subs --workers 6

  For just one new show you added today:
      python fetch_subtitles.py --tmdb 12345 --media-root "G:\TV" --out subs

  Check progress any time:
      python progress.py --media-root "G:\TV"

  -----------------------------------------------------------------------
  STEP 2: Extract audio + transcribe with Whisper  (one-time per file)
  -----------------------------------------------------------------------
  prepare.py samples 90 seconds of audio per episode, transcribes with
  faster-whisper (GPU recommended), and stores the transcript in
  identify_state.sqlite. This is the slowest one-time step -- expect
  ~2-5 hours for a fresh library of thousands of episodes.

  Resumable; safe to interrupt:
      python prepare.py "G:\TV" `
          --state-db identify_state.sqlite `
          --audio-cache .audio_cache `
          --whisper-model base

  Watch progress:
      python diagnose.py

  -----------------------------------------------------------------------
  STEP 3: Build subtitle FTS index                 (minutes)
  -----------------------------------------------------------------------
  Once you have a meaningful pile of .srt files (Step 1) on disk, index
  them for fast matching. Re-runnable any time as more subs arrive.

      python index_subs.py "G:\TV" --db subtitles.sqlite

  -----------------------------------------------------------------------
  STEP 4: Identify mis-named or DVD-rip files      (minutes per pass)
  -----------------------------------------------------------------------
  For files whose filename intake couldn't parse, or DVD rips with
  cryptic names, content-based identification matches the transcript
  against subtitles to figure out what episode it really is.

      python identify.py "G:\TV" `
          --subs-db subtitles.sqlite `
          --state-db identify_state.sqlite `
          --report identify_report.csv

  Status outputs:
     ok                Confident match within the file's show folder.
     low_confidence    Match found but below score/margin threshold.
     no_match          No match -- usually means subs aren't downloaded.
     cross_show_match  Best match was in a DIFFERENT show than the
                       file's source folder. Manual-review pile.
     error             Probe failed, too short, or Whisper crashed.

  Sample any status:
      python diagnose.py --status ok --limit 20
      python diagnose.py --status cross_show_match --limit 20

  -----------------------------------------------------------------------
  STEP 5: Apply identified renames                 (minutes)
  -----------------------------------------------------------------------
  Once you trust the identify results, actually rename the files:

      # Dry run first - inspect rename_plan.csv
      python apply_renames.py identify_report.csv --media-root "G:\TV"

      # Commit + embed metadata tags in MKV/MP4 containers
      python apply_renames.py identify_report.csv --media-root "G:\TV" `
          --commit --write-tags

  Then strip episode titles from filenames if you prefer unambiguous
  SxxEyy naming (avoids TMDB-vs-broadcast title disagreement):

      python strip_episode_titles.py "G:\TV" --commit


================================================================================
  HOUSEKEEPING TASKS (run when you notice they're needed)
================================================================================

  Find duplicate video files by AUDIO FINGERPRINT (not filename)
  -------------------------------------------------------------
      python dedupe.py "G:\TV" --db dedupe.sqlite --report dedupe_report.csv

  Then delete the duplicates that the report flagged:
      python apply_dedupe.py dedupe_report.csv --folder "Look at Later"
      python apply_dedupe.py dedupe_report.csv --folder "Look at Later" --commit

  Merge show folders that share a {tmdb-NNNN} but have slightly different names
  ----------------------------------------------------------------------------
      python merge_show_folders.py "G:\TV"            (dry run)
      python merge_show_folders.py "G:\TV" --commit

  Normalize season folder names to "Season NN (YYYY)"
  ---------------------------------------------------
      python fix_season_folders.py "G:\TV" --commit   (Season 1 -> Season 01)
      python fix_season_year.py    "G:\TV" --commit   (adds year suffix)

  Refresh artwork + theme songs across the library
  ------------------------------------------------
      python download_artwork.py --scan "G:\TV" --no-videos    (artwork only)
      python download_artwork.py --scan "G:\TV"                (also trailers)

  Add a show on disk that isn't in your tracking xlsx yet
  -------------------------------------------------------
      python find_missing_shows.py "G:\TV" `
          --xlsx "H:\_mediaprep\series_stats_details.xlsx" `
          --out missing_shows.csv --append-xlsx

  Refresh the xlsx episode counts after a batch of changes
  --------------------------------------------------------
      python update_stats_xlsx.py "G:\TV" `
          "H:\_mediaprep\series_stats_details.xlsx"

  Generate a phone-friendly inventory for thrift-store trips
  ----------------------------------------------------------
      python export_inventory.py "G:\TV" `
          --xlsx "H:\_mediaprep\series_stats_details.xlsx" `
          --out inventory.csv --html inventory.html


================================================================================
  TYPICAL WORKFLOWS
================================================================================

  Most common: "I downloaded a few new episodes"
  ----------------------------------------------
     1. Drop files in F:\intake.
     2. Run-Intake.ps1, confirm the dry run, commit.
     3. Run fetch_subtitles.py for the new show (only if it's NEW).
     Done. Subs will trickle in over the next day or two via your
     daily cron / scheduled task.

  "I added a whole new show I never had before"
  ---------------------------------------------
     1. Drop episode files in F:\intake.
     2. Run-Intake.ps1, confirm, commit. The wrapper passes
        --download-art so the new show folder gets posters + theme.
     3. python find_missing_shows.py to register it in your xlsx.
     4. python fetch_subtitles.py for that show's TMDB id.
     5. Later, when subs are in, run index_subs.py and identify.py to
        validate that intake's filename-based matching was correct.

  "I added a pile of DVD rips with cryptic names"
  ----------------------------------------------
     1. Drop them in F:\intake. Intake will probably mark most as
        'unparseable' or 'no_tmdb_match' and leave them put.
     2. Pre-organize them: put each show's files in
        G:\TV\<Show Name (Year) {tmdb-id}\Look at Later\
     3. Wait for subs to download.
     4. Run prepare.py + index_subs.py + identify.py + apply_renames.py
        on the whole library; that pipeline will figure out the right
        episode for each file from its actual audio content.

  "I want to validate that everything is correctly named"
  -------------------------------------------------------
     1. python sample_matches.py --n 20
     2. Open the listed files in VLC, confirm title cards match.
     3. If wrong, use revert_renames.py --show <name> to undo specific
        renames, then either fix manually or retry with different
        identify thresholds.


================================================================================
  TROUBLESHOOTING
================================================================================

  "ERROR: required external tool 'fpcalc' not found on PATH."
     -> Download from acoustid.org/chromaprint, drop fpcalc.exe in the
        TV_Fixer folder (Python checks current dir on Windows).

  "Set TMDB_API_KEY env var."
     -> Your .env-tv.ps1 isn't loading, or the var is empty. Verify
        the file exists next to Run-Intake.ps1 with the two $env
        lines. Run-Intake.ps1 prints a warning if it can't find it.

  PowerShell command line shows weird quote escaping errors.
     -> Avoid putting complex SQL or Python -c inline. Use the
        diagnose.py / reset_matches.py / progress.py wrappers instead.

  A file ended up in the wrong show folder.
     -> Run sample_matches.py around it to see what intake / identify
        thought it was. If it was intake (filename), pre-rename the
        source file. If it was identify (content), bump --threshold
        and --min-margin and re-run that file specifically.

  intake_report.csv shows lots of 'no_tmdb_match' entries.
     -> Filenames lack a show title TMDB can recognize. Either rename
        them to include a clearer show name, or move them to a show
        folder under G:\TV and let prepare.py + identify.py figure
        them out content-based.

  "OpenSubtitles quota exhausted"
     -> Normal. Your paid tier resets at 00:00 UTC. fetch_subtitles.py
        stops cleanly when it hits the cap; just re-run tomorrow.

================================================================================
