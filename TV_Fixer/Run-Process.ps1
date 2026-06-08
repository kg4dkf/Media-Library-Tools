<#
.SYNOPSIS
    Run the full content-ID + rename pipeline against your TV library.

.DESCRIPTION
    Designed for the "drop DVD-ripped files into G:\TV\<Show>\Look at Later\,
    then process" workflow. Runs these steps in order:

      1. prepare.py        Whisper-transcribe a 90s sample of any new file
      2. index_subs.py     Refresh the FTS subtitle index
      3. identify.py       Match transcripts against subtitles per show
      4. apply_renames.py  Move + rename files to Plex naming (with --commit)
      5. strip_episode_titles.py   Remove episode titles from filenames
      6. fix_season_year.py        Normalize 'Season NN' -> 'Season NN (YYYY)'
      7. download_artwork.py       Refresh artwork (incremental; skips existing)
      8. update_stats_xlsx.py      Refresh the tracking xlsx

    Confirms before each destructive step. Skipping confirm aborts that
    step (and everything after it). All scripts are resumable so you can
    safely interrupt and resume later.

.NOTES
    Same setup as Run-Intake.ps1 -- needs the venv, API keys, etc.
    Set those in the CONFIG block below.
#>

# ============================ CONFIG ============================

$ScriptDir     = $PSScriptRoot
$MediaRoot     = "G:\TV"
$TrackingXlsx  = "H:\_mediaprep\series_stats_details.xlsx"

# API keys -- replace placeholders with your real keys.
$env:TMDB_API_KEY          = "xxxx"
$env:OPENSUBTITLES_API_KEY = "xxxx"

# Whisper model: tiny | base | small | medium | large-v3
$WhisperModel  = "base"

# Paths to artifacts (usually no need to change)
$VenvActivate  = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
$StateDb       = Join-Path $ScriptDir "identify_state.sqlite"
$SubsDb        = Join-Path $ScriptDir "subtitles.sqlite"
$AudioCache    = Join-Path $ScriptDir ".audio_cache"
$IdentifyCsv   = Join-Path $ScriptDir "identify_report.csv"

# ================================================================

$Host.UI.RawUI.WindowTitle = "TV Process"

function Pause-OnExit($code = 0) {
    Write-Host ""
    Write-Host "Press any key to close..." -ForegroundColor DarkGray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit $code
}

function Section($title) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor Cyan
    Write-Host "  $title" -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor Cyan
}

function Confirm-Step($prompt, [bool]$default = $false) {
    $defaultStr = if ($default) { "[Y/n]" } else { "[y/N]" }
    $r = Read-Host "$prompt $defaultStr"
    if (-not $r) { return $default }
    return ($r -match "^(y|yes)$")
}

function Invoke-Step($name, $argv) {
    Write-Host ""
    Write-Host "[$name] $($argv -join ' ')" -ForegroundColor DarkGray
    & python @argv
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host "$name exited with code $code." -ForegroundColor Red
        if (-not (Confirm-Step "Continue with the rest of the pipeline?" $false)) {
            Pause-OnExit $code
        }
    }
}

# ============================ START ============================

Write-Host "=== TV Process Pipeline ===" -ForegroundColor Cyan
Write-Host "  Script dir:   $ScriptDir"
Write-Host "  Media root:   $MediaRoot"
Write-Host "  State DB:     $StateDb"
Write-Host "  Subs DB:      $SubsDb"
Write-Host "  Whisper:      $WhisperModel"

if (Test-Path $VenvActivate) {
    Write-Host "[venv] Activating $VenvActivate" -ForegroundColor DarkGray
    & $VenvActivate
} else {
    Write-Host "[venv] No venv found -- using system Python" -ForegroundColor Yellow
}

# Sanity checks
if (-not $env:TMDB_API_KEY -or $env:TMDB_API_KEY -eq "PUT_YOUR_TMDB_V3_KEY_HERE") {
    Write-Host "ERROR: TMDB_API_KEY placeholder in this .ps1 file hasn't been filled in." -ForegroundColor Red
    Pause-OnExit 1
}
if (-not (Test-Path $MediaRoot)) {
    Write-Host "ERROR: Media root not found: $MediaRoot" -ForegroundColor Red
    Pause-OnExit 1
}

# === 1. prepare.py ===
Section "Step 1/8: Transcribe new files (prepare.py)"
Write-Host "Whisper transcribes a 90s sample for any file not yet in $StateDb."
Write-Host "This is the slow step. It's resumable; safe to Ctrl-C."
if (Confirm-Step "Run prepare.py now?" $true) {
    Invoke-Step "prepare" @(
        (Join-Path $ScriptDir "prepare.py"),
        $MediaRoot,
        "--state-db", $StateDb,
        "--audio-cache", $AudioCache,
        "--whisper-model", $WhisperModel
    )
} else {
    Write-Host "Skipped." -ForegroundColor Yellow
}

