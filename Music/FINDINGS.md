# Music Collection Cleanup — Findings Report

**Snapshot date:** 2026-05-07 (E:\Music)
**Scope:** 205,380 files / 111,602 audio / 12,243 folders / 800.41 GB

---

## TL;DR

The collection is heavily nested inside genre buckets that need to be flattened, and there's a lot of duplicate and zero-byte cruft to retire before the rename. **Roughly 71% of audio (about 79,000 files) lives inside genre/era buckets** like `Rock\`, `90s\`, `Irish\`, `Soundtracks\`, `WVUM\`. Tags are reasonably good (90% have artist, 83% have album, 94% have title) but inconsistent enough that automated renaming will need a clear precedence rule. **There are ~298 GB of redundant duplicate copies** that should be deduped before reorganizing — otherwise we move them into the new tree, then have to clean them up again.

---

## 1. Scope / format mix

| Extension | Files | Size |
|---|--:|--:|
| .mp3 | 98,564 | 567.4 GB |
| .flac | 3,889 | 101.1 GB |
| .m4a | 6,209 | 28.4 GB |
| .mp4 | 1,405 | 33.5 GB |
| .wma | 1,426 | 3.1 GB |
| .wav | 64 | 5.7 GB |
| .m4p (DRM) | 34 | 0.1 GB |
| .ogg | 10 | 0.4 GB |

**Companion files:** 41,223 `.lrc` (synced lyrics) — 41,208 of them have a matching audio sibling in the same folder. **These must move with their MP3 during the rename.**

---

## 2. Errors and unreadable files (2,412)

| Category | Count |
|---|--:|
| MP3 `HeaderNotFoundError: can't sync to MPEG frame` | 1,963 |
| `mutagen: unrecognized format` (mostly .mp4) | 425 |
| FLAC: not a valid FLAC file | 23 |
| Other | 1 |

The 1,963 unsynced MP3s are likely either truncated downloads, mis-extensioned files (e.g., a renamed video), or DRM-protected content. **Drilldown:** `analysis_read_errors.csv`.

The 34 `.m4p` files are DRM-protected iTunes purchases. We can't retag those — they need to be excluded from the tag-write phase, just renamed/moved.

---

## 3. Garbage to retire before reorganizing

- **758 zero-byte files** (287 mp3, 394 mp4, 32 jpg, etc.) — almost certainly failed downloads/placeholders. The big eye-popper: `Bring On The Night.mp4` exists as a 0-byte file 683 times across the tree. Drilldown: `analysis_zero_byte_files.csv`.
- **5,701 audio files under 1 MB** — likely junk, snippets, or broken.
- **6,482 audio files with no artist, no album, AND no title tag** — pure mystery files. Drilldown: `analysis_no_tags.csv`.

---

## 4. Duplicates by hash — biggest win

- **31,167** distinct hash groups have more than one copy.
- **79,484** files (71% of all audio!) are involved in some duplicate group.
- **48,317** redundant copies could be removed.
- **~298 GB** of recoverable space.

