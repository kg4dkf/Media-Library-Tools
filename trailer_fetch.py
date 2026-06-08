#!/usr/bin/env python3
"""
trailer_fetch.py — Walk H:\\Movies and download official trailers from
YouTube (via TMDB's videos endpoint + yt-dlp) into each movie folder.

Plex's trailer convention: `<Movie Folder>\\<Movie> -trailer.mp4` (or in a
`Trailers/` subfolder). This script uses the inline naming.

Prerequisite: yt-dlp installed. `pip install yt-dlp`.

Usage:
    py trailer_fetch.py --config config.json
    py trailer_fetch.py --config config.json --dry-run
    py trailer_fetch.py --config config.json --quality 720
    py trailer_fetch.py --config config.json --imdb tt0078346
    py trailer_fetch.py --config config.json --in-subfolder   # Trailers/<name>.mp4
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    _norm_path,
    load_config,
    long,
)

try:
    import requests  # type: ignore
except ImportError:
    print("ERROR: requests not installed", file=sys.stderr); sys.exit(3)

try:
    import yt_dlp  # type: ignore
    _HAS_YTDLP = True
except ImportError:
    _HAS_YTDLP = False


VERSION = "1.0.0"
log = logging.getLogger("trailer_fetch")

TMDB_BASE = "https://api.themoviedb.org/3"
IMDB_RX = re.compile(r"\{imdb-(tt\d{7,10})\}", re.IGNORECASE)
TMDB_RX = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)


def setup_logging(mediaprep: Path, run_id: str) -> Path:
    mediaprep.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep / f"trailer_fetch_{run_id}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear(); log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)
    log.info("trailer_fetch.py v%s starting; log: %s", VERSION, log_path)
    return log_path


@dataclass
class MovieFolder:
    path: Path
    imdb_id: str = ""
    tmdb_id: str = ""
    title: str = ""


def walk_library(root: Path) -> List[MovieFolder]:
    out: List[MovieFolder] = []
    if not root.exists():
        return out
    for dirpath, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs
                   if d not in ("_mediaprep", "duplicates", ".tmm", ".sonarr")]
        bn = os.path.basename(dirpath)
        m_imdb = IMDB_RX.search(bn)
        m_tmdb = TMDB_RX.search(bn)
        if not (m_imdb or m_tmdb):
            continue
        mf = MovieFolder(path=_norm_path(dirpath),
                         imdb_id=m_imdb.group(1).lower() if m_imdb else "",
                         tmdb_id=m_tmdb.group(1) if m_tmdb else "",
                         title=bn)
        if not mf.tmdb_id:
            mf.tmdb_id = _read_nfo_tmdb_id(mf.path)
        out.append(mf)
    return out


def _read_nfo_tmdb_id(folder: Path) -> str:
    try:
        for entry in os.scandir(long(folder)):
            if entry.is_file() and entry.name.lower().endswith(".nfo"):
                try:
                    with open(long(_norm_path(entry.path)), "rb") as f:
                        data = f.read()
                    root = ET.fromstring(data)
                    e = root.find("tmdbid")
                    if e is not None and e.text:
                        return e.text.strip()
                except (OSError, ET.ParseError):
                    continue
    except OSError:
        pass
    return ""


class TmdbClient:
    def __init__(self, api_key: str, language: str = "en-US"):
        self.api_key = api_key
        self.lang = language
        self._session = requests.Session()

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
                log.warning("TMDB %s attempt %d: %s", url, attempt+1, e)
                time.sleep(2 ** attempt)
        return None

    def resolve_imdb_to_tmdb(self, imdb_id: str) -> Optional[str]:
        data = self._get(f"{TMDB_BASE}/find/{imdb_id}",
                         {"api_key": self.api_key, "external_source": "imdb_id"})
        if not data: return None
        for r in data.get("movie_results", []):
            return str(r.get("id"))
        return None

    def get_videos(self, tmdb_id: str) -> Optional[List[Dict[str, Any]]]:
        data = self._get(f"{TMDB_BASE}/movie/{tmdb_id}/videos",
                         {"api_key": self.api_key, "language": self.lang})
        if not data: return None
        return data.get("results") or []


def pick_trailer_candidates(videos: List[Dict[str, Any]]) -> List[str]:
    """Return YouTube ids in priority order: best trailer first, then
    teaser/clip/featurette fallbacks. Caller tries each until one works
    — TMDB often has stale ids for trailers YouTube has since removed."""
    if not videos:
        return []

    def score(v: Dict[str, Any]) -> tuple:
        kind = (v.get("type") or "").lower()
        site = (v.get("site") or "").lower()
        official = bool(v.get("official"))
        kind_rank = {"trailer": 0, "teaser": 1, "clip": 2, "featurette": 3}.get(kind, 4)
        site_rank = 0 if site == "youtube" else 1
        official_rank = 0 if official else 1
        return (kind_rank, site_rank, official_rank, -int(v.get("size") or 0))

    sorted_videos = sorted(videos, key=score)
    ids: List[str] = []
    for v in sorted_videos:
        if (v.get("site") or "").lower() != "youtube":
            continue
        yt_id = v.get("key")
        if yt_id and yt_id not in ids:
            ids.append(yt_id)
    return ids


def download_trailer(url_or_id: str, dst: Path, quality: int) -> bool:
    """url_or_id can be a YouTube video id, a full URL, or a yt-dlp
    search expression like 'ytsearch3:Movie (Year) trailer'."""
    if not _HAS_YTDLP:
        log.error("yt-dlp not installed. pip install yt-dlp")
        return False
    if url_or_id.startswith("http") or url_or_id.startswith("ytsearch"):
        target = url_or_id
    else:
        target = f"https://www.youtube.com/watch?v={url_or_id}"
    fmt = f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<={quality}][ext=mp4]/best[height<={quality}]"
    opts = {
        "outtmpl": str(dst.with_suffix(".%(ext)s")),
        "format": fmt,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_check_certificate": True,
        # For search results, take only the first match
        "playlist_items": "1",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([target])
    except Exception as e:
        log.warning("yt-dlp failed for %s: %s", target, e)
        return False
    # yt-dlp may have written .mp4 or some other ext; find the actual file
    candidates = list(dst.parent.glob(dst.stem + ".*"))
    candidates = [c for c in candidates if c.suffix.lower() in (".mp4", ".mkv", ".webm")]
    if not candidates:
        return False
    actual = candidates[0]
    if actual.suffix.lower() != ".mp4":
        # Rename to mp4 (Plex prefers it; even if container differs, the
        # extension hint is what Plex looks at)
        new = actual.with_suffix(".mp4")
        try:
            os.replace(str(actual), str(new))
        except OSError:
            pass
    return True


@dataclass
class TrailerResult:
    folder: str
    title: str
    action: str   # downloaded | skipped_exists | no_trailer | api_failed | ytdlp_failed
    yt_id: str = ""
    notes: str = ""


def process_folder(mf: MovieFolder, client: TmdbClient, quality: int,
                   replace: bool, dry_run: bool,
                   in_subfolder: bool,
                   youtube_search_fallback: bool = False,
                   movie_title_hint: str = "") -> TrailerResult:
    if not mf.tmdb_id and mf.imdb_id:
        mf.tmdb_id = client.resolve_imdb_to_tmdb(mf.imdb_id) or ""
    if not mf.tmdb_id:
        return TrailerResult(str(mf.path), mf.title, "api_failed",
                             notes="no TMDB id")

    # Where the trailer goes
    base = mf.path.name
    if in_subfolder:
        trailer_dir = mf.path / "Trailers"
        dst = trailer_dir / f"{base}-trailer.mp4"
    else:
        dst = mf.path / f"{base}-trailer.mp4"

    if dst.exists() and not replace:
        return TrailerResult(str(mf.path), mf.title, "skipped_exists",
                             notes=str(dst))

    videos = client.get_videos(mf.tmdb_id)
    if videos is None:
        return TrailerResult(str(mf.path), mf.title, "api_failed",
                             notes="videos endpoint failed")

    candidates = pick_trailer_candidates(videos)

    if dry_run:
        first = candidates[0] if candidates else "(search)"
        return TrailerResult(str(mf.path), mf.title, "downloaded",
                             yt_id=first,
                             notes=f"DRY-RUN {len(candidates)} TMDB candidates"
                                   + (" + search fallback" if youtube_search_fallback else ""))

    if in_subfolder:
        trailer_dir.mkdir(parents=True, exist_ok=True)

    # Try each TMDB candidate in turn.
    tried = 0
    for yt_id in candidates:
        tried += 1
        ok = download_trailer(yt_id, dst, quality)
        if ok:
            note = "" if tried == 1 else f"first {tried-1} candidate(s) unavailable; used #{tried}"
            return TrailerResult(str(mf.path), mf.title, "downloaded",
                                 yt_id=yt_id, notes=note)

    # All TMDB candidates exhausted (or TMDB had none). Fall back to a
    # YouTube search if opted in. This rescues both ytdlp_failed AND
    # no_trailer cases.
    if youtube_search_fallback and movie_title_hint:
        query = f"ytsearch3:{movie_title_hint} official trailer"
        log.info("  trying YouTube search: %s", movie_title_hint)
        ok = download_trailer(query, dst, quality)
        if ok:
            origin = ("TMDB had none" if not candidates
                      else f"TMDB had {len(candidates)} stale ids")
            return TrailerResult(str(mf.path), mf.title, "downloaded",
                                 yt_id="(search)",
                                 notes=f"{origin}; got via YouTube search for "
                                       f"'{movie_title_hint}'")

    # Nothing worked
    if not candidates:
        return TrailerResult(str(mf.path), mf.title, "no_trailer",
                             notes=f"TMDB returned no YouTube videos"
                                   + (" (search fallback also failed)" if youtube_search_fallback else ""))
    return TrailerResult(str(mf.path), mf.title, "ytdlp_failed",
                         yt_id=candidates[0],
                         notes=f"all {len(candidates)} YouTube candidates unavailable"
                               + (" (search fallback also failed)" if youtube_search_fallback else ""))


def write_results(path: Path, rows: List[TrailerResult]) -> None:
    headers = ["action", "title", "yt_id", "folder", "notes"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({
                "action": r.action, "title": r.title, "yt_id": r.yt_id,
                "folder": r.folder, "notes": r.notes,
            })


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="trailer_fetch.py",
        description="Download official trailers from YouTube via TMDB.",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quality", type=int, default=720,
                   help="max video height in pixels (480/720/1080/2160)")
    p.add_argument("--in-subfolder", action="store_true",
                   help="put trailer in <folder>/Trailers/ rather than inline")
    p.add_argument("--replace-existing", action="store_true")
    p.add_argument("--imdb", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--youtube-search-fallback", action="store_true",
                   help="when all TMDB-listed YouTube ids fail, search YouTube directly "
                        "for '<Title> (<Year>) official trailer' and try the first hit. "
                        "Catches another ~70% of TMDB-stale cases but occasionally grabs "
                        "fan content for obscure titles.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config(_norm_path(args.config))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr); return 3
    if not _HAS_YTDLP and not args.dry_run:
        print("yt-dlp is required for real runs. Install: pip install yt-dlp",
              file=sys.stderr); return 4
    if not cfg.tmdb_api_key or cfg.tmdb_api_key == "REPLACE_ME":
        print("TMDB API key missing", file=sys.stderr); return 4

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    setup_logging(cfg.mediaprep_dir, run_id)

    folders = walk_library(cfg.movies_root)
    log.info("Found %d movie folders", len(folders))
    if args.imdb:
        wanted = {x.strip().lower() for x in args.imdb.split(",") if x.strip()}
        folders = [f for f in folders if f.imdb_id.lower() in wanted]
    if args.limit:
        folders = folders[:args.limit]

    client = TmdbClient(cfg.tmdb_api_key, cfg.language)
    all_results: List[TrailerResult] = []
    t0 = time.time()
    for i, mf in enumerate(folders, 1):
        if i % 10 == 0 or i == len(folders):
            log.info("[%d/%d] processing ...", i, len(folders))
        # Clean the folder name into a search-friendly title (strip {imdb-tt} tag).
        title_hint = re.sub(r"\s*\{(?:imdb-tt\d+|tmdb-\d+)\}\s*", "", mf.title).strip()
        try:
            res = process_folder(mf, client, args.quality,
                                 args.replace_existing, args.dry_run,
                                 args.in_subfolder,
                                 args.youtube_search_fallback,
                                 title_hint)
        except Exception as e:
            log.exception("crash on %s: %s", mf.path, e)
            res = TrailerResult(str(mf.path), mf.title, "api_failed",
                                notes=f"{type(e).__name__}: {e}")
        all_results.append(res)

    counts: Dict[str, int] = {}
    for r in all_results:
        counts[r.action] = counts.get(r.action, 0) + 1

    log.info("=" * 60)
    log.info("Summary (%s):", "DRY-RUN" if args.dry_run else "APPLY")
    for k in ("downloaded", "skipped_exists", "no_trailer", "api_failed", "ytdlp_failed"):
        log.info("  %-15s %d", k, counts.get(k, 0))

    csv_path = cfg.mediaprep_dir / "trailer_fetch.csv"
    write_results(csv_path, all_results)
    log.info("Wrote: %s", csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
