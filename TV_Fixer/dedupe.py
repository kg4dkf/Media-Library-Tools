#!/usr/bin/env python3
"""
Chromaprint-based video dedupe.

Fingerprints a fixed audio window of every video file under a root directory,
clusters near-identical fingerprints, and emits a CSV report identifying
duplicate copies of the same content (regardless of codec, container, or
resolution).

Per cluster, copies are ranked by (height, vbitrate, size) so you can keep
the best one.

Usage:
    python dedupe.py "D:\\Media\\TV" --db dedupe.sqlite --report dedupe.csv

External tools: ffmpeg, ffprobe, fpcalc (all on PATH).
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import struct
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from tqdm import tqdm

import common


# ---------- Fingerprint plumbing ----------

def chromaprint(
    path: Path,
    offset: float,
    length: float,
    ffmpeg: str,
    fpcalc: str,
) -> Optional[Tuple[str, int]]:
    """
    Pipe a length-second mono PCM stream from `path` starting at `offset`
    into fpcalc with -raw, returning a comma-separated string of 32-bit
    fingerprint integers plus the duration in seconds, or None.

    Using -raw avoids the need for pyacoustid / libchromaprint at decode time;
    fpcalc itself emits the raw fingerprint ints.
    """
    ff = subprocess.Popen(
        [
            ffmpeg, "-v", "error", "-nostdin",
            "-ss", f"{offset:.3f}",
            "-i", str(path),
            "-t", f"{length:.3f}",
            "-vn", "-ac", "1", "-ar", "22050",
            "-f", "wav", "pipe:1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        wav_bytes = ff.stdout.read() if ff.stdout else b""
        fp = subprocess.run(
            [fpcalc, "-length", str(int(length)), "-raw", "-json", "-"],
            input=wav_bytes,
            capture_output=True, timeout=180,
        )
    except subprocess.SubprocessError:
        return None
    finally:
        if ff.stdout:
            ff.stdout.close()
        try:
            ff.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ff.kill()

    if fp.returncode != 0:
        return None

    try:
        import json
        data = json.loads(fp.stdout.decode("utf-8", "ignore"))
    except Exception:
        return None

    raw = data.get("fingerprint")
    dur = int(float(data.get("duration", 0) or 0))
    if raw is None:
        return None

    # fpcalc -raw -json can emit either an array of ints or a comma-separated
    # string depending on version. Normalize to a comma-separated string.
    if isinstance(raw, list):
        if not raw:
            return None
        fp_str = ",".join(str(int(x)) for x in raw)
    elif isinstance(raw, str):
        if not raw.strip():
            return None
        fp_str = raw
    else:
        return None

    return fp_str, dur


def decode_fp(fp_str: str) -> List[int]:
    """
    Parse a comma-separated string of raw chromaprint integers (as produced
    by `fpcalc -raw`) back into a Python int list. Values may be signed; we
    mask to unsigned 32-bit for consistent XOR math in ber().
    """
    out: List[int] = []
    for tok in fp_str.split(","):
        tok = tok.strip()
        if not tok:
            continue
        out.append(int(tok) & 0xFFFFFFFF)
    return out


def ber(a: List[int], b: List[int]) -> float:
    """Bit-error rate between two int arrays. 0.0 = identical."""
    n = min(len(a), len(b))
    if n == 0:
        return 1.0
    errs = 0
    for i in range(n):
        errs += (a[i] ^ b[i]).bit_count() if hasattr(int, "bit_count") else bin(a[i] ^ b[i]).count("1")
    return errs / (n * 32)


def bucket_key(ints: List[int]) -> bytes:
    """
    Coarse LSH bucket: top byte of first 4 ints. Files with the same prefix
    are candidates for full comparison. Cheap, conservative collisions, but
    keeps pairwise comparison count manageable.
    """
    head = ints[:4] if len(ints) >= 4 else ints + [0] * (4 - len(ints))
    return struct.pack(">IIII", *[x & 0xFFFFFFFF for x in head])


# ---------- Database ----------

DDL = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    size        INTEGER,
    duration    REAL,
    width       INTEGER,
    height      INTEGER,
    vbitrate    INTEGER,
    fp          TEXT,                  -- compressed fingerprint
    fp_dur      INTEGER,
    bucket      BLOB,
    error       TEXT,                  -- non-null if probe/fp failed
    processed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_bucket ON files(bucket);
"""

