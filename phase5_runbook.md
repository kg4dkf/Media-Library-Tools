# Phase 5 Runbook — Radarr, Bazarr, Plex

The migration ran. Files are renamed, NFOs are written, duplicates are isolated.
Now we wire up the management layer so the library stays organized as you add
movies, and so subtitles flow automatically.

Three pieces, in order:

1. **Plex** — reload the metadata for the reorganized library (already done if
   you ran `plex_scan.py --force --wait`).
2. **Radarr** — index the existing library and become the manager for any future
   additions.
3. **Bazarr** — connect to Radarr, fetch missing subtitles.

Estimated time: 30 minutes of clicks plus an overnight subtitle scan.

---

## 0. Where you are

Run `py plex_scan.py --config config.json --list` to confirm Plex sees your
Movies section and where it lives on disk. Output should match what we set up.

If you already ran `--force --wait`, every movie folder under `H:\Movies` has
been re-analyzed and the NFO IDs we wrote are now Plex's source of truth for
metadata. Open a few movies in Plex Web — confirm the title, year, poster, and
plot are correct. Special attention to franchise members (Bond, Star Wars,
Harry Potter): they should appear individually but Plex may group them under
their collection.

If something looks wrong on a specific movie, open `H:\Movies\<that
title>\<that title>.nfo` and verify the `<tmdbid>` and `<imdbid>` look right.
If they do, Plex will eventually self-correct on the next agent refresh.

---

## 1. Plex configuration check

Before adding Radarr to the picture, set Plex's Movies library agent
appropriately:

- **Settings → Manage → Libraries → Movies → Manage Recommendations →
  edit library**
- **Advanced** tab. Under **Movie Database**, select the **Plex Movie**
  agent (not the legacy "Plex Movie (Legacy)" or "The Movie Database").
- Under **Use local assets**, ensure **Local Media Assets** is enabled —
  this tells Plex to read your `.nfo`, `<stem>-poster.jpg`, etc. as
  preferred over its own scrapers.
- **Auto-scan**: enable it now (you paused it during phase 1's
  preflight). Default schedule of every few hours is fine.
- **Run a partial scan when changes are detected**: enable. Plex will
  pick up new files within seconds.

After saving, run `py plex_scan.py --config config.json --wait` once more
so Plex re-reads with the agent settings applied.

---

## 2. Radarr — install and configure

### 2a. Install (if you don't have it)

Download from https://radarr.video/#downloads — the Windows installer
(Radarr setup x64 executable). Run it; accept defaults except:

- **Service vs Tray application** — service mode is cleaner for a
  dedicated server box; tray mode is fine if Radarr just runs while you're
  logged in.
- **Database**: SQLite (default). Don't switch to Postgres unless you
  know why you'd want it.

After install, Radarr listens on `http://localhost:7878`. First-time UI:

- Set an authentication method (Forms login or Basic) and pick a username
  and password. None is fine if Radarr is firewalled to localhost; you
  may revisit this later.

### 2b. Set the root folder to H:\Movies

**Settings → Media Management → Root Folders → Add**

