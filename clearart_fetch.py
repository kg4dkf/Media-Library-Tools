#!/usr/bin/env python3
"""
clearart_fetch.py — Download clearlogo, clearart, and banner images from
Fanart.tv into each movie folder. Fanart.tv has higher-quality art for
older/cult films than TMDB does.

Prerequisite: free Fanart.tv personal API key from
https://fanart.tv/get-an-api-key/. Add to config.json:
    "fanart_tv_api_key": "your-personal-key"

Plex naming:
    clearlogo.png    — transparent text logo of the movie title
    clearart.png     — transparent character/scene art
    banner.jpg       — wide banner
    disc.png         — disc art (Plex doesn't natively use, Kodi does)

Usage:
    py clearart_fetch.py --config config.json
    py clearart_fetch.py --config config.json --dry-run
    py clearart_fetch.py --config config.json --types clearlogo,clearart
    py clearart_fetch.py --config config.json --imdb tt0078346
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


VERSION = "1.0.0"
log = logging.getLogger("clearart_fetch")

FANART_BASE = "https://webservice.fanart.tv/v3/movies"
IMDB_RX = re.compile(r"\{imdb-(tt\d{7,10})\}", re.IGNORECASE)

# Fanart.tv asset key → (target filename, prefer_lang)
ASSET_MAP = {
    "clearlogo": ("clearlogo.png", "hdmovielogo", "en"),
    "clearart":  ("clearart.png",  "hdmovieclearart", "en"),
    "banner":    ("banner.jpg",    "moviebanner", "en"),
    "disc":      ("disc.png",      "moviedisc", "en"),
}


def setup_logging(mediaprep: Path, run_id: str) -> Path:
    mediaprep.mkdir(parents=True, exist_ok=True)
    log_path = mediaprep / f"clearart_fetch_{run_id}.log"
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log.handlers.clear(); log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)
    log.info("clearart_fetch.py v%s starting; log: %s", VERSION, log_path)
    return log_path


@dataclass
class MovieFolder:
    path: Path
    imdb_id: str = ""
    title: str = ""


def walk_library(root: Path) -> List[MovieFolder]:
    out: List[MovieFolder] = []
    if not root.exists(): return out
    for dirpath, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs
                   if d not in ("_mediaprep", "duplicates", ".tmm", ".sonarr")]
        bn = os.path.basename(dirpath)
        m_imdb = IMDB_RX.search(bn)
        if not m_imdb:
            continue
        out.append(MovieFolder(path=_norm_path(dirpath),
                               imdb_id=m_imdb.group(1).lower(), title=bn))
    return out


class FanartClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session = requests.Session()
        self._session.headers["api-key"] = api_key

    def get_movie(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        url = f"{FANART_BASE}/{imdb_id}"
        for attempt in range(3):
            try:
                r = self._session.get(url, timeout=20)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", "5")))
                    continue
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                log.warning("Fanart %s attempt %d: %s", imdb_id, attempt+1, e)
                time.sleep(2 ** attempt)
        return None

    def download(self, url: str, dst: Path, timeout: int = 30) -> bool:
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
            log.warning("download failed: %s: %s", url, e)
            return False


def pick_best(images: List[Dict[str, Any]], prefer_lang: str) -> Optional[Dict[str, Any]]:
    """Sort by language preference then likes; return top."""
    if not images:
        return None

    def score(img: Dict[str, Any]) -> tuple:
        lang = (img.get("lang") or "").lower()
        lang_rank = 0 if lang == prefer_lang else (1 if lang in ("", "00") else 2)
        likes = int(img.get("likes") or 0)
        return (lang_rank, -likes)

    return sorted(images, key=score)[0]


@dataclass
class ArtResult:
    folder: str
    title: str
    asset: str
    action: str   # downloaded | skipped_exists | no_image | api_failed
    size_bytes: int = 0
    notes: str = ""


def process_folder(mf: MovieFolder, client: FanartClient,
                   types: List[str], replace: bool,
                   dry_run: bool) -> List[ArtResult]:
    results: List[ArtResult] = []
    if not mf.imdb_id:
        for asset in types:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "api_failed", notes="no IMDB id"))
        return results

    data = client.get_movie(mf.imdb_id)
    if data is None:
        for asset in types:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "api_failed", notes="Fanart.tv 404 or error"))
        return results

    for asset in types:
        if asset not in ASSET_MAP:
            continue
        target_name, fanart_key, lang = ASSET_MAP[asset]
        target_path = mf.path / target_name
        if target_path.exists() and not replace:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "skipped_exists",
                                     size_bytes=os.path.getsize(long(target_path))))
            continue
        bucket = data.get(fanart_key) or []
        best = pick_best(bucket, lang)
        if not best:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "no_image",
                                     notes=f"no {fanart_key} on Fanart.tv"))
            continue
        url = best.get("url")
        if not url:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "no_image", notes="no url in record"))
            continue
        if dry_run:
            results.append(ArtResult(str(mf.path), mf.title, asset, "downloaded",
                                     notes=f"DRY-RUN {url}"))
            continue
        ok = client.download(url, target_path)
        if ok:
            sz = os.path.getsize(long(target_path)) if target_path.exists() else 0
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "downloaded", size_bytes=sz))
        else:
            results.append(ArtResult(str(mf.path), mf.title, asset,
                                     "api_failed", notes="download failed"))
    return results


def write_results(path: Path, rows: List[ArtResult]) -> None:
    headers = ["action", "asset", "title", "size_bytes", "folder", "notes"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({"action": r.action, "asset": r.asset, "title": r.title,
                        "size_bytes": r.size_bytes, "folder": r.folder,
                        "notes": r.notes})


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="clearart_fetch.py",
                                description="Download clearlogo/clearart/banner from Fanart.tv.")
    p.add_argument("--config", default="config.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--types", default="clearlogo,clearart",
                   help="comma-separated: clearlogo,clearart,banner,disc")
    p.add_argument("--replace-existing", action="store_true")
    p.add_argument("--imdb", default="")
    p.add_argument("--limit", type=int, default=0)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        cfg = load_config(_norm_path(args.config))
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr); return 3
    fanart_key = cfg.raw.get("fanart_tv_api_key", "")
    if not fanart_key or fanart_key == "REPLACE_ME":
        print('Add "fanart_tv_api_key": "..." to config.json '
              '(free at https://fanart.tv/get-an-api-key/)', file=sys.stderr)
        return 4

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    setup_logging(cfg.mediaprep_dir, run_id)

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    for t in types:
        if t not in ASSET_MAP:
            log.error("unknown asset: %r (valid: %s)", t, list(ASSET_MAP))
            return 4

    folders = walk_library(cfg.movies_root)
    log.info("Found %d movie folders with IMDB id", len(folders))
    if args.imdb:
        wanted = {x.strip().lower() for x in args.imdb.split(",") if x.strip()}
        folders = [f for f in folders if f.imdb_id.lower() in wanted]
    if args.limit:
        folders = folders[:args.limit]

    client = FanartClient(fanart_key)
    all_results: List[ArtResult] = []
    t0 = time.time()
    for i, mf in enumerate(folders, 1):
        if i % 25 == 0 or i == len(folders):
            log.info("[%d/%d] processing ...", i, len(folders))
        try:
            res = process_folder(mf, client, types,
                                 args.replace_existing, args.dry_run)
        except Exception as e:
            log.exception("crash on %s: %s", mf.path, e)
            res = [ArtResult(str(mf.path), mf.title, "?", "api_failed",
                             notes=f"{type(e).__name__}: {e}")]
        all_results.extend(res)

    counts: Dict[str, int] = {}
    bytes_total = 0
    for r in all_results:
        counts[r.action] = counts.get(r.action, 0) + 1
        if r.action == "downloaded":
            bytes_total += r.size_bytes

    log.info("=" * 60)
    log.info("Summary (%s):", "DRY-RUN" if args.dry_run else "APPLY")
    for k in ("downloaded", "skipped_exists", "no_image", "api_failed"):
        log.info("  %-15s %d", k, counts.get(k, 0))
    log.info("  Bytes downloaded: %.1f MB", bytes_total / (1024*1024))

    csv_path = cfg.mediaprep_dir / "clearart_fetch.csv"
    write_results(csv_path, all_results)
    log.info("Wrote: %s", csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
