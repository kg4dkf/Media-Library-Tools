#!/usr/bin/env python3
"""
Apply Plex-style renames from identify_report.csv: move the video file into
the correct {show}/Season XX/ folder with the correct filename, rename any
associated SRT sidecar, and optionally embed metadata tags in the video.

Reads the report produced by identify.py and acts only on rows with
status='ok' by default (use --include-low-confidence to also handle
'low_confidence' rows for shows where you want to take the risk).

For each row:
  1. Look up canonical show metadata via TMDB (resolves season/episode title,
     air year for the folder name).
  2. Build destination path:
        <media-root>/Show Name (Year) {tmdb-NNNN}/Season XX/
            Show Name (Year) - SXXEYY - Episode Title.ext
  3. If the destination already has a different episode file, do a quality
     compare (height -> bitrate -> size). Higher quality wins; loser goes to
     Recycle Bin.
  4. Move the video. Rename matching SRT sidecars (S01E01.srt -> the new
     basename + .en.srt).
  5. If --write-tags: embed show/season/episode/title metadata into the file
     via mkvpropedit (MKV) or ffmpeg (MP4). Skipped on other containers.

Default is DRY-RUN; pass --commit to actually move things.

Usage:
    python apply_renames.py identify_report.csv ^
        --media-root "G:\\TV" --commit --write-tags

Requires:
    TMDB_API_KEY env var (for episode title + show metadata lookup)
    pip install send2trash
    mkvpropedit on PATH (for --write-tags on .mkv)
    ffmpeg on PATH    (for --write-tags on .mp4 and quality probe)
"""
from __future__ import annotations

import argparse
import csv
import json
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
TMDB_FOLDER_RE = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)
SE_RE = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})")


def _safe(s) -> str:
    """Round-trip strings through UTF-8 with errors='replace' so unpaired
    Windows surrogates in filenames don't crash csv/log writers."""
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


# ---------- TMDB ----------

def tmdb_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    s.params = {"api_key": api_key}
    return s