Top patterns include:
- 7× copies of every Seamus Kennedy track scattered through `Gaelic\Irish Celtic Music Collection\Seamus Kennedy\`, `Irish\`, etc.
- Whole-album-as-single-FLAC rips of James Bond soundtracks duplicated alongside their split versions (1.9 GB WAV alumni show duplicated, ~400 MB FLAC Bond films duplicated).

Drilldown: `analysis_duplicates_audio.csv` — sorted by group size, includes bitrate so we can pick the best copy.

---

## 5. Tag completeness (audio files, error-free, n=109,190)

| Field | Coverage |
|---|--:|
| title | 94.0% |
| artist | 90.0% |
| album | 83.1% |
| track number | 82.7% |
| date/year | 81.4% |
| genre | 79.5% |
| album_artist | 72.8% |
| embedded art | 37.0% |
| composer | 19.6% |

The composer gap is mostly classical and soundtracks. The album_artist gap matters for compilations: when artist and album_artist disagree, that's our signal that a track is on a compilation rather than an artist album.

---

## 6. Bitrate distribution (audio)

| Bucket | Files |
|---|--:|
| <96 kbps (very low) | 4,450 |
| 96-127 | 5,945 |
| 128-159 | 31,423 |
| 160-191 | 7,321 |
| 192-255 | 20,230 |
| 256-319 | 4,776 |
| 320-499 (mp3 max) | 31,009 |
| ≥500 (lossless) | 3,931 |
| unknown | 2,517 |

About 4% of audio is below 96 kbps — worth flagging as candidates to re-rip when CDs are digitized.

---

## 7. Genre/era buckets that need flattening

These all sit at depth 1 of `E:\Music\` and contain artist subfolders that need to be hoisted up to `Letter\Artist\`:

| Bucket | Subfolders | Audio files | Distinct artists |
|---|--:|--:|--:|
| WVUM | 914 | 14,867 | 839 |
| Irish | 1,340 | 12,607 | 688 |
| Rock | 1,551 | 10,955 | 644 |
| 70s | 802 | 7,144 | 573 |
| 90s | 787 | 6,252 | 1,056 |
| Soundtracks | 860 | 6,098 | 1,518 |
| 80s | 561 | 5,083 | 614 |
| Gaelic | 214 | 3,899 | 446 |
| Oldies | 761 | 3,266 | 431 |
| Country | 726 | 3,216 | 281 |
| Alternative | 297 | 2,634 | 147 |
| Classical | 492 | 1,714 | 250 |
| Pop | 161 | 1,414 | 11 |
| Video Game Sounds | 7 | 248 | 1 |

Total ≈ 79,000 audio files inside buckets. WVUM alone is 13% of the entire collection — radio-station alumni shows that may be live performances or interviews; we'll need a policy decision (own folder vs. attribute to artist).

---

## 8. Scattered artists (same artist in many places)

Top offenders by spread:

| Artist | Top-level folders | Files |
|---|--:|--:|
| Various Artists | 21 | 800 |
| David Bowie | 11 | 2,483 |
| They Might Be Giants | 8 | 7,300 |
| Depeche Mode | 8 | 1,632 |
| The Beatles | 8 | 1,198 |
| Aerosmith | 8 | 676 |
| Sting | 12 | 527 |
| Mariah Carey | 8 | 425 |
| Weezer | 8 | 248 |
| Radiohead | 8 | 225 |
| Elton John | 11 | 169 |

Drilldown: `analysis_scattered_artists.csv` — every (artist, top-folder) pair so we can see exactly where each artist's tracks landed.

---

## 9. Tag/folder disagreements

When the parent folder name disagrees with the artist tag, we usually want to trust the tag. Top mismatches:

| Folder name | Tag artist | Files |
|---|---|--:|
| `Jack Benny` | `Jack Benny Show` | 587 |
| `Beethoven` | `Robert Greenberg` | 144 |
| `They Might Be Giants` | `They Might Be Giants (TMBG)` | 249 |
| `They Might Be Giants\Apollo 18` | `They Might Be Giants` | 226 |
| `The Doors\Live in New York` | `The Doors` | 180 |
| `Irish Celtic Music Collection\Dropkick Murphys` | `Dropkick Murphys` | 408 |

The "Beethoven → Robert Greenberg" case is interesting — that's almost certainly the audiobook *Beethoven (lectures)* by Robert Greenberg living inside the Beethoven music folder. Suggests we should sweep for audiobooks.

Drilldown: `analysis_tag_folder_mismatches.csv`.

---

## 10. Loose audio at the root

403 audio files sit directly in `E:\Music\` with no folder. Most are correctly tagged, so they can be auto-routed by tag (`Letter\Artist\Year - Album\…`). Drilldown: `analysis_loose_top_level_audio.csv`.

---

## Drilldown CSVs (in `H:\Music Cleanup\`)

| File | Rows | Use for |
|---|--:|---|
| `analysis_duplicates_audio.csv` | 79k+ | Pick keepers, mark deletions |
| `analysis_zero_byte_files.csv` | 758 | Bulk delete |
| `analysis_tag_folder_mismatches.csv` | 100k+ | Decide tag-vs-folder precedence |
| `analysis_missing_artist_tag.csv` | 11k+ | Recover from folder name or MusicBrainz |
| `analysis_no_tags.csv` | 6,482 | Manual review or audio-fingerprint lookup |
| `analysis_read_errors.csv` | 2,412 | Quarantine corrupt/wrong-extension |
| `analysis_scattered_artists.csv` | 6,038 | Confirm consolidation strategy |
| `analysis_depth1_folders_for_review.csv` | 446 | Categorize each top-level folder |
| `analysis_loose_top_level_audio.csv` | 403 | Auto-route by tag |
| `analysis_various_artists_tracks.csv` | ~1,200 | Compilation routing |
| `analysis_drm_m4p_files.csv` | 34 | Excluded from retag |
