# Phase 2 — Cleanup Preview Review

Four CSVs for you to look over before any file is touched. Open them in Excel/LibreOffice. Edit the `user_override` (or `action`) column where you disagree, save, and tell me you're done.

---

## 1. `cleanup_zero_byte_to_quarantine.csv`  (758 rows)

Every 0-byte placeholder file. These were almost certainly failed downloads.

| Action default | What I'll do |
|---|---|
| `MOVE` | Move to `E:\Music\_QUARANTINE\zero_byte\<original-rel-path>` |

If you want to keep any (you won't — they're 0 bytes), change `action` to anything other than `MOVE`. Big offender: `Bring On The Night.mp4` × 683 copies, all 0 bytes.

---

## 2. `cleanup_read_errors_to_quarantine.csv`  (1,729 rows)

Files mutagen couldn't parse — mostly MP3s where the audio frames are corrupt or missing, plus 425 `.mp4` files that aren't actually MP4 audio. Some of these may be wrong-extension content (e.g., a renamed video) that you'd want to recover.

| Action default | What I'll do |
|---|---|
| `MOVE` | Move to `E:\Music\_QUARANTINE\unreadable\<original-rel-path>` |

After Phase 3 you can run a quick `file` command over the quarantine to see what's actually in there.

---

## 3. `cleanup_dedupe_to_quarantine.csv`  (78,801 rows = 31,166 keepers + 47,635 losers)

Every audio file that has a hash-identical twin somewhere. Each row has:

- `group_id` — sort by this to see all copies in a duplicate group together
- `role` — `KEEP` or `QUARANTINE`
- `path_score` — lower = more likely to be the canonical location (genre buckets get +1000, "compilation/playlist" tokens get +200, longer paths get penalized lightly)
- `keeper_rel_path` — which sibling I picked as the keeper

**~298 GB of duplicate copies will be quarantined.**

If you disagree with a specific keeper choice, change the `role` from `QUARANTINE` to `KEEP` on the row you want kept, and from `KEEP` to `QUARANTINE` on the original keeper. (The execution script will read these flags.)

Spot-check tip: sort by `group_size` descending. The top groups (7×, 8×) are mostly Seamus Kennedy tracks scattered across Irish/Gaelic buckets; the keeper consistently picks the cleanest top-level folder.

---

## 4. `cleanup_depth1_categorization.csv`  (446 rows)

Every depth-1 folder under `E:\Music\` mapped to a destination bucket.

**Auto-classification results:**

| Category | Count | What happens |
|---|--:|---|
| ARTIST | 340 | Contents go to `Music\<Letter>\<Artist>\<Year - Album>\…` |
| COMPILATION | 40 | Contents go to `Music\Compilations\…` |
| SOUNDTRACK | 12 | Contents go to `Music\Soundtracks\…` |
| RADIO | 5 | Contents go to `Music\Radio\…` (WVUM-derived) |
| SOUNDEFFECTS | 1 | Contents go to `Music\Sound Effects\…` |
| VIDEOGAME | 1 | Contents go to `Music\Video Game Soundtracks\…` |
| DISSOLVE | 14 | The bucket itself disappears; each file routes to a destination based on its own tags (this catches `90s\`, `Country\`, `Rock\`, etc.) |
| **REVIEW** | 33 | Need your call |

**Confidence:** 359 high · 54 medium · 33 low (= the REVIEW pile)

**Override how:** edit the `user_override` column. Valid values:
`ARTIST` · `SOUNDTRACK` · `COMPILATION` · `RADIO` · `AUDIOBOOK` · `COMEDY` · `SOUNDEFFECTS` · `VIDEOGAME` · `DISSOLVE` · `KEEP_AS_IS` · `IGNORE`

Leave `user_override` blank to accept the auto value.

### Notes on the REVIEW pile

| Folder | Likely call |
|---|---|
| Irish Celtic Music Collection Version 2 (10 GB, 0 audio) | Probably a torrent of non-audio files (videos / rars). Mark `IGNORE` and we'll skip it, or tell me if you want me to peek inside. |
| Irish Gaelic Language Learning Pack (2.88 GB, 0 audio) | Probably an audiobook/coursework dump. `AUDIOBOOK` or `IGNORE`. |
| Japanese Kodo Drummers... (0.68 GB, 0 audio) | Probably a video file. `IGNORE`. |
| Celtic Mythology A to Z | `AUDIOBOOK`. |
| Downloads · Google Play · Misc · Janis Joplin · The Police · Melanie's · The Rolling Stone 100 Greatest... | All show 0 scanned audio — these may be empty stubs or hold non-audio. Likely `IGNORE`. |
| Lemon Demon (633 files, untagged) | Clearly `ARTIST` — just needs tags fixed in Phase 6. |
| Sesame Street Musical Guests (41) | `COMPILATION` (mixed artists per show). |
| Sponge - Rotting Piñata (11) | `ARTIST` (it's an album by Sponge). |
| Gioachino Rossini · Giuseppe Verdi · Johann Sebastian Bach · Garrick Ohlsson | Classical — `ARTIST`, but tag fixup will be needed because the "tag_artist" is the work title, not the composer/performer. |
| Queen - Greatest Hits I - II - III Platinum Collection | Edge call — `ARTIST` if you keep Queen-only Greatest Hits with the artist; `COMPILATION` if you treat it as a multi-disc cross-album set. |
| Ranma 1/2 OST · Ranma 1/2 all openings | `SOUNDTRACK`. |
| The Phantom Of The Opera Original London Cast | `SOUNDTRACK`. |
| VA - Celtic Moods 2CDs (35 files, 8 artists) | `COMPILATION` (the "VA -" prefix says it). |
| Phil Collins & Mark Mancina | `SOUNDTRACK` (this is Tarzan soundtrack). |
| Eric Clapton · Daft Punk (small folder) | `ARTIST`. |
| Family Friendly (52, untagged) | Tell me — playlist? Compilation? Likely `COMPILATION`. |
| New folder (2 files) | Almost certainly `IGNORE`. |
| Veruca Salt - American Thighs · Fountains of Wayne - Welcome Interstate Managers · bagatelle songs · Ireland's Greatest Hits | `ARTIST` — small mismatches from features/tag noise. |

---

## Order of operations after you finish reviewing

1. You save changes to all four CSVs.
2. I generate quarantine move scripts (Phase 3) — actually moves zero-byte, unreadable, and duplicate-loser files to `E:\Music\_QUARANTINE\…`. Reversal log produced.
3. I take a fresh metadata snapshot (much smaller, post-quarantine).
4. I generate the rename preview CSV (Phase 4) for every remaining audio file using your category decisions.
5. You review the rename preview.
6. I execute the renames.
7. Tag fixup (Phase 6).
8. Album art (Phase 7).
