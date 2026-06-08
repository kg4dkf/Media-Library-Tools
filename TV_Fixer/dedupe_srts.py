#!/usr/bin/env python3
"""
Find duplicate SRTs across your TV library and recycle the redundant ones.

For each show folder under <media-root>, groups .srt files by the
(season, episode) parsed from their filenames. Where multiple SRTs
exist for the same (S, E), picks ONE to keep and sends the others to
the Windows Recycle Bin.

Pick strategy:
  --prefer plex-position  (default, RECOMMENDED) keep the SRT that is in
                          a Season subfolder with the SAME basename as a
                          video file in that folder -- the one Plex
                          actually uses for matching.
  --prefer largest        keep the largest file -- more dialogue, but may
                          be in the wrong place for Plex to find.
  --prefer canonical      keep the one named exactly S<NN>E<NN>.srt
  --prefer newest         keep the most recently modified

Dry-run by default. Pass --commit to act.

Usage:
    python dedupe_srts.py "G:\\TV"                           # dry run
    python dedupe_srts.py "G:\\TV" --commit                   # apply
    python dedupe_srts.py "G:\\TV" --commit --prefer canonical
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

SE_RE = re.compile(
    r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})|"
    r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)"
)
CANONICAL_RE = re.compile(r"^S\d{2}E\d{2,3}\.srt$", re.IGNORECASE)


def _safe(s) -> str:
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def parse_se(name: str):
    m = SE_RE.search(name)
    if not m:
        return None
    if m.group(1):
        return (int(m.group(1)), int(m.group(2)))
    return (int(m.group(3)), int(m.group(4)))


def rank_largest(p: Path) -> Tuple[int, int, str]:
    """Sort key for 'prefer largest' -- bigger first, then canonical, then name."""
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    canonical = 1 if CANONICAL_RE.match(p.name) else 0
    return (-size, -canonical, p.name)


def rank_canonical(p: Path) -> Tuple[int, int, str]:
    """Sort key for 'prefer canonical' -- canonical first, then largest."""
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    canonical = 1 if CANONICAL_RE.match(p.name) else 0
    return (-canonical, -size, p.name)


def rank_newest(p: Path) -> Tuple[float, int, str]:
    """Sort key for 'prefer newest' -- most recent first, then canonical."""
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0
    canonical = 1 if CANONICAL_RE.match(p.name) else 0
    return (-mtime, -canonical, p.name)


_VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".m2ts",
               ".mpg", ".mpeg", ".wmv", ".webm"}


def rank_plex_position(p: Path) -> Tuple[int, int, int, int, str]:
    """Sort key for 'prefer plex-position' -- keep the SRT Plex would actually
    use.

    Ranking order (lower-is-better tuple components):
      1. Has a video sibling with matching basename (the gold standard).
      2. Is in a Season-NN-style subfolder (proper Plex layout).
      3. Path depth (deeper = more nested, usually means inside a season folder).
      4. Largest file size as tiebreaker.
      5. Filename for fully-deterministic ordering.
    """
    parent_name = p.parent.name
    in_season_folder = bool(re.match(r"^[Ss]eason\s+\d+", parent_name))
    # Check for a matching video sibling
    has_video_sibling = False
    stem = p.stem
    # Strip language suffix (.en, .es, etc.) for matching
    base_stem = re.sub(r"\.[a-z]{2,3}$", "", stem, flags=re.IGNORECASE)
    if p.parent.is_dir():
        for sibling in p.parent.iterdir():
            if (sibling.is_file()
                    and sibling.suffix.lower() in _VIDEO_EXTS
                    and (sibling.stem == base_stem or sibling.stem == stem)):
                has_video_sibling = True
                break
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return (
        -int(has_video_sibling),     # has matching video first
        -int(in_season_folder),      # in Season folder second
        -len(p.parts),               # deeper paths preferred
        -size,                       # then larger
        p.name,
    )


RANKERS = {
    "plex-position": rank_plex_position,
    "largest": rank_largest,
    "canonical": rank_canonical,
    "newest": rank_newest,
}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("media_root", type=Path)
    ap.add_argument("--prefer", choices=list(RANKERS.keys()),
                    default="plex-position",
                    help="Which file to KEEP per (S, E) group "
                         "(default: plex-position -- keep what Plex matches)")
    ap.add_argument("--commit", action="store_true",
                    help="Actually send losers to Recycle Bin. Default is dry-run.")
    ap.add_argument("--permanent", action="store_true",
                    help="Permanently delete (skip Recycle Bin). Requires --commit.")
    ap.add_argument("--report", type=Path, default=Path("dedupe_srts_report.csv"))
    args = ap.parse_args()

    if not args.media_root.is_dir():
        sys.exit(f"Not a directory: {args.media_root}")

    rank = RANKERS[args.prefer]

    plans = []  # list of dicts {keep, drop, show, season, episode, size}
    by_show: Dict[str, int] = defaultdict(int)
    total_dropped = 0
    total_bytes = 0

    for show in sorted(args.media_root.iterdir()):
        if not show.is_dir():
            continue
        groups: Dict[Tuple[int, int], List[Path]] = defaultdict(list)
        for srt in show.rglob("*.srt"):
            se = parse_se(srt.name)
            if se is None:
                continue
            groups[se].append(srt)
        for (season, episode), paths in groups.items():
            if len(paths) < 2:
                continue
            paths.sort(key=rank)
            keeper = paths[0]
            losers = paths[1:]
            for loser in losers:
                try:
                    sz = loser.stat().st_size
                except OSError:
                    sz = 0
                total_bytes += sz
                plans.append({
                    "show": show.name, "season": season, "episode": episode,
                    "keep": str(keeper), "drop": str(loser),
                    "drop_size": sz,
                })
                by_show[show.name] += 1
                total_dropped += 1

    if not plans:
        print("No duplicate SRTs found.")
        return

    print(f"Found {total_dropped:,} redundant SRTs across "
          f"{len(by_show)} show(s).")
    print(f"Total bytes to be freed: {total_bytes/1e6:.1f} MB")
    print(f"Strategy: keep --prefer {args.prefer}")
    print()
    print("Top 10 shows by redundant SRT count:")
    for show, n in sorted(by_show.items(), key=lambda x: -x[1])[:10]:
        print(f"  {n:>4}  {show}")

    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["show", "season", "episode", "keep", "drop", "drop_bytes"])
        for p in plans:
            w.writerow([_safe(p["show"]), p["season"], p["episode"],
                        _safe(p["keep"]), _safe(p["drop"]), p["drop_size"]])
    print(f"\nReport: {args.report}")

    print("\nSample of 5 actions:")
    for p in plans[:5]:
        print(f"  S{p['season']:02d}E{p['episode']:02d} of {p['show']}:")
        print(f"    KEEP   {Path(p['keep']).name}")
        print(f"    drop   {Path(p['drop']).name}  ({p['drop_size']:,} bytes)")

    if not args.commit:
        print("\nDRY RUN. Inspect the report, then re-run with --commit.")
        return

    if args.permanent:
        print("\n** PERMANENT DELETE -- files will NOT go to Recycle Bin. **")
        remover = lambda p: Path(p).unlink()
    else:
        try:
            from send2trash import send2trash
        except ImportError:
            sys.exit("pip install send2trash  (or use --permanent at your risk)")
        remover = lambda p: send2trash(str(p))

    dropped = 0
    failed = 0
    for p in plans:
        try:
            remover(p["drop"])
            dropped += 1
        except Exception as e:
            sys.stderr.write(f"FAIL: {_safe(p['drop'])}: {e}\n")
            failed += 1
    print(f"\nDone. Removed {dropped} files. Failed {failed}.")


if __name__ == "__main__":
    main()
