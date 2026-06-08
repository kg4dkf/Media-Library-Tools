#!/usr/bin/env python3
"""
Add (YYYY) year suffixes to season folders so they match the canonical
'Season NN (YYYY)' convention used throughout the library.

For each show folder under <media-root>:
  - Extract the TMDB id from the folder name's {tmdb-NNNN} marker.
  - Query TMDB for the show's season air dates.
  - For every 'Season NN' subfolder that LACKS a year suffix, rename it to
    'Season NN (YYYY)' using the season's air-date year.
  - Skip 'Season 00' / Specials by default -- they conventionally don't
    carry a year. Pass --include-specials to override.
  - If the year-suffixed folder already exists alongside the bare one,
    merge the bare folder's contents into the year-suffixed one and rmdir
    the empty bare folder.

Default is DRY-RUN. Pass --commit to act.

Requires TMDB_API_KEY env var.

Usage:
    python fix_season_year.py "G:\\TV"           # dry run
    python fix_season_year.py "G:\\TV" --commit  # apply
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from tqdm import tqdm


TMDB_API = "https://api.themoviedb.org/3"
TMDB_FOLDER_RE = re.compile(r"\{tmdb-(\d+)\}", re.IGNORECASE)

# 'Season NN' or 'Season N' with NO trailing year. We add a negative
# lookahead for a parenthesized 4-digit year suffix.
SEASON_BARE_RE = re.compile(r"^[Ss]eason\s+(\d{1,2})\s*$")

# Folders that ALREADY have the year suffix; leave alone.
SEASON_WITH_YEAR_RE = re.compile(r"^[Ss]eason\s+(\d{1,2})\s+\((\d{4})\)$")


def _safe(s) -> str:
    return str(s).encode("utf-8", errors="replace").decode("utf-8")


def tmdb_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.params = {"api_key": api_key}
    return s


def get_show(sess: requests.Session, tmdb_id: str) -> Optional[dict]:
    try:
        r = sess.get(f"{TMDB_API}/tv/{tmdb_id}", timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def season_year(show_data: dict, season_num: int) -> Optional[int]:
    for s in show_data.get("seasons") or []:
        if s.get("season_number") == season_num:
            air = s.get("air_date") or ""
            if len(air) >= 4 and air[:4].isdigit():
                return int(air[:4])
    return None


def merge_into(src: Path, dst: Path, actions: list,
               show: str, commit: bool) -> None:
    """Merge src/* into dst/, then rmdir src if possible."""
    for child in src.iterdir():
        target = dst / child.name
        if target.exists():
            target = dst / f"_dup_{child.name}"
        actions.append((show, "merge", str(child), str(target)))
        if commit:
            shutil.move(str(child), str(target))
    actions.append((show, "rmdir", str(src), ""))
    if commit:
        try:
            src.rmdir()
        except OSError as e:
            actions.append((show, "rmdir_failed", str(src), str(e)))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("media_root", type=Path)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--report", type=Path, default=Path("season_year_fix_report.csv"))
    ap.add_argument("--include-specials", action="store_true",
                    help="Also add year suffix to Season 00 (default: skip)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N show folders (0 = unlimited)")
    args = ap.parse_args()

    if not args.media_root.is_dir():
        sys.exit(f"Not a directory: {args.media_root}")

    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        sys.exit("Set TMDB_API_KEY env var")
    sess = tmdb_session(api_key)

    shows = [p for p in sorted(args.media_root.iterdir()) if p.is_dir()]
    if args.limit:
        shows = shows[: args.limit]

    actions: list = []
    by_kind: Dict[str, int] = defaultdict(int)
    skipped_no_tmdb = 0
    skipped_no_data = 0

    for show_dir in tqdm(shows, desc="shows", unit="show"):
        m = TMDB_FOLDER_RE.search(show_dir.name)
        if not m:
            skipped_no_tmdb += 1
            continue
        tmdb_id = m.group(1)
        show_data = get_show(sess, tmdb_id)
        if not show_data:
            skipped_no_data += 1
            continue

        for child in sorted(show_dir.iterdir()):
            if not child.is_dir():
                continue
            bare = SEASON_BARE_RE.match(child.name)
            if not bare:
                continue
            snum = int(bare.group(1))
            if snum == 0 and not args.include_specials:
                actions.append((show_dir.name, "skip_specials", str(child), ""))
                by_kind["skip_specials"] += 1
                continue
            year = season_year(show_data, snum)
            if year is None:
                actions.append((show_dir.name, "skip_no_year", str(child),
                                f"S{snum:02d} has no air_date on TMDB"))
                by_kind["skip_no_year"] += 1
                continue

            new_name = f"Season {snum:02d} ({year})"
            target = show_dir / new_name
            if target.exists():
                # Both exist -- merge bare into year-suffixed.
                merge_into(child, target, actions, show_dir.name, args.commit)
                by_kind["merge_into_yeared"] += 1
            else:
                actions.append((show_dir.name, "rename_folder",
                                str(child), str(target)))
                by_kind["rename_folder"] += 1
                if args.commit:
                    try:
                        child.rename(target)
                    except OSError as e:
                        actions.append((show_dir.name, "rename_failed",
                                        str(child), str(e)))
                        by_kind["rename_failed"] += 1

    # Report
    with args.report.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["show", "action", "source", "destination"])
        for row in actions:
            w.writerow([_safe(c) for c in row])

    print(f"\nActions {'committed' if args.commit else 'planned'}:")
    for k, v in sorted(by_kind.items()):
        print(f"  {k:24s}  {v}")
    print(f"  total:                    {sum(by_kind.values())}")
    print(f"  shows skipped (no tmdb):  {skipped_no_tmdb}")
    print(f"  shows skipped (no data):  {skipped_no_data}")
    print(f"  report:                   {args.report}")
    if not args.commit:
        print("\nDRY RUN. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
