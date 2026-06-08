# intake.py — Quick Reference

## Before running

1. Drop new movie files into `H:\Temp\`
   - Single `.mkv` at root is fine
   - Or a folder with `.mkv` + `.nfo` + posters
2. Rename files with year, ideally with IMDB tag:
   - **Best:** `Movie Title (Year) {imdb-tt#######}.mp4` — bypasses TMDB search, hits conf=95
   - **OK:** `Movie Title (Year).mp4` — works for most titles
   - **Risky:** `Movie Title.mp4` — picks wrong era for common titles (Superman, Alice, etc.)
3. **Don't put TV episodes here** — they trigger in-batch collisions and won't process

### Editions (Theatrical / Extended / Director's Cut / etc.)

Drop both versions in `H:\Temp\` at once. Edition words anywhere in the
filename get translated into Plex's `{edition-X}` tag and both files land
in the same target folder, so Plex shows one movie with a version picker
rather than two separate entries.

```
H:\Temp\Avatar Theatrical (2009) {imdb-tt0499549}.mp4
H:\Temp\Avatar Extended (2009)   {imdb-tt0499549}.mp4
```

→ Both end up inside `H:\Movies\Avatar (2009) {imdb-tt0499549}\`:

```
Avatar (2009) {edition-Theatrical} {imdb-tt0499549}.mp4
Avatar (2009) {edition-Extended}   {imdb-tt0499549}.mp4
Avatar (2009) {imdb-tt0499549}.nfo
```

Words intake.py recognizes (case-insensitive): Theatrical, Extended,
Director's Cut, Extended Director's Cut, Extended Cut, Theatrical Cut,
Final Cut, Ultimate Edition, Special Edition, Extended Edition,
Remastered, Unrated, and the explicit Plex form `{edition-Anything}`.

### 3D discs

Add `3D` (and optionally `HSBS` or `HOU`) to the filename. The 3D `.mp4`
lives alongside its 2D sibling so Plex groups them; the `.mkv` source
goes to a separate archive root that Plex doesn't index.

```
H:\Temp\Gravity 3D (2013) {imdb-tt1454468}.mp4   →  playable, Plex sees it
H:\Temp\Gravity 3D (2013) {imdb-tt1454468}.mkv   →  source archive, OUT of Plex
```

→ The `.mp4` lands in `H:\Movies\Gravity (2013) {imdb-tt1454468}\`:

```
Gravity (2013) {imdb-tt1454468}.mp4              # the 2D version (if you have it)
Gravity (2013) {imdb-tt1454468} - 3D.HSBS.mp4    # NEW — Plex switches TV to 3D
```

→ The `.mkv` lands in **`H:\3D MKV\`** (flat, no per-movie subfolder):

```
Gravity (2013) {imdb-tt1454468} - 3D.HSBS.mkv
```

**Default 3D format is HSBS** (half side-by-side — most MakeMKV/Handbrake
output). Override per file by including `HOU` or `FSBS` in the name.

To use a different archive path, add this to `config.json`:

```json
"threed_mkv_root": "X:\\Some\\Other\\Path"
```

**Combinations work.** `Avatar Extended 3D (2009) {imdb-tt0499549}.mp4` →
`Avatar (2009) {edition-Extended} {imdb-tt0499549} - 3D.HSBS.mp4` in the
canonical Avatar folder.

### Extras (bonus features, behind-the-scenes, etc.)

Add `Extra`, `Extras`, or `Extra 2` / `Extra 3` etc. at the END of the
filename (just before the extension). They get routed into
`<MovieFolder>\Extras\`, which Plex auto-recognizes as bonus features.

```
H:\Temp\The Lorax (2012) {imdb-tt1482459} Extra.mp4
H:\Temp\The Lorax (2012) {imdb-tt1482459} Extra 2.mp4
H:\Temp\The Lorax (2012) {imdb-tt1482459} Extra 3.mp4
```

→ All three land in `H:\Movies\The Lorax (2012) {imdb-tt1482459}\Extras\`:

```
The Lorax (2012) - Extra.mp4
The Lorax (2012) - Extra 2.mp4
The Lorax (2012) - Extra 3.mp4
```

The IMDB tag is required so intake.py knows which movie they belong to.
Without it, the file becomes "REVIEW conf=0" and gets skipped — fix by
adding `{imdb-tt#######}` to the name.

**Word position matters.** "Extra" must be at the END of the stem
(before the extension). This avoids misclassifying "Extra Ordinary
(2019)" or similar real titles. Files like `Movie (Year) {imdb-...} Extra.mp4`
match; `Extra Movie (Year).mp4` does not.

**Combinations.** Extras override variant routing — a file marked as an
extra will never go to the flat 3D MKV archive, even if it's a `.mkv`
with `3D` in the name. Bonus content belongs in `<Movie>\Extras\`.

## Run

```powershell
cd H:\movie-cleanup
py intake.py --config config.json --full
py library_export.py --config config.json --enrich
```

## Variants

```powershell
py intake.py --config config.json --dry-run             # preview, don't move
py intake.py --config config.json --yes                 # skip confirm prompt
py intake.py --config config.json --plex-scan           # refresh Plex after success
py intake.py --config config.json --replace-existing    # force overwrite (rare)
py intake.py --config config.json --intake-dir D:\Rips  # use a different folder
```

## Enrichment (artwork, trailers)

The default run moves files and writes the NFO, but does NOT download
posters, fanart, clearart, or trailers. Add these flags to fetch them
just for the newly-added titles:

```powershell
py intake.py --config config.json --with-art          # posters + fanart + logo from TMDB
py intake.py --config config.json --with-clearart     # clearart/clearlogo from Fanart.tv
py intake.py --config config.json --with-trailer      # YouTube trailer via TMDB + yt-dlp
py intake.py --config config.json --full              # all three above + --plex-scan
```

The `--full` flag is the "do everything" shortcut and is what you most
likely want for routine disc-rip intake. Extra time per title:
- art: ~10 seconds
- clearart: ~10 seconds (needs fanart_tv_api_key in config.json)
- trailer: ~1-3 minutes (downloads ~50 MB video)

## What you'll see

| Confidence | Meaning | Auto-process? |
|---|---|---|
| 95 | IMDB tag in filename or NFO — exact match | yes |
| 75-85 | TMDB search matched title + year | yes |
| 60-65 | Match found but ambiguous (multi-result) | **REVIEW** — fix filename and re-run |
| 30 + COLLISION | TV content or multi-disc set | skipped — move out of H:\Temp |
| 0 | No TMDB match | **REVIEW** — fix typo or add IMDB tag |

EXISTS notes mean the target folder already has a video. Script auto-decides via ffprobe quality comparison:
- "NEW WINS" → archives existing to `H:\duplicates\intake_replaced\`, installs new
- "KEEP EXISTING" → moves new source to duplicates, library untouched
- "UNDECIDED" → skips (use `--replace-existing` to force)

## If something goes wrong

- **Wrong movie matched?** Rename source with `{imdb-tt#######}` and re-run.
- **All TV episodes flagged as collision?** That's correct — move them out of H:\Temp.
- **Undo a run?** Find the run_id in `H:\_mediaprep\intake_<run_id>.log` filename. Then:
  ```powershell
  py rollback.py --config config.json --journal intake_moves.jsonl --run-id 2026-04-29T13-02-08
  ```

## Tip: get IMDB IDs in your filenames automatically

If you use MakeMKV or similar, check whether your rip tool writes a sidecar `.nfo` with `<imdbid>tt1234567</imdbid>`. intake.py reads it and matches at conf=95 without needing the filename to be perfect.
