cd H:\movie-cleanup

Write-Host "===== Step 1/2: Process new movies =====" -ForegroundColor Cyan
py intake.py --config config.json --full

Write-Host ""
Write-Host "===== Step 2/2: Refresh my_movies in workbook =====" -ForegroundColor Cyan

# CSV is still written to OneDrive so the phone-friendly copy stays in sync.
# If you no longer want the OneDrive CSV, change $csvPath to "$env:TEMP\my_movies_export.csv".
$csvPath = "$env:TEMP\my_movies_export.csv"
$workbook = "H:\_mediaprep\series_stats_details.xlsx"

py library_export.py --config config.json --enrich --out $csvPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "library_export.py failed - workbook NOT updated." -ForegroundColor Red
    Pause; exit $LASTEXITCODE
}

py update_my_movies_sheet.py --csv $csvPath --xlsx $workbook --sheet my_movies
if ($LASTEXITCODE -ne 0) {
    Write-Host "Workbook update failed. Is '$workbook' open in Excel? Close it and re-run." -ForegroundColor Red
    Pause; exit $LASTEXITCODE
}

try {
    python "H:\movie-cleanup\find_missing_series_movies.py" "H:\Movies" "H:\_mediaprep\series_stats_details.xlsx" 2>&1 |
        Tee-Object -FilePath "H:\_mediaprep\logs\missing_movies_$(Get-Date -Format yyyy-MM-dd).log"
} catch {
    Write-Warning "Missing-movies scan failed: $_"
}

Write-Host ""
Write-Host "Done. New movies are in Plex; my_movies sheet refreshed in $workbook." -ForegroundColor Green

# Recreate the staging folder for next time
New-Item -ItemType Directory -Path H:\Temp -Force | Out-Null
Pause