def opendb(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(DDL)
    conn.commit()
    return conn


# ---------- Pipeline ----------

def _safe_path_str(p) -> str:
    """Return str(p) with any non-UTF-8-encodable characters replaced.

    Windows / NTFS allows unpaired Unicode surrogates in filenames; SQLite
    stores TEXT as UTF-8, which forbids them. Round-tripping through
    encode('utf-8', errors='replace') swaps any offending code points for
    U+FFFD so the path is safe to store. The actual Path object retains the
    real codepoints and continues to work with subprocess/ffmpeg.
    """
    s = str(p)
    return s.encode("utf-8", errors="replace").decode("utf-8")


def fingerprint_pass(
    root: Path, conn: sqlite3.Connection,
    offset_pct: float, length: float,
    ffmpeg: str, fpcalc: str, ffprobe: str,
) -> None:
    """Fingerprint every video under root that we haven't seen yet."""
    cur = conn.cursor()
    cur.execute("SELECT path FROM files")
    done = {r[0] for r in cur.fetchall()}

    targets = [p for p in common.iter_videos(root) if _safe_path_str(p) not in done]
    if not targets:
        print("No new files to fingerprint.")
        return

    for path in tqdm(targets, desc="fingerprint", unit="file"):
        spath = _safe_path_str(path)
        info = common.probe(path, ffprobe=ffprobe)
        if info is None or info.duration <= 0:
            conn.execute(
                "INSERT OR REPLACE INTO files (path, error, processed_at) VALUES (?, ?, ?)",
                (spath, "probe_failed", time.time()),
            )
            conn.commit()
            continue

        if info.duration < length + 5:
            # Too short for our window; just record metadata, no fingerprint.
            conn.execute(
                "INSERT OR REPLACE INTO files "
                "(path, size, duration, width, height, vbitrate, error, processed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (spath, info.size, info.duration, info.width, info.height,
                 info.vbitrate, "too_short", time.time()),
            )
            conn.commit()
            continue

        offset = common.compute_offset(info.duration, offset_pct, length)
        fp = chromaprint(path, offset, length, ffmpeg=ffmpeg, fpcalc=fpcalc)

        if fp is None:
            conn.execute(
                "INSERT OR REPLACE INTO files "
                "(path, size, duration, width, height, vbitrate, error, processed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (spath, info.size, info.duration, info.width, info.height,
                 info.vbitrate, "fpcalc_failed", time.time()),
            )
            conn.commit()
            continue

        fp_str, fp_dur = fp
        try:
            ints = decode_fp(fp_str)
            bk = bucket_key(ints)
        except Exception as e:
            conn.execute(
                "INSERT OR REPLACE INTO files "
                "(path, size, duration, width, height, vbitrate, error, processed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (spath, info.size, info.duration, info.width, info.height,
                 info.vbitrate, f"decode_failed: {e}", time.time()),
            )
            conn.commit()
            continue

        conn.execute(
            "INSERT OR REPLACE INTO files "
            "(path, size, duration, width, height, vbitrate, fp, fp_dur, bucket, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (spath, info.size, info.duration, info.width, info.height,
             info.vbitrate, fp_str, fp_dur, bk, time.time()),
        )
        conn.commit()


