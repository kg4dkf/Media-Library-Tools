#!/usr/bin/env python3
"""
Pre-process a media collection: extract audio samples and transcribe them
with faster-whisper now, so identify.py can match against the subtitle
index later without re-doing the heavy lifting.

For each video file under <root>:
  1. ffprobe to get duration etc.
  2. Extract a 90s audio segment (skip first 10% to clear the intro).
  3. Optionally save the segment as Opus 32kbps under <audio-cache>/
     (~360 KB per file -- ~12 GB for a 35K-file library).
  4. Transcribe with faster-whisper.
  5. Store the transcript in the same SQLite DB identify.py uses,
     marked with status='prepared'.

When identify.py runs against the same --state-db, it skips extraction
and transcription for any 'prepared' row -- it just does the BM25 match.

Resumable: SQLite tracks per-file progress; Ctrl-C/reboot is safe.

Typical usage (run in parallel with fetch_subtitles.py):
    python prepare.py "G:\\TV" ^
        --state-db identify_state.sqlite ^
        --audio-cache .audio_cache ^
        --whisper-model base

Tuning:
    --whisper-model       tiny | base | small | medium | large-v3
    --device              auto | cpu | cuda
    --no-save-audio       skip Opus cache (transcripts only)
    --audio-length 120    longer sample for shows with thin dialogue
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from tqdm import tqdm

import common
# Reuse the Transcriber wrapper and DB DDL from identify.py
from identify import Transcriber, open_results


def _safe_path_str(p) -> str:
    """Mirror dedupe.py's path sanitization so paths roundtrip via UTF-8."""
    s = str(p)
    return s.encode("utf-8", errors="replace").decode("utf-8")


def cache_path_for(source: str, cache_root: Path) -> Path:
    """Stable on-disk path derived from a hash of the source path.

    Uses two-level fanout so a 35K-file run doesn't create a single
    huge directory.
    """
    digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()
    return cache_root / digest[:2] / digest[2:4] / f"{digest}.ogg"


def extract_opus(
    in_path: Path, out_path: Path, offset: float, length: float, ffmpeg: str
) -> bool:
    """Extract a length-second segment and encode to Opus 32kbps mono 16k."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-y",
        "-ss", f"{offset:.3f}",
        "-i", str(in_path),
        "-t", f"{length:.3f}",
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libopus", "-b:a", "32k",
        "-f", "ogg",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return out_path.exists() and out_path.stat().st_size >= 256
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("root", type=Path, help="Media root directory")
    ap.add_argument("--state-db", type=Path, default=Path("identify_state.sqlite"),
                    help="SQLite DB shared with identify.py")
    ap.add_argument("--audio-cache", type=Path, default=Path(".audio_cache"),
                    help="Where to store compressed audio samples")
    ap.add_argument("--no-save-audio", action="store_true",
                    help="Skip writing audio cache files; transcripts only")
    ap.add_argument("--audio-offset-pct", type=float, default=0.10)
    ap.add_argument("--audio-length", type=float, default=90.0)
    ap.add_argument("--whisper-model", default="base",
                    help="tiny|base|small|medium|large-v3")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--compute-type", default="auto")
    ap.add_argument("--language", default="en",
                    help="Whisper language code; pass an empty string for auto-detect")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N files this run (0 = unlimited)")
    args = ap.parse_args()

    if not args.root.is_dir():
        sys.exit(f"Not a directory: {args.root}")

    ffmpeg = common.require_tool("ffmpeg")
    ffprobe = common.require_tool("ffprobe")

    device = args.device
    compute_type = args.compute_type
    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    lang = args.language if args.language else None
    transcriber = Transcriber(args.whisper_model, device, compute_type, lang)

    conn = open_results(args.state_db)
    cur = conn.cursor()
    # Any existing row, in any status, counts as already-handled here.
    # If a row was status='error' from a previous run, we leave it alone.
    # Use --state-db rm or DELETE WHERE status='error' to retry failures.
    cur.execute("SELECT path FROM results")
    done = {r[0] for r in cur.fetchall()}

    targets = []
    for p in common.iter_videos(args.root):
        spath = _safe_path_str(p)
        if spath in done:
            continue
        targets.append(p)
        if args.limit and len(targets) >= args.limit:
            break

    if not targets:
        print("No new files to prepare. (Everything already has a row in "
              f"{args.state_db})")
        return

    print(f"Preparing {len(targets)} files. "
          f"whisper={args.whisper_model} device={device} compute={compute_type}")
    if not args.no_save_audio:
        args.audio_cache.mkdir(parents=True, exist_ok=True)
        print(f"Audio cache: {args.audio_cache}")

    ok_n = err_n = 0
    for path in tqdm(targets, desc="prepare", unit="file"):
        spath = _safe_path_str(path)
        info = common.probe(path, ffprobe=ffprobe)
        if info is None or info.duration <= 0:
            conn.execute(
                "INSERT OR REPLACE INTO results "
                "(path, status, detail, processed_at) VALUES (?, ?, ?, ?)",
                (spath, "error", "probe_failed", time.time()),
            )
            conn.commit()
            err_n += 1
            continue
        if info.duration < args.audio_length + 5:
            conn.execute(
                "INSERT OR REPLACE INTO results "
                "(path, status, detail, processed_at) VALUES (?, ?, ?, ?)",
                (spath, "error", "too_short", time.time()),
            )
            conn.commit()
            err_n += 1
            continue

        offset = common.compute_offset(
            info.duration, args.audio_offset_pct, args.audio_length,
        )

        # Extract audio to either the persistent cache (default) or a temp WAV.
        tmpdir: Optional[tempfile.TemporaryDirectory] = None
        try:
            if args.no_save_audio:
                tmpdir = tempfile.TemporaryDirectory()
                sample = Path(tmpdir.name) / "sample.wav"
                common.extract_audio_segment(
                    path, offset, args.audio_length,
                    ffmpeg=ffmpeg, out_path=sample,
                )
                if not sample.exists() or sample.stat().st_size < 1024:
                    raise RuntimeError("audio_extract_failed")
                transcribe_target = sample
            else:
                opus = cache_path_for(spath, args.audio_cache)
                if not opus.exists():
                    if not extract_opus(path, opus, offset, args.audio_length, ffmpeg):
                        raise RuntimeError("audio_extract_failed")
                transcribe_target = opus

            try:
                transcript = transcriber.transcribe_file(transcribe_target)
            except Exception as e:
                raise RuntimeError(f"whisper_failed: {e}")

            conn.execute(
                "INSERT OR REPLACE INTO results "
                "(path, transcript, status, processed_at) VALUES (?, ?, ?, ?)",
                (spath, transcript, "prepared", time.time()),
            )
            conn.commit()
            ok_n += 1
        except Exception as e:
            conn.execute(
                "INSERT OR REPLACE INTO results "
                "(path, status, detail, processed_at) VALUES (?, ?, ?, ?)",
                (spath, "error", str(e)[:200], time.time()),
            )
            conn.commit()
            err_n += 1
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()

    print(f"\nDone. prepared={ok_n}  errors={err_n}")
    print(f"State DB: {args.state_db}")
    if not args.no_save_audio:
        print(f"Audio cache: {args.audio_cache}")
    print("Next: when subtitles are ready, run identify.py against the same"
          " --state-db and it will skip transcription for prepared rows.")


if __name__ == "__main__":
    main()
