# daily_intake.ps1
#
# Daily routine for newly-ripped music.
# 1. Runs intake_new_music.py on H:\Music Cleanup\incoming\ (executes the moves)
# 2. Runs add_replaygain.py on E:\Music (rsgain auto-skips already-tagged files)
# 3. Logs everything to H:\Music Cleanup\daily_intake_logs\<timestamp>.log
#
# Run by hand:  .\daily_intake.ps1
# Run silently: powershell -ExecutionPolicy Bypass -File "H:\Music Cleanup\daily_intake.ps1"
# Schedule it:  Task Scheduler -> daily -> action: the silent command above

# ------------ Configuration (edit if needed) ------------
$MusicRoot   = "E:\Music"
$IncomingDir = "E:\Music Intake"
$ScriptDir   = "H:\Music Cleanup"
$LogDir      = "H:\Music Cleanup\daily_intake_logs"
$Python      = "python"   # or full path, e.g. "C:\Users\xxxx\AppData\Local\Programs\Python\Python310\python.exe"

# Optional: skip ReplayGain on tiny intake runs (true = always run, false = run weekly)
$RunReplayGainEveryRun = $true

# --------------------------------------------------------

$ErrorActionPreference = 'Continue'  # keep going through errors so the log captures everything
$Timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$LogPath   = Join-Path $LogDir "daily_intake_$Timestamp.log"

# Ensure log directory + incoming directory exist
foreach ($d in @($LogDir, $IncomingDir)) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

function Invoke-Step {
    param(
        [string]$Name,
        [string[]]$ArgList
    )
    Write-Log ""
    Write-Log "=========================================================="
    Write-Log "  STEP: $Name"
    Write-Log "  CMD : $Python $($ArgList -join ' ')"
    Write-Log "=========================================================="
    $started = Get-Date
    try {
        # Stream all output (stdout + stderr) into the log AND the console
        & $Python @ArgList 2>&1 | Tee-Object -FilePath $LogPath -Append
        $exit = $LASTEXITCODE
    } catch {
        Write-Log "EXCEPTION: $_"
        $exit = -1
    }
    $elapsed = [int]((Get-Date) - $started).TotalSeconds
    Write-Log "  -- exit=$exit  elapsed=${elapsed}s"
    return $exit
}

# --- Start ---
Write-Log "=========================================================="
Write-Log "DAILY INTAKE START $Timestamp"
Write-Log "  music_root  = $MusicRoot"
Write-Log "  incoming    = $IncomingDir"
Write-Log "  script_dir  = $ScriptDir"
Write-Log "  log         = $LogPath"
Write-Log "=========================================================="

# Validate environment variables for API keys
foreach ($var in @("ACOUSTID_API_KEY", "LASTFM_API_KEY", "DISCOGS_TOKEN")) {
    $val = [Environment]::GetEnvironmentVariable($var, "User")
    if (-not $val) {
        $val = [Environment]::GetEnvironmentVariable($var, "Process")
    }
    if ($val) {
        # Only log first/last few chars
        $masked = if ($val.Length -gt 10) { $val.Substring(0,4) + "..." + $val.Substring($val.Length-4) } else { "(short)" }
        Write-Log "  $var = $masked"
    } else {
        Write-Log "  WARNING: $var is not set"
    }
}

# Count incoming files before
$incomingFiles = @(Get-ChildItem -Path $IncomingDir -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -match '\.(mp3|flac|m4a|m4b|mp4|aac|ogg|oga|opus|wma|wav|aiff|aif)$' })
$incomingCount = $incomingFiles.Count
Write-Log ""
Write-Log "Incoming audio files: $incomingCount"

if ($incomingCount -eq 0) {
    Write-Log "Nothing to intake. Skipping intake step."
} else {
    $intakeArgs = @(
        (Join-Path $ScriptDir "intake_new_music.py"),
        "--incoming", $IncomingDir,
        "--music-root", $MusicRoot,
        "--execute"
    )
    $intakeExit = Invoke-Step -Name "intake_new_music" -ArgList $intakeArgs

    # Optional: log what's still in incoming after the run (failures)
    $leftover = @(Get-ChildItem -Path $IncomingDir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -match '\.(mp3|flac|m4a|m4b|mp4|aac|ogg|oga|opus|wma|wav|aiff|aif)$' })
    Write-Log ""
    Write-Log "After intake: $($leftover.Count) audio files still in incoming"
    if ($leftover.Count -gt 0 -and $leftover.Count -le 20) {
        foreach ($f in $leftover) { Write-Log "  leftover: $($f.FullName)" }
    } elseif ($leftover.Count -gt 20) {
        Write-Log "  (showing first 20)"
        foreach ($f in $leftover[0..19]) { Write-Log "  leftover: $($f.FullName)" }
    }
}

# ReplayGain
if ($RunReplayGainEveryRun) {
    $rgArgs = @(
        (Join-Path $ScriptDir "add_replaygain.py"),
        "--music-root", $MusicRoot
    )
    Invoke-Step -Name "add_replaygain" -ArgList $rgArgs | Out-Null
} else {
    Write-Log ""
    Write-Log "ReplayGain skipped (RunReplayGainEveryRun = false)"
}

Write-Log ""
Write-Log "=========================================================="
Write-Log "DAILY INTAKE END  ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))"
Write-Log "Full log: $LogPath"
Write-Log "=========================================================="