def cluster_pass(conn: sqlite3.Connection, threshold: float) -> List[List[dict]]:
    """
    Within each bucket, do pairwise BER comparison and union-find clusters.
    Returns a list of clusters; each cluster is a list of file dicts.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT path, size, duration, width, height, vbitrate, fp, bucket "
        "FROM files WHERE fp IS NOT NULL"
    )
    rows = cur.fetchall()
    by_bucket: dict[bytes, List[Tuple[int, dict, List[int]]]] = defaultdict(list)
    for idx, row in enumerate(rows):
        path, size, dur, w, h, br, fp_str, bucket = row
        try:
            ints = decode_fp(fp_str)
        except Exception:
            continue
        rec = {
            "path": path, "size": size, "duration": dur,
            "width": w, "height": h, "vbitrate": br,
        }
        by_bucket[bucket].append((idx, rec, ints))

    parent = list(range(len(rows)))
    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for bucket, items in by_bucket.items():
        # Pairwise within bucket. Buckets should stay small in practice.
        for i in range(len(items)):
            idx_i, _, ints_i = items[i]
            for j in range(i + 1, len(items)):
                idx_j, _, ints_j = items[j]
                if ber(ints_i, ints_j) <= threshold:
                    union(idx_i, idx_j)

    groups: dict[int, List[dict]] = defaultdict(list)
    for idx, row in enumerate(rows):
        path, size, dur, w, h, br, _fp, _bk = row
        groups[find(idx)].append({
            "path": path, "size": size, "duration": dur,
            "width": w, "height": h, "vbitrate": br,
        })

    # Only return clusters with >1 file.
    return [g for g in groups.values() if len(g) > 1]


def rank(rec: dict) -> Tuple[int, int, int]:
    """Higher is better: prefer higher resolution, then bitrate, then size."""
    return (rec.get("height", 0) or 0,
            rec.get("vbitrate", 0) or 0,
            rec.get("size", 0) or 0)


def write_report(clusters: List[List[dict]], out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["cluster", "rank", "keep", "path", "width", "height",
                    "vbitrate", "size", "duration"])
        for ci, group in enumerate(clusters, 1):
            ranked = sorted(group, key=rank, reverse=True)
            for ri, rec in enumerate(ranked):
                w.writerow([
                    ci, ri, "KEEP" if ri == 0 else "dup",
                    rec["path"],
                    rec.get("width", 0), rec.get("height", 0),
                    rec.get("vbitrate", 0), rec.get("size", 0),
                    f"{rec.get('duration', 0):.1f}",
                ])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path, help="Directory to scan")
    ap.add_argument("--db", type=Path, default=Path("dedupe.sqlite"),
                    help="SQLite database for state (resumable)")
    ap.add_argument("--report", type=Path, default=Path("dedupe_report.csv"),
                    help="Output CSV report path")
    ap.add_argument("--threshold", type=float, default=0.05,
                    help="Max bit-error rate for a duplicate match (0.0-1.0)")
    ap.add_argument("--length", type=float, default=120.0,
                    help="Seconds of audio to fingerprint")
    ap.add_argument("--offset-pct", type=float, default=0.10,
                    help="Skip this fraction of the file before sampling")
    ap.add_argument("--skip-fingerprint", action="store_true",
                    help="Only re-run clustering against existing DB")
    args = ap.parse_args()

    if not args.root.is_dir():
        sys.exit(f"Not a directory: {args.root}")

    ffmpeg = common.require_tool("ffmpeg")
    ffprobe = common.require_tool("ffprobe")
    fpcalc = common.require_tool("fpcalc")

    conn = opendb(args.db)

    if not args.skip_fingerprint:
        fingerprint_pass(
            args.root, conn,
            offset_pct=args.offset_pct,
            length=args.length,
            ffmpeg=ffmpeg, fpcalc=fpcalc, ffprobe=ffprobe,
        )

    clusters = cluster_pass(conn, threshold=args.threshold)
    write_report(clusters, args.report)
    n_dups = sum(len(g) - 1 for g in clusters)
    print(f"Found {len(clusters)} duplicate clusters covering "
          f"{n_dups} redundant files. Report: {args.report}")


if __name__ == "__main__":
    main()
