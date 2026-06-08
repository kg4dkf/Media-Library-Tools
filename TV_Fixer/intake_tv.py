#!/usr/bin/env python3
"""
Intake new TV episodes from a drop folder into a Plex-formatted library.

Workflow per file:
  1. Parse the filename with guessit to extract show name, season, episode,
     and quality hints.
  2. Look up the show on TMDB to get the canonical name + year + id.
  3. Build the destination path in Plex naming:
        <media-root>\Show Name (Year) {tmdb-NNNN}\Season XX\
                Show Name (Year) - S01E01 - Episode Title.ext
  4. If the destination episode does NOT exist yet, move the file there.
  5. If it DOES exist, ffprobe both copies and compare quality
     (height -> bitrate -> size). Keep the winner. Send the loser to the
     Recycle Bin so it's recoverable for a few days.
  6. If the show folder is new, optionally trigger download_artwork.py to
     pull poster, fanart, clearlogo, season posters, and trailers.

Requires:
    TMDB_API_KEY env var.
    pip install guessit send2trash
    download_artwork.py in the same directory (for --download-art).

Safety:
    --dry-run by default; nothing is moved or deleted.
    Pass --commit to actually act.

Example:
    python intake_tv.py "F:\intake" --media-root "G:\TV" --commit
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
from typing import Dict, List, Optional, Tuple

import requests
from tqdm import tqdm

import common

TMDB_API = "https://api.themoviedb.org/3"

VIDEO_EXTS = common.VIDEO_EXTS  # reuse the same set as dedupe / identify


def _safe(s) -> str:
    """Round-trip strings through UTF-8 with errors='replace' so unpaired
    Windows surrogates in filenames don't crash csv writers."""
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


# ---------- Filename parsing ----------

def parse_filename(name: str) -> dict:
    """Extract title/season/episode/year/quality from a filename via guessit."""
    try:
        import guessit
    except ImportError:
        sys.exit("pip install guessit")
    info = guessit.guessit(name)
    # guessit returns 'episode' as int OR a list (for multi-episode files).
    ep = info.get("episode")
    if isinstance(ep, list):
        ep = ep[0]  # take the first; multi-episode handling is below
    return {
        "title": info.get("title"),
        "year": info.get("year"),
        "season": info.get("season"),
        "episode": ep,
        "episodes_all": info.get("episode") if isinstance(info.get("episode"), list) else None,
        "episode_title": info.get("episode_title"),
        "screen_size": info.get("screen_size"),  # e.g. '1080p'
        "source": info.get("source"),            # e.g. 'WEB-DL'
        "video_codec": info.get("video_codec"),
    }


# ---------- TMDB ----------

def tmdb_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    s.params = {"api_key": api_key}
    return s


def tmdb_search_show(s: requests.Session, title: str, year: Optional[int]) -> Optional[dict]:
    params = {"query": title}
    if year:
        params["first_air_date_year"] = year
    r = s.get(f"{TMDB_API}/search/tv", params=params, timeout=30)
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return None
    # If year was supplied, prefer exact matches.
    if year:
        for hit in results:
            d = hit.get("first_air_date") or ""
            if d.startswith(str(year)):
                return hit
    return results[0]


def tmdb_episode(s: requests.Session, tmdb_id: str,
                 season: int, episode: int) -> Optional[dict]:
    r = s.get(f"{TMDB_API}/tv/{tmdb_id}/season/{season}/episode/{episode}",
              timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# ---------- Path / naming ----------

def safe_segment(s: str, maxlen: int = 100) -> str:
    out = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", s)
    out = re.sub(r"\s+", " ", out).strip(" .")
    return out[:maxlen] or "untitled"


def show_folder_name(canonical_name: str, year: Optional[int],
                     tmdb_id: str) -> str:
    parts = [safe_segment(canonical_name)]
    if year:
        parts[-1] = f"{parts[-1]} ({year})"
    parts.append(f"{{tmdb-{tmdb_id}}}")
    return " ".join(parts)


def episode_filename(canonical_name: str, year: Optional[int],
                     season: int, episode: int,
                     episode_title: Optional[str], ext: str) -> str:
    base = safe_segment(canonical_name)
    year_part = f" ({year})" if year else ""
    se = f"S{season:02d}E{episode:02d}"
    title = f" - {safe_segment(episode_title)}" if episode_title else ""
    return f"{base}{year_part} - {se}{title}{ext}"


_TMDB_FOLDER_MARKER = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)