- Path: `H:\Movies`
- Quality Profile: **Any** (we'll create one in a moment)
- Movie Profile / Monitor: **Existing**

### 2c. Naming format — match our convention

This is the most important Radarr setting and the one most people get wrong.

**Settings → Media Management → Movie Naming**

- **Rename Movies**: ON
- **Replace Illegal Characters**: ON
- **Standard Movie Format**:
  ```
  {Movie Title} ({Release Year}) {imdb-{ImdbId}}
  ```
- **Movie Folder Format**:
  ```
  {Movie Title} ({Release Year}) {imdb-{ImdbId}}
  ```

Click **Test** under each — Radarr will show you a sample. Verify the
output matches the pattern your library already uses. Save.

### 2d. Quality Profile

**Settings → Profiles → Quality Profiles → Add**

- Name: `Existing` (or whatever)
- Allowed qualities: enable everything from `WORKPRINT` to `Bluray-2160p`
- Cutoff: highest you care about
- Language: **English** (or your preference)

This profile is permissive — it accepts whatever quality your existing
files are. You can create stricter profiles later for new acquisitions.

### 2e. Connect to Plex

**Settings → Connect → + → Plex Media Server**

- Host: `localhost` (or your Plex server's address)
- Port: `32400`
- Auth Token: same Plex token from `config.json`
- Update Library: ON
- Test → Save

Now whenever Radarr renames or imports a movie, Plex auto-refreshes.

### 2f. Import the existing library

**Movies → Import → H:\Movies**

Radarr scans every folder under `H:\Movies`, matches each against TMDB
using the `{imdb-tt#######}` token in the folder name, and queues them as
movies-to-import. The IMDB tag in the folder name is what makes this go
fast and accurate — Radarr knows exactly what each folder is.

For each detected movie:
- Verify the matched title/year is correct
- Click **Import** (or **Import All** if everything looks right)

Movies in franchise sub-folders (e.g. `H:\Movies\James Bond\Dr. No (1962)
{imdb-tt0055928}`) will import correctly because Radarr walks subfolders.

After import, Radarr's Movies tab should show ~2,100 movies. If you see
substantially fewer, the missing ones probably:
- Don't have `{imdb-tt}` in their folder name (the 29 folder_name_pattern
  warnings from verify.py — manual fix needed)
- Are inside a franchise folder Radarr didn't recurse (rare; verify
  Settings → Media Management → "Skip Free Space Check" related options
  aren't blocking)

### 2g. Indexers and download client (optional)

If you also want Radarr to *acquire* new movies, configure indexers (under
**Settings → Indexers**) and a download client (**Settings → Download
Clients**). For library management of an existing collection only, this
is optional. Skip if you only want naming/metadata management.

---

## 3. Bazarr — install and configure

Bazarr fills in missing subtitles automatically.

### 3a. Install

Download from https://www.bazarr.media/ — the Windows installer or
zip. Default install path is fine. Bazarr listens on `http://localhost:6767`.

### 3b. Connect Bazarr to Radarr

Bazarr can talk to Radarr (movies) and Sonarr (TV) — for now, just Radarr.

**Settings → Radarr**

- Use Radarr: ON
- Address: `http://localhost`
- Port: `7878`
- Base URL: leave blank
- API Key: copy from Radarr (**Settings → General → API Key**)
- Test → Save

After save, Bazarr pulls Radarr's full movie list and starts tracking
which subtitles each movie has.

### 3c. Language profile

**Settings → Languages → Language Profiles → Add**

- Name: `English`
- Languages: pick `English`
- Cutoff: `English`
- Hearing Impaired (`hi`) and Forced (`forced`) — your call. I'd leave
  off unless you need them.
- **Use Original Audio Language**: off
- Save.

Then **Settings → Languages → Default Movie Profile**: set to `English`.
This applies to all movies unless you override per-title.

### 3d. Subtitle providers

**Settings → Providers → +**

Free providers worth enabling:
- **OpenSubtitles.com** — register a free account at opensubtitles.com,
  put credentials in. Daily download limits but generous.
- **Subscene** — no account needed; some movies have great fan-translated subs.
- **Addic7ed** — no account for read-only; account gets you more.
- **Podnapisi** — no account needed.
- **Yify Subtitles** — no account.

I'd start with OpenSubtitles + Podnapisi + Subscene. Don't enable
everything at once — you'll hit per-provider rate limits faster.

### 3e. Sync paths

Bazarr writes subtitles next to the video file. If your config matches
Radarr's, paths are auto-aligned. Confirm via **Settings → Radarr** —
the path mappings section should be empty (only fill it if Bazarr runs
in a docker container with different mount points than Radarr).

### 3f. Initial scan and download

**Movies → Wanted** — Bazarr lists every movie missing your default-language
subtitles.

**Search All** to kick off the bulk download. This pulls subtitles for
~2,100 movies, hitting provider rate limits along the way. Realistic
runtime: overnight (8-12 hours), depending on how aggressive the
providers' rate limits are. Bazarr respects rate limits and will resume
automatically.

After it settles, **Movies → Wanted** should be near-empty. Movies still
listed are ones with no subtitles available from any enabled provider —
common for niche titles, foreign-language movies, or recent releases.

---

## 4. Final verification

### 4a. Pick 5-10 movies

Open random movies in Plex Web. For each, verify:

- Correct title, year, poster, plot
- Subtitles available (English) and selectable from the player UI
- Plays without skipping or stuttering

If everything looks right, the migration is complete.

### 4b. Run `verify.py` one more time

```
py verify.py --config config.json
```

The numbers should match the previous clean run. If new errors appeared,
Plex or Radarr may have moved/renamed something during their work.

### 4c. (Optional) Hash verification overnight

```
py verify.py --config config.json --hash-verify
```

Hours-long. Confirms byte integrity of every main video against phase 1's
hash. Worth doing once at end-of-migration as final paranoia. Errors
here would indicate disk corruption — extremely unlikely on a same-drive
rename, but the check is here if you want it.

---

## 5. Troubleshooting

**Radarr import shows "Movie has no files" for some titles.**
The folder name doesn't have `{imdb-tt}` and Radarr can't auto-match.
Either rename the folder to add the IMDB tag, or use Radarr's manual
match: click the movie → Manual Search → pick the right TMDB entry.

**Plex shows duplicate entries for some movies.**
The IMDB tag landed twice in your library. Run:
```
Get-ChildItem H:\Movies -Recurse -Directory |
    Where-Object { $_.Name -like '*{imdb-tt*' } |
    ForEach-Object {
        if ($_.Name -match '\{imdb-(tt\d+)\}') { $matches[1] }
    } |
    Group-Object | Where-Object { $_.Count -gt 1 } |
    Format-Table Name, Count
```
Any output indicates duplicate IDs. Pick which folder to keep, move the
other to `H:\duplicates\` or delete it.

**Bazarr says "no providers available" for many movies.**
Check **Settings → Providers** — at least one should be green. If all are
red or disabled, downloads will silently fail. OpenSubtitles credentials
are the most common point of failure: re-test the login from the OpenSubtitles
provider page in Bazarr.

**Subtitles download but Plex doesn't show them.**
Plex caches what's available. **Settings → Library → Optimize** to force
a re-read of sidecar files. Or just wait — Plex picks them up within
~10 minutes on its own.

**Some titles don't show in Radarr but exist in `H:\Movies`.**
Inspect their folder name — if it's missing `{imdb-tt}`, that's the cause.
The 29 `folder_name_pattern` warnings from verify.py list them. Manually
look up each on TMDB, get its IMDB ID, rename the folder to include
`{imdb-tt#######}`. Then **Library → Update Movies** in Radarr.

**Radarr keeps trying to upgrade movies you don't want upgraded.**
Settings → Profiles → Quality Profile → set Cutoff to your current
quality. Movies at or above cutoff don't get further upgrades.

**Bazarr re-downloads subtitles repeatedly.**
Settings → Subtitles → "Adaptive Searching" — if off, Bazarr will keep
trying providers even after success. Turn it on.

---

## What's done after phase 5

- Library structure: `H:\Movies\<Title> (<Year>) {imdb-tt#######}\<Title>
  (<Year>) {imdb-tt#######}.<ext>` plus matching `.nfo`, posters, extras
- Plex sees ~2,100 movies with rich metadata
- Radarr indexes them and will manage future additions
- Bazarr ensures subtitles stay current
- Duplicates parked in `H:\duplicates\` for manual review
- 6 mojibake titles + 29 IMDB-less folders + 65 missing-from-dups remain as
  manual cleanup items, addressable when convenient

The pipeline (inventory → review → execute → verify → integrate) is
reusable: when you onboard a new bulk batch (a friend's hard drive, an
old USB), you can run phases 1 through 4 on a sandbox folder and have it
ready before merging into the main library.

TV shows are a separate phase 6 with similar structure — the inventory
+ identification logic differs (Sonarr instead of Radarr, episode
numbering, season folders, etc.) but the shape of the pipeline transfers.
