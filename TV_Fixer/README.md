# TV Episode Identifier — content-based pipeline

A set of Python scripts for cleaning up a large TV-rip collection where filenames lack
season/episode info. Identifies episodes by **what's actually in the file** rather than
by filename, using a transcribe-and-match pipeline against a local subtitle corpus.

Designed for the case where FileBot's filename heuristics fail (DVD rips named like
`Avatar- The Last Airbender Book One- Water Disc 1 T00`, with special features and
duplicates mixed in).

## How it works

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  dedupe.py      │    │  fetch_subs.py   │    │  identify.py    │
│  (Chromaprint)  │    │  (OpenSubtitles) │    │  (Whisper+FTS5) │
└────────┬────────┘    └────────┬─────────┘    └────────┬────────┘
         │                      │                       │
         ▼                      ▼                       ▼
   duplicate groups       subtitle corpus         S/E + confidence
   (keep best copy)       (per-show .srt)         (propose renames)
```

**Two independent passes:**

1. **Dedupe pass** (`dedupe.py`) — fingerprints a 120-second audio window of each
   file with Chromaprint and clusters near-identical fingerprints. Two rips of the
   same episode in different codecs/resolutions will land in the same cluster.
   Per cluster, ranks copies by resolution + bitrate so you can keep the best.

2. **Identification pass** (`identify.py`) — for each file, extracts an audio
   segment from past the intro, transcribes it with `faster-whisper`, then queries
   a SQLite FTS5 index of subtitle text built from per-show `.srt` corpora. The
   episode whose subtitles best contain the transcribed dialogue wins, with a
   BM25 confidence score.

Subtitle corpora come from `fetch_subtitles.py` (OpenSubtitles REST API) or any
other source — the indexer doesn't care.

## Why this beats filename matching

- Robust to arbitrary filename junk (`Disc 1 T00`).
- Special features / promos / commentary tracks fall out automatically — their
  dialogue won't match any indexed episode and they end up below the confidence
  threshold.
- Multiple physical copies of the same episode (DVD + Blu-ray rip, etc.) get
  caught by the Chromaprint pass regardless of container or codec.

## External dependencies

You need these installed and on PATH:

| Tool         | Purpose                          | Install on Windows                 |
|--------------|----------------------------------|------------------------------------|
| `ffmpeg`     | Audio extraction, duration probe | `choco install ffmpeg` or download from ffmpeg.org |
| `ffprobe`    | (ships with ffmpeg)              | same                               |
| `fpcalc`     | Chromaprint fingerprinting       | https://acoustid.org/chromaprint — drop in PATH |

## Python setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

GPU acceleration for Whisper is strongly recommended for a 50K-file run. On
Windows with NVIDIA:

```
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

`faster-whisper` will pick these up automatically.

## OpenSubtitles API key (only needed for `fetch_subtitles.py`)

1. Register at https://www.opensubtitles.com/
2. Go to your profile → "Consumers" → create an API consumer
3. Set the env var: `set OPENSUBTITLES_API_KEY=...`

**Rate-limit warning:** Free tier is 5 downloads/day. VIP ($3/month) gets you
1000/day. For a serious run you almost certainly want VIP, or to source subtitles
from elsewhere (archive.org dumps, Addic7ed, your existing collection of `.srt`
sidecars).

The indexer (`index_subs.py`) accepts any directory tree of `.srt` files, so the
source doesn't matter.

## Typical workflow

```powershell
# 1. Dedupe pass across the whole collection
python dedupe.py "D:\Media\TV" --db dedupe.sqlite --report dedupe_report.csv

# 2. For each show, fetch subtitles (one-time per show)
python fetch_subtitles.py --show "Avatar: The Last Airbender" --out subs\avatar

# 3. Build FTS index from all your subtitle .srt files
python index_subs.py subs\ --db subtitles.sqlite

# 4. Identify episodes in a show directory
python identify.py "D:\Media\TV\Avatar" --subs-db subtitles.sqlite ^
                   --show "Avatar: The Last Airbender" ^
                   --report avatar_id.csv

# 5. Review the CSV, then apply renames (the script writes a separate
#    rename_plan.ps1 you can inspect before executing)
```

## Tuning knobs worth knowing

- **`--whisper-model`** — default `base`. Bump to `small` or `medium` if you're
  getting low-confidence matches on a show with sparse dialogue.
- **`--audio-offset` / `--audio-length`** — defaults skip 10% in and take 90s.
  Tweak if intros run long or the show has cold-open dialogue you want to use.
- **`--threshold`** — default BM25 score cutoff. Below this, the file is dumped
  into a review queue rather than getting a proposed rename.
- **`--multi-window`** — try 2 or 3 audio segments and take best match. Slower
  but more robust on shows with thin dialogue.

## Resumability

Every script uses SQLite to track progress. Crash mid-run, re-run the same
command, and it skips already-processed files. Safe to Ctrl-C.

## What this won't catch

- Clip shows that quote dialogue from other episodes — may match the wrong one.
- Silent / heavily musical content (dance shows, nature footage).
- Episodes that aren't on OpenSubtitles (very old or very obscure shows).
- Foreign-language audio + only English subtitles in your corpus (run Whisper
  with `--language` set appropriately, or grab native-language subs).

Review queue is your friend for these.
