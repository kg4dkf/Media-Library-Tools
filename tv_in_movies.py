#!/usr/bin/env python3
"""
tv_in_movies.py — Find TV episodes / series content masquerading as
movies in H:\\Movies. Combines two signals:

    1. **Filename pattern** — anything with SxxExx, 1x01, "Season N",
       "Episode N", "Disc N", "T00", etc. is highly likely TV.
    2. **Runtime** — if you've already run runtime_scan.py, episodes
       under 50 minutes are likely TV. (Pass --runtime-csv to use.)
    3. **TMDB type** — for any candidate, optionally check TMDB whether
       the IMDB id is actually classified as a TV series (slow).

Output: tv_in_movies.csv, sorted by confidence that it's really TV.

Usage:
    py tv_in_movies.py --config config.json
    py tv_in_movies.py --config config.json --runtime-csv H:\\_mediaprep\\runtime_scan.csv
    py tv_in_movies.py --config config.json --check-tmdb
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    _norm_path,
    load_config,
)


VERSION = "1.0.0"
log = logging.getLogger("tv_in_movies")
TMDB_BASE = "https://api.themoviedb.org/3"

FOLDER_RX = re.compile(
    r"^(?P<title>.+?)\s*(?:\((?P<year>\d{4})\))?\s*\{(?:imdb-(?P<imdb>tt\d{7,10})|tmdb-(?P<tmdb>\d+))\}\s*$"
)

# Strong TV indicators in filenames
STRONG_PATTERNS = [
    (re.compile(r"S\d{1,2}E\d{1,3}", re.I),         "SxxExx marker"),
    (re.compile(r"\b\d{1,2}x\d{1,3}\b"),            "NxNN format"),
    (re.compile(r"Season[\s_\.]+\d+", re.I),        "'Season N'"),
    (re.compile(r"Episode[\s_\.]+\d+", re.I),       "'Episode N'"),
    (re.compile(r"\bSeries[\s_\.]+\d+", re.I),      "'Series N' (UK)"),
    (re.compile(r"\bEp[\s_\.]+\d+", re.I),          "'Ep N'"),
    (re.compile(r"Disc\s*\d+\s+of\s+\d+", re.I),    "'Disc N of N'"),
    (re.compile(r"\b\d+\s+of\s+\d+\b"),             "'N of N'"),
]

# Weaker patterns that suggest TV special/short
WEAK_PATTERNS = [
    (re.compile(r"Charlie Brown", re.I),            "Charlie Brown special"),
    (re.compile(r"Anniversary Special", re.I),      "anniversary special"),
    (re.compile(r"Christmas Special", re.I),        "Christmas special"),
    (re.compile(r"Holiday Special", re.I),          "holiday special"),
    (re.compile(r"\bMiniseries\b", re.I),           "miniseries"),
    (re.compile(r"\b\d+\s+Episodes?\b", re.I),      "'N Episodes' suffix"),
]


def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)


@dataclass
class Suspect:
    folder: str
    title: str
    imdb_id: str
    tmdb_id: str
    score: int = 0           # higher = more confident it's TV
    reasons: List[str] = field(default_factory=list)
    runtime_min: float = 0.0
    runtime_class: str = ""  # from runtime_scan.csv if available
    tmdb_says_tv: bool = False


def walk_library(root: Path) -> List[Suspect]:
    out: List[Suspect] = []
    for dirpath, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs
                   if d not in ("_mediaprep", "duplicates", ".tmm", ".sonarr")]
        bn = os.path.basename(dirpath)
        m = FOLDER_RX.match(bn)
        if not m:
            # No imdb/tmdb tag — note it as a candidate but lower score
            # (folders without tags are often weird content)
            out.append(Suspect(folder=dirpath, title=bn, imdb_id="", tmdb_id=""))
            continue
        out.append(Suspect(folder=dirpath,
                           title=m.group("title").strip(),
                           imdb_id=m.group("imdb") or "",
                           tmdb_id=m.group("tmdb") or ""))
    return out


def score_filename(s: Suspect, also_check_files: bool = True) -> None:
    """Add score and reasons based on folder name and (optionally) the
    filenames of videos inside."""
    candidates = [s.title]
    if also_check_files:
        try:
            for entry in os.scandir(s.folder):
                if entry.is_file() and entry.name.lower().endswith(
                        ('.mp4', '.mkv', '.avi', '.mov', '.m4v', '.wmv', '.mpg')):
                    candidates.append(entry.name)
        except OSError:
            pass

    for cand in candidates:
        for rx, label in STRONG_PATTERNS:
            if rx.search(cand):
                s.score += 30
                s.reasons.append(f"{label} in '{cand[:50]}'")
                break  # one strong hit per candidate is enough
        for rx, label in WEAK_PATTERNS:
            if rx.search(cand):
                s.score += 8
                s.reasons.append(f"{label} in '{cand[:50]}'")


def load_runtime_csv(path: Path) -> Dict[str, Dict[str, str]]:
    """Load runtime_scan.csv keyed by lowercase folder name basename."""
    out: Dict[str, Dict[str, str]] = {}
    if not path.exists():
        return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tf = r.get("target_folder") or ""
            bn = os.path.basename(tf).lower()
            if bn:
                out[bn] = r
    return out


def apply_runtime(s: Suspect, by_folder: Dict[str, Dict[str, str]]) -> None:
    key = os.path.basename(s.folder).lower()
    row = by_folder.get(key)
    if not row:
        return
    s.runtime_class = row.get("classification", "")
    try:
        s.runtime_min = float(row.get("duration_min", "0") or 0)
    except (ValueError, TypeError):
        s.runtime_min = 0.0
    if s.runtime_class in ("tv_short", "tv_episode"):
        s.score += 25
        s.reasons.append(f"runtime {s.runtime_min:.1f}m → {s.runtime_class}")
    elif s.runtime_class == "tv_special":
        s.score += 5  # weaker signal — could be legit
        s.reasons.append(f"runtime {s.runtime_min:.1f}m → {s.runtime_class}")


def check_tmdb_tv(s: Suspect, api_key: str) -> None:
    """Optional: query TMDB to see if the IMDB id resolves to TV instead of movie."""
    try:
        import requests
    except ImportError:
        return
    if not s.imdb_id:
        return
    try:
        r = requests.get(f"{TMDB_BASE}/find/{s.imdb_id}",
                         params={"api_key": api_key, "external_source": "imdb_id"},
                         timeout=15)
        if r.status_code != 200:
            return
        data = r.json()
        has_tv = bool(data.get("tv_results") or data.get("tv_episode_results"))
        has_movie = bool(data.get("movie_results"))
        if has_tv and not has_movie:
            s.score += 40
            s.reasons.append("TMDB classifies as TV-only")
            s.tmdb_says_tv = True
        elif has_tv and has_movie:
            s.reasons.append("TMDB has both movie and TV records")
    except Exception:
        pass


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="tv_in_movies.py",
        description="Find TV content masquerading as movies in H:\\Movies.",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--runtime-csv", default="",
                   help="path to runtime_scan.csv to use duration as a signal")
    p.add_argument("--check-tmdb", action="store_true",
                   help="query TMDB to verify IMDB id classifies as TV (slow)")
    p.add_argument("--min-score", type=int, default=20,
                   help="suspicion score threshold for inclusion (default: 20)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging()
    try:
        cfg = load_config(_norm_path(args.config))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr); return 3

    log.info("Walking %s ...", cfg.movies_root)
    suspects = walk_library(cfg.movies_root)
    log.info("Inspecting %d entries", len(suspects))

    for s in suspects:
        score_filename(s)

    if args.runtime_csv:
        rt_path = _norm_path(args.runtime_csv)
        rt_data = load_runtime_csv(rt_path)
        log.info("Loaded runtime data for %d folders", len(rt_data))
        for s in suspects:
            apply_runtime(s, rt_data)

    if args.check_tmdb:
        if not cfg.tmdb_api_key or cfg.tmdb_api_key == "REPLACE_ME":
            log.error("--check-tmdb requires a valid tmdb_api_key in config.json")
            return 4
        log.info("Cross-checking IMDB ids against TMDB classification ...")
        # Only check ones that already scored above threshold to save calls
        candidates = [s for s in suspects if s.score >= 10 and s.imdb_id]
        for i, s in enumerate(candidates, 1):
            if i % 25 == 0:
                log.info("  TMDB-checked %d/%d", i, len(candidates))
            check_tmdb_tv(s, cfg.tmdb_api_key)

    flagged = [s for s in suspects if s.score >= args.min_score]
    flagged.sort(key=lambda s: -s.score)
    log.info("Found %d suspected TV-in-movies entries (score >= %d)",
             len(flagged), args.min_score)

    # Output CSV
    out_path = cfg.mediaprep_dir / "tv_in_movies.csv"
    headers = ["score", "title", "runtime_min", "runtime_class", "tmdb_says_tv",
               "imdb_id", "tmdb_id", "folder", "reasons"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for s in flagged:
            w.writerow({
                "score": s.score, "title": s.title,
                "runtime_min": f"{s.runtime_min:.1f}" if s.runtime_min else "",
                "runtime_class": s.runtime_class,
                "tmdb_says_tv": "yes" if s.tmdb_says_tv else "",
                "imdb_id": s.imdb_id, "tmdb_id": s.tmdb_id,
                "folder": s.folder, "reasons": " | ".join(s.reasons),
            })

    # Console preview
    log.info("Top 20 highest-confidence TV-in-movies:")
    for s in flagged[:20]:
        log.info("  score=%-3d  %s — %s", s.score, s.title,
                 ", ".join(s.reasons[:2])[:70])

    log.info("")
    log.info("Wrote: %s", out_path)
    log.info("Action: review the top-scoring rows in Excel. Anything you confirm")
    log.info("is TV gets moved to G:\\TV-staging\\ (or wherever you stage TV) for")
    log.info("the eventual phase 6 TV pipeline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