def _pick_show_folder(media_root: Path, tmdb_id: str,
                      canonical_name: str, year: Optional[int]) -> Path:
    """Find an existing show folder by its {tmdb-NNNN} marker so we don't
    create a duplicate when TMDB's canonical name differs from the user's
    on-disk name. Falls back to a fresh canonical folder name."""
    if media_root.is_dir():
        for child in media_root.iterdir():
            if not child.is_dir():
                continue
            m = _TMDB_FOLDER_MARKER.search(child.name)
            if m and m.group(1) == str(tmdb_id):
                return child
    return media_root / show_folder_name(canonical_name, year, tmdb_id)


_SEASON_DIR_RE = re.compile(r"^[Ss]eason\s+(\d{1,2})(?:\s+\((\d{4})\))?\s*$")


def _pick_season_folder(show_dir: Path, snum: int,
                        air_date: Optional[str] = None) -> Path:
    """Return the season folder path to write into.

    Prefers an existing 'Season NN (YYYY)' or 'Season NN' folder. If
    neither exists and air_date is provided ('YYYY-MM-DD'), create with
    year suffix to match the canonical naming.
    """
    if show_dir.is_dir():
        for child in show_dir.iterdir():
            if not child.is_dir():
                continue
            m = _SEASON_DIR_RE.match(child.name)
            if m and int(m.group(1)) == snum:
                return child
    year = None
    if air_date and len(air_date) >= 4 and air_date[:4].isdigit():
        year = int(air_date[:4])
    if year:
        return show_dir / f"Season {snum:02d} ({year})"
    return show_dir / f"Season {snum:02d}"


# ---------- Quality comparison ----------

def quality_key(info: Optional[common.MediaInfo]) -> Tuple[int, int, int]:
    """Higher is better: (height, vbitrate, size)."""
    if info is None:
        return (0, 0, 0)
    return (info.height or 0, info.vbitrate or 0, info.size or 0)


def quality_label(info: Optional[common.MediaInfo]) -> str:
    if info is None:
        return "unknown"
    h = info.height or 0
    br = (info.vbitrate or 0) // 1000
    sz_mb = (info.size or 0) // (1024 * 1024)
    return f"{h}p {br}kbps {sz_mb}MB"


# ---------- Pipeline ----------

@dataclass
class IntakeRow:
    src: Path
    status: str
    reason: str = ""
    dest: Optional[Path] = None
    show: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    quality_before: str = ""
    quality_after: str = ""


def find_existing_episode(season_dir: Path, season: int, episode: int) -> Optional[Path]:
    """Find any file in season_dir that looks like SxxEyy for our episode."""
    if not season_dir.is_dir():
        return None
    se_re = re.compile(
        rf"[Ss]{season:02d}[Ee]{episode:02d}(?!\d)|"
        rf"(?<!\d){season}x{episode:02d}(?!\d)",
    )
    for p in season_dir.iterdir():
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        if se_re.search(p.name):
            return p
    return None


