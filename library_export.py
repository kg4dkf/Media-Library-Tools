#!/usr/bin/env python3
"""
library_export.py — Walk H:\\Movies and emit a phone-friendly CSV with
one row per movie. Useful for keeping a "what I own" list on your phone
when browsing library sales / used DVD bins.

Reads movies_root from config.json. Walks the library, finds every
folder with {imdb-tt#######} or {tmdb-#####} in the name, extracts
title and year, and writes a sorted CSV.

Optionally calls TMDB to enrich rows with director, runtime, and genres
(use --enrich; slower, requires API key).

Usage:
    py library_export.py --config config.json
    py library_export.py --config config.json --enrich
    py library_export.py --config config.json --out my_movies.csv
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
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    _norm_path,
    load_config,
    long,
)

try:
    import requests  # type: ignore
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


VERSION = "1.0.0"
log = logging.getLogger("library_export")
TMDB_BASE = "https://api.themoviedb.org/3"

# Folder name pattern: "<Title> (<Year>) {imdb-tt#######}"
FOLDER_RX = re.compile(
    r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*\{(?:imdb-(?P<imdb>tt\d{7,10})|tmdb-(?P<tmdb>\d+))\}\s*$"
)


@dataclass
class MovieRow:
    title: str
    year: str
    imdb: str
    tmdb: str
    folder: str
    # Optional enriched fields
    runtime_min: str = ""
    director: str = ""
    genres: str = ""
    rating: str = ""


def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)


def walk_library(root: Path) -> List[MovieRow]:
    rows: List[MovieRow] = []
    if not root.exists():
        return rows
    for dirpath, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs
                   if d not in ("_mediaprep", "duplicates", ".tmm", ".sonarr")]
        bn = os.path.basename(dirpath)
        m = FOLDER_RX.match(bn)
        if not m:
            continue
        rows.append(MovieRow(
            title=m.group("title").strip(),
            year=m.group("year"),
            imdb=m.group("imdb") or "",
            tmdb=m.group("tmdb") or "",
            folder=dirpath,
        ))
    return rows


def enrich_from_tmdb(rows: List[MovieRow], api_key: str, language: str,
                     mediaprep: Path) -> None:
    """Use the TMDB cache built by execute.py to fill runtime/director/genres.

    For folders tagged with {imdb-tt...} but no {tmdb-N}, the function first
    resolves the IMDB id to a TMDB id via /find/{imdb_id}, caching the
    mapping in an imdb_to_tmdb table so subsequent runs don't re-query.
    """
    import sqlite3
    cache_path = mediaprep / "tmdb_cache.sqlite"
    if not cache_path.exists():
        log.warning("TMDB cache not found at %s; enrichment limited", cache_path)
        db = None
    else:
        db = sqlite3.connect(str(cache_path))
        # Cache table for IMDB → TMDB resolution. Created on demand so an
        # older cache file works without migration.
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS imdb_to_tmdb (
                imdb_id TEXT PRIMARY KEY,
                tmdb_id TEXT,
                fetched_at TEXT)""")
            db.commit()
        except sqlite3.Error as e:
            log.warning("could not create imdb_to_tmdb cache: %s", e)

    sess = requests.Session() if _HAS_REQUESTS else None

    def _from_cache(tmdb_id: str) -> Optional[Dict[str, Any]]:
        if not db:
            return None
        try:
            row = db.execute(
                "SELECT json FROM rich_movie WHERE tmdb_id=? AND language=?",
                (int(tmdb_id), language)
            ).fetchone()
            if row:
                return json.loads(row[0])
        except (ValueError, sqlite3.Error, json.JSONDecodeError):
            pass
        return None

    def _from_api(tmdb_id: str) -> Optional[Dict[str, Any]]:
        if not sess:
            return None
        try:
            r = sess.get(f"{TMDB_BASE}/movie/{tmdb_id}",
                         params={"api_key": api_key, "language": language,
                                 "append_to_response": "credits"},
                         timeout=20)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                r = sess.get(f"{TMDB_BASE}/movie/{tmdb_id}",
                             params={"api_key": api_key, "language": language,
                                     "append_to_response": "credits"},
                             timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning("TMDB enrich failed for %s: %s", tmdb_id, e)
            return None

    def _resolve_imdb(imdb_id: str) -> str:
        """Return the TMDB id for an IMDB id. Cache hit → instant; miss →
        one /find/ API call. Negative results are cached too so we don't
        re-hit dead IMDB ids."""
        if not imdb_id or not db:
            return ""
        try:
            row = db.execute(
                "SELECT tmdb_id FROM imdb_to_tmdb WHERE imdb_id=?",
                (imdb_id,)
            ).fetchone()
            if row is not None:
                return row[0] or ""   # may be "" for known-empty results
        except sqlite3.Error:
            pass
        if not sess:
            return ""
        try:
            r = sess.get(f"{TMDB_BASE}/find/{imdb_id}",
                         params={"api_key": api_key,
                                 "external_source": "imdb_id"},
                         timeout=20)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")))
                r = sess.get(f"{TMDB_BASE}/find/{imdb_id}",
                             params={"api_key": api_key,
                                     "external_source": "imdb_id"},
                             timeout=20)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            log.warning("TMDB find failed for %s: %s", imdb_id, e)
            return ""
        movie_results = data.get("movie_results") or []
        tmdb_id = str(movie_results[0].get("id") or "") if movie_results else ""
        try:
            db.execute(
                "INSERT OR REPLACE INTO imdb_to_tmdb "
                "(imdb_id, tmdb_id, fetched_at) VALUES (?, ?, ?)",
                (imdb_id, tmdb_id, "")
            )
            db.commit()
        except sqlite3.Error:
            pass
        return tmdb_id

    # First-pass: resolve missing TMDB ids from IMDB so the enrichment
    # loop below has something to look up. This is the slow step on first
    # run for an IMDB-tagged library; subsequent runs hit the cache.
    need_resolve = [r for r in rows if not r.tmdb and r.imdb]
    if need_resolve:
        log.info("Resolving %d IMDB → TMDB mappings ...", len(need_resolve))
        resolved = 0
        for i, row in enumerate(need_resolve, 1):
            tid = _resolve_imdb(row.imdb)
            if tid:
                row.tmdb = tid
                resolved += 1
            if i % 100 == 0 or i == len(need_resolve):
                log.info("  resolved %d/%d (%d found)", i, len(need_resolve),
                         resolved)

    for i, row in enumerate(rows, 1):
        if not row.tmdb:
            continue
        data = _from_cache(row.tmdb) or _from_api(row.tmdb)
        if not data:
            continue
        rt = data.get("runtime")
        if rt:
            row.runtime_min = str(rt)
        genres = data.get("genres") or []
        if genres:
            row.genres = ", ".join(g.get("name", "") for g in genres)
        # Director from credits.crew
        credits = data.get("credits") or {}
        for crew in (credits.get("crew") or []):
            if crew.get("job") == "Director":
                row.director = crew.get("name", "")
                break
        # Rating (US)
        rd = data.get("release_dates") or {}
        for entry in rd.get("results", []):
            if entry.get("iso_3166_1") == "US":
                for d in entry.get("release_dates", []):
                    cert = (d.get("certification") or "").strip()
                    if cert:
                        row.rating = cert
                        break
                break
        if i % 100 == 0 or i == len(rows):
            log.info("  enriched %d/%d", i, len(rows))

    if db:
        db.close()


def write_csv(path: Path, rows: List[MovieRow], enriched: bool) -> None:
    if enriched:
        headers = ["title", "year", "rating", "runtime_min", "director",
                   "genres", "imdb", "tmdb", "folder"]
    else:
        headers = ["title", "year", "imdb", "tmdb", "folder"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({
                "title": r.title, "year": r.year, "imdb": r.imdb,
                "tmdb": r.tmdb, "folder": r.folder,
                "rating": r.rating, "runtime_min": r.runtime_min,
                "director": r.director, "genres": r.genres,
            })


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="library_export.py",
        description="Export a phone-friendly CSV of every movie in your library.",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--enrich", action="store_true",
                   help="add runtime/director/genres/rating columns via TMDB (slower)")
    p.add_argument("--out", default="",
                   help="output path (default: <mediaprep>/my_movies.csv)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging()
    try:
        cfg = load_config(_norm_path(args.config))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr); return 3

    log.info("library_export.py v%s — walking %s", VERSION, cfg.movies_root)
    rows = walk_library(cfg.movies_root)
    rows.sort(key=lambda r: (r.title.lower().lstrip("the ").lstrip("a "), r.year))
    log.info("Found %d movies", len(rows))

    if args.enrich:
        log.info("Enriching with TMDB data ...")
        enrich_from_tmdb(rows, cfg.tmdb_api_key, cfg.language, cfg.mediaprep_dir)

    out_path = _norm_path(args.out) if args.out else (cfg.mediaprep_dir / "my_movies.csv")
    write_csv(out_path, rows, args.enrich)
    log.info("Wrote %d rows to %s", len(rows), out_path)
    log.info("(Sort/filter in Excel — phone-friendly CSV)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
