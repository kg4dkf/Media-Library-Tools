#!/usr/bin/env python3
"""
Download TMDB artwork and extras (trailers, featurettes, behind-the-scenes,
deleted scenes, etc.) for one or many TV shows. Uses Plex / Jellyfin / Sonarr
local-media naming conventions so all three players pick them up automatically.

Per-show assets written at the show root:
    poster.jpg            primary key art (English preferred)
    background.jpg        fanart / backdrop (English or language-neutral)
    banner.jpg            wide banner
    clearlogo.png         transparent logo (English preferred)
    Trailers/             *-trailer.mp4 (when youtube link available)
    Featurettes/          *-featurette.mp4
    Behind The Scenes/    *-behindthescenes.mp4
    Deleted Scenes/       *-deleted.mp4
    Clips/                *-scene.mp4
    Other/                everything else

Per-season:
    Season XX/Season XX-poster.jpg

Requires:
    TMDB_API_KEY env var.
    yt-dlp on PATH for trailer/extras video downloads (skip with --no-videos).

Modes:
    --scan G:\\TV
        Scan a media root, infer TMDB id from each folder's {tmdb-NNNN}
        marker, and download artwork for every show found.

    --tmdb 1668 --out "G:\\TV\\Friends (1994) {tmdb-1668}"
        Single-show mode for ad-hoc use.

    --from-folder "G:\\TV\\Friends (1994) {tmdb-1668}"
        Infer TMDB id from that one folder's name.

Safe to re-run: existing files are skipped. Use --force to re-download.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests
from tqdm import tqdm


TMDB_API = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/original"
TMDB_FOLDER_RE = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)

# Plex community-hosted theme songs, indexed by TVDB id (not TMDB).
# We resolve TVDB id via TMDB's external_ids endpoint.
THEME_BASE = "http://tvthemes.plexapp.com"


# Maps TMDB video types to the Plex/Jellyfin extras folder name
VIDEO_TYPE_FOLDER = {
    "trailer":              ("Trailers",          "trailer"),
    "teaser":               ("Trailers",          "trailer"),
    "featurette":           ("Featurettes",       "featurette"),
    "behind the scenes":    ("Behind The Scenes", "behindthescenes"),
    "bloopers":             ("Featurettes",       "featurette"),
    "clip":                 ("Clips",             "scene"),
    "opening credits":      ("Other",             "other"),
}


def safe_filename(s: str, maxlen: int = 100) -> str:
    out = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", s)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:maxlen] or "untitled"


_SEASON_DIR_RE = re.compile(r"^[Ss]eason\s+(\d{1,2})(?:\s+\((\d{4})\))?\s*$")


def _pick_season_folder(show_dir: Path, snum: int,
                        air_date: Optional[str] = None) -> Path:
    """Return the season folder path to write into.

    Prefers an existing 'Season NN (YYYY)' or 'Season NN' folder. If
    neither exists and `air_date` is provided (TMDB format 'YYYY-MM-DD'),
    create with year suffix so it matches the user's canonical naming.
    """
    existing = None
    for child in show_dir.iterdir() if show_dir.is_dir() else []:
        if not child.is_dir():
            continue
        m = _SEASON_DIR_RE.match(child.name)
        if m and int(m.group(1)) == snum:
            existing = child
            break
    if existing is not None:
        return existing
    year = None
    if air_date and len(air_date) >= 4 and air_date[:4].isdigit():
        year = int(air_date[:4])
    if year:
        return show_dir / f"Season {snum:02d} ({year})"
    return show_dir / f"Season {snum:02d}"


def make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    s.params = {"api_key": api_key}
    return s


def tmdb_get(s: requests.Session, endpoint: str,
             params: Optional[dict] = None) -> Optional[dict]:
    try:
        r = s.get(f"{TMDB_API}/{endpoint}",
                  params=params or {}, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        sys.stderr.write(f"TMDB error on {endpoint}: {e}\n")
        return None


def pick_best(images: List[dict], lang_pref: str = "en") -> Optional[dict]:
    """Choose the highest-rated image, preferring a given language then null."""
    if not images:
        return None
    en = [i for i in images if i.get("iso_639_1") == lang_pref]
    nul = [i for i in images if i.get("iso_639_1") is None]
    cands = en or nul or images
    cands.sort(
        key=lambda i: (i.get("vote_average", 0) or 0,
                       i.get("width", 0) or 0),
        reverse=True,
    )
    return cands[0]


def download_file(url: str, out_path: Path, force: bool = False) -> bool:
    if out_path.exists() and not force and out_path.stat().st_size > 0:
        return False  # already present, skipped
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        tmp.replace(out_path)
        return True
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        sys.stderr.write(f"  ! failed to download {url}: {e}\n")
        return False


def yt_dlp_available() -> bool:
    return shutil.which("yt-dlp") is not None


def download_youtube(youtube_id: str, out_path: Path,
                     max_height: int = 1080) -> bool:
    """Download a YouTube video via yt-dlp. Returns True on success."""
    if out_path.exists() and out_path.stat().st_size > 0:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Let yt-dlp pick the extension; we'll rename if needed.
    template = str(out_path.with_suffix(".%(ext)s"))
    cmd = [
        "yt-dlp",
        "-q", "--no-warnings",
        "-f", f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
        "--merge-output-format", "mp4",
        "-o", template,
        f"https://www.youtube.com/watch?v={youtube_id}",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"  ! yt-dlp timed out for {youtube_id}\n")
        return False
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode("utf-8", "ignore")[:200] if e.stderr else str(e)
        sys.stderr.write(f"  ! yt-dlp failed for {youtube_id}: {msg}\n")
        return False
    # yt-dlp may have written .mkv or .mp4 depending on streams.
    if out_path.exists() and out_path.stat().st_size > 0:
        return True
    candidates = sorted(out_path.parent.glob(out_path.stem + ".*"))
    for c in candidates:
        if c.suffix.lower() in {".mp4", ".mkv", ".webm"}:
            c.rename(out_path.with_suffix(c.suffix))
            return True
    return False


@dataclass
class ShowJob:
    tmdb_id: str
    out_dir: Path
    label: str = ""


def download_theme(sess: requests.Session, tmdb_id: str,
                   out_path: Path, force: bool = False) -> Optional[bool]:
    """Resolve TVDB id via TMDB external_ids, then pull theme.mp3 from
    tvthemes.plexapp.com. Returns True on download, False on skip, None on
    failure (no TVDB id available or no theme on the server)."""
    if out_path.exists() and not force and out_path.stat().st_size > 0:
        return False
    ext = tmdb_get(sess, f"tv/{tmdb_id}/external_ids") or {}
    tvdb_id = ext.get("tvdb_id")
    if not tvdb_id:
        return None
    theme_url = f"{THEME_BASE}/{tvdb_id}.mp3"
    try:
        r = requests.get(theme_url, timeout=30, stream=True)
        if r.status_code != 200:
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".part")
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        # Sanity-check: 404s sometimes return HTML; theme.mp3 should be >= a few KB.
        if tmp.stat().st_size < 1024:
            tmp.unlink()
            return None
        tmp.replace(out_path)
        return True
    except Exception:
        return None


def process_show(sess: requests.Session, job: ShowJob,
                 want_videos: bool, want_themes: bool, force: bool) -> dict:
    """Download artwork + extras for one show. Returns a stats dict."""
    stats = {"images": 0, "season_posters": 0, "videos": 0, "themes": 0,
             "skipped": 0, "errors": 0, "label": job.label or job.tmdb_id}

    details = tmdb_get(sess, f"tv/{job.tmdb_id}")
    if details is None:
        stats["errors"] += 1
        return stats
    name = details.get("name") or job.label
    stats["label"] = name

    # Show-level images. include_image_language asks TMDB to also return
    # language-neutral assets (null), which is where many clearlogos live.
    images = tmdb_get(sess, f"tv/{job.tmdb_id}/images",
                      {"include_image_language": "en,null"}) or {}

    # poster.jpg
    best = pick_best(images.get("posters") or [])
    if best:
        ok = download_file(IMAGE_BASE + best["file_path"],
                           job.out_dir / "poster.jpg", force=force)
        if ok: stats["images"] += 1
        else: stats["skipped"] += 1

    # background.jpg (Plex prefers this name; Jellyfin also accepts fanart.jpg)
    best = pick_best(images.get("backdrops") or [])
    if best:
        ok = download_file(IMAGE_BASE + best["file_path"],
                           job.out_dir / "background.jpg", force=force)
        if ok: stats["images"] += 1
        else: stats["skipped"] += 1

    # clearlogo.png
    best = pick_best(images.get("logos") or [])
    if best:
        # TMDB returns .png most of the time but sometimes .svg
        suffix = Path(best["file_path"]).suffix or ".png"
        target = job.out_dir / f"clearlogo{suffix}"
        ok = download_file(IMAGE_BASE + best["file_path"], target, force=force)
        if ok: stats["images"] += 1
        else: stats["skipped"] += 1

    # theme.mp3 (community-hosted via Plex)
    if want_themes:
        theme_target = job.out_dir / "theme.mp3"
        res = download_theme(sess, job.tmdb_id, theme_target, force=force)
        if res is True: stats["themes"] += 1
        elif res is False: stats["skipped"] += 1
        # res is None -> no TVDB id or no theme on the server; ignore silently

    # Per-season posters.
    for season in details.get("seasons", []) or []:
        snum = season.get("season_number")
        if snum is None:
            continue
        si = tmdb_get(sess, f"tv/{job.tmdb_id}/season/{snum}/images",
                      {"include_image_language": "en,null"}) or {}
        best = pick_best(si.get("posters") or [])
        if not best:
            continue
        # Prefer an existing season folder (which may be "Season 01 (1987)").
        # Only create a bare "Season NN" if no matching folder exists.
        season_dir = _pick_season_folder(job.out_dir, snum, season.get("air_date"))
        # Plex / Jellyfin both accept "Season XX-poster.jpg" in show root
        # OR "Season XX.jpg" inside the season folder. Drop one in each
        # so whichever the user's player prefers, it works.
        flat_target = job.out_dir / f"Season {snum:02d}-poster.jpg"
        nested_target = season_dir / "poster.jpg"
        a = download_file(IMAGE_BASE + best["file_path"], flat_target, force=force)
        b = download_file(IMAGE_BASE + best["file_path"], nested_target, force=force)
        stats["season_posters"] += int(a) + int(b)
        if not a: stats["skipped"] += 1
        if not b: stats["skipped"] += 1

    # Videos (trailers, featurettes, behind-the-scenes, etc.) via yt-dlp.
    if want_videos:
        vids = tmdb_get(sess, f"tv/{job.tmdb_id}/videos") or {}
        for v in vids.get("results", []) or []:
            if v.get("site") != "YouTube":
                continue
            key = v.get("key")
            if not key:
                continue
            vtype = (v.get("type") or "").lower()
            folder, suffix = VIDEO_TYPE_FOLDER.get(vtype, ("Other", "other"))
            base_name = safe_filename(v.get("name") or vtype or "extra")
            target = job.out_dir / folder / f"{base_name}-{suffix}.mp4"
            if target.exists():
                stats["skipped"] += 1
                continue
            ok = download_youtube(key, target)
            if ok: stats["videos"] += 1
            else: stats["errors"] += 1

    return stats


def scan_media_root(root: Path) -> List[ShowJob]:
    """Find every immediate child folder with a {tmdb-NNNN} marker."""
    jobs: List[ShowJob] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        m = TMDB_FOLDER_RE.search(child.name)
        if not m:
            continue
        jobs.append(ShowJob(tmdb_id=m.group(1), out_dir=child, label=child.name))
    return jobs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("source (pick one)")
    src.add_argument("--scan", type=Path, help="Scan a media root for {tmdb-NNNN} folders")
    src.add_argument("--from-folder", type=Path, help="Single existing show folder")
    src.add_argument("--tmdb", help="Single TMDB id (use with --out)")
    ap.add_argument("--out", type=Path, help="Target folder (required with --tmdb)")

    ap.add_argument("--no-videos", action="store_true",
                    help="Skip trailer/featurette downloads (artwork only)")
    ap.add_argument("--no-themes", action="store_true",
                    help="Skip theme.mp3 downloads from tvthemes.plexapp.com")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if the target file already exists")
    ap.add_argument("--report", type=Path, default=Path("artwork_report.csv"))
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N shows (0 = unlimited)")
    args = ap.parse_args()

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        sys.exit("Set TMDB_API_KEY env var. Get a key at "
                 "https://www.themoviedb.org/settings/api")

    if not args.no_videos and not yt_dlp_available():
        sys.stderr.write("WARN: yt-dlp not on PATH; trailer/extras downloads will fail.\n"
                         "      Install with: pip install yt-dlp\n"
                         "      Or re-run with --no-videos to skip videos entirely.\n")

    jobs: List[ShowJob] = []
    if args.scan:
        if not args.scan.is_dir():
            sys.exit(f"Not a directory: {args.scan}")
        jobs = scan_media_root(args.scan)
        print(f"Found {len(jobs)} show folders under {args.scan}")
    elif args.from_folder:
        m = TMDB_FOLDER_RE.search(args.from_folder.name)
        if not m:
            sys.exit(f"No {{tmdb-NNNN}} marker in folder name: {args.from_folder.name}")
        jobs.append(ShowJob(tmdb_id=m.group(1), out_dir=args.from_folder,
                            label=args.from_folder.name))
    elif args.tmdb:
        if not args.out:
            sys.exit("--out is required with --tmdb")
        jobs.append(ShowJob(tmdb_id=args.tmdb, out_dir=args.out,
                            label=args.out.name))
    else:
        sys.exit("Pass --scan, --from-folder, or --tmdb.")

    if args.limit:
        jobs = jobs[: args.limit]

    sess = make_session(api_key)
    rows = []
    for job in tqdm(jobs, desc="shows", unit="show"):
        stats = process_show(sess, job,
                             want_videos=not args.no_videos,
                             want_themes=not args.no_themes,
                             force=args.force)
        rows.append({
            "label": stats["label"],
            "tmdb_id": job.tmdb_id,
            "folder": str(job.out_dir),
            "images": stats["images"],
            "season_posters": stats["season_posters"],
            "videos": stats["videos"],
            "themes": stats["themes"],
            "skipped": stats["skipped"],
            "errors": stats["errors"],
        })

    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["label","tmdb_id","folder","images","season_posters","videos","themes","skipped","errors"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    totals = {k: sum(int(r.get(k, 0) or 0) for r in rows)
              for k in ("images", "season_posters", "videos", "themes", "skipped", "errors")}
    print(f"\nDone. Shows processed: {len(rows)}")
    print(f"  new images:         {totals['images']}")
    print(f"  new season posters: {totals['season_posters']}")
    print(f"  new videos:         {totals['videos']}")
    print(f"  new themes:         {totals['themes']}")
    print(f"  skipped (existed):  {totals['skipped']}")
    print(f"  errors:             {totals['errors']}")
    print(f"  report:             {args.report}")


if __name__ == "__main__":
    main()