def to_recycle(path: Path) -> None:
    from send2trash import send2trash
    send2trash(str(path))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("intake", type=Path, help="Drop folder to scan for new files")
    ap.add_argument("--media-root", type=Path, required=True,
                    help="Plex-formatted library root, e.g. G:\\TV")
    ap.add_argument("--commit", action="store_true",
                    help="Actually move files. Without this, prints planned actions only.")
    ap.add_argument("--report", type=Path, default=Path("intake_report.csv"))
    ap.add_argument("--download-art", action="store_true",
                    help="Run download_artwork.py against any new show folder created")
    ap.add_argument("--no-art-videos", action="store_true",
                    help="With --download-art, skip trailer/extras downloads (artwork only)")
    ap.add_argument("--failed-dir", type=Path, default=None,
                    help="Move unparseable / unidentifiable files here instead of leaving in intake")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N files (0 = unlimited)")
    args = ap.parse_args()

    if not args.intake.is_dir():
        sys.exit(f"Intake folder not found: {args.intake}")
    if not args.media_root.exists():
        args.media_root.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        sys.exit("Set TMDB_API_KEY env var.")

    ffprobe = common.require_tool("ffprobe")

    sess = tmdb_session(api_key)

    # Cache TMDB lookups within this run.
    show_cache: Dict[str, dict] = {}
    def lookup_show(title: str, year: Optional[int]) -> Optional[dict]:
        key = f"{title}::{year or ''}"
        if key in show_cache:
            return show_cache[key]
        hit = tmdb_search_show(sess, title, year)
        show_cache[key] = hit
        return hit

    targets: List[Path] = []
    for p in sorted(args.intake.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            targets.append(p)
        if args.limit and len(targets) >= args.limit:
            break

    if not targets:
        print(f"No video files found under {args.intake}")
        return

    print(f"Found {len(targets)} files in {args.intake}. "
          f"{'COMMIT mode' if args.commit else 'DRY RUN (use --commit to act)'}")

    rows: List[IntakeRow] = []
    new_show_folders: set = set()

    for src in tqdm(targets, desc="intake", unit="file"):
        info_parsed = parse_filename(src.name)
        title = info_parsed.get("title")
        season = info_parsed.get("season")
        episode = info_parsed.get("episode")
        year = info_parsed.get("year")

        if not title or season is None or episode is None:
            row = IntakeRow(src=src, status="unparseable",
                            reason=f"guessit could not extract show/S/E from {src.name!r}")
            rows.append(row)
            if args.commit and args.failed_dir:
                args.failed_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(args.failed_dir / src.name))
            continue

        hit = lookup_show(title, year)
        if not hit:
            row = IntakeRow(src=src, status="no_tmdb_match",
                            reason=f"TMDB found no TV show for {title!r} year={year}")
            rows.append(row)
            continue
        tmdb_id = str(hit["id"])
        canonical = hit.get("name") or title
        first_air = (hit.get("first_air_date") or "")[:4]
        canonical_year = int(first_air) if first_air.isdigit() else year

        ep_meta = tmdb_episode(sess, tmdb_id, int(season), int(episode))
        ep_title = ep_meta.get("name") if ep_meta else None

        show_dir = _pick_show_folder(args.media_root, tmdb_id, canonical, canonical_year)
        # ep_meta has 'air_date' for this specific season/episode; use that
        # as the year hint when creating a new season folder.
        season_air = ep_meta.get("air_date") if ep_meta else None
        season_dir = _pick_season_folder(show_dir, int(season), season_air)
        target_name = episode_filename(canonical, canonical_year,
                                       int(season), int(episode),
                                       ep_title, src.suffix.lower())
        target = season_dir / target_name

        # If destination already exists OR another file in the season dir
        # represents the same episode, treat as a quality showdown.
        existing = target if target.exists() else find_existing_episode(
            season_dir, int(season), int(episode),
        )

        row = IntakeRow(
            src=src, status="planned", dest=target,
            show=show_dir.name, season=int(season), episode=int(episode),
        )

        if existing is None:
            row.status = "move"
            row.reason = "new episode"
            row.quality_after = quality_label(common.probe(src, ffprobe=ffprobe))
            if args.commit:
                if not show_dir.exists():
                    new_show_folders.add(show_dir)
                season_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(target))
        else:
            src_info = common.probe(src, ffprobe=ffprobe)
            old_info = common.probe(existing, ffprobe=ffprobe)
            new_key = quality_key(src_info)
            old_key = quality_key(old_info)
            row.quality_before = quality_label(old_info)
            row.quality_after = quality_label(src_info)
            if new_key > old_key:
                row.status = "replace"
                row.reason = f"new file is higher quality ({row.quality_after} > {row.quality_before})"
                if args.commit:
                    to_recycle(existing)
                    season_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(target))
            elif new_key < old_key:
                row.status = "discard_new"
                row.reason = f"existing file is higher quality ({row.quality_before} > {row.quality_after})"
                if args.commit:
                    to_recycle(src)
            else:
                row.status = "tie"
                row.reason = f"existing tied on quality; keeping existing, discarding new ({row.quality_after} == {row.quality_before})"
                if args.commit:
                    to_recycle(src)
        rows.append(row)

    # Write the report.
    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "status","reason","src","dest","show","season","episode",
            "quality_before","quality_after",
        ])
        w.writeheader()
        for r in rows:
            w.writerow({
                "status": r.status,
                "reason": _safe(r.reason),
                "src": _safe(r.src),
                "dest": _safe(r.dest) if r.dest else "",
                "season": r.season if r.season is not None else "",
                "episode": r.episode if r.episode is not None else "",
                "quality_before": r.quality_before,
                "quality_after": r.quality_after,
            })

    counts = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("\nSummary:")
    for st, n in sorted(counts.items()):
        print(f"  {st:15s}  {n}")
    print(f"  report:        {args.report}")

    if args.download_art and args.commit and new_show_folders:
        print(f"\nDownloading artwork for {len(new_show_folders)} new show folder(s)...")
        script = Path(__file__).resolve().parent / "download_artwork.py"
        for folder in new_show_folders:
            cmd = [sys.executable, str(script), "--from-folder", str(folder)]
            if args.no_art_videos:
                cmd.append("--no-videos")
            try:
                subprocess.run(cmd, check=False)
            except Exception as e:
                sys.stderr.write(f"  ! artwork download failed for {folder}: {e}\n")

    if not args.commit:
        print("\nDRY RUN -- no files were moved or deleted. Re-run with --commit.")


if __name__ == "__main__":
    main()
