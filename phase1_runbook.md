# Phase 1 Runbook — `inventory.py`

This script is **read-only** against `H:\Movies`. It writes only to `H:\_mediaprep\`. Nothing on your movie library can be harmed by running it.

---

## 1. Prerequisites (one-time setup)

### 1.1 Rotate the TMDB API key

The previous key was leaked. Regenerate it first:

1. Go to https://www.themoviedb.org/settings/api
2. Click **Regenerate** next to your v3 API key.
3. Copy the new 32-character hex key. Do **not** paste it anywhere outside `config.json`.

### 1.2 Enable Windows long-path support

Run this in an **elevated** PowerShell (Run as Administrator):

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

No reboot required for most applications, but a reboot won't hurt.

### 1.3 Install ffmpeg (provides ffprobe)

```powershell
winget install Gyan.FFmpeg
```

Close and reopen PowerShell so `ffprobe` lands on your PATH. Verify:

```powershell
ffprobe -version
```

### 1.4 Add Defender exclusions for the run

Real-time antivirus scanning will slow hashing dramatically. In an elevated PowerShell:

```powershell
Add-MpPreference -ExclusionPath "H:\Movies"
Add-MpPreference -ExclusionPath "H:\_mediaprep"
```

You can remove these later with `Remove-MpPreference -ExclusionPath ...` once we're done.

### 1.5 Pause Plex library scanning

In Plex Web: **Settings → Library → Scan my library automatically → OFF** (for the Movies library). You can re-enable after phase 5.

---

## 2. Project setup

Pick a project folder. I'll assume `C:\Users\xxxx\movie-cleanup\` for examples.

```powershell
mkdir C:\Users\xxxx\movie-cleanup
cd C:\Users\xxxx\movie-cleanup
```

Copy these files into that folder:
- `inventory.py`
- `requirements.txt`
- `config.example.json`

### 2.1 Create a virtual environment

Virtual envs keep this project's dependencies isolated from any other Python work you do.

```powershell
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
```

If activation fails with an execution-policy error:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Then try `Activate.ps1` again.

### 2.2 Install dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

### 2.3 Create `config.json`

```powershell
Copy-Item config.example.json config.json
notepad config.json
```

Paste your new TMDB key into the `tmdb_api_key` field. Save. Close.

### 2.4 Create the working directory

```powershell
mkdir H:\_mediaprep
mkdir H:\duplicates
```

The script will create them too, but doing it manually catches any permission issues early.

---

## 3. First run: smoke test (50 titles)

Before the full overnight run, do a 50-title dry run to verify everything works:

```powershell
py -3.13 inventory.py --config config.json --limit 50
```

This takes ~5 minutes. Check `H:\_mediaprep\`:

- `inventory.csv` — one row per file scanned
- `plan.csv` — one row per title folder identified (look at `action`, `confidence`, `target_folder`)
- `unresolved.csv` — things it couldn't identify
- `duplicates.csv` — duplicate groups
- `inventory_YYYYMMDD_HHMMSS.log` — full debug log
- `tmdb_cache.sqlite` — TMDB response cache (keeps growing; reused across runs)
- `state.json` — resume checkpoint

**Look at `plan.csv` in Excel.** Sort by `confidence` ascending. The low-confidence rows should be things you'd also find hard to identify from the filename. The high-confidence rows should have correct titles, years, and target folders.

**Things to verify:**

- [ ] Target folders look right (spot check a franchise)
- [ ] Collection names have "Collection" stripped (e.g., "Fast & Furious" not "Fast & Furious Collection")
- [ ] IMDb IDs are in the filename template
- [ ] `unresolved.csv` is small relative to `plan.csv`
- [ ] No Python traceback at the end of the log

If anything looks wrong, come back and we'll debug. **Do not run the execute script yet** — that's phase 3, which doesn't exist.

---

## 4. Full run (overnight)

Once the smoke test passes, run against the whole library:

```powershell
py -3.13 inventory.py --config config.json
```

Expect 6–12 hours depending on drive speed. You can:

- Let it run unattended. It's read-only.
- Ctrl-C at any point — progress is checkpointed every 50 titles.
- Resume with: `py -3.13 inventory.py --config config.json --resume`

The slowest phase is BLAKE3 hashing of same-size files during duplicate detection. If you're impatient and want to skip that (only catches TMDB-ID duplicates, not byte-identical duplicates):

```powershell
py -3.13 inventory.py --config config.json --skip-hash
```

I don't recommend `--skip-hash` for the real run; exact-hash detection is very reliable and catches genuine 1-to-1 duplicates that TMDB-matching would miss if TMDB identification fails.

---

## 5. What the CSVs mean

### `inventory.csv`
Every file that lives under `H:\Movies`. Don't edit; it's the ground-truth manifest.

### `plan.csv` — **the important one; you edit this in phase 2**
One row per title folder. Key columns:

| Column | Meaning |
|---|---|
| `action` | `AUTO` = confident match, will be auto-applied; `REVIEW` = medium confidence, needs your approval |
| `confidence` | 0–100 score |
| `target_folder` | The full target path the movie will be moved to |
| `duplicate_role` | `keeper`, `duplicate_of:<id>`, or blank |
| `flags` | Comma-separated issue flags; worth filtering on |
| `notes` | Script-generated explanation of how it matched |

### `unresolved.csv`
Things the script couldn't identify. We'll review these together later. Lower priority than the plan.

### `duplicates.csv`
Duplicate groups. The `keeper_path` is the one that stays; `duplicate_paths` are the losers that will move to `H:\duplicates\` in phase 3.

---

## 6. After the full run

Reply to me with:

1. The `Summary` line from the log (total, AUTO, REVIEW, UNRESOLVED, DUP_GROUPS).
2. Any tracebacks or `ERROR`-level messages in the log.
3. Your gut feel after spot-checking 20 rows of `plan.csv`.

Then we'll move to phase 2 (CSV review) and I'll start writing `execute.py`.

---

## 7. Troubleshooting

**"Missing required packages"** — `pip install -r requirements.txt` didn't run or was run outside the venv. Re-activate the venv and retry.

**"ffprobe not found"** — ffmpeg install didn't complete or PATH didn't refresh. Close PowerShell, reopen, verify with `ffprobe -version`.

**Lots of `year-not-matched` or `multiple-tmdb-matches` flags** — normal on a messy library. That's exactly what phase 2 review is for.

**Long-path errors** — confirm the registry setting from §1.2 and that you restarted PowerShell.

**Script dies on a specific file** — check the log for the last title_id processed; the error is for that one file. Most per-title errors are caught and the title gets flagged as unresolved rather than crashing the run.

**Want to start over** — delete `H:\_mediaprep\state.json` (keep `tmdb_cache.sqlite` — it's a valuable cache of API responses you don't want to re-fetch).
