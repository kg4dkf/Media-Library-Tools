<#
.SYNOPSIS
    Run intake_tv.py interactively from Windows Explorer.

.DESCRIPTION
    Double-click this file (or right-click -> Run with PowerShell) to
    process the configured intake folder. The script:
      1. Activates the virtual environment if present.
      2. Sets API keys from the config block below.
      3. Runs intake_tv.py in DRY-RUN mode and shows the plan.
      4. Prompts y/n before committing.
      5. On confirmation, runs again with --commit --download-art.

    Edit the CONFIG block below to put in your API keys and paths.

.NOTES
    SECURITY: This file contains your API keys. Do not commit it to git,
    do not share it, do not upload it. If a key leaks, rotate it at:
      TMDB:          https://www.themoviedb.org/settings/api
      OpenSubtitles: https://www.opensubtitles.com/  (Consumers section)
#>

# ============================ CONFIG ============================

$ScriptDir     = $PSScriptRoot
$IntakeFolder  = "H:\Temp TV"
$MediaRoot     = "G:\TV"

# API keys -- replace the placeholders with your real keys.
$env:TMDB_API_KEY          = "xxxx"
$env:OPENSUBTITLES_API_KEY = "xxxx"

# Paths to scripts and venv -- usually no need to change.
$VenvActivate  = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
$IntakeScript  = Join-Path $ScriptDir "intake_tv.py"
$ReportFile    = Join-Path $ScriptDir "intake_report.csv"

# ================================================================

$Host.UI.RawUI.WindowTitle = "TV Intake"

function Pause-OnExit($code = 0) {
    Write-Host ""
    Write-Host "Press any key to close..." -ForegroundColor DarkGray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit $code
}

Write-Host "=== TV Intake ===" -ForegroundColor Cyan
Write-Host "  Script dir:    $ScriptDir"
Write-Host "  Intake folder: $IntakeFolder"
Write-Host "  Media root:    $MediaRoot"
Write-Host ""

# Activate venv if present
if (Test-Path $VenvActivate) {
    Write-Host "[venv] Activating $VenvActivate" -ForegroundColor DarkGray
    & $VenvActivate
} else {
    Write-Host "[venv] No venv found at $VenvActivate -- using system Python" -ForegroundColor Yellow
}

# Sanity checks on the config block
if (-not $env:TMDB_API_KEY -or $env:TMDB_API_KEY -eq "PUT_YOUR_TMDB_V3_KEY_HERE") {
    Write-Host "ERROR: TMDB_API_KEY placeholder in this .ps1 file hasn't been filled in." -ForegroundColor Red
    Write-Host "       Open Run-Intake.ps1 in Notepad and replace 'PUT_YOUR_TMDB_V3_KEY_HERE'" -ForegroundColor Red
    Write-Host "       with your real TMDB v3 key (free, from themoviedb.org/settings/api)." -ForegroundColor Red
    Pause-OnExit 1
}
if (-not $env:OPENSUBTITLES_API_KEY -or $env:OPENSUBTITLES_API_KEY -eq "PUT_YOUR_OPENSUBTITLES_KEY_HERE") {
    Write-Host "WARN: OPENSUBTITLES_API_KEY placeholder hasn't been filled in." -ForegroundColor Yellow
    Write-Host "      Intake will still work, but fetch_subtitles.py won't." -ForegroundColor Yellow
}
if (-not (Test-Path $IntakeFolder)) {
    Write-Host "ERROR: Intake folder not found: $IntakeFolder" -ForegroundColor Red
    Pause-OnExit 1
}
if (-not (Test-Path $MediaRoot)) {
    Write-Host "ERROR: Media root not found: $MediaRoot" -ForegroundColor Red
    Pause-OnExit 1
}
if (-not (Test-Path $IntakeScript)) {
    Write-Host "ERROR: intake_tv.py not found: $IntakeScript" -ForegroundColor Red
    Pause-OnExit 1
}

# Count files in the intake folder so user sees scope up front
$videoExts = @(".mkv",".mp4",".avi",".mov",".m4v",".ts",".mpg",".mpeg",".wmv",".webm")
$inFiles = Get-ChildItem -Path $IntakeFolder -Recurse -File `
    | Where-Object { $videoExts -contains $_.Extension.ToLower() }
Write-Host ""
Write-Host "Found $($inFiles.Count) video file(s) in $IntakeFolder" -ForegroundColor Cyan
if ($inFiles.Count -eq 0) {
    Write-Host "Nothing to do." -ForegroundColor Green
    Pause-OnExit 0
}

# === DRY RUN ===
Write-Host ""
Write-Host "--- Dry run (no files will be moved) ---" -ForegroundColor Cyan
$dryArgs = @(
    $IntakeScript,
    $IntakeFolder,
    "--media-root", $MediaRoot,
    "--report", $ReportFile
)
& python @dryArgs
$dryExit = $LASTEXITCODE
if ($dryExit -ne 0) {
    Write-Host "Dry run exited with code $dryExit. Check the messages above." -ForegroundColor Red
    Pause-OnExit $dryExit
}

Write-Host ""
Write-Host "Dry-run report: $ReportFile" -ForegroundColor Cyan
Write-Host "Inspect it (Ctrl+click to open) before committing."
Write-Host ""

# === CONFIRM ===
$resp = Read-Host "Commit the plan? [y/N]"
if ($resp -notmatch "^(y|yes)$") {
    Write-Host "Aborted. Nothing was moved." -ForegroundColor Yellow
    Pause-OnExit 0
}

# === COMMIT ===
Write-Host ""
Write-Host "--- Committing ---" -ForegroundColor Cyan
$commitArgs = @(
    $IntakeScript,
    $IntakeFolder,
    "--media-root", $MediaRoot,
    "--report", $ReportFile,
    "--commit",
    "--download-art"
)
& python @commitArgs
$commitExit = $LASTEXITCODE
if ($commitExit -ne 0) {
    Write-Host "Commit exited with code $commitExit." -ForegroundColor Red
    Pause-OnExit $commitExit
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  - python fetch_subtitles.py --media-root `"$MediaRoot`" --out subs --workers 6"
Write-Host "  - (subs land over time at OpenSubtitles 2000/day cap)"
Write-Host "  - python update_stats_xlsx.py `"$MediaRoot`" `"H:\_mediaprep\series_stats_details.xlsx`""

Pause-OnExit 0
