#!/usr/bin/env python3
"""
Identify TV episodes by content.

For each video under a root directory:
  1. Extract an audio sample (defaults: skip first 10%, take 90s).
  2. Transcribe with faster-whisper.
  3. Query a SQLite FTS5 subtitle index (built by index_subs.py) for the
     episode whose subtitles best contain the transcribed dialogue.
  4. Emit a CSV report and a PowerShell rename script for review.

Usage:
    python identify.py "D:\\Media\\TV\\Avatar" ^
        --subs-db subtitles.sqlite ^
        --show "Avatar: The Last Airbender" ^
        --report avatar_id.csv ^
        --rename-script avatar_rename.ps1

Notes:
- --show is the show's display name; it's normalized to match index_subs.py's
  show_norm. If omitted, the indexer will query across all shows in the DB
  (slower, weaker matches).
- BM25 scores in SQLite FTS5 are LOWER = better. We invert sign for clarity.
- Confidence is the gap between best and second-best match; below threshold,
  files land in the review queue.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from tqdm import tqdm

import common


# ---------- DB ----------

DDL = """
CREATE TABLE IF NOT EXISTS results (
    path TEXT PRIMARY KEY,
    show TEXT,
    season INTEGER,
    episode INTEGER,
    score REAL,
    margin REAL,
    transcript TEXT,
    status TEXT,             -- 'ok' | 'low_confidence' | 'no_match' | 'error'
    detail TEXT,
    processed_at REAL
);
"""

def open_results(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(DDL)
    conn.commit()
    return conn


# ---------- Whisper ----------

class Transcriber:
    def __init__(self, model_size: str, device: str, compute_type: str, language: Optional[str]):
        try:
            from faster_whisper import WhisperModel
        except Exception:
            sys.exit("pip install faster-whisper")
        print(f"Loading whisper model: {model_size} ({device}, {compute_type})...",
              file=sys.stderr)
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.language = language

    def transcribe_file(self, wav_path: Path) -> str:
        segments, _info = self.model.transcribe(
            str(wav_path),
            language=self.language,
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


# ---------- Matching ----------

# Words to drop from the transcript before turning it into an FTS MATCH query.
# Common words leak into BM25 noise and FTS has no built-in stop list.
STOP = set("""
a an the and or but if then so as of in on at to for from by with into out off up
down over under again is are was were be been being have has had do does did will
would can could should may might must shall i you he she it we they me him her us
them my your his hers its our their this that these those there here what when
where why how which who whom no not yes ok okay yeah right just very really so
oh ah uh um hey hi well now then come go come on let lets let's
""".split())


def to_fts_query(transcript: str, max_terms: int = 60) -> str:
    """Reduce a transcript to a usable FTS5 MATCH query.

    Tokens are letters only -- apostrophes are NOT kept, because FTS5's
    MATCH parser rejects them as bareword characters and the whole query
    becomes a syntax error. Contractions like "it's", "can't", "i'm"
    therefore tokenize to ["it","s"], ["can","t"], ["i","m"] and get
    filtered out (the leading parts are in the STOP list, and the trailing
    letters are too short). That's fine -- contractions carry little
    discriminative weight for episode matching.
    """
    words = re.findall(r"[a-z]+", transcript.lower())
    kept = []
    seen = set()
    for w in words:
        if len(w) < 3:
            continue
        if w in STOP:
            continue
        if w in seen:
            continue
        seen.add(w)
        kept.append(w)
        if len(kept) >= max_terms:
            break
    return " OR ".join(kept) if kept else ""


@dataclass
class Match:
    show: str
    season: int
    episode: int
    score: float


def search(
    conn: sqlite3.Connection,
    query: str,
    show_norm: Optional[str],
    top_k: int = 5,
) -> List[Match]:
    if not query:
        return []
    sql = (
        "SELECT show_norm, season, episode, bm25(episodes_fts) AS score "
        "FROM episodes_fts WHERE episodes_fts MATCH ? "
    )
    params: list = [query]
    if show_norm:
        sql += "AND show_norm = ? "
        params.append(show_norm)
    sql += "ORDER BY score ASC LIMIT ?"
    params.append(top_k)
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
    except sqlite3.OperationalError as e:
        # Surface the failure once via stderr instead of silently returning
        # nothing -- a malformed MATCH query (apostrophes, quote chars, etc.)
        # used to cause every file to land in no_match without explanation.
        import sys as _sys
        _sys.stderr.write(
            f"WARN: FTS5 MATCH failed for query={query!r}; error={e}\n"
        )
        return []
    return [Match(show=row[0], season=row[1], episode=row[2], score=-float(row[3]))
            for row in cur.fetchall()]


# ---------- Main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path, help="Directory of video files to identify")
    ap.add_argument("--subs-db", type=Path, required=True,
                    help="SQLite FTS index built by index_subs.py")
    ap.add_argument("--show", help="Show name (restricts matching to this show)")
    ap.add_argument("--state-db", type=Path, default=Path("identify_state.sqlite"),
                    help="SQLite db for per-file progress (resumable)")
    ap.add_argument("--report", type=Path, default=Path("identify_report.csv"))
    ap.add_argument("--rename-script", type=Path,
                    default=Path("rename_plan.ps1"),
                    help="PowerShell rename plan written for review (not auto-executed)")
    ap.add_argument("--audio-offset-pct", type=float, default=0.10)
    ap.add_argument("--audio-length", type=float, default=90.0)
    ap.add_argument("--multi-window", type=int, default=1,
                    help="Try N spaced windows; keep best match")
    ap.add_argument("--whisper-model", default="base",
                    help="tiny|base|small|medium|large-v3")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    ap.add_argument("--compute-type", default="auto",
                    help="auto|int8|int8_float16|float16|float32")
    ap.add_argument("--language", default="en", help="Whisper language code")
    ap.add_argument("--threshold", type=float, default=15.0,
                    help="Minimum (inverted) BM25 score to accept; below = review queue. "
                         "Observed: real matches score 40+, false positives 10-30.")
    ap.add_argument("--min-margin", type=float, default=5.0,
                    help="Required score gap between best and second-best match. "
                         "Observed: real matches have margin 15+, false positives 1-3.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N files (0 = unlimited)")
    args = ap.parse_args()

    if not args.root.is_dir():
        sys.exit(f"Not a directory: {args.root}")
    if not args.subs_db.is_file():
        sys.exit(f"Subtitles DB not found: {args.subs_db}")

    ffmpeg = common.require_tool("ffmpeg")
    ffprobe = common.require_tool("ffprobe")

    # Resolve device/compute_type sensibly.
    device = args.device
    compute_type = args.compute_type
    if device == "auto":
        try:
            import torch  # noqa
            device = "cuda" if __import__("torch").cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    transcriber = Transcriber(args.whisper_model, device, compute_type, args.language)

    subs_conn = sqlite3.connect(str(args.subs_db))
    state_conn = open_results(args.state_db)

    show_norm = common.normalize_show_name(args.show) if args.show else None

    # Treat a row as "already done" only if it has reached a terminal status.
    # Rows with status='prepared' (written by prepare.py) still need matching,
    # so they are NOT in the done set -- they get their cached transcript
    # injected below and skip straight to the FTS search.
    TERMINAL = ("ok", "low_confidence", "no_match", "error")
    done = {
        r[0] for r in state_conn.execute(
            f"SELECT path FROM results WHERE status IN ({','.join('?' * len(TERMINAL))})",
            TERMINAL,
        )
    }
    prepared_transcripts: dict[str, str] = {
        r[0]: r[1] or "" for r in state_conn.execute(
            "SELECT path, transcript FROM results WHERE status = 'prepared'"
        )
    }

    def _safe_path(p) -> str:
        return str(p).encode("utf-8", errors="replace").decode("utf-8")

    def _source_show_norm(path_str: str) -> Optional[str]:
        """Pull the show folder name out of a file's path and normalize it
        to the same form index_subs.py used. Returns None if the path isn't
        under args.root (e.g., subdirectory mismatch) or has no show dir."""
        try:
            rel = Path(path_str).relative_to(args.root)
        except ValueError:
            return None
        if len(rel.parts) < 2:
            return None
        return common.normalize_show_name(rel.parts[0])

    targets = []
    for p in common.iter_videos(args.root):
        if _safe_path(p) in done:
            continue
        targets.append(p)
        if args.limit and len(targets) >= args.limit:
            break

    if not targets:
        print("No new files to identify.")
    else:
        n_prepared = sum(1 for p in targets if _safe_path(p) in prepared_transcripts)
        if prepared_transcripts:
            print(f"Identifying {len(targets)} files "
                  f"({n_prepared} already-prepared, "
                  f"{len(targets) - n_prepared} need transcription)...")
        else:
            print(f"Identifying {len(targets)} files...")

    for path in tqdm(targets, desc="identify", unit="file"):
        spath = _safe_path(path)

        # Fast path: prepare.py already transcribed this file.
        if spath in prepared_transcripts:
            transcript = prepared_transcripts[spath]
            query = to_fts_query(transcript)
            transcript_used = transcript

            # Constrain matching to the file's own show folder first. Falls
            # back to global search only if the constrained search returns
            # nothing -- and in that case the result is flagged as a
            # cross-show match for manual review.
            src_show_norm = show_norm or _source_show_norm(spath)
            in_show_hits = (
                search(subs_conn, query, show_norm=src_show_norm, top_k=2)
                if query and src_show_norm else []
            )
            best = in_show_hits[0] if in_show_hits else None
            best_runner_up = in_show_hits[1] if len(in_show_hits) > 1 else None
            cross_show = False
            if best is None and query:
                # Try unconstrained; if we find something, flag it.
                cross_hits = search(subs_conn, query, show_norm=None, top_k=2)
                if cross_hits:
                    best = cross_hits[0]
                    best_runner_up = cross_hits[1] if len(cross_hits) > 1 else None
                    cross_show = True

            if best is None:
                state_conn.execute(
                    "INSERT OR REPLACE INTO results "
                    "(path, score, status, transcript, processed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (spath, 0.0, "no_match", transcript_used, time.time()),
                )
                state_conn.commit()
                continue

            margin = best.score - (best_runner_up.score if best_runner_up else 0.0)
            if cross_show:
                status = "cross_show_match"
                detail = f"source folder show_norm={src_show_norm!r}; proposed alt show"
            elif best.score < args.threshold or margin < args.min_margin:
                status = "low_confidence"
                detail = None
            else:
                status = "ok"
                detail = None
            state_conn.execute(
                "INSERT OR REPLACE INTO results "
                "(path, show, season, episode, score, margin, transcript, "
                " status, detail, processed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (spath, best.show, best.season, best.episode,
                 best.score, margin, transcript_used, status, detail, time.time()),
            )
            state_conn.commit()
            continue

        info = common.probe(path, ffprobe=ffprobe)
        if info is None or info.duration <= 0:
            state_conn.execute(
                "INSERT OR REPLACE INTO results (path, status, detail, processed_at) "
                "VALUES (?, ?, ?, ?)",
                (spath, "error", "probe_failed", time.time()),
            )
            state_conn.commit()
            continue

        best: Optional[Match] = None
        best_runner_up: Optional[Match] = None
        transcript_used = ""

        # Pick N evenly-spaced windows in the "middle" of the file.
        usable = max(0.0, info.duration - args.audio_length - 30.0)
        start = max(30.0, info.duration * args.audio_offset_pct)
        spacing = (usable - start) / max(1, args.multi_window) if args.multi_window > 1 else 0
        offsets = [start + i * spacing for i in range(args.multi_window)]

        with tempfile.TemporaryDirectory() as td:
            for off in offsets:
                wav = Path(td) / f"sample_{int(off)}.wav"
                ok = common.extract_audio_segment(
                    path, off, args.audio_length, ffmpeg=ffmpeg, out_path=wav,
                )
                # extract_audio_segment returns None for file mode (success or failure).
                if not wav.exists() or wav.stat().st_size < 1024:
                    continue
                try:
                    transcript = transcriber.transcribe_file(wav)
                except Exception as e:
                    state_conn.execute(
                        "INSERT OR REPLACE INTO results "
                        "(path, status, detail, processed_at) VALUES (?, ?, ?, ?)",
                        (spath, "error", f"whisper_failed: {e}", time.time()),
                    )
                    state_conn.commit()
                    transcript = ""
                    break

                query = to_fts_query(transcript)
                if not query:
                    continue
                # Constrain to the file's own show first (per-file).
                src_show_norm = show_norm or _source_show_norm(spath)
                hits = search(subs_conn, query, show_norm=src_show_norm, top_k=2)
                if not hits:
                    continue
                top = hits[0]
                runner = hits[1] if len(hits) > 1 else None
                if best is None or top.score > best.score:
                    best = top
                    best_runner_up = runner
                    transcript_used = transcript

        if best is None:
            state_conn.execute(
                "INSERT OR REPLACE INTO results "
                "(path, score, status, transcript, processed_at) VALUES (?, ?, ?, ?, ?)",
                (spath, 0.0, "no_match", transcript_used, time.time()),
            )
            state_conn.commit()
            continue

        margin = best.score - (best_runner_up.score if best_runner_up else 0.0)
        status = "ok"
        if best.score < args.threshold or margin < args.min_margin:
            status = "low_confidence"

        state_conn.execute(
            "INSERT OR REPLACE INTO results "
            "(path, show, season, episode, score, margin, transcript, status, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (spath, best.show, best.season, best.episode,
             best.score, margin, transcript_used, status, time.time()),
        )
        state_conn.commit()

    # ---------- Reports ----------

    rows = list(state_conn.execute(
        "SELECT path, show, season, episode, score, margin, status, detail "
        "FROM results ORDER BY status, path"
    ))

    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "path", "show", "season", "episode", "score",
                    "margin", "proposed_name", "detail"])
        for row in rows:
            path, show, season, episode, score, margin, status, detail = row
            proposed = ""
            if season is not None and episode is not None:
                proposed = f"S{season:02d}E{episode:02d}{Path(path).suffix}"
            w.writerow([status, path, show or "", season or "", episode or "",
                        f"{score:.2f}" if score is not None else "",
                        f"{margin:.2f}" if margin is not None else "",
                        proposed, detail or ""])

    # PowerShell rename plan: KEEP COMMENTED until you've reviewed the CSV.
    with args.rename_script.open("w", encoding="utf-8") as f:
        f.write("# Rename plan generated by identify.py\n")
        f.write("# REVIEW THIS FILE BEFORE EXECUTING. Lines are commented out by default.\n")
        f.write("# To apply: uncomment the Move-Item lines you trust.\n\n")
        for row in rows:
            path, show, season, episode, score, margin, status, detail = row
            if status != "ok" or season is None or episode is None:
                continue
            p = Path(path)
            new_name = f"S{season:02d}E{episode:02d}{p.suffix}"
            new_path = p.with_name(new_name)
            f.write(f"# score={score:.2f} margin={margin:.2f}\n")
            f.write(f"# Move-Item -LiteralPath {ps_quote(str(p))} "
                    f"-Destination {ps_quote(str(new_path))}\n\n")


    total = len(rows)
    ok = sum(1 for r in rows if r[6] == "ok")
    low = sum(1 for r in rows if r[6] == "low_confidence")
    nomatch = sum(1 for r in rows if r[6] == "no_match")
    cross = sum(1 for r in rows if r[6] == "cross_show_match")
    err = sum(1 for r in rows if r[6] == "error")
    print(f"Done. {total} files: ok={ok}, low_confidence={low}, "
          f"no_match={nomatch}, cross_show_match={cross}, error={err}")
    print(f"  CSV report:     {args.report}")
    print(f"  Rename script:  {args.rename_script}  (review before executing)")


def ps_quote(s: str) -> str:
    """Single-quote a string for PowerShell, escaping embedded single quotes."""
    return "'" + s.replace("'", "''") + "'"


if __name__ == "__main__":
    main()
