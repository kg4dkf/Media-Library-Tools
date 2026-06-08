#!/usr/bin/env python3
"""
dedupe_check.py — One-time utility to verify that every video in a
"suspect" folder (e.g. the TMM trash, an old backup) has an exact-byte
duplicate somewhere in your main library, so you can safely delete the
suspect copy.

Strategy: size-pre-filter then BLAKE3 hash.

    1. Walk the suspect folder; record each video's full path and size.
    2. Walk the main library; for every video whose size MATCHES any
       suspect file, hash it.
    3. Hash every suspect file.
    4. For each suspect file, look for any main-library file with the
       same hash. If found, the suspect is a true byte-identical
       duplicate (safe to delete). Otherwise it's unique.

Files whose size matches nothing in the suspect folder are never
hashed — that's the speed win on large libraries.

Usage:
    py dedupe_check.py --config config.json \
        --suspect-dir "G:/_tmm_trash" \
        --library-dir "G:/TV"

Outputs `dedupe_check.csv` in the suspect folder (or wherever you
specify with --out).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    VIDEO_EXTS,
    _norm_path,
    hash_file,
    long,
)


VERSION = "1.0.0"
log = logging.getLogger("dedupe_check")


def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)


def walk_videos(root: Path) -> List[Tuple[Path, int]]:
    """Return [(path, size)] for every video file under root."""
    out: List[Tuple[Path, int]] = []
    if not root.exists():
        log.error("Path does not exist: %s", root)
        return out
    for dirpath, dirs, files in os.walk(str(root)):
        for fn in files:
            p = _norm_path(os.path.join(dirpath, fn))
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                sz = os.path.getsize(long(p))
            except OSError:
                continue
            out.append((p, sz))
    return out


def hash_with_progress(files: List[Path], algo: str = "blake3") -> Dict[str, str]:
    """Hash each file. Return {path_str: hash_hex}."""
    out: Dict[str, str] = {}
    n = len(files)
    if not n:
        return out
    t0 = time.time()
    last_log = t0
    for i, f in enumerate(files, 1):
        try:
            h = hash_file(f, algo)
        except OSError as e:
            log.warning("hash failed: %s: %s", f, e)
            continue
        out[str(f)] = h
        now = time.time()
        if now - last_log > 5:
            elapsed = now - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (n - i) / rate if rate else 0
            log.info("  hashed %d/%d (%.1f files/s, ETA %.0fs)", i, n, rate, eta)
            last_log = now
    log.info("  hashed %d files in %.1fs", len(out), time.time() - t0)
    return out


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dedupe_check.py",
        description="Verify suspect-folder videos are duplicates of main-library videos.",
    )
    p.add_argument("--config", default="config.json",
                   help="path to config.json (used only for hash_algo)")
    p.add_argument("--suspect-dir", required=True,
                   help="directory whose contents you want to verify (e.g. G:/_tmm_trash)")
    p.add_argument("--library-dir", required=True,
                   help="main library to compare against (e.g. G:/TV or H:/Movies)")
    p.add_argument("--out", default="",
                   help="output CSV path (default: <suspect-dir>/dedupe_check.csv)")
    p.add_argument("--algo", default="blake3",
                   help="hash algorithm (default: blake3, falls back to sha256)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging()
    log.info("dedupe_check.py v%s", VERSION)

    suspect = _norm_path(args.suspect_dir)
    library = _norm_path(args.library_dir)
    out_path = _norm_path(args.out) if args.out else (suspect / "dedupe_check.csv")

    if not suspect.exists():
        log.error("Suspect dir does not exist: %s", suspect)
        return 4
    if not library.exists():
        log.error("Library dir does not exist: %s", library)
        return 4

    log.info("Walking suspect: %s", suspect)
    suspect_files = walk_videos(suspect)
    log.info("Found %d video files in suspect", len(suspect_files))
    if not suspect_files:
        log.info("Nothing to check.")
        return 0

    log.info("Walking library: %s", library)
    library_files = walk_videos(library)
    log.info("Found %d video files in library", len(library_files))

    # Build a size index of suspect files for fast prefiltering
    suspect_sizes: Dict[int, List[Path]] = defaultdict(list)
    for p, sz in suspect_files:
        suspect_sizes[sz].append(p)

    # From the library, pick only files whose size matches a suspect file
    library_to_hash: List[Path] = []
    for p, sz in library_files:
        if sz in suspect_sizes:
            library_to_hash.append(p)
    log.info("Library files with size match (will hash): %d / %d "
             "(%.1f%% reduction by size pre-filter)",
             len(library_to_hash), len(library_files),
             100 * (1 - len(library_to_hash) / max(1, len(library_files))))

    # Hash both sets
    log.info("Hashing %d suspect files ...", len(suspect_files))
    suspect_hashes = hash_with_progress([p for p, _ in suspect_files], args.algo)

    log.info("Hashing %d library files (size-matched only) ...", len(library_to_hash))
    library_hashes = hash_with_progress(library_to_hash, args.algo)

    # Build hash -> [library paths] index
    library_by_hash: Dict[str, List[str]] = defaultdict(list)
    for path_str, h in library_hashes.items():
        library_by_hash[h].append(path_str)

    # Cross-reference
    rows: List[Dict[str, str]] = []
    duplicate_count = 0
    unique_count = 0
    hash_failed_count = 0
    for p, sz in suspect_files:
        path_str = str(p)
        h = suspect_hashes.get(path_str, "")
        if not h:
            rows.append({
                "suspect_path": path_str, "size_bytes": str(sz),
                "hash": "", "status": "hash_failed",
                "library_matches": "",
            })
            hash_failed_count += 1
            continue
        matches = library_by_hash.get(h, [])
        if matches:
            rows.append({
                "suspect_path": path_str, "size_bytes": str(sz),
                "hash": h, "status": "duplicate",
                "library_matches": " | ".join(matches),
            })
            duplicate_count += 1
        else:
            rows.append({
                "suspect_path": path_str, "size_bytes": str(sz),
                "hash": h, "status": "unique",
                "library_matches": "",
            })
            unique_count += 1

    # Sort: unique first (these need attention), then duplicates
    rows.sort(key=lambda r: (r["status"] != "unique", r["suspect_path"]))

    headers = ["status", "size_bytes", "suspect_path", "library_matches", "hash"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(out_path, "w", encoding="utf-8", newline="")
    except PermissionError:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = out_path.with_name(f"{out_path.stem}_{ts}{out_path.suffix}")
        log.warning("Could not write original out_path; writing to %s", out_path)
        f = open(out_path, "w", encoding="utf-8", newline="")
    with f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in headers})

    log.info("")
    log.info("=" * 72)
    log.info("Summary:")
    log.info("  Suspect files total:         %d", len(suspect_files))
    log.info("  Duplicate (safe to delete):  %d", duplicate_count)
    log.info("  Unique (REVIEW before delete): %d", unique_count)
    if hash_failed_count:
        log.info("  Hash failed:                 %d", hash_failed_count)
    log.info("  Output: %s", out_path)
    log.info("=" * 72)

    if unique_count == 0:
        log.info("All %d suspect files have duplicates in the library. "
                 "The suspect folder can be deleted safely.", duplicate_count)
    else:
        log.info("%d file(s) in the suspect folder are NOT duplicated elsewhere. "
                 "Review them in the CSV before deleting.", unique_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