# === 2. index_subs.py ===
Section "Step 2/8: Refresh subtitle index (index_subs.py)"
Write-Host "Rebuilds the FTS5 index from all .srt files under $MediaRoot."
if (Confirm-Step "Run index_subs.py now?" $true) {
    Invoke-Step "index_subs" @(
        (Join-Path $ScriptDir "index_subs.py"),
        $MediaRoot,
        "--db", $SubsDb
    )
} else {
    Write-Host "Skipped." -ForegroundColor Yellow
}

# === 3. identify.py ===
Section "Step 3/8: Match transcripts (identify.py)"
Write-Host "Matches each prepared transcript to its episode using the FTS index."
Write-Host "Skips files already in a terminal status."
if (Confirm-Step "Run identify.py now?" $true) {
    Invoke-Step "identify" @(
        (Join-Path $ScriptDir "identify.py"),
        $MediaRoot,
        "--subs-db", $SubsDb,
        "--state-db", $StateDb,
        "--report", $IdentifyCsv
    )
} else {
    Write-Host "Skipped." -ForegroundColor Yellow
}

# === 4. apply_renames.py ===
Section "Step 4/8: Apply renames (apply_renames.py)"
Write-Host "DESTRUCTIVE: moves files into Plex layout. Loser of quality"
Write-Host "compares goes to Recycle Bin. Will run a dry run first."
Write-Host ""
Write-Host "  Dry run (no changes):"
Invoke-Step "apply_renames(dry)" @(
    (Join-Path $ScriptDir "apply_renames.py"),
    $IdentifyCsv,
    "--media-root", $MediaRoot
)
Write-Host ""
Write-Host "Inspect rename_plan.csv now before committing."
if (Confirm-Step "Commit the rename plan?" $false) {
    Invoke-Step "apply_renames(commit)" @(
        (Join-Path $ScriptDir "apply_renames.py"),
        $IdentifyCsv,
        "--media-root", $MediaRoot,
        "--commit",
        "--write-tags"
    )
} else {
    Write-Host "Skipped. Stopping pipeline." -ForegroundColor Yellow
    Pause-OnExit 0
}

# === 5. strip_episode_titles.py ===
Section "Step 5/8: Strip episode titles (strip_episode_titles.py)"
Write-Host "Removes ' - Title' from filenames to avoid TMDB-vs-broadcast disagreement."
if (Confirm-Step "Run strip_episode_titles.py with --commit?" $true) {
    Invoke-Step "strip_titles" @(
        (Join-Path $ScriptDir "strip_episode_titles.py"),
        $MediaRoot,
        "--commit"
    )
} else {
    Write-Host "Skipped." -ForegroundColor Yellow
}

# === 6. fix_season_year.py ===
Section "Step 6/8: Normalize season folders (fix_season_year.py)"
Write-Host "Adds (YYYY) suffix to any bare 'Season NN' folder via TMDB lookup."
if (Confirm-Step "Run fix_season_year.py with --commit?" $true) {
    Invoke-Step "fix_season_year" @(
        (Join-Path $ScriptDir "fix_season_year.py"),
        $MediaRoot,
        "--commit"
    )
} else {
    Write-Host "Skipped." -ForegroundColor Yellow
}

# === 7. download_artwork.py ===
Section "Step 7/8: Refresh artwork (download_artwork.py)"
Write-Host "Incremental scan -- skips assets already on disk."
Write-Host "(Trailers via yt-dlp are slow; offered separately.)"
if (Confirm-Step "Run artwork pass (artwork only, no trailers)?" $true) {
    Invoke-Step "artwork" @(
        (Join-Path $ScriptDir "download_artwork.py"),
        "--scan", $MediaRoot,
        "--no-videos"
    )
} else {
    Write-Host "Skipped." -ForegroundColor Yellow
}
if (Confirm-Step "Also fetch trailers / featurettes (slow)?" $false) {
    Invoke-Step "artwork_videos" @(
        (Join-Path $ScriptDir "download_artwork.py"),
        "--scan", $MediaRoot
    )
}

# === 8. update_stats_xlsx.py ===
Section "Step 8/8: Refresh tracking xlsx (update_stats_xlsx.py)"
if (Test-Path $TrackingXlsx) {
    Write-Host "Updates $TrackingXlsx in place (timestamped .bak written first)."
    if (Confirm-Step "Run update_stats_xlsx.py now?" $true) {
        Invoke-Step "update_stats" @(
            (Join-Path $ScriptDir "update_stats_xlsx.py"),
            $MediaRoot,
            $TrackingXlsx
        )
    } else {
        Write-Host "Skipped." -ForegroundColor Yellow
    }
} else {
    Write-Host "Tracking xlsx not found at $TrackingXlsx; skipping." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Pipeline complete ===" -ForegroundColor Green
Pause-OnExit 0
