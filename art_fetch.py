#!/usr/bin/env python3
"""
art_fetch.py — Walk H:\\Movies and download missing posters, fanart, and
landscape images from TMDB into each movie folder using Plex/Kodi-standard
naming (poster.jpg, fanart.jpg, landscape.jpg, logo.png).

Reads tmdb_api_key + movies_root + mediaprep_dir from config.json.

For each movie folder containing {imdb-tt#######} or {tmdb-#####} in its
name, fetches the TMDB images list, picks the best by language + vote
average, and downloads what's missing.

Usage:
    py art_fetch.py --config config.json
    py art_fetch.py --config config.json --dry-run
    py art_fetch.py --config config.json --types poster,fanart
    py art_fetch.py --config config.json --replace-existing
    py art_fetch.py --config config.json --imdb tt0078346,tt0058150
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from execute import (  # type: ignore
    _norm_path,
    load_config,
    long,
)

try:
    import requests  # type: ignore
except ImportError:
    print("ERROR: requests not installed. pip install requests", file=sys.stderr)
    sys.exit(3)


VERSION = "1.0.0"
log = logging.getLogger("art_fetch")

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"

# Default image sizes per Plex's tastes:
SIZE_POSTER     = "w780"
SIZE_FANART     = "original"  # backdrops should be high-res
SIZE_LANDSCAPE  = "w1280"
SIZE_LOGO       = "w500"

# Target filenames per asset type (Plex convention)
TARGET_NAMES = {
    "poster":    "poster.jpg",
    "fanart":    "fanart.jpg",
    "landscape": "landscape.jpg",
    "logo":      "clearlogo.png",
}

# TMDB API endpoints by type
TMDB_TYPE = {
    "poster":    ("posters",   SIZE_POSTER,    ".jpg"),
    "fanart":    ("backdrops", SIZE_FANART,    ".jpg"),
    "landscape": ("backdrops", SIZE_LANDSCAPE, ".jpg"),  # wide variant
    "logo":      ("logos",     SIZE_LOGO,      ".png"),
}


IMDB_IN_FOLDER_RX = re.compile(r"\{imdb-(tt\d{7,10})\}", re.IGNORECASE)
TMDB_IN_FOLDER_RX = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)


def setup_logging(mediaprep: Path, run_id: str) -> Path:
    mediaprep.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep / f"art_fetch_{run_id}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)
    log.info("art_fetch.py v%s starting; log: %s", VERSION, log_path)
    return log_path


# ---------------------------------------------------------------------------
# Walk + identify
# ---------------------------------------------------------------------------

@dataclass
class MovieFolder:
    path: Path
    imdb_id: str = ""
    tmdb_id: str = ""
    title: str = ""


def walk_library(root: Path) -> List[MovieFolder]:
    """Find every folder under root whose name contains an IMDB or TMDB id."""
    out: List[MovieFolder] = []
    if not root.exists():
        return out
    for entry in os.walk(str(root)):
        dirpath, dirs, files = entry
        # Skip the duplicates and mediaprep directories if they're under root
        skip_names = ("_mediaprep", "duplicates", ".tmm", ".sonarr")
        dirs[:] = [d for d in dirs if d not in skip_names]
        bn = os.path.basename(dirpath)
        m_imdb = IMDB_IN_FOLDER_RX.search(bn)
        m_tmdb = TMDB_IN_FOLDER_RX.search(bn)
        if not (m_imdb or m_tmdb):
            continue
        mf = MovieFolder(path=_norm_path(dirpath),
                         imdb_id=m_imdb.group(1).lower() if m_imdb else "",
                         tmdb_id=m_tmdb.group(1) if m_tmdb else "",
                         title=bn)
        # If we only have imdb_id and no tmdb_id, look in the NFO
        if not mf.tmdb_id:
            mf.tmdb_id = _read_nfo_tmdb_id(mf.path)
        out.append(mf)
    return out


def _read_nfo_tmdb_id(folder: Path) -> str:
    """Pull <tmdbid> from any .nfo in the folder."""
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


# ---------------------------------------------------------------------------
# TMDB API
# ---------------------------------------------------------------------------

class TmdbImageClient:
    def __init__(self, api_key: str, language: str = "en-US"):
        self.api_key = api_key
        self.lang_short = language.split("-")[0]  # "en" from "en-US"
        self._session = requests.Session()

    def _get(self, url: str, params: Dict[str, Any], timeout: int = 20
             ) -> Optional[Dict[str, Any]]:
        for attempt in range(3):
            try:
                r = self._session.get(url, params=params, timeout=timeout)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", "5")))
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                log.warning("TMDB GET %s attempt %d failed: %s", url, attempt+1, e)
                time.sleep(2 ** attempt)
        return None

    def resolve_imdb_to_tmdb(self, imdb_id: str) -> Optional[str]:
        data = self._get(f"{TMDB_BASE}/find/{imdb_id}",
                         {"api_key": self.api_key, "external_source": "imdb_id"})
        if not data:
            return None
        for r in data.get("movie_results", []):
            return str(r.get("id"))
        return None

    def get_images(self, tmdb_id: str) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        # include_image_language: prefer english, also accept null (language-agnostic)
        params = {
            "api_key": self.api_key,
            "include_image_language": f"{self.lang_short},null",
        }
        return self._get(f"{TMDB_BASE}/movie/{tmdb_id}/images", params)

    def download(self, file_path_token: str, size: str, dst: Path,
                 timeout: int = 30) -> bool:
        url = f"{TMDB_IMG_BASE}/{size}{file_path_token}"
        try:
            r = self._session.get(url, timeout=timeout, stream=True)
            r.raise_for_status()
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            with open(long(tmp), "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
            os.replace(str(tmp), str(dst))
            return True
        except (requests.RequestException, OSError) as e:
            log.warning("image download failed: %s -> %s: %s", url, dst, e)
            return False


# ---------------------------------------------------------------------------
# Pick the best image
# ---------------------------------------------------------------------------

def pick_best(images: List[Dict[str, Any]], lang_short: str,
              prefer_wide: bool = False) -> Optional[Dict[str, Any]]:
    """Sort by language preference and TMDB vote_average; return the top."""
    if not images:
        return None

    def score(img: Dict[str, Any]) -> Tuple[int, float, int]:
        lang = (img.get("iso_639_1") or "")
        lang_rank = 0 if lang == lang_short else (1 if lang == "" else 2)
        vote = float(img.get("vote_average") or 0.0)
        # Aspect-ratio nudge: landscape-favoring picks higher aspect, else lower
        w = int(img.get("width") or 0)
        h = int(img.get("height") or 1)
        aspect = w / max(h, 1)
        aspect_rank = (-aspect if prefer_wide else aspect) if h else 0
        # Lower lang_rank wins; higher vote wins; aspect tiebreaker
        return (lang_rank, -vote, int(aspect_rank * 1000))

    return sorted(images, key=score)[0]


# ---------------------------------------------------------------------------
# Per-movie processing
# ---------------------------------------------------------------------------

@dataclass
class ArtResult:
    folder: str
    title: str
    asset: str
    action: str   # downloaded | skipped_exists | no_image | api_failed
    size_bytes: int = 0
    notes: str = ""


def process_folder(mf: MovieFolder, client: TmdbImageClient,
                   types_to_fetch: List[str], replace_existing: bool,
                   dry_run: bool) -> List[ArtResult]:
    results: List[ArtResult] = []

    if not mf.tmdb_id and mf.imdb_id:
        mf.tmdb_id = client.resolve_imdb_to_tmdb(mf.imdb_id) or ""
    if not mf.tmdb_id:
        for asset in types_to_fetch:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "api_failed", notes="no TMDB id available"))
        return results

    images = client.get_images(mf.tmdb_id)
    if images is None:
        for asset in types_to_fetch:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "api_failed", notes="images endpoint failed"))
        return results

    for asset in types_to_fetch:
        tmdb_key, size, ext = TMDB_TYPE[asset]
        target_name = TARGET_NAMES[asset]
        target_path = mf.path / target_name

        if target_path.exists() and not replace_existing:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "skipped_exists",
                                     size_bytes=os.path.getsize(long(target_path))))
            continue

        bucket = images.get(tmdb_key) or []
        prefer_wide = asset in ("fanart", "landscape")
        best = pick_best(bucket, client.lang_short, prefer_wide=prefer_wide)
        if not best:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "no_image", notes=f"no {tmdb_key} in TMDB"))
            continue

        if dry_run:
            results.append(ArtResult(str(mf.path), mf.title, asset, "downloaded",
                                     notes=f"DRY-RUN would fetch {size}{best['file_path']}"))
            continue

        ok = client.download(best["file_path"], size, target_path)
        if ok:
            sz = os.path.getsize(long(target_path)) if target_path.exists() else 0
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "downloaded", size_bytes=sz))
        else:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "api_failed", notes="download failed"))

    return results


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def write_results(path: Path, rows: List[ArtResult]) -> None:
    headers = ["action", "asset", "title", "size_bytes", "folder", "notes"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({
                "action": r.action, "asset": r.asset, "title": r.title,
                "size_bytes": r.size_bytes, "folder": r.folder, "notes": r.notes,
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="art_fetch.py",
        description="Download missing posters/fanart/landscape/logo from TMDB.",
    )
    p.add_argument("--config", default="config.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--types", default="poster,fanart,landscape,logo",
                   help="comma-separated subset: poster,fanart,landscape,logo")
    p.add_argument("--replace-existing", action="store_true",
                   help="re-download even if target already exists")
    p.add_argument("--imdb", default="",
                   help="comma-separated imdb ids to process only")
    p.add_argument("--limit", type=int, default=0)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config(_norm_path(args.config))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr); return 3

    if not cfg.tmdb_api_key or cfg.tmdb_api_key == "REPLACE_ME":
        print("TMDB API key not configured", file=sys.stderr); return 4

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    setup_logging(cfg.mediaprep_dir, run_id)

    types_to_fetch = [t.strip() for t in args.types.split(",") if t.strip()]
    for t in types_to_fetch:
        if t not in TARGET_NAMES:
            log.error("unknown asset type: %r (valid: %s)", t, list(TARGET_NAMES))
            return 4

    log.info("Walking %s ...", cfg.movies_root)
    folders = walk_library(cfg.movies_root)
    log.info("Found %d movie folders with IMDB/TMDB tag", len(folders))

    if args.imdb:
        wanted = {x.strip().lower() for x in args.imdb.split(",") if x.strip()}
        folders = [f for f in folders if f.imdb_id.lower() in wanted]
        log.info("Filtered to %d folders via --imdb", len(folders))
    if args.limit:
        folders = folders[:args.limit]
        log.info("Limited to %d folders via --limit", len(folders))

    client = TmdbImageClient(cfg.tmdb_api_key, cfg.language)
    all_results: List[ArtResult] = []
    t0 = time.time()
    for i, mf in enumerate(folders, 1):
        if i % 25 == 0 or i == len(folders):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            log.info("[%d/%d] (%.1f folders/s) ...", i, len(folders), rate)
        try:
            res = process_folder(mf, client, types_to_fetch,
                                 args.replace_existing, args.dry_run)
        except Exception as e:
            log.exception("folder %s crashed: %s", mf.path, e)
            res = [ArtResult(str(mf.path), mf.title, "?", "api_failed",
                             notes=f"{type(e).__name__}: {e}")]
        all_results.extend(res)

    # Summary
    counts: Dict[str, int] = {}
    bytes_total = 0
    for r in all_results:
        counts[r.action] = counts.get(r.action, 0) + 1
        if r.action == "downloaded":
            bytes_total += r.size_bytes

    log.info("=" * 60)
    log.info("Summary (mode=%s):", "DRY-RUN" if args.dry_run else "APPLY")
    for k in ("downloaded", "skipped_exists", "no_image", "api_failed"):
        log.info("  %-15s %d", k, counts.get(k, 0))
    log.info("  Bytes downloaded: %.1f MB", bytes_total / (1024*1024))

    csv_path = cfg.mediaprep_dir / "art_fetch.csv"
    write_results(csv_path, all_results)
    log.info("Wrote: %s", csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
