#!/usr/bin/env python3
"""
missing_pieces.py — Find franchise/collection movies you DON'T have.

For every movie in H:\\Movies, look up its TMDB collection. For each
distinct collection that has at least one member in your library,
fetch the full member list and report what's missing.

Output: missing_pieces.csv, sorted by completeness — collections you
have most of (e.g. "I have 4 of 5 James Bonds") near the top, near-misses
that are worth a trip to the bin.

Usage:
    py missing_pieces.py --config config.json
    py missing_pieces.py --config config.json --only-incomplete
    py missing_pieces.py --config config.json --min-have 2
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    _norm_path,
    load_config,
)

try:
    import requests  # type: ignore
except ImportError:
    print("ERROR: requests not installed", file=sys.stderr); sys.exit(3)


VERSION = "1.0.0"
log = logging.getLogger("missing_pieces")
TMDB_BASE = "https://api.themoviedb.org/3"

FOLDER_RX = re.compile(
    r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)\s*\{(?:imdb-(?P<imdb>tt\d{7,10})|tmdb-(?P<tmdb>\d+))\}\s*$"
)


def setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)


@dataclass
class OwnedMovie:
    tmdb_id: str
    imdb_id: str
    title: str
    year: str
    folder: str


def walk_owned(root: Path) -> List[OwnedMovie]:
    owned: List[OwnedMovie] = []
    if not root.exists():
        return owned
    for dirpath, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs
                   if d not in ("_mediaprep", "duplicates", ".tmm", ".sonarr")]
        bn = os.path.basename(dirpath)
        m = FOLDER_RX.match(bn)
        if not m:
            continue
        owned.append(OwnedMovie(
            tmdb_id=m.group("tmdb") or "",
            imdb_id=m.group("imdb") or "",
            title=m.group("title").strip(),
            year=m.group("year"),
            folder=dirpath,
        ))
    return owned


class TmdbClient:
    def __init__(self, api_key: str, mediaprep: Path, language: str = "en-US"):
        self.api_key = api_key
        self.lang = language
        self._session = requests.Session()
        # Reuse the rich_movie cache that execute.py built. Add a
        # collection cache table.
        self.cache_path = mediaprep / "tmdb_cache.sqlite"
        self._db = sqlite3.connect(str(self.cache_path))
        self._db.execute("""CREATE TABLE IF NOT EXISTS rich_movie (
            tmdb_id INTEGER PRIMARY KEY, language TEXT, json TEXT, fetched_at TEXT)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS collection_cache (
            collection_id INTEGER PRIMARY KEY, language TEXT, json TEXT, fetched_at TEXT)""")
        self._db.commit()

    def _get(self, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for attempt in range(3):
            try:
                r = self._session.get(url, params=params, timeout=20)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", "5")))
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                log.warning("TMDB attempt %d: %s", attempt+1, e)
                time.sleep(2 ** attempt)
        return None

    def get_movie(self, tmdb_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            "SELECT json FROM rich_movie WHERE tmdb_id=? AND language=?",
            (int(tmdb_id), self.lang)
        ).fetchone() if tmdb_id.isdigit() else None
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass
        data = self._get(f"{TMDB_BASE}/movie/{tmdb_id}",
                         {"api_key": self.api_key, "language": self.lang,
                          "append_to_response": "external_ids"})
        if data:
            self._db.execute(
                "INSERT OR REPLACE INTO rich_movie (tmdb_id, language, json, fetched_at) VALUES (?,?,?,?)",
                (data.get("id"), self.lang, json.dumps(data, ensure_ascii=False), "")
            )
            self._db.commit()
        return data

    def get_collection(self, collection_id: int) -> Optional[Dict[str, Any]]:
        row = self._db.execute(
            "SELECT json FROM collection_cache WHERE collection_id=? AND language=?",
            (collection_id, self.lang)
        ).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                pass
        data = self._get(f"{TMDB_BASE}/collection/{collection_id}",
                         {"api_key": self.api_key, "language": self.lang})
        if data:
            self._db.execute(
                "INSERT OR REPLACE INTO collection_cache (collection_id, language, json, fetched_at) VALUES (?,?,?,?)",
                (collection_id, self.lang, json.dumps(data, ensure_ascii=False), "")
            )
            self._db.commit()
        return data

    def find_by_imdb(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        data = self._get(f"{TMDB_BASE}/find/{imdb_id}",
                         {"api_key": self.api_key, "external_source": "imdb_id"})
        if not data: return None
        for r in data.get("movie_results", []):
            return r
        return None

    def close(self) -> None:
        try: self._db.close()
        except Exception: pass


@dataclass
class CollectionState:
    collection_id: int
    name: str
    owned: List[Tuple[str, str, str]] = field(default_factory=list)  # (title, year, tmdb_id)
    all_members: List[Tuple[str, str, str]] = field(default_factory=list)


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="missing_pieces.py",
        description="Find franchise movies you don't have.",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--only-incomplete", action="store_true",
                   help="omit collections you have 100% of")
    p.add_argument("--min-have", type=int, default=1,
                   help="only report collections where you own at least N (default: 1)")
    p.add_argument("--include-future", action="store_true",
                   help="include unreleased movies in TMDB collections (default: skip)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    setup_logging()
    try:
        cfg = load_config(_norm_path(args.config))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr); return 3
    if not cfg.tmdb_api_key or cfg.tmdb_api_key == "REPLACE_ME":
        print("TMDB API key missing", file=sys.stderr); return 4

    log.info("missing_pieces.py v%s — walking %s", VERSION, cfg.movies_root)
    owned = walk_owned(cfg.movies_root)
    log.info("Found %d movies in library", len(owned))

    client = TmdbClient(cfg.tmdb_api_key, cfg.mediaprep_dir, cfg.language)

    # Step 1: resolve TMDB ids for movies where we only have IMDB
    log.info("Resolving TMDB ids for owned movies (cache first) ...")
    for m in owned:
        if not m.tmdb_id and m.imdb_id:
            match = client.find_by_imdb(m.imdb_id)
            if match:
                m.tmdb_id = str(match.get("id") or "")

    # Step 2: build collections map. For each owned movie, fetch its
    # TMDB record (cached) and read belongs_to_collection.
    log.info("Looking up collection membership ...")
    collections: Dict[int, CollectionState] = {}
    for i, m in enumerate(owned, 1):
        if not m.tmdb_id:
            continue
        if i % 100 == 0 or i == len(owned):
            log.info("  resolved %d/%d", i, len(owned))
        data = client.get_movie(m.tmdb_id)
        if not data:
            continue
        coll = data.get("belongs_to_collection")
        if not coll:
            continue
        cid = coll.get("id")
        if not cid:
            continue
        state = collections.get(cid)
        if state is None:
            state = CollectionState(collection_id=cid, name=coll.get("name", ""))
            collections[cid] = state
        state.owned.append((m.title, m.year, m.tmdb_id))

    log.info("Found %d collections with at least one owned member", len(collections))

    # Step 3: for each collection, fetch the full member list
    log.info("Fetching full collection member lists ...")
    for i, (cid, state) in enumerate(collections.items(), 1):
        if i % 25 == 0 or i == len(collections):
            log.info("  fetched %d/%d collections", i, len(collections))
        data = client.get_collection(cid)
        if not data:
            continue
        for p in data.get("parts") or []:
            tid = str(p.get("id") or "")
            title = p.get("title") or ""
            rel = (p.get("release_date") or "")
            year = rel[:4] if rel else ""
            # Skip unreleased unless requested
            if not year and not args.include_future:
                continue
            state.all_members.append((title, year, tid))

    # Step 4: compute missing
    rows = []
    for state in collections.values():
        owned_ids = {tid for _, _, tid in state.owned}
        missing = [(t, y, tid) for t, y, tid in state.all_members
                   if tid and tid not in owned_ids]
        if len(state.owned) < args.min_have:
            continue
        if args.only_incomplete and not missing:
            continue
        completeness = len(state.owned) / max(1, len(state.all_members))
        rows.append({
            "collection": state.name,
            "owned_count": len(state.owned),
            "total_count": len(state.all_members),
            "completeness_pct": f"{100*completeness:.0f}",
            "missing_count": len(missing),
            "missing_titles": " | ".join(f"{t} ({y})" for t, y, _ in missing),
            "owned_titles": " | ".join(f"{t} ({y})" for t, y, _ in sorted(state.owned, key=lambda x: x[1])),
        })

    # Sort by: most-complete-but-not-full first (most actionable),
    # then by total_count descending, then by name
    def sort_key(r):
        comp = int(r["completeness_pct"])
        # Put complete (100) AFTER incomplete (so user sees near-misses first)
        bucket = 0 if 0 < comp < 100 else 1
        return (bucket, -comp, -int(r["total_count"]), r["collection"].lower())
    rows.sort(key=sort_key)

    out_path = cfg.mediaprep_dir / "missing_pieces.csv"
    headers = ["collection", "owned_count", "total_count", "completeness_pct",
               "missing_count", "missing_titles", "owned_titles"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Summary to console
    incomplete = [r for r in rows if r["missing_count"] != "0" and int(r["missing_count"]) > 0]
    log.info("")
    log.info("=" * 60)
    log.info("Found %d collections (%d incomplete)", len(rows), len(incomplete))
    log.info("Top 15 collections you're MOST CLOSE to completing:")
    near_miss = [r for r in incomplete if int(r["completeness_pct"]) >= 50]
    near_miss.sort(key=lambda r: (-int(r["completeness_pct"]), int(r["missing_count"])))
    for r in near_miss[:15]:
        log.info("  %s%%  %s — own %s/%s, missing: %s",
                 r["completeness_pct"].rjust(3),
                 r["collection"], r["owned_count"], r["total_count"],
                 r["missing_titles"][:80])
    log.info("Wrote: %s", out_path)
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