def tmdb_show(sess: requests.Session, tmdb_id: str) -> Optional[dict]:
    try:
        r = sess.get(f"{TMDB_API}/tv/{tmdb_id}", timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def tmdb_episode(sess: requests.Session, tmdb_id: str,
                 season: int, episode: int) -> Optional[dict]:
    try:
        r = sess.get(
            f"{TMDB_API}/tv/{tmdb_id}/season/{season}/episode/{episode}",
            timeout=30,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def tmdb_search(sess: requests.Session, name_hint: str) -> Optional[dict]:
    """Fallback when identify.py's show_norm doesn't resolve to a tmdb id."""
    try:
        r = sess.get(f"{TMDB_API}/search/tv",
                     params={"query": name_hint}, timeout=30)
        r.raise_for_status()
        results = r.json().get("results") or []
        return results[0] if results else None
    except requests.RequestException:
        return None


# ---------- Path resolution ----------

def safe_segment(s: str, maxlen: int = 100) -> str:
    out = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", s or "")
    out = re.sub(r"\s+", " ", out).strip(" .")
    return out[:maxlen] or "untitled"


def build_show_folder(name: str, year: Optional[int], tmdb_id: str) -> str:
    base = safe_segment(name)
    if year:
        base = f"{base} ({year})"
    return f"{base} {{tmdb-{tmdb_id}}}"


def build_episode_basename(name: str, year: Optional[int],
                           season: int, episode: int,
                           episode_title: Optional[str]) -> str:
    """The basename without extension. We'll add the source file's extension
    when we build the final path."""
    base = safe_segment(name)
    year_part = f" ({year})" if year else ""
    se = f"S{season:02d}E{episode:02d}"
    title = f" - {safe_segment(episode_title)}" if episode_title else ""
    return f"{base}{year_part} - {se}{title}"


def folder_tmdb_id(path: Path) -> Optional[str]:
    """Walk up the path looking for a folder with a {tmdb-NNNN} marker."""
    for parent in [path] + list(path.parents):
        m = TMDB_FOLDER_RE.search(parent.name)
        if m:
            return m.group(1)
    return None


_SEASON_DIR_RE = re.compile(r"^[Ss]eason\s+(\d{1,2})(?:\s+\((\d{4})\))?\s*$")


def pick_show_folder(media_root: Path, tmdb_id: str,
                     canonical_name: str, year: Optional[int]) -> Path:
    """Find an existing show folder by its {tmdb-NNNN} marker so we don't
    create a duplicate when TMDB's canonical name differs from the user's
    on-disk name. Falls back to a fresh canonical folder name."""
    if media_root.is_dir():
        for child in media_root.iterdir():
            if not child.is_dir():
                continue
            m = TMDB_FOLDER_RE.search(child.name)
            if m and m.group(1) == str(tmdb_id):
                return child
    return media_root / build_show_folder(canonical_name, year, tmdb_id)


def pick_season_folder(show_dir: Path, snum: int,
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


def find_show_by_normalized_name(media_root: Path,
                                 show_norm: str) -> Optional[Path]:
    """Look for a folder under media_root whose normalized name matches."""
    norm = re.sub(r"[^a-z0-9]+", "", show_norm.lower())
    for child in media_root.iterdir():
        if not child.is_dir():
            continue
        c_norm = re.sub(r"[^a-z0-9]+", "", child.name.lower())
        if c_norm == norm or c_norm.startswith(norm) or norm.startswith(c_norm[:max(8, len(norm))]):
            return child
    return None


# ---------- Quality compare ----------

def quality_key(info) -> Tuple[int, int, int]:
    if info is None:
        return (0, 0, 0)
    return (info.height or 0, info.vbitrate or 0, info.size or 0)


def quality_label(info) -> str:
    if info is None:
        return "unknown"
    return f"{info.height or 0}p {(info.vbitrate or 0)//1000}kbps {(info.size or 0)//(1024*1024)}MB"


# ---------- Metadata tag writing ----------

def write_mkv_tags(path: Path, show: str, season: int, episode: int,
                   title: str, summary: str) -> bool:
    if not shutil.which("mkvpropedit"):
        return False
    # mkvpropedit needs an XML tags file.
    xml = (
        f"<?xml version='1.0'?>\n"
        f"<Tags>\n"
        f" <Tag><Targets><TargetTypeValue>70</TargetTypeValue></Targets>\n"
        f"  <Simple><Name>TITLE</Name><String>{_xml_escape(show)}</String></Simple>\n"
        f" </Tag>\n"
        f" <Tag><Targets><TargetTypeValue>60</TargetTypeValue></Targets>\n"
        f"  <Simple><Name>PART_NUMBER</Name><String>{season}</String></Simple>\n"
        f" </Tag>\n"
        f" <Tag><Targets><TargetTypeValue>50</TargetTypeValue></Targets>\n"
        f"  <Simple><Name>TITLE</Name><String>{_xml_escape(title)}</String></Simple>\n"
        f"  <Simple><Name>PART_NUMBER</Name><String>{episode}</String></Simple>\n"
        f"  <Simple><Name>SYNOPSIS</Name><String>{_xml_escape(summary)}</String></Simple>\n"
        f" </Tag>\n"
        f"</Tags>\n"
    )
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                     encoding="utf-8") as f:
        f.write(xml)
        tags_path = f.name
    try:
        subprocess.run(
            ["mkvpropedit", str(path), "-t", f"all:{tags_path}",
             "--edit", "info", "--set", f"title={title}"],
            check=True, capture_output=True, timeout=60,
        )
        return True
    except Exception:
        return False
    finally:
        try:
            os.unlink(tags_path)
        except OSError:
            pass


def write_mp4_tags(path: Path, show: str, season: int, episode: int,
                   title: str, summary: str) -> bool:
    """Use ffmpeg -movflags use_metadata_tags to rewrite the file's MP4
    metadata in place. Because ffmpeg can't edit in place, this writes to
    a sibling file and atomic-renames."""
    if not shutil.which("ffmpeg"):
        return False
    tmp = path.with_suffix(path.suffix + ".tagging")
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-i", str(path),
        "-map", "0", "-c", "copy",
        "-metadata", f"title={title}",
        "-metadata", f"show={show}",
        "-metadata", f"season_number={season}",
        "-metadata", f"episode_id={episode}",
        "-metadata", f"episode_sort={episode}",
        "-metadata", f"synopsis={summary}",
        "-metadata", f"media_type=10",  # TV Show
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        tmp.replace(path)
        return True
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return False


def _xml_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- Main pipeline ----------

@dataclass
class Plan:
    src: Path
    dest: Path
    show_folder: Path
    season: int
    episode: int
    show_name: str
    episode_title: str
    summary: str
    srt_src: Optional[Path]
    srt_dest: Optional[Path]
    action: str          # 'move' | 'replace' | 'discard_new' | 'tie' | 'skip'
    reason: str = ""
    quality_before: str = ""
    quality_after: str = ""


def find_srt(video_path: Path, season: int, episode: int) -> Optional[Path]:
    """Look for a matching SRT sidecar near the video. Searches the same
    folder as the video and the show folder root (which is where
    fetch_subtitles.py writes them when --media-root is used)."""
    needle = f"S{season:02d}E{episode:02d}".lower()
    for candidate in (video_path.parent, video_path.parent.parent):
        if not candidate.is_dir():
            continue
        for p in candidate.glob("*.srt"):
            if needle in p.name.lower():
                return p
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("report", type=Path,
                    help="identify_report.csv from identify.py")
    ap.add_argument("--media-root", type=Path, required=True,
                    help="Library root, e.g. G:\\TV")
    ap.add_argument("--commit", action="store_true",
                    help="Actually move/rename. Default is dry-run.")
    ap.add_argument("--write-tags", action="store_true",
                    help="Also embed metadata into the video file "
                         "(MKV via mkvpropedit, MP4 via ffmpeg)")
    ap.add_argument("--include-low-confidence", action="store_true",
                    help="Also act on 'low_confidence' rows. Default: 'ok' only.")
    ap.add_argument("--plan-out", type=Path, default=Path("rename_plan.csv"))
    ap.add_argument("--log", type=Path, default=Path("apply_renames.log"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        sys.exit("Set TMDB_API_KEY env var")

    if not args.report.is_file():
        sys.exit(f"Report not found: {args.report}")

    ffprobe = common.require_tool("ffprobe")
    sess = tmdb_session(api_key)

    # Read identify report.
    rows = []
    with args.report.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    accept = {"ok"}
    if args.include_low_confidence:
        accept.add("low_confidence")
    rows = [r for r in rows if r["status"] in accept]
    if args.limit:
        rows = rows[: args.limit]
    print(f"Loaded {len(rows)} actionable rows from {args.report}")

    # Cache TMDB lookups
    show_cache: Dict[str, dict] = {}
    def show_meta(tmdb_id: str) -> Optional[dict]:
        if tmdb_id in show_cache:
            return show_cache[tmdb_id]
        d = tmdb_show(sess, tmdb_id)
        if d:
            show_cache[tmdb_id] = d
        return d

    plans: List[Plan] = []
    skipped_no_show = 0

    for r in tqdm(rows, desc="planning", unit="file"):
        src = Path(r["path"])
        if not src.is_file():
            continue
        try:
            season = int(r["season"])
            episode = int(r["episode"])
        except (ValueError, TypeError):
            continue

        # Resolve TMDB id: prefer the {tmdb-NNNN} marker on the source's
        # show folder if available; fall back to title search.
        tmdb_id = folder_tmdb_id(src)
        if not tmdb_id:
            show_norm = r.get("show") or ""
            existing_folder = find_show_by_normalized_name(args.media_root, show_norm)
            if existing_folder:
                tmdb_id = folder_tmdb_id(existing_folder)
            if not tmdb_id:
                hit = tmdb_search(sess, show_norm)
                if hit:
                    tmdb_id = str(hit["id"])
        if not tmdb_id:
            skipped_no_show += 1
            continue

        meta = show_meta(tmdb_id)
        if not meta:
            skipped_no_show += 1
            continue
        show_name = meta.get("name") or r.get("show") or "unknown"
        first_air = (meta.get("first_air_date") or "")[:4]
        year = int(first_air) if first_air.isdigit() else None

        ep_meta = tmdb_episode(sess, tmdb_id, season, episode)
        episode_title = ep_meta.get("name") if ep_meta else None
        summary = ep_meta.get("overview", "") if ep_meta else ""

        show_folder = pick_show_folder(args.media_root, tmdb_id, show_name, year)
        # Use ep_meta's air_date as a year hint if we need to create a new
        # season folder (most existing folders already have year suffix).
        season_air = ep_meta.get("air_date") if ep_meta else None
        season_folder = pick_season_folder(show_folder, season, season_air)
        basename = build_episode_basename(show_name, year, season, episode,
                                          episode_title)
        dest = season_folder / f"{basename}{src.suffix.lower()}"

        # SRT sidecar
        srt_src = find_srt(src, season, episode)
        srt_dest = (season_folder / f"{basename}.en.srt") if srt_src else None

        # Decide action.
        action = "move"
        reason = "new episode"
        qb = qa = ""
        # If src == dest we'd no-op
        if src.resolve() == dest.resolve():
            action = "skip"
            reason = "already at destination"
        elif dest.exists():
            old_info = common.probe(dest, ffprobe=ffprobe)
            new_info = common.probe(src, ffprobe=ffprobe)
            qb = quality_label(old_info)
            qa = quality_label(new_info)
            new_k = quality_key(new_info)
            old_k = quality_key(old_info)
            if new_k > old_k:
                action = "replace"
                reason = f"new is higher quality ({qa} > {qb})"
            elif new_k < old_k:
                action = "discard_new"
                reason = f"existing is higher quality ({qb} > {qa})"
            else:
                action = "tie"
                reason = f"quality tied; keeping existing ({qa})"

        plans.append(Plan(
            src=src, dest=dest, show_folder=show_folder,
            season=season, episode=episode,
            show_name=show_name,
            episode_title=episode_title or "",
            summary=summary,
            srt_src=srt_src, srt_dest=srt_dest,
            action=action, reason=reason,
            quality_before=qb, quality_after=qa,
        ))

    # Write plan CSV.
    with args.plan_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["action", "reason", "src", "dest", "srt_src", "srt_dest",
                    "show", "season", "episode", "quality_before", "quality_after"])
        for p in plans:
            w.writerow([
                p.action, p.reason, _safe(p.src), _safe(p.dest),
                _safe(p.srt_src) if p.srt_src else "",
                _safe(p.srt_dest) if p.srt_dest else "",
                _safe(p.show_name), p.season, p.episode,
                p.quality_before, p.quality_after,
            ])

    counts = {}
    for p in plans:
        counts[p.action] = counts.get(p.action, 0) + 1
    print(f"\nPlanned actions ({'COMMIT' if args.commit else 'DRY RUN'}):")
    for k, v in sorted(counts.items()):
        print(f"  {k:14s}  {v}")
    print(f"  skipped_no_show:  {skipped_no_show}")
    print(f"  plan:             {args.plan_out}")

    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")
        return

    # Acting on the plan.
    try:
        from send2trash import send2trash
    except ImportError:
        sys.exit("pip install send2trash")

    moved = 0
    tagged = 0
    with args.log.open("a", encoding="utf-8") as logf:
        for p in tqdm(plans, desc="apply", unit="file"):
            if p.action == "skip":
                continue
            try:
                if p.action == "replace":
                    send2trash(str(p.dest))
                if p.action in ("move", "replace"):
                    p.dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p.src), str(p.dest))
                    if p.srt_src and p.srt_dest:
                        try:
                            shutil.move(str(p.srt_src), str(p.srt_dest))
                        except Exception as e:
                            logf.write(f"srt_move_failed\t{_safe(p.srt_src)}\t{e}\n")
                    if args.write_tags:
                        ok = False
                        if p.dest.suffix.lower() == ".mkv":
                            ok = write_mkv_tags(p.dest, p.show_name, p.season,
                                                p.episode, p.episode_title, p.summary)
                        elif p.dest.suffix.lower() == ".mp4":
                            ok = write_mp4_tags(p.dest, p.show_name, p.season,
                                                p.episode, p.episode_title, p.summary)
                        if ok:
                            tagged += 1
                    moved += 1
                    logf.write(f"{p.action}\t{_safe(p.src)}\t{_safe(p.dest)}\n")
                elif p.action in ("discard_new", "tie"):
                    send2trash(str(p.src))
                    if p.srt_src:
                        send2trash(str(p.srt_src))
                    logf.write(f"{p.action}\t{_safe(p.src)}\t(recycled)\n")
                logf.flush()
            except Exception as e:
                logf.write(f"FAIL\t{_safe(p.src)}\t{e}\n")
                sys.stderr.write(f"FAIL: {_safe(p.src)}: {e}\n")
    print(f"\nDone. Moved/replaced: {moved}. Tags written: {tagged}.")
    print(f"  log: {args.log}")


if __name__ == "__main__":
    main()
